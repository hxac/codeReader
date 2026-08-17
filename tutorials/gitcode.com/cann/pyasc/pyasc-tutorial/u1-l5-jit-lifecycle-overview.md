# 一次 JIT 调用的完整旅程：编译执行主链路总览

## 1. 本讲目标

前四讲我们知道了 pyasc 是什么、怎么装、代码放在哪、Add 示例怎么跑。本讲把镜头拉高，回答一个贯穿全书的问题：

> 当你写下 `vadd_kernel[8, stream](x, y, z, block_length)` 并敲下回车，pyasc 内部到底依次发生了什么？

学完本讲，你应该能够：

1. 不看资料说出主链路的五步调用顺序：`_run` → `_cache_kernel` → `_run_codegen` → `_run_compiler` → `_run_launcher`。
2. 说出每一步的输入与输出：源码/AST → ASC-IR（`ir.ModuleOp`）→ Ascend C 文本 → Kernel 二进制（`.o`）→ 设备执行。
3. 理解 `CodegenOptions` / `CompileOptions` / `LaunchOptions` 三类配置各管哪一段、分别从哪里传入。
4. 会用 `PYASC_DUMP_PATH` 环境变量导出 `codegen.mlir`、`ascir.mlir`、`ascendc.cpp`（以及 `binary.o`）四个中间产物，并在它们之间找到同一个 `asc.add` 的对应关系。

本讲是「地图讲」：只建立全局观，不深入任何一个模块的细节。后续单元 3（runtime 模块逐文件精读）、单元 4（FunctionVisitor）、单元 5/6（Dialect 与 Pass）都会反复回到这张地图。

## 2. 前置知识

### 2.1 回顾：三层中间表示

u1-l1 已经给出核心数据流，这里从「主链路的加工对象」角度再表述一次：

```text
Python 源码 ──(inspect/ast)──> AST ──(FunctionVisitor)──> ASC-IR (MLIR)
        ──(Translation)──> Ascend C 文本 ──(毕昇编译器)──> .o 二进制 ──(aclrt)──> NPU 执行
```

- **AST**：Python 自带的语法树，`ast` 标准库的产物。
- **ASC-IR**：pyasc 基于 MLIR 定义的中间表示（`Asc` 方言），是前后端的分界线。
- **Ascend C**：昇腾的 C++ 算子编程语言，pyasc 的接口与它一一对应。

### 2.2 JIT（Just-In-Time，即时编译）

与 C/C++ 的「提前编译」不同，JIT 指**第一次调用函数时才编译**。这带来两个必然现象，本讲会反复用到：

1. 第一次调用慢（要做完整编译），后续调用快（命中缓存直接执行）。
2. 「编译什么」取决于**这次调用传了什么参数**——参数类型不同，生成的 Kernel 就不同，这正是缓存 key 要考虑参数类型的原因。

### 2.3 缓存（cache）与缓存 key

JIT 每次都重新编译显然太浪费。pyasc 用两级缓存避免重复编译：

- **内存缓存**：进程内的一个字典，进程退出即失效。
- **文件缓存**：落盘到 `~/.pyasc/cache`（可用 `PYASC_CACHE_DIR` 改路径），下次运行程序仍然有效。

是否重编译由**缓存 key** 决定：key 相同 → 复用；key 变化 → 重编。

### 2.4 MLIR 与 Pass

- **MLIR**：一个可扩展的编译基础设施，「方言（Dialect）+ 操作（Operation）」是它的基本词汇。pyasc 的 ASC-IR 就是一个自定义方言（u1-l3 已介绍目录镜像规律）。
- **Pass（编译 pass，可理解为「一趟 IR 变换」）**：对 IR 做一次遍历和改写，例如「把惰性声明的 Tensor 物化成真实内存分配」。多个 Pass 按固定顺序排成流水线。

本讲只需记住：`codegen.mlir` 是 Pass **之前**的 IR，`ascir.mlir` 是 Pass **之后**的 IR。

### 2.5 dataclass 与选项对象

pyasc 用 Python 的 `@dataclass` 定义三个「选项袋」类（`CodegenOptions`、`CompileOptions`、`LaunchOptions`）。每个字段就是一个可配置项，字段名就是合法的关键字参数名。理解了这一点，你就知道 `@asc.jit(debug=True)` 里的 `debug` 能取哪些值：去对应 dataclass 里查字段即可。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [python/asc/runtime/jit.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py) | `@asc.jit` 与 `JITFunction`：主链路的「总调度」 | `_run` / `_cache_kernel` / `_run_codegen` / `_run_compiler` / `_run_launcher` 五步 |
| [python/asc/codegen/function.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py) | `Function` 基类：装饰时抓源码、算缓存 key、分流参数 | `self.node`（AST）从哪来 |
| [python/asc/codegen/function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py) | AST → ASC-IR 的转换器（本讲只看构造） | `_run_codegen` 里 visitor 如何被创建 |
| [python/asc/runtime/compiler.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py) | `Compiler`：跑 Pass、翻译成 Ascend C、调毕昇编译 | `run` 中三份 dump 文件的落盘时机 |
| [python/asc/runtime/launcher.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py) | `Launcher`：参数打包、注册二进制、下发执行 | `run` 的收尾动作 |
| [python/asc/runtime/cache.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py) | 两级缓存与缓存 key | `get_mem_cache_key` / `get_file_cache_key` |
| [python/asc/runtime/utils.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/utils.py) | 文件工具 | `FileUtils.dump_file` 的「目录为 None 则跳过」 |
| [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py) | u1-l4 精读过的 Add 示例 | 本讲所有实践的实验对象 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. **4.1 `JITFunction._run` 主链路**：五步调用顺序与每步的输入输出。
2. **4.2 编译子链与两级缓存**：`_cache_kernel` 如何决定「编译还是复用」。
3. **4.3 三类 Options 的分工**：配置从哪里进、管到哪一段。
4. **4.4 中间产物 dump**：`PYASC_DUMP_PATH` 与四个导出文件。

