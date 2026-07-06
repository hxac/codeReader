# tileiras 编译器调用与 cubin 生成

## 1. 本讲目标

上一讲（u7-l2）我们停留在「`.tileirbc` 字节码文件长什么样」——magic 头、各 section、类型表、版本门控。但字节码本身并不能在 GPU 上跑，它只是 cuTile 与外部编译器 `tileiras` 之间的**中间产物契约**。本讲要回答最后一跳的问题：**这些字节是怎么被送进 `tileiras`、被翻译成 GPU 可执行的 cubin 的？cuTile 又是怎么在形形色色的机器上找到那个 `tileiras` 可执行文件的？**

读完本讲，你应当能够：

1. 说清 `_find_compiler_bin` 的**四级查找顺序**（pip 包 → `PATH` → `CUDA_HOME` → 默认 CTK 安装路径），并解释每级查找返回的 `_CompilerBinary` 里那个 `pass_cuda_home_var` 标志的作用——也就是「为什么 pip 找到的 tileiras 要把 `CUDA_HOME` 从子进程环境里删掉」。
2. 读懂 `compile_cubin` 如何把「字节码文件路径 + `CompilerOptions` + `sm_arch` + 超时」拼成一条 `tileiras` 命令行，掌握 `--gpu-name` / `-O` / `--lineinfo` / `--device-debug` 四类参数的来源与触发条件，并理解 `subprocess.run` 失败/超时如何被翻译成 `TileCompilerExecutionError` / `TileCompilerTimeoutError`。
3. 解释 `get_sm_arch` 如何通过 C++ 扩展调用 CUDA 驱动拿到当前设备的 compute capability、拼成 `sm_<major><minor>`，并理解它为什么被 `@cache`。
4. 理解 `_get_max_supported_bytecode_version` 是 cuTile 进程里对 `tileiras` 的**第一次真实调用**——它用「空字节码探针」从高到低试版本，既是版本探测，也是 `compile_cubin` 这套子进程机制的一次「热身」。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **compile_tile 流水线全貌**（u5-l2）：`compile_tile` 是「线性向下 + 三处短路 return」的流水线，后端那一跳就是「IR → 字节码 → cubin」。本讲聚焦其中「字节码 → cubin」这一跳，也就是 [`_compile.py:541-560`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L541-L560) 那段「写临时文件 → `compile_cubin` → 读回 cubin」的代码。
- **字节码文件格式与 BytecodeVersion**（u7-l2）：字节码文件以 magic `\x7fTileIR\x00` 开头、内嵌版本号；`BytecodeVersion` 用 `major*10000+minor*100+tag` 打包成 `IntEnum`，使「数值大小 == 版本新旧」。本讲会用到 `V_13_1/2/3` 这套版本枚举，并解释 cuTile 如何在运行时**探测**当前工具链支持到哪个版本。
- **`@cache` 与 `@global_compiler_lock`**：`_find_compiler_bin`、`get_sm_arch`、`_get_max_supported_bytecode_version`、`_get_compiler_version_string` 都被 `functools.cache` 装饰——进程内只解析一次。`compile_tile` 被 `global_compiler_lock` 包裹，保证整个编译流水线（含子进程调用）线程安全。
- **`CompilerOptions` 与 `ByTarget`**（u5-l1）：编译旋钮 `opt_level` 可以是一个「按架构取不同值」的 `ByTarget[int]`，`opt_level_for_target("sm_100")` 负责解析出当前架构下的具体值。本讲会看到它如何流入 `tileiras` 的 `-O` 参数。

一个关键直觉先建立起来：**cuTile 自己不会把 Tile IR 翻译成机器码**。生成 cubin 这件事完全外包给一个名为 `tileiras` 的外部可执行文件（CUDA Toolkit 的一部分，也可作为 pip 包安装）。cuTile 在这里的职责只是三件：① 找到它；② 把字节码写进临时文件、拼好命令行；③ 调用它、读回 cubin、把错误翻译成 Python 异常。本讲就是围绕这三件事展开。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `src/cuda/tile/_compile.py` | **本讲主战场**：`compile_cubin`（命令行拼装与子进程调用）、`_find_compiler_bin` / `_find_pip_tileiras` / `_find_compiler_in_default_cuda_toolkit_paths` / `_get_default_cuda_toolkit_paths`（四级查找）、`_CompilerBinary.run`（统一子进程执行器）、`_tileiras_effective_opt_and_device_debug`（lineinfo/device-debug 决策）、`get_sm_arch`（架构探测）、`_get_max_supported_bytecode_version`（版本探针）、`_try_get_compiler_version` / `_get_compiler_version_string`（版本字符串）、`_get_cuda_home` / `is_windows`。 |
| `src/cuda/tile/_load_libcuda.py` | 加载 `libcuda.so.1` / `nvcuda.dll`，拿到 `cuGetProcAddress_v2` 入口——`get_compute_capability`（进而 `get_sm_arch`）最终经它落到 CUDA 驱动 API。 |
| `src/cuda/tile/_compiler_options.py` | `CompilerOptions.opt_level_for_target`：把 `opt_level`（可能是 `ByTarget`）解析为当前架构的具体整数，喂给 `tileiras` 的 `-O`。 |
| `src/cuda/tile/_exception.py` | `TileCompilerExecutionError` / `TileCompilerTimeoutError` 与 `_parse_tileir_stderr`（把 tileiras 的 stderr 解析成带行号的可读错误）。 |
| `src/cuda/tile/_context.py` | `CUDA_TILE_COMPILER_TIMEOUT_SEC` 解析（`get_compile_timeout_from_env`）、`compiler_timeout` 上下文管理器、`TileContextConfig.compiler_timeout_sec`。 |
| `src/cuda/tile/_debug.py` | `EXPERIMENTAL_CUDA_TILE_DEBUG_BUILD` 环境变量读取——`--device-debug` 与 `-O0` 的总开关。 |
| `src/cuda/tile/_bytecode/version.py` | `BytecodeVersion`（`V_13_1/2/3/4`），版本探针的候选集来源。 |

