# 编译选项与环境配置

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `TileIROptions` 这个数据类里每一组字段的作用，并区分「TileIR 真正用到的旋钮」与「仅为兼容其它后端而保留的字段」。
- 说清 `TileIREnvConf` 里每一个环境变量的默认值与生效方式，尤其是 `TILEIR_ENABLE_APPROX`、`TILEIR_ENABLE_FTZ` 这两个默认关闭的数值优化开关。
- 复述 `tileiras` 外部编译器路径的「三级解析顺序」，以及为什么 `CUDA_HOME` 要从 `tileiras` 的位置反推而不是读系统环境变量。
- 解释 `occupancy` 与 `num_warps` 在 TileIR 后端下与 NVIDIA PTX 后端的语义差异。

本讲只读源码、不修改任何代码。

## 2. 前置知识

在进入本讲之前，请确认你已经理解以下概念（它们在 u1-l1、u1-l4、u2-l1 中已建立）：

- **编译三阶段**：`make_ttir` → `make_tileir` → `make_cubin`，其中 `make_cubin` 把 IR 交给外部编译器 `tileiras`。
- **旋钮（knob）**：影响编译产物的可调参数，如 `num_warps`、`num_ctas`、`num_stages`、`occupancy`。
- **数据类（dataclass）**：Python 用 `@dataclass` 自动生成 `__init__` 的类，字段带默认值，实例通常是不可变的（这里用了 `frozen=True`）。
- **属性（property）**：用 `@property` 装饰的方法，访问时像字段、但每次访问都会重新执行函数体。
- **denormal / subnormal 浮点数**：非常接近 0 的浮点数，硬件处理它们比处理普通浮点慢，因此有「刷零（flush-to-zero, FTZ）」这种用精度换速度的优化。
- **approx（近似计算）**：fast-math 风格的近似，例如对超越函数做低精度逼近，同样用精度换速度。

一句话回顾：TileIR 与 PTX 两后端共享前端（Python kernel → TTIR），区别在 TTIR 之后的走向；本讲解的就是「TTIR 之后，编译 TileIR 时有哪些旋钮和环境变量在起作用」。

## 3. 本讲源码地图

本讲涉及的关键文件只有两个，加上一处上游交叉引用：

| 文件 | 作用 |
|------|------|
| `third_party/tileir/backend/conf.py` | `TileIREnvConf` 类：集中解析所有影响 TileIR 的环境变量；以及 `tileiras` 路径与 `CUDA_HOME` 的推导；外加 `set_env_var` 上下文管理器。 |
| `third_party/tileir/backend/compiler.py` | `TileIROptions` 数据类（所有编译旋钮）与 `TileIRBackend`（在 `parse_options` 里组装选项、在 `make_tileir` 里把旋钮喂给 MLIR pass、在 `call_tileiras` 里调用外部编译器）。 |
| `python/triton/compiler/compiler.py` | 上游通用编译入口，其中 `metadata = {..., **options.__dict__, ...}` 一行决定了哪些旋钮会被快照进 `metadata`——这是理解 `num_warps` 与 `occupancy` 流向差异的关键。 |

定位口诀：**「选项在 compiler.py，环境变量在 conf.py，二者在 parse_options 里合流」**。

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：

- 4.1 `TileIROptions` 字段
- 4.2 `TileIREnvConf` 环境变量
- 4.3 `tileiras` 路径解析

---

### 4.1 TileIROptions 字段

#### 4.1.1 概念说明

`TileIROptions` 是一个 `@dataclass(frozen=True)`（不可变）的数据类，它保存「编译某一个 kernel 时」使用的全部旋钮。每一次 JIT 编译都会构造一个 `TileIROptions` 实例，它的 `hash()` 决定了这次编译的结果落在缓存的哪个键下。

它有两层来源：

1. **用户显式传入**：`@triton.jit` 装饰器或 `triton.compile(...)` 传进来的 `num_ctas=2` 之类。
2. **环境变量与默认值**：由 `TileIRBackend.parse_options` 在用户没传时用环境变量或硬编码默认值补齐。

字段分三类（这也是源码里用注释分块的方式）：