### 4.1 主链路：`JITFunction._run` 的五步旅程

#### 4.1.1 概念说明

u1-l4 讲过：`@asc.jit` 把函数包装成 `JITFunction` 对象，`kernel[核数, 流](参数)` 触发执行。本模块把「触发执行」之后的事完整展开。

`JITFunction` 采用**组合式设计**：它本身不实现编译器，而是持有三个**可替换的类属性**——`codegen`（默认 `FunctionVisitor`）、`compiler`（默认 `Compiler`）、`launcher`（默认 `Launcher`）。主链路只是按顺序把这三者串起来：

```text
kernel[core_num, stream](args...)
   │  __getitem__ 解析中括号 → 得到 core_num/stream，返回 self._run
   ▼
_run(*args, **kwargs)                          # jit.py:204
   │  ① 分流 options、绑定参数、拆分 runtime/constexpr
   ▼
_cache_kernel(...)                             # jit.py:156  查缓存，未命中才编译
   │  ├─ _run_codegen(spec, options)           # jit.py:184  AST → ASC-IR
   │  │     └─ FunctionVisitor.visit(node) → ir.ModuleOp
   │  └─ _run_compiler(mod, options)           # jit.py:196  IR → Pass → Ascend C → .o
   │        └─ Compiler.run(mod, name) → CompiledKernel
   ▼
_run_launcher(kernel, launch_options, args)    # jit.py:200  下发执行
   └─ Launcher.run(kernel, name, args) → rt.launch_kernel(...)
```

#### 4.1.2 核心流程

把 `_run` 逐行翻译成自然语言：

1. **合并选项**：`kwargs` 与装饰时传入的默认选项合并（所以 `@asc.jit(debug=True)` 和调用时传 `debug=True` 等价，调用时的值优先）。
2. **抽取两类选项**：从 `kwargs` 里把属于 `CodegenOptions`、`CompileOptions` 的字段分别抽走，剩下的才是函数实参。
3. **绑定参数**：`inspect.signature(self.fn).bind(...)` 把位置参数/关键字参数对齐到函数签名。
4. **拆分参数**：按类型标注把参数分成 `runtime_args`（进 Kernel ABI）和 `constexprs`（编译期常量）。
5. **取二进制**：`_cache_kernel` 返回 `CompiledKernel`（缓存命中则直接复用）。
6. **下发执行**：`_run_launcher` 把二进制和实参交给 `Launcher`。

#### 4.1.3 源码精读

**入口语法：`__getitem__` 把中括号变成启动配置。**

[python/asc/runtime/jit.py:48-57](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L48-L57) 实现了 `kernel[8, stream]` 这种语法：Python 的 `obj[...]` 会调用 `__getitem__`，这里把中括号内容解析成 `LaunchOptions`（单个整数是核数，元组依次是 `core_num`、`stream`），然后**返回 `self._run` 方法本身**——所以 `kernel[8, stream](x, y)` 等价于「先配置启动参数，再立刻调用」。

**组合式设计：三个类属性决定三个阶段由谁实现。**

[python/asc/runtime/jit.py:30-33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L30-L33) 中 `codegen`、`compiler`、`launcher` 都是类属性。想做实验（比如换一个自定义 Compiler）时，子类覆盖其一即可，不用动主链路。

**主流程 `_run`：选项分流 → 参数绑定 → 编译 → 执行。**

[python/asc/runtime/jit.py:204-212](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L204-L212) 就是上图五步的源码本体。注意 205 行先把装饰器选项与调用选项合并；206-207 行 `extract_kwargs` 从中抽走配置；210 行 `split_args` 完成运行时参数与编译期常量的分流；211 行拿二进制；212 行下发。

**AST 从哪来：装饰时就抓好了。**

`self.node` 不是调用时才解析的——`Function` 基类在**装饰那一刻**就用 `inspect.getsource` 抓源码、`ast.parse` 得到 `ast.FunctionDef` 并缓存在 `self.node`：[python/asc/codegen/function.py:36-49](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L36-L49)（构造）、[python/asc/codegen/function.py:89-100](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L89-L100)（`get_function_node` 的抓取与解析）。所以到 `_run_codegen` 时 AST 是现成的。

**`_run_codegen`：创建 MLIR 上下文，让 visitor 遍历 AST。**

[python/asc/runtime/jit.py:184-194](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L184-L194)：先 `create_context()` 建立MLIR Context 并加载方言（实现见 [python/asc/runtime/jit.py:106-111](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L106-L111)），再把全局构造器 `global_builder` 绑定到该 Context（`set_ir_builder` 会同时创建空模块，见 [python/asc/language/core/utils.py:143-151](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L143-L151)），然后 `visitor.visit(self.node)` 遍历 AST——遍历过程中每个 `asc.xxx` 调用都会通过 `create_asc_*` 系列函数往模块里添加 IR 操作。`finally` 里的 `teardown()` 保证无论成功失败都清理全局状态。

