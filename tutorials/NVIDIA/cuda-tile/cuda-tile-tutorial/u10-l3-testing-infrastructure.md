# 测试基础设施：lit 与 FileCheck

## 1. 本讲目标

CUDA Tile IR 是一个工程化的编译基础设施，每一项语义（类型、操作、属性、字节码格式、变换 Pass）都靠一套自动化测试来保证正确性。本讲带你理解这套测试体系是如何组织、如何配置、如何运行的。

学完本讲，你应当能够：

- 说清 `test/` 目录下 `Bytecode/`、`Dialect/`、`Transforms/`、`CAPI/`、`python/`、`lib/` 各子目录的职责，以及 `.mlir`、`.c`、`.py` 三类测试文件分别走哪条运行路径。
- 理解 `check-cuda-tile` 这个 CMake 目标如何把「构建依赖」与「执行 lit」串起来，以及为何要单独留一个 `check-cuda-tile-build-only`。
- 读懂 `lit.site.cfg.py.in`（CMake 模板）与 `lit.cfg.py`（运行时配置）的分工，掌握 `%round_trip_test`、`%PYTHON`、`%PATH%` 等替换符（substitution）的来源与含义。
- 掌握 round-trip（往返）测试机制的原理：把 MLIR 文本序列化成字节码再反序列化回来，与「不走字节码」的参考输出比对，从而同时验证写入器和读取器。
- 了解仅测试构建时才注册的「测试 Pass」（`TestPasses.cpp`）如何挂到 `cuda-tile-opt` 上，用于测试编译器内部工具函数。

本讲是整个学习手册的工程化收尾之一，承接 [u7-l1](u7-l1-bytecode-translation.md)（字节码翻译管线，round-trip 直接复用 `cuda-tile-translate` 的两个翻译）与 [u10-l2](u10-l2-python-bindings.md)（Python 绑定，`%PYTHON` 测试受 `enable_bindings_python` 控制）。

## 2. 前置知识

### 2.1 什么是 lit 与 FileCheck

**lit**（LLVM Integrated Tester）是 LLVM 项目自带的测试执行器。它的工作方式很朴素：扫描某个目录下所有「被标记为测试」的文件，逐个文件解析其中以 `RUN:` 开头的指令行，把指令里的替换符（如 `%s`、`%t`）替换成实际路径，然后在 shell 里执行这些命令，根据命令的退出码（0 成功、非 0 失败）判定测试通过与否。一句话：**lit 本身不判断对错，它只负责「跑命令、看退出码」**。

**FileCheck** 是配套的「输出比对」工具。它的输入是两路：一是被测命令的标准输出/错误流，二是测试文件里以 `CHECK`、`CHECK-NEXT`、`CHECK-NOT` 等开头的「期望行」。FileCheck 按顺序在输出里匹配这些期望行，全部命中则通过。与普通 `diff` 不同，FileCheck 允许用 `{{...}}` 写正则、用 `[[NAME:...]]` 捕获变量，因此只检查「关心的部分」而忽略无关的空白与编号。

这两者组合起来，就构成了 LLVM/MLIR 生态的测试范式：

```bash
# 一条 RUN 行：跑工具 → 把输出管道给 FileCheck → 比对期望
// RUN: cuda-tile-opt %s --pass-pipeline='...' | FileCheck %s
```

### 2.2 与本讲相关的两个前置结论

- **u7-l1**：`cuda-tile-translate` 注册了两个互逆翻译 `mlir-to-cudatilebc`（文本→字节码）与 `cudatilebc-to-mlir`（字节码→文本）。本讲的 round-trip 测试正是把这两个翻译首尾相接。
- **u10-l2**：Python 绑定依赖构建期开关 `CUDA_TILE_ENABLE_BINDINGS_PYTHON`。本讲的 `%PYTHON` 测试只在开启该开关时才纳入测试套件。

### 2.3 几个 lit 替换符速查

| 替换符 | 含义 |
|---|---|
| `%s` | 当前测试文件的源路径 |
| `%S` | 当前测试文件所在目录 |
| `%t` | 本测试专属的临时文件路径前缀（唯一，可放心写中间产物） |
| `%PYTHON` | 配置好的 Python 解释器（含 sanitizer 预加载） |
| `%round_trip_test` | 跨平台的 round-trip 脚本（Windows 用 `.py`，Linux 用 `.sh`） |
| `%PATH%` / `%shlibext` | 系统 PATH 与共享库后缀 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `test/CMakeLists.txt` | 测试的 CMake 入口：生成 `lit.site.cfg.py`、收集构建依赖、定义 `check-cuda-tile` 目标 |
| `test/lit.site.cfg.py.in` | CMake 模板：把构建期路径（工具目录、库目录、Python 解释器等）以 `@VAR@` 注入 lit 配置 |
| `test/lit.cfg.py` | lit 运行时配置：声明测试格式、后缀名、替换符、工具路径、环境变量 |
| `test/round_trip_test.py` | 跨平台的 round-trip 脚本（Windows 入口）：MLIR→字节码→MLIR 后与参考输出比对 |
| `test/Dialect/CudaTile/round_trip_test.sh` | Linux/Unix 下的 round-trip shell 版本 |
| `test/lib/TestPasses.cpp` | 仅测试构建注册的辅助 Pass，用于测试编译器内部工具函数 |
| `test/lib/CMakeLists.txt` | 把 `TestPasses.cpp` 编成 `CudaTileTestPasses` 库 |
| `test/Bytecode/operationsTest.mlir` | 典型的 round-trip 测试用例，覆盖一大批操作的往返 |
| `test/python/lit.local.cfg` | Python 子目录的局部配置：未启用绑定时整目录跳过 |
| `test/CAPI/register.c` | C API 测试用例示例（`.c` 文件，编成可执行程序） |

