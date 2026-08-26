# 构建与环境搭建

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 PyPTO「Python 包 + C++ 扩展」双层结构的构建原理：`pip install -e .` 背后发生了什么。
2. 理解 `pyproject.toml`（scikit-build-core 构建后端）与 `CMakeLists.txt`（C++ 编译入口）如何分工衔接。
3. 理解 nanobind 绑定机制：C++ 源码如何被编译成 `pypto_core` 扩展模块，再被纯 Python 层 `pypto` 包裹。
4. 在本地完成开发模式安装（含 CPU-only torch 技巧），并跑通 `tests/ut/core` 单元测试。
5. 掌握本仓库特有的「机器资源限制」纪律：为什么不能用裸的 `-j` / `-n auto`。

## 2. 前置知识

本讲不需要你已经懂编译器，但需要几个基础概念，我们用通俗语言先解释：

- **PEP 517 / PEP 518 —— Python 的构建标准**。老式 Python 项目用 `setup.py` 描述自己怎么构建；现代做法是把「构建工具的声明」写进 `pyproject.toml` 的 `[build-system]` 段。当执行 `pip install .` 时，pip 不会直接编译你的项目，而是先创建一个隔离环境、安装声明里的构建工具，再让「构建后端」去完成真正的工作。
- **scikit-build-core —— 把 CMake 请进 pip 的桥梁**。PyPTO 的核心是 C++ 写的编译器，C++ 项目通常用 CMake 构建。scikit-build-core 就是一个构建后端：pip 调用它，它再去驱动 CMake，最后把 CMake 的产物（一个 `.so` 共享库）塞进 Python 的 wheel（安装包）里。
- **CMake —— C++ 的构建总指挥**。CMake 本身不编译代码，它读取 `CMakeLists.txt` 里的描述（有哪些源文件、用什么编译选项、链接哪些库），生成对应平台的实际构建文件（Linux 上默认是 ninja 或 make 的脚本），然后由后者真正调用 `g++`/`clang++` 编译。
- **nanobind —— C++ 与 Python 的翻译官**。C++ 类和函数无法被 Python 直接调用，需要一层「绑定」代码把 C++ 接口暴露成 Python 模块。nanobind（PyBind11 作者的继任作品）通过在编译期生成胶水代码来完成这件事，产出一个 Python 可直接 `import` 的二进制模块。
- **git submodule（子模块）**。把别的 git 仓库「挂载」到本仓库的某个目录下。PyPTO 把 `msgpack-c`（序列化库）和 `libbacktrace`（崩溃回溯库）以子模块形式放在 `3rdparty/` 下，克隆后需要一条 `git submodule update --init` 才能拿到它们的代码。
- **开发模式安装（editable install）**。`pip install -e .` 不把代码拷贝进 site-packages，而是让 Python 始终「指着」你的源码目录。你改一行 Python 代码，立即生效，无需重装。注意：**C++ 部分改了仍然要重新编译**，开发模式只是免去「重装包」这一步。

一个需要先建立的全局图景（承接上一讲）：

```text
你的 Python 代码
   │  import pypto
   ▼
python/pypto/          ← 纯 Python 层：DSL、jit、runtime（改了立即生效）
   │  from .pypto_core import ...
   ▼
pypto_core.*.so        ← nanobind 编译出的 C++ 扩展（IR、Pass、CodeGen，改了要重编）
   ▲
   │  nanobind_add_module 链接
src/**.cpp + include/pypto/**.h   ← 约 250 个 C++ 源文件
```

本讲要解决的问题就是：**这张图里的 `.so` 是怎么从源码变出来的，以及怎么让它出现在你的 Python 环境里。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pyproject.toml](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/pyproject.toml) | Python 包元数据 + 构建系统声明 + scikit-build 配置 + ruff/pyright 工具配置 |
| [CMakeLists.txt](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt) | C++ 构建总入口：编译选项、依赖、约 250 个源文件的清单 |
| [python/bindings/CMakeLists.txt](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/CMakeLists.txt) | nanobind 扩展模块 `pypto_core` 的构建与安装规则 |
| [python/bindings/bindings.cpp](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/bindings.cpp) | 扩展模块入口：注册所有 C++ 子模块绑定 |
| [python/pypto/__init__.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/__init__.py) | 纯 Python 顶层包，从 `pypto_core` 转载符号 |
| [cmake/msgpack.cmake](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/cmake/msgpack.cmake) / [cmake/libbacktrace.cmake](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/cmake/libbacktrace.cmake) | 两个第三方依赖的引入脚本（子模块自动初始化） |
| [build-constraints.txt](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/build-constraints.txt) | 构建工具链的**钉死版本**（区别于 pyproject 里的兼容区间） |
| [README.md](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md) | 官方安装与测试指引 |
| [AGENTS.md](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/AGENTS.md) | 面向贡献者/AI 协作者的开发约定与推荐命令 |
| [.claude/skills/testing/load-env.sh](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/.claude/skills/testing/load-env.sh) | 机器本地资源限制加载器（构建/测试并行度） |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**4.1 构建声明**（pyproject.toml）→ **4.2 C++ 编译入口**（CMakeLists.txt）→ **4.3 绑定层**（nanobind 与 pypto_core）→ **4.4 测试运行**（pytest 与资源限制）。四个模块恰好对应一次 `pip install -e .` 从声明到验证的完整链路。

### 4.1 pyproject.toml：构建体系的「总说明书」

#### 4.1.1 概念说明

`pyproject.toml` 回答三个问题：

