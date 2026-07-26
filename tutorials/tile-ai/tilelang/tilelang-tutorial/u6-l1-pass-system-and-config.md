# Pass 系统、PassContext 与 PassConfigKey

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **Pass**、**PassContext**、**PassConfigKey** 三个概念各自的职责与三者关系。
- 看懂 tilelang 的 Pass 是如何组织的：Python 侧 `tilelang/transform` 包与 C++ 侧 `src/transform` 的镜像关系，以及 `_ffi_api` 的桥接方式。
- 读懂 `PassConfigKey` 枚举，知道 `tl.*`、`tirx.*`、`tir.*` 三类配置键分别控制什么。
- 追踪 `pass_configs` / `compile_flags` 是如何从用户调用 `@tilelang.jit(...)` 一路传到 `PassContext(config=...)` 的。
- 会用 `TL_ENABLE_DUMP_IR`、`TL_PASS_PROFILE` 等可观测性开关，亲手看到编译期各 Pass 的 IR 与耗时。

本讲是单元 6（Pass 体系与代码生成）的第一讲，承接 u4-l1（编译总流程）。u4-l1 讲了「`lower()` 按 target 查表调度 Pass 流水线」，本讲则钻进这张表本身：Pass 从哪来、如何被组织、又如何被配置开关逐个调控。

## 2. 前置知识

本讲默认你已经掌握 u4-l1 的结论：

- tilelang 的编译主干是 **IRModule 在一串 Pass 之间的流转**，每个 Pass 是「IRModule → IRModule」的纯变换。
- `tilelang.lower()` 是编译总入口，内部 `resolve_pipeline(target)` 按 `target.kind.name` 选出该后端的 Pass 序列。
- Pass 运行时有一个全局的 **PassContext** 容器，承载 `opt_level`、`config`、`instruments` 等运行参数。

此外需要一点点 TVM 的常识：TVM 的 TIR（Tensor IR）是 kernel 的中间表示，`PrimFunc` 是其中的函数节点，`IRModule` 是若干 `PrimFunc` 的容器。本讲不会改动这些 IR 本身，只关注「谁在变换它、变换时读了哪些开关」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tilelang/transform/pass_config.py` | 定义 `PassConfigKey` 枚举（全部配置键）与 `normalize_pass_configs` 归一化函数。本讲的核心文件。 |
| `tilelang/transform/__init__.py` | Python 侧 Pass 的门面：把每个 Pass 包成同名函数，转发到 C++ `_ffi_api`；并再导出 `PassContext`、`get_pass_context`。 |
| `tilelang/transform/simplify.py` | `Simplify` / `simplify_prim_func` 等纯 Python 辅助封装，演示 Pass 的轻量包装风格。 |
| `tilelang/backend/pass_pipeline/pipeline.py` | `PassPipeline` 注册表：`register_pipeline` / `resolve_pipeline`，决定「哪个后端跑哪串 Pass」。 |
| `tilelang/backend/pass_pipeline/pipeline_utils.py` | 一组「读 PassContext 配置」的谓词（`allow_vectorize` 等），是「Pass 内部如何读配置键」的范例。 |
| `tilelang/cuda/pipeline.py` | CUDA 后端真实的 Pass 序列（约 50 个 Pass），也是配置键被消费的现场。 |
| `tilelang/jit/kernel.py` | JIT 层把用户的 `pass_configs` / `compile_flags` 收拢、注入 instruments、最终 `with PassContext(...)` 打开上下文的地方。 |
| `src/config.h` | C++ 侧用 `PassContext::Current()->GetConfig(...)` 读配置键的范例。 |
| `tilelang/utils/pass_timing.py` | Pass 计时仪器 `TileLangPassTimingInstrument`，对应 `TL_PASS_PROFILE`。 |

## 4. 核心概念与源码讲解

### 4.1 Pass、PassContext、PassConfigKey：三者关系

#### 4.1.1 概念说明

在 tilelang 里，**Pass 就是一道 IR 变换函数**，签名恒为 `IRModule -> IRModule`。例如「把 `T.gemm` 占位展开成 WGMMA 指令」「把循环向量化」「合并 shared memory 分配」都是一个个 Pass。整条编译流水线就是「把几十个 Pass 串成一个序列，让 IRModule 依次流过」。

但每个 Pass 在变换时往往需要一些「开关」：要不要开启向量化？要不要做激进 shared memory 合并？要不要 dump 中间 IR？这些开关不能写死在 Pass 里，否则换一个场景就得改源码。于是需要一个**运行时配置容器**——这就是 **PassContext**。

三者职责可以这样区分：

- **Pass**：干活的变换。`Simplify`、`LayoutInference`、`LowerTileOp` 都是 Pass。
- **PassContext**：配置容器。它持有 `opt_level`（优化等级）、`config`（一个字典，存放所有开关键值对）、`instruments`（观察 Pass 执行的仪器，如 dump IR、计时）。任一时刻有一个「当前上下文」`PassContext.current()`，所有 Pass 都从它读取配置。
- **PassConfigKey**：配置键的**枚举字典表**。它本身不存值，只是把「这个开关叫什么字符串名字、含义是什么」集中记录，避免到处写魔法字符串 `"tl.enable_fast_math"`。

一句话：**PassConfigKey 是键名表，PassContext 是装着键值对的运行时容器，Pass 是消费这些键值对的变换。**

#### 4.1.2 核心流程

配置从用户到 Pass 的数据流：

```text
用户写 pass_configs={...}
        │
        ▼
