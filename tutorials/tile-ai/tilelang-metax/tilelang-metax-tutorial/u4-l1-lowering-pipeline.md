# 从 DSL 到 IR 的 lowering 流程

## 1. 本讲目标

本讲是进阶层（U4 编译流水线）的第一讲，负责把「用户写好的 kernel」与「后端能编译的代码」之间的那条鸿沟打通。

读完本讲，你应该能够：

- 说清楚 `tilelang.engine.lower` 这个**编译主入口**的输入、输出与它在整个编译链中的位置。
- 解释为什么一个 TileLang kernel 会被拆成 **host IR**（CPU 侧启动代码）和 **device IR**（GPU 侧 kernel 体），以及拆分是靠什么属性完成的。
- 理解 host/device codegen 是如何**按 target 动态分派**的（cuda/hip/maca 各注册自己的 codegen 与 pass 流水线）。
- 掌握 postproc 回调机制：它是注册在 Python 侧、却被 C++ codegen 调用的「源码拦截点」，以及 metax 分支为此新增的 `maca` 回调。
- 知道在下译**之前**还有一道与后端无关的**语义检查**。

本讲承接 u3-l2（JIT 与 kernel 对象）——你已经知道 `.compile()` 最终委托给 `KernelCache` 做「下译、codegen、包装」，本讲就钻进这个「下译」内部看个究竟。

## 2. 前置知识

- **TIR / PrimFunc**：TileLang 复用 TVM 的中间表示。`@T.prim_func` 在编译期把 Python 函数重写成一棵 TIR `PrimFunc`（见 u2-l1）。本讲的输入就是这棵 `PrimFunc`。
- **IRModule**：一组 `PrimFunc` 的容器。下译过程本质上是在反复变换 `IRModule`。
- **target**：回答「kernel 编给谁」（cuda/hip/maca/metal/llvm…），由 kind + attrs 组成（见 u3-l1）。
- **host 与 device**：GPU 程序天然分两段——**host 代码**跑在 CPU 上，负责准备参数、设置 grid/block、发起 kernel 启动；**device 代码**才是真正跑在 GPU 上的 kernel 体。TileLang 下译的最终产物就是这两段。
- **pass（变换 pass）**：编译器里对 IR 做一次确定变换的步骤，如 `LowerTileOp`、`LayoutInference`。多个 pass 串成 pass pipeline。

一个直觉比喻：`lower` 像一个「翻译车间调度员」。你递给它一份用 TileLang DSL 写的规格（PrimFunc）和目标语言（target），它先做体检（语义检查），再按 target 选一条流水线（pass pipeline）把规格逐步降级成低级 IR，然后把结果按 host/device 两本「册子」分开装订，最后交给 codegen 印成源码。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
| --- | --- |
| [tilelang/engine/lower.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py) | **编译主入口**：`lower`、`lower_to_host_device_ir`、`device_codegen`、`host_codegen`，以及 cuda/hip/maca 的编译/校验回调实现。 |
| [tilelang/engine/callback.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/callback.py) | postproc 回调的**注册 API**：`register_cuda_postproc` / `register_hip_postproc` / `register_maca_postproc` / `register_c_postproc`。 |
| [tilelang/engine/__init__.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/__init__.py) | engine 包对外导出 `lower`、`is_device_call`、各 `register_*_postproc`。 |
| [tilelang/engine/param.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/param.py) | `CompiledArtifact`：下译产物的容器（host_mod/device_mod/kernel_source 等）。 |
| [tilelang/engine/semantic_check.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/semantic_check.py) | `PreLowerSemanticCheck`：下译前的后端无关校验。 |
| [tilelang/backend/pass_pipeline/pipeline.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/pass_pipeline/pipeline.py) | `PassPipeline` 抽象与 `resolve_pipeline`：按 target 名取流水线。 |
| [tilelang/backend/device_codegen.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/device_codegen.py) | device codegen 注册表与 `resolve_device_codegen`。 |
| [tilelang/cuda/pipeline.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py) | cuda 的 pass 流水线 `CUDAPassPipelineBody`。 |
| [tilelang/maca/pipeline.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py) | **metax 分支核心**：maca 的 pass 流水线 `MACAPassPipelineBody`。 |
| [src/cuda/codegen/rt_mod_cuda.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/rt_mod_cuda.cc) / [src/maca/codegen/rt_mod_maca.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc) | C++ 侧 codegen：**调用** postproc 回调的地方。 |

> 阅读建议：先精读 `lower.py` 的两个顶层函数 `lower` 与 `lower_to_host_device_ir`，建立全局；再按需深入 codegen 注册表与各 backend 的 pipeline。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**lower 编译主入口**、**host/device 拆分与 codegen**、**postproc 回调机制**、**下译前的语义检查**。