- **TileIR 核心选项**：真正影响 TileIR 编译产物的旋钮。
- **类型与精度控制**：决定 dot（矩阵乘）输入精度、支持的 fp8 类型等，部分与其它后端兼容。
- **兼容性字段**：TileIR「并不需要」、但为了和 PTX 后端共用同一套 `@triton.jit` 接口而保留的字段（如 `num_warps`、`maxnreg`），通常有固定默认值且不参与实际资源分配。

#### 4.1.2 核心流程

选项的生命周期可以画成一条单向流水线：

```text
用户调用 @triton.jit(... num_ctas=2 ...)
        │
        ▼
TileIRBackend.parse_options(opts)        # 见 4.1.3 源码精读
   1. arch ← TRITON_OVERRIDE_ARCH 或 sm{target.arch}
   2. 用用户传入的 opts 覆盖默认字段
   3. 按 capability 补齐 fp8 类型、imprecise_acc、enable_fp_fusion
        │
        ▼
TileIROptions(**args)  →  __post_init__ 校验 num_warps 为 2 的幂
        │
        ▼
options.__dict__  →  上游 compile() 把它展开进 metadata   # 见 4.1.4
        │
        ▼
make_tileir(opt, metadata, ...)          # opt 与 metadata 都被喂给 MLIR pass
```

关键直觉：**`num_warps`/`num_ctas`/`num_stages` 走的是 `metadata` 这条路，而 `occupancy`/`enable_approx`/`enable_ftz`/`enable_fp_fusion` 走的是 `opt` 这条路。** 这两条路在 `make_tileir` 里汇合。理解这个分流，是本模块最重要的认知。

#### 4.1.3 源码精读

先看核心选项的定义（注释里写明了每个旋钮的语义）：

[third_party/tileir/backend/compiler.py:58-72](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L58-L72) —— 定义 `TileIROptions` 的核心字段：`num_ctas` 默认 1、`num_stages` 默认 3、`opt_level` 默认 3、`occupancy` 默认 1、`enable_fp_fusion` 默认 True，以及 `tileir_tileiras_path`（在类定义处就调用 `TileIREnvConf.get_tileiras_path()` 拿到外部编译器路径）。

> 注意：`tileir_tileiras_path` 的默认值是在**类定义时**（import 时）求值的，而不是每次实例化时。这意味着如果你在 import triton 之后再设 `TRITON_TILEIRAS_PATH`，已 import 的默认值不会变（见 4.3）。

接着是类型与精度字段，以及「兼容性」字段（源码注释直言这些只是为兼容其它后端）：

[third_party/tileir/backend/compiler.py:74-102](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L74-L102) —— 精度字段（如 `allowed_dot_input_precisions` 支持 `tf32/tf32x3/bf16x3/bf16x6/ieee`）与兼容字段（`num_warps=4`、`cluster_dims=(1,1,1)`、`launch_pdl=False` 等）。注释明确指出 `maxnreg`「在 tileir 后端只是为兼容，tileir 用 occupancy 控制寄存器使用」。

最重要的设计点——两个「动态属性」：

[third_party/tileir/backend/compiler.py:107-113](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L107-L113) —— `enable_ftz` 与 `enable_approx` 不是 dataclass 字段，而是 `@property`，每次访问都**实时调用** `TileIREnvConf.enable_ftz()` / `enable_approx()` 读环境变量。

为什么这很重要？因为它是 `@property` 而非字段，它**不会出现在 `options.__dict__` 里**。这直接影响了它如何进入编译流程（见 4.1.4）。

再看 `__post_init__` 的校验与 `hash()`：

[third_party/tileir/backend/compiler.py:115-127](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L115-L127) —— 只校验 `num_warps` 必须是 2 的幂（用经典的 `n & (n-1) == 0` 判定）；`hash()` 则把 `__dict__`（普通字段）和所有 `property` 的值合在一起算 sha256，所以 `enable_ftz`/`enable_approx` 的变化会改变缓存键——即改环境变量会触发重新编译。

最后看选项是如何被组装出来的（环境变量与用户入参的合流点）：

