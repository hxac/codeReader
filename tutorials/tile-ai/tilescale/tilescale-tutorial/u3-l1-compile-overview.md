# 编译总览：从 PrimFunc 到 JITKernel

## 1. 本讲目标

在前两单元里，你已经能用 `T.Kernel`、`T.copy`、`T.gemm`、`T.Pipelined` 这些 `T.*` 原语写出一个单 GPU kernel，并用 `@tilelang.jit` 把它跑起来（见 u1-l3、u2-l1）。但 `@tilelang.jit` 背后到底发生了什么？一段 Python 写的 tile 程序，是怎么变成 GPU 上真正执行的一段 CUDA 代码的？

本讲是**编译流水线总览**，学完后你应当掌握：

1. TileLang 编译主流程的**三个阶段**：`PreLowerSemanticCheck` → `LowerAndLegalize` → `OptimizeForTarget`，以及它们各自负责什么。
2. `tilelang.lower` 与 `tilelang.compile` 的区别：前者产出 `CompiledArtifact`（编译产物），后者在此基础上包出可调用的 `JITKernel`。
3. host 代码与 device 代码是如何在编译末尾被**拆分（filter）**的，`target` 是如何被处理与绑定的。
4. `CompiledArtifact` 这个数据结构里每个字段代表什么，以及如何直接调用 `tilelang.lower` 去检视它。

本讲**只画全景图**，不深入单个 pass 的算法细节——每个 pass 的精读留给 u3-l3、u3-l4、u3-l5 以及整个 Unit 4。读完后你会拿到一张「地图」，知道后续每一讲插在编译链的哪一格。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个概念。

### 2.1 什么是「编译 pass」

GPU kernel 编译器不是一步到位地把 Python 翻译成 CUDA。它更像一条流水线：中间表示（IR）经过一道道工序，每道工序只做一件事，比如「把负数下标变成正数」「把高层 tile 操作降级成低层循环」「插入同步屏障」。每一道工序就叫一个 **pass**。

形式上，可以把整条流水线写成 pass 的复合：

\[
\text{mod}_{i+1} = \mathrm{Pass}_i(\text{mod}_i), \qquad \text{mod}_0 = \mathrm{IRModule}(\{\text{PrimFunc}\})
\]

其中 `mod` 是一个 `IRModule`（一组函数的容器），`PrimFunc` 是 TVM 里「纯函数式」的中间表示，对应你用 `@T.prim_func` 写的那个 kernel。本讲的源码几乎都在做一件事：决定 pass 的**顺序**和**分组**。

### 2.2 host 与 device：一个 kernel 程序的两半

GPU 程序天然分两半：

- **device 代码**：真正跑在 GPU 上、由线程块（threadblock）执行的部分，最终编译成 CUDA/HIP 源码，再编成 cubin。
- **host 代码**：跑在 CPU 上、负责「分配显存、准备参数、启动 kernel、回收结果」的胶水代码。

TileLang 在编译**末尾**会把一个模块里的函数按 `calling_conv`（调用约定）属性拆成两堆：device 函数送去 device codegen，host 函数送去 host codegen。这是本讲第 3 个最小模块的核心。

### 2.3 target：编译目标

`target` 描述「为哪种硬件编译」，例如 `"cuda"`、`"cuda -arch=sm_80"`、`"hip"`、`"metal"`。它决定了 codegen 选哪个后端、也决定了 `OptimizeForTarget` 阶段走哪条优化分支（比如 Hopper 架构才走 TMA + warp 特化）。`target="auto"` 时，TileLang 会自动探测本机有没有 CUDA / HIP / Metal。

## 3. 本讲源码地图

本讲涉及的关键文件与职责：

| 文件 | 职责 |
|------|------|
| [tilelang/engine/lower.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py) | 编译主入口 `lower()`，编排三个阶段、做 host/device 拆分与 codegen，产出 `CompiledArtifact`；还含 cuda/hip 编译回调与 target 处理。 |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py) | 定义三大阶段函数 `PreLowerSemanticCheck` / `LowerAndLegalize` / `OptimizeForTarget`，逐个 pass 排好序。 |
| [tilelang/engine/param.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/param.py) | 定义 `CompiledArtifact`（编译产物）与 `KernelParam`（参数描述）两个数据类。 |
| [tilelang/jit/__init__.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py) | 高层入口 `compile()`、装饰器 `@tilelang.jit` / `@tilelang.lazy_jit`，产出 `JITKernel`。 |
| [tilelang/jit/kernel.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py) | `JITKernel` 类：在内部调用 `tilelang.lower` 拿到 `CompiledArtifact`，再用 adapter 封装成可调用对象。 |
| [tilelang/cache/\_\_init\_\_.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/cache/__init__.py) | `cached()`：按 `execution_backend` 分派到各后端的 `KernelCache`，是 `compile()` 的实际后端路由。 |
| [tilelang/utils/target.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/target.py) | `determine_target()`：把 `"auto"` / 字符串解析成具体的 TVM `Target` 对象。 |

