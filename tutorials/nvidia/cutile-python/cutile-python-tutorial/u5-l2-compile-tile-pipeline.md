# 编译总流程：compile_tile 流水线

## 1. 本讲目标

上一讲（u5-l1）我们拆开了 `@ct.kernel` 的「装饰与对象构造」阶段，看到一个内核对象在装饰期就把 `AnnotatedFunction`（参数注解掩码）和 `CompilerOptions`（编译期 hint）固化了下来。但那时我们刻意停在了 `_compile` 方法的门口——它只是把这两份固化信息和运行期的 `KernelSignature`、`sm_arch` 凑齐，然后整个转发给了一个函数：`compile_tile`。

本讲要彻底走通 `compile_tile`。它是整个 Python 编译前端的**总入口**：吃进一个 `AnnotatedFunction` 加一组 `KernelSignature`，吐出一个 `CompilationResult`（其中可能包含 cubin、字节码、final IR 三者之一或全部）。cuTile 内核从「一段 Python 函数」变成「GPU 上可执行的 cubin」，全部秘密都在这一个函数及其调用的少数几个辅助函数里。

读完本讲你应该能够：

- 把 `compile_tile` 的函数体**逐行**标注成「前端 / IR 变换 / 后端 / 缓存」四个阶段，并说清每一步为什么在这个位置。
- 理解 `_IrKeeper` 如何**按 signature 懒生成（lazy memoization）** final IR，以及为什么这是合理的设计。
- 说出 `_transform_ir` 里十来个优化 pass 的**执行顺序与依赖原因**（例如「为什么循环不变量外提必须在 token 排序 pass 之后」）。
- 理解 `compile_cubin` 如何定位并调用后端编译器 `tileiras`、如何用磁盘缓存避免重复编译，以及 `return_cubin / return_bytecode / return_final_ir` 三个开关如何分别短路流水线。
- 用 `CUDA_TILE_DUMP_TILEIR` / `CUDA_TILE_DUMP_BYTECODE` / `CUDA_TILE_LOGS` 等环境变量在本地观察编译中间产物。

本讲只讲**编排逻辑（orchestration）**：谁先调用谁、在什么条件下提前返回。流水线里每个具体子模块（ast2hir、hir2ir、IR 核心数据结构、各个优化 pass、字节码编码、tileiras 调用）都只点到为止，它们的深入拆解分别属于 u5-l3 ~ u5-l7、U6、U7 的内容。

## 2. 前置知识

本讲假设你已经掌握以下概念（它们在前置讲义中已建立，这里只做最简提示）：

- **内核对象的 `_compile` 入口**（u5-l1）：`kernel._compile(signature, context)` 把装饰期固化的 `AnnotatedFunction` + `CompilerOptions` 与运行期的 `KernelSignature` 凑齐，调用 `compile_tile`，再从结果里取出 `cubin` 和 `symbol`。本讲正是从这个调用点往下走。
- **Tile IR 与编译全链路直觉**（u1-l1）：`AST → HIR → Tile IR →（优化 pass）→ 字节码 →（tileiras）cubin →（缓存）→ cuLaunchKernel`。本讲是把这条链路在源码里「落实」。
- **Constant 常量嵌入**（u3-l5）：用 `ct.Constant[T]` 标注的参数在编译期被烘焙进 IR，从运行时签名中消失。这解释了为什么同一个内核函数会对应**多个** `KernelSignature`（每个 Constant 取值组合一份）。
- **`KernelSignature` 与 `ParameterConstraint`**：`KernelSignature` 描述「这次编译针对的具体参数假设」（每个参数是 `ScalarConstraint` / `ArrayConstraint` / `ListConstraint` / `ConstantConstraint` 之一），它是编译流水线贯穿始终的「配置钥匙」。

补充两个 Python / 工程层面的前置点：

- **`@functools.cache`**：把函数的返回值按参数 memoize（缓存）。`_find_compiler_bin`、`_get_max_supported_bytecode_version` 等都用了它，意思是「整个进程里只查找一次」。
- **`threading.RLock`（可重入锁）**：同一个线程可以多次获取而不会死锁的锁。`compile_tile` 整个被一把全局编译锁包住，保证多线程并发 `ct.launch` 时编译过程串行、不会踩坏磁盘缓存。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件，外加几个它调用的「邻居」：

| 文件 | 职责 |
| --- | --- |
| `src/cuda/tile/_compile.py` | **本讲核心**。定义 `compile_tile`、`_IrKeeper`、`_transform_ir`、`compile_cubin`、`_find_compiler_bin`、`_get_max_supported_bytecode_version` 等。 |
| `src/cuda/tile/_execution.py` | `kernel._compile` 在此调用 `compile_tile`，是本讲的「上游入口」。 |
| `src/cuda/tile/_passes/ast2hir.py` | `get_function_hir`：把 Python 函数解析成 HIR（前端第一步）。 |
| `src/cuda/tile/_passes/hir2ir.py` | `hir2ir`：把 HIR 降级成具体 Tile IR 操作（IR 变换第一步）。 |
| `src/cuda/tile/_ir2bytecode.py` | `generate_bytecode_for_kernel`：把 final IR 编码成字节码（后端第一步）。 |
| `src/cuda/tile/_cache.py` | `cache_key` / `cache_lookup` / `cache_store` / `evict_lru`：基于 SQLite 的 cubin 磁盘缓存。 |
| `src/cuda/tile/_context.py` | `TileContextConfig`：承载 `cache_dir` / `temp_dir` / `compiler_timeout_sec` / `log_cutile_ir` 等运行期配置（来自环境变量）。 |
| `src/cuda/tile/_debug.py` | 读取 `CUDA_TILE_DUMP_TILEIR` / `CUDA_TILE_DUMP_BYTECODE` 等调试环境变量。 |

