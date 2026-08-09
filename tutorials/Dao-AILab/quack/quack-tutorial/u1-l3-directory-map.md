# 目录结构与模块地图

## 1. 本讲目标

QuACK 的源码体量很大：`quack/` 顶层就有近 50 个 Python 文件，下面还挂着 13 个子包。如果一开始就扎进某个内核文件，很容易迷路。本讲的目标是先帮你建立一张「鸟瞰图」，让你在阅读任何具体代码之前，知道每个目录、每个文件大概负责什么。

学完本讲，你应当能够：

1. 说出 `quack` 顶层包分成哪三大「层」（公共 API 层、内核层、工具层），并解释每一层的职责。
2. 说出 `epilogue/`、`cache/`、`dsl/`、`spec/` 等 13 个子包各自负责什么，并能根据功能需求定位到对应目录。
3. 复述 `quack/__init__.py` 的导入链：它先导入什么、为什么导入它、最终向用户暴露了哪些名字。
4. 看懂一个「导入链」——例如 `softmax` 内核依赖哪些核心工具模块，并能自己用 `grep` 把这样的依赖链画出来。

## 2. 前置知识

在继续之前，请确认你已经理解了上一讲（u1-l1）引入的几个概念，本讲会直接使用它们：

- **CuTe-DSL**：用 Python 写、再被工具链编译成 GPU 机器码的方式，是 QuACK 所有内核的写作语言。
- **SM（流多处理器）编号**：H100 是 SM90、B200/B300 是 SM100、RTX 50 是 SM120。同一算子按 SM 编号分发不同实现。
- **包名与导入名**：分发（pip 安装）名是 `quack-kernels`，但在 Python 里 `import` 的名字是 `quack`。
- **内核（kernel）**：真正跑在 GPU 上的一段代码；本讲里你只需要知道 QuACK 的业务就是「写内核」。

另外两个 Python 概念本讲会反复用到，先在这里点一下：

- **`__init__.py`**：一个目录里有这个文件，Python 才把它当作「包（package）」。包的 `__init__.py` 在你第一次 `import` 该包时执行，常用来做初始化、副作用（side effect）和导出公开名字（`__all__`）。
- **导入的副作用**：有时候 `import` 一个模块不仅是为了拿到它的函数，还为了触发它对全局状态的修改（例如给别的类「打补丁」）。QuACK 里就有这样的用法，后面会专门讲。

## 3. 本讲源码地图

本讲涉及的关键文件很少，但它们是整张地图的「入口」：

