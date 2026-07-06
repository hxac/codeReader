# 调试、性能与开发者工具

## 1. 本讲目标

本讲是「运行时调度、导出与扩展」单元的收尾，集中讲解 cuTile 提供给内核开发者的**可观测性与调优工具**。读完本讲，你应当能够：

- 用一组环境变量（`CUDA_TILE_LOGS` / `CUDA_TILE_DUMP_*` / `CUDA_TILE_COMPILER_TIMEOUT_SEC` 等）在不改源码的前提下，把内核编译过程中的中间产物（cuTile IR、TileIR 字节码、MLIR）显式打印或落盘。
- 理解这些环境变量是如何在 `import cuda.tile` 时被一次性读取进 `TileContextConfig`，以及少数几个「模块级常量」为何走另一条路径在 `_debug.py` 里直接读取。
- 在 `tileiras` 编译失败或超时时，用 `enable_crash_dump` 收集一份可供 bug report 的崩溃快照 zip，并知道里面包含哪些产物。
- 用 `ct.compiler_timeout` 上下文管理器临时收紧/放宽编译超时，定位「编译卡死」类问题。
- 用 `kernel.replace_hints` 在不重定义内核的前提下切换编译期 hint（`num_ctas` / `occupancy` / `opt_level` / `num_worker_warps`，可配 `ByTarget`），并理解它为何会派生出**独立的 JIT 缓存**。

本讲覆盖五个最小模块：`CUDA_TILE_LOGS`、`CUDA_TILE_DUMP_TILEIR`、`enable_crash_dump`、`compiler_timeout`、`replace_hints`。

## 2. 前置知识

本讲假设你已经掌握前置讲义的两条主线：

- **编译流水线与后端**（u5-l2 / u7-l3）：Python 内核经 `AST → HIR → Tile IR →（优化 pass）→ 字节码 → tileiras → cubin → 缓存` 的链路被 JIT 编译。本讲的工具几乎全部插在这条链路的「观察点」上——打印某阶段的中间表示、把字节码落盘、在 `tileiras` 失败时留档。
- **launch 与 KernelFamily 缓存**（u8-l1）：`ct.launch` 触发的 JIT 链有两级缓存（按参数类型的 profile 缓存 + 按 Constant 取值的 kernel family 缓存），外加 u7-l4 的 SQLite 磁盘缓存。理解这一点才能理解「为什么改 hint 会重新编译」。

两个关键概念先点明，后面反复用到：

- **cuTile IR**：Python 前端降级、经过 `_transform_ir` 优化 pass 之后、序列化成字节码之前的树形 IR（`ir.Block`）。它由 `_IrKeeper.get_final_ir` 产出。
- **TileIR MLIR**：字节码经 `tileiras` 内部（确切地说是 `cuda.tile_internal._internal_cext.bytecode_to_mlir_text`）反序列化后得到的 MLIR 文本。**注意**：这个内部扩展目前不是公开特性，未安装时相关功能会优雅降级并打印提示。

整条链路上有几个天然的「观察窗口」，本讲就是教你如何把这些窗口打开：