一条记忆线索：**`compile`/`jit`（高层，产 `JITKernel`） → `cached`（路由） → `JITKernel._compile_and_create_adapter`（内部） → `tilelang.lower`（低层，产 `CompiledArtifact`） → 三大 phase → filter → codegen**。本讲自底向上讲 `lower`，再回头点出 `compile` 怎么包住它。

## 4. 核心概念与源码讲解

### 4.1 `lower()` 的阶段编排与 target 处理

#### 4.1.1 概念说明

`tilelang.lower` 是编译器的**真正主入口**。它的职责是把一个 `PrimFunc`（或一个 `IRModule`）翻译成一个 `CompiledArtifact`。

之所以叫 `lower`（「降级」），是因为编译器圈子里「lowering」特指「把高层抽象逐步降成底层表示」的过程。`@T.prim_func` 里那些 `T.copy`、`T.gemm`、`T.Pipelined` 都是高层 sugar，`lower` 的工作就是把这些 sugar 展开成真正的循环、搬运、矩阵乘指令。

需要先区分两个 API 层次：

- `tilelang.lower`：**低层**，产出 `CompiledArtifact`（含 host/device IR 与生成的源码字符串）。
- `tilelang.compile` / `@tilelang.jit`：**高层**，在 `lower` 之上再包一层，把 `CompiledArtifact` 封装成可直接 `kernel(a, b, c)` 调用的 `JITKernel`。

本模块聚焦 `lower` 本身的**编排**与 **target 处理**；具体每个 pass 做什么留给 4.2，产物结构留给 4.3。

#### 4.1.2 核心流程

`lower` 的执行过程可以用下面的伪代码概括（对应源码主函数）：

```
lower(func_or_mod, target="auto", target_host=None,
      runtime_only=False,
      enable_host_codegen=False,
      enable_device_compile=False) -> CompiledArtifact:

    1. 如果输入是 PrimFunc：
         - 抽取参数 params = extrac_params(func)   # 非 runtime_only 时
         - 包装成 IRModule：mod = IRModule({global_symbol: func})
       否则输入已是 IRModule，直接用。

    2. 解析 target：
         - target = determine_target(target)        # "auto" → 探测出 cuda/hip/metal
         - target_host = canon_target_host(...)     # 缺省取 "llvm" 或 "c"

    3. 构造 host/device 谓词（用于第 5 步的拆分）。

    4. 跑三大阶段：
         PreLowerSemanticCheck(mod)                  # 阶段 0：只校验，不改 mod
         mod = LowerAndLegalize(mod, target)         # 阶段 1：合法化 + 布局推理 + 降级 tile op
         mod = OptimizeForTarget(mod, target)        # 阶段 2：目标相关优化（含 SplitHostDevice）

    5. 拆分 host / device：
         host_mod   = Filter(is_host_call)(mod)
         device_mod = Filter(is_device_call)(mod)

    6. device codegen（生成 CUDA/HIP 源码，可选是否真编成 cubin）。

    7. 可选 host codegen（仅 enable_host_codegen=True 时）。

    8. 返回 CompiledArtifact(host_mod, device_mod, params, kernel_source, rt_mod?)。
```

两个布尔开关 `enable_host_codegen` / `enable_device_compile` 很关键，它们决定了「编译到哪一步」：

- 默认都是 `False`：device 只生成**源码字符串**（不调 nvcc），host 不做 codegen。`JITKernel` 默认走 `tvm_ffi` 后端时会把它们设为 `True`（见 4.3）。
- `runtime_only=True`：跳过参数抽取（`params=None`），用于「只关心运行时加载、不重新描述参数」的场景（呼应 u1-l4 提到的 `libtilelang_module.so` runtime-only 产物）。

用 pass 复合的记号，整条链可写成：

\[
\text{CompiledArtifact} = \mathrm{CodeGen}\!\left(\mathrm{Filter}\!\left(\mathrm{OptimizeForTarget}\big(\mathrm{LowerAndLegalize}(\mathrm{PreCheck}(\text{mod}_0),\,t),\,t\big)\right)\right)
\]

其中 \(t\) 是 target。

#### 4.1.3 源码精读

`lower` 的函数签名与开关定义在这里——注意它**默认不做 host/device 的完整编译**，因为 JIT 层会按需打开：

[tilelang/engine/lower.py:243-256](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L243-L256) —— `lower` 的签名，`enable_host_codegen` / `enable_device_compile` 默认 `False`，注释说明 JIT 层有自己的实现。

第一步：把 `PrimFunc` 包装成 `IRModule`，并在非 `runtime_only` 时抽取参数。`global_symbol`（函数名）作为该函数在模块里的键：