| 文件 | 作用 |
| --- | --- |
| [`quack/__init__.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py) | 整个包的入口。定义版本号、执行副作用导入、向用户暴露公开 API。 |
| [`AGENTS.md`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/AGENTS.md) | 项目自带的「架构说明书」，第 49–77 行的 Architecture 一节直接给出了内核模式与核心工具的清单。 |
| [`quack/dsl/__init__.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/dsl/__init__.py) | DSL 集成子包入口，导出 `cute_op`，并在导入时给 CuTe 的张量类打补丁。 |
| [`quack/cache/__init__.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/__init__.py) | 缓存子包入口，导出 `jit_cache`、`CompilePending` 等，是「冷编译慢」问题的核心。 |

> 本讲是「地图课」，我们主要看的是入口文件和目录组织；具体内核、拷贝、布局的细节会在后续讲义展开。本讲引用的真实文件清单，均来自当前仓库 `git ls-files` 的实际结果。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：① 顶层包结构与三大分层；② 13 个子包的职责划分；③ 入口导出与导入链。

### 4.1 quack 顶层包结构与三大分层

#### 4.1.1 概念说明

打开一个大型项目，最怕「一锅粥」。QuACK 的做法是把代码按「离用户的距离」分成三层：

1. **公共 API 层**：用户直接调用的函数和模块。例如 `quack.rmsnorm(...)`、`quack.softmax(...)`，以及更上层的 `Linear`、`MLP`。这一层关心的是「接口长什么样、参数怎么校验、走哪条后端」。
2. **内核层**：真正用 CuTe-DSL 写的 GPU 内核，以及围绕内核的编译、调度、epilogue（尾融合）机制。这是 QuACK 的核心资产。
3. **工具层**：被一切内核复用的通用工具：拷贝、布局、dtype 映射、缓存、DSL 集成钩子等。这一层本身不实现某个具体算子，但没了它内核写不出来。

这样分层的好处是：当你想找「这个算子的接口」就往 API 层去；想找「这个算子怎么跑的」就往内核层去；想找「这段通用逻辑在哪」就往工具层去——三个方向不会混。

#### 4.1.2 核心流程

把 `quack/` 顶层的近 50 个 `.py` 文件按职责归类的流程是：

1. 列出所有顶层文件。
2. 根据文件名前缀和它 `import` 了什么，判断它属于哪一层。
3. 同一层内部再按「算子家族」分组（归约家族、GEMM 家族）。

下面这张表是按「算子家族 + 层」整理的顶层模块清单（仅列代表性文件，便于你建立印象）：

| 层 | 家族 / 职责 | 代表文件 |
| --- | --- | --- |
| 公共 API | 归约算子导出 | `rmsnorm.py`、`softmax.py`、`cross_entropy.py` |
| 公共 API | GEMM 入口与变体 | `gemm.py`、`gemm_interface.py`、`gemm_iface.py` |
| 公共 API | 高层融合神经网络算子 | `linear.py`、`mlp.py`、`linear_cross_entropy.py` |
| 内核 | 归约内核与基类 | `reduction_base.py`、`reduce.py`、`rms_final_reduce.py` |
| 内核 | GEMM 设备侧内核 | `gemm_base.py`、`gemm_sm90.py`、`gemm_sm100.py`、`gemm_sm120.py`、`gemm_sm80.py` |
| 内核 | GEMM 主机侧与配置 | `gemm_config.py`、`gemm_tvm_ffi_utils.py`、`split_k_reduce.py` |
| 内核 | 架构专用工具 | `sm90_utils.py`、`sm100_utils.py`、`sm80_utils.py` |
| 内核 | 流水线与调度 | `pipeline.py`、`tile_scheduler.py`、`varlen_utils.py` |
| 工具 | 拷贝与布局 | `copy_utils.py`、`layout_utils.py` |
| 工具 | DSL 与编译支持 | `cute_dsl_utils.py`、`compile_utils.py`、`utils.py` |
| 工具 | 数值与原语 | `fast_math.py`、`rounding.py`、`activation.py`、`rotary.py`、`complex.py` |
| 工具 | 其它 | `autotuner.py`、`tensormap_manager.py`、`trace.py`、`jax_utils.py` |

> 一个文件可能同时承担多个角色。比如 `rmsnorm.py` 既定义了公共 API 函数 `rmsnorm`，又包含内核类 `RMSNorm`，所以它横跨「公共 API 层」和「内核层」——这是 QuACK 里归约内核的典型写法。判断时以「这个文件被谁直接 `import`」为准。

#### 4.1.3 源码精读

最能体现「三层结构」的文字说明在项目的 AGENTS.md 里：

- [`AGENTS.md`:L51-L53](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/AGENTS.md#L51-L53) 说明归约内核（`rmsnorm.py`、`softmax.py`、`cross_entropy.py`）都继承 `reduction_base.py` 里的 `ReductionBase`，共享「配置 cluster、取 tiled copy、分配带 mbarrier 的归约缓冲、再启动 `@cute.kernel`」这套模式。
- [`AGENTS.md`:L55-L69](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/AGENTS.md#L55-L69) 说明 GEMM 的多层设计：`gemm.py` 是公共 API，`gemm_interface.py` 是跨 SM 的统一接口，`gemm_sm90.py`/`gemm_sm100.py` 是各架构实现，并指向 `epilogue/`、`gemm_runtime/`、`operand_transform/` 等子包。
- [`AGENTS.md`:L71-L77](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/AGENTS.md#L71-L77) 列出核心工具：`copy_utils.py`（拷贝）、`layout_utils.py`（布局代数）、`cute_dsl_utils.py`（dtype 映射与设备能力）、`tile_scheduler.py`（持久化调度）、`varlen_utils.py`（变长序列）。

这三段恰好对应「内核层」和「工具层」的代表文件。而「公共 API 层」的代表则是顶层 [`quack/__init__.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py) 的 `__all__`（见 4.3 节）。