@tilelang.jit / tilelang.compile   （tilelang/jit/__init__.py）
        │
        ▼
normalize_pass_configs(...)         （归一化 PassConfigKey→字符串、弃用告警）
        │
        ▼
with tvm.transform.PassContext(config=pass_configs, instruments=...):
        │   ← 这里建立了「当前上下文」
        ▼
tilelang.lower(...)  →  resolve_pipeline(target)  →  Pass 序列依次执行
        │
        ▼
每个 Pass 内部用 PassContext.current().config.get("键名") 读取自己的开关
```

关键点：PassContext 是用 Python 的 `with` 语句打开的**线程局部上下文**。在 `with` 块内，`PassContext.current()` 返回这个上下文；块结束后配置失效。所以配置只对「这一次 lower 调用」生效。

#### 4.1.3 源码精读

`tilelang/transform/__init__.py` 把 TVM 的 `PassContext` 直接再导出，并提供拿到当前上下文的便捷函数：

[tilelang/transform/__init__.py:8-16](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/__init__.py#L8-L16) —— 再导出 `PassContext`，并提供 `get_pass_context()` 返回 `PassContext.current()`。

而 `PassConfigKey` 是一个 `(str, Enum)`：它既是一个枚举成员，**其值本身就是配置键字符串**。这是 tilelang 一贯的设计——你可以直接用枚举成员当字典 key，也可以用裸字符串，二者等价：

[tilelang/transform/pass_config.py:10-11](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/pass_config.py#L10-L11) —— `class PassConfigKey(str, Enum)`，每个成员的 `.value` 就是写进 PassContext 的字符串键。

举一个最典型的配置键，开启它会让 nvcc 加上 `--use_fast_math`：

[tilelang/transform/pass_config.py:61-65](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/pass_config.py#L61-L65) —— `TL_ENABLE_FAST_MATH = "tl.enable_fast_math"`，文档说明开启后会向 nvcc 传 `--use_fast_math`。

它最终被消费的地方，是 `engine/lower.py` 里的 CUDA 编译回调（见 4.5 节）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「PassConfigKey 的成员值 == PassContext 里用的字符串键」。

**步骤**：

1. 在本地 `python` 交互环境执行：
   ```python
   import tilelang
   k = tilelang.PassConfigKey.TL_ENABLE_FAST_MATH
   print(repr(k), "|", k.value, "|", k == "tl.enable_fast_math")
   ```
2. 观察输出：`k.value` 应为 `"tl.enable_fast_math"`，且 `k == "tl.enable_fast_math"` 为 `True`。

**预期结果**：你会看到枚举成员同时具备「名字 `TL_ENABLE_FAST_MATH`」与「字符串值 `tl.enable_fast_math`」，并且能与裸字符串直接相等比较——这正是它既可读又不易写错的原因。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PassConfigKey` 要继承 `(str, Enum)` 而不是普通 `Enum`？

**参考答案**：因为它的成员值就是要写进 `PassContext.config` 的字符串键。继承 `str` 后，枚举成员本身就是字符串，既能 `== "tl.xxx"` 比较，又能直接当 dict 的 key（`config[PassConfigKey.XXX]`），且 `json.dumps` 时会序列化成字符串，免去手动 `.value` 转换。

**练习 2**：如果同一进程里先后用两套不同的 `pass_configs` 编译两个 kernel，会互相干扰吗？

**参考答案**：不会。`pass_configs` 是通过 `with PassContext(config=...)` 打开的线程局部上下文，`with` 块结束后即恢复，下一次编译打开新的上下文，彼此隔离。

---

### 4.2 Pass 的组织：Python transform 包与 C++ src/transform 的镜像

#### 4.2.1 概念说明

tilelang 的 Pass 实现**分两面**：

- **C++ 实现面**（`src/transform/`、`src/cuda/` 等）：Pass 的真正算法逻辑写在 C++ 里（`LayoutInference`、`LowerTileOp`、`InjectSoftwarePipeline`……约 39 个 Pass）。这是为了性能与对 TVM IR 的直接操作。
- **Python 门面面**（`tilelang/transform/`、`tilelang/cuda/transform/` 等）：每个 Pass 都有一个同名的 Python 薄函数，它**不做任何变换**，只负责转发到 C++。

二者通过 TVM 的 FFI（Foreign Function Interface）连接：C++ 侧用 `TVM_REGISTER_GLOBAL` 注册一个全局函数，Python 侧用 `_ffi_api.XXX()` 调用它。这套「C++ 注册、Python 转发」的模式贯穿整个 tilelang（u1-l3 已介绍），Pass 体系也不例外。

