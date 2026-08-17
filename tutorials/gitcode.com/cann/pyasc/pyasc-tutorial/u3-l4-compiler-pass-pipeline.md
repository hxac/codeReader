# 编译器驱动：CompileOptions 与 MLIR Pass 流水线

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐字段说清 `CompileOptions` 中每个选项的作用，并知道它们分别影响「跑哪些 Pass」「拼什么编译命令」「是否进缓存 key」。
2. 按顺序列出 lowering、optimizing、postprocessing 三个阶段各自调度的 Pass 名称，并能区分「MLIR 通用 Pass」和「pyasc 自定义的 ascendc Pass」。
3. 讲清 `Compiler.run` 的五个步骤与 `codegen.mlir`、`ascir.mlir`、`ascendc.cpp`、`binary.o` 四个 dump 产物的落盘时机。
4. 解释 `insert_sync` 三态语义（None/True/False）：`need_insert_sync` 如何检测 `LocalTensorAuto` 风格并触发「EraseSync → HoistQueBind → InsertSync」同步重建链。
5. 掌握 `kernel_type` 为 `None` 时的自动推导规则：DetectKernelTypePass 在 IR 上打 `asc.compile_mix` 属性，Python 侧读该属性决定 `MIX_AIC_1_2` / `AIC_ONLY` / `AIV_ONLY`。

## 2. 前置知识

### 2.1 什么是 Pass 与 PassManager

MLIR 中的 **Pass（变换）** 是一个「读入 IR、改写 IR」的独立单元，例如「把循环不变量提到循环外」「删除死代码」。**PassManager（Pass 管理器）** 是一个有序的 Pass 容器：`pm.run(mod)` 会按添加顺序依次对模块执行每个 Pass。pyasc 没有自己造 Pass 框架，而是直接复用 MLIR 的 PassManager，通过 pybind11 暴露给 Python（后面会看到 `python/src/Passes.cpp`）。

Pass 有两个作用层级，这个区别在 pyasc 源码里肉眼可见：

- **模块级 Pass**：作用于整个 `ModuleOp`（整个编译单元）。
- **函数级 Pass**：嵌套作用于每个 `func::FuncOp`（每个函数各自跑一遍）。

### 2.2 承接前讲：Compiler 在主链路中的位置

回顾 u1-l5 与 u3-l1 建立的主链路：`JITFunction._run` 在缓存未命中时调用 `_run_compiler`，把 FunctionVisitor 生成的 `ir.ModuleOp` 交给 `Compiler`。注意 `Compiler` 是**每次真编译时新构造一个**（见 [python/asc/runtime/jit.py:L196-L198](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L196-L198)），构造时传入从调用参数里抽出来的 `CompileOptions`。所以本讲解读的所有「选项」都是**一次编译一份**，不存在跨调用残留。

另外回顾 u3-l1 的一个结论：`@asc.jit` 小括号里的选项名白名单由 `CodegenOptions + CompileOptions + LaunchOptions` 三个 dataclass 的字段拼成，所以本讲讲的每个 `CompileOptions` 字段都可以在调用时用关键字参数覆盖。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/asc/runtime/compiler.py` | 本讲主角：`CompileOptions` 选项袋、`Compiler.run/run_passes` 主流程、三个 `_schedule_*` Pass 调度函数 |
| `python/asc/runtime/config.py` | `KernelType` 八种核类型枚举、`Backend`/`Platform` 与 `set_platform` |
| `include/ascir/Dialect/Asc/Transforms/Passes.td` | 全部 pyasc 自定义 Pass 的 TableGen 声明（名字、作用、构造器、作用层级） |
| `python/src/Passes.cpp` | 把 PassManager 和所有 Pass 注册成 Python 可调用的 `passes.*` 函数 |
| `python/src/IR.cpp` | `ModuleOp.need_insert_sync` 的 C++ 实现（检测 `LocalTensorAutoOp`） |
| `lib/Dialect/Asc/Transforms/DetectKernelTypePass.cpp` | 在 IR 上打 `asc.compile_mix` 属性的 Pass 实现 |
| `python/asc/runtime/utils.py` | `FileUtils.dump_file`：四个中间产物的实际写盘工具 |
| `python/test/kernels/insert_sync/test_vadd.py` | `LocalTensorAuto` 风格示例（自动触发 insert_sync 的活样本） |
| `examples/02_add_framework/add_framework.py` | 本讲实践的运行对象 |

## 4. 核心概念与源码讲解

### 4.1 CompileOptions：编译选项袋

#### 4.1.1 概念说明

`CompileOptions` 是一个 dataclass「选项袋」：所有影响**编译过程**（而不影响运行时下发）的开关都集中在这里。它承接 u3-l1 讲过的机制——`_run` 里的 `extract_kwargs(CompileOptions, kwargs)` 会把调用时传的同名关键字参数抽出来装进这个袋子。

按「影响什么」可以把字段分成三类：

| 类别 | 字段 | 影响对象 |
| --- | --- | --- |
| 影响 Pass 调度 | `run_passes`、`insert_sync`、`verify_sync`、`strip_loc`、`print_ir_before_all`、`matmul_cube_only`、`kernel_type` | `_schedule_*` 三个函数里装不装某个 Pass |
| 影响毕昇编译命令 | `opt_level`、`auto_sync`、`auto_sync_log`、`bisheng_options`、`debug` | `_get_compiler_cmd` 拼出的命令行 |
| 影响缓存与调试 | `always_compile`（绕过两级缓存） | `jit.py` 的 `_cache_kernel` |

注意一个细节：**整个 `CompileOptions` 的全部字段值都会拼进文件缓存 key**（见 [python/asc/runtime/jit.py:L137-L142](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L137-L142) 把 `vars(compile_options).items()` 逐项 join）。这意味着哪怕只是加一个 `print_ir_before_all=True` 做观察，也会生成一个新的缓存 key、触发一次真编译——观察类选项并不「免费」。

#### 4.1.2 核心流程

`Compiler.__init__` 在构造时做四件事：

1. 查询 SoC 版本，映射到 `CompilePlatform`（910B 系或 910_93 系）。
2. 调 `_check_compile_options` 做三道合法性校验，任何一道不过直接抛 `RuntimeError`。
3. 读取 `PYASC_DUMP_PATH` 环境变量准备 dump 目录。
4. 用 `shutil.which` 定位毕昇编译器与链接器可执行文件（可用 `PYASC_COMPILER`/`PYASC_LINKER` 覆盖）。

#### 4.1.3 源码精读

选项袋定义：

```python
@dataclass
class CompileOptions:
    debug: bool = False
    strip_loc: bool = False
    verify_sync: bool = False
    print_ir_before_all: bool = False
    run_passes: bool = True
    kernel_type: Optional[KernelType] = None
    ...
    insert_sync: Optional[bool] = None