[third_party/tileir/backend/compiler.py:155-182](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L155-L182) —— `parse_options` 先用 `TRITON_OVERRIDE_ARCH`（或 `sm{arch}`）设 `arch`，再用用户传入且非 None 的字段覆盖默认值，最后按 `capability` 补齐 `supported_fp8_dtypes`（sm90+ 加 `fp8e4nv`）、`deprecated_fp8_dot_operand_dtypes`、以及 `enable_fp_fusion`（仅当用户没传时读 `TRITON_DEFAULT_FP_FUSION`），并把 `max_num_imprecise_acc_default` 设为 `2**30`（仅 sm90）或 0。

> 一个容易被忽略的优先级细节：`enable_fp_fusion` 只有在「用户没传」时才读环境变量 `TRITON_DEFAULT_FP_FUSION`。用户显式传值优先级最高，其次是环境变量，最后是 dataclass 默认值 `True`。

#### 4.1.4 代码实践：追踪 opt 与 metadata 的分流

**实践目标**：亲眼确认「`num_warps` 走 metadata、`occupancy` 走 opt」这条分流。

**操作步骤**：

1. 阅读上游编译入口里 metadata 的初始化：

   [python/triton/compiler/compiler.py:291-296](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py#L291-L296) —— `metadata = {"hash": ..., "target": ..., **options.__dict__, **env_vars}`。这一行把 `TileIROptions` 的所有**普通字段**（含 `num_warps`、`num_ctas`、`num_stages`）展开进 `metadata`；但 `enable_ftz`/`enable_approx` 是 `@property`，不在 `__dict__` 里，所以**不会**进入 metadata。

2. 再对照 `make_tileir` 里这些值是怎么被取用的：

   [third_party/tileir/backend/compiler.py:296-320](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L296-L320) —— 注意 `add_triton_to_cudatile` 的实参：`metadata["num_ctas"]`、`metadata["num_warps"]`、`metadata["num_stages"]` 取自 metadata，而 `opt.occupancy`、`opt.enable_approx`、`opt.enable_ftz` 取自 opt。

**需要观察的现象**：`make_tileir` 同时持有 `opt` 和 `metadata` 两个对象，从 `metadata` 取 num_* 三件套、从 `opt` 取 occupancy 与两个 property。这正是分流的证据。

**预期结果**：你能画出这样一张映射表——

| 取值方式 | 来源 | 包含的旋钮 |
|----------|------|-----------|
| `metadata["..."]` | `options.__dict__` 快照 | `num_warps`、`num_ctas`、`num_stages`、`arch` 等 dataclass 字段 |
| `opt.xxx` | 实时访问 options | `occupancy`（字段）、`enable_ftz`/`enable_approx`（property，每次读环境变量） |

**待本地验证**：上面涉及运行期 import 与属性访问，结论可从静态阅读得到，无需运行；若想动态确认，可在装好仓库后执行 `python -c "from triton.backends.tileir.compiler import TileIROptions; o=TileIROptions(arch='sm100'); print('enable_ftz' in o.__dict__, o.enable_ftz)"`（需 GPU 环境，**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `enable_ftz` 被设计成 `@property` 而不是普通 dataclass 字段？

**参考答案**：作为 `@property`，每次访问都实时读 `TILEIR_ENABLE_FTZ` 环境变量；这样用户可以在**两次编译之间**改环境变量，而无需重建 options 对象就能生效。同时它被排除在 `__dict__` 之外，因此不会随 `**options.__dict__` 进入 metadata，避免与 metadata 快照语义冲突；但 `hash()` 里特意用反射把 property 值也算进去，保证环境变量变化会改变缓存键、触发重编译。

**练习 2**：用户既没传 `enable_fp_fusion`，也没设 `TRITON_DEFAULT_FP_FUSION`，最终 fma 融合是否开启？

**参考答案**：开启。`parse_options` 里 `os.getenv("TRITON_DEFAULT_FP_FUSION", "1")` 默认 `"1"`，所以 `enable_fp_fusion=True`，`make_tileir` 中 `if opt.enable_fp_fusion:` 为真，会挂载 `add_fma_fusion` pass。

**练习 3**：`num_warps` 在 TileIROptions 里默认是 4 且被 `__post_init__` 校验为 2 的幂，但 README 说 TileIR「不支持 num_warps」。这两者矛盾吗？

**参考答案**：不矛盾。`num_warps` 字段存在是为了**接口兼容**（让同一份 `@triton.jit` 代码能在两后端间切换不报错），校验只是保证它是个合法值。TileIR 编译器**不真正用它来分配线程资源**——它用 `occupancy` 控制寄存器/驻留块数。`num_warps` 虽然被传进了 `make_tileir` 的 `metadata["num_warps"]`，但当前 CUDA 13.1 的 TileIR 路径并未将其作为资源旋钮使用（详见 4.1.4 与 4.2.4）。

---

### 4.2 TileIREnvConf 环境变量

#### 4.2.1 概念说明

`TileIREnvConf` 是一个只有静态方法的工具类，它把「读环境变量」这件事集中到一个地方，避免散落在各处。它的设计哲学是：**环境变量是「编译期可调的开关」，默认值都偏保守（优先正确性）**。

最值得关注的是两个默认关闭的数值优化：

- `TILEIR_ENABLE_APPROX`（默认 `"0"`）：开启近似计算（fast-math 风格），用精度换性能。
- `TILEIR_ENABLE_FTZ`（默认 `"0"`）：开启刷零（把 denormal 浮点数当 0 处理），用精度换性能。

它们在 u1-l1 已知问题里被点名为「默认关闭的两类数值优化」。本模块把它们和源码对应起来。

#### 4.2.2 核心流程

环境变量的读取遵循一个统一模式：

```text
os.getenv("<VAR>", "<默认值>") == "1"   # 或 != "1" 表示「默认开、可关」
```

默认值的设计有两类：

- 默认 **"0"**（关）：`TILEIR_ENABLE_APPROX`、`TILEIR_ENABLE_FTZ`、`RUN_FULL_TEST`、`NVT_RUN_RELEASE_PIPELINE`、`NVT_TMA_OFFSET_CHECK`。这类是「需要显式打开、偏保守」的开关。
- 默认 **"1"**（开）：`TILEIR_ENABLE_AUTOGEN_ALIAS_MEM_TOKEN`、`TRITON_DEFAULT_FP_FUSION`。这类是「默认就开、必要时可关」的优化。

`make_tileir` 会在转换链里把 approx/ftz/autogen-token/fma 这些开关**烘焙进 IR**（即作为参数传给对应 pass）。一旦烘焙完成，下游的 `tileiras` 外部编译器就再也看不到这些旋钮了——它只看最终的 cuda_tile bytecode。

#### 4.2.3 源码精读

`TileIREnvConf` 的类定义与两个核心数值开关：

[third_party/tileir/backend/conf.py:6-15](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py#L6-L15) —— `enable_approx()` 与 `enable_ftz()`，默认均为 `"0"`（关闭）。注释写明两者都是「牺牲数值精度换取性能」。

内存模型相关的开关：

[third_party/tileir/backend/conf.py:17-19](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py#L17-L19) —— `enable_autogen_alias_mem_token()`，默认 `"1"`（开启）。它控制是否为别名访存自动生成串行化 memory token（对应 u3-l6 的无序内存模型）。`TileIROptions` 里也有同名字段 `enable_autogen_alias_mem_token=True` 作为镜像。

> 注意这个开关在 `make_tileir` 里同时受 opt 字段控制：`add_auto_gen_memtoken(pm, opt.enable_autogen_alias_mem_token)`。opt 字段默认 True，但用户可以在 `@triton.jit` 里传 `enable_autogen_alias_mem_token=False` 关掉（env 变量只是 conf 的默认读取，并未直接接进 opt 字段——opt 字段默认硬编码 True）。

一个**需要诚实说明**的细节——`get_fmad_flag`：

[third_party/tileir/backend/conf.py:21-24](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py#L21-L24) —— `get_fmad_flag()` 读 `TILE_IR_DISABLE_FMAD`（默认 `"0"`，用 `!= "1"` 表示「默认开」）。**但在当前代码库里检索不到任何调用者**（它只在 conf.py 出现一次定义）。也就是说，这个方法目前是「定义了但未接线」的预留钩子。真正在管线里起作用的 FMA 控制是 `enable_fp_fusion`（→ `add_fma_fusion`），不要把它和 `get_fmad_flag` 混为一谈。

其余环境变量（多用于测试/流水线/运行期，而非核心编译 IR 行为）：

[third_party/tileir/backend/conf.py:74-98](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py#L74-L98) —— `get_device()`（`ENABLE_CPU_TORCH`）、`in_nightly_pipeline()`（`RUN_FULL_TEST`）、`in_release_pipeline()`（`NVT_RUN_RELEASE_PIPELINE`）、`get_sm_arch()`、`enable_tma_offset_assert_check()`（`NVT_TMA_OFFSET_CHECK`）。这些偏向测试与发布流水线，本讲只作了解。

最后是一个**很有用的工具**——`set_env_var` 上下文管理器：

[third_party/tileir/backend/conf.py:101-118](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py#L101-L118) —— 临时修改一个环境变量、用完自动恢复原值（即便原值不存在也能正确还原）。它正是运行期 fallback（u4-l3）临时把 `ENABLE_TILE` 置 0 的底层依赖。

#### 4.2.4 代码实践：列出全部影响编译的环境变量表

**实践目标**：把影响 TileIR 编译行为的环境变量整理成一张「变量 / 默认值 / 在哪读 / 作用」的表。

**操作步骤**：

1. 用前面读过的源码，填出下表（默认值以源码 `os.getenv(..., "<默认>")` 的第二个参数为准）：

   | 环境变量 | 默认值 | 读取位置 | 作用（编译行为） |
   |---------|--------|----------|------------------|
   | `TILEIR_ENABLE_APPROX` | `"0"`（关） | `conf.py:10`，经 `opt.enable_approx` 进 `make_tileir` | 开启近似计算（fast-math），烘焙进 IR |
   | `TILEIR_ENABLE_FTZ` | `"0"`（关） | `conf.py:15`，经 `opt.enable_ftz` 进 `make_tileir` | 刷零 denormal，烘焙进 IR |
   | `TILEIR_ENABLE_AUTOGEN_ALIAS_MEM_TOKEN` | `"1"`（开） | `conf.py:19` | 别名访存 memory token 生成（注意 opt 字段默认硬编码 True） |
   | `TILE_IR_DISABLE_FMAD` | `"0"`（即默认开） | `conf.py:24` `get_fmad_flag` | **当前无调用者**，预留钩子 |
   | `TRITON_DEFAULT_FP_FUSION` | `"1"`（开） | `compiler.py:179` | 仅当用户未传 `enable_fp_fusion` 时决定是否挂 fma 融合 pass |
   | `TRITON_OVERRIDE_ARCH` | `sm{target.arch}` | `compiler.py:156` | 覆盖目标架构 capability |
   | `TRITON_TILEIRAS_PATH` | 未设 | `conf.py:29` | 指定外部 `tileiras` 所在目录（见 4.3） |

   另外，`ENABLE_TILE`（u2-l1）虽不属于「编译产物旋钮」，但决定是否启用 TileIR 后端本身。

2. （选做，源码阅读型）确认 `enable_fp_fusion` 的优先级链：用户入参 > `TRITON_DEFAULT_FP_FUSION` > dataclass 默认 `True`。

**需要观察的现象**：表中的「烘焙进 IR」类变量（approx/ftz）一旦在 `make_tileir` 里传给 `add_triton_to_cudatile`，就与后续的 `tileiras` 子进程无关了——`tileiras` 看不到这些旋钮，只看最终 bytecode。

**预期结果**：你能口述「TileIR 默认关闭 approx 与 FTZ，是为了优先保证数值正确性；要开就 `export TILEIR_ENABLE_APPROX=1`」。

**待本地验证**：表内容由静态阅读得到，结论可靠；动态验证（确认改环境变量触发重编译）需 GPU 环境，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`TILEIR_ENABLE_APPROX` 默认是开还是关？为什么这么设计？

**参考答案**：默认关（`"0"`）。因为 approx 是牺牲精度的优化，TileIR 作为孵化期后端优先保证数值正确性，把这类优化设为 opt-in（显式开启）。

**练习 2**：`set_env_var` 与直接 `os.environ["X"]="1"` 有什么本质区别？

**参考答案**：`set_env_var` 是上下文管理器，在 `finally` 里恢复原值（包括「原本不存在」的情况也会正确删除）。它适合「临时改、用完必须还原」的场景，例如运行期 fallback 要临时关掉 `ENABLE_TILE`、切到 PTX 后端、再恢复——绝不能让临时改动泄漏到后续编译。

**练习 3**：既然 `get_fmad_flag()` 没有调用者，为什么还要留在代码里？

**参考答案**：作为预留接口，便于将来把 FMA（乘加融合）控制接到 C++ 层或某个 pass 上；当前真正生效的 FMA 控制是 `enable_fp_fusion`/`TRITON_DEFAULT_FP_FUSION`。阅读源码时要区分「定义存在」与「实际接线」。

---

### 4.3 tileiras 路径解析

#### 4.3.1 概念说明

`tileiras` 是来自 CUDA 13.1 的外部编译器，负责把 cuda_tile 方言的 bytecode 编译成 `.cubin`（GPU 可执行）。它是 `make_cubin` 阶段的唯一执行者。

一个核心设计点是：**`tileiras` 还需要 `ptxas`、`libnvvm`、`libdevice` 等配套工具**，它们都在同一个「CUDA 工具链根目录」下。因此 TileIR 不单独配置 `tileiras` 路径，而是配置「工具链根目录」，再从根目录推导出 `tileiras` 和 `CUDA_HOME`。

#### 4.3.2 核心流程

`get_tileiras_path()` 用三级优先级解析 `tileiras` 的可执行路径：

```text
1. 显式环境变量 TRITON_TILEIRAS_PATH
      → os.path.join(env_path, "tileiras")
2. 构建期随包下载的内置二进制
      → <triton包>/backends/nvidia/tileir_cuda/bin/tileiras
      （需同时满足：文件存在 且 可执行）
3. 系统 PATH
      → shutil.which("tileiras")
4. 都没有 → raise RuntimeError
```

然后 `get_tileir_cuda_home()` 从**已解析出的 `tileiras` 路径**反推 `CUDA_HOME`：

```text
CUDA_HOME = dirname(dirname(tileiras))
        # 即 tileiras 总是位于 <CUDA_HOME>/bin/tileiras
```

这个反推是刻意的：**绝不读取系统 `CUDA_HOME` 环境变量**。

#### 4.3.3 源码精读

`get_tileiras_path()` 的三级解析：

[third_party/tileir/backend/conf.py:26-56](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py#L26-L56) —— 第一优先级是 `TRITON_TILEIRAS_PATH`（拼上 `tileiras`）；第二是内置二进制路径 `backends/nvidia/tileir_cuda/bin/tileiras`，并用 `os.path.isfile(...) and os.access(..., os.X_OK)` 双重校验（存在且可执行）；第三是 `shutil.which("tileiras")` 查 PATH；都没有则抛 `RuntimeError`，错误信息明确列出三种来源。

`get_tileir_cuda_home()` 的反推逻辑：

[third_party/tileir/backend/conf.py:58-72](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py#L58-L72) —— `os.path.dirname(os.path.dirname(tileiras))`。注释详细解释了动机：刻意不读系统 `CUDA_HOME`，这样「一个陈旧/更旧的系统 CUDA（如 13.2）永远不会遮蔽随包带的 13.3 工具链」。也就是说，**工具链版本必须和 `tileiras` 同源**。

这个 `CUDA_HOME` 在哪里被用？在 `call_tileiras` 里，它被注入到子进程环境：

[third_party/tileir/backend/compiler.py:221-225](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L221-L225) —— `tileiras_env = {**os.environ, "CUDA_HOME": TileIREnvConf.get_tileir_cuda_home()}`。注意它**只**注入给 `tileiras` 子进程（`subprocess.run(..., env=tileiras_env)`），不修改全局 `os.environ`，因此不会污染 Triton 主进程的其它 CUDA 调用。

最后看 `tileiras` 命令本身是怎么拼的（这是 `tileir_tileiras_path` 字段最终的消费点）：

[third_party/tileir/backend/compiler.py:205-219](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L205-L219) —— `tileiras = opt.tileir_tileiras_path`，命令为 `[tileiras, --gpu-name=sm_{capability}, --opt-level={opt.opt_level}]`，后面再追加输入 bytecode 文件与 `-o` 输出 cubin。

#### 4.3.4 代码实践：追踪 tileiras 的三级解析

**实践目标**：在不运行编译的前提下，推断当前环境下 `tileiras` 会从哪一级解析、对应的 `CUDA_HOME` 是什么。

**操作步骤**：

1. 阅读上述三个代码点，确认解析顺序与「双重校验」（存在 + 可执行）。
2. 回答下面三个推断题（基于你机器的实际情况）：
   - 若你设了 `export TRITON_TILEIRAS_PATH=/opt/cuda-13.3`，`get_tileiras_path()` 返回什么？`get_tileir_cuda_home()` 又返回什么？
   - 若没设环境变量，但 wheel 里自带了 `backends/nvidia/tileir_cuda/bin/tileiras`，会走哪一级？
   - 若前两级都不满足，`shutil.which("tileiras")` 返回 `None` 会怎样？

**需要观察的现象**：三级之间存在「短路」——一旦某级命中就不再往下。

**预期结果**：

- 设了 `TRITON_TILEIRAS_PATH=/opt/cuda-13.3` → `tileiras` 为 `/opt/cuda-13.3/tileiras`，`CUDA_HOME` 为 `/opt/cuda-13.3`。
- wheel 自带 → 走第二级内置二进制。
- `which` 返回 `None` → 抛 `RuntimeError("tileiras not found: ...")`。

**待本地验证**：以上为静态推断；实际路径取决于安装方式（源码安装通常需自行提供 `tileiras` 或设 `TRITON_TILEIRAS_PATH`，wheel 安装则自带）。可用 `python -c "from triton.backends.tileir.conf import TileIREnvConf as E; print(E.get_tileiras_path())"` 实地确认（需已装好仓库，**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接读系统环境变量 `CUDA_HOME`？

**参考答案**：因为系统 CUDA 可能版本不对（比如装了 13.2，而 `tileiras` 需要 13.3 配套的 `ptxas`/`libnvvm`/`libdevice`）。从 `tileiras` 所在位置反推 `CUDA_HOME`，能保证工具链与 `tileiras` 同源、版本一致，避免被陈旧系统 CUDA 遮蔽。

**练习 2**：`TRITON_TILEIRAS_PATH` 指向的是「`tileiras` 可执行文件本身」还是「它所在的目录」？

**参考答案**：是**目录**。代码用 `os.path.join(env_path, "tileiras")` 拼出可执行文件路径，所以环境变量应设为 `<CUDA_HOME>/bin` 所在的、含 `tileiras` 的目录。相应地反推出的 `CUDA_HOME = dirname(dirname(tileiras))`。

**练习 3**：`tileiras_env` 里的 `CUDA_HOME` 会影响 Triton 主进程吗？

**参考答案**：不会。它通过 `subprocess.run(..., env=tileiras_env)` **只**传给 `tileiras` 子进程，是对 `os.environ` 的拷贝再做局部覆盖，主进程的 `os.environ` 不受影响（见 compiler.py:221-225 的注释「NOT global os.environ」）。

---

## 5. 综合实践

本实践把三个模块串起来，完成规格里要求的两个产出。

### 任务一：整理「影响 TileIR 编译行为」的完整环境变量清单

结合 4.2.4 的表，再补上与编译间接相关的变量，输出一份 Markdown 文档（写在你自己的笔记里，不要写进仓库），至少包含：

1. 直接烘焙进 IR 的开关：`TILEIR_ENABLE_APPROX`、`TILEIR_ENABLE_FTZ`（默认均关）。
2. 影响管线 pass 挂载的开关：`TILEIR_ENABLE_AUTOGEN_ALIAS_MEM_TOKEN`、`TRITON_DEFAULT_FP_FUSION`（默认均开）。
3. 影响目标与工具链：`TRITON_OVERRIDE_ARCH`、`TRITON_TILEIRAS_PATH`。
4. 预留但当前未接线：`TILE_IR_DISABLE_FMAD`（明确标注「无调用者」）。
5. 启用后端本身：`ENABLE_TILE`（u2-l1，非编译旋钮）。

每一项都要标出默认值与「在哪个文件哪一行读取」。

### 任务二：解释 occupancy 与 num_warps 在 TileIR 下与 PTX 后端的语义差异

请用你自己的话写一段说明，要点需覆盖：

- **PTX 后端**：`num_warps` 决定每个线程块的线程数（`threads = num_warps × warp_size`），是直接控制并行度与寄存器占用的旋钮；`num_warps` 越大，单块线程越多、每线程可用寄存器越少。
- **TileIR 后端**：当前（CUDA 13.1）`num_warps` **尚未真正暴露/生效**（README Known issues 原话「`num_warps` is not exposed yet」），字段仅为兼容而保留（默认 4、校验为 2 的幂）；真正控制资源的是 `occupancy`——一个 1 到 32 的整数，表示程序员期望每个 SM 上同时驻留 N 个线程块（见 [README.md:75](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L75) 的描述）。
- **occupancy 的直觉**：SM 的寄存器总量固定。occupancy 越高，编译器就越要压低每个块的寄存器用量，好让更多块同时驻留（提高延迟隐藏能力），但单块性能可能下降；occupancy 为 1 则允许每个块用满寄存器。这是一个「每 SM 并发块数 vs 每块资源预算」的权衡，与 PTX 用 `num_warps` 控制线程数的思路不同。
- **迁移结论**：从 PTX 后端移植 autotune 配置时，`num_warps` 不能直接照搬（往往无效），应改为围绕 `occupancy`（1–32）、`num_ctas`（dot 类负载推荐 2）、更宽的 `num_stages` 重新 autotune。

完成后自查：你能否不看源码就说出 `TILEIR_ENABLE_APPROX` 的默认值，以及 `occupancy` 的取值范围与含义？能则通过。

## 6. 本讲小结

- `TileIROptions` 是冻结的数据类，字段分三类：核心旋钮（`num_ctas`/`num_stages`/`opt_level`/`occupancy`/`enable_fp_fusion`）、精度字段、纯兼容字段（`num_warps`/`maxnreg` 等，TileIR 并不真用）。
- `enable_ftz` 与 `enable_approx` 被特意设计成 `@property`，每次访问实时读环境变量，因此不进 `__dict__`、不进 metadata，但会进 `hash()`（改环境变量会触发重编译）。
- 关键分流：`num_warps`/`num_ctas`/`num_stages` 走 `metadata`（来自 `**options.__dict__`），`occupancy`/`enable_*` 走 `opt`，二者在 `make_tileir` 汇合。
- `TileIREnvConf` 集中解析环境变量，默认偏保守：approx/FTZ 默认关，autogen-token/fp-fusion 默认开。
- `get_fmad_flag`/`TILE_IR_DISABLE_FMAD` 当前**无调用者**，是预留钩子；真正生效的 FMA 控制是 `enable_fp_fusion`。
- `tileiras` 路径三级解析（`TRITON_TILEIRAS_PATH` > 内置二进制 > 系统 PATH），`CUDA_HOME` 从 `tileiras` 位置反推、刻意不读系统变量，且只注入给子进程。

## 7. 下一步学习建议

本讲讲清了「选项与环境变量怎么进来」，但还没讲它们如何被**逐段消费**。建议按以下顺序继续：

- **u2-l3 三段式编译流水线**：把 `make_ttir`/`make_tileir`/`make_cubin` 每一段挂载的 pass 逐个讲清，你会看到本讲的 `occupancy`/`num_stages`/`enable_approx` 到底喂给了哪个 C++ pass。
- **u2-l7 tileiras 外部编译器调用与 cubin 生成**：深入 `call_tileiras` 的错误分类（`OutOfResources` vs `TileirasError`），承接本讲的 `tileiras` 路径解析。
- 若想看 occupancy 等旋钮的实战取值，可先跳读 **u4-l2 性能调优实践** 的 `PerformanceTuningTips.md`，再回来对照本讲的字段定义。