**`_run_compiler` 与 `_run_launcher`：薄薄的委托层。**

[python/asc/runtime/jit.py:196-198](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L196-L198) 构造 `Compiler` 并调用其 `run`；[python/asc/runtime/jit.py:200-202](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L200-L202) 构造 `Launcher` 并调用其 `run`。真正的重活在这两个类内部（4.2 与单元 3 展开）。

**收尾：`Launcher.run` 做了什么。**

[python/asc/runtime/launcher.py:127-151](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L127-L151)：按 `kernel.kernel_args` 声明补齐参数（如 FftsAddr）、可选的 debug 缓冲、把参数规整成字节块、`rt.register_device_binary_kernel` 注册二进制、`rt.register_function` 拿到函数句柄，最后 `launch_kernel` → `rt.launch_kernel(function, core_num, inputs, stream)` 真正下发，`rt.synchronize()` 等待完成，再拷回输出、释放内存。注意 128-130 行有个 `DRY_RUN` 环境变量：设置了就只走完编译、直接跳过下发（无设备时的编译实验利器，见 4.4 实践）。

#### 4.1.4 代码实践：给五步主链路「打点计时」

在不修改仓库源码的前提下，用 monkey-patch 在自己的实验脚本里给三个阶段包一层计时器。

1. **实践目标**：亲眼看到「首次调用走了 codegen + compiler，第二次调用两者都没走」，把 `_run` 的五步从文字变成观测。
2. **操作步骤**：
   - 把 `examples/01_add/add.py` 复制到自己的实验目录（例如 `~/pyasc-lab/timing_add.py`），不要改动仓库内的示例文件。
   - 在文件 `import asc` 之后、`@asc.jit` 之前插入以下**示例代码**：

     ```python
     import time
     import asc.runtime.jit as jit_mod

     def wrap(name):
        orig = getattr(jit_mod.JITFunction, name)
        def timed(self, *a, **kw):
            t0 = time.perf_counter()
            r = orig(self, *a, **kw)
            print(f"[trace] {name}: {time.perf_counter() - t0:.4f}s")
            return r
        setattr(jit_mod.JITFunction, name, timed)

     for name in ("_run_codegen", "_run_compiler", "_run_launcher"):
        wrap(name)
     ```

   - 先删除旧缓存再运行（保证第一次一定编译）：`rm -rf ~/.pyasc/cache && python3 timing_add.py -r Model`（无 NPU 时用 Model 模式；有 NPU 则 `-r NPU -v Ascend910B1`）。
   - 再**立刻运行第二次**（不动缓存）：`python3 timing_add.py -r Model`。
3. **需要观察的现象**：
   - 第一次运行应打印 `_run_codegen` 与 `_run_compiler` 的耗时（通常明显大于 `_run_launcher`）。
   - 第二次运行应**完全没有** `_run_codegen` / `_run_compiler` 的打印，只剩 `_run_launcher`——因为 `_cache_kernel` 在内存/文件缓存命中后提前返回，这两个函数根本没被调用。
4. **预期结果**：第二次总耗时显著低于第一次，证明「编译只发生一次，执行每次发生」。耗时数值与机器相关，具体秒数**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`vadd_kernel[8, stream](x, y, z, block_length)` 里的中括号语法在源码里由哪个方法实现？它返回什么？

答案：由 `JITFunction.__getitem__`（[python/asc/runtime/jit.py:48-57](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L48-L57)）实现。它把中括号内容解析成 `LaunchOptions`（存入 `self.launch_options`），然后返回 `self._run` 本身，因此后面紧跟的小括号就是真正调用 `_run`。

**练习 2**：为什么 `_run_codegen` 要放在 `try/finally` 里调用 `global_builder.teardown()`？

答案：`global_builder` 是进程级全局单例（[python/asc/language/core/utils.py:170](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L170)），codegenn 过程中会把 builder/模块挂到它身上供 language 层 API 使用。若遍历 AST 中途抛异常（比如遇到不支持的语法），不加 `finally` 会导致脏的 builder 留在全局位置，污染同进程内下一个 Kernel 的编译。

**练习 3**：`DRY_RUN` 环境变量设置后，主链路停在哪一步？为什么它适合「无设备环境验证编译」？

答案：停在 `_run_launcher` 内部：[python/asc/runtime/launcher.py:128-130](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L128-L130) 在 `Launcher.run` 开头检查到该变量即 `return`，跳过参数注册与下发；而其前的 codegen、Pass、翻译、毕昇编译全部照常执行，dump 文件也照常生成。因此它能验证「编译是否成功」而不需要真正把 Kernel 跑在设备上（是否可在纯 CPU 环境使用取决于 CANN 仿真组件是否可用，待本地验证）。

### 4.2 编译子链与两级缓存：`_cache_kernel` 的决策逻辑

#### 4.2.1 概念说明

`_run` 拿到参数后的第一件大事不是编译，而是**问缓存**：「这套参数 + 这套选项，之前编过吗？」这由 `_cache_kernel` 完成。它像一个三层的漏斗：

```text
请求编译 (runtime_args, constexprs, 两类 options)
   │
   ├─ 第 1 层：内存缓存 self.kernel_cache（进程内字典）
   │     命中 → 直接返回 CompiledKernel（最快，微秒级）
   │
   ├─ 第 2 层：文件缓存 ~/.pyasc/cache/<key>/<函数名>.o（pickle 的 CompiledKernel）
   │     命中 → 反序列化返回（免编译，毫秒级）
   │
   └─ 第 3 层：真编译
         ├─ _run_codegen : AST → ASC-IR (ir.ModuleOp)
         └─ _run_compiler: IR → Ascend C → 毕昇 → .o 字节
         结果同时写回内存缓存与文件缓存
```