> 注意：AGENTS.md 里把 SM 实现列举为 `gemm_sm90.py` / `gemm_sm100.py`，而实际仓库里还有 `gemm_sm120.py`（RTX 50）和 `gemm_sm80.py`（前代架构）。AGENTS.md 是一份「架构指引」而非「完整清单」，以实际文件为准。

#### 4.1.4 代码实践

**实践目标**：用只读 git 命令自己「量」出顶层包的规模，亲手把文件归到三层。

**操作步骤**：

1. 在仓库根目录列出顶层（非子包）Python 文件：

```bash
git ls-files 'quack/*.py' | grep -v '/.*' | sort
```

> 说明：`grep -v '/.*'` 过滤掉所有含 `/` 的路径（即子包里的文件），只留下 `quack/` 正下方的顶层模块。

2. 对每个文件名，按下表猜测它属于哪一层，然后写进你的笔记。

| 文件名 | 你猜的层 |
| --- | --- |
| `gemm.py` | ？ |
| `gemm_sm100.py` | ？ |
| `copy_utils.py` | ？ |
| `linear.py` | ？ |
| `cute_dsl_utils.py` | ？ |

**需要观察的现象**：顶层模块数量大致接近 50（本讲写作时为约 48 个），其中 GEMM 家族文件最多。

**预期结果**：`gemm.py`→公共 API；`gemm_sm100.py`→内核；`copy_utils.py`、`cute_dsl_utils.py`→工具；`linear.py`→公共 API（在 GEMM 之上）。

> 待本地验证：若仓库在你看时有新增/删除文件，数量会略有出入，以 `git ls-files` 的实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：`gemm.py`、`gemm_interface.py`、`gemm_sm100.py` 都和 GEMM 有关，它们分别属于哪一层？为什么？

> **答案**：`gemm.py` 是公共 API（校验输入、选 SM、缓存计划）；`gemm_interface.py` 是跨 SM 的统一接口层（在 API 与设备实现之间）；`gemm_sm100.py` 是设备侧内核层（SM100 的具体实现）。三者是「自上而下」的调用关系。

**练习 2**：`copy_utils.py` 属于哪一层？为什么它不在任何一个算子文件里？

> **答案**：工具层。因为「拷贝（gmem↔smem↔register、异步 cp.async、TMA）」是被几乎所有内核复用的能力，单独成文件、避免重复实现，也方便单独维护。

---

### 4.2 子包职责划分

#### 4.2.1 概念说明

除了顶层 `.py` 文件，`quack/` 下面还有 13 个子包（每个子包是一个含 `__init__.py` 的目录）。子包的用途是把「围绕同一个主题的一组文件」收拢在一起，避免顶层过于拥挤。

判断「一个功能该是顶层文件还是子包」的经验法则是：**只涉及一两个文件、职责单一的，放顶层；涉及多个文件、自成体系的，开子包。** 例如 epilogue（尾融合）系统有 `ops.py`/`mixin.py`/`frontend.py`/`visit.py`/`library.py` 加上多个领域模块，体量大且自成体系，所以单独开 `epilogue/` 子包。

#### 4.2.2 核心流程

下面这张表把 13 个子包按「主题」分组列出，给出各自的职责和代表文件（代表文件均来自实际仓库）：