> 阅读建议：本讲的源码引用高度集中在 `_compile.py`，建议你打开该文件对照阅读。下面凡是用 `[文件:行号]` 形式给出的链接都可点击直达 GitHub 上对应 HEAD 的源码行。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应 `compile_tile` 流水线的四段：

1. **4.1 `compile_tile`**：总入口与「前端准备 → IR 生成 → 后端 → 缓存」的阶段编排。
2. **4.2 `_IrKeeper`**：按 signature 懒生成 final IR 的「记忆体」。
3. **4.3 `_transform_ir`**：优化 pass 流水线的执行顺序与依赖。
4. **4.4 `compile_cubin` 与磁盘缓存**：字节码 → cubin 的后端编译与缓存。

### 4.1 compile_tile：编译流水线的总入口

#### 4.1.1 概念说明

`compile_tile` 是 cuTile 编译前端的**总调度函数（orchestrator）**。它本身不做任何 AST 解析或代码生成，它的职责是**按正确顺序调用正确的子函数，并在合适的时机短路返回**。

它的输入输出契约很清晰：

- **输入**：`ann_func`（`AnnotatedFunction` 或裸 Python 函数）、`signatures`（一组 `KernelSignature`）、以及若干可选的编译配置（`sm_arch`、`compiler_options`、`context`、`bytecode_version`）和三个返回值开关。
- **输出**：一个 `CompilationResult`，含 `kernel_signatures`、`cubin`、`bytecode`、`final_ir` 四个字段，后三者按开关选择性填充。

三个返回值开关 `return_cubin` / `return_bytecode` / `return_final_ir` 是理解整条流水线「在哪一步可以提前停下」的关键——它们让同一个函数既能服务 JIT 启动（只要 cubin），也能服务 AOT 导出（可能只要字节码），还能服务调试与测试（只要 final IR）。

为什么 `signatures` 是一个**序列**而不是单个？因为同一个内核函数在运行期可能针对不同的 Constant 取值组合编译出多份 cubin，每份对应一个 signature。`compile_tile` 一次性把一个函数的多个 signature 全编译进同一个字节码模块（一个字节码文件可以含多个 function）。

为什么整个函数被一把全局编译锁包住？因为编译会写临时文件、写磁盘缓存，并发编译可能互相踩踏。`@global_compiler_lock` 用 `RLock`（可重入锁）保证同一时刻只有一个线程在编译，而 RLock 的可重入性允许锁内的代码再次进入被同一把锁保护的函数（比如崩溃 dump 时重新调用 `_get_bytecode`）。

#### 4.1.2 核心流程

`compile_tile` 的执行顺序可以概括为下面这张「阶段表」（行号对应 `_compile.py`）：

| 阶段 | 代码行 | 做的事 |
| --- | --- | --- |
| **前端准备** | 335–351 | 规范化 `ann_func`；为缺 `symbol` 的 signature 补上 mangled 名；解析 `sm_arch`、`bytecode_version` |
| **前端：AST→HIR** | 353–354 | `get_function_hir` 把 Python 函数解析成 HIR |
| **构造 IR Keeper** | 355–361 | 构造 `_IrKeeper`，**此时还不生成 IR**，只持有 HIR 和 signatures |
| **IR 生成分支** | 363–367 | 若不需要字节码（只要 final IR），逐个 signature 调 `get_final_ir` 后直接返回 |
| **后端：IR→字节码** | 369 | `_get_bytecode` 触发每个 signature 的 final IR 生成 + `_transform_ir` + 字节码编码 |
| **调试 dump** | 371–395 | 按配置打印 / 落盘 MLIR、字节码 |
| **后端短路** | 397–401 | 若不要 cubin，带着字节码直接返回 |
| **缓存查询** | 403–420 | 算 `cache_key`，`cache_lookup` 命中则直接返回缓存的 cubin |
| **后端：字节码→cubin** | 422–441 | 调 `compile_cubin` 让 `tileiras` 把字节码编成 cubin（失败可落 crash dump） |
| **缓存写入** | 443–445 | `cache_store` 存 cubin，`evict_lru` 按 LRU 淘汰 |
| **返回** | 447 | 返回填好 cubin 的 `CompilationResult` |

把这张表抽象成一句话：**「前端把 Python 变成 HIR；IR Keeper 懒生成并优化成 final IR；后端把 final IR 编成字节码、再让 tileiras 变成 cubin；磁盘缓存兜住 cubin 避免重复编译。」**

三个返回值开关如何决定在哪一步短路：

- `return_final_ir=True` 且 `return_bytecode=False` 且 `return_cubin=False`：在第 363–367 行就返回，**根本不生成字节码、不调 tileiras**。适合调试 / 单测 IR。
- `return_bytecode=True` 但 `return_cubin=False`：在第 400–401 行返回，**生成字节码但不调 tileiras**。适合 AOT 导出 `.tileirbc`。
- `return_cubin=True`（默认）：走完整流水线到第 447 行。适合 JIT 启动。

#### 4.1.3 源码精读

先看函数签名与锁装饰：[src/cuda/tile/_compile.py:325-334](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L325-L334)。注意 `@global_compiler_lock` 把整个函数体包进全局编译锁；三个返回值开关默认值是 `return_cubin=True`、其余为 `False`，说明「默认行为是编出 cubin」。

前端准备阶段——规范化输入与补 symbol：[src/cuda/tile/_compile.py:335-351](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L335-L351)。三件事：把裸 Python 函数包成 `AnnotatedFunction`；为没有 `symbol` 的 signature 调 `with_mangled_symbol` 自动生成名称修饰后的符号名（详见 u8-l2）；在 `sm_arch` / `bytecode_version` 未指定时分别用 `get_sm_arch()` 和 `_get_max_supported_bytecode_version(...)` 探测。