### 4.1 lower：编译主入口

#### 4.1.1 概念说明

`lower`（下译）是 TileLang 编译器对外的**主入口函数**：吃进一份用 TIR 描述的 `PrimFunc`（或 `IRModule`）和一个 target，吐出一个 `CompiledArtifact`——里面装着拆好的 host/device IR、生成的 kernel 源码字符串、参数签名等。

它在整个链路中的位置（承接 u3-l2）：

```
@T.prim_func            # u2-l1：构造 PrimFunc
    │
    ▼
tilelang.compile / .compile()    # u3-l2：JIT 收口，进 KernelCache
    │
    ▼
tilelang.lower(...)      ◄── 本讲：下译主入口
    │
    ▼
CompiledArtifact ──► adapter 包装 ──► JITKernel（可被 torch 张量调用）
```

JIT 路径里对 `lower` 的真实调用在 [tilelang/jit/kernel.py:258-264](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L258-L264)，它在 `PassContext` 与 target 上下文里发起下译。

#### 4.1.2 核心流程

`lower` 本身很短，它只做「编排」，真正的活分派给三个子步骤：

```
lower(func_or_mod, target, target_host,
      enable_host_codegen, enable_device_compile)
  │
  ├─① lower_to_host_device_ir(...)   # 下译 + 拆分 → host_mod, device_mod
  │        （详见 4.2）
  │
  ├─② device_codegen(device_mod)      # device 侧 codegen → 取 kernel_source
  │     或 device_codegen_without_compile（取决于 enable_device_compile）
  │
  └─③ （可选）host_codegen(host_mod)   # 仅当 enable_host_codegen=True
  │
  └─► 返回 CompiledArtifact
```

两个布尔开关控制下译「走多远」：

- `enable_device_compile`（默认 `False`）：是否真的把 device 源码编译成二进制（cubin/mcbin）。关掉时只产出源码字符串，真正的设备编译延后到 JIT 适配层按需触发——这正是两级缓存（u3-l2）能复用源码的原因。
- `enable_host_codegen`（默认 `False`）：是否在 `lower` 内完成 host codegen。默认关闭，因为 JIT 有自己的 host codegen 实现（见函数 docstring）。

#### 4.1.3 源码精读