```

这是 [python/asc/runtime/compiler.py:L27-L41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L27-L41) 的定义，上面代码只摘了与本讲相关的字段。注意 `kernel_type` 与 `insert_sync` 都是 `Optional` 且默认 `None`——`None` 不是 `False`，而是「交给框架自动推导」，这是 4.5 节的主题。

构造函数中的三道校验：

```python
is_soc_version_valid = self.soc_version.value.startswith("Ascend910B") or \
    self.soc_version.value.startswith("Ascend910_93")
is_core_type_valid = self.options.kernel_type is None or (isinstance(self.options.kernel_type, KernelType) and \
    self.options.kernel_type.value <= 7 and self.options.kernel_type.value >= 0)
is_opt_level_valid = self.options.opt_level in [1, 2, 3]
```

见 [python/asc/runtime/compiler.py:L211-L217](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L211-L217)。这三行分别校验：SoC 版本受支持、`kernel_type` 要么为 `None` 要么是 0~7 的合法枚举、`opt_level` 只能取 1/2/3。任何一项不过，`__init__` 就在 [python/asc/runtime/compiler.py:L91-L92](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L91-L92) 抛出 `RuntimeError("Please check input compile option")`。

一个文档与代码不一致的坑：[docs/architecture_introduction.md:L99](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/architecture_introduction.md#L99) 写 `opt_level` 取值范围是 `0,1,2,3`，但代码实际只放行 `[1, 2, 3]`——传 `opt_level=0` 会直接报错。**读代码时以 `_check_compile_options` 为准**。

dump 目录与工具链定位：

```python
dump_dir = os.environ.get("PYASC_DUMP_PATH", None)
...
compiler = shutil.which(os.environ.get("PYASC_COMPILER", "bisheng"))
linker = shutil.which(os.environ.get("PYASC_LINKER", "ld.lld"))
```

见 [python/asc/runtime/compiler.py:L98-L113](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L98-L113)。`PYASC_DUMP_PATH` 在这里被一次性读入 `self.dump_dir`，之后所有 dump 都往这里写；毕昇编译器默认从 `PATH` 里找 `bisheng`。

#### 4.1.4 代码实践

1. **实践目标**：亲手触发一次选项校验失败，并确认「观察类选项会改变缓存 key」。
2. **操作步骤**：
   - 在装好 pyasc 的机器上，把 `examples/02_add_framework/add_framework.py` 的调用改为 `vadd_kernel[USE_CORE_NUM, rt.current_stream()](x, y, z, block_length, tile_length, opt_level=0)`（示例代码，改动方式参考 u3-l1 讲过的调用处传选项）。
   - 运行 `python3 add_framework.py -r Model`，记录报错信息。
   - 恢复 `opt_level`，先正常跑一次；再以 `print_ir_before_all=True` 跑第二次。
3. **需要观察的现象**：
   - 第一次：抛出 `RuntimeError: Please check input compile option`（由 `_check_compile_options` 返回 `False` 触发）。
   - 第二组：两次运行都发生了真实编译而非缓存命中——因为 `print_ir_before_all` 字段值进了文件缓存 key。
4. **预期结果**：报错文本与 [python/asc/runtime/compiler.py:L92](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L92) 完全一致；带 `print_ir_before_all=True` 的那次运行在 stderr 上输出大量 IR（见 4.4 节）。具体报错栈的层级为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`debug` 和 `print_ir_before_all` 都和「调试」沾边，它们的作用对象有什么不同？

**答案**：`print_ir_before_all=True` 只影响 Python 侧 PassManager，让每个 Pass 前后把 IR 打到 stderr（观察编译器自身的行为）；`debug=True` 则进入 `_get_compiler_cmd`，给毕昇编译器追加 `-g` 等选项（见 [python/asc/runtime/compiler.py:L335-L336](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L335-L336)），生成带调试信息的 Kernel 二进制（观察 Kernel 自身的行为）。

**练习 2**：为什么 `insert_sync` 的类型是 `Optional[bool]` 而不是 `bool`？

**答案**：因为它需要区分三种状态：`None` 表示「由框架根据 IR 里有没有 `LocalTensorAutoOp` 自动判定」（默认值）；`True` 表示「强制走同步重建链」；`False` 表示「强制跳过」。如果只用 `bool`，默认值 `False` 就会和「用户显式要求跳过」混淆，自动推导将无从谈起。

### 4.2 KernelType：八种核类型

#### 4.2.1 概念说明

昇腾芯片上有两类计算核心：**Vector 核（向量核，AIV）** 负责向量/元素级计算，**Cube 核（矩阵核，AIC）** 负责矩阵乘。一个 Kernel 可能只跑在其中一类上，也可能两类混合跑。`KernelType` 枚举把所有组合形式化。它是 `CompileOptions.kernel_type` 字段的类型，直接决定后面「编译成什么架构的目标代码、编译几个 `.o`、用什么 CoreType 下发」。

`KernelType` 定义在 `config.py` 而不是 `compiler.py`，因为它同时被前端（`@asc.jit(kernel_type=...)` 白名单校验）和编译器使用，放在中立的 `config` 模块里避免循环依赖。

#### 4.2.2 核心流程

`kernel_type` 的取值流向：

```
用户显式指定 @asc.jit(kernel_type=KernelType.XXX)
        │
        ▼
CompileOptions.kernel_type = XXX ──► 直接使用（_check_compile_options 校验 0~7）
        │ 用户没指定（None）
        ▼
run_passes 跑完 Pass 后读 IR 属性推导（4.5 节）
        │
        ▼
CompilationTarget.get(kernel_type, platform) ──► 决定 arch（dav-c220-vec / dav-c220-cube）
        │
        ▼