> 名称修饰（name mangling）：把「函数名 + signature」编码成一个唯一的 C 符号名，让不同 signature 的同一内核在 cubin 里互不冲突。`with_mangled_symbol` 内部调 `mangle_kernel_name`（u8-l2 详讲）。

前端第一步——AST 到 HIR：[src/cuda/tile/_compile.py:353-354](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L353-L354)。`get_function_hir(ann_func.pyfunc, entry_point=True)` 解析 Python 源码的 AST，产出一份与具体 signature 无关的 HIR（HIR 只依赖函数本身，不依赖运行期参数假设，所以这一步在 signature 循环之外、只做一次）。`entry_point=True` 表示这是内核入口而非内联的 tile 函数，会影响 early-return 的 IR 形态。

构造 IR Keeper（这一步只持有数据，不干活）：[src/cuda/tile/_compile.py:355-361](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L355-L361)。`keep_all=context.config.enable_crash_dump or return_final_ir` 这个参数很关键：只有当「需要崩溃 dump」或「调用方要 final_ir」时，`_IrKeeper` 才会在内部 memo 住每个 signature 的 final IR；否则用完即弃以省内存。我们会在 4.2 详讲。

「只要 final IR」的短路分支：[src/cuda/tile/_compile.py:363-367](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L363-L367)。`need_bytecode = return_bytecode or return_cubin`；若不需要字节码，就逐个 signature 触发 `get_final_ir(i)`，然后带着 `final_ir=ir_keeper.final_ir` 返回。注意这里**连字节码都不生成**，是最轻量的编译路径。

后端第一步——final IR 到字节码：[src/cuda/tile/_compile.py:369](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L369)。`_get_bytecode(...)` 内部会遍历每个 signature，触发其 `get_final_ir`（含 `_transform_ir`），再用 `generate_bytecode_for_kernel` 编码成字节码（4.4 节展开）。

调试 dump 三连——日志 / 字节码 / MLIR：[src/cuda/tile/_compile.py:371-395](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L371-L395)。三段分别对应：`log_tileir`（打印 MLIR 到 stderr）、`CUDA_TILE_DUMP_BYTECODE`（把原始字节码写进 `.tileirbc` 文件）、`CUDA_TILE_DUMP_TILEIR`（把 MLIR 文本写进 `.tileir` 文件，依赖内部扩展）。本讲的代码实践就靠这几个开关。

「只要字节码」的短路分支：[src/cuda/tile/_compile.py:397-401](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L397-L401)。`return_cubin=False` 时在这里返回，`cubin` 字段保持 `None`。

磁盘缓存查询：[src/cuda/tile/_compile.py:403-420](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L403-L420)。先用 `_get_compiler_version_string()` 拿到 tileiras 版本；`cache_dir` 为空或版本未知时禁用缓存；否则算 `cache_key`、`cache_lookup`，命中就填上 cubin 提前返回——**完全跳过 tileiras 调用**，这是第二次启动同一内核极快的根本原因。

后端第二步——字节码到 cubin：[src/cuda/tile/_compile.py:422-441](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L422-L441)。把字节码写进临时文件，调 `compile_cubin`（4.4 节展开）。若抛 `TileCompilerError` 且开启了 crash dump，会重新生成一份**匿名化的**字节码（`anonymize_debug_info=True`，去掉位置信息保护隐私）连同 final IR 一起打包成 zip 落盘，再 `raise`。

缓存写入与 LRU 淘汰：[src/cuda/tile/_compile.py:443-445](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L443-L445)。编译成功后 `cache_store` 存 cubin，再 `evict_lru` 按累计大小 + 访问时间淘汰，使缓存目录不超过 `cache_size_limit`（默认 2GB）。

#### 4.1.4 代码实践

**实践目标**：把 `compile_tile` 的函数体逐行标注成四个阶段，体会「流水线」一词的字面含义。

**操作步骤**：

1. 打开 `src/cuda/tile/_compile.py`，定位到 `compile_tile`（第 325 行起）。
2. 对照 4.1.2 的阶段表，在源码旁用注释标出每一段属于「前端准备 / 前端 AST→HIR / IR 生成 / 后端 IR→字节码 / 后端 字节码→cubin / 缓存」中的哪一类。例如第 353 行 `get_function_hir` 旁边写「前端：AST→HIR」，第 369 行 `_get_bytecode` 旁边写「后端：IR→字节码（内部触发 IR 生成）」。
3. 特别标注三处短路点：第 367 行（只要 final IR）、第 401 行（只要字节码）、第 420 行（缓存命中）。

**需要观察的现象**：你会清楚看到，`compile_tile` 的主体是「线性向下 + 三处 if 提前 return」的结构，没有任何循环回退；流水线严格单向流动。

**预期结果**：标注完成后，你应该能一眼指出「`_get_bytecode` 调用既属于后端（产出字节码），又内部触发了 IR 生成（`get_final_ir` + `_transform_ir`）」——这是本讲最容易被忽略的一个事实：**IR 生成是被后端步骤「按需拉动」的，而不是在前面主动做完**。

> 说明：本实践是纯源码阅读，不涉及运行，因此结果可由阅读直接确定，无需「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果调用方设置 `return_final_ir=True, return_bytecode=False, return_cubin=False`，`compile_cubin` 会被调用吗？为什么？

> **答案**：不会。`need_bytecode = return_bytecode or return_cubin` 为 `False`，代码在第 363–367 行的分支里就 `return` 了，根本走不到第 422 行的 `compile_cubin`。这条路径甚至不生成字节码，是开销最小的编译方式，常用于 IR 测试。

**练习 2**：为什么 `get_function_hir` 在 signature 循环之外只调用一次，而 `get_final_ir` 要对每个 signature 各调用一次？