[tilelang/engine/lower.py:258-263](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L258-L263) —— 输入是 `PrimFunc` 时抽取参数、包装成 `IRModule`。

参数抽取由 `extrac_params`（注意源码里就是这拼法）完成：遍历函数形参，在 `buffer_map` 里的当张量、其余当标量，统一封装成 `KernelParam`：

[tilelang/engine/lower.py:164-176](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L164-L176) —— `extrac_params`：张量参数走 `from_buffer`，标量走 `from_var`；handle 形参若没绑 buffer 会报友好错误。

第二步：target 处理。`"auto"` 或字符串经 `determine_target` 变成 `Target` 对象（探测 CUDA/HIP/Metal），`target_host` 缺省时取 `"llvm"`（无 llvm 则 `"c"`）：

[tilelang/engine/lower.py:265-271](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L265-L271) —— 解析 target 与 target_host，并组合成 `(target, target_host)` 对。

[tilelang/engine/lower.py:179-183](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L179-L183) —— `canon_target_host`：host 目标缺省选 `llvm`，否则 `c`。

`determine_target` 的自动探测逻辑（按 CUDA → HIP → Metal 顺序，CUDA 还会用 torch 读设备算力拼出 `sm_XX`）：

[tilelang/utils/target.py:114-133](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/target.py#L114-L133) —— `"auto"` 分支：依次探测 CUDA / HIP / Metal 可用性。

第三步：三大阶段的调用——三行代码、三个阶段，顺序就是执行顺序：

[tilelang/engine/lower.py:277-283](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L277-L283) —— `PreLowerSemanticCheck` → `LowerAndLegalize` → `OptimizeForTarget`。

第四、五步（拆分与 codegen）见 4.3。

> 高层入口参考：`tilelang.compile` 只是把 prim_func 交给 `cached` 路由——
> [tilelang/jit/\_\_init\_\_.py:108-117](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py#L108-L117)；
> `cached` 按 `execution_backend` 分派——
> [tilelang/cache/\_\_init\_\_.py:73-86](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/cache/__init__.py#L73-L86)。
> 顶层包在 `tilelang/__init__.py` 同时导出了二者：`compile`（[L142](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/__init__.py#L142)）与 `lower`（[L175](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/__init__.py#L175)）。

#### 4.1.4 代码实践

**实践目标**：确认 `lower` 的输入输出类型，验证它「不依赖 `@tilelang.jit`」也能独立工作。

**操作步骤**（基于 u1-l3 的 quickstart，需要 CUDA 环境）：

```python
# 示例代码：直接对 prim_func 调 lower，绕过 @tilelang.jit
import tilelang
import tilelang.language as T

# 一个极简的 creator，返回 prim_func（注意：这里不加 @tilelang.jit）
def make_add(M=128, N=128):
    @T.prim_func
    def add_kernel(
        A: T.Tensor((M, N), "float16"),
        B: T.Tensor((M, N), "float16"),
        C: T.Tensor((M, N), "float16"),
    ):
        with T.Kernel(T.ceildiv(N, 32), T.ceildiv(M, 32), threads=128) as (bx, by):
            A_s = T.alloc_shared((32, 32), "float16")
            B_s = T.alloc_shared((32, 32), "float16")
            C_f = T.alloc_fragment((32, 32), "float16")
            T.copy(A[by*32, bx*32], A_s)
            T.copy(B[by*32, bx*32], B_s)
            for i, j in T.Parallel(32, 32):
                C_f[i, j] = A_s[i, j] + B_s[i, j]
            T.copy(C_f, C[by*32, bx*32])
    return add_kernel

prim_func = make_add()
artifact = tilelang.lower(prim_func, target="cuda")

print(type(artifact))                # <class 'tilelang.engine.param.CompiledArtifact'>
print(len(artifact.params))          # 3（A、B、C 三个张量参数）
print(artifact.rt_mod is None)       # 默认 True（没开 host/device 完整编译）
```

**需要观察的现象**：`tilelang.lower` 不需要 `@tilelang.jit` 也能返回一个 `CompiledArtifact`；`params` 长度等于 kernel 的张量形参个数。

**预期结果**：打印出 `CompiledArtifact` 类型、`params` 数量 = 3、`rt_mod` 为 `None`。

> 说明：若 CUDA 不可用，`target="cuda"` 会在 `determine_target` 处报错；此时可把 target 换成 `"c"` 或在无 GPU 机器上跳过运行，改为阅读型实践（见综合实践）。具体运行数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `tilelang.lower(prim_func, target="cuda")` 改成 `runtime_only=True`，`artifact.params` 会变成什么？为什么？

**答案**：会变成 `None`。因为 [lower.py:262](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L262) 写着 `params = extrac_params(func) if not runtime_only else None`——`runtime_only` 模式假定参数描述由外部（已加载的模块）提供，故跳过抽取。

**练习 2**：`target="auto"` 在一台装了 CUDA 的机器上，最终 `Target` 的 `arch` 字段是怎么定的？

**答案**：`determine_target` 探测到 CUDA 可用后，若 `torch.cuda.is_available()`，会读 `torch.cuda.get_device_capability(0)` 并经 `nvcc.get_target_arch` 拼成 `"sm_XX"`，构造 `Target({"kind":"cuda","arch":"sm_XX"})`（见 [target.py:123-125](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/target.py#L123-L125)）。

---

### 4.2 三大阶段：PreLowerSemanticCheck / LowerAndLegalize / OptimizeForTarget

#### 4.2.1 概念说明

`lower` 把几十个 pass 编进三个阶段函数，每个阶段有一个明确的主题：

| 阶段 | 函数 | 主题 | 是否改 `mod` |
|------|------|------|--------------|
| 阶段 0 | `PreLowerSemanticCheck` | **校验**：在进入复杂 C++ 栈之前，先在 Python 侧给出友好报错 | 不改（仅校验） |
| 阶段 1 | `LowerAndLegalize` | **合法化 + 布局推理 + 降级**：把高层 tile IR 变成 TVM 能理解的标准 IR | 改 |
| 阶段 2 | `OptimizeForTarget` | **目标相关优化**：软件流水、warp 特化、存储重写、host/device 分离 | 改 |

为什么要分这三组？因为它们的**失败模式**和**关心点**不同：阶段 0 是「用户写错了没」，阶段 1 是「把 sugar 展开并把布局定下来」，阶段 2 是「针对具体硬件榨性能并最终拆出 device kernel」。后续讲义（u3-l3 讲阶段 1，u3-l4 讲阶段 2，Unit 4 深入单个机制）就是按这个分组展开的。

#### 4.2.2 核心流程

**阶段 0 `PreLowerSemanticCheck`**（只读校验）：

```
if 启用 AST 打印:    ASTPrinter()(mod)        # 调试用
NestedLoopChecker()(mod)                       # 检查是否有非法的嵌套循环
FragmentLoopChecker()(mod)                     # 检查 T.Parallel + fragment 的非法组合
```

**阶段 1 `LowerAndLegalize`**（高层 → 标准 IR，节选关键 pass）：

```
BindTarget(target)                             # 把 target 绑到模块上
AddWrapperForSingleBufStore                    # 给单 buffer 写入加 wrapper
LegalizeNegativeIndex                          # 负下标 → 正下标
InjectAssumes / Simplify                       # 注入假设加速证明；化简
LayoutReducer / LayoutInference                # ★ 推理 fragment/shared 的线程布局
LowerTileOp                                    # ★ 把 T.copy/T.gemm 等高层 op 降级
LowerL2Persistent                              # L2 持久化映射降级
LegalizeVectorizedLoop / LegalizeSafeMemoryAccess  # 向量化合法化 + 越界安全检查
Simplify                                       # 再次化简，清掉安全检查引入的冗余
```

**阶段 2 `OptimizeForTarget`**（节选，分两条分支）：

```
LowerSharedBarrier / LowerSharedTmem           # 屏障/tmem 降级
if allow_tma_and_warp_specialized (Hopper):    # ★ TMA + warp 特化分支
    WarpSpecialized / InjectTmaBarrier / PipelinePlanning / InjectSoftwarePipeline
    RewriteWgmmaSync / InjectFenceProxy ...
else:                                          # ★ 普通分支
    PipelinePlanning / InjectSoftwarePipeline ...
# 公共尾部
FlattenBuffer / ConfigIndexBitwidth / VectorizeLoop / StorageRewrite / UnrollLoop
SplitHostDevice                                # ★ 把 host/device 函数拆开（打上 calling_conv）
MergeSharedMemoryAllocations / ThreadSync(shared) / ThreadSync(shared.dyn)
MakePackedAPI / LowerDeviceKernelLaunch        # 打包成可调用 API
PersistThreadblock                             # 持久化线程块（对应 T.Persistent）
```

注意一个**强顺序约束**：阶段 2 里的 `SplitHostDevice` 会给 device 函数打上 `calling_conv = DEVICE_KERNEL_LAUNCH` 标记，而 4.3 的 host/device filter 正是依赖这个标记来拆分——所以 **filter 必须在 `OptimizeForTarget` 之后**才能工作。

#### 4.2.3 源码精读

**阶段 0**——三个 checker，注意 docstring 强调「validation-only，不改不返回」：

[tilelang/engine/phase.py:114-127](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L114-L127) —— `PreLowerSemanticCheck`：可选 AST 打印 + `NestedLoopChecker` + `FragmentLoopChecker`，只校验不修改。

**阶段 1**——开头绑 target，中段做布局推理与 tile op 降级（本讲只标位置，精读见 u3-l3）：

[tilelang/engine/phase.py:152-152](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L152) —— `BindTarget`：把 target 信息绑进模块，后续 pass 可读。

[tilelang/engine/phase.py:166-172](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L166-L172) —— `LayoutReducer` → `LayoutInference` → `LowerTileOp`：先定 fragment/shared 布局，再把高层 tile op 降级。

**阶段 2**——两条分支由 `allow_tma_and_warp_specialized` 决定（Hopper + 未禁用 TMA/warp 特化才走特化分支）：

[tilelang/engine/phase.py:197-223](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L197-L223) —— TMA+warp 特化分支 vs 普通分支。特化分支多出 `WarpSpecialized` / `InjectTmaBarrier` / `RewriteWgmmaSync` / `InjectFenceProxy` 等 Hopper 专项 pass（详见 u4-l2、u4-l3）。

阶段 2 尾部的 host/device 拆分与打包：

[tilelang/engine/phase.py:262-262](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L262) —— `SplitHostDevice`：把 device 函数分离并打上 `DEVICE_KERNEL_LAUNCH` 标记（4.3 的 filter 依赖它）。

[tilelang/engine/phase.py:275-280](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L275-L280) —— `MakePackedAPI` → `LowerDeviceKernelLaunch` → `PersistThreadblock`：把函数打包成可从 host 调用的 API，并落地持久化线程块。

> `allow_tma_and_warp_specialized` 的判定逻辑（CUDA + 有 TMA + 未禁用相关开关）见 [phase.py:21-27](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L21-L27)。

#### 4.2.4 代码实践

**实践目标**：用源码阅读法，把阶段 1 / 阶段 2 的 pass 顺序抄成一张表，体会「为什么是这个顺序」。

**操作步骤**：

1. 打开 [tilelang/engine/phase.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py)。
2. 在 `LowerAndLegalize`（[L130-187](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L130-L187)）里，按出现顺序列出每个 `transform`，给每个 pass 写一句话作用。
3. 同样处理 `OptimizeForTarget`（[L190-282](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L190-L282)），特别注意 `if/else` 两条分支的差异。
4. 回答：为什么 `LegalizeNegativeIndex` 必须在 `LayoutInference` 之前？为什么 `SplitHostDevice` 必须在 `MergeSharedMemoryAllocations` 之前（源码注释里有提示）？

**需要观察的现象**：phase.py 是一个**纯顺序的 pass 列表**，没有复杂控制流，唯一分叉是阶段 2 的 `if allow_tma_and_warp_specialized`。

**预期结果**：你应得到两张 pass 顺序表，并理解「负下标合法化要先于布局推理」「合并共享内存分配要在 host/device 拆分之后（注释 [phase.py:264-265](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L264-L265) 说明合并点在每个 device 函数开头）」。

#### 4.2.5 小练习与答案

**练习 1**：`PreLowerSemanticCheck` 为什么「不改模块」？如果它改了会怎样？

**答案**：它的定位是「在进入 C++ 栈前的友好校验」（见 [phase.py:115-119](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L115-L119) 的 docstring）。若它改模块，就会和阶段 1 的 pass 职责重叠，破坏「校验与变换分离」的清晰边界；而且 `lower` 调用它时甚至没接收返回值（[lower.py:277](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L277)），印证它不产出新模块。

**练习 2**：阶段 2 里，什么条件下会走「TMA + warp 特化」分支？

**答案**：当 `allow_tma_and_warp_specialized(pass_ctx, target)` 为真，即目标是 CUDA、硬件有 TMA（Hopper 及以上）、且 pass_config 没有禁用 `tl.disable_tma_lower` 与 `tl.disable_warp_specialized`（见 [phase.py:21-27](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L21-L27)）。

---

### 4.3 host/device filter 与 CompiledArtifact 产出

#### 4.3.1 概念说明

三大阶段跑完后，`mod` 里**同时**含有 host 函数和 device 函数（`SplitHostDevice` 已经给 device 函数打了标记，但还没物理分到两个模块）。`lower` 的最后一步就是用 `tir.transform.Filter` 把它们**筛分成两个模块**，分别送去 host codegen 和 device codegen，最终把所有产物装进 `CompiledArtifact`。

`CompiledArtifact` 是 `lower` 的**唯一返回值**，是一个 `@dataclass`，字段含义如下（先建立印象，4.3.3 对源码）：

| 字段 | 类型 | 含义 |
|------|------|------|
| `host_mod` | `IRModule` | host 侧 IR（CPU 上准备/启动 kernel 的胶水函数） |
| `device_mod` | `IRModule` | device 侧 IR（真正的 GPU kernel 函数） |
| `params` | `list[KernelParam]` | kernel 的参数描述（每个张量/标量的 dtype + shape） |
| `kernel_source` | `str` | device codegen 生成的源码字符串（CUDA/HIP/C/...） |
| `rt_mod` | `Module \| None` | 可运行时模块；默认 `None`，仅 `enable_host_codegen=True` 时才有值 |

而 `JITKernel`（`compile` / `@tilelang.jit` 的产物）就是在 `CompiledArtifact` 之上，再加一个 **adapter**（按 `execution_backend` 选 tvm_ffi/cython/nvrtc/torch/cutedsl），把 `rt_mod` 包成可以直接 `kernel(a, b, c)` 调用的对象。

#### 4.3.2 核心流程

```
# 经过三大阶段后，mod 同时含 host/device 函数
host_mod   = Filter(is_host_call)(mod)      # 只留 host 函数
device_mod = Filter(is_device_call)(mod)    # 只留 device 函数（calling_conv == DEVICE_KERNEL_LAUNCH）

# device codegen：把 device IR 变成源码；是否真编成 cubin 取决于开关
codegen_mod = device_codegen(device_mod, target)            if enable_device_compile
              device_codegen_without_compile(device_mod, target)  否则（默认）

if enable_host_codegen:                     # 默认 False
    host_mod = host_codegen(host_mod, target_host)
    host_mod.import_module(codegen_mod)
    return CompiledArtifact(host_mod, device_mod, params, codegen_mod.inspect_source(), rt_mod=host_mod)

return CompiledArtifact(host_mod, device_mod, params, codegen_mod.inspect_source())
                                                              # rt_mod 缺省 None
```

判定一个函数是否 device 函数的依据是它的 `calling_conv` 属性：等于 `DEVICE_KERNEL_LAUNCH` 就是 device kernel，否则是 host。这个属性是阶段 2 的 `SplitHostDevice` / `LowerDeviceKernelLaunch` 打上去的——这就是为什么 filter 必须在三大阶段之后。

device codegen 会按 target 选后端（cuda / hip / c / llvm / webgpu / metal），并把生成的源码用 `inspect_source()` 取成字符串放进 `kernel_source`。两个变体的区别只在于「是否真的调 nvcc/hipcc 编成二进制」：`device_codegen` 会触发 `tilelang_callback_cuda_compile`（含 NVSHMEM 链接，见 u3-l5），`device_codegen_without_compile` 只产出源码、不编译。

#### 4.3.3 源码精读

host/device 拆分——两行 `Filter`，谓词由 `get_host_call` / `get_device_call` 给出：

[tilelang/engine/lower.py:285-286](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L285-L286) —— 用 `tir.transform.Filter` 把 host 与 device 函数分别筛进两个模块。

谓词定义：device 判定看 `calling_conv == DEVICE_KERNEL_LAUNCH`，host 就是「非 device」：

[tilelang/engine/lower.py:30-48](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L30-L48) —— `has_device_kernel_launch` / `is_device_call`：基于 `calling_conv` 判定 device 函数。

[tilelang/engine/lower.py:51-56](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L51-L56) —— `get_device_call` / `get_host_call`：返回用于 Filter 的判定函数（host = 非 device）。

device codegen 的两个变体——按 target 选全局函数，差别在 `_without_compile` 后缀：

[tilelang/engine/lower.py:288-288](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L288) —— 按开关选择「真编译」还是「只生成源码」。

[tilelang/engine/lower.py:204-217](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L204-L217) —— `device_codegen`：cuda 选 `target.build.tilelang_cuda`（或 `cutedsl`），hip 选 `tilelang_hip`。

[tilelang/engine/lower.py:220-240](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L220-L240) —— `device_codegen_without_compile`：后缀 `_without_compile`，只出源码不编译；还多支持 `c`/`llvm`/`webgpu`/`metal` 后端。

最终装配与返回：

[tilelang/engine/lower.py:290-295](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L290-L295) —— `enable_host_codegen=True` 时做 host codegen、`import_module` 挂上 device 产物、返回带 `rt_mod` 的 `CompiledArtifact`；否则返回 `rt_mod=None` 的版本。

`CompiledArtifact` 数据类定义与字段：

[tilelang/engine/param.py:153-164](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/param.py#L153-L164) —— `CompiledArtifact`：`host_mod` / `device_mod` / `params` / `kernel_source` / `rt_mod`（可空）。

`KernelParam` 用 `tvm.DataType`（而非 torch.dtype）以保留 float8 等特殊类型：

[tilelang/engine/param.py:12-25](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/param.py#L12-L25) —— `KernelParam`：`dtype`（tvm.DataType）+ `shape`，注释说明为何不用 torch.dtype。

**JITKernel 如何消费 CompiledArtifact**——它根据 `execution_backend` 决定 `lower` 的两个开关（只有 `tvm_ffi` 后端会把两个开关都设 `True`），再用对应 adapter 封装：

[tilelang/jit/kernel.py:244-253](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L244-L253) —— `JITKernel._compile_and_create_adapter`：在 `PassContext + target` 作用域内调 `tilelang.lower`，`enable_host_codegen`/`enable_device_compile` 仅 `tvm_ffi` 后端为真。

> 也就是说：你用 `@tilelang.jit`（默认 `tvm_ffi`）时，`lower` 内部会完成 host+device 的**完整编译**并产出可运行的 `rt_mod`；而 `nvrtc` / `cython` / `torch` / `cutedsl` 后端则只拿源码，由各自 adapter 自行编译。这正是 4.1 两个开关存在的意义。`JITKernel` 把 `artifact` 存在 `self.artifact`（[kernel.py:255](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L255)），原始 prim_func 存在 `self.prim_func`（[kernel.py:99](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L99)）。

#### 4.3.4 代码实践

**实践目标**：直接调用 `tilelang.lower`，逐字段检视返回的 `CompiledArtifact`，把每个字段和 4.3.1 的表对上。

**操作步骤**（承接 4.1.4 的 `make_add`，或复用 quickstart）：

```python
# 示例代码：逐字段检视 CompiledArtifact
import tilelang

prim_func = make_add()                      # 来自 4.1.4
artifact = tilelang.lower(prim_func, target="cuda")

# 1) device_mod：device 侧 IR，应能看到 kernel 函数（global_symbol 一般就是 prim_func 名）
print("device_mod 函数:", list(artifact.device_mod.functions.keys()))

# 2) host_mod：host 侧 IR（默认未做 host codegen，仍是 IR）
print("host_mod 函数:", list(artifact.host_mod.functions.keys()))

# 3) params：参数描述列表
for i, p in enumerate(artifact.params):
    print(f"param[{i}]: dtype={p.dtype}, shape={p.shape}, is_scalar={p.is_scalar()}")

# 4) kernel_source：生成的设备源码（默认 _without_compile，仍是完整 CUDA 源码字符串）
src = artifact.kernel_source
print("kernel_source 前 200 字符:\n", src[:200])
print("是否含 'extern \"C\"' 或 '__global__':", ('extern "C"' in src) or ('__global__' in src))

# 5) rt_mod：默认 None（没开 enable_host_codegen）
print("rt_mod:", artifact.rt_mod)
```

**需要观察的现象**：

- `device_mod` 里应有且仅有 device kernel 函数；`host_mod` 里是 host 侧的包装/启动函数（函数集合与 `device_mod` 互补）。
- `params` 的每个元素是 `KernelParam`，`is_scalar()` 对张量为 `False`、shape 为 `[128, 128]`。
- `kernel_source` 是一段 CUDA C++ 源码字符串（非空），通常能看到 `__global__` 或 `extern "C"` 之类的标记。
- `rt_mod` 为 `None`。

**预期结果**：五项检查均与上述描述一致。其中 `kernel_source` 的具体内容、`device_mod`/`host_mod` 里函数的确切名字**待本地验证**（取决于版本与 target）。

> 对照实验：把调用改成 `tilelang.lower(prim_func, target="cuda", enable_host_codegen=True, enable_device_compile=True)`，再观察 `artifact.rt_mod` 应变为非 `None`（一个可加载执行的 `Module`）。这模拟了 `JITKernel` 在 `tvm_ffi` 后端下的行为（[kernel.py:244-245](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L244-L245)）。

#### 4.3.5 小练习与答案

**练习 1**：为什么默认（`enable_device_compile=False`）时 `kernel_source` 仍然非空，但 `rt_mod` 是 `None`？

**答案**：默认走 `device_codegen_without_compile`，它调用带 `_without_compile` 后缀的 codegen（[lower.py:220-240](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L220-L240)），**仍然生成完整源码**（故 `inspect_source()` 非空），但**不调 nvcc 编成 cubin**，也不做 host codegen，所以没有可运行的 `rt_mod`。这种「只出源码」的模式是给 nvrtc/cython 等自行编译的后端用的。

**练习 2**：如果把 `OptimizeForTarget` 阶段从 `lower` 里去掉（假设），4.3 的 host/device filter 还能正常工作吗？为什么？

**答案**：不能。filter 的 device 谓词依赖 `calling_conv == DEVICE_KERNEL_LAUNCH`（[lower.py:30-44](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L30-L44)），而这个标记是 `OptimizeForTarget` 里的 `SplitHostDevice` / `LowerDeviceKernelLaunch` 打上去的（[phase.py:262](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L262)、[phase.py:277](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L277)）。没有这些 pass，所有函数都还是默认调用约定，filter 会把所有函数都当作 host，`device_mod` 变空。

---

## 5. 综合实践

**任务**：用 `tilelang.lower` 把一个 quickstart 风格的 matmul kernel「拆」开看，画出从 `PrimFunc` 到 `CompiledArtifact` 的完整数据流，并标注每个字段由哪个阶段产生。

**操作步骤**：

1. 复制 u1-l3 的 quickstart matmul（去掉 relu，保留纯 matmul 即可），但**先不要**加 `@tilelang.jit`，用一个普通函数返回 `@T.prim_func`。
2. 调用 `prim_func = matmul(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)`。
3. 调 `artifact = tilelang.lower(prim_func, target="cuda")`。
4. 依次检视并记录：
   - `artifact.params`：几个参数？每个的 dtype/shape？（由 `lower` 开头的 `extrac_params` 产生）
   - `artifact.device_mod`：用 `artifact.device_mod.functions` 列出 device 函数名。（由三大阶段 + filter 产生）
   - `artifact.kernel_source`：前 500 字符里能不能找到 `__global__`、`wgmma` 或 `mma` 之类痕迹？（由 device codegen 产生，具体指令取决于架构，**待本地验证**）
   - `artifact.rt_mod`：默认应为 `None`。
5. 再跑一次对照：`artifact2 = tilelang.lower(prim_func, target="cuda", enable_host_codegen=True, enable_device_compile=True)`，确认 `artifact2.rt_mod` 非 `None`（模拟 `JITKernel` 的 `tvm_ffi` 路径）。
6. 画一张数据流图：

   ```
   PrimFunc
     └─ extrac_params ──► params
     └─ determine_target ──► Target(sm_XX)
     └─ PreLowerSemanticCheck (校验)
     └─ LowerAndLegalize     ──► 合法化后的 IR
     └─ OptimizeForTarget    ──► 含 host/device 标记的 IR
     └─ Filter               ──► host_mod / device_mod
     └─ device codegen       ──► kernel_source (+ 可选 rt_mod)
     └─ (可选) host codegen  ──► rt_mod
                                   ╰──► CompiledArtifact
   ```

**验收标准**：你能指着图上每个箭头，说出对应源码的位置（lower.py / phase.py 的具体行），并解释 `JITKernel` 默认（`tvm_ffi`）会比「裸 lower」多做哪一步（答：把两个开关都设 `True`，从而拿到 `rt_mod`，再包 adapter）。

> 若本机无 CUDA，可改为「源码阅读型」实践：直接阅读 [lower.py:243-295](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L243-L295) 与 [kernel.py:244-253](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L244-L253)，把上图的每一步标注到具体行号，完成同样的数据流图。

## 6. 本讲小结

- `tilelang.lower` 是编译器主入口：输入 `PrimFunc`/`IRModule` + target，输出 `CompiledArtifact`；`tilelang.compile` / `@tilelang.jit` 是更高一层，在 `lower` 之上包出可调用的 `JITKernel`。
- 编译分三大阶段：`PreLowerSemanticCheck`（只校验）→ `LowerAndLegalize`（合法化 + 布局推理 + 降级 tile op）→ `OptimizeForTarget`（目标相关优化，含软件流水 / warp 特化 / host-device 拆分）。
- target 经 `determine_target` 解析（`"auto"` 自动探测 CUDA/HIP/Metal），host 目标缺省取 `llvm`/`c`，并在阶段 1 开头用 `BindTarget` 绑进模块。
- host/device 拆分靠 `tir.transform.Filter` + `calling_conv` 判定，**必须在 `OptimizeForTarget` 之后**；device codegen 按 target 选后端，两个开关 `enable_host_codegen`/`enable_device_compile` 控制「编译到哪一步」。
- `CompiledArtifact` 五个字段（`host_mod`/`device_mod`/`params`/`kernel_source`/`rt_mod`）分别由不同阶段产出；`rt_mod` 仅在开启 host codegen 时非空，`JITKernel` 的 `tvm_ffi` 后端默认开启它。

## 7. 下一步学习建议

本讲只画了全景。接下来按编译链顺序深入：

1. **u3-l2 前端解析与 TIR**：往**前**看——`@T.prim_func` 是怎么变成 `PrimFunc` 的（`language/parser`、`language/v2`），即本讲 `mod_0` 是怎么来的。
2. **u3-l3 LowerAndLegalize**：往**里**看阶段 1——`LayoutInference`、`LowerTileOp`、负索引合法化等 pass 的算法细节。
3. **u3-l4 OptimizeForTarget**：往**里**看阶段 2——软件流水、warp 特化、`SplitHostDevice` 等。
4. **u3-l5 代码生成与目标后端**：往**后**看——`device_codegen` 选后端、`tilelang_callback_cuda_compile` 如何调 nvcc 并链接 NVSHMEM。
5. **u3-l6 JIT 适配器与运行时**：看 `JITKernel` 如何把 `CompiledArtifact` 包成各 `execution_backend` 的可调用对象。

如果你更关心「为什么这样优化」，也可以直接跳到 **Unit 4**（Layout 推理、软件流水、warp 特化、存储 pass），那里会用到本讲建立的 pass 顺序认知。