run_compilation ──► 决定 CompiledKernel.core_type（AiCore / VectorCore / CubeCore）
```

#### 4.2.3 源码精读

八种核类型：

```python
class KernelType(Enum):
    """get kernel type"""
    AIV_ONLY = 0          # 纯向量核
    AIC_ONLY = 1          # 纯矩阵核
    MIX_AIV_HARD_SYNC = 2 # 混合：向量核 + 硬同步
    MIX_AIC_HARD_SYNC = 3 # 混合：矩阵核 + 硬同步
    MIX_AIV_1_0 = 4       # 混合：向量核 1_0 形态
    MIX_AIC_1_0 = 5       # 混合：矩阵核 1_0 形态
    MIX_AIC_1_1 = 6       # 混合：cube+vec 双目标编译
    MIX_AIC_1_2 = 7       # 混合：cube+vec 双目标编译
```

见 [python/asc/runtime/config.py:L36-L45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/config.py#L36-L45)。

`kernel_type` 如何映射到编译目标架构：

```python
if kernel_type in [KernelType.MIX_AIC_1_1, KernelType.MIX_AIC_1_2]:
    return CompilationTarget(vec_arch="dav-%s-vec" % arch, cube_arch="dav-%s-cube" % arch, ...)
elif kernel_type in [KernelType.MIX_AIV_1_0, KernelType.MIX_AIV_HARD_SYNC, KernelType.AIV_ONLY]:
    return CompilationTarget(common_arch="dav-%s-vec" % arch, ...)
else:
    return CompilationTarget(common_arch="dav-%s-cube" % arch, ...)
```

见 [python/asc/runtime/compiler.py:L68-L74](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L68-L74)。三条分支：MIX_AIC 系要**同时**给出 vec 与 cube 两个架构（后续编两个 `.o` 再链接）；AIV 系只需 vec 架构；其余（AIC_ONLY、MIX_AIC_HARD_SYNC、MIX_AIC_1_0）用 cube 架构。

#### 4.2.4 代码实践

1. **实践目标**：用源码回答「`MIX_AIC_1_2` 和 `AIV_ONLY` 各会产出几个目标文件」，不运行任何代码。
2. **操作步骤**：对照阅读 [python/asc/runtime/compiler.py:L291-L324](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L291-L324) 的 `_gen_dst_kernel`：MIX_AIC_1_1/1_2 分支先编 `output_cube.o` 再编 `output_vec.o`，最后用 `ld.lld` 链接成一个文件；其余分支只编一个 `.o`。
3. **需要观察的现象**：纯代码阅读，无运行现象。
4. **预期结果**：`MIX_AIC_1_2` → 2 次编译 + 1 次链接；`AIV_ONLY` → 1 次编译 + 1 次链接（该分支链接命令的输入输出是同一个 `dst` 文件，见 [python/asc/runtime/compiler.py:L320-L324](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L320-L324)）。此实践为源码阅读型，结论可直接从代码得出。

#### 4.2.5 小练习与答案

**练习 1**：`CompiledKernel.core_type` 有哪三种取值，分别对应哪些 `KernelType`？

**答案**：见 [python/asc/runtime/compiler.py:L201-L208](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L201-L208)：MIX_AIC_1_1/1_2 → `CoreType.AiCore`；AIV_ONLY、MIX_AIV_HARD_SYNC、MIX_AIV_1_0 → `CoreType.VectorCore`；其余（AIC_ONLY 等）→ `CoreType.CubeCore`。core_type 随 `CompiledKernel` 传给 Launcher 决定下发到哪类核。

**练习 2**：为什么 `CompilationTarget.get` 里 910B 和 910_93 两个平台共用 `arch = "c220"`？

**答案**：见 [python/asc/runtime/compiler.py:L66-L67](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L66-L67)，两个平台的 if 分支都把 arch 赋为 `"c220"`，即两者在毕昇编译器里对应同一代 AI Core 架构（dav-c220），差别由其他编译选项体现；pyasc 目前也只支持这两个平台族（[python/asc/runtime/compiler.py:L61](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L61) 的 if 条件外的平台会直接抛 RuntimeError）。

### 4.3 Compiler.run 与 run_passes：主流程与 dump 时机

#### 4.3.1 概念说明

`Compiler.run` 是编译器的总入口，被 `@final` 装饰（[python/asc/runtime/compiler.py:L162](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L162)）——子类不允许覆写它，只能覆写 `run_passes`、`_schedule_*` 等内部步骤。这保证了「dump 顺序、翻译顺序」这条主干对所有 Compiler 子类都稳定不变，是 u3-l1 讲过的「组合式扩展点」设计在编译器侧的延续。

四个 dump 产物与主流程的对应关系是理解本讲的钥匙：

| 产物 | 落盘时机 | 内容 |
| --- | --- | --- |
| `codegen.mlir` | `run_passes` **之前** | FunctionVisitor 刚生成的「前端原始 IR」 |
| `ascir.mlir` | `run_passes` **之后**、翻译之前 | 跑完全部 Pass 的「后端整形 IR」 |
| `ascendc.cpp` | 翻译成 Ascend C 之后（可能注入 dump 代码） | 最终交给毕昇的 C++ 源码 |
| `binary.o` | `run_compilation` 内部 | 毕昇编出的 Kernel 二进制 |

#### 4.3.2 核心流程

`run` 的五步（省略号为本讲不展开的部分）：

```
run(mod, func_name):
    1. dump codegen.mlir            # Pass 前
    2. if run_passes: run_passes(mod)  # 三阶段 Pass 流水线
    3. dump ascir.mlir               # Pass 后
    4. source = run_translation(mod) # IR → Ascend C 文本
       (若 enable_debug: 注入 InitDump 打印代码)
       dump ascendc.cpp
    5. run_compilation(source, ...)  # 毕昇编译 → CompiledKernel（内部 dump binary.o）
```

`run_passes` 的八步：

```
run_passes(mod):
    1. 创建 PassManager（绑定当前 MLIR Context）
    2. pm.enable_verifier()                  # 每个 Pass 后校验 IR 合法性
    3. if print_ir_before_all: pm.enable_printing()
    4. if insert_sync is None: insert_sync = mod.need_insert_sync()  # 自动判定
    5. _schedule_passes(pm)                   # 装填三阶段所有 Pass
    6. pm.run(mod)                            # 真正执行
    7. if kernel_type is None: 读 IR 属性推导 kernel_type
    8. 读 IR 属性 + 环境变量推导 enable_debug