`always_compile=True`（`CompileOptions` 的字段）可以强行跳过前两层，永远走第三层——调试 dump 时非常关键（见 4.4）。

#### 4.2.2 核心流程

缓存 key 的构造分两级：

\[ \text{mem\_key} = \mathrm{sha256}(\text{cache\_factors}) \]

\[ \text{file\_key} = \mathrm{sha256}(\ \text{pyasc\_key} \,\|\, \text{fn\_cache\_key} \,\|\, \text{cache\_factors}\ ) \]

其中 `cache_factors` 是五段内容的字符串拼接（[python/asc/runtime/jit.py:137-154](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L137-L154)）：

| 段 | 内容 | 变化后果举例 |
|----|------|--------------|
| 1 | 全部 `CodegenOptions` 字段 | 改 `capture_exceptions` → 重编 |
| 2 | 全部 `CompileOptions` 字段 | 改 `opt_level` → 重编 |
| 3 | 所有 `ConstExpr` 参数的名与值 | `TILE_NUM` 值变了 → 重编 |
| 4 | 所有运行时参数的**类型**（不含值） | `int` 换 `float` → 重编；只改数值 → **不**重编 |
| 5 | 函数名 | 改名 → 重编 |

`pyasc_key`（[python/asc/runtime/cache.py:108-136](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L108-L136)）把整个前端（`codegen/`、`language/` 下所有 `.py`）和 `libpyasc` 扩展库的哈希拼进 file key——**升级 pyasc 或改了前端源码后旧文件缓存自动失效**，这是防止「新编译器用旧缓存」的安全阀。`fn_cache_key` 则来自 `Function.cache_key`（[python/asc/codegen/function.py:63-87](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L63-L87)），由函数源码哈希、起始行号和全局 `ConstExpr` 依赖构成——改了 kernel 源码，缓存也会失效。

#### 4.2.3 源码精读

**决策主体 `_cache_kernel`。**

[python/asc/runtime/jit.py:156-182](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L156-L182) 完整实现了三层漏斗：160-162 行查内存缓存；164-167 行算 file key、取缓存目录中的 `<函数名>.o` 文件；169-172 行文件命中则 `pickle.load` 反序列化；173-176 行两层都未命中才真正执行 `_run_codegen` + `_run_compiler`；178-180 行把结果写回两级缓存。每一处判断都有 `not compile_options.always_compile` 前缀——强制编译模式下连「写缓存」都被跳过。

**两级 key 的计算。**

[python/asc/runtime/cache.py:139-150](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L139-L150)：`get_file_cache_key` 拼接 `pyasc_key + fn_cache_key + cache_factors` 后取 sha256；`get_mem_cache_key` 只对 `cache_factors` 取 sha256。两者都带 `@functools.lru_cache`，相同的输入不会重复计算。

**缓存目录与原子写。**

[python/asc/runtime/cache.py:20-26](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L20-L26) 定义缓存根目录（`PYASC_CACHE_DIR` 或 `PYASC_HOME` 下的 `.pyasc/cache`）；[python/asc/runtime/cache.py:66-92](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L66-L92) 的 `put` 先写进临时目录再用 `os.replace` 原子替换，避免并发进程读到写了一半的文件。

#### 4.2.4 代码实践：验证「什么会触发重编译」

1. **实践目标**：用可观测的证据（缓存目录内容 + 耗时）验证上表的 5 个 key 因素，重点是「参数值变化不重编、ConstExpr 值变化重编」。
2. **操作步骤**（在自己实验目录中，基于复制的 add.py 改造）：
   - 在 kernel 调用前后加计时（`time.perf_counter`），并打印 `os.path.getmtime` 观察缓存文件变化；也可以直接观察 `~/.pyasc/cache` 下的目录列表。
   - 实验一（参数值变化）：把 Host 侧 `size = 8 * 2048` 改成 `size = 8 * 4096` 后重跑（`block_length` 数值随之变化，但类型仍是 `int`）。
   - 实验二（ConstExpr 变化）：把模块级常量 `TILE_NUM = 8` 改成 `TILE_NUM = 16` 后重跑。注意：`TILE_NUM` 在 kernel 里被当作全局常量使用，其值参与函数缓存哈希（见 `Function.cache_key` 对全局依赖的处理，[python/asc/codegen/function.py:79-86](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L79-L86)）。
   - 实验三（强制编译）：调用时传 `always_compile=True`，即 `vadd_kernel[USE_CORE_NUM, rt.current_stream()](x, y, z, block_length, always_compile=True)`。
3. **需要观察的现象**：实验一第二次运行应当秒回（无重编译）；实验二第一次运行应重新出现编译耗时且生成新的缓存子目录；实验三每次运行都重编译。
4. **预期结果**：与上表一致。`TILE_NUM` 作为全局变量参与失效的具体行为依赖 `DependenciesFinder` 的分析（单元 3 的 u3-l2 会精读），此处若观测与预期不符，记录现象待 u3-l2 解释。具体耗时**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：同一段代码，`block_length` 传 2048 和 4096，会生成几份 Kernel？为什么？

答案：一份。`cache_factors` 第 4 段只记录参数的**类型**（`get_arg_dtype`，[python/asc/runtime/jit.py:113-123](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L113-L123)），两个值都是 `int`，key 相同。`block_length` 是运行时参数，值通过参数区在 launch 时传入，不烧进 Kernel。