`lower` 的完整实现：[tilelang/engine/lower.py:348-393](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py#L348-L393)。关键骨架：

```python
def lower(func_or_mod, target="auto", target_host=None,
          runtime_only=False,
          enable_host_codegen=False,
          enable_device_compile=False) -> CompiledArtifact:

    # ① 下译并拆分 host/device（4.2 详解）
    host_mod, device_mod, params, target, target_host = lower_to_host_device_ir(
        func_or_mod=func_or_mod, target=target,
        target_host=target_host, runtime_only=runtime_only)

    # ② device codegen：按开关决定是否真编译
    codegen_mod = (device_codegen(device_mod, target)
                   if enable_device_compile
                   else device_codegen_without_compile(device_mod, target))
    kernel_source = codegen_mod.inspect_source()   # 取出生成的源码字符串

    # ③ 可选 host codegen
    if enable_host_codegen:
        host_mod = host_codegen(host_mod, target_host, target=target)
        host_mod.import_module(codegen_mod)
        return CompiledArtifact(host_mod, device_mod, params, kernel_source,
                                rt_mod=host_mod, target=target, target_host=target_host)

    return CompiledArtifact(host_mod, device_mod, params, kernel_source,
                            target=target, target_host=target_host)
```

返回类型 `CompiledArtifact` 定义在 [tilelang/engine/param.py:153-166](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/param.py#L153-L166)，它把一次下译的所有产物打包：

```python
@dataclass
class CompiledArtifact:
    host_mod: tvm.IRModule          # host 侧 IR
    device_mod: tvm.IRModule        # device 侧 IR
    params: list[KernelParam]       # 参数签名（dtype/shape）
    kernel_source: str              # 生成的设备源码（CUDA/HIP/MACA…）
    rt_mod: tvm.runtime.Module | None = None   # 运行期模块（lazy）
    target: Target | None = None
    target_host: Target | None = None
```

engine 包通过 [tilelang/engine/__init__.py:2-9](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/__init__.py#L2-L9) 把 `lower`、`is_device_call`、四个 `register_*_postproc` 导出；顶层 `tilelang/__init__.py` 再把它们挂到 `tilelang.lower` 等名字上，所以 `tilelang.lower(...)` 与 `tilelang.engine.lower(...)` 是同一个函数。

#### 4.1.4 代码实践

**实践目标**：确认 `lower` 在 JIT 链路中确实被调用，并看清它的实参。

1. 打开 [tilelang/jit/kernel.py:253-264](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L253-L264)。
2. 阅读 `with (...):` 上下文块，注意它同时打开了 `jit_phase("lower", ...)`、`tvm.transform.PassContext(...)` 和 `self.target` 三个上下文管理器——说明下译是在「带 pass 配置 + 绑定当前 target」的环境里发生的。
3. 记录传给 `tilelang.lower(...)` 的 5 个实参：`tilelang_func`、`target`、`target_host`、`enable_host_codegen`、`enable_device_compile`。

**需要观察的现象**：`lower` 被包在一个带 `PassContext(opt_level=3, config=pass_configs)` 的上下文里，说明下译过程中所有 pass 都会读到这份 `pass_configs`——这正是 postproc/编译回调里能取到 pass 配置的根源（见 4.3）。

**预期结果**：能在本地源码中定位到这一处调用，并用自己的话解释为什么 JIT 要用上下文管理器包裹它。（纯源码阅读型实践，无需运行。）

#### 4.1.5 小练习与答案

**练习 1**：`lower` 的两个布尔参数默认值是什么？为什么 JIT 默认不开 `enable_device_compile`？

> **答案**：默认都是 `False`。不开 `enable_device_compile` 是为了只产出设备**源码**而不立即调用 nvcc/mxcc 编译二进制，真正的设备编译延后到 JIT 适配层按需进行，从而与两级缓存配合、避免重复编译。

**练习 2**：`CompiledArtifact` 里哪个字段保存了「最终交给后端编译器的那段 CUDA/MACA 源码」？

> **答案**：`kernel_source: str`。它在 `lower` 中由 `codegen_mod.inspect_source()` 取得。

---

### 4.2 host/device 拆分与 codegen

#### 4.2.1 概念说明

GPU kernel 下译有一个贯穿始终的设计：**一份输入最终要变成两份产物**——host IR（CPU 侧）和 device IR（GPU 侧）。这是因为 GPU 程序本质上是「CPU 发起 + GPU 执行」的协处理器模型。

`lower_to_host_device_ir` 就是负责「下译 + 拆分」的那一步。它做四件事：

1. 解析 target（含 auto 检测，见 u3-l1）。
2. 下译前语义检查（4.4 详解）。
3. 按 target 取一条 pass 流水线，把高层 Tile IR 一路降级成低级 IR。
4. 用 `Filter` 把降级后的模块按函数属性拆成 host/device 两个 `IRModule`。

#### 4.2.2 核心流程

```
lower_to_host_device_ir(func_or_mod, target, target_host, runtime_only)
  │
  ├─ 输入归一化：PrimFunc → IRModule；target 字符串 → Target 对象
  │   （determine_target，u3-l1）
  │
  ├─ PreLowerSemanticCheck(mod)        # 4.4：后端无关体检
  │
  ├─ pipeline = resolve_pipeline(target)   # 按 target.kind.name 取流水线
  ├─ mod = pipeline.lower(mod, target)     # ★ 跑完整后端 pass 序列
  │       （内含 SplitHostDevice、LowerDeviceKernelLaunch，
  │        生成带 calling_conv 标记的 host/device 函数）
  │
  ├─ host_mod   = Filter(is_host_call)(mod)     # 留下 host 函数
  ├─ device_mod = Filter(is_device_call)(mod)   # 留下 device 函数
  │
  └─► return host_mod, device_mod, params, target, target_host
```

**靠什么区分 host/device？** 靠函数属性 `calling_conv`（调用约定）。device kernel 的 `calling_conv` 等于 `DEVICE_KERNEL_LAUNCH`。判定函数：[tilelang/engine/lower.py:29-47](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py#L29-L47)。

```python
def has_device_kernel_launch(attrs) -> bool:
    return bool(attrs and "calling_conv" in attrs
                and attrs["calling_conv"] == CallingConv.DEVICE_KERNEL_LAUNCH)

def is_device_call(func: tirx.PrimFunc):
    return has_device_kernel_launch(func.attrs)
```

而 `calling_conv = DEVICE_KERNEL_LAUNCH` 这个标记，是流水线里的 `LowerDeviceKernelLaunch` pass 打上去的；`SplitHostDevice` pass 则负责把 host/device 函数在 IR 层面分离开。所以**先有 pass 打标记，后有 Filter 物理拆分**。

#### 4.2.3 源码精读

`lower_to_host_device_ir` 全文：[tilelang/engine/lower.py:310-345](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py#L310-L345)。

```python
def lower_to_host_device_ir(func_or_mod, target="auto",
                            target_host=None, runtime_only=False):
    mod = func_or_mod
    params = None
    if isinstance(func_or_mod, tirx.PrimFunc):
        func = func_or_mod
        params = extrac_params(func) if not runtime_only else None
        mod = tvm.IRModule({func.attrs["global_symbol"]: func})

    if isinstance(target, str):
        target = determine_target(target)            # auto 检测（u3-l1）

    target_host = canon_target_host(target, target_host)
    target_host = tvm.target.Target(target_host)
    target = tvm.target.Target(target, target_host)

    _is_host_call   = get_host_call(is_device_c=is_cpu_device_backend(target))
    _is_device_call = get_device_call(is_device_c=is_cpu_device_backend(target))

    PreLowerSemanticCheck(mod)                       # 4.4 语义检查
    pipeline = resolve_pipeline(target)              # 按 target 取流水线
    mod = pipeline.lower(mod, target)                # ★ 跑 pass 序列

    host_mod   = tirx.transform.Filter(_is_host_call)(mod)
    device_mod = tirx.transform.Filter(_is_device_call)(mod)
    return host_mod, device_mod, params, target, target_host
```

注意 `is_cpu_device_backend(target)` 这个分支（[lower.py:25-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py#L25-L26)）：当 target 是 `c`（CPU 后端）时，host/device 的判定逻辑不同，因为 CPU 没有「独立设备」概念，device 调用用 `C_PACKED_FUNC` 约定区分（见 [lower.py:34-43](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py#L34-L43)）。本讲聚焦 GPU 后端，记住「GPU 后端靠 `DEVICE_KERNEL_LAUNCH` 判定」即可。

**流水线怎么取？** `resolve_pipeline` 极简：[tilelang/backend/pass_pipeline/pipeline.py:46-48](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/pass_pipeline/pipeline.py#L46-L48)。

```python
def resolve_pipeline(target: Target) -> PassPipeline:
    return get_pipeline(target.kind.name)   # 直接用 target kind 名查表
```

每个后端在自己的 `pipeline.py` 末尾注册，名字必须等于 `target.kind.name`：

- cuda：[tilelang/cuda/pipeline.py:257-259](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L257-L259) 注册 `PassPipeline("cuda", CUDAPassPipelineBody)`。
- maca：[tilelang/maca/pipeline.py:151-153](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py#L151-L153) 注册 `PassPipeline("maca", MACAPassPipelineBody)`。

`CUDAPassPipelineBody`（[tilelang/cuda/pipeline.py:141-254](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L141-L254)）是一长串 pass，其中与本讲直接相关的两步是：

- `SplitHostDevice()`（[pipeline.py:213](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L213)）：把 host/device 函数在 IR 层分离。
- `LowerDeviceKernelLaunch()`（[pipeline.py:248](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L248)）：给 device 函数打上 `DEVICE_KERNEL_LAUNCH` 调用约定。

（流水线里其它 pass 如 `LowerTileOp`、`LayoutInference`、`InjectSoftwarePipeline` 分别在 u4-l2/u4-l3/u4-l4 详讲。）

**device codegen 与 host codegen 的分派** 也是按 target 查注册表：

- device：`device_codegen` 调 `resolve_device_codegen(target).lower(mod, target, compile_device=...)`（[lower.py:300-307](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py#L300-L307)）。`resolve_device_codegen`（[device_codegen.py:102-110](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/device_codegen.py#L102-L110)）按 `target.kind.name` 查 `DeviceCodegen` 表。maca 后端的注册在 [tilelang/maca/codegen.py:12-21](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/codegen.py#L12-L21)，把 `build` / `build_without_compile` 绑到 C++ 全局函数 `target.build.tilelang_maca` / `target.build.tilelang_maca_without_compile`。
- host：`host_codegen`（[lower.py:266-290](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py#L266-L290)）跑一组 TVM 标准 host pass（`BindTarget`、`LowerTVMBuiltin`、`LowerIntrin`、`CombineContextCall`…），再交 `resolve_host_codegen(target_host).lower(...)`。

下译前 device 模块还会过一道统一的预处理 `_prepare_device_codegen_mod`（[lower.py:293-297](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py#L293-L297)）：`LowerIntrin` → `Simplify` → `HoistBroadcastValues`。

> **metax 视角**：MACA 流水线 [tilelang/maca/pipeline.py:77-148](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py#L77-L148) 与 cuda 高度同构（大量复用 `tilelang.cuda.transform.*` pass），关键差异是在 [pipeline.py:124](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py#L124) 多插了一步 `tilelang.maca.transform.LowerMACAIntrin()`——把 MACA 专属 intrinsic 降级为底层调用。这条「公共骨架 + 后端专属 pass」的模式正是 u7 系列要深挖的内容。

#### 4.2.4 代码实践

**实践目标**：亲眼看到一次下译把单个 PrimFunc 拆成了两个 IRModule。

下面是一段**示例代码**（非项目原有文件），可在装好 tilelang 的环境里运行：

```python
# 示例代码：直接调用 lower 的内部入口，观察 host/device 拆分
import tilelang.language as T
from tilelang.engine.lower import lower_to_host_device_ir

@T.prim_func
def add_kernel(A: T.Tensor((128, 128), "float32"),
               B: T.Tensor((128, 128), "float32"),
               C: T.Tensor((128, 128), "float32")):
    with T.Kernel(T.ceildiv(128, 16), T.ceildiv(128, 16), threads=128) as (bx, by):
        A_shared = T.alloc_shared((16, 16), "float32")
        T.copy(A[bx*16:(bx+1)*16, by*16:(by+1)*16], A_shared)
        # ...（省略计算与写回，仅为触发完整下译）

host_mod, device_mod, params, target, target_host = lower_to_host_device_ir(
    add_kernel, target="cuda"
)

print("host functions  :", [name for name, _ in host_mod.functions.items()])
print("device functions:", [name for name, _ in device_mod.functions.items()])
print("params          :", [(str(p.dtype), p.shape) for p in params])
```

**操作步骤**：

1. 保存为 `inspect_lower.py`，在仓库根目录运行 `python inspect_lower.py`（无 GPU 时可把 target 换成 `"c"` 或借助 `tilelang_maca_without_compile` 只看源码）。
2. 关注 `host_mod` 与 `device_mod` 各自包含哪些函数。

**需要观察的现象**：`device_mod` 里应能看到带 `DEVICE_KERNEL_LAUNCH` 调用约定的 kernel 函数；`host_mod` 里是启动/打包相关的 host 函数；二者函数名/数量不同。

**预期结果**：两个 IRModule 的函数集合互补——合起来等于下译后的完整模块。若因环境缺失无法运行，标注「待本地验证」并改为阅读 [lower.py:342-343](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py#L342-L343) 的两行 `Filter` 代码理解拆分逻辑。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Filter` 拆分发生在 `pipeline.lower(...)` **之后**而不是之前？

> **答案**：因为区分 host/device 的依据是函数的 `calling_conv` 属性，而这个属性是流水线内部的 `LowerDeviceKernelLaunch` pass 才打上去的。必须先跑完流水线、让标记就位，才能用 `Filter` 正确地物理拆分。

**练习 2**：`resolve_pipeline(target)` 用什么键查流水线表？如果新增一个 target kind 叫 `mygpu`，需要在哪注册？

> **答案**：用 `target.kind.name` 查表。需要在 `mygpu` 后端的 `pipeline.py` 里调用 `register_pipeline(PassPipeline("mygpu", MyGPUPassPipelineBody))`，名字必须等于 `"mygpu"`（见 u9-l1）。

---

### 4.3 postproc 回调机制

#### 4.3.1 概念说明

postproc（后处理）回调是一个**源码拦截点**：当 C++ codegen 把 device IR 印成一段 CUDA/HIP/MACA 源码字符串后、**在真正交给 nvcc/hipcc/mxcc 编译之前**，会先回头问一句「Python 侧有没有注册过对应的 postproc 函数？有的话把源码交给它处理一遍」。

它的设计很巧妙：

- **注册在 Python**：用户用 `register_cuda_postproc(func)` 等 API 注册，本质是往 TVM 全局函数表里塞一个具名可调用对象。
- **调用在 C++**：C++ codegen 用固定的全局函数名（如 `tilelang_callback_cuda_postproc`）反查并调用它。
- **默认不注册**：tilelang 自身默认**不**注册任何 postproc。它是留给用户/测试的扩展点；不注册时 C++ 侧的 `GetGlobal` 返回空，直接跳过。

这给了用户在不改编译器的前提下「改写最终源码」的能力——比如注入一段手写代码、加注释、做统计。

#### 4.3.2 核心流程

注册侧（Python）：

```
register_maca_postproc(func)
    │  tvm_ffi.register_global_func("tilelang_callback_maca_postproc", f=func)
    ▼
   TVM 全局函数表：{ "tilelang_callback_maca_postproc": func, ... }
```

调用侧（C++ codegen，以 maca 为例）：

```
BuildTileLangMACA(mod, target):
    code = cg.Finish()                       # 印出 MACA 源码字符串
    if f = GetGlobal("tilelang_callback_maca_postproc"):   # 反查全局表
        code = f(code, target)               # ★ 把源码交给 Python 回调改写
    mcir = GetGlobal("tilelang_callback_maca_compile")(code, target, ...)
                # 再交给 mxcc 编译（u3-l3）
```

签名约定：所有 postproc 回调都是 `func(code: str, target: Target) -> str`——吃进源码与 target，吐出（可能改写过的）源码。

> 同源地，`tilelang_callback_*_compile` 是「编译回调」（调 nvcc/hipcc/mxcc），`tilelang_callback_*_validate` 是「校验回调」（下译后结构检查）。本讲聚焦 postproc，另外两类在 u5/u7 详讲。

#### 4.3.3 源码精读

注册 API 全部集中在 [tilelang/engine/callback.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/callback.py)，每个后端一个函数，区别仅在注册的全局函数名：

| 注册函数 | 全局函数名 | 代码位置 |
| --- | --- | --- |
| `register_cuda_postproc` | `tilelang_callback_cuda_postproc` | [callback.py:8-16](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/callback.py#L8-L16) |
| `register_hip_postproc` | `tilelang_callback_hip_postproc` | [callback.py:19-27](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/callback.py#L19-L27) |
| `register_maca_postproc` | `tilelang_callback_maca_postproc` | [callback.py:30-38](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/callback.py#L30-L38) |
| `register_c_postproc` | `tilelang_callback_c_host_postproc` | [callback.py:41-53](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/callback.py#L41-L53) |

maca 版本实现（最具本分支特色）：

```python
def register_maca_postproc(func: Callable[[str, Target], str], override: bool = True):
    """Register a post-processing function for MACA code generation."""
    tvm_ffi.register_global_func("tilelang_callback_maca_postproc", f=func, override=override)
```

注意 `override=True` 默认值——后注册的会覆盖先注册的，方便实验时反复替换。

C++ 侧的调用点（maca）：[src/maca/codegen/rt_mod_maca.cc:121-125](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L121-L125)。

```cpp
std::string code = cg.Finish();
if (const auto f = ffi::Function::GetGlobal("tilelang_callback_maca_postproc")) {
  code = (*f)(code, target).cast<std::string>();   // ★ 把源码交给 Python 回调
}
```

对照 cuda 的同一段：[src/cuda/codegen/rt_mod_cuda.cc:117-120](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/rt_mod_cuda.cc#L117-L120)。两者结构完全一致，只是全局函数名不同。**注意**：即使在 `without_compile`（只产出源码、不编译二进制）的路径里，postproc 照样会被调用（见 [rt_mod_maca.cc:163-167](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L163-L167)、[rt_mod_cuda.cc:160-163](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/rt_mod_cuda.cc#L160-L163)）——所以你能在没有 GPU/SDK 的机器上、只通过 `kernel_source` 观察到自己注册的 postproc 是否生效。

此外，[lower.py:72-100](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py#L72-L100) 还用同一个 `@tvm_ffi.register_global_func` 装饰器一次性注册了 `tilelang_callback_cuda_validate` 与 `tilelang_callback_maca_validate`（校验回调，验证外部源码 kernel 的 `global_symbol` 与 `__global__` 入口名是否一致），体现了「Python 注册、C++ 按名调用」这套机制的通用性。

#### 4.3.4 代码实践

**实践目标**：注册一个 postproc 回调，在生成的源码顶部插一行注释标记，并验证它确实生效。

```python
# 示例代码：注册 maca postproc，注入标记行
import tilelang
import tilelang.language as T
from tilelang.engine import register_maca_postproc

MARK = "// === postproc hooked by my-tutorial ==="

def my_postproc(code, target):
    # code 是 C++ codegen 刚印出的 MACA 源码字符串
    return MARK + "\n" + code

register_maca_postproc(my_postproc, override=True)

# ...（用 target={"kind":"maca"} 编译任意小 kernel，见 u3-l3）
# kernel = my_kernel.compile(M=128, block_M=16, ...)
# print(kernel.get_kernel_source().splitlines()[0])   # 应为 MARK
```

**操作步骤**：

1. 在 `register_*_postproc` 之后、`.compile()` 之前完成注册。
2. 编译后用 `kernel.get_kernel_source()`（u3-l2）取出生成的源码，看首行。

**需要观察的现象**：生成的源码首行出现你注入的 `MARK` 注释。无设备时，借助 `tilelang_maca_without_compile`（u3-l3）只取源码也能看到——因为 postproc 在 without_compile 路径同样触发。

**预期结果**：源码首行正是 `MARK`。若环境无 MACA SDK/GPU，可改用 `register_cuda_postproc` 并把 target 设为 `"cuda"` 在纯源码模式下观察（待本地验证）。

> 参考官方测试 [testing/python/jit/test_tilelang_jit_callback.py:88-98](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/jit/test_tilelang_jit_callback.py#L88-L98)，里面就是用同样的 `tilelang_callback_{cuda,hip,maca}_postproc` 注册方式做断言的。

#### 4.3.5 小练习与答案

**练习 1**：如果你**没有**注册任何 postproc，C++ codegen 会报错吗？为什么？

> **答案**：不会报错。C++ 侧用的是 `if (const auto f = GetGlobal(...))` 守卫，取不到就跳过整段。postproc 是可选扩展点，tilelang 默认不注册。

**练习 2**：postproc 回调与 `tilelang_callback_*_compile` 编译回调的职责分别是什么？谁先执行？

> **答案**：postproc 负责**改写源码字符串**（输入输出都是 str），compile 负责**把源码编译成二进制**（输入 str、输出 cubin/mcbin 字节）。先执行 postproc 改写源码，再把改写后的源码交给 compile——顺序见 [rt_mod_maca.cc:121-139](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L121-L139)。

---

### 4.4 下译前的语义检查

#### 4.4.1 概念说明

在下译（跑那一长串 pass）**之前**，TileLang 先做一次「体检」——`PreLowerSemanticCheck`。它的特点是**后端无关**：不关心你是 cuda 还是 maca，只检查 TileLang DSL 层面的结构性约束是否满足。一旦在这里就发现问题，可以提前抛出清晰的错误，避免把非法 IR 送进 pass 流水线、最后在某个深层 pass 里爆出难以理解的失败。

#### 4.4.2 核心流程

```
PreLowerSemanticCheck(mod):
    if TL_DISABLE_PRELOWER_SEMANTIC_CHECK 为 True: 直接返回（可关闭）
    if TL_AST_PRINT_ENABLE 为 True: ASTPrinter 打印 AST（调试用）
    NestedLoopChecker()(mod)      # 检查嵌套循环结构合法性
    FragmentLoopChecker()(mod)    # 检查 fragment 相关循环约束
```

它由 `should_enable_prelower_semantic_check` 控制总开关（读 pass 配置 `TL_DISABLE_PRELOWER_SEMANTIC_CHECK`），可在调试时关闭以隔离「是语义检查报错还是 pass 报错」。

#### 4.4.3 源码精读

`PreLowerSemanticCheck` 实现：[tilelang/engine/semantic_check.py:21-30](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/semantic_check.py#L21-L30)。

```python
def PreLowerSemanticCheck(mod: IRModule) -> None:
    """Run backend-independent validation before lowering."""
    if not should_enable_prelower_semantic_check():
        return
    if should_enable_ast_print():
        tilelang.analysis.ASTPrinter()(mod)
    tilelang.analysis.NestedLoopChecker()(mod)
    tilelang.analysis.FragmentLoopChecker()(mod)
```

开关函数：[semantic_check.py:9-18](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/semantic_check.py#L9-L18)。

```python
def should_enable_prelower_semantic_check(pass_ctx=None) -> bool:
    if pass_ctx is None:
        pass_ctx = tilelang.transform.get_pass_context()
    return not pass_ctx.config.get(
        tilelang.PassConfigKey.TL_DISABLE_PRELOWER_SEMANTIC_CHECK, False)
```

调用点在 `lower_to_host_device_ir` 里、`resolve_pipeline` **之前**：[lower.py:336-340](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py#L336-L340)。

```python
# Run backend-independent semantic checks before target-specific lowering.
PreLowerSemanticCheck(mod)
pipeline = resolve_pipeline(target)
mod = pipeline.lower(mod, target)
```

注释 `backend-independent` 一词点明了它的定位：与 target 无关，所以在选流水线之前就能跑。两个检查器 `NestedLoopChecker` 与 `FragmentLoopChecker` 都属于 `tilelang.analysis`（编译器各阶段的细粒度分析 pass，见 u1-l3 对 `testing/` 的描述）。

#### 4.4.4 代码实践

**实践目标**：体会语义检查的「提前拦截」价值，并学会在调试时关闭它。

1. **阅读型**：在 [lower.py:337](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py#L337) 处确认 `PreLowerSemanticCheck(mod)` 位于 `pipeline.lower` 之前。
2. **关闭开关**：构造 kernel 时在 pass 配置里设置 `TL_DISABLE_PRELOWER_SEMANTIC_CHECK=True`（可通过 `pass_configs` 传入，u3-l2），重新编译，观察原本会被语义检查拦截的问题是否改为在更深的 pass 里以另一种形式出现。
3. **AST 打印**：设置 `TL_AST_PRINT_ENABLE=True`，观察 `ASTPrinter` 输出的下译前 AST。

**需要观察的现象**：关闭语义检查后，某些结构性错误不再被提前拦截，而是延后到 pass 流水线内部报错，错误信息会更晦涩——这正是语义检查存在的意义。

**预期结果**：能用自己的话说明「语义检查 = 把后端无关的错误前移到下译前抛出」。若不确定具体配置项写法，标注「待本地验证」并查阅 `tilelang.PassConfigKey`。

#### 4.4.5 小练习与答案

**练习 1**：`PreLowerSemanticCheck` 为什么放在 `resolve_pipeline` **之前**？

> **答案**：因为它是后端无关的（backend-independent）结构性检查，不依赖 target；放在选流水线之前可以尽早拦截非法 IR，给出清晰错误，避免在某个 target 专属 pass 里深层失败。

**练习 2**：如何在不改源码的前提下临时关闭这道检查？

> **答案**：在编译时的 pass 配置（`pass_configs`）里把 `TL_DISABLE_PRELOWER_SEMANTIC_CHECK` 设为 `True`，`should_enable_prelower_semantic_check` 就会返回 `False`，`PreLowerSemanticCheck` 直接 `return`。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个端到端的「下译流程解剖」任务。

**任务**：选一个最简 GEMM PrimFunc（可复用 `examples/gemm/example_gemm.py` 的 kernel 定义，见 u1-l4），手动走一遍 `lower` 全流程并产出一张「host/device 拆分示意图」。

**步骤**：

1. **入口追踪**：从 `@T.prim_func` 出发，沿着 `tilelang.compile` → `KernelCache` → [jit/kernel.py:258](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L258) 的 `tilelang.lower(...)` 找到下译主入口，标出 5 个实参。
2. **拆分观察**：改用 `lower_to_host_device_ir` 直接调用（如 4.2.4 示例），打印 `host_mod` 与 `device_mod` 的函数列表，确认它们互补。
3. **画图**：仿照本讲 4.2.2 的流程框图，自己画一张更完整的图，标清每一步的**输入 → 处理 → 输出**：
   - `PrimFunc` →（语义检查）→（resolve_pipeline + pipeline.lower，含 SplitHostDevice / LowerDeviceKernelLaunch）→（Filter 拆分）→ `host_mod` + `device_mod` →（device_codegen / host_codegen）→ `CompiledArtifact`。
4. **回调注入**：注册一个 `register_cuda_postproc`（或 `register_maca_postproc`），在源码顶部插一行 `// lowered by <你的名字>`，编译后用 `get_kernel_source()` 验证生效。
5. **对照 metax**：把 target 在 `"cuda"` 与 `{"kind":"maca"}` 间切换，对比 `resolve_pipeline` 取到的流水线与 `resolve_device_codegen` 取到的 codegen 名字有何不同（参考 4.2.3 的 maca/cuda 注册差异）。

**交付物**：一张 host/device 拆分示意图（手绘或工具画均可）+ 一份能跑通的 `inspect_lower.py` 脚本 + postproc 注入后的源码首行截图/文本。

> 无 GPU 时可全程使用 `without_compile` 路径或 `tilelang_maca_without_compile`（u3-l3），只观察源码层面的产物即可完成。

## 6. 本讲小结

- `tilelang.engine.lower` 是下译**主入口**：编排「下译拆分 → device codegen →（可选）host codegen」，产出 `CompiledArtifact`；两个布尔开关控制是否真编译设备二进制与是否在 lower 内做 host codegen。
- 下译的核心是 `lower_to_host_device_ir`：先做后端无关的语义检查，再按 `target.kind.name` 取一条 pass 流水线降级 IR，最后用 `Filter` 按 `calling_conv` 把模块**物理拆成 host_mod 与 device_mod**。
- host/device codegen 与 pass 流水线都**按 target 动态分派**：cuda、maca 各自在 `pipeline.py` / `codegen.py` 里注册；maca 流水线复用 cuda 骨架并多插 `LowerMACAIntrin`。
- postproc 回调是「Python 注册、C++ 调用」的**源码拦截点**，位于 codegen 印出源码之后、真正编译之前；默认不注册，`register_{cuda,hip,maca,c}_postproc` 是其 API，metax 分支新增了 `maca` 版本。
- 下译前的 `PreLowerSemanticCheck` 是后端无关体检，可在 pass 配置中关闭，用于把结构性错误前移。

## 7. 下一步学习建议

本讲建立了「下译主流程」的全局视图，后续讲义沿着这条流水线逐站深入：

- **u4-l2（tile 算子与 T.gemm 的分派）**：进入流水线内部的 `LowerTileOp`，看 `T.gemm` 如何按 target/dtype 分派到 wgmma/mfma/mma 指令。
- **u4-l3（内存布局推断）**：展开本讲提到的 `LayoutInference` pass，理解 fragment 的寄存器布局是怎么推出来的。
- **u4-l4（软件流水线）**：展开 `PipelinePlanning` / `InjectSoftwarePipeline` 两个 pass，理解 `T.Pipelined` 的下译。
- **u5-l1/u5-l2（C++ 编译器核心与 transform 体系）**：从 Python 侧切到 C++ 侧，系统了解 `src/transform` 下各 pass 与 `LowerTileOp`、`SplitHostDevice`、`LowerDeviceKernelLaunch` 的实现。
- **u7 系列（MACA 后端）**：如果你最关心 metax 分支，可直接跳到 u7-l1，看 MACA 后端如何在本文描述的每一个挂载点（target 注册、pipeline、device codegen、postproc/compile 回调）上落地。