```

注意第 6 步与第 7 步的顺序：**kernel_type 的推导发生在 Pass 全部跑完之后**，因为推导依据的 `asc.compile_mix` 属性本身就是流水线里 DetectKernelTypePass 打上去的（详见 4.5 节）。

#### 4.3.3 源码精读

主入口：

```python
@final
def run(self, mod: ir.ModuleOp, func_name: str) -> CompiledKernel:
    utils.FileUtils.dump_file(self.dump_dir, "codegen.mlir", str(mod))
    if self.options.run_passes:
        self.run_passes(mod)
    utils.FileUtils.dump_file(self.dump_dir, "ascir.mlir", str(mod))
    source = self.run_translation(mod)
    if self.enable_debug:
        source = self._gen_init_dump_code(source, func_name)
    utils.FileUtils.dump_file(self.dump_dir, "ascendc.cpp", source)
    kernel_args = ir.get_kernel_arg_attrs(mod)
    return self.run_compilation(source, kernel_args)
```

见 [python/asc/runtime/compiler.py:L162-L173](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L162-L173)。三个 `dump_file` 调用点就是三份文本产物的精确落盘位置。dump 的实现很朴素：目录为 `None` 直接返回，否则写字符串到文件（[python/asc/runtime/utils.py:L22-L35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/utils.py#L22-L35)）——所以「没设 `PYASC_DUMP_PATH` 就什么都不落盘，也不报错」。

Pass 执行主体：

```python
def run_passes(self, mod: ir.ModuleOp) -> None:
    pm = passes.PassManager(mod.get_context())
    pm.enable_verifier()
    if self.options.print_ir_before_all:
        pm.enable_printing()
    if self.options.insert_sync is None:
        self.options.insert_sync = mod.need_insert_sync()
    self._schedule_passes(pm)
    pm.run(mod)
    ...
```

见 [python/asc/runtime/compiler.py:L175-L183](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L175-L183)。`enable_verifier` 让 PassManager 在每个 Pass 之后校验 IR 不变量，坏 Pass 会在第一时间暴露；`enable_printing` 对应 pybind 侧的实现其实「Pass 前和 Pass 后都打印」（见 [python/src/Passes.cpp:L62-L74](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Passes.cpp#L62-L74)，两个回调都返回 `true`，输出走 `llvm::errs()` 即 stderr）——选项名沿用了 MLIR `--mlir-print-ir-before-all` 的叫法，实际信息量比名字更大。

PassManager 抛错的方式值得知道：`pm.run` 的 pybind 包装在 Pass 失败时抛 `std::runtime_error("Failed to run passes")`（[python/src/Passes.cpp:L52-L59](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Passes.cpp#L52-L59)），Python 侧会看到 RuntimeError。

#### 4.3.4 代码实践

1. **实践目标**：验证「`run_passes=False` 时两份 mlir 完全相同」，从反面理解 Pass 流水线做了什么。
2. **操作步骤**：
   - `export PYASC_DUMP_PATH=/tmp/pyasc_dump_a`，正常运行 `examples/01_add/add.py -r Model`，保留四份产物。
   - 把内核调用改成传 `run_passes=False`（同样在调用处加关键字参数，示例代码），设 `PYASC_DUMP_PATH=/tmp/pyasc_dump_b` 重跑。
   - 执行 `diff /tmp/pyasc_dump_a/codegen.mlir /tmp/pyasc_dump_b/codegen.mlir` 与 `diff /tmp/pyasc_dump_b/codegen.mlir /tmp/pyasc_dump_b/ascir.mlir`。
3. **需要观察的现象**：第二个 diff 应无任何输出（同一个 `mod` 没被改写就 dump 了两次）；第一个 diff 展示的正是 Pass 流水线的全部改写量。此外 `run_passes=False` 时 `kernel_type` 不会被推导（保持 `None`）。
4. **预期结果**：`codegen.mlir` 两份应当一致（同一个示例、同一套选项，仅 `run_passes` 不同——注意它也在缓存 key 里，所以会触发重新编译）。`run_passes=False` 下能否顺利完成后续 Ascend C 翻译与毕昇编译属于「待本地验证」——从未整形的 IR 直接翻译可能失败或产出不完整代码；即便编译通过，`CompilationTarget.get(None, ...)` 会落入最后的 else 分支按 cube 架构处理（[python/asc/runtime/compiler.py:L74](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L74)），这通常不是向量算子想要的。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `codegen.mlir` 的 dump 写在 `if self.options.run_passes` 判断之前？

**答案**：见 [python/asc/runtime/compiler.py:L164-L167](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L164-L167)。dump 前端原始 IR 与「跑不跑 Pass」无关——即使 `run_passes=False`，用户也应能导出 codegen 阶段产物做排查；如果 dump 放在判断之内，关闭 Pass 时就丢失了唯一一份 IR 记录。

**练习 2**：`ir.get_kernel_arg_attrs(mod)` 在 `run` 里于翻译之后调用，它取的是什么？

**答案**：见 [python/asc/runtime/compiler.py:L172](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L172) 与 [python/src/IR.cpp:L646-L656](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L646-L656)：它从（已被 LegalizeKernelArgs Pass 处理过的）模块上提取 Kernel 参数属性列表（`Explicit`/`FftsAddr` 等 `KernelArgument` 枚举），随 `CompiledKernel` 传给 Launcher，用于决定下发时参数的排布。这也是「必须先跑完 Pass 再取」的又一处顺序依赖。

### 4.4 三阶段 Pass 调度：lowering、optimizing、postprocessing

#### 4.4.1 概念说明

pyasc 的 Pass 流水线分三个阶段，由 `_schedule_passes` 依次装填（[python/asc/runtime/compiler.py:L232-L235](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L232-L235)）：

- **lowering（降级整形）**：把前端「面向人」的 IR 改写成「面向硬件」的 IR。前端允许惰性创建 Tensor、允许调用 Device 子函数，这一阶段把它们落实成真实的 UB 内存分配与单函数体。
- **optimizing（通用优化）**：先跑 MLIR 通用优化（循环不变量外提、常量传播等）；若判定需要重建同步，再执行 pyasc 特色的同步重建链。
- **postprocessing（收尾）**：为翻译成 Ascend C 做最后准备——生成 include/样板代码、合法化 Kernel 参数、检测核类型与 debug 标记。

Pass 分两个来源：`passes.common.*` 是 MLIR 自带通用 Pass 的转发；`passes.ascendc.*` 是 pyasc 在 `Passes.td` 里声明的自定义 Pass。两者都经 [python/src/Passes.cpp:L91-L111](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Passes.cpp#L91-L111) 注册成 Python 函数。

#### 4.4.2 核心流程

三个调度函数的调用关系与执行顺序（一行一个 Pass，按添加顺序执行）：

```
_schedule_passes(pm)
 ├─ _schedule_lowering(pm)          # 静态方法
 │   privatize_func → inliner → symbol_dce → canonicalizer
 │   → reconcile_unrealized_casts → input_output_tensor
 │   → hoist_ub_allocation → materialize_tensor → unify_pipe
 │   → canonicalizer → cse
 ├─ _schedule_optimizing(pm)        # 实例方法（读 options）
 │   licm → sccp → canonicalizer
 │   [若 insert_sync]:
 │     erase_sync → hoist_que_bind → insert_sync → unify_pipe → canonicalizer
 └─ _schedule_postprocessing(pm)    # 实例方法（读 options）
     declare_py_struct → generate_boilerplate
     [若 matmul_cube_only]: define_cube_only
     → legalize_kernel_args → detect_kernel_type → detect_enable_debug
     [若 verify_sync]: verify_sync
     [若 strip_loc]: strip_debug_info