**练习 2**：为什么文件缓存 key 要额外混入 `pyasc_key`，而内存缓存不需要？

答案：内存缓存存活于当前进程，进程里加载的一定是「当前这份」pyasc 代码，天然一致。文件缓存跨进程存活，可能是在旧版本 pyasc（或被你改过的前端源码）下生成的；`pyasc_key` 把前端所有 `.py` 与 `libpyasc` 的哈希计入（[python/asc/runtime/cache.py:108-136](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L108-L136)），版本一变 key 就变，旧缓存自然不会被复用。

**练习 3**：`always_compile=True` 时，第 178-180 行的「写回缓存」为什么也被跳过？

答案：写回条件同样带着 `not compile_options.always_compile`。强制编译模式通常用于调试：用户想看的正是「当前代码此刻编译出的产物」，若写回缓存，后续正常模式可能读到这份调试配置下生成的 Kernel，造成行为混淆。

### 4.3 三类 Options 的分工：配置从哪里进、管到哪一段

#### 4.3.1 概念说明

主链路被切成 codegen、compiler、launch 三段，每段配一个选项袋：

| 选项类 | 定义位置 | 从哪里传入 | 管辖范围 | 代表字段 |
|--------|----------|-----------|----------|----------|
| `CodegenOptions` | [python/asc/codegen/function_visitor.py:36-39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L36-L39) | 小括号 kwargs / 装饰器 | AST → ASC-IR | `capture_exceptions`、`ir_multithreading` |
| `CompileOptions` | [python/asc/runtime/compiler.py:27-41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L27-L41) | 小括号 kwargs / 装饰器 | Pass 流水线 + Ascend C 翻译 + 毕昇编译 | `opt_level`、`debug`、`kernel_type`、`insert_sync`、`verify_sync`、`always_compile` |
| `LaunchOptions` | [python/asc/runtime/launcher.py:48-51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L48-L51) | **中括号** `kernel[...]` | 参数打包与任务下发 | `core_num`、`stream` |

记忆法：**小括号管「编成什么样」，中括号管「怎么跑」**。

#### 4.3.2 核心流程

```text
@asc.jit(debug=True)            # 装饰器默认选项（任意一类字段都能放）
     │
kernel[8, stream](x, y, n, opt_level=3)     # 调用
     │
     ├─ __getitem__((8, stream)) → LaunchOptions(core_num=8, stream=stream)   # 中括号 → LaunchOptions
     │
     └─ _run(*args, **kwargs)
          ├─ merge_dict(default_options, kwargs)          # 装饰器选项 + 调用选项合并
          ├─ extract_kwargs(CodegenOptions, kwargs)       # 按字段名抽走 Codegen 字段
          ├─ extract_kwargs(CompileOptions, kwargs)       # 按字段名抽走 Compile 字段
          └─ 剩下的 kwargs + args → 函数实参
```

三条隐含规则：

1. **字段名即关键字名**。合法关键字 = 三个 dataclass 的全部字段名并集（[python/asc/runtime/jit.py:125-135](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L125-L135) 的 `get_config_keywords` 有缓存地收集它们）。
2. **函数形参名不得与配置关键字撞名**，否则报错（[python/asc/runtime/jit.py:37-43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L37-L43)、[python/asc/runtime/jit.py:59-64](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L59-L64)）——因为撞名的关键字会被当成配置抽走，函数永远收不到它。
3. **传了不认识的关键字直接报错**（[python/asc/runtime/jit.py:41-43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L41-L43)），这是防拼错（如 `debu=True`）的保护。

#### 4.3.3 源码精读

**抽取器 `extract_kwargs`。**

[python/asc/runtime/jit.py:96-104](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L96-L104)：用 `dataclasses.fields` 枚举目标选项类的字段名，从 `kwargs` 里「认领」同名条目并从原字典删除，最后实例化选项对象。`_run` 连续调用它两次（Codegen 一次、Compile 一次），剩下的就是函数实参。

**合法关键字清单 `get_config_keywords`。**

[python/asc/runtime/jit.py:125-135](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L125-L135)：遍历三个 dataclass 收集字段名，结果缓存在类属性 `_config_keywords` 里，避免每次装饰都反射。

**`CompileOptions` 字段全景。**

[python/asc/runtime/compiler.py:27-41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L27-L41)：注意几个后续单元的主角——`run_passes`（能否跳过整个 Pass 流水线）、`kernel_type`（不填则由 Pass 检测，见 [python/asc/runtime/compiler.py:184-189](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L184-L189)）、`insert_sync`（自动同步重建开关）、`verify_sync`（同步校验）、`always_compile`（绕过缓存）。

**`LaunchOptions` 与中括号的对接。**

[python/asc/runtime/launcher.py:48-51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L48-L51) 只有两个字段 `core_num`、`stream`，顺序与 `kernel[core_num, stream]` 的元组一致，因此 [python/asc/runtime/jit.py:53](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L53) 直接 `LaunchOptions(*user_launch_options)` 解包。

#### 4.3.4 代码实践：亲手触发两条选项保护