1. **用什么工具构建我？**（`[build-system]` 段）—— 答案是 scikit-build-core，外加 nanobind、ninja、cmake 三个帮手。
2. **我是谁、我依赖谁？**（`[project]` 段）—— 包名 `pypto`、版本、运行期依赖（numpy、torch）。
3. **构建后端的行为开关**（`[tool.scikit-build]` 段）—— Python 包从哪个目录找、CMake 用什么构建类型、构建目录放哪。

一个容易被忽视但很重要的设计：**「兼容区间」与「实际构建版本」是两份文件**。`pyproject.toml` 里写的是「我们兼容哪些版本」，而 [build-constraints.txt](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/build-constraints.txt) 里钉死的是「CI 实际用哪些版本构建」。CI 通过 `PIP_CONSTRAINT` 环境变量应用这份约束，这样同一个 commit 今天和明天构建方式一致。

#### 4.1.2 核心流程

`pip install -e .` 执行时的流程：

```text
pip 读取 pyproject.toml
  │
  ├─ 1. 发现 build-backend = scikit_build_core.build
  │     → 在隔离环境中安装 scikit-build-core、nanobind、ninja、cmake
  │       （版本受 PIP_CONSTRAINT/build-constraints.txt 约束）
  │
  ├─ 2. scikit-build-core 读取 [tool.scikit-build] 段
  │     → 得知：Python 包在 python/pypto、构建目录 build、
  │             构建类型 RelWithDebInfo、额外 CMake 参数
  │
  ├─ 3. scikit-build-core 调用 cmake 配置 + 构建
  │     → 产物是 pypto_core.*.so（见 4.3）
  │
  └─ 4. 以开发模式登记：以后 import pypto 都指向本仓库的 python/pypto/
        （.so 被放进包内，Python 层改动即时生效）
```

#### 4.1.3 源码精读

**构建系统声明**——注明了这是「兼容区间，不是实际构建版本」：