## 4. 核心概念与源码讲解

### 4.1 _find_compiler_bin：四级查找定位 tileiras

#### 4.1.1 概念说明

`tileiras` 不是一个 Python 模块，而是一个**独立的可执行文件**（CUDA Toolkit 里和 `nvcc` 并列的编译器）。这就带来一个现实问题：不同用户的机器上，`tileiras` 装在哪千差万别——有人用 `pip install cuda-tile[tileiras]` 把它装进虚拟环境的 `nvidia/cu13/bin/`，有人装了系统级 CUDA Toolkit 放在 `/usr/local/cuda/bin/`，还有人的 `tileiras` 就在 `PATH` 里。

`_find_compiler_bin` 就是 cuTile 的「寻路器」：它按固定的优先级顺序，一级一级地找 `tileiras`，找到就返回一个 `_CompilerBinary` 对象（里面除了路径，还带着「要不要把 `CUDA_HOME` 透传给子进程」这个关键标志）。整个函数被 `@cache` 装饰，**进程内只解析一次**，后续所有 `tileiras` 调用（编译、版本探测）都复用这一次的结论。

#### 4.1.2 核心流程

四级查找的顺序与各自的「环境变量策略」如下：

```text
_find_compiler_bin()  [@cache，进程内一次]
│
├─ ① pip 包：_find_pip_tileiras()
│     校验 nvidia-cuda-tileiras / nvidia-cuda-nvcc / nvidia-nvvm 三件套都装了且主次版本一致
│     → 在 nvidia.cu13 包目录下的 bin/ 里 which("tileiras")
│     → pass_cuda_home_var = False   ← 命中后 strip 掉 CUDA_HOME/CUDA_PATH
│
├─ ② PATH：shutil.which("tileiras")
│     → pass_cuda_home_var = True    ← 透传用户环境
│
├─ ③ CUDA_HOME：在 $CUDA_HOME/bin（Windows 用 $CUDA_PATH）下 which
│     → pass_cuda_home_var = True
│
├─ ④ 默认 CTK 安装路径：_find_compiler_in_default_cuda_toolkit_paths()
│     扫描 /usr/local/cuda* 或 C:\Program Files\...\CUDA\v*，取版本号最高者
│     → pass_cuda_home_var = False
│
└─ 全部失败 → FileNotFoundError（提示 pip install cuda-tile[tileiras] 或装系统 CTK 13.1+）
```

为什么 `pass_cuda_home_var` 在不同级别取值不同？因为 `tileiras` 内部还会去找 `ptxas`、`libnvvm` 等伙伴工具（见 quickstart 文档对依赖的说明），它靠 `CUDA_HOME`/`CUDA_PATH` 来定位它们。当 `tileiras` 来自 **pip 包**或**默认安装路径**时，cuTile 已经知道它「自带」了一套匹配的伙伴工具（就装在它的同级目录），此时若再透传一个可能指向**另一个版本**系统 CTK 的 `CUDA_HOME`，反而会让 `tileiras` 找错伙伴工具、引发版本不匹配。所以这两级命中后，`_CompilerBinary.run` 会**主动从子进程环境里删掉** `CUDA_HOME`/`CUDA_PATH`。反之，②③两级是用户自己用 `PATH`/`CUDA_HOME` 指明的，应当尊重用户环境，于是透传。

#### 4.1.3 源码精读

`_CompilerBinary` 这个 dataclass 同时承载「路径」与「环境策略」，`run` 是后续所有 `tileiras` 调用的统一执行器：

[`_compile.py:592-624` —— `_CompilerBinary` 与统一执行器 `run`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L592-L624) 注意 `run` 把 `command + flags` 拼成完整命令行、复制一份 `os.environ`、设 `LD_LIBRARY_PATH`/`PATH`、按 `pass_cuda_home_var` 决定是否 `pop` 掉 `CUDA_HOME`/`CUDA_PATH`，然后用 `subprocess.run(..., check=True, capture_output=True, timeout=...)` 同步执行。两个 `except` 分别把「非零退出」和「超时」翻译成 `TileCompilerExecutionError` 与 `TileCompilerTimeoutError`（详见 4.2）。

四级查找的主干（注意每一级 return 的最后一个参数就是 `pass_cuda_home_var`）：

[`_compile.py:675-710` —— `_find_compiler_bin` 四级查找](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L675-L710) ① 先 `_find_pip_tileiras()`（`False`）→ ② `shutil.which("tileiras")`（`True`）→ ③ `_get_cuda_home()` 下的 `bin`（`True`）→ ④ `_find_compiler_in_default_cuda_toolkit_paths()`（`False`）→ 全空则 `raise FileNotFoundError`，错误信息同时给出「pip 装」与「系统 CTK」两条出路。

第一级 pip 查找最复杂——它要先校验三件套版本一致，再定位 `nvidia.cu13` 包：