1. **实践目标**：通过两个故意的错误，验证「形参与配置关键字冲突」和「未知选项名」两条保护规则。
2. **操作步骤**（在自己的实验目录新建小脚本）：

   ```python
   import asc

   @asc.jit                      # 示例代码：故意撞名
   def bad_kernel(x: asc.GlobalAddress, stream):   # stream 是 LaunchOptions 字段
       pass

   @asc.jit(foo=1)               # 示例代码：故意传未知选项
   def bad_options(x: asc.GlobalAddress, n: int):
       pass
   ```

   逐个取消注释导入并观察（`bad_kernel` 在**装饰时**就会报错，`bad_options` 同样在装饰时报错）。
3. **需要观察的现象**：第一段应抛出 `The following argument names conflict with JIT configuration options: stream`；第二段应抛出 `The following option names are unknown: foo`。
4. **预期结果**：与 [python/asc/runtime/jit.py:37-43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L37-L43) 的两个 `RuntimeError` 文案一致；这说明冲突检查发生在**装饰时**而非调用时，能在最早时刻拦截问题。报错文案**待本地验证**（以实际输出为准）。

#### 4.3.5 小练习与答案

**练习 1**：`@asc.jit(core_num=4)` 能替代 `kernel[4](...)` 吗？

答案：不能。`core_num` 属于 `LaunchOptions`，小括号/装饰器传入的 kwargs 只会被抽取成 `CodegenOptions` 和 `CompileOptions`（`_run` 里只调了两次 `extract_kwargs`，[python/asc/runtime/jit.py:206-207](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L206-L207)）。`LaunchOptions` 只能经 `__getitem__`（中括号）设置。运行期核数是执行属性，同一份编译产物可用不同核数多次启动，把它混进「编译配置」语义上也不成立。

**练习 2**：想在编译期看到每个 Pass 之前的 IR，应该传哪个字段？它属于哪一类 Options？

答案：`print_ir_before_all=True`，属于 `CompileOptions`（[python/asc/runtime/compiler.py:32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L32)），它在 [python/asc/runtime/compiler.py:178-179](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L178-L179) 被消费（`pm.enable_printing()`）。写法：`kernel[8, stream](..., print_ir_before_all=True)`。

**练习 3**：为什么不把三个 Options 合并成一个大类？

答案：三者生命周期与来源不同：`LaunchOptions` 每次调用都可能变（跟中括号走）且**不参与缓存 key**；`CodegenOptions`/`CompileOptions` 描述「编成什么样」，**全部字段进缓存 key**（4.2 的第 1、2 段）。合并会让「改核数要不要重编」这类问题变得含糊，也失去 `get_config_keywords` 按类枚举的清晰边界。

### 4.4 中间产物 dump：PYASC_DUMP_PATH 与四个导出文件

#### 4.4.1 概念说明

编译链路对用户是黑盒，但 pyasc 提供了一个「开窗」开关：设置环境变量 `PYASC_DUMP_PATH=<目录>` 后，`Compiler` 会把链路上的关键产物写到该目录：

| 文件 | 产生位置 | 内容 | 对应链路段 |
|------|----------|------|-----------|
| `codegen.mlir` | [python/asc/runtime/compiler.py:164](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L164) | Pass **之前**的 ASC-IR（前端原始产出） | `_run_codegen` 的输出 |
| `ascir.mlir` | [python/asc/runtime/compiler.py:167](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L167) | Pass **之后**的 ASC-IR（规范化后） | `run_passes` 的输出 |
| `ascendc.cpp` | [python/asc/runtime/compiler.py:171](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L171) | 翻译出的 Ascend C 源码 | `run_translation` 的输出 |
| `binary.o` | [python/asc/runtime/compiler.py:199-200](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L199-L200) | 毕昇编译出的 Kernel 目标文件 | `run_compilation` 的输出 |

两个容易踩的坑：