> 也有少量 Pass 是**纯 Python** 的薄包装，例如 `Simplify` 之上的 `simplify_prim_func` 装饰器；它们只是把「调用 Pass + 包成 IRModule + 取回结果」的样板封装得更好用。

#### 4.2.2 核心流程

一个 Python 侧 Pass 函数的固定骨架是：

```python
def LowerTileOp():
    """..."""
    return _ffi_api.LowerTileOp()   # 转发到 C++，返回一个 tvm.transform.Pass 对象
```

返回值 `tvm.transform.Pass` 是 TVM 的 Pass 对象，它**可调用**：`mod = LowerTileOp()(mod)` 即把一个 IRModule 变换成另一个 IRModule。

后端的整条流水线，就是把这些 Pass 对象**按固定顺序**手动串起来。例如 CUDA 后端的 `CUDAPassPipelineBody` 就是一个「`mod = passA()(mod); mod = passB()(mod); ...`」的长函数（见 4.2.3）。而「哪个后端对应哪个长函数」由 `PassPipeline` 注册表决定。

#### 4.2.3 源码精读

先看 Python 门面。`tilelang/transform/__init__.py` 里几乎每个函数都是同一个模板，以 `LowerTileOp` 为例：

[tilelang/transform/__init__.py:52-60](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/__init__.py#L52-L60) —— `LowerTileOp()` 仅返回 `_ffi_api.LowerTileOp()`，真正算法在 C++。

带参数的 Pass 则把参数透传给 FFI，例如 `VectorizeLoop` 接受一个 `enable_vectorize` 开关：

[tilelang/transform/__init__.py:260-268](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/__init__.py#L260-L268) —— `VectorizeLoop(enable_vectorize=True)` 把开关透传给 C++ 实现。

再看注册表。`PassPipeline` 是「名字 + 一个 lower 函数」的二元组，`register_pipeline` 按 `target.kind.name` 存入字典，`resolve_pipeline` 按名字取出：

[tilelang/backend/pass_pipeline/pipeline.py:11-23](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/pass_pipeline/pipeline.py#L11-L23) —— `PassPipeline` 类，`lower(mod, target)` 就是调用构造时传入的那个长函数。

[tilelang/backend/pass_pipeline/pipeline.py:46-48](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/pass_pipeline/pipeline.py#L46-L48) —— `resolve_pipeline(target)` 用 `target.kind.name`（如 `"cuda"`、`"hip"`）查表。

最后看真实序列。CUDA 后端在导入时注册自己的 pipeline：

[tilelang/cuda/pipeline.py:257-259](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L257-L259) —— `PassPipeline("cuda", CUDAPassPipelineBody)` 并注册。

其前半段（prologue）就体现了「Pass 序列 + 配置开关条件分支」的典型写法：

[tilelang/cuda/pipeline.py:68-117](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L68-L117) —— CUDA prologue：`MaterializeKernelLaunch → LegalizeNegativeIndex → InjectAssumes → Simplify → ... → PipelinePlanning → InjectSoftwarePipeline → LayoutInference → LowerTileOp`。注意 `should_force_let_inline()`、`should_enable_race_check()` 这些「读配置」的谓词直接决定了某些 Pass 是否执行。

作为「纯 Python 薄包装」的典型，`simplify.py` 把「调用 C++ `Simplify` + 处理 IRModule 包装」封成了装饰器：

[tilelang/transform/simplify.py:20-28](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/simplify.py#L20-L28) —— `Simplify(simplify_arguments=False)` 仍转发到 `_ffi_api.Simplify`。

[tilelang/transform/simplify.py:53-58](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/simplify.py#L53-L58) —— `simplify_prim_func` 装饰器：自动把被装饰函数的输出过一个 `Simplify` Pass。

#### 4.2.4 代码实践（源码阅读型）

**目标**：用 `get_pass_context()` 在 Pass 执行现场读到上下文，验证「PassContext 在 lower 期间确实存在」。

**步骤**：阅读 [tilelang/cuda/pipeline.py:141-144](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L141-L144)，注意 `CUDAPassPipelineBody` 一进来就调用 `tilelang.transform.get_pass_context()` 取上下文。这说明：**pipeline 函数整体就是在 `with PassContext(...)` 块内被 `lower()` 调用的**，因此这里能稳定拿到当前配置。

**预期结果**：你能在脑中画出「JIT 打开 PassContext → lower() → resolve_pipeline → pipeline 函数内 get_pass_context() 取到同一个上下文」的调用栈。

#### 4.2.5 小练习与答案

**练习 1**：如果我想新增一个 C++ Pass 给 tilelang，Python 侧需要做什么？

**参考答案**：在 `tilelang/transform/__init__.py` 里新增一个同名薄函数 `def MyPass(): return _ffi_api.MyPass()`（假设 C++ 侧已 `TVM_REGISTER_GLOBAL("tirx.transform.MyPass")` 且经 `init_ffi_api` 暴露）。然后视情况把它插入到对应后端 pipeline 的合适位置（如 `tilelang/cuda/pipeline.py`）。Python 侧本身不含算法逻辑。

**练习 2**：`resolve_pipeline(target)` 用 `target.kind.name` 作为查找键，这意味着什么？

**参考答案**：Pass 序列是**按后端 kind** 隔离的——`cuda`、`hip`、`metal`、`webgpu`、`c`/`llvm` 各有一套独立注册的 pipeline。CuTeDSL 后端 kind 仍是 `cuda`（u4-l4 已述），所以它复用 `CUDAPassPipelineBody`，仅靠 target 的额外标签在 device codegen 阶段分流。

---

### 4.3 PassConfigKey：配置键的全集与分类

#### 4.3.1 概念说明

`PassConfigKey` 把 tilelang 所有受 PassContext 控制的开关集中在一个枚举里，按命名前缀分成三大类：

| 前缀 | 含义 | 典型示例 |
| --- | --- | --- |
| `tl.*` | tilelang 自有配置：控制 tilelang 各个自有 Pass 的行为、设备编译选项、可观测性 | `tl.enable_fast_math`、`tl.disable_vectorize_256`、`tl.enable_dump_ir` |
| `tirx.*` | tilelang 对 TVM TIR 通用 Pass 的开关封装（`tirx` ≈ tilelang 版的 tir） | `tirx.disable_vectorize`、`tirx.Simplify`、`tirx.use_async_copy` |
| `tir.*` | 直接沿用上游 TVM 的原生配置键 | `tir.detect_global_barrier`、`tir.enable_equiv_terms_in_cse_tir` |
| `cuda.*` | 输出目录类配置 | `cuda.kernels_output_dir` |

设计意图很清晰：**`tl.*` 是 tilelang 自己的扩展面**，`tirx.*` 是「tilelang 想覆盖 TVM 默认行为的薄封装」，`tir.*` 则原样转发给上游。这样读者只看键名前缀就能判断「这个开关归谁管」。

每个成员都有详尽的 docstring，说明默认值与副作用，这是 tilelang 里「配置即文档」的体现。

#### 4.3.2 核心流程

配置键的生命周期：

1. **定义**：在 `pass_config.py` 里以 `XXX = "tl.xxx"` 形式登记，附 docstring。
2. **传入**：用户用枚举成员或字符串作 key 写进 `pass_configs` 字典。
3. **归一化**：`normalize_pass_configs` 把枚举成员转回字符串、对弃用键发告警。
4. **注入**：JIT 把字典塞进 `PassContext(config=...)`。
5. **读取**：Pass 在执行时 `config.get("tl.xxx", 默认值)` 取值。

#### 4.3.3 源码精读

配置键按功能簇分布。先看「设备编译选项」簇——它们不改变 IR，但改变 nvcc 的行为：

[tilelang/transform/pass_config.py:61-83](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/pass_config.py#L61-L83) —— `TL_ENABLE_FAST_MATH`（nvcc `--use_fast_math`）、`TL_PTXAS_REGISTER_USAGE_LEVEL`（ptxas 寄存器用量等级）、`TL_DEVICE_COMPILE_FLAGS`（任意额外 nvcc/NVRTC 选项）。

再看「可观测性」簇——本讲实践要用的两个键就在这里：

[tilelang/transform/pass_config.py:280-290](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/pass_config.py#L280-L290) —— `TL_ENABLE_DUMP_IR`（开 IR 落盘）、`TL_DUMP_IR_DIR`（落盘目录，默认 `./dump_ir/`）、`TL_PASS_PROFILE`（开 Pass 计时）、`TL_PASS_PROFILE_THRESHOLD_MS`（只显示慢于此阈值的 Pass）。

`TL_SIMPLIFY` 则展示了一种**字典型配置**——一个键的值本身又是一个子字典，控制 `Simplify` Pass 的多个子开关：

[tilelang/transform/pass_config.py:15-31](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/pass_config.py#L15-L31) —— `tl.Simplify` 是 dict 配置，含 `enable_simplify_let_inline`、`transitively_prove_inequalities` 等子项；用法示例展示了「`with PassContext(config={"tl.Simplify": {...}})`」的标准写法。

弃用键有专门的告警表，`normalize_pass_configs` 在归一化时发出 `DeprecationWarning`：

[tilelang/transform/pass_config.py:293-297](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/pass_config.py#L293-L297) —— 弃用键 `tl.disable_tma_lower` 的迁移提示，引导改用 `T.copy(..., disable_tma=True)`。

#### 4.3.4 代码实践（源码阅读型）

**目标**：浏览 `PassConfigKey` 全集并按前缀归类。

**步骤**：

1. 打开 [tilelang/transform/pass_config.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/pass_config.py)，从上到下扫读每个成员的 docstring。
2. 列出三个表格：`tl.*`（tilelang 自有）、`tirx.*`（TIR 封装）、`tir.*`/`cuda.*`（上游/输出）。

**预期结果**：你会发现 `tl.*` 数量最多，覆盖 fast_math、async_copy、wgmma、shared memory 合并、layout 可视化、dump/profile 等几乎所有 tilelang 特性开关；`tirx.*` 主要控制向量化、CSE、storage rewrite、async copy 等 TVM 通用优化。

#### 4.3.5 小练习与答案

**练习 1**：用户想关掉向量化，应该用 `tl.*` 还是 `tirx.*` 的键？

**参考答案**：用 `PassConfigKey.TIR_DISABLE_VECTORIZE`（`"tirx.disable_vectorize"`）。向量化是 TVM TIR 的通用 Pass，tilelang 用 `tirx.` 前缀封装了对它的开关。CUDA pipeline 里 `allow_vectorize()` 谓词正是读这个键（见 4.5 节）。

**练习 2**：`tl.Simplify` 这种「值是字典」的配置，相比拆成多个扁平键有什么好处？

**参考答案**：把同一 Pass 的多个相关子开关收拢到一个命名空间下，避免键名爆炸（如 `tl.simplify_enable_let_inline`、`tl.simplify_transitive_prove`……）。也便于把整个子配置作为一个对象传递与序列化。

---

### 4.4 从 JIT 到 PassContext：pass_configs / compile_flags 的流转

#### 4.4.1 概念说明

用户通常不会自己写 `with PassContext(...)`，而是通过 `@tilelang.jit(pass_configs={...})` 或 `tilelang.compile(..., pass_configs={...})` 传入。本节追踪这两个参数如何一路流到 PassContext：

- **`pass_configs`**：一个「配置键 → 值」的字典，最终原样（归一化后）成为 `PassContext.config`。
- **`compile_flags`**：额外的 **nvcc/NVRTC 设备编译选项**（如 `["-O3", "--ptxas-options=-v"]`）。它**不是** Pass 行为开关，而是设备编译器选项，所以 tilelang 会把它**合并进** `tl.device_compile_flags` 这个配置键，再随 `pass_configs` 一起进入 PassContext，最终在 CUDA 编译回调里被取出。

二者最终汇流到同一个 `PassContext(config=pass_configs)` 调用。理解这一点很重要：`compile_flags` 没有独立通道，它是「搭便车」走 `tl.device_compile_flags` 这个键进入 PassContext 的。

#### 4.4.2 核心流程

JIT 层（`JITKernel._build`）的处理流程：

```text
1. self.pass_configs = normalize_pass_configs(用户传入的 pass_configs)
       —— 枚举成员转字符串、弃用键告警

2. 若用户传了 compile_flags：
       把它合并进 pass_configs["tl.device_compile_flags"]
       （已有的额外 flags + 用户新 flags）

3. 若 TL_ENABLE_DUMP_IR 为真：
       base_pass_instruments 追加 tvm.ir.instrument.DumpIR(dump_dir=...)

4. 若 TL_PASS_PROFILE 为真（或环境变量 TILELANG_PASS_PROFILE）：
       构造 TileLangPassTimingInstrument，插入 instruments

5. with tvm.transform.PassContext(opt_level=3,
                                  config=pass_configs,
                                  instruments=pass_instruments):
       tilelang.lower(...)   # Pass 序列在此上下文内执行
```

#### 4.4.3 源码精读

JIT 构造时先做归一化（注意 `normalize_pass_configs` 的导入）：

[tilelang/jit/kernel.py:29-106](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L29-L106) —— 导入 `normalize_pass_configs`；构造函数把用户 `pass_configs` 经归一化后存为 `self.pass_configs`。

`compile_flags` 的合并逻辑（关键：它被搭进 `tl.device_compile_flags`）：

[tilelang/jit/kernel.py:227-232](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L227-L232) —— 若 `compile_flags` 非空，取出已有的 `tl.device_compile_flags`（可能为 None），把二者拼接后写回同一键。

可观测性 instruments 的装配——DumpIR：

[tilelang/jit/kernel.py:240-242](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L240-L242) —— 读 `TL_ENABLE_DUMP_IR`，若为真则加一个 `tvm.ir.instrument.DumpIR(dump_dir=TL_DUMP_IR_DIR 默认 ./dump_ir)`。

Pass 计时的阈值解析（配置键与环境变量二选一）：

[tilelang/jit/kernel.py:245-255](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L245-L255) —— 若 `TL_PASS_PROFILE` 或环境变量 `TILELANG_PASS_PROFILE` 任一开启，则解析阈值（优先配置键，回退环境变量），再用 `build_pass_instruments` 把计时仪器插到 instruments 最前。

最终打开上下文、调用 lower：

[tilelang/jit/kernel.py:268-283](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L268-L283) —— 用 `with` 同时打开「计时报告上下文 + 阶段日志 + `PassContext(opt_level=3, config=pass_configs, instruments=...)` + target」，并在其中调用 `tilelang.lower(...)`。这就是所有 Pass 运行时的「当前上下文」。

归一化函数本身很短：

[tilelang/transform/pass_config.py:300-317](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/pass_config.py#L300-L317) —— 遍历用户字典，把 `PassConfigKey` 成员转成 `.value` 字符串，并对弃用键发一次 `DeprecationWarning`。

> 补充：`tilelang.compile()` 还支持**函数级**配置——用户可在 `@T.prim_func` 上挂 `tilelang_pass_configs` / `tilelang_compile_flags` 属性，`compile()` 会把它们与外部传入的合并（外部覆盖函数级），见 [tilelang/jit/__init__.py:146-159](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L146-L159)。

#### 4.4.4 代码实践（动手型）

**目标**：用 `pass_configs` 同时开启 IR dump 与 fast_math，确认二者都生效。

**步骤**：把下面这段示例代码保存为 `pass_cfg_demo.py`（**示例代码**，非项目自带文件；kernel 改写自 `examples/quickstart.py` 的 GEMM）。

```python
# 示例代码
import torch
import tilelang
import tilelang.language as T

@tilelang.jit(
    pass_configs={
        tilelang.PassConfigKey.TL_ENABLE_FAST_MATH: True,   # nvcc --use_fast_math
        tilelang.PassConfigKey.TL_ENABLE_DUMP_IR: True,     # 落盘各 Pass 的 IR
        tilelang.PassConfigKey.TL_DUMP_IR_DIR: "./dump_ir_demo",  # 指定目录
    },
)
def matmul_kernel(M=N=K=512):
    # 标准的 tile 级 GEMM 蓝图（细节见 u1-l4 / u3-l1）
    ...

if __name__ == "__main__":
    kernel = matmul_kernel()
    # 1) 观察 dump_ir_demo/ 目录里是否出现按 Pass 命名的 IR 文件
    # 2) 开启 TILELANG_VERBOSE=1 再次运行，留意编译日志里的 --use_fast_math
```

**需要观察的现象**：

1. 运行后工程目录下应出现 `dump_ir_demo/` 文件夹，内含每个 Pass 执行后的 IR（文件名通常含 Pass 名与序号）。
2. 以 `TILELANG_VERBOSE=1 python pass_cfg_demo.py` 运行，编译日志的 nvcc 选项里应能看到 `--use_fast_math`。

**预期结果**：`dump_ir_demo/` 非空；verbose 日志含 `--use_fast_math`。若你的环境无 GPU，无法真正触发 nvcc 编译，则 fast_math 那条**待本地验证**；但 DumpIR 仪器是在 PassContext 层挂载的，理论上 lower 阶段就会落盘（同样建议在有 GPU 环境验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `compile_flags` 最终会被塞进 `pass_configs["tl.device_compile_flags"]`，而不是单独作为 `PassContext` 的一个参数？

**参考答案**：因为 `PassContext.config` 是 TVM 提供的统一配置通道，所有「在 Pass 流水线内或编译回调内需要读取的运行时参数」都走它。把 `compile_flags` 搭进 `tl.device_compile_flags` 这个键，就能让 CUDA 编译回调（`tilelang_callback_cuda_compile`）通过同一个 `pass_config` 参数取到，无需开辟第二条传参路径。

**练习 2**：`TL_PASS_PROFILE` 既可由 `pass_configs` 设置，也可由环境变量 `TILELANG_PASS_PROFILE` 设置，二者同时存在时谁优先？

**参考答案**：在 [tilelang/jit/kernel.py:246-251](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L246-L251) 中，只要**任一**开启就启用计时；而阈值通过 `resolve_pass_profile_threshold_ms` 解析，**配置键优先**于环境变量（配置键存在则用配置键，否则回退到 `env.get_pass_profile_threshold_ms()`）。

---

### 4.5 在 Pass 内读取配置：Python 谓词与 C++ GetConfig

#### 4.5.1 概念说明

配置写进 PassContext 后，**谁在读它**？两类读者：

- **Python 谓词**：pipeline 函数里用一个小函数把「读配置 + 返回布尔」封装成谓词，直接决定「这个 Pass 跑不跑 / 怎么跑」。例：`allow_vectorize()` 读 `tirx.disable_vectorize`，决定 `VectorizeLoop` 是否真的向量化。
- **C++ Pass 内部**：C++ Pass 用 `PassContext::Current()->GetConfig("键名", Optional<类型>())` 读取，常封装在 `src/config.h` 的 inline 小函数里。

此外还有一类**特殊读者**：编译回调 `tilelang_callback_cuda_compile`。它在 IR 流水线之外（代码生成之后）读 `tl.enable_fast_math`、`tl.device_compile_flags` 等键来组装 nvcc 命令行。这解释了为什么 fast_math 这类键虽然是「设备编译选项」，却仍走 PassContext 通道——因为 TVM 的 codegen 回调签名里能拿到 `pass_config`。

#### 4.5.2 核心流程

Python 谓词的统一写法：

```python
def allow_vectorize(pass_ctx=None):
    if pass_ctx is None:
        pass_ctx = tilelang.transform.get_pass_context()
    disable_vectorize = pass_ctx.config.get("tirx.disable_vectorize", False)
    return not disable_vectorize
```

C++ 读者的统一写法：

```cpp
inline bool Vectorize256Disabled() {
  auto ctxt = transform::PassContext::Current();
  return ctxt->GetConfig("tl.disable_vectorize_256", ffi::Optional<Bool>())
      .value_or(Bool(false));
}
```

编译回调读者的写法（Python）：

```python
cfg = pass_config or {}
enable_fast_math = bool(cfg.get(PassConfigKey.TL_ENABLE_FAST_MATH, False))
```

三者形态不同，但本质一致：**从当前 PassContext 取键，带默认值**。

#### 4.5.3 源码精读

Python 谓词集中住在 `pipeline_utils.py`。最典型的 `allow_vectorize`：

[tilelang/backend/pass_pipeline/pipeline_utils.py:10-14](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/pass_pipeline/pipeline_utils.py#L10-L14) —— 读 `tirx.disable_vectorize`，默认 `False`，取反返回。CUDA pipeline 的 `VectorizeLoop(enable_vectorize=allow_vectorize(...))` 直接消费它。

同文件的 `should_enable_aggressive_merge`、`should_force_let_inline` 则读 `tl.*` 键：

[tilelang/backend/pass_pipeline/pipeline_utils.py:24-34](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/pass_pipeline/pipeline_utils.py#L24-L34) —— 分别读 `tl.enable_aggressive_shared_memory_merge` 与 `tl.force_let_inline`。

CUDA pipeline 在 `MergeSharedMemoryAllocations` 处就是用这些谓词把配置喂给 Pass 的：

[tilelang/cuda/pipeline.py:222-224](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L222-L224) —— `should_enable_aggressive_merge(...)` / `should_disable_shared_memory_reuse(...)` 的返回值成为 `MergeSharedMemoryAllocations` 的实参。

C++ 侧的读者范例在 `src/config.h`：

[src/config.h:19-33](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/config.h#L19-L33) —— `VectorizePlannerVerboseEnabled()` 与 `Vectorize256Disabled()` 用 `PassContext::Current()->GetConfig(...)` 读 `tl.enable_vectorize_planner_verbose` / `tl.disable_vectorize_256`，`.value_or(Bool(false))` 给出默认值。

最后是编译回调读者——fast_math 真正变成 nvcc 选项的地方：

[tilelang/engine/lower.py:101-114](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L101-L114) —— `tilelang_callback_cuda_compile(code, target, pass_config)` 从 `pass_config` 读 `tl.enable_fast_math`（与 `tl.ptxas_register_usage_level`）。

[tilelang/engine/lower.py:144-145](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L144-L145) —— `if enable_fast_math: options.append("--use_fast_math")`。这就是 4.4 实践里 verbose 日志会看到的那条选项的来源。

Pass 计时仪器则展示了一种「不读配置、但由配置驱动挂载」的可观测性手段：

[tilelang/utils/pass_timing.py:104-118](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/utils/pass_timing.py#L104-L118) —— `TileLangPassTimingInstrument` 在 `run_before_pass`/`run_after_pass` 之间用 `time.monotonic()` 记录每个 Pass 的 inclusive/self 时长，由 `TL_PASS_PROFILE` 触发挂载。

#### 4.5.4 代码实践（动手型）

**目标**：用 `TL_PASS_PROFILE` 看到 CUDA 编译各 Pass 的耗时排名。

**步骤**：

```python
# 示例代码
import tilelang, tilelang.language as T

@tilelang.jit(pass_configs={
    tilelang.PassConfigKey.TL_PASS_PROFILE: True,
})
def matmul_kernel(M=N=K=512):
    ...

kernel = matmul_kernel()
```

或等价地设环境变量 `TILELANG_PASS_PROFILE=1` 后编译任意 GEMM。

**需要观察的现象**：PassContext 退出时（`report_pass_timing_on_exit`），日志会打印一张「Pass 名 / inclusive 时长 / self 时长 / 占比 / Top 10 最慢」的报告表。

**预期结果**：你能看到 `LowerTileOp`、`LayoutInference`、`StorageRewrite`、`VectorizeLoop` 等 Pass 的耗时排序，找出编译瓶颈所在 Pass。报告格式见 [tilelang/utils/pass_timing.py:163-215](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/utils/pass_timing.py#L163-L215)。完整运行输出**待本地验证**（需要可编译的 GPU 环境）。

#### 4.5.5 小练习与答案

**练习 1**：`allow_vectorize()` 读的是 `tirx.disable_vectorize`，但用户在 `PassConfigKey` 里对应的是哪个成员？

**参考答案**：`PassConfigKey.TIR_DISABLE_VECTORIZE`（值为 `"tirx.disable_vectorize"`），定义见 [tilelang/transform/pass_config.py:254-255](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/pass_config.py#L254-L255)。

**练习 2**：为什么 `pipeline_utils.py` 里每个谓词都接受一个可选的 `pass_ctx=None`，并在为 None 时调用 `get_pass_context()`？

**参考答案**：为了**复用上下文、避免重复取**。pipeline 函数开头已经取过一次 `pass_ctx = get_pass_context()`（见 [tilelang/cuda/pipeline.py:142-144](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L142-L144)），把它显式传给谓词可省去每个谓词各自再 `current()` 一次；而默认 `None` 又保证谓词能独立调用（如单元测试）。

## 5. 综合实践

把本讲三个核心概念（PassConfigKey 配置、PassContext 流转、Pass 内读取）串起来，做一次「带完整可观测性的 GEMM 编译」：

1. **准备 kernel**：参考 `examples/quickstart.py`（或 `examples/gemm/example_gemm.py`）写一个可跑的 tile 级 GEMM（`T.Kernel` + `T.copy` + `T.gemm` + `T.Pipelined`，详见 u1-l4、u3-l1）。
2. **挂三组配置**编译它：
   ```python
   pass_configs={
     tilelang.PassConfigKey.TL_ENABLE_FAST_MATH: True,
     tilelang.PassConfigKey.TL_ENABLE_DUMP_IR: True,
     tilelang.PassConfigKey.TL_DUMP_IR_DIR: "./dump_ir_gemmdemo",
     tilelang.PassConfigKey.TL_PASS_PROFILE: True,
   }
   ```
3. **读 dump IR**：打开 `dump_ir_gemmdemo/`，找到 `LowerTileOp`、`LayoutInference`、`InjectSoftwarePipeline` 三个 Pass 对应的 IR 文件，对照 u6-l2（后续讲义）确认：`T.gemm` 占位是在 `LowerTileOp` 处被展开成底层指令的、布局是在 `LayoutInference` 处被推断出来的。
4. **读 pass profile**：从打印的耗时表里找出该 GEMM 编译最慢的 3 个 Pass，记下名字与占比。
5. **验证 fast_math**：以 `TILELANG_VERBOSE=1` 重跑，确认 nvcc 选项含 `--use_fast_math`（来源是 [tilelang/engine/lower.py:144-145](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L144-L145)）。

**验收标准**：能说清「我设的这三个配置键，分别被 4.5 节里哪一类读者（Python 谓词 / C++ GetConfig / 编译回调 / 仪器）消费的」。

> 若本地无 GPU：步骤 3 的 dump IR 可在 lower 阶段产出（仪器在 PassContext 层挂载），可尝试；步骤 4、5 需要 codegen/编译真正发生，**待本地验证**。

## 6. 本讲小结

- **Pass / PassContext / PassConfigKey** 是「变换 / 配置容器 / 键名表」的铁三角：PassConfigKey 给出键名，PassContext 装键值对，Pass 读键值对。
- tilelang 的 Pass **双面镜像**：算法在 C++（`src/transform`），Python 侧（`tilelang/transform/__init__.py`）只是转发 `_ffi_api`；后端 Pass 序列写死在各 `tilelang/<backend>/pipeline.py` 里，由 `PassPipeline` 注册表按 `target.kind.name` 查找。
- `PassConfigKey` 用 `(str, Enum)` 让枚举成员即字符串键，按 `tl.*` / `tirx.*` / `tir.*` 前缀分类，`tl.*` 是 tilelang 自有扩展面。
- `pass_configs` 经 `normalize_pass_configs` 归一化后成为 `PassContext.config`；`compile_flags` 不走独立通道，而是搭进 `tl.device_compile_flags` 这个键一并进入 PassContext。
- 配置的三类读者：Python 谓词（`pipeline_utils.py`）、C++ `GetConfig`（`src/config.h`）、编译回调（`tilelang_callback_cuda_compile`）。
- 可观测性也由配置驱动：`TL_ENABLE_DUMP_IR` 挂 `DumpIR` 仪器、`TL_PASS_PROFILE` 挂 `TileLangPassTimingInstrument` 计时仪器。

## 7. 下一步学习建议

- **下一讲 u6-l2（关键 lowering Pass 解读）**：本讲只讲了 Pass 系统的「骨架与配置」，下一讲将钻进 `LayoutInference`、`LowerTileOp`、`InjectSoftwarePipeline`、`LegalizeSafeMemoryAccess` 等具体 Pass 的算法。建议先在本讲综合实践里 dump 出这些 Pass 的 IR，带着 IR 去读下一讲。
- **u6-l3（设备代码生成与模板）**：本讲的 `tl.enable_fast_math` 在 `tilelang_callback_cuda_compile` 里变成 nvcc 选项；u6-l3 会完整讲 CUDA codegen 与 `tl_templates` 模板注入。
- **延伸阅读源码**：通读 [tilelang/transform/pass_config.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/pass_config.py) 的全部 docstring（最好的配置速查表），并对照 [tilelang/cuda/pipeline.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py) 看每个配置键具体在哪个 Pass 旁被读取。
