# 安装、运行与包目录结构

## 1. 本讲目标

上一篇我们认识了 Tilus 是什么——一种「线程块级、张量为一等公民」的 GPU 内核 DSL。本讲不写内核，而是解决一个更现实的问题：**把 Tilus 装到本机能跑起来，并建立一张「源码在哪里」的整体地图。**

学完本讲，你应当能够：

- 用 `pip` 完成 Tilus 的安装，并知道在什么情况下需要额外约束 `cuda-python` 版本。
- 说出 `python/tilus/` 下每一个顶层目录（`lang`、`ir`、`transforms`、`backends`、`drivers.py`、`runtime`、`kernels`、`utils` 等）各自承担什么职责。
- 用 `tilus.option` 设置缓存目录、打开 IR dump 等全局选项，并理解这些选项如何影响编译与调试。

## 2. 前置知识

- **包（package）**：Python 用目录组织代码，一个顶层目录加上 `__init__.py` 就是一个包。Tilus 的顶层包叫 `tilus`。
- **依赖（dependency）**：一个项目运行所需要的外部库。Tilus 的依赖写在 `pyproject.toml` 里。
- **target（编译目标）**：我们编译出的 CUDA 代码最终要在某款 GPU 上运行。Tilus 用一个 `Target` 对象描述「为哪块卡编译」（例如 Ampere 的 `sm80`、Hopper 的 `sm90a`、Blackwell 的 `sm100a`）。本讲会看到 Tilus 如何在运行时自动探测你的卡并选择 target。
- **缓存（cache）**：Tilus 每次把内核编译成 `.so` 都要调用 `nvcc`，很慢。所以它会把编译结果按内容哈希缓存，下次直接复用。这一讲我们先了解「缓存目录在哪、怎么设」，缓存机制的细节留到后续进阶讲义。

承接上一篇：Tilus 站在 **Hidet** 的肩膀上（Hidet 提供低层 IR 与运行时），所以你会看到 Tilus 包里直接内嵌了一份 `hidet` 子包。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pyproject.toml](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/pyproject.toml) | 项目元信息、依赖、开发依赖、构建配置（setuptools + setuptools_scm）。 |
| [README.md](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/README.md) | 顶层说明，包含安装方式与 `cuda-python` 兼容性提示。 |
| [python/tilus/__init__.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/__init__.py) | 包入口，汇总导出数据类型、张量、`Script`、`option` 等公共 API。 |
| [python/tilus/option.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py) | 全局选项：`cache_dir`、`debug.dump_ir`、`bench_*` 等。 |
| [python/tilus/target.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/target.py) | 硬件 target 抽象与自动探测。 |
| [python/tilus/version.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/version.py) | `__version__` 的来源。 |
| [python/tilus/drivers.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py) | 编译流水线入口 `build_program`，以及缓存目录解析 `get_cache_dir`。 |
| [.github/workflows/tests.yaml](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/.github/workflows/tests.yaml) | CI 配置，展示了「官方推荐」的安装与测试命令。 |

---

## 4. 核心概念与源码讲解

### 4.1 安装与依赖

#### 4.1.1 概念说明

Tilus 是一个标准的 Python 包，发布在 PyPI 上，名字就叫 `tilus`。安装它最简单的方式是一行 `pip install tilus`。但因为 Tilus 要调用 CUDA、要和 GPU 驱动打交道，所以有两个细节值得一开始就讲清楚：

1. **它需要一块 NVIDIA GPU**：Tilus 的编译与运行都假定有 CUDA 环境。后续你会看到，连「探测当前 target」这一步都需要 `torch.cuda`。
2. **`cuda-python` 的版本要与驱动匹配**：Tilus 依赖 `cuda-python`，新版的 `cuda-python` 对 GPU 驱动版本有要求。如果你的驱动较旧，就需要手动约束 `cuda-python` 的版本。

#### 4.1.2 核心流程

安装路径有两条：

- **从 PyPI 安装（推荐给使用者）**：
  ```
  pip install tilus
  ```
  若驱动旧于 `580.65.06`，改用：
  ```
  pip install tilus "cuda-python<13"
  ```

- **从源码安装（推荐给阅读源码/二次开发者）**：在仓库根目录执行 `pip install ".[dev]"`，其中 `[dev]` 会额外装上 ruff、mypy、pytest、pre-commit 等开发工具。CI 里就是这么做的。