```

方括号表示条件装填——「跑哪些 Pass」完全由 `CompileOptions` 决定，这就是选项袋与调度函数的连接方式。

#### 4.4.3 源码精读

lowering 阶段：

```python
@staticmethod
def _schedule_lowering(pm: passes.PassManager) -> None:
    passes.ascendc.add_privatize_func(pm)
    passes.common.add_inliner(pm)
    passes.common.add_symbol_dce(pm)
    passes.common.add_canonicalizer(pm)
    passes.common.add_reconcile_unrealized_casts(pm)
    passes.ascendc.add_input_output_tensor(pm)
    passes.ascendc.add_hoist_ub_allocation(pm)
    passes.ascendc.add_materialize_tensor(pm)
    passes.ascendc.add_unify_pipe(pm)
    passes.common.add_canonicalizer(pm)
    passes.common.add_cse(pm)
```

见 [python/asc/runtime/compiler.py:L119-L131](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L119-L131)。与 Passes.td 声明对照（td 里的 `summary` 是官方一句话说明）：

| 顺序 | Python 调用 | td 注册名（作用层级） | 作用（摘自 Passes.td summary） |
| --- | --- | --- | --- |
| 1 | `ascendc.add_privatize_func` | `ascendc-privatize-func`（ModuleOp） | 把无 `ascendc.global` 属性的函数标记为 private |
| 2 | `common.add_inliner` | MLIR 内置 | 函数内联（Device 子函数并入 Kernel） |
| 3 | `common.add_symbol_dce` | MLIR 内置 | 死符号消除 |
| 4 | `common.add_canonicalizer` | MLIR 内置 | 模式规范化 |
| 5 | `common.add_reconcile_unrealized_casts` | MLIR 内置 | 消除 `unrealized_conversion_cast` |
| 6 | `ascendc.add_input_output_tensor` | `ascendc-input-output-tensor`（FuncOp） | 为 `local_tensor_auto` 设置输入输出 operand |
| 7 | `ascendc.add_hoist_ub_allocation` | `ascendc-hoist-ub-allocation`（FuncOp） | 把 tensor 分配提升到函数根 |
| 8 | `ascendc.add_materialize_tensor` | `ascendc-materialize-tensor`（FuncOp） | 为 `local_tensor_auto` 插入 tbuf/queue/alloca |
| 9 | `ascendc.add_unify_pipe` | `ascendc-unify-pipe`（FuncOp） | 统一 pipe 操作 |
| 10 | `common.add_canonicalizer` | MLIR 内置 | 再次规范化 |
| 11 | `common.add_cse` | MLIR 内置 | 公共子表达式消除 |

td 声明示例（节选）：

```
def HoistUBAllocation : Pass<"ascendc-hoist-ub-allocation", "func::FuncOp"> {
  let summary = "Hoist tensor allocations to the function root";
  let constructor = "mlir::ascendc::createHoistUBAllocationPass();
}
```

见 [include/ascir/Dialect/Asc/Transforms/Passes.td:L49-L52](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L49-L52)（注意实际代码 constructor 一行末尾有 `"`，此处为排版省略）。`Pass<"名字", "作用Op">` 的第二个模板参数决定它是模块级还是函数级 Pass。

optimizing 阶段（唯一的「读选项」体现在 insert_sync 块）：

```python
def _schedule_optimizing(self, pm: passes.PassManager) -> None:
    passes.common.add_licm(pm)
    passes.common.add_sccp(pm)
    passes.common.add_canonicalizer(pm)
    if self.options.insert_sync:
        passes.ascendc.add_erase_sync(pm)
        passes.ascendc.add_hoist_que_bind(pm)
        passes.ascendc.add_insert_sync(pm)
        passes.ascendc.add_unify_pipe(pm)
        passes.common.add_canonicalizer(pm)
```

见 [python/asc/runtime/compiler.py:L133-L142](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L133-L142)。同步重建链三步的语义：**EraseSync**（`ascendc-erase-sync`，[Passes.td:L33-L36](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L33-L36)）先把用户手写的核内同步操作全部删掉；**HoistQueBind**（`ascendc-hoist-que-bind`，[Passes.td:L44-L47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L44-L47)）把 TQueBind/TQue/TBuf 初始化提升到统一位置；**InsertSync**（`ascendc-insert-sync`，[Passes.td:L59-L63](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L59-L63)）再按 API 依赖分析重新插入同步。「先删光再重建」而不是「打补丁」，是为了让同步完全由编译器推导，不受用户手写遗漏/错序影响。

postprocessing 阶段：