| 主题 | 子包 | 职责 | 代表文件 |
| --- | --- | --- | --- |
| 内核机制 | `epilogue/` | 可组合的 GEMM 尾融合系统 | `ops.py`、`mixin.py`、`frontend.py`、`library.py`、`rotary.py` |
| 内核机制 | `operand_transform/` | A 操作数变换（反量化 / dropout / 值函数） | `transform.py`、`kinds.py`、`frontend.py`、`host.py`、`formats/qtip.py` |
| 内核机制 | `gemm_runtime/` | 所有 epilogue/transform 共享的通用主机管线 | `host.py`、`identity.py`、`torch_op.py`、`autotune.py` |
| 内核机制 | `spec/` | TMA 描述符 / MMA 指令 / TMEM 布局 / TensorSpec | `tma.py`、`mma.py`、`tmem.py`、`tensor_spec.py`、`smem.py` |
| 内核机制 | `sync/` | 同步原语（barrier / mbarrier / Semaphore） | `barrier.py` |
| 量化 | `blockscaled/` | 块缩放量化（MXFP8/NVFP4/MXFP6）的操作数容器与量化工具 | `operand.py`、`quantize.py`、`utils.py`、`nvfp4_utils.py` |
| 分布式 | `distributed/` | AllGather + GEMM 融合 | `all_gather_gemm.py` |
| 算法 | `sort/` | 双调排序网络 | `bitonic_sort.py`、`sorting_networks.py` |
| 算法 | `transform/` | Hadamard 变换等数学变换 | `hadamard.py` |
| 工具/集成 | `dsl/` | CuTe-DSL 集成钩子（`cute_op` 注册、张量索引补丁、ptxas 补丁） | `torch_library_op.py`、`cute_tensor_indexing.py`、`cute_dsl_ptxas.py` |
| 工具/集成 | `cache/` | `.o` JIT 缓存 + 异步编译池 | `jit.py`、`async_compile.py`、`_pool_preload.py` |
| 工具/集成 | `bench/` | 基准测量协议 | `bench_utils.py`、`cublaslt_quant_out.py` |
| 工具/集成 | `testing/` | pytest 插件与 trace 工具 | `pytest_plugin.py`、`trace.py` |

> 你会发现「内核机制」这一组子包（epilogue / operand_transform / gemm_runtime / spec / sync）几乎全部服务于 GEMM——这印证了 u1-l1 里提到的事实：GEMM 体系是 QuACK 里最庞大的部分，需要多个子包来组织。

#### 4.2.3 源码精读

每个子包的 `__init__.py` 顶部通常有一句 docstring 点明职责，是确认子包用途最可靠的来源：