> **答案**：HIR 只依赖 Python 函数本身（语法结构），与运行期参数假设无关，所以做一次就够。而 final IR 依赖具体 signature（参数的 dtype、ndim、stride 假设、Constant 取值都不同），每个 signature 对应一份独立的 final IR，所以必须每 signature 一份——这正是 `_IrKeeper` 存在的理由。

### 4.2 _IrKeeper：按 signature 懒生成 final IR

#### 4.2.1 概念说明

`_IrKeeper` 是一个**记忆体（memoization holder）**。它的核心作用是：**按 signature 索引，懒生成并（可选地）缓存 final IR**。

「懒生成（lazy）」是关键词：构造 `_IrKeeper` 时**不**做任何 IR 生成，只是把 HIR、signatures、bytecode_version、sm_arch 这些「原料」存起来；只有当有人调 `get_final_ir(i)` 时，才真正为第 `i` 个 signature 生成 final IR，并把结果存进 `self.final_ir[i]` 供下次复用。

为什么需要「懒」+「记忆」？两个理由：

1. **按需省功**：4.1 提到，当只要 final IR 时，流水线会在生成完 IR 后短路、不生成字节码；当要字节码 / cubin 时，`_get_bytecode` 又会逐个调 `get_final_ir`。无论哪种路径，`get_final_ir` 都只在被需要时才跑——如果调用方根本不要某些 signature 的产物，就永远不付这份代价。
2. **崩溃复用**：当 `tileiras` 编译失败时，crash dump（4.1 的第 436 行）需要拿到 `ir_keeper.final_ir`。如果 IR 不是记忆住的，这里就得重新生成一遍——有了 memo，直接读缓存即可。

「`keep_all`」参数控制是否真正记忆：只有 `enable_crash_dump` 或 `return_final_ir` 为真时，`self.final_ir` 才是一个真正的列表（否则为 `None`，用完即弃）。这是一个内存与功能的权衡。

#### 4.2.2 核心流程

`get_final_ir(signature_index)` 的执行顺序：

1. **查 memo**：若 `self.final_ir[signature_index]` 已存在，直接返回（这是「记忆」生效的地方）。
2. **建 IRContext**：创建一个全新的 `ir.IRContext`，挂上日志开关、tileiras 版本、以及自定义的 `_TileTypingHooks`（决定如何由 dtype+shape 构造 `TileTy`）。
3. **进入 Builder + 注册表上下文**：`with ir.Builder(...) as ir_builder` + `with tile_impl_registry.as_current()`。Builder 是线程局部的 IR 构造器，`tile_impl_registry` 让 `@impl` 注册的 IR 实现（u5-l7）能被找到。
4. **创建参数 Var**：`_create_kernel_parameters(...)` 把当前 signature 的 `ParameterConstraint` 列表 + `AnnotatedFunction` 的常量掩码，翻译成一批 IR 参数变量。Constant 参数被烘焙成 `loosely_typed_const` 字面量；非 Constant 参数按 `Scalar/Array/List` 约束构造类型并 flatten。
5. **HIR → IR**：`hir2ir(...)` 把 HIR 降级成具体 IR 操作（u5-l4 详讲）。
6. **组装函数体 Block**：新建一个 `ir.Block`，把 flatten 后的非 Constant 参数作为 block 参数，把 builder 产出的 ops 作为 body。
7. **优化**：`_transform_ir(func_body, ...)` 跑优化 pass 流水线（4.3 节）。
8. **（可选）日志**：若 `log_cutile_ir`，打印 final IR 文本。
9. **dtype 校验**：`check_dtype_support` 检查 IR 里用到的 dtype 在目标 sm 架构 + bytecode 版本下是否受支持。
10. **记忆**：若 `self.final_ir is not None`，存进 `self.final_ir[signature_index]`，然后返回。

#### 4.2.3 源码精读

`_IrKeeper` 的构造与字段：[src/cuda/tile/_compile.py:246-265](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L246-L265)。注意第 261 行：`self.final_ir = [None] * len(signatures) if keep_all else None`——这就是「是否记忆」的开关。`_TileTypingHooks`（[第 241–243 行](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L241-L243)）把 `(dtype, shape)` 映射成 `TileTy(dtype, shape)`，是前端把「用户类型」接进 IR 类型系统的扩展点。

`get_final_ir` 的主体——memo + 懒生成：[src/cuda/tile/_compile.py:267-298](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L267-L298)。逐行对应 4.2.2 的十步。重点理解第 268 行的判断 `if self.final_ir is None or self.final_ir[signature_index] is None`：前者表示「根本不记忆」（keep_all=False），后者表示「还没生成过这一份」；两种情况都要现场生成，区别只在生成后是否回填 memo。

参数创建的核心 `_create_kernel_parameters`：[src/cuda/tile/_compile.py:126-159](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L126-L159)。对每个参数：若该位置在 `constant_parameter_mask` 里为真，要求 constraint 必须是 `ConstantConstraint`，把它烘焙成 `loosely_typed_const(constraint.value)`（从运行时签名消失，见 u3-l5）；否则按 `ScalarConstraint` / `ArrayConstraint` / `ListConstraint` 构造对应 IR 类型，`make_var` 建变量、`flatten_block_parameters` 把聚合类型（如 `List[Array]`）展平成扁平 Var 序列。返回的 `_KernelParameters` 同时保留了「聚合视图 `aggregate_vars`」（喂给 hir2ir）和「扁平视图 `nonconstant_flat_vars`」（作为 block 参数 + 喂给数据流分析）。