```python
def _schedule_postprocessing(self, pm: passes.PassManager) -> None:
    passes.ascendc.add_declare_py_struct(pm)
    passes.ascendc.add_generate_boilerplate(pm)
    if self.options.matmul_cube_only:
        passes.ascendc.add_define_cube_only(pm)
    passes.ascendc.add_legalize_kernel_args(pm)
    passes.ascendc.add_detect_kernel_type(pm)
    passes.ascendc.add_detect_enable_debug(pm)
    if self.options.verify_sync:
        passes.ascendc.add_verify_sync(pm)
    if self.options.strip_loc:
        passes.common.add_strip_debug_info(pm)
```

见 [python/asc/runtime/compiler.py:L219-L230](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L219-L230)。各 Pass 的 td 注册名：`ascendc-declare-py-struct`（[Passes.td:L16-L20](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L16-L20)）、`ascendc-generate-boilerplate`（[Passes.td:L38-L42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L38-L42)）、`ascendc-define-cube-only`（[Passes.td:L22-L26](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L22-L26)）、`ascendc-legalize-kernel-args`（[Passes.td:L71-L78](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L71-L78)）、`ascendc-detect-kernel-type`（[Passes.td:L28-L31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L28-L31)）、`ascendc-detect-enable-debug`（[Passes.td:L100-L103](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L100-L103)）、`ascendc-verify-sync`（[Passes.td:L95-L98](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L95-L98)）。

两个容易忽略的细节：