- [`quack/spec/__init__.py`:L1-L3](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/__init__.py#L1-L3) 明确写道：「Spec-layer helpers for TensorSpec, TMA descriptors, and TMEM layouts.」（为 TensorSpec、TMA 描述符与 TMEM 布局提供 spec 层辅助）。
- [`quack/cache/__init__.py`:L1-L30](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/__init__.py#L1-L30) 用一段较长的 docstring 说明了两件事：一是持久化 `.o` 缓存（`jit_cache`），二是异步编译（`CompilePending`、`pool_scope`）；并且特别强调了一段「关键顺序（CRITICAL ORDERING）」——静态配置标志必须在导入 `quack.cache.jit` 之前定义，否则第一次编译内核会因 `AttributeError` 而失败。这说明 `cache/` 不只是「存文件」，它还深度参与编译生命周期的初始化顺序。

#### 4.2.4 代码实践

**实践目标**：用「看 docstring」的方式，亲手确认 5 个子包的职责，而不是死记表格。

**操作步骤**：

1. 列出所有子包入口：

```bash
git ls-files 'quack/*/__init__.py'
```

2. 对 `cache/`、`spec/`、`dsl/` 三个子包，分别打开它们的 `__init__.py`，读第一段 docstring 或开头的注释。
3. 在笔记里用自己的话写一句话总结每个子包。

**需要观察的现象**：

- `quack/cache/__init__.py` 顶部有一段多行 docstring，强调「persistent `.o` cache」和「async compilation」。
- `quack/spec/__init__.py` 只有一行 docstring，点出 TensorSpec / TMA / TMEM。
- `quack/dsl/__init__.py` 的 docstring 是「CuTe DSL helpers and integration hooks.」，并且你能看到它 `import` 了 `cute_tensor_indexing` 等模块（这是「副作用导入」，见 4.3 节）。

**预期结果**：你能不看本讲的表格，仅凭 `__init__.py` 的 docstring 复述这三个子包的职责。

#### 4.2.5 小练习与答案

**练习 1**：`epilogue/` 和 `operand_transform/` 都做「变换」，它们变换的对象有什么不同？

> **答案**：`epilogue/` 变换的是 GEMM 的**输出**（在累加器写出成 D 的过程中融合 bias、激活、rotary、量化等）；`operand_transform/` 变换的是 GEMM 的 **A 输入**（在进入 MMA 之前做反量化、dropout、值函数等）。一前一后，一个改输出，一个改输入。

**练习 2**：为什么 `cache/` 被归到「工具/集成」而不是「内核机制」？

> **答案**：`cache/` 不实现任何具体算子的计算逻辑，它服务于「所有内核共有的编译流程」——把编译产物 `.o` 缓存起来、冷编译时用 worker 池并行。它是横切所有内核的基础设施，所以归工具层。

**练习 3**：你想找一个「给所有线程做同步屏障」的原语，应该去哪个子包？

> **答案**：`sync/`，具体是 `quack/sync/barrier.py`（mbarrier / Semaphore）。AGENTS.md 的 GEMM 多层设计里提到的 `mbarrier` 协作就来自这类同步原语。

---

### 4.3 入口导出与导入链

#### 4.3.1 概念说明

当一个用户写下 `import quack` 时，到底发生了什么？理解这一点，是理解整个包「从哪里开始」的关键。

QuACK 的入口 [`quack/__init__.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py) 做了三类事：

1. **定义版本号**：`__version__`。
2. **执行副作用导入**：导入某些模块不是为了拿函数，而是为了触发它们对全局状态的修改（打补丁）。
3. **导出公开 API**：用 `__all__` 声明「用户可见的名字」。

其中「副作用导入」是最容易让人困惑、也最值得讲清楚的点。

#### 4.3.2 核心流程

`import quack` 时的执行顺序可以用下面这段伪代码描述：

```
1. __version__ = "0.6.4"
2. import quack.dsl        # ← 副作用：给 CuTe 张量类打「切片语法糖」补丁
3. if 设了环境变量 CUTE_DSL_PTXAS_PATH:
       导入 quack.dsl.cute_dsl_ptxas 并调用 .patch()   # ← 副作用：替换 ptxas cubin
4. from quack.rmsnorm import rmsnorm
   from quack.softmax import softmax
   from quack.cross_entropy import cross_entropy
   from quack.rounding import RoundingMode
5. __all__ = ["rmsnorm", "softmax", "cross_entropy", "RoundingMode"]
```

要点有两个：

- **顺序很重要**：第 2 步的 `import quack.dsl` 必须在第 4 步导入各内核模块**之前**完成，因为内核模块依赖那些已经被打好补丁的张量类与 `cute_op`。
- **`__all__` 决定可见性**：只有列在 `__all__` 里的名字是「官方公开 API」。`quack.gemm`、`quack.linear` 等虽然也能 `import`，但不在 `__all__` 里，属于「可以用但不是最顶层推荐入口」的二级 API。

#### 4.3.3 源码精读

逐段看入口文件：

- [`quack/__init__.py`:L1](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py#L1) 定义 `__version__ = "0.6.4"`，这与 u1-l1 讲到的当前版本一致。
- [`quack/__init__.py`:L5](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py#L5) `import quack.dsl as _quack_dsl` 是一次**副作用导入**。
- [`quack/__init__.py`:L15-L17](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py#L15-L17) 的注释直接解释了这个副作用：导入 `quack.dsl` 会顺带导入 `quack.dsl.cute_tensor_indexing`，它会 monkey-patch（给 CuTe 的张量类打补丁）整个进程，从而让你能用 Python 风格的切片语法（`:` / `...`）来索引 CuTe 张量。
- [`quack/__init__.py`:L18-L21](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py#L18-L21) 是真正的公开 API 导入：`rmsnorm`、`softmax`、`cross_entropy` 三个归约算子，外加一个枚举 `RoundingMode`（舍入模式）。
- [`quack/__init__.py`:L24-L29](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py#L24-L29) 用 `__all__` 把这四个名字声明为包的公开接口。

而副作用导入的目标 [`quack/dsl/__init__.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/dsl/__init__.py) 自己也是个「会触发副作用」的入口：

- [`quack/dsl/__init__.py`:L5-L7](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/dsl/__init__.py#L5-L7) 在导入时就 `import` 了 `cute_tensor_indexing`、`cute_tensor`、`mixed_constexpr_if`——这些 import 的副作用（打补丁）正是被上层依赖的东西。
- [`quack/dsl/__init__.py`:L8](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/dsl/__init__.py#L8) 同时 `from quack.dsl.torch_library_op import cute_op`，把 `cute_op` 暴露出来。`cute_op` 是把内核注册成 `torch.library` 自定义算子的装饰器，是后续讲义（u2-l6）的重点。

#### 4.3.4 代码实践

**实践目标**：跟踪一条真实内核的导入链，亲眼看到「内核依赖哪些核心工具模块」。

**操作步骤**：

1. 用 `grep` 提取 `softmax.py` 开头对 `quack` 自身模块的导入：

```bash
grep -nE '^import quack\.|^from quack\.' quack/softmax.py
```

2. 你会看到类似下面这样的结果（以实际仓库为准）：

```
15:import quack.utils as utils
16:import quack.copy_utils as copy_utils
17:from quack.compile_utils import make_fake_tensor as fake_tensor
18:from quack.dsl import cute_op
19:from quack.reduce import row_reduce, online_softmax_reduce
20:from quack.reduction_base import ReductionBase
21:from quack.cache import jit_cache
22:from quack.cute_dsl_utils import torch2cute_dtype_map
```

3. 把这些被依赖的模块按 4.1 的三层归类。

**需要观察的现象**：一个归约内核（softmax）会同时依赖工具层（`copy_utils`、`compile_utils`、`cute_dsl_utils`、`utils`、`cache`、`dsl`）和内核层的共享件（`reduce`、`reduction_base`）。

**预期结果**：你得到一张「`softmax` → {copy_utils, reduce, reduction_base, compile_utils, cache, cute_dsl_utils, dsl, utils}」的依赖清单，并能指出其中哪些是工具层、哪些是内核层。

> 待本地验证：行号可能随版本微调，以你本机 `grep` 的实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `quack/__init__.py` 要在导入 `rmsnorm`/`softmax` 之前先 `import quack.dsl`？顺序反了会怎样？

> **答案**：因为 `import quack.dsl` 会顺带执行 `cute_tensor_indexing` 的 monkey-patch，给 CuTe 张量类装上切片语法糖；而 `rmsnorm.py` 等内核模块内部会用到这些张量类。如果反过来先导入内核模块，内核可能在补丁打好之前就引用了未打补丁的张量类，导致行为不一致甚至报错。

**练习 2**：`__all__` 里没有 `gemm`，是不是说明 QuACK 不能做矩阵乘？

> **答案**：不能。`__all__` 只决定「顶层推荐入口」，`quack.gemm` 仍然可以 `from quack import gemm` 或 `import quack.gemm` 使用。GEMM 不在 `__all__` 里，更多是因为它的入口是 `quack.gemm.gemm(...)` 这类二级 API，且使用模式（配置、autotune）比归约算子复杂。

**练习 3**：`CUTE_DSL_PTXAS_PATH` 环境变量设置了之后，`__init__.py` 会多做什么事？

> **答案**：见 [`quack/__init__.py`:L7-L13](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py#L7-L13)：会导入 `quack.dsl.cute_dsl_ptxas` 并调用 `.patch()`，目的是在导入任何实例化 CuTeDSL 的模块**之前**，强制 dump PTX，让 CUDA library loader 能用系统 ptxas 组装的 cubin 替换 CUTLASS DSL 内嵌的 ptxas-library cubin。这是一次有严格时序要求的副作用操作。

---

## 5. 综合实践

把本讲三个模块串起来，画一张「模块依赖草图」。这张图是后续阅读任何内核时的导航图。

**任务**：为 `rmsnorm`、`softmax`、`gemm` 三个算子，分别画出它们「依赖哪些核心工具/共享模块」。

**操作步骤**：

1. 对三个文件分别跑 4.3.4 里那条 `grep` 命令，提取各自 `import quack.*` 的清单：

```bash
grep -nE '^import quack\.|^from quack\.' quack/rmsnorm.py
grep -nE '^import quack\.|^from quack\.' quack/softmax.py
grep -nE '^import quack\.|^from quack\.' quack/gemm.py
```

2. 把每个算子依赖的模块去重，整理成一张表。本讲写作时的实际结果（以你本机为准）大致是：

| 算子 | 依赖的核心工具 / 共享模块 |
| --- | --- |
| `rmsnorm` | `utils`、`copy_utils`、`layout_utils`、`compile_utils`、`dsl`(`cute_op`)、`pipeline`、`reduce`、`reduction_base`、`cache`(`jit_cache`)、`cute_dsl_utils`、`autotuner`、`rmsnorm_config` |
| `softmax` | `utils`、`copy_utils`、`compile_utils`、`dsl`(`cute_op`)、`reduce`、`reduction_base`、`cache`(`jit_cache`)、`cute_dsl_utils` |
| `gemm` | `cache`(`jit_cache`)、`split_k_reduce`、`gemm_config`、`compile_utils`、`cute_dsl_utils`、`gemm_default_epi`、`rounding`、`gemm_tvm_ffi_utils` |

3. 在草图中圈出「被三者共同依赖」的模块，这些就是 QuACK 最核心的基础设施。

**需要观察的现象**：

- `compile_utils`、`cache`(`jit_cache`)、`cute_dsl_utils`、`dsl`(`cute_op`) 几乎被所有算子依赖——它们是「编译 + 注册 + 缓存」三件套，是整个项目的地基。
- 归约家族（`rmsnorm`、`softmax`）都依赖 `reduce`、`reduction_base`、`copy_utils`；GEMM 则依赖完全不同的一套（`gemm_config`、`gemm_default_epi`、`split_k_reduce`、`gemm_tvm_ffi_utils`）。这印证了 u1-l1 里「归约内核相对独立、自包含」的说法。
- `gemm` 不直接 `import reduce`/`reduction_base`，因为它的归约走的是另一套（Split-K、epilogue 内的行列归约）。

**预期结果**：你得到一张可重复生成的依赖草图，并能指出「公共地基」「归约专用件」「GEMM 专用件」三组模块。下次读任何内核，先跑一次这条 `grep`，就能立刻定位它用了哪些积木。

> 提示：若想看得更深，可以对子包也做同样的事，例如 `grep -rnE '^import quack\.epilogue|^from quack\.epilogue' quack/gemm*.py`，看看哪些 GEMM 文件用到了 epilogue 系统。

---

## 6. 本讲小结

- `quack/` 顶层的近 50 个 `.py` 文件可以按「离用户的距离」分成三层：**公共 API 层**（`rmsnorm`/`softmax`/`gemm`/`linear` 等入口）、**内核层**（设备侧内核 + 主机侧编译调度）、**工具层**（`copy_utils`/`layout_utils`/`cute_dsl_utils` 等被一切复用的积木）。
- 顶层下面有 13 个子包，按主题可分为：内核机制（`epilogue`/`operand_transform`/`gemm_runtime`/`spec`/`sync`）、量化（`blockscaled`）、分布式（`distributed`）、算法（`sort`/`transform`）、工具与集成（`dsl`/`cache`/`bench`/`testing`）。其中大部分服务于 GEMM。
- 入口 [`quack/__init__.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py) 的执行顺序是：定义版本 → 副作用导入 `quack.dsl`（给 CuTe 张量类打补丁）→ 条件性 ptxas 补丁 → 导入四个公开 API → 用 `__all__` 声明可见名字。
- 「副作用导入」是 QuACK 入口的关键设计：`import quack.dsl` 会在导入时执行 monkey-patch，必须早于内核模块导入。
- 看任何一个子包的 `__init__.py` 顶部 docstring，是确认其职责最可靠的方法（如 `cache/`、`spec/`）。
- 用 `grep -nE '^import quack\.|^from quack\.' <file>` 可以一键画出任意模块的依赖链——这是本讲留给你的通用导航技巧。

## 7. 下一步学习建议

本讲建立的是「地图」，接下来可以沿两条路线深入：

1. **想理解「内核怎么写」**：进入第 2 单元，从 [`quack/reduction_base.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py) 的 `ReductionBase` 共享基类（u2-l1）和 [`quack/softmax.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py) 的前向内核（u2-l2）开始。建议先读 u1-l4（CuTe-DSL 编程模型），建立 `@cute.jit`/`@cute.kernel`/`const_expr` 的概念。
2. **想先把「工具层」吃透**：第 3 单元逐一讲解 [`copy_utils.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/copy_utils.py)、[`layout_utils.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/layout_utils.py)、[`tile_scheduler.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py)、[`pipeline.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/pipeline.py)——这些是本讲「依赖草图」里反复出现的积木。

无论走哪条路线，记住本讲的通用技巧：**先 `grep` 出依赖链，再读代码**，你就不会在 50 个文件里迷路。