```
Python 函数
  └─ get_function_hir ──► HIR                         ← log_ir_on_error 时报错打印
        └─ hir2ir + _transform_ir ──► cuTile IR (Block) ← CUDA_TILE_LOGS=CUTILEIR 打印
              └─ generate_bytecode_for_kernel ──► 字节码  ← CUDA_TILE_DUMP_BYTECODE 落盘 .tileirbc
                    └─ bytecode_to_mlir_text ──► MLIR     ← CUDA_TILE_LOGS=TILEIR 打印 / CUDA_TILE_DUMP_TILEIR 落盘 .tileir
                          └─ tileiras ──► cubin           ← 超时/失败时 enable_crash_dump 留档
```

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/cuda/tile/_context.py` | 定义 `TileContextConfig` 与所有「进程级」环境变量的解析函数（`CUDA_TILE_LOGS` / `CUDA_TILE_COMPILER_TIMEOUT_SEC` / `CUDA_TILE_ENABLE_CRASH_DUMP` / `CUDA_TILE_TEMP_DIR` / `CUDA_TILE_CACHE_*`），以及 `compiler_timeout` 上下文管理器。 |
| `src/cuda/tile/_debug.py` | 定义少数「模块级」调试开关（`CUDA_TILE_DUMP_TILEIR` / `CUDA_TILE_DUMP_BYTECODE` / `CUDA_TILE_TESTING_DISABLE_*` / `EXPERIMENTAL_CUDA_TILE_DEBUG_BUILD`），在 import 时直接读取。 |
| `src/cuda/tile/_compile.py` | 编译总入口 `compile_tile`。日志打印（`_IrKeeper`、`_log_mlir`）、dump 落盘、崩溃快照（`_compiler_crash_dump`）、超时调用 `tileiras`（`compile_cubin`）全部在此发生。 |
| `src/cuda/tile/_execution.py` | `kernel.replace_hints`：基于 `dataclasses.replace` 派生新内核。 |
| `src/cuda/tile/_compiler_options.py` | `CompilerOptions` 冻结 dataclass，列出全部可调 hint 字段。 |
| `docs/source/debugging.rst` / `docs/source/performance.rst` | 官方文档对环境变量、超时、性能 hint（`ByTarget`、`latency`/`allow_tma`、`assume_divisible_by`）的权威说明。 |

## 4. 核心概念与源码讲解

在进入五个最小模块前，先讲清一个贯穿全讲的**配置加载机制**，它会决定你「什么时候设置环境变量才生效」。

cuTile 的调试开关有**两条加载路径**，二者读取时机不同，这是初学者最容易踩的坑：

1. **进程级配置（`TileContextConfig`）**：在 `import cuda.tile` 时由 `init_context_config_from_env` 一次性解析进一个 `TileContextConfig` dataclass，之后挂在 `default_tile_context.config` 上被整个编译流程共享。`CUDA_TILE_LOGS`、`CUDA_TILE_COMPILER_TIMEOUT_SEC`、`CUDA_TILE_ENABLE_CRASH_DUMP`、`CUDA_TILE_CACHE_DIR/SIZE`、`CUDA_TILE_TEMP_DIR` 都走这条路径。**这意味着这些变量必须在 `import cuda.tile` 之前设置**（通常用 `os.environ[...] = ...` 写在脚本最顶端，或在启动 Python 前导出）。

   [`_context.py:15-35`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_context.py#L15-L35) —— `TileContextConfig` 字段与 `init_context_config_from_env` 把所有进程级开关归一到一个 dataclass。

2. **模块级常量（`_debug.py`）**：少数几个 dump 与测试开关在 `_debug.py` 被导入时直接 `os.environ.get`，成为模块级全局变量，再被 `_compile.py` 顶部导入。它们同样在 import 时定型。

   [`_debug.py:10-18`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_debug.py#L10-L18) —— `CUDA_TILE_DUMP_TILEIR` / `CUDA_TILE_DUMP_BYTECODE` 等在模块导入时即被读取。

   [`_compile.py:52-58`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L52-L58) —— `_compile.py` 顶部把这些常量导入，后续在 `compile_tile` 中按需消费。

> 一个例外是 `ct.compiler_timeout`——它是**运行时**修改 `compiler_timeout_sec` 的上下文管理器，可以在脚本任意位置临时生效（见 4.5）。

下面逐一展开五个最小模块。

### 4.1 CUDA_TILE_LOGS：编译期日志

#### 4.1.1 概念说明

`CUDA_TILE_LOGS` 是一个逗号分隔的关键字列表，决定编译过程中**往 stderr 打印哪些中间表示**。它只接受两个关键字：

- `CUTILEIR` → 打开 `log_cutile_ir`：打印**优化后的 cuTile IR**（`_transform_ir` 跑完的 `ir.Block`），即字节码生成前的那棵 IR 树。
- `TILEIR` → 打开 `log_tileir`：打印字节码经内部扩展反序列化后的 **TileIR MLIR** 文本。

这是「最小侵入」的调试手段：不改源码、不落盘，只在编译时把中间产物喷到 stderr，最适合用来定位 `TileTypeError`（看类型如何被提升/传播）或核对优化 pass 是否按预期改写了 IR。

#### 4.1.2 核心流程

```
CUDA_TILE_LOGS="CUTILEIR,TILEIR"
        │
        ▼  import 时解析
get_log_keys_from_env()  →  {log_cutile_ir=True, log_tileir=True}
        │
        ▼  存入 TileContextConfig
context.config.log_cutile_ir / log_tileir
        │
        ├── log_cutile_ir ──► _IrKeeper(log_cutile_ir=...)
        │                        ├─ 传给 IRContext(log_ir_on_error=...) → hir2ir 出错时打印 HIR
        │                        └─ get_final_ir 末尾打印 "==== CuTile IR for ... ===="
        │
        └── log_tileir   ──► compile_tile 中段调用 _log_mlir(bytecode_buf)
                                └─ _internal_cext.bytecode_to_mlir_text → 打印 MLIR module