[`_compile.py:639-672` —— `_find_pip_tileiras` 校验三件套并定位 bin](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L639-L672) 它遍历 `_PIP_TILEIRAS_PACKAGES = ("nvidia-cuda-tileiras", "nvidia-cuda-nvcc", "nvidia-nvvm")`（见 [`_compile.py:627-631`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L627-L631)），任一缺失就 `return None`（回退到下一级）；再用 `_get_major_minor` 比较三者主次版本，不一致就发 warning 并回退；最后 `import nvidia.cu13 as cu13_pkg`，在其 `__path__[0]/bin` 下 `shutil.which("tileiras")`。这一整套对应 quickstart 里「`pip install cuda-tile[tileiras]` 会把 `nvidia-cuda-tileiras/nvidia-cuda-nvcc/nvidia-nvvm` 装进虚拟环境、且三者必须 major.minor 一致」的安装约束。

第四级默认 CTK 路径扫描，负责「用户既没装 pip 包、也没设 PATH/CUDA_HOME，但系统装了 CUDA Toolkit」的兜底：

[`_compile.py:751-782` —— 默认 CTK 路径扫描与版本排序](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L751-L782) `_get_default_cuda_toolkit_paths` 在 Linux 下扫 `/usr/local`（匹配 `cuda-13.2` 这样的目录名，并额外把裸 `cuda` 当作候选且优先级最低）、Windows 下扫 `C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA`；用正则解析主/次版本，**按版本号降序**返回，于是 `_find_compiler_in_default_cuda_toolkit_paths` 会优先挑版本最高的那份 `tileiras`。

最后，`CUDA_HOME` 本身的取值在 Windows 与 Linux 下变量名不同：

[`_compile.py:581-589` —— `is_windows` 与 `_get_cuda_home`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L581-L589) Windows 读 `CUDA_PATH`、其它系统读 `CUDA_HOME`。

#### 4.1.4 代码实践（环境验证型·待本地验证）

1. **目标**：在你「未安装 pip tileiras、只装了系统 CTK」的机器上，验证 cuTile 通过第 ②/③/④ 级之一找到了 `tileiras`，并看清它返回的 `pass_cuda_home_var`。
2. **步骤**：
   ```python
   # probe_find.py
   import cuda.tile._compile as c

   # _find_compiler_bin 被 @cache，先清缓存确保重新解析
   c._find_compiler_bin.cache_clear()
   c._get_compiler_version_string.cache_clear()

   binary = c._find_compiler_bin()
   print("tileiras path       :", binary.path)
   print("bin_path (PATH)     :", binary.bin_path)
   print("ld_path (LD_LIB)    :", binary.ld_path)
   print("pass_cuda_home_var  :", binary.pass_cuda_home_var)
   print("compiler version    :", c._get_compiler_version_string())
   ```
3. **观察**：`binary.path` 应指向 `tileiras` 真实位置（如 `/usr/local/cuda/bin/tileiras`）；`pass_cuda_home_var` 为 `True`（若由 `CUDA_HOME` 命中）或 `False`（若由默认 CTK 路径命中）。
4. **预期**：你能据 `pass_cuda_home_var` 反推出 cuTile 走的是四级里的哪一级——`True` ⇒ 第 ② 或 ③ 级，`False` ⇒ 第 ① 或 ④ 级。**待本地验证**：具体路径与布尔值取决于你的安装方式。
5. **延伸**：把 `which tileiras` 在 shell 里跑一遍，与本脚本输出对照；若临时 `unset CUDA_HOME` 后再跑，观察是否回退到默认 CTK 路径（`pass_cuda_home_var` 由 `True` 变 `False`）。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `_find_compiler_bin` 要被 `@cache`，而不是每次编译都重新查找？
  - **答**：`tileiras` 的位置在进程生命周期内不会变；且查找涉及 `importlib.metadata.version`、`import nvidia.cu13`、`shutil.which`、`os.listdir` 等带 I/O 的操作，每次编译都跑一遍既慢又可能因环境抖动得出不同结论。缓存保证「一次解析、全程复用」，也让 `_get_compiler_version_string`、`_get_max_supported_bytecode_version` 等同样 `@cache` 的函数共享同一份结论。
- **练习 2**：假设用户同时装了 pip 版 `tileiras`（13.2）和系统 CTK 的 `tileiras`（13.3，在 `PATH` 里）。`_find_compiler_bin` 会用哪个？为什么？
  - **答**：用 pip 版（13.2）。因为第 ① 级 pip 查找先于第 ② 级 `PATH` 查找，只要三件套齐全且版本一致就立即 return，根本不会落到 `PATH` 那级。这也解释了为何 pip 命中后要把 `CUDA_HOME` 从环境里删掉——防止系统 CTK 13.3 的路径污染 pip 自带的 13.2 工具链。

---

### 4.2 compile_cubin：拼装命令行并调用 tileiras

#### 4.2.1 概念说明

`compile_cubin` 是「字节码 → cubin」这一跳的入口。它的输入非常朴素：一个已经写好的 `.bytecode` 临时文件路径、一份 `CompilerOptions`、一个 `sm_arch` 字符串、一个超时秒数。它的输出也朴素：一个 `.cubin` 文件路径。

它真正的核心工作是**把 cuTile 侧的抽象旋钮翻译成 `tileiras` 的命令行参数**。`tileiras` 的命令行风格与 `nvcc` 类似：位置参数是输入文件、`-o` 指定输出、`--gpu-name` 指定目标架构、`-O` 指定优化等级，再附加调试信息相关的开关。`compile_cubin` 负责把这些参数算准、拼好，然后委托给 `_CompilerBinary.run`（4.1 讲过的统一执行器）真正跑起来。