> 注意：README 明确写了「`pip install tilus`」是入门方式，而 `cuda-python<13` 是针对旧驱动的兜底方案，这是它和普通 Python 包最大的不同点。

#### 4.1.3 源码精读

先看 README 的安装小节，确认官方安装指令与兼容性提示：

[README.md:24-34](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/README.md#L24-L34) — 安装方式与「旧驱动需约束 `cuda-python<13`」的 NOTE。第 25–28 行是基本的 `pip install tilus`，第 30–34 行是兼容性兜底。

再看 `pyproject.toml`，这是 Tilus 的「身份证」。注意三点：版本号是**动态生成**的、要求 Python ≥ 3.10、有一份核心依赖清单：

[pyproject.toml:5-12](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/pyproject.toml#L5-L12) — `name = "tilus"`、`dynamic = ["version"]`（版本号不在文件里硬编码）、`requires-python = ">=3.10"`。`keywords` 也透露了它的定位：`GPU / Compiler / CUDA / hidet / tensor / torch`。

[pyproject.toml:28-35](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/pyproject.toml#L28-L35) — 运行时核心依赖。可以看到 Tilus 依赖 `numpy`、`torch`（张量与 GPU 探测）、`tabulate`、`tqdm`、`filelock`（缓存锁）、`apache-tvm-ffi`。注意这里**没有直接列出 `cuda-python`**——它是作为 `torch`/cuda 生态的传递依赖进来的，所以 README 才单独提醒它。

[pyproject.toml:37-59](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/pyproject.toml#L37-L59) — 可选的 `dev` 依赖组：`ruff==0.11.0`（格式/ lint）、`mypy==1.15.0`（类型检查）、`pytest`、`pre-commit`、`sphinx`（文档）等。`pip install ".[dev]"` 会装上它们。

版本号「动态」是怎么来的？靠的是 `setuptools_scm`，它根据 **git tag** 自动算出语义化版本：

[pyproject.toml:65-68](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/pyproject.toml#L65-L68) — `[tool.setuptools_scm]` 启用基于 git 的版本生成；`package-dir = "python"` 说明真正的包代码在 `python/tilus/`，而不是仓库根目录。

包入口最终会把版本号暴露成 `tilus.__version__`，它来自 `version.py`：

[python/tilus/version.py:15-17](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/version.py#L15-L17) — `__version__ = version("tilus")`，通过 `importlib.metadata` 从已安装的发行版元数据里读取版本号。所以 `__version__` 只有在「真正安装后」才有效。

CI 文件 `.github/workflows/tests.yaml` 给出了「官方推荐」的从源码安装与冒烟测试流程，值得作为本地环境搭建的参照：

[.github/workflows/tests.yaml:86-97](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/.github/workflows/tests.yaml#L86-L97) — CI 在 Python 3.10 / 3.11 / 3.12 / 3.13 四个版本下分别建虚拟环境、`pip install ".[dev]"`，再用 `pytest tests/kernels/matmul/test_matmul_v2.py` 做冒烟测试。这告诉你两件事：① Tilus 支持的 Python 版本范围；② 装好后跑通的最小验证用例是 `test_matmul_v2.py`（CLAUDE.md 也建议大型重构后先用它做冒烟）。

> 顺带一提，CI 跑在 `nvidia/cuda:13.0.0-devel` 容器里（见 [tests.yaml:46-52](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/.github/workflows/tests.yaml#L46-L52)），说明开发态需要带 `nvcc` 的 CUDA 工具链。

#### 4.1.4 代码实践：确认环境可用（安装后第一步）

**实践目标**：安装 Tilus 后，验证「能 import、版本号正确、能探测到当前 GPU target」。

**操作步骤**：

1. 安装：`pip install tilus`（或源码 `pip install ".[dev]"`）。
2. 新建 `check_env.py`，内容如下（**示例代码**）：

   ```python
   import tilus

   print("tilus.__version__ =", tilus.__version__)
   print("current target   =", tilus.get_current_target())
   ```

3. 运行：`python check_env.py`。

**需要观察的现象**：

- `tilus.__version__` 应打印出一个形如 `0.2.0` 的版本号（来自 git tag，由 `setuptools_scm` 生成）。
- `get_current_target()` 应打印 `nvgpu/sm80`、`nvgpu/sm90a`、`nvgpu/sm100a` 这类字符串，对应你的卡（Ampere / Hopper / Blackwell）。

**预期结果 / 注意事项**：

- 若机器**没有 NVIDIA GPU**，`get_current_target()` 会抛出 `RuntimeError("No GPU is available.")`——这是正常行为，说明 Tilus 必须在 CUDA 环境里用（详见 4.3 节）。这一点**待本地验证**：是否报错取决于你机器上是否有可用 GPU。
- 若提示找不到版本号，多半是没有「真正安装」（仅 `PYTHONPATH` 指向源码时 `importlib.metadata` 查不到发行版），用 `pip install -e .` 做可编辑安装可解决。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pyproject.toml` 里写的是 `dynamic = ["version"]`，而不是写死 `version = "0.2.0"`？
**参考答案**：因为 Tilus 用 `setuptools_scm` 从 git tag 自动推导版本号，避免人工同步维护版本字符串；发布时打 tag 即可得到对应版本。

**练习 2**：README 为什么要单独提醒 `pip install tilus "cuda-python<13"`？
**参考答案**：新版 `cuda-python` 要求较新的 GPU 驱动（≥ `580.65.06`）。旧驱动环境下，必须把 `cuda-python` 钉到 `<13` 才能正常加载 CUDA 运行时。

---

### 4.2 顶层包目录职责

#### 4.2.1 概念说明

Tilus 是一个**编译器**项目：它把你用 Python 写的内核（Tilus Script），经过若干层 IR 和变换，最终编译成 CUDA C、再编成 `.so`。这种「源码 → IR → 变换 → 后端 → 运行时」的结构，会直接反映在包目录的划分上。理解目录划分，等于理解了项目的整体架构。

真正的代码都住在 [python/tilus/](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus) 下（因为 `pyproject.toml` 里 `package-dir = "python"`）。

#### 4.2.2 核心流程

从「你写代码」到「内核跑起来」，数据依次流经这些目录：

```
用户写的 Tilus Script
      │   （lang/ 把 Python AST 转译成 IR）
      ▼
   ir/   ← Tilus IR（Program/Function/Stmt/Inst/Tensor/Layout）
      │   （transforms/ 在 IR 上做优化/降级/布局推理）
      ▼
  drivers.py  ← build_program：编排整条流水线
      │   （backends/ 把 IR 生成 Hidet IR → CUDA C）
      ▼
   .so 文件
      │   （runtime/ 加载 .so 并按 grid/warps 启动）
      ▼
  GPU 上运行
```

#### 4.2.3 源码精读

包入口 `__init__.py` 把「最常用的东西」直接挂在 `tilus.` 下。先看它导出了哪些公共 API，你就能知道这个库对外暴露的面有多大：

[python/tilus/__init__.py:98-106](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/__init__.py#L98-L106) — 这几行把核心构造重导出为顶层名字：`RegisterLayout`/`SharedLayout`（布局）、`RegisterTensor`/`SharedTensor`/`GlobalTensor`（三种张量）、`InstantiatedScript`/`Script`/`autotune`（编程模型）、`Pipeline`（软件流水线）、以及 `empty`/`from_torch`/`zeros`/`ones` 等张量工厂函数。这些正是后续讲义的主角。

[python/tilus/__init__.py:108-110](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/__init__.py#L108-L110) — 这一行很关键：`from . import kernels, logging, option, utils`，再加上 `get_current_target` 与 `__version__`。也就是说 `tilus.option.xxx`、`tilus.utils.xxx`、`tilus.kernels.xxx`、`tilus.get_current_target()`、`tilus.__version__` 都是顶层可用的。

（前面 [python/tilus/__init__.py:16-97](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/__init__.py#L16-L97) 还导出了一大堆数据类型，从 `float16`/`int8` 到 `f4e2m1`/`int4b` 这种任意位宽低精度类型，呼应了 u1-l1 讲过的「1–8 bit 低精度」卖点。）

下表给出各顶层目录/文件的职责（按数据流顺序排列）：

| 路径 | 职责 | 你会在哪一讲深入 |
| --- | --- | --- |
| `lang/` | **用户编程面**：`Script`/`InstantiatedScript`（写内核）、`instructions/`（`self.*` 上的指令与指令组）、`transpiler/`（把 `__call__` 的 Python AST 转成 IR）、`constructs/`、`classes/`、`methods/` | U1-L3、U2、U3-L2 |
| `ir/` | **Tilus IR**：`prog.py`/`func.py`/`stmt.py`/`inst.py`/`tensor.py`（IR 节点）、`layout/`（布局系统，Tilus 的核心创新）、`functors/`（访问者/重写器）、`tools/`（verify/printer/collector）、`builders/`、`analyzers/` | U3-L3~L5、U4 |
| `transforms/` | **IR 变换（Pass）**：`base.py`（Pass 框架）、`layout_inference.py`（布局推理）、`dead_code_elimination.py`、`lower_*.py`（降级）、`bound_aware_simplify.py`、`instruments/` | U5 |
| `backends/` | **后端代码生成**：`codegen.py`（IR→Hidet IR）、`emitter.py`（发射器注册表）、`emitters/`（各指令的 CUDA 发射器）、`contexts/`（编译期内存分配/同步状态） | U6 |
| `drivers.py` | **编译流水线编排**：`build_program` 串起 verify→optimize→lower→codegen→nvcc 六大阶段 | U3-L1 |
| `runtime/` | **运行时**：`compiled_program.py` 加载 `.so`、把 torch 张量映射成指针、按 metadata 启动内核 | U8-L3 |
| `kernels/` | **内置算子库**：目前含 `cast.py` 等，是「用 Tilus 写好的现成算子」 | U1-L4 |
| `utils/` | **工具集**：`bench_utils.py`（基准测试）、`multiprocess.py`（并行调优）、`profiler/`（ncu/nsys 剖析）、`cuda_sanitizer.py`、`cache_utils.py` | U8-L2/L4 |
| `testing/` | 测试辅助工具 | — |
| `hidet/` | **内嵌的 Hidet**：提供低层 IR 目标与运行时（u1-l1 提到的传承关系） | 贯穿后端讲义 |
| `target.py` | 硬件 **target** 抽象与自动探测 | 本讲 4.3、U7 |
| `tensor.py` | 顶层**张量工具**：`from_torch`/`view_torch`/`empty`/`ones`/`zeros` 等 | U8-L3 |
| `option.py` | **全局选项** | 本讲 4.3 |
| `version.py` | `__version__` | 本讲 4.1 |
| `logging.py` | 日志 | — |

> 记住一条主线：**`lang`（写）→ `ir`（表示）→ `transforms`（改）→ `drivers`（编排）→ `backends`（生成）→ `runtime`（跑）**。后面所有讲义都围绕这条线展开。

#### 4.2.4 代码实践：用目录定位一段功能

**实践目标**：建立「看到一个 API，能猜到它住在哪个目录」的直觉。

**操作步骤**：

1. 在终端进入仓库根目录，分别查看几个关键目录的一级内容：
   ```bash
   ls python/tilus/lang
   ls python/tilus/ir
   ls python/tilus/backends
   ```
2. 打开 [python/tilus/__init__.py:108](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/__init__.py#L108) 这一行，确认 `kernels`、`option`、`utils` 是被「作为子模块」导入的，因此你可以用 `tilus.option.cache_dir(...)` 而无需手动 `import tilus.option`。

**需要观察的现象**：

- `lang/` 下能看到 `script.py`（Script 类）、`instantiated_script.py`、`instructions/`、`transpiler/` 等子项，与你后续要读的 U1-L3、U2 讲义对得上。
- `ir/` 下能看到 `prog.py`、`func.py`、`stmt.py`、`tensor.py`、`layout/`，对应 IR 的核心节点。
- `backends/` 下能看到 `codegen.py`、`emitter.py`、`emitters/`、`contexts/`。

**预期结果**：目录结构与上表一致。若你看到的子项略有出入（比如新增了文件），那是项目演进所致，主目录划分通常稳定。

#### 4.2.5 小练习与答案

**练习 1**：如果我想给 Tilus 增加一条「把无用指令删掉」的编译优化，应该改哪个目录？
**参考答案**：`transforms/`。该目录专门放 IR 变换（Pass），其中 `dead_code_elimination.py` 正是做这件事的。

**练习 2**：`tilus.from_torch` 和 `tilus.get_current_target` 为什么能直接从顶层 `tilus` 包用，而不需要写完整路径？
**参考答案**：因为 `__init__.py` 在 [L106](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/__init__.py#L106) 把 `from_torch` 等函数从 `tensor` 模块导入、又在 [L109](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/__init__.py#L109) 把 `get_current_target` 导入到了包命名空间。

---

### 4.3 option 全局选项（cache_dir / dump_ir 等）

#### 4.3.1 概念说明

Tilus 有一些「全局生效」的开关型配置，统称为 **option**。它们不是每次调用时传参，而是在进程级别设置一次，影响后续所有编译。最常用的几个：

- **`cache_dir`**：编译产物（`.so`、生成的 `source.cu`、dump 的 IR）存到哪个目录。
- **`debug.dump_ir`**：是否在每个 Pass 之后把 IR 转储到缓存目录（调试利器）。
- **`debug.disable_ptxas_opt`**：是否关闭 `ptxas` 优化、保留可读 PTX（调试利器）。
- **`bench_warmup` / `bench_repeat`**：自动调优时基准测试的预热与重复次数。
- **`parallel_workers`**：并行编译/调优的 worker 数。

这些选项底层复用了 Hidet 的选项机制（`tilus.hidet.option`），所以你会看到 `option.py` 里大量 `_hidet_option` 调用。

#### 4.3.2 核心流程

设置一个选项的典型流程：

1. 进程启动时，`option.py` 在导入阶段调用 `_register_options()`，把所有选项注册进 Hidet 的选项表，并给定**默认值**。
2. 用户代码里调用形如 `tilus.option.cache_dir("/tmp/my-cache")`，它转调 `set_option`，把「进程级」的值改掉。
3. 后续每次编译时，`drivers.get_cache_dir` 等代码通过 `tilus.option.get_option("cache_dir")` 读回当前值。

> 默认缓存目录的取法很巧妙：如果 Tilus 装在某个 git 仓库里，默认就用仓库根下的 `.cache/`；否则用 `~/.cache/tilus`。开发时因此能很方便地在项目里看到缓存产物。

#### 4.3.3 源码精读

先看选项是怎么注册的，以及默认缓存目录的推导逻辑：

[python/tilus/option.py:24-48](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L24-L48) — `_get_default_cache_dir()`：用 `git rev-parse --is-inside-work-tree` 判断当前文件是否在 git 仓库内；是则取 `parents[2]/.cache`（即仓库根的 `.cache/`），否则取 `~/.cache/tilus`。这就是为什么你在 Tilus 仓库里跑内核后，会在仓库根看到一个 `.cache/` 目录。

[python/tilus/option.py:51-94](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L51-L94) — `_register_options()` 注册了全部选项。重点关注：
- [L53-59](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L53-L59) `tilus.cache_dir`，带环境变量 `TILUS_CACHE_DIR`——意味着你也可以用环境变量而非代码来设置它。
- [L66-72](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L66-L72) `tilus.debug.dump_ir`，环境变量 `TILUS_DUMP_IR`。
- [L73-79](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L73-L79) `tilus.debug.disable_ptxas_opt`。
- 末尾 [L94](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L94) `_register_options()` 在模块导入时立即执行——所以 `import tilus` 之后选项就已经可用了。

再看你最常用的两个设置函数：

[python/tilus/option.py:114-123](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L114-L123) — `cache_dir(dir_path)`：接受 `str` 或 `Path`，转成字符串后写入 `tilus.cache_dir` 选项。这就是 CLAUDE.md 里建议「开发期用 `tilus.option.cache_dir(...)` 指定缓存目录以便检查生成的 `source.cu`」的那个函数。

[python/tilus/option.py:162-187](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L162-L187) — `debug` 命名空间类：`debug.dump_ir(enable=True)`（[L163-173](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L163-L173)）打开逐 Pass IR 转储；`debug.disable_ptxas_opt(enabled=True)`（[L175-187](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L175-L187)）让 `ptxas` 用 `-O0` 关闭优化，便于阅读生成的 PTX。

> 小贴士：所有「读」都走 `get_option`（[L97-111](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L97-L111)），它会自动给名字加上 `tilus.` 前缀。所以代码里既写 `cache_dir(...)`，也写 `get_option("cache_dir")`，两者底层都是 `tilus.cache_dir` 这一项。

那么 `cache_dir` 在编译时被谁读取呢？看 `drivers.py` 的 `get_cache_dir`：

[python/tilus/drivers.py:193-244](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L193-L244) — 它把 `BuildOptions` + `disable_ptxas_opt` + `current target` 拼成 `options_text`，把程序文本 `str(prog)` 拼成 `prog_text`，再做 `sha256(options_text + prog_text)` 取前 12 位作为哈希，最终落到 `<cache_dir>/programs/<hash>/`（[L221-224](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L221-L224)）。这里读取的就是 `tilus.option.get_option("cache_dir")`。也就是说，**改 `cache_dir` 选项 = 改所有编译产物的落盘位置**。

最后顺带看清「target 探测」——它与环境检查（4.1 的实践）直接相关：

[python/tilus/target.py:265-279](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/target.py#L265-L279) — `get_current_target()`：首次调用时惰性初始化为 `get_default_target()`，之后缓存复用。

[python/tilus/target.py:324-409](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/target.py#L324-L409) — `get_default_target()`：用 `torch.cuda.is_available()` 判断有无 NVIDIA GPU；若有，读 `torch.cuda.get_device_capability()`，再在一张预定义表里匹配出最合适的 target（优先架构特定变体 `a`，其次家族变体 `f`，最后基础架构；[L376-385](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/target.py#L376-L385)）。若没有任何 GPU，[L408-409](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/target.py#L408-L409) 抛 `RuntimeError("No GPU is available.")`——这正是 4.1 实践里「无 GPU 会报错」的根因。

> [target.py:123-259](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/target.py#L123-L259) 还列出了 Tilus 预定义的全部 NVIDIA target，从 `sm70`（Volta）一直到 `sm121`，覆盖 u1-l1 提到的 Ampere（sm80/86/89）、Hopper（sm90/sm90a）、Blackwell（sm100/sm100a）等架构。

#### 4.3.4 代码实践：设置缓存目录并观察产物

**实践目标**：用 `tilus.option.cache_dir` 指定一个临时目录，验证「环境可用」并把缓存产物引导到可控位置。

**操作步骤**：

1. 安装好 Tilus（见 4.1.4）。
2. 新建 `check_option.py`（**示例代码**）：

   ```python
   import tilus

   # 1) 把缓存目录设到一个我们自己清楚的位置，方便检查生成的 source.cu / IR
   tilus.option.cache_dir("/tmp/tilus-tutorial-cache")

   # 2) 基本环境信息
   print("version :", tilus.__version__)
   print("target  :", tilus.get_current_target())
   print("cache   :", tilus.option.get_option("cache_dir"))

   # 3) （可选）打开 IR dump，方便后续讲义观察各 Pass 的 IR
   tilus.option.debug.dump_ir(True)
   ```

3. 运行 `python check_option.py`。
4. 运行后，`ls /tmp/tilus-tutorial-cache` 查看是否生成了缓存目录结构（如果你还跑过某个内核，会看到 `programs/<hash>/`）。

**需要观察的现象**：

- `cache` 一行应打印 `/tmp/tilus-tutorial-cache`，说明 `cache_dir()` 生效。
- 设置 `cache_dir` **不会**立即创建目录——目录是在真正编译某个内核、`get_cache_dir` 被调用时才创建的。
- `version` / `target` 打印与 4.1.4 一致。

**预期结果 / 注意事项**：

- `target` 行需要 GPU；无 GPU 会抛 `RuntimeError`（见 4.3.3 的 `get_default_target`）。这一点**待本地验证**。
- 你也可以用环境变量代替代码：`TILUS_CACHE_DIR=/tmp/xxx TILUS_DUMP_IR=1 python check_option.py` 会达到同样效果（因为注册时绑定了 `env`，见 [option.py:57](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L57) 与 [L70](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L70)）。

#### 4.3.5 小练习与答案

**练习 1**：默认缓存目录是怎么决定的？为什么在 Tilus 仓库里开发时会默认看到 `.cache/`？
**参考答案**：`_get_default_cache_dir` 检测当前文件是否在 git 仓库内（`git rev-parse`），是则用仓库根的 `.cache/`，否则用 `~/.cache/tilus`。开发时几乎总是身处 git 仓库，因此默认就是 `.cache/`。

**练习 2**：`debug.dump_ir(True)` 之后，IR 会 dump 到哪里？依据是什么？
**参考答案**：会 dump 到「当前 cache_dir」下（Tilus IR 在 `ir/`、Hidet IR 在 `module/ir/`，详见 CLAUDE.md）。依据是 `dump_ir` 只设置 `tilus.debug.dump_ir` 开关，落盘位置仍由 `cache_dir` 选项决定，而 `get_cache_dir` 正是读 `cache_dir` 来确定根目录的。

**练习 3**：为什么 CLAUDE.md 强调「修改 emitter 后必须手动删缓存」？
**参考答案**：因为缓存键基于 `prog_text`（IR 文本）与 options 的哈希（见 [drivers.py:221-223](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L221-L223)），而改 emitter 改的是「从相同 IR 生成 CUDA」这一步，IR 文本不变、哈希不变，于是旧 `.so` 被复用。必须删掉缓存目录才能强制重编。

---

## 5. 综合实践

把本讲的三件事（安装/环境检查、目录认知、option 设置）串起来：

**任务**：搭好 Tilus 开发环境，并产出一份「环境自检报告」。

1. 按你的角色选择安装方式：使用者 `pip install tilus`；源码阅读者 `pip install ".[dev]"`。
2. 写一个 `env_report.py`，完成：
   - 打印 `tilus.__version__`；
   - 打印 `tilus.get_current_target()`（并据此判断你的卡属于 Ampere / Hopper / Blackwell 哪一代——可对照 [target.py:123-259](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/target.py#L123-L259)）；
   - 用 `tilus.option.cache_dir` 把缓存指到 `/tmp/tilus-env-report`；
   - 打开 `tilus.option.debug.dump_ir(True)`；
   - 打印 `tilus.option.get_option("cache_dir")` 确认设置成功。
3. 对照 4.2 的目录表，在 `python/tilus/` 下分别找到 `lang/script.py`、`ir/prog.py`、`transforms/base.py`、`backends/codegen.py`、`runtime/compiled_program.py` 这五个文件，确认你能把它们对应到「写→表示→改→生成→跑」这条主线上的正确环节。
4. 把上述结果整理成几行文字（版本、target 代际、五个文件各属于哪个环节）作为「自检报告」。

**验收标准**：

- 报告里的 version 是有效版本号；
- target 与你机器的 GPU 一致（无 GPU 则如实记录报错信息，并说明 Tilus 必须在 CUDA 环境运行）；
- 五个文件与主线环节一一对应正确。

## 6. 本讲小结

- Tilus 通过 `pip install tilus` 安装；旧 GPU 驱动（< `580.65.06`）需额外约束 `cuda-python<13`；源码开发用 `pip install ".[dev]"`。
- 版本号由 `setuptools_scm` 从 git tag 动态生成，`tilus.__version__` 来自已安装发行版的元数据；Python 要求 ≥ 3.10。
- 真实代码在 `python/tilus/` 下，按数据流划分为 `lang`（写）→ `ir`（表示）→ `transforms`（改）→ `drivers.py`（编排）→ `backends`（生成）→ `runtime`（跑），外加 `kernels`/`utils`/`hidet`/`target.py`/`tensor.py` 等支撑模块。
- 全局选项集中在 `tilus.option`：`cache_dir` 控制缓存落盘、`debug.dump_ir`/`debug.disable_ptxas_opt` 用于调试、`bench_warmup`/`bench_repeat` 影响自动调优；多数选项还支持同名环境变量（`TILUS_CACHE_DIR` 等）。
- 默认缓存目录在 git 仓库内是 `.cache/`，否则是 `~/.cache/tilus`；缓存键是「options + 程序文本」的 SHA256 前 12 位，落在 `programs/<hash>/`。
- `get_current_target()` 在首次调用时通过 `torch.cuda` 探测 GPU 并匹配 target；无 GPU 会抛错——Tilus 必须在 CUDA 环境运行。

## 7. 下一步学习建议

环境就绪、地图在手之后，下一讲 **u1-l3《第一个内核：vector_add 逐行精读》** 会带你写出并读懂第一个 Tilus 内核，正式进入 `lang/` 目录，动手体验 `Script` 的 `__init__`/`__call__` 骨架与 `global_view`/`load_global`/`store_global` 的用法。

在进入下一讲前，建议你：

- 通读 [README.md](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/README.md) 与 CLAUDE.md 的「Cache」「Compilation Pipeline」两节，对缓存和编译流水线建立印象（细节会在 U3、U8 展开）。
- 浏览 `examples/vector_add/` 目录，对下一个要精读的例子先有个直观感受。
- 试着在解释器里 `import tilus` 并 `print(tilus.__version__)`，确认本讲的环境检查你已经亲手做过一遍。