```

两个关键字作用在编译流水线的**不同阶段**：`CUTILEIR` 作用在 IR 生成与优化之后、字节码生成之前；`TILEIR` 作用在字节码生成之后、交给 `tileiras` 之前。

#### 4.1.3 源码精读

环境变量解析——把字符串映射成 `TileContextConfig` 的布尔字段：

[`_context.py:48-68`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_context.py#L48-L68) —— `_LOG_KEYS` 把 `CUTILEIR`/`TILEIR` 映射到 `log_cutile_ir`/`log_tileir`；`get_log_keys_from_env` 按逗号切分、大小写不敏感，遇到未知关键字抛 `RuntimeError`（典型用法：`CUDA_TILE_LOGS=CUTILEIR` 或 `CUDA_TILE_LOGS=CUTILEIR,TILEIR`）。

`log_cutile_ir` 的消费点之一——在每个 signature 的 final IR 生成完毕后打印：

[`_compile.py:386-417`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L386-L417) —— `_IrKeeper.get_final_ir` 在 `_transform_ir` 之后（第 408-411 行）按 `self._log_cutile_ir` 把 `func_body.to_string(include_loc=False)` 打到 stderr；同时第 390 行把同一标志作为 `log_ir_on_error` 传给 `IRContext`。

`log_ir_on_error` 的另一面——`hir2ir` 出错时打印出错的 HIR 块并用 `-->` 标记出错位置：

[`hir2ir.py:117-127`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/hir2ir.py#L117-L127) —— 这是「编译报错时才打印」的路径，能在不污染正常日志的前提下，让报错带上出错的 HIR 上下文。它由 `log_cutile_ir` 间接打开。

`log_tileir` 的消费点——把字节码反序列化成 MLIR 文本后打印：

[`_compile.py:297-312`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L297-L312) —— `_log_mlir` 依赖内部扩展 `cuda.tile_internal._internal_cext`；缺失时打印降级提示，转换失败时打印 traceback 但不中断编译。

[`_compile.py:490-491`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L490-L491) —— `compile_tile` 在字节码生成之后，按 `context.config.log_tileir` 调用 `_log_mlir`。

#### 4.1.4 代码实践

1. **实践目标**：观察 `CUDA_TILE_LOGS=CUTILEIR` 打印出的 cuTile IR 长什么样，并理解它出现在流水线的哪个阶段。
2. **操作步骤**：
   - 写一个最小的 `vector_add` 内核（参考 u3-l1 的 load–compute–store 范式）。
   - 在脚本**最顶端**（`import cuda.tile` 之前）加入：
     ```python
     import os
     os.environ["CUDA_TILE_LOGS"] = "CUTILEIR"
     ```
   - 启动一次内核。
3. **需要观察的现象**：stderr 会打印一段 `==== CuTile IR for <kernel>====` 开头的文本，里面是 SSA 风格的 IR（含 `tile_load` / `tile_store` / 算术 op 等）。
4. **预期结果**：能在 IR 中找到你写的 load/store 与算术对应的 op；若把 `TILE_SIZE` 改成不同取值（作为 `Constant`），会看到打印的 IR 里嵌入了不同的常量字面量。
5. 若改用 `CUDA_TILE_LOGS=TILEIR` 但未安装内部扩展，会看到 `Can't print MLIR because the internal extension is missing.` 的降级提示——这是预期行为，**待本地验证** MLIR 是否能正常打印。

#### 4.1.5 小练习与答案

- **练习 1**：把 `CUDA_TILE_LOGS` 设成 `CUTILEIR,TILEIR` 与设成 `cutileir,tileir`（小写）效果一样吗？
  **答案**：一样。`get_log_keys_from_env` 对每段做 `.upper().strip()`，大小写与首尾空格不影响识别。
- **练习 2**：如果误写成 `CUDA_TILE_LOGS=CUTILE`（少打几个字母），会发生什么？
  **答案**：`_LOG_KEYS` 查不到该关键字，抛 `RuntimeError: Unexpected value CUTILE in CUDA_TILE_LOGS, supported values are ['CUTILEIR', 'TILEIR']`。

### 4.2 CUDA_TILE_DUMP_TILEIR 与 CUDA_TILE_DUMP_BYTECODE：落盘中间产物

#### 4.2.1 概念说明

`CUDA_TILE_LOGS` 是「打印到 stderr」，适合快速看一眼；当你需要**保存**中间产物（比如贴进 issue、做前后对比、或喂给其他工具）时，用 dump 系列把字节码与 MLIR **写到文件**：

- `CUDA_TILE_DUMP_BYTECODE=<dir>`：把序列化后的 TileIR **字节码**（`.tileirbc` 二进制）写到 `<dir>`。
- `CUDA_TILE_DUMP_TILEIR=<dir>`：把字节码反序列化后的 **MLIR 文本**（`.tileir`）写到 `<dir>`。

二者都把变量值设成**目标目录**（不是 `1/0`），目录不存在会自动创建。文件名由 `unique_path_from_func_desc` 按「函数名 + 文件名 + 行号」生成，避免多次编译互相覆盖。

#### 4.2.2 核心流程

```
compile_tile 生成 bytecode_buf
        │
        ├── 若 CUDA_TILE_DUMP_BYTECODE 是目录
        │       └─ unique_path_from_func_desc(...) → 写 .tileirbc（原始字节）
        │
        └── 若 CUDA_TILE_DUMP_TILEIR 是目录
                └─ bytecode_to_mlir_text(bytecode_buf) → 写 .tileir（MLIR 文本）
                   （依赖内部扩展，缺失则降级提示）
```

dump 与 log 的关键区别：dump 走 `_debug.py` 的模块级常量（不是 `TileContextConfig`），消费点是 `compile_tile` 里的两段独立 `if`。字节码 dump 不依赖任何内部扩展（就是写原始 `bytearray`），MLIR dump 依赖内部扩展。

#### 4.2.3 源码精读

模块级常量定义：