[pyproject.toml:10-20](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/pyproject.toml#L10-L20) 声明构建后端为 scikit-build-core，并要求 nanobind、ninja、cmake。注意 nanobind 被限制在 `>=2.0.0,<3`，[pyproject.toml:11-18](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/pyproject.toml#L11-L18) 的注释解释了原因：nanobind 3.0.0 不再接受本项目用作参数默认值的 null holder，导致 `ir.TileView()` 无法匹配自身的全默认重载。这是一段很有教育意义的注释——它记录了「为什么这个版本上限存在」，避免后人盲目放宽。

**包元数据与运行期依赖**：

[pyproject.toml:30-31](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/pyproject.toml#L30-L31) 要求 Python ≥ 3.10、依赖 `numpy>=2.0` 和 `torch>=2.0.0`。这就是为什么安装 PyPTO 之前建议先单独装 CPU 版 torch——如果让 pip 自己解析 `torch>=2.0.0`，在装有 CUDA 的机器上它会拉取带 CUDA 的巨大 wheel。

**开发依赖与命令行入口**：

[pyproject.toml:47-48](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/pyproject.toml#L47-L48) 注册了一个 `pypto-ir-trace` 命令（IR 降级轨迹报告工具的入口，第 7 单元调试课会用到）。[pyproject.toml:50-58](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/pyproject.toml#L50-L58) 定义 `dev` 附加依赖：pytest 全家桶（含 `pytest-xdist` 并行执行）、pyright 类型检查、ruff 代码风格、clang-tidy。`pip install -e ".[dev]"` 里的 `[dev]` 就是指这段。

**scikit-build 行为开关**：

[pyproject.toml:131-145](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/pyproject.toml#L131-L145) 是理解构建行为的钥匙：

- `wheel.packages = ["python/pypto"]`：纯 Python 包从仓库的 `python/pypto` 目录取。
- `cmake.build-type = "RelWithDebInfo"`：默认「优化 + 带调试符号」，兼顾日常开发速度与可调试性。
- `build-dir = "build"`：CMake 构建目录固定为仓库根的 `build/`，**增量编译**的关键——只改一个 `.cpp` 时，下次构建只重编那一个文件再重新链接。
- `cmake.args` 里打开 `CMAKE_EXPORT_COMPILE_COMMANDS`：生成 `build/compile_commands.json`，供 clangd/clang-tidy 等工具理解每个文件的编译参数。

**钉死的构建工具链版本**：

[build-constraints.txt:21-24](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/build-constraints.txt#L21-L24) 钉死 cmake/nanobind/ninja/scikit-build-core 的具体版本。该文件开头注释记录了一个真实事故：2026-08-24 nanobind 3.0.0 发布后，`nanobind>=2.0.0` 这个无上界区间自动解析到了新版本，所有分支的 CI 同时变红且无从 bisect——「兼容区间防不住这种事，只有钉死输入才能防住」。

#### 4.1.4 代码实践

**实践目标**：不安装任何东西，只通过读文件和 pip 的输出，验证你对构建声明的理解。

**操作步骤**：

1. 打开 [pyproject.toml:10-20](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/pyproject.toml#L10-L20)，记下 `requires` 列表里的四个工具名。
2. 对比 [build-constraints.txt:21-24](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/build-constraints.txt#L21-L24) 的钉死版本，做成一张两列的表：`pyproject 兼容区间` vs `constraints 钉死版本`。
3. 执行 `pip install -e . --dry-run`（或直接正常安装并观察日志），在输出中搜索这四个名字，确认 pip 在「Installing build dependencies」阶段安装了它们。
4. 安装完成后执行 `pip show pypto`，核对 Version 与 [pyproject.toml:24](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/pyproject.toml#L24) 中的 `version` 字段一致。

**需要观察的现象**：pip 日志里出现独立的「构建依赖安装」阶段，且该阶段发生在任何 C++ 编译之前。

**预期结果**：`pip show pypto` 显示 Version `0.1.0`、Location 指向你的仓库路径（开发模式的特征）。dry-run 的确切输出格式随 pip 版本变化，具体日志内容**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pip install -e ".[dev]"` 比 `pip install -e .` 多装东西？多装的东西写在 pyproject.toml 的哪一段？

**答案**：`[dev]` 是「附加依赖组」（extras），定义在 [pyproject.toml:50-58](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/pyproject.toml#L50-L58)，包含 pytest、pytest-xdist、pyright、ruff、clang-tidy 等。它们是开发工具而非运行依赖，所以不放在 `[project].dependencies`——普通用户装包时不需要它们。

**练习 2**：把 `[tool.scikit-build]` 的 `build-dir = "build"` 改成别的目录会怎样？为什么不建议改？

**答案**：功能上仍能构建，但会丢失现有 `build/` 目录里已编译的目标文件，触发全量重编。固定 `build/` 是为了让增量编译生效、让 `compile_commands.json` 路径稳定（clangd 等工具依赖它）。此外 `pyproject.toml:116` 的 ruff 配置对 `build_output/**` 有专门的行宽豁免，构建目录是仓库约定。

**练习 3**：README 建议先执行 `pip install torch --index-url https://download.pytorch.org/whl/cpu` 再装 PyPTO，这解决了什么问题？

**答案**：[pyproject.toml:31](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/pyproject.toml#L31) 只约束 `torch>=2.0.0`，未约束平台变体。先从 PyTorch 官方 CPU 源装 torch，环境中就已有满足版本约束的 torch，pip 不会再解析下载约 2GB 的 CUDA 版 wheel。PyPTO 的单元测试只用 torch 做数值对照，不需要 GPU 版。

### 4.2 CMakeLists.txt：一份脚本服务两种构建

#### 4.2.1 概念说明

根目录的 `CMakeLists.txt` 是 C++ 世界的入口，它有一个精巧设计：**同时服务两种构建模式**。

- **scikit-build 模式**：`pip install` 触发。scikit-build-core 驱动 CMake 时会预定义变量 `SKBUILD_PROJECT_NAME`。
- **原生 CMake 模式**：开发者手动执行 `cmake -B build`。此模式下没有 SKBUILD 变量，脚本改为自己从 `pyproject.toml` 里解析版本号。

为什么要有原生模式？因为日常开发中你只想重编一个 `.so` 放进源码树，而不是每次都走完整的 pip 安装流程——这是本仓库开发循环（AGENTS.md 推荐命令）的常态。

这份文件还承担「源文件清单」的职责：`PYPTO_SOURCES` 变量列出了全部约 250 个 `.cpp` 文件，**新增 C++ 源文件必须手工加进这个清单**，否则不会被编译。

#### 4.2.2 核心流程

CMake 配置阶段（`cmake -B build`）依次做这些事：

```text
1. 判断模式：SKBUILD_PROJECT_NAME 已定义？
     是 → scikit-build 模式，版本由 pip 传入
     否 → 原生模式，从 pyproject.toml 正则解析版本
2. 设定语言标准：C++17 / C11
3. 探测 ccache（编译缓存，存在则挂到编译器启动器）
4. 确定构建类型（默认 RelWithDebInfo）与编译选项
5. find_package(Python) + find_package(nanobind)
6. include cmake/libbacktrace.cmake、cmake/msgpack.cmake
     → 需要时自动 git submodule update --init
7. 定义 PYPTO_SOURCES（约 250 个源文件，按模块分组）
8. add_subdirectory(python/bindings) → 进入 4.3 的绑定构建
9. 若 BUILD_TESTING 且非 wheel 模式 → 附加 tests/ut/cpp 的 C++ 测试
```

关于编译耗时，有一个直观的量级关系：设源文件数为 \( N \)、单文件平均编译耗时 \( c \)、并行度为 \( J \)，则全量构建墙钟时间近似为

\[ T_{\text{full}} \approx \frac{N \cdot c}{J} + T_{\text{link}} \]

\( N \) 约 250，\( c \) 通常为秒级，\( T_{\text{link}} \) 是把所有目标文件链成一个 `.so` 的时间（大项目里往往不可忽略）。这就是为什么本仓库强调固定 `build/` 目录做增量构建、强调用 ccache、并限制并行度 \( J \)（见 4.4）——三者分别砍掉「重编文件数」「单文件耗时」「对机器的冲击」。

#### 4.2.3 源码精读

**双模式判定**：

[CMakeLists.txt:13-29](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L13-L29) 检查 `SKBUILD_PROJECT_NAME` 是否已定义；原生模式分支在 [CMakeLists.txt:19-26](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L19-L26) 用 `file(STRINGS ... REGEX)` 从 `pyproject.toml` 抓取 `version = "..."` 行再正则提取版本号——**版本的唯一事实来源始终是 pyproject.toml**，两套构建不会出现版本漂移。紧接的 [CMakeLists.txt:31](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L31) 把扩展模块名定为 `pypto_core`（Python 侧 `import pypto.pypto_core` 对应的就是它）。

**语言标准与编译缓存**：

[CMakeLists.txt:36-38](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L36-L38) 强制 C++17（`CXX_STANDARD_REQUIRED ON` 意味着不达标直接报错而非降级）。[CMakeLists.txt:41-49](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L41-L49) 探测 ccache 并设为编译器启动器——装了就自动用，没装就跳过，无需配置。

**构建类型与调试信息路径重映射**：

[CMakeLists.txt:52-59](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L52-L59) 定义三种构建类型的编译选项，默认 RelWithDebInfo（`-g -O2`）。[CMakeLists.txt:61-66](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L61-L66) 的 `-fdebug-prefix-map` 是个小而实用的技巧：pip 构建在临时目录进行，DWARF 调试信息里会记录绝对路径；把它重映射为 `.` 后，崩溃回溯显示干净的相对路径。这直接服务于 PyPTO 自己的 backtrace 能力（`src/core/backtrace.cpp`，依赖 4.2.3 下面引入的 libbacktrace）。

**依赖探测与第三方库引入**：

[CMakeLists.txt:69-70](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L69-L70) 用 `find_package` 定位 Python 开发头文件与 nanobind（后者由构建隔离环境提供）。[CMakeLists.txt:77-78](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L77-L78) 引入两个外部依赖脚本：

- [cmake/msgpack.cmake:11-32](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/cmake/msgpack.cmake#L11-L32) 在配置期自动执行 `git submodule update --init`（msgpack-c 缺失时），并在 [cmake/msgpack.cmake:36-43](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/cmake/msgpack.cmake#L36-L43) 以 header-only 接口目标（`INTERFACE` 库）方式接入，用 `MSGPACK_NO_BOOST` 关掉 Boost 依赖。msgpack 用于 IR 的 `.pto` 序列化（第 4 单元 u4-l8）。
- [cmake/libbacktrace.cmake:39-53](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/cmake/libbacktrace.cmake#L39-L53) 用 ExternalProject 在构建期编译 libbacktrace 并创建导入的静态库目标，供错误栈打印使用。

**源文件清单（本讲只需看结构，不需逐行读）**：

[CMakeLists.txt:81-330](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L81-L330) 的 `PYPTO_SOURCES` 用注释分组，本身就是一张「C++ 代码地图」，值得整体扫一遍建立印象：

| 分组注释 | 对应能力 | 后续讲义 |
| --- | --- | --- |
| `# Core` | 错误与回溯基础设施 | — |
| `# IR - Core / Operators / Arith` | IR 节点、算子注册、整数集分析 | 单元 4 |
| `# IR - Transforms` | 全部编译 Pass（第 5 单元的主角） | 单元 5 |
| `# IR - Verifier` | IR 合法性验证器 | u5-l1 |
| `# IR - Serialization` | `.pto` 序列化 | u4-l8 |
| `# Codegen` | PTO 指令与编排代码生成 | 单元 6 |
| `# Backend` | 910B / 950 双后端实现 | u6-l3 |

**进入绑定层与 C++ 测试**：

[CMakeLists.txt:333](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L333) 把控制权交给 `python/bindings` 子目录（见 4.3）。[CMakeLists.txt:337-340](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L337-L340) 只在原生模式下附加 `tests/ut/cpp` 的 C++ 原生测试（目前只有 DSA-RP 求解器一个小测试）——wheel 构建不需要它。

#### 4.2.4 代码实践

**实践目标**：走一遍原生 CMake 配置，逐条读懂 CMake 打印的状态消息，把「配置阶段做了什么」落到具体输出上。

**操作步骤**：

1. 确认子模块已初始化：`git submodule update --init --recursive`（对应 `.gitmodules` 里的三个子模块：libbacktrace、msgpack-c、simpler 运行时）。
2. 执行配置：`cmake -B build -DCMAKE_BUILD_TYPE=RelWithDebInfo`。
3. 完整阅读输出，重点找这几条消息并在笔记里各记一行：
   - `Found ccache: ...` 或 `ccache not found`（来自 [CMakeLists.txt:44-48](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L44-L48)）
   - `Initializing msgpack-c git submodule...` 或 `msgpack-c configured from: ...`（来自 [cmake/msgpack.cmake](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/cmake/msgpack.cmake)）
   - nanobind 与 Python 解释器被找到的记录（来自 [CMakeLists.txt:69-70](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L69-L70)）
4. 配置结束后检查两个产物：`build/CMakeCache.txt`（搜索 `CMAKE_BUILD_TYPE` 确认其值）与 `build/compile_commands.json`（确认存在，并任选一条记录看它记录了什么）。

**需要观察的现象**：CMake 输出按 4.2.2 流程图的顺序推进；`CMakeCache.txt` 里 `CMAKE_BUILD_TYPE` 为 `RelWithDebInfo`。

**预期结果**：配置成功结束，`build/` 下出现上述两个文件。ccache 一行的内容取决于机器是否安装了 ccache，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：CMake 怎么知道当前是 pip 触发的构建而不是你手动 `cmake -B build`？两种模式在「`.so` 装到哪」上有什么差别？

**答案**：scikit-build-core 驱动 CMake 时预定义 `SKBUILD_PROJECT_NAME`，[CMakeLists.txt:13-17](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L13-L17) 据此设 `SKBUILD_MODE=ON`。安装位置的差别由绑定层的 [python/bindings/CMakeLists.txt:72-87](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/CMakeLists.txt#L72-L87) 处理：wheel 模式 `install(TARGETS ... DESTINATION .)` 装进包内；原生模式把输出目录设为 `python/pypto`（源码树内），配合 `PYTHONPATH=$(pwd)/python` 直接使用。

**练习 2**：你在 `src/ir/transforms/` 下新建了 `my_pass.cpp`，编译链接一切正常，但运行时 Python 里找不到对应功能。最可能的原因是什么？

**答案**：忘了把它加进 [CMakeLists.txt:81-330](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L81-L330) 的 `PYPTO_SOURCES` 清单——CMake 不会自动扫描目录，清单外的源文件根本不参与编译。同理，第 5 单元写新 Pass 时改动的就是这份清单。

**练习 3**：`RelWithDebInfo`、`Release`、`Debug` 三种构建类型各适合什么场景？本仓库默认哪一种？

**答案**：对应编译选项定义在 [CMakeLists.txt:57-59](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L57-L59)。`Release`（`-O2 -DNDEBUG`）跑得最快，适合性能测量；`Debug`（`-g -O0`）可精确单步调试且会启用 `PYPTO_DEBUG` 宏（见 4.3.3）；`RelWithDebInfo`（`-g -O2`）兼顾两者，是仓库默认（[pyproject.toml:139](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/pyproject.toml#L139) 与 [CMakeLists.txt:52-54](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L52-L54) 双重默认）。

### 4.3 nanobind 绑定：pypto_core 扩展模块如何诞生

#### 4.3.1 概念说明

4.2 解决了「C++ 怎么编译」，本模块解决「编译出来的东西怎么变成 Python 能 import 的模块」。这需要三层配合：

1. **绑定源文件**（`python/bindings/modules/*.cpp`）：每个文件手写「哪个 C++ 类暴露成哪个 Python 类、哪个方法叫什么名字」。命名约定是 C++ 驼峰转 Python 蛇形（如 `GetValue` → `get_value`）。
2. **模块入口**（`python/bindings/bindings.cpp`）：定义模块名 `pypto_core` 并依次调用各 `Bind*` 函数。
3. **纯 Python 包裹层**（`python/pypto/`）：真正面向用户的 `import pypto`，它从 `pypto_core` 转载符号，并在其上构建 DSL/jit/runtime 等纯 Python 设施。同目录下的 `*.pyi` 类型桩让 IDE 与 pyright 认识这个二进制模块。

这套分层意味着：**改 `python/pypto/` 下的纯 Python 代码不需要重编 C++；改 `src/`、`include/`、`python/bindings/` 下的任何 C++ 都需要重新构建。**

#### 4.3.2 核心流程

```text
nanobind_add_module(pypto_core, 绑定源文件 + PYPTO_SOURCES)
        │  编译约 250 个项目源文件 + 12 个绑定文件
        │  链接 libbacktrace（静态）与 msgpackc-cxx（接口）
        ▼
pypto_core.*.so  （Linux 命名；macOS 为 .so 后缀的动态库）
        │  安装位置由模式决定：
        │    pip/wheel → 装进 wheel 根，随包分发
        │    原生 cmake → 输出到 python/pypto/ 源码树内
        ▼
import pypto
   → python/pypto/__init__.py 执行
   → from .pypto_core import ...（加载 .so，触发导入期自检）
```

#### 4.3.3 源码精读

**扩展模块的构建与链接**：

[python/bindings/CMakeLists.txt:13-26](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/CMakeLists.txt#L13-L26) 列出 12 个绑定源文件（error/core/logging/testing/ir/ir_builder/passes/codegen/backend/arith/functor + 入口 bindings.cpp），随后 [python/bindings/CMakeLists.txt:41-44](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/CMakeLists.txt#L41-L44) 用 `nanobind_add_module` 把它们**连同整个 `PYPTO_SOURCES`** 编进一个名为 `pypto_core` 的模块——注意这不是「库 + 薄绑定」的两段式，而是一次性整体编译链接。

[python/bindings/CMakeLists.txt:66-69](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/CMakeLists.txt#L66-L69) 链接 4.2 引入的 `libbacktrace` 与 `msgpackc-cxx`；[python/bindings/CMakeLists.txt:52-55](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/CMakeLists.txt#L52-L55) 只在 Debug 构建定义 `PYPTO_DEBUG` 宏——它控制 nanobind 的泄漏告警是否开启，避免日常构建被解释器关闭期的误报淹没。

**按模式决定产物去向**：

[python/bindings/CMakeLists.txt:72-87](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/CMakeLists.txt#L72-L87)：wheel 模式安装到包根（含 macOS 的 dSYM 符号处理）；原生模式在 [python/bindings/CMakeLists.txt:83-86](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/CMakeLists.txt#L83-L86) 把 `LIBRARY_OUTPUT_DIRECTORY` 指到 `${CMAKE_SOURCE_DIR}/python/pypto`——这就是 AGENTS.md 里 `export PYTHONPATH=$(pwd)/python` 能生效的原因：`.so` 就躺在源码树的包目录里。

**模块入口与导入期自检**：

[python/bindings/bindings.cpp:29-37](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/bindings.cpp#L29-L37) 用 `NB_MODULE(pypto_core, m)` 定义模块（nanobind 的宏，生成 Python 入口点），随后 [python/bindings/bindings.cpp:40-70](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/bindings.cpp#L40-L70) 依次注册错误、核心类型、IR、IR Builder、Pass、日志、CodeGen、后端、算术化简、IR 访问器等子模块。最值得注意的是 [python/bindings/bindings.cpp:72-77](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/bindings.cpp#L72-L77)：`import` 时立即调用 `OpRegistry` 的两个校验（所有 `tile.*` 算子必须有内存规格；所有原地算子必须声明参数副作用）——**注册表若有缺口，import 直接失败**，而不是等到编译某个内核时才爆。这是「快速失败」设计在构建层的体现。

**纯 Python 层的包裹**：

[python/pypto/__init__.py:19-39](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/__init__.py#L19-L39) 展示了包裹关系：先 import 纯 Python 子模块（`compile_profiling`、`ir`、`language`、`runtime`），再从二进制的 `pypto_core` 转载 `DataType`、错误类型、日志函数、`passes` 等。[python/pypto/__init__.py:42-62](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/__init__.py#L42-L62) 进一步把 `DataType` 枚举成员包装成 `DT_FP16` 这类顺手常量。类型桩位于 `python/pypto/pypto_core/*.pyi`（`ir.pyi`、`passes.pyi` 等），pyright 靠它们对二进制模块做类型检查——这也是本仓库「C++ / 绑定 / 桩三层同步」纪律的第三层。

#### 4.3.4 代码实践

**实践目标**：亲手定位 `.so` 文件，验证「import pypto → 加载 pypto_core → 触发注册表自检」这条链路。

**操作步骤**：

1. 构建完成后，定位二进制模块：
   ```bash
   python -c "import pypto; print(pypto.__file__)"
   python -c "from pypto import pypto_core; print(pypto_core.__file__)"
   ```
2. 确认输出的路径：第一条应指向仓库的 `python/pypto/__init__.py`（或开发模式登记的路径）；第二条应指向 `.so` 文件所在处。对照 [python/bindings/CMakeLists.txt:83-86](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/CMakeLists.txt#L83-L86) 说明它为什么在这个位置。
3. 触发一次导入期自检的「存在性证明」：执行 `python -c "import pypto; print(pypto.DT_FP16)"`，能打印出 DataType 成员即说明 [python/bindings/bindings.cpp:72-77](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/bindings.cpp#L72-L77) 的两个校验已静默通过。
4. 打开 `python/pypto/pypto_core/ir.pyi` 任意看 10 行，体会「桩文件是二进制模块的签名说明书」。

**需要观察的现象**：两条 `__file__` 输出分别落在 Python 层与二进制层；`import pypto` 不产生任何报错。

**预期结果**：`DT_FP16` 正常打印。若你用的是 pip 安装而非源码树 + PYTHONPATH，第二条路径会位于 site-packages 下——两种都算成功，差别只对应 4.3.2 里的两种安装去向。`.so` 的确切文件名带平台后缀（如 `pypto_core.cpython-310-x86_64-linux-gnu.so`），**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `.so` 被直接输出到 `python/pypto/` 目录，而不是 `build/` 里就完事？

**答案**：Python 的 import 机制按包目录查找模块。`pypto` 包位于 `python/pypto/`，`pypto_core` 作为它的子模块必须躺在同一包目录下，`from .pypto_core import ...` 才能找到。[python/bindings/CMakeLists.txt:83-86](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/CMakeLists.txt#L83-L86) 把输出目录设过去，使得 `export PYTHONPATH=$(pwd)/python` 之后无需任何安装步骤即可使用。

**练习 2**：改了 `python/pypto/runtime/runner.py` 之后需要重新编译吗？改了 `src/ir/expr.cpp` 呢？

**答案**：前者不需要——`python/pypto/` 是纯 Python 层，改完即生效（配合开发模式/PYTHONPATH）。后者需要重新执行 `cmake --build build` 重编并重新链接 `pypto_core`，因为 [python/bindings/CMakeLists.txt:41-44](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/CMakeLists.txt#L41-L44) 把 `src/` 源文件直接编进了扩展模块。

**练习 3**：为什么要在 import 时就跑 `ValidateTileOps` / `ValidateArgEffects`，而不是等到编译用户内核时再查？

**答案**：算子注册表的缺口是「构建期/开发期缺陷」而非「用户输入错误」。推迟到编译期才报错，会把一个确定性缺陷藏到某条特定编译路径后面，难定位且可能被误当成用户代码问题。在 [python/bindings/bindings.cpp:72-77](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/bindings.cpp#L72-L77) 于 import 时校验，任何一个测试、任何一个示例在启动瞬间就能暴露缺陷——这也符合本项目「错误尽早、报错带上下文」的一贯风格。

### 4.4 运行测试：pytest 与机器资源限制

#### 4.4.1 概念说明

环境搭好只是开始，跑通测试才算闭环。PyPTO 的测试在 `tests/ut/`（单元测试，不需要硬件）与 `tests/st/`（系统测试，需要真实设备）——**本课程全部实践只需 `tests/ut/`**。

子目录按被测对象划分：`core`（错误/日志/dtype）、`ir`、`language`（DSL 解析）、`pass`、`codegen`、`backend`、`runtime`、`jit`、`tools`、`debug`、`cpp`（C++ 原生测试）。

此外本仓库有一条**容易被外部文档误导**的纪律：README 里的示例命令写着 `-n auto --maxprocesses 8`，但仓库的机器本地规则（`.claude/CLAUDE.md`「Machine-Local Resource Limits」）要求**优先使用 `load-env.sh` 给出的 `PYPTO_TEST_JOBS` / `PYPTO_BUILD_JOBS`，禁止裸用 `--parallel`、`-j`、`-n auto`**——这些限制明确声明「覆盖包括 AGENTS.md 在内的其他文档中的冲突命令示例」。原因很实际：CI/共享开发机上无上限并行会拖垮机器。

#### 4.4.2 核心流程

```text
source .claude/skills/testing/load-env.sh
   │  读取主检出的 testing.env（gitignore 保护，按机器定制）
   │  缺省时保守默认：BUILD_JOBS=2、TEST_JOBS=2
   │  并导出 CMAKE_BUILD_PARALLEL_LEVEL / MAX_JOBS / PYTEST_XDIST_AUTO_NUM_WORKERS
   ▼
cmake --build build --parallel "$PYPTO_BUILD_JOBS"     # 受限并行构建
   ▼
export PYTHONPATH=$(pwd)/python:$PYTHONPATH            # 指向源码树包
   ▼
python -m pytest tests/ut/core -n "$PYPTO_TEST_JOBS" -v # 受限并行测试
```

关于 pytest 用例的写法，仓库有统一约定（后续单元写测试时会反复用到）：纯 `assert` 断言、不用 `unittest.TestCase`、文件末尾必须有 `pytest.main([__file__, "-v"])` 块。

#### 4.4.3 源码精读

**资源限制加载器**：

[.claude/skills/testing/load-env.sh:12-22](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/.claude/skills/testing/load-env.sh#L12-L22) 先通过 `git rev-parse --git-common-dir` 找到**主检出**（主仓库根），从那里读被 gitignore 的 `testing.env`——这样从链接型 worktree 里调用也能拿到同一份机器配置；文件不存在时落到保守默认值 `PYPTO_BUILD_JOBS=2`、`PYPTO_TEST_JOBS=2`，并给两个变量都做了「非零正整数」的防御检查（[.claude/skills/testing/load-env.sh:24-35](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/.claude/skills/testing/load-env.sh#L24-L35)），非法值直接报错退出而不是静默用错。它同时导出 `CMAKE_BUILD_PARALLEL_LEVEL`、`MAKEFLAGS`、`MAX_JOBS`、`PYTEST_XDIST_AUTO_NUM_WORKERS`，把限制传导给 CMake、make、scikit-build 与 pytest-xdist 各个层面。

**推荐命令（AGENTS.md）**：

[AGENTS.md:71-96](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/AGENTS.md#L71-L96) 给出从安装到测试到 lint 的完整命令序列，其中 [AGENTS.md:83-89](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/AGENTS.md#L83-L89) 是核心开发循环：「配置（若未配置）→ 构建 → 设 PYTHONPATH → 测试」。注意其中 `-n auto --maxprocesses 8` 的写法是通用示例，在装载了 load-env.sh 的会话里应以 `-n "$PYPTO_TEST_JOBS"` 替代（理由见 4.4.1）。

**仓库地图（AGENTS.md）**：

[AGENTS.md:101-112](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/AGENTS.md#L101-L112) 一张表说清各目录职责，是本讲「源码地图」的官方版本，也是下一讲（仓库结构）的预习材料。

**测试文件的仓库约定示例**：

[tests/ut/core/test_dtype.py:41-44](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/core/test_dtype.py#L41-L44) 展示了典型结构：测试类分组、方法用 `test_` 前缀、断言用裸 `assert`；文件末尾的 [tests/ut/core/test_dtype.py:451-453](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/core/test_dtype.py#L451-L453) 是强制的 `pytest.main([__file__, "-v"])` 块，让文件可以 `python tests/ut/core/test_dtype.py` 直接运行。

**README 的安装与测试一节**：

[README.md:46-58](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md#L46-L58) 是官方推荐的开发模式安装步骤（含 CPU torch 技巧），[README.md:130-146](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md#L130-L146) 是官方测试指引。

#### 4.4.4 代码实践

**实践目标**：按仓库纪律跑通 `tests/ut/core`，并对比「受限并行」与官方 README 命令的差异。

**操作步骤**：

1. 加载资源限制并确认生效：
   ```bash
   source .claude/skills/testing/load-env.sh
   echo "BUILD=$PYPTO_BUILD_JOBS TEST=$PYPTO_TEST_JOBS"
   ```
2. 指向源码树并运行核心测试：
   ```bash
   export PYTHONPATH=$(pwd)/python:$PYTHONPATH
   python -m pytest tests/ut/core -n "$PYPTO_TEST_JOBS" -v
   ```
3. 再单独跑一个文件，体会粒度控制（单文件可不加 `-n`）：
   ```bash
   python -m pytest tests/ut/core/test_dtype.py -v
   ```
4. 阅读输出：统计通过/跳过的数量；若个别用例 skip，读其跳过原因（通常与可选依赖如 Simpler 运行时有关，`tests/ut/conftest.py` 里有对应的 fixture 处理）。

**需要观察的现象**：`tests/ut/core` 下 4 个测试文件（test_error、test_dtype、test_logging、test_compile_profiling）全部通过；xdist 输出显示的 worker 数等于 `PYPTO_TEST_JOBS` 而非 CPU 核数。

**预期结果**：全绿。确切的用例总数与是否出现 skip 取决于环境（例如是否装有 torch 的具体版本），**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`load-env.sh` 为什么从 `git rev-parse --git-common-dir` 推导主检出，而不是直接读当前目录下的 `testing.env`？

**答案**：见 [.claude/skills/testing/load-env.sh:10-14](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/.claude/skills/testing/load-env.sh#L10-L14) 的注释——同一仓库可能同时有多个链接型 worktree，机器资源限制是「机器级」属性而非「检出级」属性，各 worktree 应共享主检出里的同一份 `testing.env`；直接读当前目录会在 worktree 里静默失去限制。

**练习 2**：`python -m pytest tests/ut/core -n "$PYPTO_TEST_JOBS"` 里，`-n` 是哪个插件提供的？不装 dev 依赖直接这样跑会发生什么？

**答案**：`-n` 来自 `pytest-xdist`（并行执行插件），声明在 [pyproject.toml:50-58](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/pyproject.toml#L50-L58) 的 dev 附加依赖里（另有 `pytest-forked`）。没装它时 pytest 会报「无法识别的参数 `-n`」。去掉 `-n "$PYPTO_TEST_JOBS"` 即可退化为串行执行，结果同样有效，只是更慢。

**练习 3**：为什么 `tests/ut/` 能在没有 AI 加速器硬件的机器上运行？

**答案**：单元测试只验证「编译器自身」的行为——IR 构造、Pass 变换、DSL 解析、错误处理，这些全部在 host 侧完成，不需要把内核下发到设备。需要真实设备的是 `tests/st/`（系统测试，见 [README.md:146](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/README.md#L146) 的指引）；数值正确性对照则由 torch 在 CPU 上完成。这也是本课程全部实践可以离线进行的原因。

## 5. 综合实践

**任务：从零搭建环境，完成一次「安装 → 定位产物 → 跑通测试 → 记录 CMake 行为」的完整闭环，并产出一份构建档案。**

前置：一台 Linux/macOS 机器，Python ≥ 3.10，C++17 编译器（GCC/Clang），CMake ≥ 3.15，网络可访问 PyPI 与 GitHub。

**步骤一：克隆与子模块**

```bash
git clone https://github.com/hw-native-sys/pypto.git
cd pypto
git submodule update --init --recursive
```

执行 `cat .gitmodules` 核对三个子模块（libbacktrace、msgpack-c、simpler 运行时）都已就位。

**步骤二：安装依赖与开发模式包**

```bash
# 先装 CPU-only torch，避免拉取 CUDA 依赖（对应 README.md:56-58）
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -e ".[dev]"
```

安装过程会触发 scikit-build-core 驱动的完整 C++ 构建，耗时视机器而定（数量级参考 4.2.2 的公式，全量构建通常以分钟计）。

**步骤三：记录 CMake 做了哪些事（本实践的核心产出）**

构建日志里逐条找出并记录以下事件，每条注明它对应本讲哪个源码位置：

| 要找的日志/证据 | 对应源码位置 |
| --- | --- |
| 构建依赖（scikit-build-core/nanobind/ninja/cmake）被安装 | [pyproject.toml:10-20](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/pyproject.toml#L10-L20) |
| ccache 被发现/未发现 | [CMakeLists.txt:41-49](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L41-L49) |
| msgpack-c 子模块初始化/配置 | [cmake/msgpack.cmake:11-45](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/cmake/msgpack.cmake#L11-L45) |
| libbacktrace 作为外部项目编译 | [cmake/libbacktrace.cmake:39-53](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/cmake/libbacktrace.cmake#L39-L53) |
| 约 250 个 `.cpp` 被编译进 `pypto_core` | [CMakeLists.txt:81-330](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L81-L330) + [python/bindings/CMakeLists.txt:41-44](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/CMakeLists.txt#L41-L44) |
| 最终链接出 `.so` | [python/bindings/CMakeLists.txt:66-69](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/CMakeLists.txt#L66-L69) |

**步骤四：验证双层层级**

```bash
python -c "import pypto; print(pypto.__file__)"
python -c "from pypto import pypto_core; print(pypto_core.__file__)"
python -c "import pypto; print(pypto.DT_FP16)"
```

三条命令分别对应 4.3 的三层：Python 包入口、二进制模块、导入期自检后的符号。

**步骤五：按仓库纪律跑通核心测试**

```bash
source .claude/skills/testing/load-env.sh
export PYTHONPATH=$(pwd)/python:$PYTHONPATH
python -m pytest tests/ut/core -n "$PYPTO_TEST_JOBS" -v
```

**验收标准（自查清单）**：

- [ ] `pip show pypto` 的 Location 指向你的克隆目录
- [ ] 构建档案表格至少填满 5 行，且能说出每行对应的源码文件
- [ ] 三条 `__file__`/符号命令全部成功，且能解释两个路径为何不同
- [ ] `tests/ut/core` 全部通过（或有明确可解释的 skip）
- [ ] 能回答：「改 `python/pypto/language/` 下的文件要不要重编？改 `src/` 呢？」（答案在 4.3.5 练习 2）

## 6. 本讲小结

- PyPTO 是「纯 Python 层 + nanobind 编译的 `pypto_core` 扩展」双层结构：前者改完即生效，后者必须重新构建；构建出口是 [python/bindings/CMakeLists.txt](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/CMakeLists.txt)。
- [pyproject.toml](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/pyproject.toml) 声明构建后端（scikit-build-core + nanobind + cmake + ninja），「兼容区间」与 [build-constraints.txt](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/build-constraints.txt) 的「钉死版本」分工：前者管兼容，后者管可复现。
- 根 [CMakeLists.txt](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt) 一份脚本双模式运行（pip 的 scikit 模式 / 手动原生模式），版本号唯一来源是 pyproject.toml；新增 C++ 源文件必须登记进 `PYPTO_SOURCES`。
- 第三方依赖 msgpack-c（header-only 接口库）与 libbacktrace（ExternalProject 静态库）由 `cmake/*.cmake` 在配置期自动初始化子模块并接入。
- `import pypto` 的瞬间会执行算子注册表自检（[python/bindings/bindings.cpp:72-77](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/bindings/bindings.cpp#L72-L77)）——快速失败是本项目的一贯设计取向。
- 本仓库的构建/测试并行度由 `.claude/skills/testing/load-env.sh` 按机器约束（默认 2），会话中应使用 `$PYPTO_BUILD_JOBS` / `$PYPTO_TEST_JOBS` 而非裸 `-j` / `-n auto`。

## 7. 下一步学习建议

下一讲（u1-l3「仓库结构与三层架构」）将把本讲接触过的目录扩展成完整的仓库地图：`include/pypto` 与 `src` 的 C++ 核心、`python/bindings` 绑定层、`python/pypto` Python API 层各自的内部组织，以及 `docs/`、`examples/`、`tests/` 的阅读入口。

在进入下一讲之前，建议先做两个热身阅读：

1. 通读 [AGENTS.md:101-112](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/AGENTS.md#L101-L112) 的仓库地图，对照你在综合实践中实际到访过的目录。
2. 把 [CMakeLists.txt:81-330](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/CMakeLists.txt#L81-L330) 的分组注释当作「C++ 模块索引」扫一遍——第 4、5、6 单元会按这张表逐块深入，现在混个眼熟，后面每讲都能对号入座。