#### 4.2.2 核心流程

```text
compile_cubin(fname_bytecode, compiler_options, sm_arch, timeout_sec)
│
├─ binary = _find_compiler_bin()              # 复用 4.1 的寻路结果
├─ fname_cubin = 把 .bytecode 后缀换成 .cubin
├─ (effective_opt, use_device_debug) =
│       _tileiras_effective_opt_and_device_debug(compiler_options, sm_arch)
│
├─ args   = [fname_bytecode, "-o", fname_cubin]
├─ flags  = ["--gpu-name", sm_arch, f"-O{effective_opt}"]
│           + (["--device-debug"] if use_device_debug else ["--lineinfo"])
│
├─ binary.run(args, flags, timeout_sec)       # subprocess.run(check, capture, timeout)
│       ├─ CalledProcessError  → TileCompilerExecutionError(解析 stderr)
│       └─ TimeoutExpired      → TileCompilerTimeoutError(建议减小 tile size)
│
└─ return fname_cubin
```

`-O` 与调试开关的决策集中在 `_tileiras_effective_opt_and_device_debug`，规则只有两条：

- **调试构建**：当 `EXPERIMENTAL_CUDA_TILE_DEBUG_BUILD=1`（见 [`_debug.py:16-18`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_debug.py#L16-L18)）时，强制 `(-O0, --device-debug)`。`--device-debug` 让 cubin 携带完整的设备端调试信息、可被 `cuda-dbg`/Nsight 单步调试，但代价是关掉所有优化（`-O0`），内核会显著变慢。
- **正常构建**：否则取 `(opt_level_for_target(sm_arch), --lineinfo)`。`--lineinfo` 只嵌入「源代码行号 → 机器码」的映射，用于 profiling 与栈回溯，**不关闭优化**，是生产默认值。`opt_level_for_target` 会把可能是 `ByTarget` 的 `opt_level` 解析成当前架构下的具体整数（默认 3）。

一个常被忽略但很重要的点：**调试构建会改变磁盘缓存键**。因为 `cache_key` 把 `effective_opt` 与 `device_debug` 都算进去了（见 [`_compile.py:531-535`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L531-L535)），所以同一个内核在普通构建与调试构建下会命中不同的缓存条目，绝不会互相污染。

#### 4.2.3 源码精读

`compile_cubin` 本体——注意输出路径就是「把输入后缀改成 `.cubin`」，以及 flags 的拼装顺序：

[`_compile.py:807-831` —— `compile_cubin` 拼装命令行并调用](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L807-L831) `args` 是位置参数（输入、`-o`、输出），`flags` 是命名选项（`--gpu-name`、`-O{opt}`、二选一的 `--device-debug`/`--lineinfo`）；最后 `binary.run(args, flags, timeout_sec)` 执行，返回 cubin 路径。注释里的 docstring 说明：调试构建时 `tileiras` 必须 `-O0` + `--device-debug`，且磁盘缓存键必须与之匹配。

`-O` 与调试开关的决策函数——只有两条分支：

[`_compile.py:569-578` —— `_tileiras_effective_opt_and_device_debug`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L569-L578) 调试构建返回 `(0, True)`，否则 `opt_level_for_target(sm_arch)` + `False`。

`opt_level` 如何按架构解析（默认 3，可被 `ByTarget` 覆盖）：

[`_compiler_options.py:48-58` —— `CompilerOptions.opt_level_for_target`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compiler_options.py#L48-L58) 若 `opt_level` 是 `ByTarget`，先查「该架构的专属值」、再查「默认值」、最后回退到字段默认（3）。这正是 `test_opt_level_for_target`（[`test_compiler_options.py:79-84`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_compiler_options.py#L79-L84)）所验证的行为。

错误翻译：`tileiras` 的 stderr 会被解析成「带行号的可读消息」。`_CompilerBinary.run` 在 [`_compile.py:617-624`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L617-L624) 把 `CalledProcessError` 包成 `TileCompilerExecutionError`、把 `TimeoutExpired` 包成 `TileCompilerTimeoutError`；前者会调 `_parse_tileir_stderr` 从 `loc("file":line:col): error: ...` 这样的行里提取位置：

[`_exception.py:234-254` —— 两个编译异常类](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_exception.py#L234-L254) `TileCompilerExecutionError` 保留 `return_code`/`stderr`/`compiler_flags`/`compiler_version` 用于排错；`TileCompilerTimeoutError` 的提示语直接建议「用更小的 tile size 降低编译时间」。stderr 解析逻辑见 [`_exception.py:201-220`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_exception.py#L201-L220)。

最后，`compile_cubin` 在 `compile_tile` 里的调用上下文——写临时 `.bytecode`、调 `compile_cubin`、读回 bytes、出错时可选 crash dump：

[`_compile.py:541-560` —— `compile_tile` 调用 `compile_cubin`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L541-L560) 用 `tempfile.NamedTemporaryFile(suffix='.bytecode', dir=temp_dir)` 写字节码，`compile_cubin(..., timeout_sec=context.config.compiler_timeout_sec)` 编译，`Path(cubin_file).read_bytes()` 读回 cubin；若抛 `TileCompilerError` 且开了 crash dump，就把匿名化的字节码与 IR 打包成 zip（`_compiler_crash_dump`）。

#### 4.2.4 代码实践（环境验证型·待本地验证）

1. **目标**：用 DEBUG 日志抓到 cuTile 实际发给 `tileiras` 的命令行，确认默认走 `--lineinfo`，并在开启调试构建后确认切换到 `-O0 --device-debug`。
2. **步骤**：
   ```python
   # probe_cubin.py
   import logging
   logging.basicConfig(level=logging.DEBUG)  # _CompilerBinary.run 用 logger.debug 打命令行
   import torch, cuda.tile as ct
   import cuda.tile._compile as c

   @ct.kernel
   def addk(a, b, r):
       bid = ct.bid(0)
       x = ct.load(a, (bid,), (32,))
       y = ct.load(b, (bid,), (32,))
       ct.store(r, (bid,), x + y)

   a = torch.ones(64, dtype=torch.float32, device="cuda")
   b = torch.ones(64, dtype=torch.float32, device="cuda")
   r = torch.empty(64, dtype=torch.float32, device="cuda")

   print("=== 正常构建（应含 --lineinfo）===")
   ct.launch(0, (2,), addk, [a, b, r])

   # 切到调试构建：改模块全局 + 清相关缓存；缓存键会因 device_debug 变化而重新编译
   c.EXPERIMENTAL_CUDA_TILE_DEBUG_BUILD = True
   print("=== 调试构建（应含 -O0 --device-debug）===")
   ct.launch(0, (2,), addk, [a, b, r])
   ```
3. **观察**：stderr 里会出现形如 `Invoke tile compiler: <path>/tileiras <tmp>.bytecode -o <tmp>.cubin --gpu-name sm_1XX -O3 --lineinfo` 的 DEBUG 行；调试构建那段则变成 `... -O0 --device-debug`。
4. **预期**：你能在日志里逐字看到 4.2.2 流程图给出的 `args + flags`。**待本地验证**：`sm_1XX` 由你的 GPU 决定；临时文件名每次不同；调试构建会因缓存键变化而真正再调一次 `tileiras`（若仍命中缓存，可设 `CUDA_TILE_CACHE_DIR=0` 关掉磁盘缓存重跑）。

#### 4.2.5 小练习与答案

- **练习 1**：`--lineinfo` 与 `--device-debug` 都往 cubin 里塞调试信息，为什么默认选前者而不是后者？
  - **答**：`--device-debug` 要求 `-O0`（关优化），内核运行会明显变慢，只适合「真的要单步调试设备代码」时用；`--lineinfo` 不关优化，只补一份「行号 ↔ 机器码」映射，足够 Nsight Compute 做 profiling 与栈回溯，对性能几乎无影响，所以作为生产默认。
- **练习 2**：为什么 `_tileiras_effective_opt_and_device_debug` 的返回值要同时进 `cache_key`（[`_compile.py:531-535`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L531-L535)），而不能只把 `compiler_options.opt_level` 进 key？
  - **答**：调试构建会**无视**用户设的 `opt_level` 强制取 0，并额外加 `--device-debug`。若缓存键只看 `opt_level`，那么「普通 `-O3`」与「调试 `-O0 --device-debug`」在用户 `opt_level=3` 时会算出同一个键，导致调试构建错误命中普通 cubin（反之亦然）。把 `effective_opt` 与 `device_debug` 都纳入键，才能保证两种构建各自缓存、互不串味。

---

### 4.3 get_sm_arch：探测当前 GPU 架构

#### 4.3.1 概念说明

`tileiras` 必须知道「为哪种 GPU 架构生成代码」，因为它要为不同的 SM（Streaming Multiprocessor）版本产出不同的指令调度与张量核用法。这个目标架构用 `tileiras` 的 `--gpu-name` 参数传入，取值形如 `sm_100`（Blackwell）、`sm_120`、`sm_90`（Hopper）等——也就是 CUDA 习惯的 `sm_<major><minor>` 命名，其中 `<major><minor>` 是设备的 **compute capability**。

`get_sm_arch` 就是「问当前设备：你的 compute capability 是多少？」。它本身极短，重点在于它**经 C++ 扩展落到 CUDA 驱动 API**，并且被 `@cache`——因为一次进程里设备的 compute capability 不会变。

#### 4.3.2 核心流程

```text
get_sm_arch()  [@cache]
│
├─ major, minor = get_compute_capability()   # _cext 暴露，C++ 侧调 cuDeviceComputeCapability
│                                                经 _load_libcuda 加载的 libcuda.so.1
└─ return f"sm_{major}{minor}"                # 如 "sm_100"
```

`compile_tile` 在用户没显式传 `sm_arch` 时，就用它当默认值（见 [`_compile.py:465-466`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L465-L466)）；AOT 导出（`export_kernel`）则要求用户显式给架构，否则不能脱离运行时设备。

#### 4.3.3 源码精读

`get_sm_arch` 只有两行，但它是 Python 与 CUDA 驱动之间的一个典型入口：

[`_compile.py:801-804` —— `get_sm_arch`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L801-L804) 调 `_cext.get_compute_capability()`（C++ 扩展，签名见 [`_cext.pyi:35-36`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cext.pyi#L35-L36)），把 `(major, minor)` 拼成 `sm_{major}{minor}`。

`get_compute_capability` 在 C++ 侧最终通过驱动符号解析调用 CUDA Driver API，而驱动符号的入口来自 `_load_libcuda.py`：

[`_load_libcuda.py:10-16` —— 加载 libcuda 并取 cuProcAddress_v2](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_load_libcuda.py#L10-L16) Linux 下 `CDLL("libcuda.so.1")`、Windows 下 `CDLL("nvcuda.dll")`，拿到 `cuGetProcAddress_v2` 的地址——后续所有驱动 API（含取 compute capability 所需的那些）都经它按名字解析。也就是说，`get_sm_arch` 的信息链是：Python `get_sm_arch` → C++ 扩展 `get_compute_capability` → `cuGetProcAddress_v2` 解析出的驱动函数 → GPU。

#### 4.3.4 代码实践（源码阅读型 + 验证型·待本地验证）

1. **目标**：看清当前设备的 `sm_arch`，并确认它被用作 `tileiras` 的 `--gpu-name`。
2. **步骤**：
   ```python
   # probe_arch.py
   import cuda.tile._compile as c
   c.get_sm_arch.cache_clear()
   print("sm_arch:", c.get_sm_arch())
   # 再结合 4.2.4 的 DEBUG 日志，确认命令行里 --gpu-name 与此一致
   ```
   或在 shell 用 `nvidia-smi --query-gpu=compute_cap --format=csv` 对照。
3. **观察**：脚本输出如 `sm_100` / `sm_120`，与 `nvidia-smi` 给的 compute capability（如 `8.0`/`10.0`/`12.0`）一一对应。
4. **预期**：`get_sm_arch()` 输出的 `<major><minor>` 与 4.2.4 抓到的 `--gpu-name` 完全相同——证明默认架构链路 `get_sm_arch → compile_cubin 的 flags` 是通的。**待本地验证**：具体值取决于你的 GPU。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `get_sm_arch` 要 `@cache`，而 `compile_cubin` 不需要？
  - **答**：设备的 compute capability 在进程内恒定，缓存可避免反复走 C++ 扩展与驱动调用；而 `compile_cubin` 每次的输入（字节码、`compiler_options`、临时文件路径）都可能不同，缓存它没有意义——真正的「跨编译去重」由更上层的磁盘 cubin 缓存（`cache_key`/`cache_lookup`，u7-l4 会讲）承担。
- **练习 2**：AOT 导出（`export_kernel`）为什么不像 JIT 这样自动用 `get_sm_arch`，而是要求显式传架构？
  - **答**：AOT 的目的就是「在 没有 GPU / 不在目标机上」预先编译，所以编译机当前设备的 compute capability 往往**不是**目标架构；若默认用 `get_sm_arch`，会把导出锁死在编译机的 GPU 上，违背 AOT 跨机部署的初衷。

---

### 4.4 _get_max_supported_bytecode_version：版本探针即首次调用

#### 4.4.1 概念说明

这个函数在 u7-l2 已经以「版本探测」的角度出现过——它从高到低试 `BytecodeVersion`，第一个能被 `tileiras` 接受的就是当前工具链支持的最高字节码版本。本讲换一个角度再读它：**它是 cuTile 进程里对 `tileiras` 的第一次真实调用**。

为什么这么说？`compile_tile` 在用户没指定 `bytecode_version` 时会调它（[`_compile.py:468-470`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L468-L470)），而它发生在任何真实内核编译之前。于是它的「副作用」是：触发了 `_find_compiler_bin` 的解析（如果还没被触发过）、完成了一次 `tileiras` 子进程的试跑。如果机器上根本找不到 `tileiras`，或 `tileiras` 坏了，错误会在这里就暴露出来——而不是等到用户第一次 `ct.launch`。

它与 `compile_cubin` 共用同一套子进程机制（都走 `_CompilerBinary.run`），但错误处理策略不同：探针里**捕获** `TileCompilerError` 并 `continue`（这个版本不支持就试下一个），而 `compile_cubin` 是**向上抛**（编译失败必须让用户知道）。

#### 4.4.2 核心流程

```text
_get_max_supported_bytecode_version(temp_dir, allow_dev=False)  [@cache per temp_dir]
│
├─ binary = _find_compiler_bin()                # 顺带触发 4.1 的寻路
├─ flags = ["--gpu-name", "sm_120"]             # 探针固定用 sm_120
│
└─ for version in reversed(_all_bytecode_versions(allow_dev)):   # 从 V_13_3 往下试
        probe = 写一个 num_functions=0 的空字节码（version 内嵌在 header）
        把 probe 写进临时 .bytecode，准备一个空 .cubin
        try:
            binary.run([bytecode, "-o", cubin], flags)   # 复用 compile_cubin 的执行器
        except TileCompilerError:
            continue                                     # 该版本不支持，试下一个
        return version                                  # 第一个成功的即为最高支持版本

    全部失败 → warning + 回退 V_13_1
```

注意几个细节：① 探针固定用 `--gpu-name sm_120`，因为字节码版本的支持与否取决于 `tileiras` 自身而非具体 GPU，用一个代表性架构即可；② 探针体是 `with bc.write_bytecode(num_functions=0, buf=probe, version=version): pass`——一个**零函数**的最小合法字节码，只含 header + 空 Func 段 + EndOfBytecode（u7-l2 的综合实践里你手写过它）；③ 候选集 `reversed(_all_bytecode_versions(allow_dev))` 在 `allow_dev=True`（`dev_features_enabled()`）时才会包含 `V_13_4`（见 [`_compile.py:713-721`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L713-L721)）。

#### 4.4.3 源码精读

探针本体——从高到低试，第一个能编过 sm_120 的版本即为答案：

[`_compile.py:724-748` —— `_get_max_supported_bytecode_version` 探针](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L724-L748) 注意它 `@cache`、`binary = _find_compiler_bin()`、`flags = ["--gpu-name", "sm_120"]`、循环里 `with bc.write_bytecode(num_functions=0, ...)` 写空字节码、`binary.run([f_in.name, "-o", f_out.name], flags)` 复用统一执行器、`except TileCompilerError: continue`、成功即 `return version`，全失败则 warning 并 `return BytecodeVersion.V_13_1`。

候选版本集合与 dev 开关：

[`_compile.py:713-721` —— `_SUPPORTED_VERSIONS` 与 `_all_bytecode_versions`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L713-L721) 生产只允许 `V_13_1/2/3`，`V_13_4` 仅当 `dev_features_enabled()` 时纳入。

把这几个函数串起来看一次完整 JIT 的「启动开销」：第一次 `ct.launch` 会先（按需）解析 `_find_compiler_bin`、`_get_compiler_version_string`、`get_sm_arch`、`_get_max_supported_bytecode_version`，其中**版本探针已经替真实编译把 `tileiras` 子进程跑通了一遍**——所以等到 `compile_cubin` 真正干活时，`tileiras` 的可执行性已经被验证过了。

#### 4.4.4 代码实践（源码阅读型·待本地验证）

1. **目标**：确认你机器上 `tileiras` 支持的最高字节码版本，并理解它与「第一次 `ct.launch` 慢」的关系。
2. **步骤**：
   ```python
   # probe_version.py
   import cuda.tile._compile as c
   from cuda.tile._bytecode.version import BytecodeVersion
   c._get_max_supported_bytecode_version.cache_clear()
   c._find_compiler_bin.cache_clear()
   v = c._get_max_supported_bytecode_version(c.default_tile_context.config.temp_dir,
                                              allow_dev=False)
   print("max supported bytecode version:", v.as_string())   # 期望 "13.1"/"13.2"/"13.3"
   print("sm_arch                       :", c.get_sm_arch())
   ```
3. **观察**：`as_string()` 给出 `"13.3"`（或更低），与 README 里「`tileiras` 版本 13.2 起」的说明对应；它等于真实编译时写进 `.tileirbc` header 的 minor（见 u7-l2 综合实践里 header 的 `0d XX` 字节）。
4. **预期**：你会看到「第一次调用 `ct.launch` 时，这个探针已经先跑过一次 `tileiras`」——可在 4.2.4 的 DEBUG 日志里观察到探针那次 `probe...` 临时文件的调用先于真实内核编译。**待本地验证**：具体版本号取决于你装的 `tileiras`。

#### 4.4.5 小练习与答案

- **练习 1**：探针为什么固定用 `--gpu-name sm_120`，而不是用 `get_sm_arch()` 的真实架构？
  - **答**：字节码版本的支持由 `tileiras` 自身的实现决定（它能解析哪个版本的格式），与目标 GPU 架构无关。用一个它能稳定接受的架构（sm_120）做探针即可；若用真实架构，反而可能在「版本其实支持、但该架构有别的限制」时误判，把能用的版本错杀掉。
- **练习 2**：若 `tileiras` 完全找不到（`_find_compiler_bin` 抛 `FileNotFoundError`），`_get_max_supported_bytecode_version` 会怎样？用户在哪一步会先看到错误？
  - **答**：`_find_compiler_bin()` 在循环外被调用，找不到时它的 `FileNotFoundError` 会直接向上抛出，根本进不了 for 循环。由于 `compile_tile` 在解析 `bytecode_version` 时（[`_compile.py:468-470`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L468-L470)）就会触发本函数，所以用户**第一次 `ct.launch`** 时就会看到「`'tileiras' compiler not found`」并附安装提示，而不会等到更深的编译阶段。

---

## 5. 综合实践

把本讲四个最小模块串起来：**复现一次完整的「定位 tileiras → 探测版本与架构 → 抓到真实命令行 → 切换调试构建」全流程，并用源码解释每一步落在哪个函数上。**

1. **准备环境**：确保「未装 pip tileiras、只有系统 CTK 13.1+」的状态（即没装 `nvidia-cuda-tileiras` 等三件套）。准备一个最小内核脚本（可复用 4.2.4 的 `probe_cubin.py`）。
2. **第一段·寻路验证**：
   - 运行 4.1.4 的 `probe_find.py`，记录 `tileiras path` 与 `pass_cuda_home_var`。
   - **解释**：据 `pass_cuda_home_var` 判断 cuTile 走的是四级里的哪一级（你应得到 `True`=PATH/CUDA_HOME 或 `False`=默认 CTK 路径之一），并说明为何此情形下子进程环境里 `CUDA_HOME` 被删/被保留。
3. **第二段·版本与架构探测**：
   - 运行 4.4.4 的 `probe_version.py`，记录最高字节码版本与 `sm_arch`。
   - **解释**：说明 `_get_max_supported_bytecode_version` 是如何「顺带」触发 `_find_compiler_bin`、并完成 `tileiras` 的首次试跑的（对应 [`_compile.py:724-748`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L724-L748)）。
4. **第三段·抓真实命令行**：
   - 开 `logging.basicConfig(level=logging.DEBUG)` 后运行内核（4.2.4），从 `Invoke tile compiler:` 这条 DEBUG 行抄下完整命令行。
   - **解释**：把命令行拆成 `args`（输入/`-o`/输出）与 `flags`（`--gpu-name`/`-O`/`--lineinfo`），逐项指认它们分别来自 `compile_cubin` 的哪一行、`-O` 来自 `_tileiras_effective_opt_and_device_debug` → `opt_level_for_target`、`--gpu-name` 来自 `get_sm_arch`。
5. **第四段·切换调试构建**：
   - 设 `cuda.tile._compile.EXPERIMENTAL_CUDA_TILE_DEBUG_BUILD = True`（或 `export EXPERIMENTAL_CUDA_TILE_DEBUG_BUILD=1` 后重跑），关掉磁盘缓存（`export CUDA_TILE_CACHE_DIR=0`）以确保重新编译，再次抓命令行。
   - **解释**：确认 flags 由 `--lineinfo` 切到 `--device-debug`、`-O3` 切到 `-O0`（对应 [`_compile.py:569-578`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L569-L578)），并说明为何缓存键随之改变（4.2.5 练习 2）。
6. **第五段·超时验证（可选）**：
   - `export CUDA_TILE_COMPILER_TIMEOUT_SEC=1` 后编译一个大 tile 的内核，预期触发 `TileCompilerTimeoutError`（对应 [`_compile.py:620-624`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L620-L624) 与 [`_context.py:38-45`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_context.py#L38-L45)）。
7. **预期产物**：一份「函数 → 行号 → 观察到的现象」对照表，能向别人讲清「一次 JIT 编译里，cuTile 是怎么找到 tileiras、怎么问清架构与版本、怎么拼出命令行、怎么处理失败的」。**待本地验证**：所有路径、版本号、架构号以你机器为准。

> 提示：`tileiras` 的定位与调用只是「字节码 → cubin」这一跳；跳完之后 cubin 的去重与持久化由 SQLite 磁盘缓存承担——那是 u7-l4（JIT 磁盘缓存）的主题。

## 6. 本讲小结

- cuTile **自己不生成机器码**，生成 cubin 完全外包给外部可执行文件 `tileiras`；cuTile 的职责只是「找到它、拼命令行、调用它、翻译错误」。
- `_find_compiler_bin`（[`_compile.py:675-710`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L675-L710)）按 **pip 包 → `PATH` → `CUDA_HOME` → 默认 CTK 路径** 四级查找，`@cache` 保证进程内只解析一次；每级返回的 `pass_cuda_home_var` 决定子进程是否透传 `CUDA_HOME`——pip/默认路径命中时**主动删掉**它以防版本污染，PATH/CUDA_HOME 命中时透传以尊重用户环境。
- `compile_cubin`（[`_compile.py:807-831`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L807-L831)）把「字节码文件 + `CompilerOptions` + `sm_arch` + 超时」翻译成 `tileiras` 命令行：`args = [bytecode, -o, cubin]`、`flags = [--gpu-name, sm_arch, -O{opt}, --lineinfo|--device-debug]`；`-O` 与调试开关由 `_tileiras_effective_opt_and_device_debug` 决定，调试构建（`EXPERIMENTAL_CUDA_TILE_DEBUG_BUILD=1`）强制 `-O0 --device-debug`，否则 `-O{opt_level_for_target}` + `--lineinfo`。
- 子进程的两种失败分别被翻译成 `TileCompilerExecutionError`（解析 stderr 提取行号）与 `TileCompilerTimeoutError`（建议减小 tile size）；`get_sm_arch`（[`_compile.py:801-804`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L801-L804)）经 C++ 扩展与 `libcuda` 取 compute capability 拼成 `sm_{major}{minor}` 作为默认 `--gpu-name`。
- `_get_max_supported_bytecode_version`（[`_compile.py:724-748`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L724-L748)）用「空字节码探针」从高到低试版本，是进程里对 `tileiras` 的**首次真实调用**——既探测最高支持版本，又顺带验证 `tileiras` 可用；它捕获 `TileCompilerError` 继续，而 `compile_cubin` 向上抛，两者共用同一套 `_CompilerBinary.run` 子进程机制。
- `effective_opt` 与 `device_debug` 同时进入磁盘缓存键（[`_compile.py:531-535`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L531-L535)），保证普通构建与调试构建的 cubin 互不串味。

## 7. 下一步学习建议

- **接 u7-l4（JIT 磁盘缓存）**：本讲只到「`compile_cubin` 产出 cubin」为止。下一次讲 `cache_key` 的构成（编译器版本 / `sm_arch` / `effective_opt` / 字节码哈希 / `device_debug`）、`cache_lookup`/`cache_store` 与 SQLite 表结构、以及 LRU 淘汰——你会看到本讲反复提到的「缓存键」是如何决定 cubin 复用的。
- **回看 u5-l2（compile_tile 流水线）**：现在带着本讲的细节重读 [`_compile.py:522-566`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L522-L566)，会看到「缓存命中就直接 return、未命中才写临时文件调 `compile_cubin`」的全貌，理解「后端按需拉动」的含义。
- **延伸到 u8-l1（launch 与调度）**：本讲的 `compile_cubin` 产出的 cubin，最终被 `ct.launch` 注册进 `TileDispatcher` 并由 `cuLaunchKernel` 启动；下一单元会讲从 Python 参数到 `cuLaunchKernel` 之间、调用约定（`cutile_python_v1`/`v2`）如何填充参数。
- **动手方向**：写一个小脚本，在不清缓存的情况下连续 `ct.launch` 同一内核两次，用 DEBUG 日志观察「第二次不再出现 `Invoke tile compiler`」——直观感受磁盘缓存如何把本讲的 `compile_cubin` 这一跳短路掉。