1. **dump 只发生在真编译时**。缓存命中不会创建 `Compiler`，自然一个文件都不会写。想稳定拿到 dump，配 `always_compile=True` 或先清缓存。
2. dump 目录不存在会自动创建（[python/asc/runtime/compiler.py:104](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L104) 调 `FileUtils.create_dir`），无需手动 `mkdir`（该函数对 `None` 直接跳过，见 [python/asc/runtime/utils.py:38-50](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/utils.py#L38-L50)）。

#### 4.4.2 核心流程

`Compiler.run` 的骨架（本讲只看 dump 时机，Pass 细节留给单元 3/6）：

```text
run(mod, func_name):
    dump codegen.mlir  ← str(mod)          # ① Pass 前 IR
    if run_passes: run_passes(mod)         # ② 三阶段 Pass 流水线
    dump ascir.mlir    ← str(mod)          # ③ Pass 后 IR
    source = translation.ir_to_ascendc(mod)# ④ IR → Ascend C 文本
    (若 enable_debug: 注入 dump 代码)
    dump ascendc.cpp                        # ⑤ Ascend C 源码
    kernel_args = ir.get_kernel_arg_attrs(mod)
    run_compilation(source, kernel_args)    # ⑥ 写 input.cce → 毕昇 → output.o
        └─ dump binary.o                    # ⑦ 目标文件副本
    return CompiledKernel(binary 字节, core_type, ...)
```

对 Add 示例，同一次 `asc.add`（[examples/01_add/add.py:60-61](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L60-L61)）在四个文件里的形态：

- **Python 层**：`asc.add(z_local[...], x_local[...], y_local[...], tile_length)`，`count` 形式对应 L2 级 API，前端在 [python/asc/language/basic/vec_binary.py:40-43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L40-L43) 分发到 `create_asc_AddL2Op`；
- **codegen.mlir / ascir.mlir**：名为 `asc.add_l2` 的 IR 操作（操作名由 `defm Add : BinaryTemplateL0123Op<"add", ...>` 展开，L0/L1/L2/L3 各生成一个变体，见 [include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td:23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td#L23) 与 [include/ascir/Dialect/Asc/IR/Base.td:209-213](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L209-L213)）；
- **ascendc.cpp**：对 `Add(...)` 的 Ascend C 函数调用；
- **binary.o**：该调用的机器码形态（已不可读）。

#### 4.4.3 源码精读

**dump 目录的初始化。**

[python/asc/runtime/compiler.py:98-104](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L98-L104)：`Compiler.__init__` 读 `PYASC_DUMP_PATH`，解析成绝对路径并创建目录。紧随其后的 106-113 行还顺带定位了毕昇编译器与链接器（可用 `PYASC_COMPILER`/`PYASC_LINKER` 覆盖，默认 `bisheng`/`ld.lld`）——找不到直接抛错，这是「装了 pyasc 但没装 CANN」时最常见的报错点。

**`run`：三份文本产物的落盘点。**

[python/asc/runtime/compiler.py:162-173](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L162-L173)：注意 `run` 是 `@final` 方法（子类不可覆盖，保证 dump 语义稳定）。164 行在跑任何 Pass **之前**先 dump `codegen.mlir`——这就是它「忠实记录前端产出」的保证；166 行 `run_passes` 原地修改 `mod`；167 行再 dump 得到 `ascir.mlir`；168 行调 [python/asc/runtime/compiler.py:115-117](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L115-L117) 的 `run_translation`（底层是 `translation.ir_to_ascendc`，C++ 侧实现，单元 6 精读）；171 行 dump `ascendc.cpp`。

**`run_compilation`：`.o` 的生成与副本。**

[python/asc/runtime/compiler.py:193-209](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L193-L209)：在临时目录里把 Ascend C 文本写成 `input.cce`，经 `_gen_dst_kernel`（按 `kernel_type` 选架构、组命令行、调毕昇，[python/asc/runtime/compiler.py:274-324](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L274-L324)）产出 `output.o`；199-200 行把它复制为 dump 目录里的 `binary.o`；最终读成字节装进 `CompiledKernel` 返回。

**dump 工具函数。**

[python/asc/runtime/utils.py:23-35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/utils.py#L23-L35)：`dump_file` 第一行就是 `if dump_dir is None: return`——没设环境变量时零开销，这就是 dump 开关的实现方式。

#### 4.4.4 代码实践：三级产物对照 `asc.add`

这是本讲的核心实践（对应任务书）：导出三个文件，找到同一个 `asc.add` 的三种形态。

1. **实践目标**：建立「Python 调用 → IR 操作 → Ascend C 调用」的一一对应观，验证黑盒里确实发生了这三步变换。
2. **操作步骤**：
   - 在自己实验目录复制 `add.py` 为 `dump_add.py`，把 kernel 调用改为带 `always_compile=True`（保证必然重编译、必然产生 dump）：

     ```python
     vadd_kernel[USE_CORE_NUM, rt.current_stream()](x, y, z, block_length, always_compile=True)
     ```

   - 运行（Model 模式即可，无需 NPU）：

     ```bash
     PYASC_DUMP_PATH=/tmp/pyasc_dump python3 dump_add.py -r Model
     ```

   - 打开 `/tmp/pyasc_dump/` 下的四个文件，依次搜索：
     - `codegen.mlir`：搜 `add`（`count` 形式应命中 `asc.add_l2` 一类操作名；若搜不到精确名，退而搜 `add` 逐条核对）；
     - `ascir.mlir`：同样搜 `add`，对比它与 `codegen.mlir` 中该操作周围的差异（Pass 改写了什么）；
     - `ascendc.cpp`：搜 `Add(`，找到对 `zLocal/xLocal/yLocal` 的 Ascend C 调用；
     - `binary.o`：`file binary.o` 或直接查看大小，确认它是目标文件。
3. **需要观察的现象**：
   - 三个文本文件中都存在与 `asc.add` 对应的条目，且操作数/参数个数一致（3 个 tensor + 1 个 count）；
   - `ascendc.cpp` 中还能找到 `vadd_kernel` 的 `__aicore__` 入口函数、`DataCopy`、`SetFlag`/`WaitFlag` 等与源码逐行对应的调用；
   - `codegen.mlir` 与 `ascir.mlir` 存在明显结构差异（例如 tensor 声明与同步的位置、数量），这就是 Pass 流水线的工作痕迹。
4. **预期结果**：完成下面这张对应表（摘录实际内容填入，「示例」列给出格式示意）：

   | 层 | 摘录（以实际 dump 为准） |
   |----|--------------------------|
   | Python | `asc.add(z_local[...], x_local[...], y_local[...], tile_length)` |
   | codegen.mlir | `asc.add_l2 ...` 操作（含 3 个 tensor 操作数与 count） |
   | ascir.mlir | 同名操作，但上下文已被 Pass 规范化 |
   | ascendc.cpp | `Add(zLocal, xLocal, yLocal, tileLength)` 形式的调用 |

   具体文本以本地 dump 为准（IR 的打印格式、变量名可能与示意不同），**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：设置了 `PYASC_DUMP_PATH` 但目录里一个文件都没有，最可能的原因是什么？

答案：缓存命中。dump 发生在 `Compiler.run` 里，而缓存命中时 `_cache_kernel` 根本不会构造 `Compiler`（[python/asc/runtime/jit.py:160-172](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L160-L172) 提前返回）。解法：传 `always_compile=True`，或删除 `~/.pyasc/cache` 对应子目录。

**练习 2**：`codegen.mlir` 和 `ascir.mlir` 内容完全一样意味着什么？

答案：两种可能：其一，`run_passes=False` 被设置，Pass 流水线整个被跳过（[python/asc/runtime/compiler.py:165-166](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L165-L166)），两次 dump 之间什么都没发生；其二，该 Kernel 的 IR 恰好没被任何 Pass 改写。对正常 Add 示例而言后者不太可能（至少样板生成、参数合法化等收尾 Pass 会追加内容），因此首先应检查是否传了 `run_passes=False`。

**练习 3**：想给「无设备环境」验证编译链路，除了 Model 模式还有什么办法？

答案：设置 `DRY_RUN=1` 环境变量：[python/asc/runtime/launcher.py:128-130](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L128-L130) 会让 `Launcher.run` 在注册与下发之前直接返回，而编译段（含全部 dump）已完整执行。注意 `Compiler.__init__` 仍需定位到毕昇编译器与 CANN 组件（[python/asc/runtime/compiler.py:106-113](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L106-L113)），所以它不能替代 CANN 安装；在具体环境是否可用**待本地验证**。

## 5. 综合实践：为 Add 算子写一份「全链路体检报告」

把本讲四个模块串起来，产出一份可留档的报告（建议存为 `add-pipeline-report.md`，放在你自己的实验目录）。

任务步骤：

1. **准备**：复制 `examples/01_add/add.py` 到实验目录，改造调用为 `always_compile=True`；准备干净缓存。
2. **采集产物**：`PYASC_DUMP_PATH=... python3 dump_add.py -r Model`，确认 4 个文件齐全。
3. **对应关系**：按 4.4.4 的方法摘录 `asc.add`、`asc.data_copy`、`asc.set_flag` 三条调用在 Python / codegen.mlir / ascir.mlir / ascendc.cpp 四层的形态，各写一行（共 12 格的对照表）。
4. **链路打点**：加入 4.1.4 的计时补丁，记录 `_run_codegen`、`_run_compiler`、`_run_launcher` 三段耗时；去掉 `always_compile=True` 再跑一次，记录缓存命中的总耗时。
5. **缓存实验**：按 4.2.4 只改 `size`（参数值）重跑一次、再改 `TILE_NUM`（编译期常量）重跑一次，记录哪种情况触发了重编译（依据：dump 目录文件时间戳是否更新 / 计时补丁是否打印 codegen）。
6. **结论**：用 5 到 8 行总结——一次 JIT 调用经历了哪些阶段、每个阶段读什么写什么、哪些因素导致重编译。

验收标准：报告能回答「`asc.add` 的一行 Python 如何变成 NPU 上的一条指令路径」「为什么第二次调用快」「改哪个参数会触发重编译」三个问题。

## 6. 本讲小结

- 主链路五步：`_run`（选项分流 + 参数绑定）→ `_cache_kernel`（两级缓存决策）→ `_run_codegen`（AST → ASC-IR）→ `_run_compiler`（Pass + 翻译 + 毕昇编译 → `.o`）→ `_run_launcher`（注册并下发执行）。
- `JITFunction` 是组合式总调度：`codegen`/`compiler`/`launcher` 三个类属性分别指向三个可替换的实现类。
- AST 在**装饰时**就被 `Function` 基类抓取缓存（`inspect.getsource` + `ast.parse`），调用时直接复用。
- 两级缓存按 key 复用 `CompiledKernel`：内存缓存看 `cache_factors`，文件缓存额外混入 `pyasc_key` 与函数源码哈希；`always_compile=True` 可强制绕过。
- 三类 Options 分工：`CodegenOptions`/`CompileOptions` 从小括号或装饰器进入并参与缓存 key；`LaunchOptions`（`core_num`、`stream`）只能从中括号进入，不参与缓存 key。
- `PYASC_DUMP_PATH` 导出 `codegen.mlir`（Pass 前 IR）→ `ascir.mlir`（Pass 后 IR）→ `ascendc.cpp`（Ascend C 源码）→ `binary.o`（目标文件），且**只在真编译时导出**——缓存命中时不产生任何文件。

## 7. 下一步学习建议

本讲是一张总地图，四条支线都只开了个头。建议按单元顺序继续：

1. **单元 2（u2-l1 起）**：如果想让「读示例」升级为「写算子」，先学类型系统（`DataType`/`ConstExpr`）与 Tensor 抽象——它们正是 `_run_codegen` 里 visitor 所消费的语言层对象。
2. **单元 3（u3-l1 起）**：沿本讲的 `jit.py` 继续下钻——u3-l1 精读 `JITFunction` 与 `@asc.jit` 的完整实现，u3-l4/u3-l5 展开 `Compiler.run_passes` 的三阶段 Pass 调度与毕昇编译命令的组装，u3-l6/u3-l8 精读 Launcher 参数 ABI 与缓存细节。
3. **单元 4（u4-l1 起）**：对 `_run_codegen` 中一句带过的 `visitor.visit(self.node)` 感兴趣的话，FunctionVisitor 如何把每类 AST 节点变成 IR 操作是整个前端的灵魂。
4. **动手验证**：完成第 5 节综合实践后，带着你的 dump 产物去读 [include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td)，尝试在 `.td` 定义里找到你在 `codegen.mlir` 中看到的每一个 `asc.` 操作——这是进入单元 5（Dialect 与 TableGen）前最好的热身。