[`_debug.py:10-11`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_debug.py#L10-L11) —— `CUDA_TILE_DUMP_TILEIR` / `CUDA_TILE_DUMP_BYTECODE` 直接 `os.environ.get(..., None)`，值为目录路径字符串或 `None`。

字节码落盘：

[`_compile.py:493-499`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L493-L499) —— 检查 `CUDA_TILE_DUMP_BYTECODE`：目录不存在则 `os.makedirs`，用 `unique_path_from_func_desc` 生成临时文件名，把 `bytecode_buf` 原样写入，并往 stderr 打印文件路径。

MLIR 落盘：

[`_compile.py:502-514`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L502-L514) —— 检查 `CUDA_TILE_DUMP_TILEIR`：调用内部扩展 `bytecode_to_mlir_text` 得到 MLIR 文本后写盘；`ImportError` 时降级提示「内部扩展缺失，当前非公开特性」。

文件名生成器：

[`_compile.py:345-357`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L345-L357) —— `unique_path_from_func_desc` 用 `tempfile.NamedTemporaryFile` 在指定目录下，以前缀 `「函数名.文件名 stem.ln行号.」` + 后缀生成唯一文件，`delete=False` 保留落盘。

#### 4.2.4 代码实践

1. **实践目标**：把一个内核的字节码与 MLIR 同时落盘，拿到两个文件。
2. **操作步骤**：
   ```python
   import os
   os.environ["CUDA_TILE_DUMP_BYTECODE"] = "/tmp/cutile_dump_bc"
   os.environ["CUDA_TILE_DUMP_TILEIR"]   = "/tmp/cutile_dump_mlir"
   import cuda.tile as ct
   # ... 定义并 launch 一个 vector_add 内核
   ```
3. **需要观察的现象**：stderr 会分别打印 `Dumping TILEIR bytecode to file: ...` 与 `Dumping TILEIR MLIR module to file: ...`；两个目录里各出现一个文件。
4. **预期结果**：`.tileirbc` 是二进制（可用 `xxd` 查看 magic 头，参见 u7-l2）；`.tileir` 是可读的 MLIR 文本。
5. 若未安装内部扩展，`.tileir` 不会生成，只会看到降级提示——**待本地验证**。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 dump 变量的值是目录路径而不是 `1`？
  **答案**：源码用 `os.environ.get(..., None)` 判断「是否非空」，并把它的值当作写入目录；设成 `1` 会被当成名为 `1` 的目录，进而 `os.makedirs("1")` 在当前工作目录下建一个文件夹。正确做法是给一个真实路径。
- **练习 2**：连续 launch 同一个内核两次，会生成几个 `.tileirbc` 文件？
  **答案**：取决于是否走到 `compile_tile` 的字节码段。若第二次命中 JIT/磁盘缓存（u7-l4 / u8-l1），不会重新编译，也就不会再次落盘；若两次都实际编译（例如改了 hint 或清了缓存），会生成两个文件（`unique_path_from_func_desc` 保证不覆盖）。

### 4.3 enable_crash_dump：编译崩溃快照

#### 4.3.1 概念说明

当 `tileiras` 编译内核失败（抛 `TileCompilerExecutionError`）或超时（抛 `TileCompilerTimeoutError`）时，你通常只拿到一条错误信息，缺少复现现场。`CUDA_TILE_ENABLE_CRASH_DUMP=1` 让 cuTile 在这一刻把**所有相关产物**打包成一个 zip，便于附在 bug report 里。

崩溃快照的代价是：开启后 `_IrKeeper` 会**保留所有 signature 的 final IR**（`keep_all=True`），多占内存；因此默认关闭，只在排障时打开。

#### 4.3.2 核心流程

```
CUDA_TILE_ENABLE_CRASH_DUMP=1  →  config.enable_crash_dump=True
        │
        ▼  影响 _IrKeeper 构造
keep_all = enable_crash_dump or return_final_ir   # 保留每个 signature 的 final IR
        │
        ▼  正常编译直到 compile_cubin 抛 TileCompilerError
except TileCompilerError as e:
    if enable_crash_dump:
        重新生成「匿名化」字节码（anonymize_debug_info=True）
        _compiler_crash_dump(...)  →  写 crash_dump_{name}_{timestamp}.zip
    raise     # 仍然把异常抛出去
```

关键点：crash dump **不会吞掉异常**，它只是在 `raise` 之前额外留档；并且为了不泄露用户代码信息，它写进 zip 的字节码是**匿名化**后的（`anonymize_debug_info=True` 重新跑了一次 `_get_bytecode`）。

#### 4.3.3 源码精读

开关解析（接受多种「真」值写法）：

[`_context.py:85-88`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_context.py#L85-L88) —— `get_enable_crash_dump_from_env` 读 `CUDA_TILE_ENABLE_CRASH_DUMP`，`.lower()` 后匹配 `1/true/yes/on`，其余视为关闭。

让 `_IrKeeper` 保留全部 final IR：

[`_compile.py:474-480`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L474-L480) —— 构造 `_IrKeeper` 时 `keep_all=context.config.enable_crash_dump or return_final_ir`（第 480 行），决定 `final_ir` 列表是否被 memo 填充。

崩溃时的留档逻辑：

[`_compile.py:547-559`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L547-L559) —— `compile_cubin` 抛 `TileCompilerError` 时，若 `enable_crash_dump` 为真，先用 `anonymize_debug_info=True` 重新生成字节码，再调 `_compiler_crash_dump`，最后仍 `raise e`。

zip 内容拼装：

[`_compile.py:315-342`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L315-L342) —— `_compiler_crash_dump` 往 zip 写三类产物：`{func_name}.bytecode`（匿名化字节码）、`debug_info.txt`（错误信息 + 编译器 flags + 编译器版本 + cuTile 版本）、以及每个 signature 的 `{func_name}.{i}.cutileir`（final IR 文本）。zip 文件名形如 `crash_dump_{func_name}_{timestamp}.zip`，写在当前工作目录。

异常类型本身（决定何时触发）：

[`_exception.py:234-254`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_exception.py#L234-L254) —— `TileCompilerExecutionError`（`tileiras` 非零返回）与 `TileCompilerTimeoutError`（超时）都继承自 `TileCompilerError`，因此 `except TileCompilerError` 能同时兜住两者。

#### 4.3.4 代码实践

1. **实践目标**：人为触发一次编译失败，收集崩溃快照，检查 zip 内容。
2. **操作步骤**：
   ```python
   import os
   os.environ["CUDA_TILE_ENABLE_CRASH_DUMP"] = "1"
   import cuda.tile as ct
   # 写一个「故意不合法」的内核，例如对一个 float32 tile 做 float8 受限浮点算术
   # （受限浮点禁止隐式提升，会触发 TileTypeError / 编译期错误）
   ```
   - 启动内核，捕获抛出的异常。
3. **需要观察的现象**：stderr 打印 `Dumping crash artifacts to /abs/path/crash_dump_<name>_<ts>.zip`；当前工作目录下出现该 zip。
4. **预期结果**：解压后能看到 `debug_info.txt`（含 `cutile version`、`compiler version`、错误信息）、`<name>.bytecode`、以及一份或多份 `<name>.0.cutileir`。
5. 若你的内核恰好能编译通过，则不会生成 zip——**待本地验证**（可故意把 tile 维度设成非 2 的幂，或制造一个类型错误来触发）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 crash dump 写进 zip 的是「匿名化字节码」而不是直接用编译时的字节码？
  **答案**：编译用的字节码可能含 debug 信息（源码路径、行号等）会泄露用户代码。`anonymize_debug_info=True` 重新生成一份剥离了这些信息的字节码，既保留复现现场又便于公开分享（贴 issue）。
- **练习 2**：开启 crash dump 但内核正常编译成功，会有什么副作用？
  **答案**：不会生成 zip（`except` 块不触发），但 `_IrKeeper.keep_all=True` 会让每个 signature 的 final IR 都驻留在内存里（`final_ir` 列表被填满），多耗一些内存。

### 4.4 compiler_timeout：tileiras 编译超时

#### 4.4.1 概念说明

`tileiras` 编译复杂内核（大 tile、深循环、高 `num_worker_warps`）可能很慢，甚至因编译器 bug 卡死。`compiler_timeout` 让你给编译子进程设一个**硬超时**：

- **进程级默认值**：`CUDA_TILE_COMPILER_TIMEOUT_SEC=<正浮点数>`，在 import 时解析进 `config.compiler_timeout_sec`。
- **运行时临时覆盖**：`with ct.compiler_timeout(sec): ...`，仅在上下文内生效，退出后还原。

超时被触发时，cuTile 抛 `TileCompilerTimeoutError`，提示「`tileiras` compiler exceeded timeout ...s. Using a smaller tile size may reduce compilation time.」——这条建议直接指向「缩小 tile」是降低编译时长最有效的手段。

#### 4.4.2 核心流程

```
CUDA_TILE_COMPILER_TIMEOUT_SEC=60   ──►  config.compiler_timeout_sec=60.0   （import 时）
                                                 ▲
ct.compiler_timeout(10)  ──────────────►  临时改写 default_tile_context.config.compiler_timeout_sec
                                                 │  （with 退出后还原）
                                                 ▼
compile_tile  →  compile_cubin(..., timeout_sec=config.compiler_timeout_sec)
                       └─ binary.run(..., timeout_sec)  →  subprocess.run(..., timeout=...)
                              └─ 超时 →  except subprocess.TimeoutExpired →  TileCompilerTimeoutError
```

#### 4.4.3 源码精读

进程级默认值解析（必须为正）：

[`_context.py:38-45`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_context.py#L38-L45) —— `get_compile_timeout_from_env` 读 `CUDA_TILE_COMPILER_TIMEOUT_SEC`，`float(t)`，**值必须 > 0** 否则抛 `ValueError`；未设置时返回 `None`（即不限时）。

运行时上下文管理器（非线程安全）：

[`_context.py:108-127`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_context.py#L108-L127) —— `compiler_timeout` 临时把 `default_tile_context.config.compiler_timeout_sec` 改成新值，`finally` 里还原旧值。docstring 明确「not thread-safe」（它改的是全局共享状态）。

把超时传给 `tileiras` 子进程：

[`_compile.py:807-831`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L807-L831) —— `compile_cubin` 拼 `tileiras` 命令行（`--gpu-name`/`-O`/`--lineinfo` 或 `--device-debug`），把 `timeout_sec` 透传给 `binary.run`。

[`_compile.py:599-624`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L599-L624) —— `_CompilerBinary.run` 用 `subprocess.run(..., timeout=timeout_sec)`；`TimeoutExpired` 被翻译成 `TileCompilerTimeoutError`（带「缩小 tile」建议），`CalledProcessError`（非零返回）被翻译成 `TileCompilerExecutionError`。

#### 4.4.4 代码实践

1. **实践目标**：用 `ct.compiler_timeout` 给一个内核的编译设一个极短超时，观察 `TileCompilerTimeoutError`。
2. **操作步骤**：
   ```python
   import cuda.tile as ct
   try:
       with ct.compiler_timeout(0.001):   # 故意设得极短
           ct.launch(stream, grid, kernel, args)
   except ct.TileCompilerTimeoutError as e:
       print("caught:", e)
   ```
3. **需要观察的现象**：第一次（未命中缓存时）编译几乎立刻被中断，抛出超时异常，提示信息含「Using a smaller tile size may reduce compilation time.」。
4. **预期结果**：捕获到 `TileCompilerTimeoutError`；退出 `with` 后再普通 launch，超时恢复为原值（或 `None`），内核可正常编译。
5. 若内核 cubin 已在磁盘缓存（u7-l4）里，会直接命中、根本不调 `tileiras`，超时不生效——**待本地验证**（先清缓存再试）。

#### 4.4.5 小练习与答案

- **练习 1**：把 `CUDA_TILE_COMPILER_TIMEOUT_SEC` 设成 `0` 或 `-5` 会怎样？
  **答案**：`get_compile_timeout_from_env` 检测到 `t <= 0` 抛 `ValueError: Value of CUDA_TILE_COMPILER_TIMEOUT_SEC must be positive`，在 `import cuda.tile` 时即报错。
- **练习 2**：`ct.compiler_timeout` 为什么「不线程安全」？
  **答案**：它直接改写 `default_tile_context.config.compiler_timeout_sec` 这个**进程级共享**字段，多线程同时用不同 `with ct.compiler_timeout(...)` 会互相覆盖；它是为单线程脚本排障设计的便捷接口。

### 4.5 replace_hints：性能 hint 调整与 ByTarget

#### 4.5.1 概念说明

前面的模块都是「观察/排障」工具；本模块转向**性能调优**。cuTile 内核的性能旋钮（`num_ctas` / `occupancy` / `opt_level` / `num_worker_warps`）是在 `@ct.kernel(...)` 装饰时固定的。但调优过程往往要**对同一个内核尝试多组 hint**，重定义函数既繁琐又会破坏对原函数的引用。

`kernel.replace_hints(**hints)` 解决这个问题：它返回一个**新 kernel 对象**，编译选项为原选项被指定字段覆盖后的副本。因为编译选项影响生成的 cubin，新 kernel 拥有**独立的 JIT 缓存**——这正是 u8-l3 自动调优能把「best config」物化成一个新 kernel 的机制（`samples/AttentionFMHA.py` 就是这么用 `replace_hints` 的）。

四个 hint 字段（取自 `CompilerOptions`）：

| 字段 | 含义 | 取值 |
|------|------|------|
| `num_ctas` | CGA 中 CTA 数量 | 1–16 的 2 的幂 |
| `occupancy` | 每 SM 期望活跃 CTA 数 | [1, 32] |
| `opt_level` | 优化等级 | [0, 3]，默认 3 |
| `num_worker_warps` | warp-specialized 内核的 CUDA core warp group 大小 | 4 或 8（CTK 13.3+） |

任一字段都可用 `ByTarget` 包裹，按 GPU 架构取不同值（见 `performance.rst`）。

#### 4.5.2 核心流程

```
@ct.kernel(occupancy=2)
def kern(...): ...
        │
        ▼  replace_hints(occupancy=4, num_ctas=2)
dataclasses.replace(self._compiler_options, occupancy=4, num_ctas=2)  →  新 CompilerOptions
        │
        ▼  重新构造 kernel(self._pyfunc, **asdict(new_options))
返回新的 kernel 实例（同一个 pyfunc，不同 CompilerOptions）
        │
        ▼  launch 新 kernel
独立的 profile / kernel family 缓存键  →  重新编译出新的 cubin
```

#### 4.5.3 源码精读

`CompilerOptions` 是冻结 dataclass，`replace_hints` 能工作的前提：

[`_compiler_options.py:16-22`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compiler_options.py#L16-L22) —— 四个 hint 字段及其默认值；`frozen=True`（u5-l1 已述）保证选项不可变，因此「改 hint」只能派生新实例。

`replace_hints` 实现：

[`_execution.py:136-166`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L136-L166) —— 用 `dataclasses.replace(self._compiler_options, **hints)` 派生新 `CompilerOptions`，再用 `dataclasses.asdict` 展开成关键字，重新走一遍 `kernel(self._pyfunc, ...)` 构造。docstring 的 doctest 演示了「原 kernel 命中缓存 → `replace_hints` → 新 kernel 重新编译 → 新 kernel 也命中缓存」的完整过程，并明确「Because hints affects compilation, the returned object will have its own JIT cache.」

权威文档对性能 hint 的描述：

[`docs/source/performance.rst:9-14`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/performance.rst#L9-L14) —— 列出三类调优手段：`ByTarget`（架构相关配置）、`latency`/`allow_tma`（load/store hint）、`assume_divisible_by`（整除性 hint，承接 u6-l2）。

> 补充：除了 `replace_hints` 调整的「编译期 hint」，还有两类性能 hint 写在**内核体内**——`ct.load`/`ct.store` 的 `latency`、`allow_tma` 关键字（[`performance.rst:30-47`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/performance.rst#L30-L47)），以及 `ct.assume_divisible_by`（[`performance.rst:59-91`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/performance.rst#L59-L91)，u6-l2 已深入讲解）。`replace_hints` 调的是「不进内核体」的那一组。

`EXPERIMENTAL_CUDA_TILE_DEBUG_BUILD` 与 hint 的交互（性能调优时的一个反直觉点）：

[`_debug.py:16-18`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_debug.py#L16-L18) 与 [`_compile.py:569-578`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L569-L578) —— 当该实验开关为 `1` 时，无论 `opt_level` 设多少，`tileiras` 都强制用 `-O0` + `--device-debug`，并使用**独立的磁盘缓存键**。性能调优时务必确认这个开关未误开，否则你比较的「不同 `opt_level`」其实都是 `-O0`。

#### 4.5.4 代码实践

1. **实践目标**：用 `replace_hints` 对同一个内核生成两个不同 `occupancy` 的版本，确认它们各自独立编译。
2. **操作步骤**：
   ```python
   import cuda.tile as ct

   @ct.kernel(occupancy=2)
   def kern(a, b):
       # ... load/compute/store（任意一个你能跑通的内核）
       ...

   k_hi = kern.replace_hints(occupancy=8)
   # 分别 launch kern 与 k_hi
   ```
   - 配合 `CUDA_TILE_LOGS=CUTILEIR` 或 `CUDA_TILE_DUMP_BYTECODE` 观察编译次数。
3. **需要观察的现象**：launch `kern` 触发一次编译；launch `k_hi` 触发**另一次**编译（不同的 hint → 不同的 cubin）；再次 launch 同一个对象则命中各自的缓存。
4. **预期结果**：两组 hint 产出两个不同的 cubin（可通过 dump 目录里出现两个 `.tileirbc` 验证）。
5. 把 `opt_level` 通过 `ByTarget` 设成不同架构不同值，再 `replace_hints(opt_level=ct.ByTarget(...))`——**待本地验证** `ByTarget` 的展开行为。

#### 4.5.5 小练习与答案

- **练习 1**：`kern.replace_hints(occupancy=4)` 之后，`kern` 本身的 `occupancy` 变了吗？
  **答案**：没有。`replace_hints` 用 `dataclasses.replace` 派生**新** `CompilerOptions`、构造**新** kernel 并返回；`kern` 仍是原对象、原 `occupancy=2`。这是 frozen dataclass + 不可变内核身份的必然结果（也是缓存一致性的要求，u5-l1）。
- **练习 2**：为什么 `replace_hints` 是自动调优（u8-l3）能把 best config 物化的关键？
  **答案**：自动调优在搜索空间里选出最优配置后，需要「在不重定义内核的前提下」把这组 hint 烘焙进一个可复用的 kernel 对象，供后续真实推理反复 launch。`replace_hints` 正好提供这种「同 pyfunc + 新 hint + 独立缓存」的派生能力（`samples/AttentionFMHA.py` 即 `tuned_kernel = fmha_kernel.replace_hints(...)`）。

## 5. 综合实践

把本讲的「日志 + dump + 性能 hint + 性能剖析」串起来，对一个真实内核做一次完整调优排障：

1. **准备内核**：基于 `samples/MatMul.py`（或你自己写的 GEMM）准备一个分块 matmul 内核，把 `tm/tn/tk` 作为 `Constant` 嵌入。
2. **打开观测**（在 `import cuda.tile` 前）：
   ```python
   import os
   os.environ["CUDA_TILE_LOGS"]        = "CUTILEIR"          # 打印优化后的 cuTile IR
   os.environ["CUDA_TILE_DUMP_TILEIR"] = "/tmp/gemm_mlir"    # 落盘 MLIR
   os.environ["CUDA_TILE_DUMP_BYTECODE"] = "/tmp/gemm_bc"    # 落盘字节码
   os.environ["CUDA_TILE_ENABLE_CRASH_DUMP"] = "1"           # 编译失败时留档
   ```
3. **首次 launch**：检查 stderr 的 `==== CuTile IR for ... ====`，确认 K 维累加循环、`mma`、token 链等结构符合预期（对照 u3-l6 / u6-l3）。查看 `/tmp/gemm_mlir`、`/tmp/gemm_bc` 里的落盘文件。
4. **故意触发一次崩溃**：把 `tk` 改成非 2 的幂，重启脚本，确认生成了 `crash_dump_*.zip`，解压检查 `debug_info.txt` 与 `.cutileir`。
5. **超时演练**：用一个很大的 tile 配置，`with ct.compiler_timeout(5):` 包住 launch，确认能捕获 `TileCompilerTimeoutError`。
6. **性能 hint 调优**：
   ```python
   candidates = [
       kern.replace_hints(occupancy=o, num_worker_warps=w)
       for o in (1, 2, 4) for w in (4, 8)
   ]
   ```
   对每个候选 kernel 计时（或更规范地用 u8-l3 的 `tune.exhaustive_search`），选出最快的一个。
7. **Nsight Compute 剖析**（**待本地验证**，需 GPU 与 `ncu`）：
   - 由于 cuTile 内核默认带 `--lineinfo`（[`_compile.py:825-828`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L825-L828)），可用 `ncu` 附加到启动内核的 Python 进程做 source-correlated profile：
     ```bash
     ncu --set=full --target-processes all python your_script.py
     ```
   - 对照最快的候选 kernel，观察内存吞吐、张量核利用率、寄存器压力等指标，验证 `replace_hints` 选出的配置是否真的在硬件层面更优。
   - 注意：若误开了 `EXPERIMENTAL_CUDA_TILE_DEBUG_BUILD`，`tileiras` 会用 `-O0 --device-debug`，profile 数字不具代表性——调优前务必确认它关闭。

## 6. 本讲小结

- cuTile 的调试/性能开关有**两条加载路径**：进程级 `TileContextConfig`（`_context.py`，import 时一次性解析）与模块级常量（`_debug.py`）；除 `ct.compiler_timeout` 外，环境变量必须在 `import cuda.tile` 之前设置。
- `CUDA_TILE_LOGS=CUTILEIR[,TILEIR]` 把 cuTile IR / TileIR MLIR 打印到 stderr，是最轻量的观测手段；`CUTILEIR` 还会顺带打开「报错时打印出错 HIR 块」。
- `CUDA_TILE_DUMP_BYTECODE` / `CUDA_TILE_DUMP_TILEIR` 把字节码与 MLIR **落盘**到指定目录，文件名按「函数名.文件.ln行号」唯一生成；MLIR dump 依赖目前非公开的内部扩展，缺失时优雅降级。
- `CUDA_TILE_ENABLE_CRASH_DUMP=1` 在 `tileiras` 抛 `TileCompilerError` 时把「匿名化字节码 + debug_info + 各 signature 的 final IR」打包成 zip，便于 bug report；它会让 `_IrKeeper` 保留全部 final IR，默认关闭。
- `compiler_timeout`（`CUDA_TILE_COMPILER_TIMEOUT_SEC` 或 `ct.compiler_timeout(sec)` 上下文管理器）给 `tileiras` 子进程设硬超时，超时抛 `TileCompilerTimeoutError`，并提示「缩小 tile 可降低编译时长」。
- `kernel.replace_hints(**hints)` 用 `dataclasses.replace` 派生新内核，是性能 hint 调优与自动调优「物化 best config」的关键；新内核拥有独立 JIT 缓存。调优时留意 `EXPERIMENTAL_CUDA_TILE_DEBUG_BUILD` 会强制 `-O0`。

## 7. 下一步学习建议

- **若你想系统掌握性能 hint 的自动搜索**：阅读 u8-l3「自动调优 tune」，看 `exhaustive_search` 如何与 `replace_hints` 配合，在子进程 worker 里安全地对多组 hint 计时。
- **若你想深入理解日志里看到的 IR**：回到 u6 系列（数据流分析、token 排序、循环分裂），理解 `CUDA_TILE_LOGS=CUTILEIR` 打印的 IR 中那些 pass 各自贡献了什么。
- **若你想做跨架构发布**：结合 u8-l2（AOT 导出）与 `ByTarget`，把不同 sm 架构的最优 hint 烘焙进各自的 cubin。
- **源码延伸阅读**：`_context.py` 里还有 `CUDA_TILE_TEMP_DIR` / `CUDA_TILE_CACHE_DIR` / `CUDA_TILE_CACHE_SIZE`（磁盘缓存，u7-l4）等开关，以及 `_debug.py` 里给测试用的 `CUDA_TILE_TESTING_DISABLE_DIV` / `CUDA_TILE_TESTING_DISABLE_TOKEN_ORDER`（可在排障时二分定位是哪个 pass 出错，参见 u6-l2 / u6-l3）。