## 4. 核心概念与源码讲解

### 4.1 测试目录组织与 lit/FileCheck 运行模型

#### 4.1.1 概念说明

CUDA Tile 的测试按「被测对象」分目录存放，每个子目录对应一类测试技术：

- `test/Bytecode/`：字节码序列化/反序列化与版本兼容测试，绝大多数走 round-trip；`invalid/` 子目录放非法字节码文件，用于检验读取器的报错路径；`versioning/` 演练版本演进。
- `test/Dialect/CudaTile/`：方言操作合法/非法语义测试，多用 `cuda-tile-opt` + `FileCheck`。
- `test/Transforms/`：变换 Pass（如 `fuse-fma`、`loop-split`）的前后对照测试，用 `--pass-pipeline` 调起。
- `test/CAPI/`：C API 集成测试，`.c` 文件被编成可执行程序，直接运行看退出码。
- `test/python/`：Python 绑定的 `pytest` 测试，用 `%PYTHON -m pytest` 跑。
- `test/lib/`：不是测试目录，而是测试辅助库，存放仅测试构建用的 Pass。

lit 如何决定「哪些文件是测试」？靠两个条件：文件后缀在白名单里、且不在排除名单里。CUDA Tile 把后缀限定为 `.mlir`、`.c`、`.py` 三类，正好对应上面三种运行模型。

#### 4.1.2 核心流程

一条测试从被发现到被执行的全过程：

1. **CMake 扫描**：`add_lit_testsuites(cuda_tile ...)` 递归扫描 `test/` 源目录，把每个含 `RUN:` 行的测试文件登记为一个 lit 测试。
2. **lit 启动**：执行 `check-cuda-tile` 目标时，lit 先加载 `lit.site.cfg.py`（构建产物，含真实路径），它再把控制权交给源码树里的 `lit.cfg.py`（真正的配置逻辑）。
3. **过滤测试**：lit 用 `config.suffixes` 选出 `.mlir/.c/.py` 文件，用 `config.excludes` 剔除配置脚本本身，用各目录的 `lit.local.cfg` 做局部开关（如未启用 Python 绑定则跳过 `python/`）。
4. **执行 RUN 行**：lit 用内部 shell（`ShTest`）把每个 `RUN:` 行里的 `%s`、`%t`、`%round_trip_test` 等替换符展开成实际路径/命令，然后执行。
5. **判定**：命令退出码为 0 即通过；对带 `not` 前缀的「期望失败」测试，反过来要求退出码非 0。

#### 4.1.3 源码精读