组装函数体 Block 的关键两行：[src/cuda/tile/_compile.py:283-285](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L283-L285)。`func_body.params = sum((vars for vars, _ in params.nonconstant_flat_vars), ())` 把所有非 Constant 参数的扁平 Var 拼成 block 的形参列表；`func_body.extend(ir_builder.ops)` 把 hir2ir 产出的 ops 填进 body。这两行把「参数」和「函数体」拼成一个完整的 IR 函数。

#### 4.2.4 代码实践

**实践目标**：验证「IR 生成是懒的、且按 signature 隔离」。

**操作步骤**：

1. 在 `get_final_ir` 第 269 行（`sig = self.signatures[signature_index]` 之后）和第 296 行（`return func_body` 之前）各加一行临时 print，例如 `print(f"[DEBUG] generating final IR for signature {signature_index}")` 与 `print(f"[DEBUG] done signature {signature_index}")`。
2. 写一个内核，它带一个 `ct.Constant[int]` 参数（这样会有多个 signature 的潜质），用两个不同的 Constant 值各 `ct.launch` 一次（参考 u3-l5 的实践）。
3. 观察打印出现的次数与顺序。

**需要观察的现象**：每个被实际编译的 signature 会触发一次「generating → done」配对；未被用到的 signature 不会触发。

**预期结果**：你会看到 IR 生成次数等于「实际被启动的 signature 数」，而非「kernel 对象上预设的 signature 数」，从而直观验证「懒生成」。**注意：此实践需修改源码做临时调试，验证后请务必还原，不要提交。具体打印次数取决于你启动的 signature 数，待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：`keep_all=False` 时，对同一个 signature 连续调两次 `get_final_ir(i)`，会生成几次 IR？

> **答案**：两次。`keep_all=False` 时 `self.final_ir is None`，第 268 行的判断恒为真，每次都现场生成、不回填。这通常不会发生（因为 `keep_all=False` 意味着调用方不要 final_ir，且不会触发 crash dump），但逻辑上确实如此。若 `keep_all=True`，则第二次直接命中 memo，只生成一次。

**练习 2**：为什么 `check_dtype_support(func_body, sm_arch, bytecode_version)` 放在 `_transform_ir` 之后、而不是之前？

> **答案**：因为优化 pass 可能改变 IR 里实际出现的 dtype 用法（例如某些 pass 可能引入或消除特定类型的操作）。校验应当在 IR「最终形态」上进行，才有意义；放在优化之后保证检查的是将要被编码进字节码、送进 tileiras 的真实 IR。

### 4.3 _transform_ir：优化 Pass 流水线

#### 4.3.1 概念说明

`_transform_ir` 是 cuTile 的**优化 pass 流水线**。它接收一个 `func_body`（IR Block），就地（in-place）对其跑一串 pass，产出「更优」的 final IR。本节只讲**编排**——哪个 pass 在哪一步、为什么是这个顺序；每个 pass 的内部原理属于 U6 的内容。

优化 pass 的设计有几个通则：

- **不动点 vs 单遍**：有些 pass（如数据流分析）需要迭代到不动点，有些（如 DCE）单遍即可。`_transform_ir` 里大多是一次性按固定顺序跑一遍，靠精心排序让每个 pass 在其前置条件满足时运行。
- **依赖共享**：`dataflow_analysis` 的结果会被 `add_divby_pass` 和 `token_order_pass` 共同消费，所以它只跑一次、结果存进 `dataflow_result` 传下去。
- **顺序敏感**：注释里明确写了「循环不变量外提必须在 token 排序 pass 之后，否则可能错误地把 load 外提到循环外」——这是顺序硬约束的典型例子。

`_transform_ir` 还接收两个额外参数：`bytecode_version`（某些 pass 的行为按版本分支，如 `unhoist_partition_views` 只在 `V_13_3` 之前跑）和 `param_constraints`（喂给数据流分析的参数假设）。

#### 4.3.2 核心流程

`_transform_ir` 的 pass 执行顺序（行号 → pass → 作用）：

| 行 | pass | 作用 |
| --- | --- | --- |
| 95 | `eliminate_assign_ops` | 消除赋值类 ops，把可变语义规整成 SSA |
| 96 | `dead_code_elimination_pass` | 死代码消除：剪除结果无人使用的 op |
| 97 | `dataflow_analysis` | 数据流分析：算别名集 + 整除性谓词，产出 `dataflow_result` |
| 99–100 | `add_divby_pass` | 用分析结果插入 `AssumeDivBy`，传递对齐信息（可用 `CUDA_TILE_TESTING_DISABLE_DIV` 关闭） |
| 102–103 | `token_order_pass` | 用分析结果为内存操作建立 token 依赖链，保证 GPU 内存模型正确性（可用 `CUDA_TILE_TESTING_DISABLE_TOKEN_ORDER` 关闭） |
| 105 | `rewrite_patterns` | 模式重写，如把 `mul+add` 融合成 FMA |
| 109 | `hoist_loop_invariants` | 循环不变量外提（**必须在 token_order 之后**，见注释） |
| 113–114 | `unhoist_partition_views` | 仅 `bytecode_version < V_13_3`：把被外提的 `MakePartitionView` 拷回消费者之前 |
| 116 | `split_loops` | 按可分裂条件拆分循环 |
| 117 | `dead_code_elimination_pass` | 再来一遍 DCE，清理前面 pass 产生的死代码 |

两个测试开关的存在说明这些 pass 对正确性并非都必需——`CUDA_TILE_TESTING_DISABLE_DIV` 和 `CUDA_TILE_TESTING_DISABLE_TOKEN_ORDER` 主要用于在单测里隔离 pass 的影响、定位 bug。

为什么 DCE 跑了两遍（第 96、117 行）？因为前面的 pass（外提、分裂、重写）会产生新的死代码，最后再扫一遍保证产物干净。

#### 4.3.3 源码精读