1. td 里还声明了一个 `Noop` Pass（`ascendc-noop`，[Passes.td:L80-L83](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L80-L83)），也注册成了 `add_noop_pass`（[python/src/Passes.cpp:L95](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Passes.cpp#L95)），但 `_schedule_*` 三个函数都没有调度它——它是空操作，仅供开发调试流水线时占位。
2. 模块级与函数级的注册差异在 [python/src/Passes.cpp:L27-L30](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Passes.cpp#L27-L30) 的两个宏：`DEFINE_ADD_PASS` 直接 `addPass`（模块级），`DEFINE_ADD_PASS_ON(func::FuncOp, ...)` 走 `addNestedPass`（对每个函数各跑一遍）。对照 Passes.td 第二个模板参数即可判断任何 Pass 的作用层级。

#### 4.4.4 代码实践

这是本讲的主实践（对应任务书）。

1. **实践目标**：对比 Pass 前后的 IR，亲眼确认至少 2 处被 Pass 改写的结构；再用逐 Pass 打印观察中间过程。
2. **操作步骤**：
   - `mkdir -p /tmp/pyasc_dump && export PYASC_DUMP_PATH=/tmp/pyasc_dump`。
   - 运行 `python3 examples/02_add_framework/add_framework.py -r Model`。
   - 打开 `/tmp/pyasc_dump/codegen.mlir` 与 `/tmp/pyasc_dump/ascir.mlir`，执行 `diff` 或并排阅读。
   - 重点搜索以下三类结构（用 grep）：
     - `LocalTensorAuto` / `local_tensor_auto`：MaterializeTensor 的输入，Pass 后应被改写为 tbuf/queue/alloca 等真实分配（td summary：「Insert ascendc.tbuf, ascendc.queue and ascendc.alloca for local_tensor_auto」）。注意 02 示例用的是 TQue 框架风格，此结构可能本就不存在——可换 `python/test/kernels/insert_sync/test_vadd.py` 观察。
     - 循环体内的 tensor/分配操作：HoistUBAllocation 应把分配提升到函数入口（td summary：「Hoist tensor allocations to the function root」）。
     - `emitc.include` / 样板代码：GenerateBoilerplate 的产物，Pass 前的 codegen.mlir 里没有，Pass 后的 ascir.mlir 里应有。
   - 第二轮：把内核调用加上 `print_ir_before_all=True` 重跑（记得它会改变缓存 key、必然重新编译），并把 stderr 重定向到文件：`python3 add_framework.py -r Model 2> /tmp/passes.log`。
3. **需要观察的现象**：
   - diff 中出现上述至少 2 类结构变化（例如分配位置移动、新增性质完全不同的操作）。
   - `/tmp/passes.log` 中每个 Pass 前后各打印一次完整 IR，能数出 lowering 11 个 + optimizing 3 个（02 示例不触发 insert_sync 链）+ postprocessing 6 个（不带 matmul_cube_only/verify_sync/strip_loc）Pass 的边界。
4. **预期结果**：两份 mlir 有实质差异、passes.log 按 Pass 分段输出 IR。具体的 IR 操作名与行数属于「待本地验证」——请以 dump 出的真实文件为准，并把你看到的操作名与 4.4.3 表格中的 Pass 一一对应记录。

#### 4.4.5 小练习与答案

**练习 1**：02_add_framework 用的是 TQue 框架风格（队列内置同步），没有手写 set_flag/wait_flag，也没有 `LocalTensorAuto`。它跑 optimizing 阶段时会执行同步重建链吗？

**答案**：不会。`run_passes` 里 `insert_sync` 默认 `None`，先由 `mod.need_insert_sync()` 判定——该函数遍历模块找 `LocalTensorAutoOp`（[python/src/IR.cpp:L569-L574](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L569-L574)），02 示例的 TQue 路径生成的是普通 LocalTensor（alloc_tensor 的实现见 [python/asc/language/fwk/tpipe.py:L63-L79](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L63-L79)），因此判定为 False，`_schedule_optimizing` 的 if 块整段跳过，只跑 licm/sccp/canonicalizer 三个通用优化。

**练习 2**：为什么 `verify_sync` 和 `strip_loc` 放在 postprocessing 的最后，而不是和同步相关的 erase/insert 放在一起？

**答案**：`verify_sync` 校验的是**最终形态**的 TQue 同步是否正确——必须等同步重建链（optimizing 阶段）和所有后续改写都完成后再校验才有意义，所以它在 postprocessing 尾部（[python/asc/runtime/compiler.py:L227-L228](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L227-L228)）；`strip_loc` 去除调试位置信息，属于「发射前清理」，放在最后可以避免影响前面任何 Pass 的诊断信息（[python/asc/runtime/compiler.py:L229-L230](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L229-L230)）。

**练习 3**：`_schedule_lowering` 是 `@staticmethod`，`_schedule_optimizing` 和 `_schedule_postprocessing` 却是实例方法，为什么？

**答案**：lowering 阶段的 Pass 列表是**无条件固定**的，不依赖任何选项；后两个阶段的装填要读 `self.options.insert_sync`、`self.options.matmul_cube_only`、`self.options.verify_sync`、`self.options.strip_loc`（[python/asc/runtime/compiler.py:L137](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L137)、[L222](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L222)、[L227-L230](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L227-L230)），必须访问实例状态。这个签名差异本身就是「哪个阶段受选项影响」的代码级提示。

### 4.5 两个自动判定：need_insert_sync 与 kernel_type/enable_debug 推导

#### 4.5.1 概念说明

这一节讲 `run_passes` 里两处「框架替用户做决定」的逻辑，它们共同的特点是：**判定依据不是 Python 侧信息，而是 Pass 流水线写到 IR 上的属性**。这形成了一条「后端 Pass → IR 属性 → 前端选项」的回传通道：

- 同步重建与否：由 IR 里**是否存在 `LocalTensorAutoOp`** 决定（`LocalTensorAuto` 是惰性 Tensor 风格——用户只声明形状，不管内存与同步）。
- 核类型：由 IR 里**是否存在 `asc.compile_mix` 属性**决定，而这个属性是 postprocessing 阶段的 DetectKernelTypePass 打上去的（它检测 Matmul 对象注册操作 `RegistMatmulObjOp`）。
- debug 开关：由 `asc.enable_debug` 属性（DetectEnableDebugPass 检测 kernel 是否用了 debug 工具）**且** `ASCENDC_DUMP` 环境变量为 true 共同决定。

#### 4.5.2 核心流程

```
run_passes 尾部（pm.run 之后）：

if kernel_type is None:
    if mod 有 "asc.compile_mix" 属性:            # DetectKernelTypePass 打的
        kernel_type = AIC_ONLY   (若 matmul_cube_only=True)
        kernel_type = MIX_AIC_1_2 (否则)
    else:
        kernel_type = AIV_ONLY

enable_debug = mod 有 "asc.enable_debug" 属性     # DetectEnableDebugPass 打的
               且 环境变量 ASCENDC_DUMP(默认"True") 小写=="true"
```

时序要点：`pm.run(mod)` 在前、属性判定在后，且打属性的 Pass（detect_kernel_type、detect_enable_debug）位于 postprocessing 阶段——所以「推导」发生在流水线跑完之后，本质是**读取流水线的产出物**。判定结果直接写回 `self.options.kernel_type`，随后被 `run_compilation` → `CompilationTarget.get` → `_gen_dst_kernel` 消费。

#### 4.5.3 源码精读

`run_passes` 的推导段：

```python
pm.run(mod)
if self.options.kernel_type is None:
    if mod.op.has_unit_attr("asc.compile_mix"):
        self.options.kernel_type = KernelType.AIC_ONLY if self.options.matmul_cube_only else\
                                   KernelType.MIX_AIC_1_2
    else:
        self.options.kernel_type = KernelType.AIV_ONLY
self.enable_debug = mod.op.has_unit_attr("asc.enable_debug") and\
    str(os.environ.get("ASCENDC_DUMP", "True")).lower() == "true"
```

见 [python/asc/runtime/compiler.py:L183-L191](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L183-L191)。两条规则：

- **用户显式指定的 kernel_type 永远优先**（只有 `None` 才推导）。
- 推导只有三分支：用了 Matmul → 混合核（MIX_AIC_1_2），纯 Cube 需求再由 `matmul_cube_only` 细分为 AIC_ONLY；没用 Matmul → AIV_ONLY。

打属性的 Pass 实现（整个文件的核心就这几行）：

```cpp
void runOnOperation() override
{
    ModuleOp op = getOperation();
    if (op.walk([](ascendc::RegistMatmulObjOp) { return WalkResult::interrupt(); }).wasInterrupted())
        op->setAttr(attr::compile_mix, UnitAttr::get(op->getContext()));
}
```

见 [lib/Dialect/Asc/Transforms/DetectKernelTypePass.cpp:L31-L39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/DetectKernelTypePass.cpp#L31-L39)。`walk` 遍历模块所有操作，一旦遇到 Matmul 对象注册操作（`RegistMatmulObjOp`，由前端 `register_matmul` 生成）就中断并给模块挂 `asc.compile_mix` 单元属性。**Python 侧不分析任何 AST，只认这个属性**——前后端通过 IR 属性解耦。

`need_insert_sync` 的实现：

```cpp
"need_insert_sync",
[](ModuleOp& self) {
    auto result = self.walk([](ascendc::LocalTensorAutoOp) { return WalkResult::interrupt(); });
    return result.wasInterrupted();
})
```

见 [python/src/IR.cpp:L569-L574](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L569-L574)。同样是 walk + 中断的模式：模块里只要有一个 `LocalTensorAutoOp` 就返回 True。`LocalTensorAutoOp` 来自前端的惰性 Tensor 构造（`asc.LocalTensorAuto(dtype, shape)` 直接创建 IR 操作，见 [python/asc/language/core/tensor.py:L470-L482](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L470-L482)）。项目甚至留了一个活注释作证，[python/test/kernels/insert_sync/test_vadd.py:L16](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/kernels/insert_sync/test_vadd.py#L16) 写着：

```python
@asc.jit  # insert_sync=True will be added implicitly because asc.LocalTensorAuto is used in the kernel
```

#### 4.5.4 代码实践

1. **实践目标**：用两个对照示例验证 `need_insert_sync` 的判定差异，并追踪一次 `kernel_type` 推导的完整证据链。
2. **操作步骤**：
   - 运行 `python/test/kernels/insert_sync/test_vadd.py`（设 `PYASC_DUMP_PATH=/tmp/dump_auto`）：其 kernel 用 `asc.LocalTensorAuto(...)` 创建张量。
   - 运行 `examples/02_add_framework/add_framework.py`（设 `PYASC_DUMP_PATH=/tmp/dump_tque`）：其 kernel 用 TQue 框架。
   - 分别在两份 `codegen.mlir` 里 grep `LocalTensorAuto`，在两份 `ascir.mlir` 里 grep 同步相关操作（如 set_flag/wait_flag 对应的 IR 操作名）。
   - 证据链追踪：阅读 [python/test/kernels/insert_sync/test_matmul.py:L44](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/kernels/insert_sync/test_matmul.py#L44)（用了 `LocalTensorAuto` 的 Matmul 算子），沿 `register_matmul` → `RegistMatmulObjOp` → DetectKernelTypePass → `asc.compile_mix` → `KernelType.MIX_AIC_1_2` 的链条在源码中走一遍。
3. **需要观察的现象**：
   - `/tmp/dump_auto/codegen.mlir` 中能找到 `LocalTensorAuto` 相关操作；`/tmp/dump_tque/codegen.mlir` 中找不到。
   - `/tmp/dump_auto/ascir.mlir` 相对其 codegen.mlir 出现了同步操作的增删（erase+insert 的净效果）；`/tmp/dump_tque` 两份之间同步结构基本不变。
4. **预期结果**：两个示例都数值校验通过；IR 差异与上述判定一致。具体 IR 操作的拼写以 dump 文件为准，「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：如果用户在 `@asc.jit(kernel_type=KernelType.AIV_ONLY)` 里显式指定了核类型，但 kernel 里用了 Matmul，会发生什么？

**答案**：显式指定优先——`run_passes` 里只有 `kernel_type is None` 才走推导分支（[python/asc/runtime/compiler.py:L184](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L184)），DetectKernelTypePass 仍会给模块打 `asc.compile_mix` 属性，但没人去读它，最终按 AIV_ONLY 的 vec 架构编译。这种「声明与用法矛盾」在 Python 侧没有拦截，编译或运行阶段是否报错「待确认」。

**练习 2**：`ASCENDC_DUMP` 环境变量默认值是 `"True"`，为什么还要做 `.lower() == "true"` 这个比较？

**答案**：见 [python/asc/runtime/compiler.py:L190-L191](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L190-L191)。它让用户可以用任意大小写（`true`/`TRUE`/`True`）关闭或开启该开关，同时默认值字符串 `"True"` 本身也通过小写比较被归一化为真——即「不设环境变量 = 允许 debug 注入」，只有显式设成 false 类值才禁用。

## 5. 综合实践

**任务：给 02_add_framework 做一份「Pass 流水线观察报告」。**

把本讲知识串成一次完整操作：

1. **准备**：

   ```bash
   mkdir -p /tmp/report_tque /tmp/report_log
   export PYASC_DUMP_PATH=/tmp/report_tque
   python3 examples/02_add_framework/add_framework.py -r Model
   ```

2. **第一部分——静态对比**：diff `codegen.mlir` 与 `ascir.mlir`，在报告中填一张三列表：

   | 被改写的结构 | codegen.mlir 中的样子 | ascir.mlir 中的样子 | 归属 Pass（对照 4.4.3 表格） |
   | --- | --- | --- | --- |
   | （至少 2 行，例如函数符号可见性、分配位置、新增样板操作） | | | |

3. **第二部分——逐 Pass 观察**：

   ```bash
   export PYASC_DUMP_PATH=/tmp/report_log
   python3 add_framework.py -r Model -v Ascend910B1 2> /tmp/passes.log    # 调用内核处加 print_ir_before_all=True
   ```

   在 `/tmp/passes.log` 里数出 IR 打印的分段数，验证与 4.4.2 的装填清单一致（本示例应为 lowering 11 段 + optimizing 3 段 + postprocessing 6 段，insert_sync 链不触发），并记录每段对应的 Pass 名。

4. **第三部分——回传通道验证**：在两份 mlir 里分别 grep `compile_mix` 与 `enable_debug`，说明它们为什么出现在 ascir.mlir 而不是 codegen.mlir（提示：谁打的属性、Python 侧谁在读）。

5. **预期结果**：一份包含 IR 摘录、Pass 对照表、分段计数的报告；所有 IR 具体内容以本机 dump 为准（本讲文中给出的操作名均需「待本地验证」后落入报告）。

## 6. 本讲小结

- `CompileOptions` 是一次性编译选项袋：字段分「影响 Pass 调度」「影响毕昇命令」「影响缓存」三类，且**全部字段进文件缓存 key**；`opt_level` 代码里只允许 1/2/3（文档写 0~3，以代码为准）。
- `Compiler.run`（`@final`，不可覆写）固定五步：dump codegen.mlir → run_passes → dump ascir.mlir → 翻译并 dump ascendc.cpp → run_compilation（内部 dump binary.o）。
- Pass 流水线三阶段：lowering（11 个 Pass，把前端惰性 IR 整形为硬件形态）→ optimizing（3 个通用优化 + 条件触发的 EraseSync→HoistQueBind→InsertSync→UnifyPipe 同步重建链）→ postprocessing（样板生成、参数合法化、属性检测等 6+3 个 Pass）。
- 自定义 Pass 在 `Passes.td` 声明（名字 + 作用层级 + summary）、`Passes.cpp` 注册成 `passes.ascendc.add_*`、`compiler.py` 调度——三处文件一一对应，是排查「某个变换是谁做的」的检索链。
- `insert_sync` 三态（None 自动 / True 强制 / False 跳过），自动判据是 IR 里存在 `LocalTensorAutoOp`；`kernel_type` 为 None 时按 `asc.compile_mix` 属性推导为 MIX_AIC_1_2 / AIC_ONLY / AIV_ONLY，属性由 DetectKernelTypePass 在流水线内打上、Python 侧在 `pm.run` 之后读取——后端通过 IR 属性向前端回传信息。

## 7. 下一步学习建议

- **u3-l5（面向硬件的编译）**：接着读 `CompilationTarget` 与 `_gen_dst_kernel/_get_compiler_cmd` 的下半部分——本讲只回答「kernel_type 从哪来」，下一讲回答「kernel_type 怎么变成毕昇命令行与 `.o` 文件」。
- **u3-l6（Launcher）**：跟踪 `CompiledKernel`（binary、core_type、kernel_args）如何被 Launcher 消费下发。
- **提前浏览 u6-l1 的 Pass 全景**：本讲从「调度者」视角数 Pass，u6 将从「实现者」视角逐个精读 `lib/Dialect/Asc/Transforms/` 下的 cpp；读完本讲再去读 MaterializeTensor.cpp 会非常有方向感。
- 动手向：用 `python/src/Passes.cpp:L43-L51` 暴露的 `get_pipeline_str()`（需自写小脚本构造 PassManager），打印流水线的文本形式，与 4.4.2 的伪代码互相印证。