测试格式与后缀白名单写在 [test/lit.cfg.py:11-19](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/lit.cfg.py#L11-L19)：

```python
config.name = "CUDA_TILE"
# ...
config.test_format = lit.formats.ShTest()
# suffixes: A list of file extensions to treat as test files.
config.suffixes = [".mlir", ".c", ".py"]
```

注意 `ShTest()` 不带 `execute_external=True`。这一点很关键——LLVM-23 已废弃外部 shell 执行，CUDA Tile 的所有测试都只用 lit 内部 shell 支持的标准构造，因此迁移到内部 shell 不会有兼容问题。

排除名单在 [test/lit.cfg.py:22](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/lit.cfg.py#L22)，把配置脚本和 round-trip 脚本自身排除，否则 lit 会把它们误当测试：

```python
config.excludes = ["lit.cfg.py", "lit.site.cfg.py", "round_trip_test.py"]
```

Python 子目录的局部开关见 [test/python/lit.local.cfg:1-3](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/python/lit.local.cfg#L1-L3)——未启用 Python 绑定时整目录标记为 `unsupported`，lit 自动跳过：

```python
if not config.enable_bindings_python:
    config.unsupported = True
```

CAPI 测试则不同：`.c` 文件不是直接被 lit 解释的脚本，而是先被 CMake 编成可执行程序，RUN 行只负责「运行这个程序」：

```
// RUN: test-cuda-tile-capi-register
```

见 [test/CAPI/register.c:11](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/CAPI/register.c#L11)。其 `main` 函数 [test/CAPI/register.c:13-30](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/CAPI/register.c#L13-L30) 注册 cuda_tile 方言、尝试加载它，加载失败则返回 `-1`（非 0 退出码 → lit 判失败）。

#### 4.1.4 代码实践

**实践目标**：用眼睛走一遍 lit 的发现与执行逻辑，理解三类测试文件的不同归宿。

**操作步骤**：

1. 打开 `test/Bytecode/operationsTest.mlir`，确认它首行是 `// RUN: %round_trip_test %s %t`，后缀 `.mlir` 在白名单里 → 会被 lit 当作测试。
2. 打开 `test/CAPI/register.c`，确认它的 `// RUN:` 行只写了一个可执行程序名，且该程序由 `test/CAPI/CMakeLists.txt` 编译产生。
3. 打开 `test/python/cuda_tile_public_bindings.py`，确认首行是 `# RUN: %PYTHON -m pytest %s`（注意 `.py` 文件用 `#` 注释，而 `.mlir`/`.c` 用 `//`）。
4. 设想在未启用 Python 绑定的构建里，`test/python/` 目录会如何被 `lit.local.cfg` 跳过。

**需要观察的现象**：三类测试文件的 RUN 行写法各不相同，但都被同一个 lit 套件收纳；它们的共同点是「后缀在 `[.mlir, .c, .py]` 里」。

**预期结果**：能口头说清「`.mlir` 走 `cuda-tile-*` 工具管道、`.c` 走预编译可执行程序、`.py` 走 pytest」三条路径。若尚未本地构建，相关结论标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `round_trip_test.py` 要被列入 `config.excludes`，而 `operationsTest.mlir` 不用？

**答案**：`round_trip_test.py` 的后缀是 `.py`，若不排除，lit 会把它当作 pytest 测试去跑，但它其实是个被 `%round_trip_test` 替换符调用的辅助脚本（入口在 `main()`，参数约定为「输入文件、输出基名」），并非 pytest 用例；`operationsTest.mlir` 则是真正的测试用例，理应被纳入。

**练习 2**：`test_format = lit.formats.ShTest()` 不传 `execute_external=True` 会带来什么约束？

**答案**：lit 使用内部 shell 解释 RUN 行，因此 RUN 行里只能用内部 shell 支持的语法（管道 `|`、重定向 `>`、`&&` 等标准构造），不能依赖某些只有外部 shell 才有的特性；这也正是注释里提到的「LLVM-23 废弃外部 shell 后必须如此」。

### 4.2 check-cuda-tile 目标与 CMake 依赖链

#### 4.2.1 概念说明

lit 测试要能跑，前提是「被测工具和库已经构建出来」。CMake 在这里扮演两个角色：一是**收集依赖**——把所有测试需要用到的工具（`cuda-tile-opt`、`cuda-tile-translate`、`cuda-tile-optimize`、`FileCheck`、`not`）和库列成一张清单；二是**定义目标**——`check-cuda-tile` 这个目标先把依赖全部构建好，再调起 lit 执行测试。

这套机制直接复用自 MLIR/LLVM 的测试基础设施（`add_lit_testsuite`、`add_lit_testsuites`、`configure_lit_site_cfg` 都是 MLIR 提供的 CMake 函数），CUDA Tile 只是把「依赖清单」换成自己的工具。

#### 4.2.2 核心流程

`test/CMakeLists.txt` 的工作流：

1. **准备站点配置变量**：算出库输出目录、宿主 OS、宿主编译器（供 sanitizer 配置用）。
2. **规范化布尔开关**：`llvm_canonicalize_cmake_booleans` 把 CMake 的 `ON/OFF` 转成 Python 的 `True/False`，供 `lit.site.cfg.py.in` 直接嵌入。
3. **生成 `lit.site.cfg.py`**：`configure_lit_site_cfg` 把 `.in` 模板里的 `@VAR@` 替换为实际路径，输出到构建目录。
4. **收集测试依赖**：定义 `MLIR_TEST_DEPENDS` 列表；按需追加 Python 模块、CAPI 测试程序。
5. **定义目标**：`check-cuda-tile-build-only`（只构建依赖、不跑测试）与 `check-cuda-tile`（构建依赖 + 跑 lit）。

#### 4.2.3 源码精读

依赖清单见 [test/CMakeLists.txt:33-44](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/CMakeLists.txt#L33-L44)：

```cmake
set(MLIR_TEST_DEPENDS
  FileCheck
  not
  cuda-tile-opt
  cuda-tile-optimize
  cuda-tile-translate
  ${CAPI_TEST_TARGETS}
)

if(CUDA_TILE_ENABLE_BINDINGS_PYTHON)
  list(APPEND MLIR_TEST_DEPENDS CudaTilePythonModules)
endif()
```

注意 `FileCheck` 和 `not` 也在其中——这两个是 LLVM 自带工具，作为依赖确保它们已被构建。`not` 是「期望失败」前缀工具，配合负向测试使用（见 `// RUN: not cuda-tile-translate ...`）。

两个目标的定义在 [test/CMakeLists.txt:50-62](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/CMakeLists.txt#L50-L62)：

```cmake
# 只构建依赖，不执行测试（供 CI 把构建步骤与测试步骤拆分）
add_custom_target(check-cuda-tile-build-only
  DEPENDS ${MLIR_TEST_DEPENDS}
)

add_lit_testsuite(check-cuda-tile "Running the cuda_tile regression tests"
  ${CMAKE_CURRENT_BINARY_DIR}
  DEPENDS ${MLIR_TEST_DEPENDS}
)

add_lit_testsuites(cuda_tile ${CMAKE_CURRENT_SOURCE_DIR}
  DEPENDS ${MLIR_TEST_DEPENDS}
)
```

`check-cuda-tile-build-only` 的存在很有工程意义：CI 机器人常把「构建」与「测试」拆成两个阶段（build 阶段可分布式、test 阶段要资源），这个目标让构建阶段不必强行跑测试就能产出全部依赖。

注意 `add_lit_testsuite`（单数）以**构建目录** `${CMAKE_CURRENT_BINARY_DIR}` 为根——lit 实际从这里启动；而 `add_lit_testsuites`（复数）以**源码目录** `${CMAKE_CURRENT_SOURCE_DIR}` 为根做递归扫描登记。两者协作：扫描来自源码树，执行落在构建树。

`lit.site.cfg.py` 的生成见 [test/CMakeLists.txt:19-24](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/CMakeLists.txt#L19-L24)，`MAIN_CONFIG` 指明真正干活的配置是源码树的 `lit.cfg.py`：

```cmake
configure_lit_site_cfg(
  ${CMAKE_CURRENT_SOURCE_DIR}/lit.site.cfg.py.in
  ${CMAKE_CURRENT_BINARY_DIR}/lit.site.cfg.py
  MAIN_CONFIG
  ${CMAKE_CURRENT_SOURCE_DIR}/lit.cfg.py
)
```

#### 4.2.4 代码实践

**实践目标**：亲手跑一次 `check-cuda-tile`，并理解拆分构建/测试的工程价值。

**操作步骤**（承接 [u1-l2](u1-l2-repo-and-build.md) 的构建方式）：

1. 配置一个带测试的构建（README 第 137-143 行说明测试默认关闭，必须显式开启）：
   ```bash
   cmake -G Ninja -S . -B build \
     -DCMAKE_BUILD_TYPE=Release \
     -DCUDA_TILE_ENABLE_TESTING=ON \
     -DCUDA_TILE_ENABLE_BINDINGS_PYTHON=ON \
     -DCUDA_TILE_ENABLE_CAPI=ON
   ```
2. 只构建依赖、不跑测试，计时：
   ```bash
   cmake --build build --target check-cuda-tile-build-only
   ```
3. 跑完整测试：
   ```bash
   cmake --build build --target check-cuda-tile
   ```

**需要观察的现象**：第 2 步只编译工具和库、不出现 lit 输出；第 3 步开头会打印 lit 的测试进度（如 `Expected Passes : NNN`），且能区分 Python 测试是否被纳入。

**预期结果**：第 3 步全部通过，输出类似 `Passed NNN tests`。若未启用 Python 绑定，则 Python 相关测试会被 `lit.local.cfg` 跳过，总数变少。若本地无 GPU/未构建，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FileCheck` 和 `not` 必须出现在 `MLIR_TEST_DEPENDS` 里？

**答案**：大量 RUN 行依赖它们（`| FileCheck %s`、`not cuda-tile-translate ...`）。把它们列为依赖，能保证执行测试前这两个 LLVM 工具已被构建出来，否则 lit 会因找不到命令而报错。

**练习 2**：`check-cuda-tile-build-only` 与 `check-cuda-tile` 共享同一份 `MLIR_TEST_DEPENDS`，二者的差别是什么？

**答案**：前者只是 `add_custom_target(DEPENDS ...)`，构建完依赖就结束、不执行 lit；后者是 `add_lit_testsuite`，在依赖构建后还会调起 lit 运行测试套件。前者服务于「构建/测试分离」的 CI 流水线。

### 4.3 lit 配置文件与替换符

#### 4.3.1 概念说明

lit 的配置分两层：

- **`lit.site.cfg.py.in`（模板）**：由 CMake 在配置期处理，把构建期的「绝对路径」与「环境信息」以 `@VAR@` 占位符的形式注入。这一层解决「不同机器、不同构建目录路径不同」的问题。
- **`lit.cfg.py`（运行时配置）**：被前一层加载，定义测试格式、后缀、**替换符**与工具路径。这一层是源码、跨机器稳定。

替换符（substitution）是 lit 的核心机制：RUN 行里的 `%round_trip_test`、`%PYTHON` 等并不是 shell 变量，而是 lit 在执行前用 `config.substitutions` 列表里登记的「字符串→命令」映射替换出来的。这让 RUN 行保持简洁、跨平台一致。

#### 4.3.2 核心流程

配置加载的顺序：

1. lit 从构建目录读 `lit.site.cfg.py`（已由 CMake 从 `.in` 生成）。
2. 该文件把所有 `@VAR@` 已替换为真实路径（工具目录、库目录、Python 解释器、宿主 OS 等），存进 `config.xxx` 字段。
3. 它调用 `lit.llvm.initialize(lit_config, config)` 初始化 LLVM lit 扩展。
4. 再用 `lit_config.load_config(...)` 加载源码树的 `lit.cfg.py`，「让它干真正的活」。
5. `lit.cfg.py` 注册替换符、设置环境变量，至此 RUN 行里的 `%xxx` 都有了展开规则。

#### 4.3.3 源码精读

模板把构建期变量注入见 [test/lit.site.cfg.py.in:5-18](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/lit.site.cfg.py.in#L5-L18)：

```python
config.llvm_tools_dir = "@LLVM_TOOLS_DIR@"
config.llvm_shlib_ext = "@CMAKE_SHARED_LIBRARY_SUFFIX@"
config.host_os = "@HOST_OS@"
config.host_cxx = "@HOST_CXX@"
config.python_executable = "@Python3_EXECUTABLE@"
config.cuda_tile_tool_dir = "@CUDA_TILE_TOOL_DIR@"
config.cuda_tile_obj_root = "@CUDA_TILE_BINARY_DIR@"
config.cuda_tile_install_dir = "@CUDA_TILE_INSTALL_DIR@"
config.enable_bindings_python = @CUDA_TILE_ENABLE_BINDINGS_PYTHON@
```

末尾两步见 [test/lit.site.cfg.py.in:20-24](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/lit.site.cfg.py.in#L20-L24)：先 `initialize`，再加载主配置：

```python
import lit.llvm
lit.llvm.initialize(lit_config, config)
# Let the main config do the real work
lit_config.load_config(config, "@CUDA_TILE_SOURCE_DIR@/test/lit.cfg.py")
```

运行时配置里，`%round_trip_test` 的跨平台分派是亮点，见 [test/lit.cfg.py:42-64](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/lit.cfg.py#L42-L64)：

```python
if platform.system() == "Windows":
    # Windows 用 Python 跑跨平台脚本
    round_trip_script = (
        f'"{python_executable}" "{config.test_source_root}/round_trip_test.py"'
    )
else:
    # Unix/Linux 用 shell 脚本
    round_trip_script = f"{config.test_source_root}/Dialect/CudaTile/round_trip_test.sh"
# ...
config.substitutions.append(("%round_trip_test", round_trip_script))
```

这样，所有 round-trip 测试的 RUN 行只需写 `%round_trip_test %s %t`，平台差异被替换符吸收。

工具路径通过 `add_tool_substitutions` 登记，见 [test/lit.cfg.py:55-61](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/lit.cfg.py#L55-L61)：

```python
tools = [
    "cuda-tile-opt",
    "FileCheck",
    "not",
]
llvm_config.add_tool_substitutions(tools, tool_dirs)
```

`%PYTHON` 替换符不仅指向解释器，还处理 sanitizer 预加载，见 [test/lit.cfg.py:69-86](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/lit.cfg.py#L69-L86)。在 Linux + sanitizer 构建下，会用 `LD_PRELOAD` 预加载 ASAN 运行时（注释链接到 google/sanitizers#1086，这是 Python + ASAN 的已知问题），然后把组合后的命令注册为 `%PYTHON`：

```python
if config.llvm_use_sanitizer and "linux" in config.host_os.lower():
    def preload(lib_name: str) -> str:
        return f"$({config.host_cxx} -print-file-name={lib_name})"
    preload_libs = [preload("libclang_rt.asan.so" if "clang" in config.host_cxx else "libasan.so")]
    preload_path = f'LD_PRELOAD="{" ".join(preload_libs)}"'
    quoted_python_executable = f"{preload_path} {quoted_python_executable}"

config.substitutions.append(("%PYTHON", quoted_python_executable))
```

Python 绑定的 `PYTHONPATH` 也在这一层设置，把构建目录的 `python_packages` 与源码树的 `test/python` 加入搜索路径，见 [test/lit.cfg.py:89-101](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/lit.cfg.py#L89-L101)。这就是为什么 [u10-l2](u10-l2-python-bindings.md) 学过的 `import cuda_tile` 能在测试里直接生效。

#### 4.3.4 代码实践

**实践目标**：在构建产物里找到生成的 `lit.site.cfg.py`，亲眼看见 `@VAR@` 被替换成了真实路径。

**操作步骤**：

1. 完成 [u1-l2](u1-l2-repo-and-build.md) 的构建后，打开 `build/test/lit.site.cfg.py`（构建目录下）。
2. 对照源码模板 `test/lit.site.cfg.py.in`，确认每行 `@...@` 都已被替换为绝对路径（例如 `config.cuda_tile_tool_dir` 应指向 `build/bin` 之类的目录）。
3. 在同一文件里找到末尾对 `lit.cfg.py` 的 `load_config` 调用，确认它指向源码树的 `test/lit.cfg.py`。
4. 回到源码 `test/lit.cfg.py`，定位 `config.substitutions.append(("%round_trip_test", ...))` 与 `("%PYTHON", ...)` 两行，理解 RUN 行里的这两个符号如何被展开。

**需要观察的现象**：构建产物里的 `lit.site.cfg.py` 不再含任何 `@` 占位符；它把「机器相关」的路径集中托管，把「逻辑相关」的规则留给源码 `lit.cfg.py`。

**预期结果**：能指出三个关键替换符（`%round_trip_test`、`%PYTHON`、工具名）分别由 `lit.cfg.py` 的哪几行注册。若尚未构建，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么要把配置拆成 `.in` 模板与 `lit.cfg.py` 两层，而不是全写在一个文件里？

**答案**：机器相关的绝对路径（工具目录、库目录、Python 解释器）必须在配置期由 CMake 注入，否则换一台机器路径就错；而测试逻辑（后缀、替换符、环境变量）是源码、跨机器稳定。拆成两层让「易变部分」隔离在生成产物里，「稳定部分」留在版本控制的源码里。

**练习 2**：`%PYTHON` 在 Linux + ASAN 构建下会比普通解释器多出什么？

**答案**：多出 `LD_PRELOAD=...` 前缀，预加载 `libclang_rt.asan.so`（或 `libasan.so`）。因为 Python 与 AddressSanitizer 一起用时有已知问题，必须预加载运行时才能正确插桩。

### 4.4 round-trip 测试机制与测试 Pass

#### 4.4.1 概念说明

**round-trip（往返）测试**是字节码格式最重要的回归测试：把一段 MLIR 文本序列化成 `.tilebc` 字节码，再反序列化回 MLIR 文本，要求「往返后的文本」与「不走字节码的参考文本」逐行一致。这一招同时检验了写入器（u7-l2）和读取器（u7-l3）——任何一侧出 bug，比对都会失败。

关键在于「参考文本」如何产生。如果直接拿原始输入比对，会因打印格式差异（空白、属性顺序）频繁误报。CUDA Tile 的做法是：让「往返后」的文本和「参考」文本都经过同一个 `cuda-tile-opt` 的规范化打印，再比对。具体地，参考路径用 `cuda-tile-opt` 直接处理原始输入，往返路径则先把输入压成字节码再翻译回来——两条路径都用 `-no-implicit-module` 关闭隐式 `ModuleOp` 包装（呼应 [u7-l1](u7-l1-bytecode-translation.md) 讲过的 `getCudaTileModuleOp` 对裸模块的兼容）。

此外，部分编译器内部工具函数没有面向用户的命令行入口，需要专门的「测试 Pass」来驱动它们。这些 Pass 只在测试构建（`CUDA_TILE_ENABLE_TESTING`）下注册，避免污染发布版工具。

#### 4.4.2 核心流程

round-trip 脚本（以跨平台 Python 版为例）的四步：

1. **序列化**：`cuda-tile-translate -mlir-to-cudatilebc` 把输入 `.mlir` 压成 `.out.tilebc`。
2. **反序列化**：`cuda-tile-translate -cudatilebc-to-mlir` 把字节码翻译回 `.roundtrip.mlir`。
3. **生成参考**：`cuda-tile-opt` 直接处理原始输入，输出 `.ref.mlir`（不走字节码）。
4. **比对**：去掉空行后逐行比较 `.ref.mlir` 与 `.roundtrip.mlir`，不一致则打印 unified diff 并以非 0 退出。

测试 Pass 的注册流程：

1. `test/lib/TestPasses.cpp` 定义仅测试用的 Pass，并在 `registerTransformsUtilsTestPasses()` 里用 `PassRegistration` 注册。
2. `test/lib/CMakeLists.txt` 把它编成 `CudaTileTestPasses` 库。
3. `tools/cuda-tile-opt/cuda-tile-opt.cpp` 在 `#ifdef CUDA_TILE_ENABLE_TESTING` 下声明并调用该注册函数，使测试 Pass 仅在测试构建时挂上 `cuda-tile-opt`。

#### 4.4.3 源码精读

round-trip 脚本的核心是四步命令，见 [test/round_trip_test.py:42-55](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/round_trip_test.py#L42-L55)：

```python
# Step 1: MLIR -> CUDA Tile BC
cmd1 = f"cuda-tile-translate -mlir-to-cudatilebc -no-implicit-module {input_file} -o {tilebc_file}"
# Step 2: CUDA Tile BC -> MLIR
cmd2 = f"cuda-tile-translate -cudatilebc-to-mlir {tilebc_file} -o {roundtrip_file} {extra_flags_str}".strip()
# Step 3: 用 cuda-tile-opt 生成参考
cmd3 = f"cuda-tile-opt {input_file} -no-implicit-module -o {ref_file} {extra_flags_str}".strip()
```

注意第三步是设计精髓：参考文本也由 `cuda-tile-opt` 产生而非直接读原始输入，从而保证两路打印格式同源。比对逻辑在 [test/round_trip_test.py:57-81](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/round_trip_test.py#L57-L81)，先各自剔除空行（等价于 `diff -B`），再比列表是否相等，不等则用 `difflib.unified_diff` 打印差异并以 `sys.exit(1)` 退出。

Linux 版的 shell 脚本逻辑完全一致，只是更精简，见 [test/Dialect/CudaTile/round_trip_test.sh:6-10](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/round_trip_test.sh#L6-L10)，末行用 `diff ... -B` 直接比对：

```bash
cuda-tile-translate -mlir-to-cudatilebc -no-implicit-module $1 -o $2.out.tilebc
cuda-tile-translate -cudatilebc-to-mlir $2.out.tilebc -o $2.roundtrip.mlir $EXTRA_FLAGS
cuda-tile-opt $1 -no-implicit-module -o $2.ref.mlir $EXTRA_FLAGS
diff $2.ref.mlir $2.roundtrip.mlir -B # expect perfect round-trip
```

典型 round-trip 用例见 [test/Bytecode/operationsTest.mlir:1](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/operationsTest.mlir#L1)：

```
// RUN: %round_trip_test %s %t
```

展开后即「把本文件做字节码往返」。该文件 [test/Bytecode/operationsTest.mlir:3-96](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/operationsTest.mlir#L3-L96) 覆盖了 `addi`/`addf`/`constant`/`for`/`assume`/`if`/`store_ptr_tko` 等一大批操作，是字节码读写器的主力回归用例。

测试 Pass 的注册端在 `cuda-tile-opt`，见声明 [tools/cuda-tile-opt/cuda-tile-opt.cpp:21](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-opt/cuda-tile-opt.cpp#L21) 与条件调用（约第 54 行，受 `#ifdef CUDA_TILE_ENABLE_TESTING` 包裹）：

```cpp
void registerTransformsUtilsTestPasses();
// ...
#ifdef CUDA_TILE_ENABLE_TESTING
  mlir::cuda_tile::test::registerTransformsUtilsTestPasses();
#endif
```

`TestPasses.cpp` 里的示例 Pass `TestDebugInfoUpdateSymbolName` 是个 `OperationPass<ModuleOp>`，参数名为 `test-debuginfo-update-symbol-name`，见 [test/lib/TestPasses.cpp:20-39](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/lib/TestPasses.cpp#L20-L39)。它的 `runOnOperation` [test/lib/TestPasses.cpp:61-98](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/lib/TestPasses.cpp#L61-L98) 解析形如 `outer::middle::inner` 的嵌套符号说明符，调用 `cuda_tile` 的工具函数 `updateSymbolName` 完成重命名，从而让变换工具（如 [u9-l4](u9-l4-debuginfo-synth-and-canonicalize.md) 提到的 DI 处理）具备可测的命令行入口。注册函数见 [test/lib/TestPasses.cpp:126-128](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/lib/TestPasses.cpp#L126-L128)：

```cpp
namespace mlir::cuda_tile::test {
void registerTransformsUtilsTestPasses() {
  PassRegistration<TestDebugInfoUpdateSymbolName>{};
}
}
```

该库的构建见 [test/lib/CMakeLists.txt:1-7](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/lib/CMakeLists.txt#L1-L7)，链接 `MLIRPass` 与 `CudaTileTransforms`（后者提供被测工具函数 `updateSymbolName`）：

```cmake
add_mlir_library(CudaTileTestPasses
  TestPasses.cpp
  LINK_LIBS PUBLIC
    MLIRPass
    CudaTileTransforms
)
```

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：亲手新增一个 round-trip 测试用例，让它被 `check-cuda-tile` 收纳并通过。

**操作步骤**：

1. 在 `test/Bytecode/` 下新建文件 `my_first_test.mlir`（注意后缀必须在 `[.mlir, .c, .py]` 白名单内）。
2. 首行写上 round-trip 的 RUN 行：
   ```
   // RUN: %round_trip_test %s %t
   ```
3. 在文件里测试两个你学过的操作。参考 `operationsTest.mlir` 的结构，下面给一个最小示例（**示例代码**，非项目原有）：
   ```
   cuda_tile.module @kernels {
     // 测试 addf（u4-l3 学过）与 constant（u4-l1 学过）
     cuda_tile.entry @my_test() {
       %c = cuda_tile.constant <f32: 1.5> : !cuda_tile.tile<f32>
       %r = cuda_tile.addf %c, %c rounding<nearest_even> : tile<f32>
     }
   }
   ```
   （注意：`constant` 标量浮点的确切属性写法以 `operationsTest.mlir` 为准；若不确定，先模仿该文件里已有的合法写法。）
4. 重新跑测试目标：
   ```bash
   cmake --build build --target check-cuda-tile
   ```

**需要观察的现象**：lit 的输出里出现 `my_first_test.mlir` 这一条；若 round-trip 失败，会在控制台打印出 `.ref.mlir` 与 `.roundtrip.mlir` 的 unified diff，告诉你哪一行不一致。

**预期结果**：新测试通过。若你写的操作语法与项目当前版本不符，`cuda-tile-opt` 会在参考步骤就报解析错误，据此修正语法即可。若尚未本地构建，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：round-trip 的「参考文本」为什么用 `cuda-tile-opt` 处理原始输入，而不是直接读原始输入文件？

**答案**：为了让「往返后」与「参考」两路文本经过同一个打印路径（`cuda-tile-opt` 的输出格式），消除空白、属性顺序等无关差异，只保留真正的语义差异。若直接读原始输入，手写排版与工具打印的排版不一致会引发大量误报。

**练习 2**：`registerTransformsUtilsTestPasses()` 为什么要被 `#ifdef CUDA_TILE_ENABLE_TESTING` 包裹，而不是始终注册？

**答案**：它注册的是仅供测试内部工具函数用的 Pass（如 `test-debuginfo-update-symbol-name`），不应出现在发布版工具里。条件编译确保只有开启测试的构建才会把它挂上 `cuda-tile-opt`，保持发布工具的干净（呼应 [u1-l2](u1-l2-repo-and-build.md) 讲过的测试构建开关与 `TILE_IR_INCLUDE_TESTS` 宏）。

**练习 3**：在 Windows 上，`%round_trip_test` 会展开成什么命令？

**答案**：展开成 `"<python>" "<源码树>/test/round_trip_test.py"`，即用 Python 解释器运行跨平台脚本；而在 Linux/Unix 上则展开成执行 `test/Dialect/CudaTile/round_trip_test.sh` 这个 shell 脚本。两者逻辑等价。

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「从加测试到看结果」的完整闭环：

1. **读 RUN 行，判断类型**：打开 `test/Bytecode/operationsTest.mlir`、`test/Transforms/fuse-fma.mlir`、`test/CAPI/register.c`、`test/python/cuda_tile_public_bindings.py`，分别确认它们走的是 round-trip、`cuda-tile-opt + FileCheck`、可执行程序、pytest 哪条路径，并指出各自依赖的替换符（`%round_trip_test`、工具名、`%PYTHON`）。
2. **追配置链路**：从 `test/CMakeLists.txt` 的 `configure_lit_site_cfg` 出发，找到构建产物 `build/test/lit.site.cfg.py`，确认其中 `config.cuda_tile_tool_dir`、`config.python_executable` 等已被 CMake 注入真实值；再追到源码 `test/lit.cfg.py` 的 `config.substitutions.append(("%round_trip_test", ...))`，说清 RUN 行里的 `%round_trip_test` 最终展开成哪个脚本。
3. **加一个自己的测试**：在 `test/Bytecode/` 下新建一个 `.mlir`，用 `%round_trip_test %s %t` 测试两个你学过的操作（如 [u4-l2](u4-l2-integer-arith.md) 的整数运算与 [u4-l3](u4-l3-float-arith.md) 的浮点运算），模仿 `operationsTest.mlir` 的 `cuda_tile.module`/`cuda_tile.entry` 骨架。
4. **跑目标、读报告**：执行 `cmake --build build --target check-cuda-tile`，确认新测试被纳入并通过；若失败，对照 round-trip 脚本打印的 unified diff 定位是「写入器/读取器」问题还是「自己的语法写错」。

> 提示：若你的测试意外失败且 diff 显示属性拼写差异，多半是某操作的可选属性（如 `rounding<...>`）写法与当前版本不符——回查 `test/Dialect/CudaTile/` 下对应操作的合法用例即可。

## 6. 本讲小结

- CUDA Tile 的测试复用 LLVM 的 **lit + FileCheck** 范式：lit 负责「跑 RUN 行、看退出码」，FileCheck 负责「按期望行匹配输出」；lit 本身不判对错。
- `test/` 按 `.mlir`（`Bytecode`/`Dialect`/`Transforms`）、`.c`（`CAPI`）、`.py`（`python`）三类文件组织，三类走不同运行模型；后缀白名单与排除名单由 `lit.cfg.py` 控制，子目录可用 `lit.local.cfg` 做局部开关。
- **CMake 目标** `check-cuda-tile` 先构建依赖（`FileCheck`/`not`/三个 `cuda-tile-*` 工具/Python 模块/CAPI 程序）再跑 lit；`check-cuda-tile-build-only` 只构建不跑测试，服务于 CI 的构建/测试分离。
- lit 配置分两层：`lit.site.cfg.py.in` 是 CMake 模板、注入机器相关的绝对路径；`lit.cfg.py` 是源码、定义测试格式与替换符。`%round_trip_test`、`%PYTHON`、工具名都在后者注册，平台差异（Windows 用 `.py`、Linux 用 `.sh`）被替换符吸收。
- **round-trip 测试**是字节码格式的核心回归手段：MLIR→字节码→MLIR 后，与用 `cuda-tile-opt` 生成的同源参考文本去空行比对，一次性验证写入器与读取器。
- 仅测试构建注册的 **测试 Pass**（`TestPasses.cpp` → `CudaTileTestPasses` 库 → `cuda-tile-opt` 的 `#ifdef`）为没有命令行入口的编译器内部工具函数提供可测入口，发布版不暴露。

## 7. 下一步学习建议

本讲是「集成、Python 绑定与测试」单元的收尾，也是整本学习手册的工程化尾声之一。建议：

- **横向打通字节码**：回看 [u7-l2](u7-l2-bytecode-writer.md) 与 [u7-l3](u7-l3-bytecode-reader.md)，结合本讲的 round-trip 测试，理解「为什么读写器必须对称——任何不对称都会被 round-trip 抓出来」。可挑一个 `test/Bytecode/invalid/` 下的非法字节码文件，跟踪读取器的报错路径。
- **深入版本兼容测试**：阅读 `test/Bytecode/versioning/` 目录，对照 [u7-l4](u7-l4-bytecode-versioning.md) 的版本模型，理解前向/后向兼容是如何用测试钉死的（注意那里用到只在测试构建启用的 250.0/250.1 版本）。
- **补全 Pass 测试视角**：结合 [u9-l1](u9-l1-passes-and-fusefma.md) 到 [u9-l4](u9-l4-debuginfo-synth-and-canonicalize.md)，体会 `--pass-pipeline` + `FileCheck` 这种「前后对照」测试如何精确描述一个变换 Pass 的行为规格。
- **动手扩展**：若你想为项目贡献，最有价值的入门练习就是——按本讲主实践的步骤，为一个尚未被 round-trip 覆盖的操作补一个 `.mlir` 用例，跑 `check-cuda-tile` 确认通过。这是熟悉整套测试设施最快的方式。