`_transform_ir` 全貌：[src/cuda/tile/_compile.py:92-117](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L92-L117)。注意函数签名接收 `func_body`、`bytecode_version`、`param_constraints`，全程就地修改 `func_body`，无返回值（transform in place）。

两条关键注释揭示了顺序硬约束。第一条（[第 107–108 行](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L107-L108)）：「Loop invariant code motion needs to run after the token order pass. Otherwise, it may incorrectly hoist load operations out of the loop.」——token 链先建好，外提才能正确判断「这个 load 移出循环后，它的内存序约束是否还能满足」。第二条（[第 111–112 行](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L111-L112)）：对 `V_13_3` 之前的字节码版本，`MakePartitionView` 必须内联在消费者之前，而外提可能把它挪到外层 block，所以要用 `unhoist_partition_views` 拷回。

两个测试开关：[第 99 行](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L99) 与 [第 102 行](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L102)。它们读自 `_debug.py`（见 [src/cuda/tile/_debug.py:12-15](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_debug.py#L12-L15)），默认关闭。

#### 4.3.4 代码实践

**实践目标**：用 `CUDA_TILE_LOGS=CUTILEIR` 观察 `_transform_ir` 前后 IR 的差异，感性认识「优化」发生了什么。

**操作步骤**：

1. 写一个最简内核，例如带循环累加的 vector_add 或一个含未使用中间结果的内核。
2. 设环境变量 `CUDA_TILE_LOGS=CUTILEIR` 后运行（该变量在 `_context.py` 的 `get_log_keys_from_env` 里被解析，置 `log_cutile_ir=True`，最终触发 `get_final_ir` 第 289–292 行打印 final IR）。
3. 再设 `CUDA_TILE_TESTING_DISABLE_TOKEN_ORDER=1` 重跑一次，对比两次打印的 IR 中 token 相关操作的差异。

**需要观察的现象**：stderr 会打印 `==== CuTile IR for <funcname> ====` 段落，展示经过全部优化 pass 之后的 final IR 文本；关闭 token_order 后，IR 里应缺少 token 链相关的 op。

**预期结果**：你能直观看到「优化后的 IR」长什么样，以及 token pass 对 IR 结构的影响。**具体 IR 文本内容依赖内核与架构，待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：如果把第 109 行的 `hoist_loop_invariants` 挪到第 103 行 `token_order_pass` 之前，会发生什么风险？

> **答案**：可能把某些 load 操作错误地外提到循环之外。因为此时 token 依赖链尚未建立，外提算法无法正确判断「移动这个 load 是否破坏内存序约束」。这正是源码注释强调的顺序硬约束。

**练习 2**：`unhoist_partition_views` 为什么用 `if bytecode_version < BytecodeVersion.V_13_3` 包起来？

> **答案**：因为只有 `V_13_3` 之前的字节码版本要求 `MakePartitionView` 必须内联在消费者之前；`V_13_3` 及之后放松了这个约束，外提不再引发问题，所以这步 unhoist 对新版本是多余的、跳过以省时间。这是一个「按字节码版本门控优化行为」的典型例子。

### 4.4 compile_cubin 与磁盘缓存：字节码到 cubin

#### 4.4.1 概念说明

到这一步，final IR 已经被 `_get_bytecode` 编码成了一段 `bytearray` 字节码。本模块讲流水线的最后两段：**字节码 → cubin**（后端编译），以及**磁盘缓存**（避免重复编译）。

后端编译由 `compile_cubin` 完成，它的本质是「调用一个外部可执行程序 `tileiras`」。`tileiras` 是 cuTile 的后端编译器（独立于这个 Python 包，u1-l1 已介绍），它读入 TileIR 字节码文件，输出 cubin（CUDA binary）文件。`compile_cubin` 负责组装命令行参数（目标架构 `--gpu-name`、优化等级 `-O`、调试信息 `--lineinfo` / `--device-debug`）、设超时、跑子进程、处理失败。

`_get_bytecode` 把 final IR 编码成字节码的过程也属于本模块的「前半段」：它遍历每个 signature，逐个 `get_final_ir`，再用 `generate_bytecode_for_kernel` 把 IR Block 编码进一个共享的 `BytecodeWriter`。

磁盘缓存（`_cache.py`）用 SQLite 存 cubin，key 由「编译器版本 + sm_arch + 优化等级 + 字节码内容 + 是否 device-debug」的 SHA-256 哈希构成。命中就跳过 `tileiras`，这是 JIT 第二次启动极快的根本原因。这部分会在 u7-l4 深讲，本节只点出它在 `compile_tile` 里的接入位置。

#### 4.4.2 核心流程

**字节码生成（`_get_bytecode`）**：

1. 建一个空 `bytearray` 作为缓冲区。
2. `with bc.write_bytecode(num_functions=..., buf=..., version=...) as writer` 打开一个字节码 writer（上下文管理器，退出时落 magic 头、section、各张表）。
3. 对每个 signature：`get_final_ir(i)` 拿 final IR，取 `signatures[i].symbol` 作为符号名，调 `generate_bytecode_for_kernel(...)` 把这个函数编码进 writer。
4. 返回填好的 `bytecode_buf`。

**cubin 编译（`compile_cubin`）**：

1. `_find_compiler_bin()` 定位 `tileiras` 可执行文件（带 `@cache`，进程内只查一次）。
2. cubin 输出路径 = 字节码路径换后缀为 `.cubin`。
3. `_tileiras_effective_opt_and_device_debug` 算出有效优化等级与是否开 device-debug（`EXPERIMENTAL_CUDA_TILE_DEBUG_BUILD` 时强制 `-O0` + `--device-debug`）。
4. 组装 flags：`--gpu-name <sm_arch>`、`-O<level>`、以及 `--device-debug` 或 `--lineinfo`（二选一）。
5. `binary.run(args, flags, timeout_sec)` 跑子进程；超时抛 `TileCompilerTimeoutError`，非零退出抛 `TileCompilerExecutionError`。
6. 返回 cubin 文件路径。

**`_find_compiler_bin` 的多级查找顺序**（带 `@cache`，只查一次）：

1. pip 包 `nvidia-cuda-tileiras`（连同 nvcc、nvvm，版本须主次一致）。
2. `PATH` 环境变量。
3. `CUDA_HOME` / `CUDA_PATH` 下的 `bin`。
4. 默认 CUDA Toolkit 安装路径兜底。
5. 都找不到 → 抛 `FileNotFoundError`，提示 `pip install cuda-tile[tileiras]` 或装系统 CTK 13.1+。

**字节码版本探测（`_get_max_supported_bytecode_version`）**：用空字节码（`num_functions=0`）逐个尝试高版本到低版本调用 tileiras，第一个能跑通的版本就是当前 tileiras 支持的最高字节码版本；全失败则回退 `V_13_1`。

#### 4.4.3 源码精读

`_get_bytecode`——多 signature 共享一个 writer：[src/cuda/tile/_compile.py:301-313](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L301-L313)。第 308 行的循环把每个 signature 的 final IR 编码进同一个 `writer`，最终产出一个含多函数的字节码模块。

`compile_cubin`——组装命令行并跑子进程：[src/cuda/tile/_compile.py:688-712](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L688-L712)。第 706–709 行是 `--device-debug` 与 `--lineinfo` 的二选一：正常构建只带 `--lineinfo`（保留行号供 profiler / nsys 用，不影响优化），调试构建才带 `--device-debug`（关优化、保留完整调试信息）。

`_CompilerBinary.run`——子进程调用与错误转换：[src/cuda/tile/_compile.py:480-505](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L480-L505)。注意它把 `LD_LIBRARY_PATH` / `PATH` 注入子进程环境，并在 `pass_cuda_home_var=False` 时主动抹掉 `CUDA_HOME` / `CUDA_PATH`（避免 pip 装的 tileiras 误用系统 CTK 的头文件）。`CalledProcessError` → `TileCompilerExecutionError`，`TimeoutExpired` → `TileCompilerTimeoutError`（提示「减小 tile 尺寸可能降低编译时间」）。

`_find_compiler_bin`——四级查找：[src/cuda/tile/_compile.py:556-591](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L556-L591)。pip 路径优先（[第 520–553 行](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L520-L553) 的 `_find_pip_tileiras` 会校验三个包版本主次一致，不一致就告警并回退）。

`_get_max_supported_bytecode_version`——空字节码探测：[src/cuda/tile/_compile.py:605-629](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L605-L629)。`reversed(...)` 从高版本往低试，第一个不抛 `TileCompilerError` 的就返回。

磁盘缓存的接入点（在 `compile_tile` 内）：[src/cuda/tile/_compile.py:414-420](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L414-L420)（`cache_key` + `cache_lookup` 命中即返回）与 [第 443–445 行](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L443-L445)（`cache_store` + `evict_lru`）。`cache_key` 的构成见 [src/cuda/tile/_cache.py:61-79](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_cache.py#L61-L79)：把编译器版本、sm_arch、`opt_level | (device_debug << 8)`、字节码内容一起喂进 SHA-256。注意 device_debug 被编进了 key 的高位字节（[第 76 行](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_cache.py#L76)），所以调试构建与正常构建的 cubin 不会在缓存里撞车。

#### 4.4.4 代码实践

**实践目标**：用 `CUDA_TILE_DUMP_BYTECODE` 抓到一份字节码文件，并验证磁盘缓存命中。

**操作步骤**：

1. 设 `CUDA_TILE_DUMP_BYTECODE=/tmp/cutile_bc` 后运行一个 vector_add 内核。
2. 观察 stderr 应出现 `Dumping TILEIR bytecode to file: ...`，并在 `/tmp/cutile_bc` 下看到一个 `.tileirbc` 文件。
3. 用 `xxd` 或十六进制查看器打开该 `.tileirbc`，对照 u7-l2 的字节码格式定位 magic 头（这一步把本讲与后端讲义串起来）。
4. 再验证缓存：连续 `ct.launch` 同一内核两次，第二次的启动应明显更快（命中 SQLite 缓存，跳过 tileiras）。可临时设 `CUDA_TILE_LOGS` 或在 `cache_lookup` 处加日志确认。

**需要观察的现象**：第一次运行后产生 `.tileirbc`；第二次启动同一内核显著快于第一次。

**预期结果**：`.tileirbc` 文件确实落盘；缓存命中使第二次编译几乎零开销。**文件确切大小 / magic 字节、两次启动的耗时差依赖环境，待本地验证。**

> 提示：`CUDA_TILE_DUMP_TILEIR`（MLIR 文本，`.tileir` 文件）依赖内部扩展 `cuda.tile_internal`，若未安装会打印「Can't print MLIR because the internal extension is missing」并跳过；而 `CUDA_TILE_DUMP_BYTECODE`（原始字节码，`.tileirbc` 文件）不依赖内部扩展，更稳妥，本实践因此选用后者。

#### 4.4.5 小练习与答案

**练习 1**：`EXPERIMENTAL_CUDA_TILE_DEBUG_BUILD=1` 时，`compile_cubin` 传给 tileiras 的 flags 会变成什么？为什么 device_debug 必须编进 `cache_key`？

> **答案**：flags 变成 `-O0` + `--device-debug`（而非默认的 `-O3` + `--lineinfo`），见 `_tileiras_effective_opt_and_device_debug`（[第 450–459 行](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_compile.py#L450-L459)）。因为同一份字节码在 `-O0/--device-debug` 与 `-O3/--lineinfo` 下产出完全不同的 cubin，若不把 device_debug 编进 key，调试构建会错误命中正常构建的缓存 cubin——这就是 `cache_key` 第 76 行把它编进高位的原因。

**练习 2**：`_get_max_supported_bytecode_version` 为什么要用「空字节码（`num_functions=0`）」去探测，而不是用真实内核的字节码？

> **答案**：探测的目的只是「问 tileiras 支不支持某个字节码版本」，与具体内核内容无关。用空字节码（最小合法输入）能把变量减到最少：只要 tileiras 能解析该版本的 magic 头 / 格式就说明支持，不会因为某个具体内核用了不支持的 op 而误判为「版本不支持」。

## 5. 综合实践

把本讲四个模块串起来，做一个「编译流水线全链路追踪」任务。

**任务**：写一个带 `ct.Constant[int]` 参数的 GEMM 内核（可基于 `samples/MatMul.py` 简化），用两种 tile 尺寸（如 `tm=tn=tk=16` 与 `32`）各 `ct.launch` 一次，然后回答下列问题，每个问题的答案都要能在 `compile_tile` 的源码里指出对应行号：

1. 这两次启动分别对应几个 `KernelSignature`？它们共享同一份 HIR 吗？（提示：4.1 的第 353 行只调一次 `get_function_hir`。）
2. 开启 `CUDA_TILE_LOGS=CUTILEIR`，确认两个 signature 各打印了一份 final IR，且内容因 Constant 取值不同而不同。（对应 4.2 的 `get_final_ir`。）
3. 开启 `CUDA_TILE_DUMP_BYTECODE=/tmp/cutile_bc`，确认产生了字节码文件；再开 `CUDA_TILE_CACHE_DIR` 指向一个空目录，连续启动两次同一 signature，第二次应命中 SQLite 缓存。（对应 4.4 的缓存接入点。）
4. 在源码旁为 `compile_tile` 的每一行标注阶段（前端 / IR 变换 / 后端 / 缓存），交出一份「带注释的 `compile_tile`」。

**验收标准**：你能用一句话向别人解释「为什么第一次启动 GEMM 很慢、第二次很快」，并且这句话的每个环节都能在 `compile_tile` 里指到具体行——HIR 生成一次、final IR 每 signature 一份、字节码一次、cubin 第一次经 tileiras 编译并写缓存、第二次直接 `cache_lookup` 命中。

> 说明：本任务涉及实际运行 cuTile，GEMM 的编译耗时与缓存命中行为依赖本地 GPU 与 tileiras 安装，相关数值「待本地验证」；但「第二次命中缓存跳过 tileiras」这一行为由源码逻辑保证，可由阅读确定。

## 6. 本讲小结

- `compile_tile` 是编译前端的总调度函数，被 `kernel._compile` 调用，被一把全局 `RLock` 包住以保证并发安全；它的主体是「线性向下 + 三处短路 return」。
- 流水线四段：**前端准备 →（AST→HIR）→ IR 生成 →（HIR→IR + `_transform_ir`）→ 后端 →（IR→字节码→cubin）→ 缓存**。三个返回值开关 `return_cubin / return_bytecode / return_final_ir` 决定在哪一步短路。
- `_IrKeeper` 按 signature 懒生成 final IR，用 `keep_all` 控制是否记忆；`get_final_ir` 内部依次建 IRContext、建参数 Var、`hir2ir`、组装 Block、`_transform_ir`、dtype 校验。
- `_transform_ir` 是固定顺序的优化 pass 流水线，关键约束是「循环不变量外提必须在 token 排序之后」；数据流分析结果被 divby 与 token_order 共享；两个测试开关可关闭 divby / token_order。
- `compile_cubin` 的本质是调用外部 `tileiras` 子进程，四级查找定位二进制，按 sm_arch / `-O` / `--lineinfo` 或 `--device-debug` 组装 flags；磁盘缓存用 SQLite 存 cubin，key 含编译器版本 / 架构 / 优化等级 / 字节码 / device-debug。
- IR 生成是被后端步骤「按需拉动」的：`_get_bytecode` 触发 `get_final_ir`，而非在流水线前端主动做完——这是理解整条链路最关键的一点。

## 7. 下一步学习建议

本讲把 `compile_tile` 的**编排**讲透了，但流水线里每个子模块仍是「黑盒」。建议按依赖顺序继续深入：

- **u5-l3（AST 到 HIR：ast2hir）**：展开本讲第 353 行的 `get_function_hir`，看 Python AST 如何变成结构化 HIR。
- **u5-l4（HIR 到 IR：hir2ir）**：展开 `get_final_ir` 第 281 行的 `hir2ir`，看 HIR Call 如何分派成具体 IR 操作、循环携带值如何用 phi 合并。
- **u5-l5（IR 核心）**：理解 `get_final_ir` 里反复出现的 `IRContext` / `Builder` / `Block` / `Var` / `Operation` 到底是什么。
- **U6（IR 优化 Pass）**：把本讲 4.3 里点到为止的每个 pass（DCE、数据流分析、整除传播、token 排序、代码外提、循环分裂、模式重写）逐个拆开。
- **U7（后端）**：展开本讲 4.4 的 `generate_bytecode_for_kernel`（u7-l1 字节码编码、u7-l2 字节码格式）、`compile_cubin` 与 `_find_compiler_bin`（u7-l3 tileiras 调用）、磁盘缓存（u7-l4 SQLite 缓存）。

一个推荐的「验收式」阅读法：读 u5-l3 ~ u7-l4 任一篇时，回到本讲的 `compile_tile` / `get_final_ir` / `_transform_ir` 源码，确认你能把那一篇讲的子模块「嵌」回它在流水线里的确切位置——能嵌回去，说明你真正掌握了整条链路。
