# sgl-kernel 是什么：项目定位与安装

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是让你在不动手写任何 CUDA 代码的前提下，先弄清楚三件事：

1. **sgl-kernel 到底是什么**：它是一个为 LLM（大语言模型）/ VLM（视觉语言模型）推理引擎服务的 GPU 算子（kernel）库，独立于 SGLang 引擎主体存在。
2. **三个容易混淆的名字**：目录名 `sgl-kernel/`、PyPI 包名 `sglang-kernel`、Python 导入名 `sgl_kernel` 分别指什么。
3. **怎么装**：从 PyPI 用 `pip` 安装的运行时要求，以及从源码用 `CMake + scikit-build-core` 构建的前置依赖。

学完本讲，你应该能够：
- 用一句话向别人解释 sgl-kernel 的定位；
- 看懂 `pyproject.toml` 里的每一行构建配置在做什么；
- 在自己的机器上完成一次「`pip install` → `import` → 打印版本号」的验证流程。

## 2. 前置知识

本讲假设你了解下面这些基础概念。如果你已经熟悉，可以跳过本节。

- **算子（kernel）**：在 GPU 上执行的一段计算函数。矩阵乘、归一化、注意力等都是算子。LLM 推理的性能瓶颈几乎全在算子的执行效率上。
- **LLM 推理引擎**：负责把一个训练好的大模型加载到显存、接收请求、跑前向计算、吐出 token 的系统。SGLang 就是这样一个引擎。
- **PyPI 与 wheel**：PyPI 是 Python 的官方包仓库（`pip install` 默认从这里下载）；wheel（`.whl`）是预编译好的二进制分发包，装上就能用，不需要在本机编译。
- **PEP 517 / 构建后端**：Python 打包标准规定，构建一个包时要先装 `[build-system]` 里声明的构建工具（这里就是 `scikit-build-core`），由它来驱动编译并产出 wheel。
- **CUDA / NVCC**：NVIDIA GPU 的编译工具链。包含 C++/CUDA 源码的扩展必须经过 NVCC 编译成 `.so` 才能被 Python 调用。

> 阅读提醒：本讲引用的源码均来自当前 HEAD `1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971`。如果你的版本不同，行号可能略有偏移，但文件与字段名一致。

## 3. 本讲源码地图

本讲只涉及「项目大门」级别的文件，目的是建立全局认知，不深入任何算子实现。

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目的「门面」：定位说明、安装方式、贡献流程、FAQ。 |
| `pyproject.toml` | Python 包的「身份证 + 说明书」：包名、版本、构建后端、打包规则。 |
| `python/sgl_kernel/version.py` | 单行版本号文件，被 `__init__.py` 导入后对外暴露为 `sgl_kernel.__version__`。 |
| `python/sgl_kernel/__init__.py`（仅第 4 行） | 把版本号 re-export 给用户。 |
| `Makefile`（`build` / `update` 目标） | 封装 `uv build` 构建流程与版本号批量更新。 |

整本手册后续会深入 `csrc/`（CUDA 源码）、`include/`（头文件）、`tests/`、`benchmark/` 等目录，本讲先不展开。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **项目定位与版权说明**——它是什么、归谁、叫什么名字。
2. **`pyproject.toml` 依赖与构建后端**——包的元数据与编译规则。
3. **`version` 与 wheel 打包**——版本号如何同步、wheel 如何圈定要打包的目录。

---

### 4.1 项目定位与版权说明

#### 4.1.1 概念说明

很多新手第一次接触 sgl-kernel 会困惑：它和 SGLang 是同一个东西吗？为什么 README 标题写的是 `sglang-kernel`，而目录却叫 `sgl-kernel`，代码里 `import` 的又是 `sgl_kernel`？

关键认知是：**sgl-kernel 是一个独立的 GPU 算子库**，它的存在意义是为 LLM/VLM 推理引擎提供「定制化的、高性能的计算原语（compute primitives）」。SGLang 推理引擎是它最主要的使用者，但它本身被设计成一个可独立打包、独立发布、独立版本管理的库——你完全可以只装它、不用整套 SGLang。

至于三个名字的差异，是历史改名 + Python 打包约定共同造成的：

| 维度 | 名字 | 出现位置 | 含义 |
| --- | --- | --- | --- |
| 源码目录名 | `sgl-kernel/` | 仓库目录树 | 这份代码在仓库里存放的文件夹 |
| PyPI 分发名 | `sglang-kernel` | `pip install` 命令 | 你 `pip install` 时写的名字 |
| Python 导入名 | `sgl_kernel` | `import sgl_kernel` | 你在 Python 代码里 `import` 的名字 |

记住一句话：**「目录看 `sgl-kernel`，安装用 `sglang-kernel`，导入写 `sgl_kernel`」**。

版权方面，项目采用 **Apache License 2.0**（一个业界常用的宽松开源协议），仓库根目录有 `LICENSE` 与 `THIRDPARTYNOTICES.txt`（第三方代码声明）两个文件。

#### 4.1.2 核心流程

理解定位时，可以按下面这条「从仓库到用户」的链路看：

```
仓库源码树 (sgl-kernel/)
   │  被 pyproject.toml 描述为 Python 包
   ▼
PyPI 分发包 sglang-kernel (pip install 的名字)
   │  装到用户机器后，import 名固定为 sgl_kernel
   ▼
import sgl_kernel  →  sgl_kernel.__version__
```

- 仓库里的文件夹叫 `sgl-kernel/`（带连字符）。
- 这个文件夹被声明成一个叫 `sglang-kernel` 的 Python 发行版（distribution），上传到 PyPI。
- 发行版里的 Python 模块路径是 `python/sgl_kernel/`，所以导入名是 `sgl_kernel`（带下划线，这是 Python 标识符的合法写法——连字符不能出现在 `import` 里）。

#### 4.1.3 源码精读

先看 README 的标题行与定位句。注意标题括号里的 `(prior sgl-kernel)`，说明这个包**历史上曾叫 `sgl-kernel`，后来改名成了 `sglang-kernel`**：

- [README.md:1-3](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/README.md#L1-L3)：标题 `# sglang-kernel (prior sgl-kernel)`，下面一行把它定位为「LLM 推理引擎的 Kernel Library」。

再看这句最关键的「三个名字」官方说明，它同时解释了目录名与导入名为什么和包名不一致：

- [README.md:12-12](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/README.md#L12-L12)：原文 `The source tree remains under the sgl-kernel/ directory and the Python import path remains sgl_kernel.`——明确说明源码树仍保留在 `sgl-kernel/` 目录、导入路径仍是 `sgl_kernel`。

版权与徽章在 README 顶部：

- [README.md:7-8](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/README.md#L7-L8)：`License: Apache-2.0` 徽章与 `PyPI` 版本徽章，分别确认协议是 Apache-2.0、发布渠道是 PyPI。

最后，PyPI 链接里的包名就是 `sglang-kernel`（注意不是 `sgl-kernel`）：

- [README.md:8-8](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/README.md#L8-L8)：徽章指向 `https://pypi.org/project/sglang-kernel`。

#### 4.1.4 代码实践

**实践目标**：亲手确认「三个名字」与版权信息。

**操作步骤**：

1. 打开本仓库的 `README.md`，找到标题行与第 12 行的说明句。
2. 在浏览器访问 `https://pypi.org/project/sglang-kernel/`，确认 PyPI 上的包名确实是 `sglang-kernel`。
3. 打开仓库根目录的 `LICENSE` 文件，确认第一行是 `Apache License`、版本 `2.0`。

**需要观察的现象**：

- README 标题里带有 `(prior sgl-kernel)` 字样，表明这是一次改名后的标题。
- PyPI 页面的包名与 `pip install` 名一致，都是 `sglang-kernel`。

**预期结果**：你会看到「目录 / 安装 / 导入」三个名字彼此不同，但指向同一份代码。

> 待本地验证：PyPI 页面与徽章显示的最新版本号可能随时间变化；本讲基于源码中的版本 `0.4.5` 讲解。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Python 的 `import` 名不能写成 `import sgl-kernel`（带连字符）？

> **答案**：Python 标识符只能包含字母、数字、下划线，且不能以数字开头；连字符 `-` 会被解析成减号运算符。所以包内模块用下划线 `sgl_kernel`，而 PyPI 分发名可以用连字符 `sglang-kernel`（分发名不是 Python 标识符）。

**练习 2**：`prior sgl-kernel` 这句话透露了什么历史信息？

> **答案**：这个包曾经以 `sgl-kernel` 为名发布过，后来改名为 `sglang-kernel`；改名后特意保留旧名说明，方便老用户过渡。源码目录名 `sgl-kernel/` 也沿用了旧名未改。

---

### 4.2 pyproject.toml 依赖与构建后端

#### 4.2.1 概念说明

`pyproject.toml` 是现代 Python 项目的「总配置文件」（PEP 518/517/621 标准）。它回答四个问题：

1. **怎么构建**（`[build-system]`）：用哪个构建后端、构建时需要哪些工具。
2. **包叫什么**（`[project]`）：名字、版本、作者、描述、支持的 Python 版本、协议。
3. **运行时依赖**（`[project].dependencies`）：装这个包会自动拉哪些别的包。
4. **怎么打包成 wheel**（`[tool.scikit-build]`）：哪些目录要打进 wheel、哪种 ABI。

对 sgl-kernel 这种「包含 C++/CUDA 扩展」的项目，`pyproject.toml` 还要交代清楚：构建后端不是默认的 `setuptools`，而是 `scikit-build-core`——一个专门用来把 `CMake` 工程编译成 Python wheel 的工具。

这里有一个非常值得注意的细节：**README 里写运行时「Requires torch == 2.11.0」，而 `pyproject.toml` 的 `[build-system].requires` 里写的是 `torch>=2.8.0`**。这两者并不矛盾：

- `torch == 2.11.0`（README）：**运行时**对预编译 wheel 的 ABI 钉版要求。发布的 wheel 是针对特定 torch/CUDA 编译的，运行时必须用匹配的 torch。
- `torch>=2.8.0`（build-system）：**从源码构建时**的最小 torch 要求——构建期需要 torch 提供的 C++ 头文件和 CMake 配置，只要不低于 2.8.0 即可。

另一个细节：`[project].dependencies = []` 是**空数组**，意味着 `pip install sglang-kernel` **不会自动帮你装 torch**——你必须自己先把符合版本的 torch 装好。

#### 4.2.2 核心流程

从「拿到源码」到「装上能用」的流程：

```
源码 (sgl-kernel/)
   │  pip/uv 读取 [build-system]
   ▼
安装构建工具：scikit-build-core>=0.10, torch>=2.8.0, wheel
   │  scikit-build-core 调用 CMake + NVCC 编译 CUDA 扩展
   ▼
产出 wheel (.whl)
   │  按 [tool.scikit-build].wheel.packages 圈定 Python 目录
   ▼
pip install wheel  →  但 dependencies=[] 不自动装 torch
   ▼
用户须自行保证 torch == 2.11.0，才能正常 import
```

#### 4.2.3 源码精读

构建后端声明——注意 `build-backend = "scikit_build_core.build"`，这就是为什么本项目用 CMake 而不是 setuptools：

- [pyproject.toml:1-7](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/pyproject.toml#L1-L7)：`[build-system]` 段声明 `scikit-build-core>=0.10`、`torch>=2.8.0`、`wheel` 三项构建期依赖，并指定构建后端为 `scikit_build_core.build`。

包元数据——包名、版本、Python 版本要求、协议都在这里：

- [pyproject.toml:9-24](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/pyproject.toml#L9-L24)：`name = "sglang-kernel"`（PyPI 名）、`version = "0.4.5"`、`description = "Kernel Library for SGLang"`、`requires-python = ">=3.10"`、`license = { file = "LICENSE" }`（协议直接引用根目录的 `LICENSE` 文件），以及 `dependencies = []`（运行时不自动拉依赖）。

把 README 与 pyproject 对应起来看，运行时与构建期的版本要求分属两处：

- [README.md:14-20](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/README.md#L14-L20)：Installation 段写明「Requires torch == 2.11.0」并提供 `pip3 install sglang-kernel --upgrade`。
- [README.md:22-27](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/README.md#L22-L27)：Building from Source 段列出从源码构建的前置：`CMake ≥3.31`、`Python ≥3.10`、`scikit-build-core`、`ninja(optional)`。

> 对照点：`Python ≥3.10` 与 `pyproject.toml` 的 `requires-python = ">=3.10"` 完全一致；而 `torch` 在两处的版本号不同（运行时 `==2.11.0` vs 构建期 `>=2.8.0`），这正是上一节强调的「运行时钉版 / 构建期下限」区别。

scikit-build 的打包规则——决定哪些目录进 wheel：

- [pyproject.toml:36-42](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/pyproject.toml#L36-L42)：`cmake.build-type = "Release"`（发布版优化编译）、`wheel.py-api = "cp310"`（限定 CPython 3.10 ABI）、`wheel.packages = ["python/sgl_kernel"]`（**只把这个目录打包进 wheel**，所以 `csrc/`、`tests/` 等不会进 wheel）。

#### 4.2.4 代码实践

**实践目标**：把 README 的版本要求与 `pyproject.toml` 的字段逐条对应，理解「为什么运行时和构建期对 torch 的版本要求不同」。

**操作步骤**：

1. 打开 `pyproject.toml` 第 1–7 行，记录构建期 `torch` 的下限版本。
2. 打开 `README.md` 第 14–16 行，记录运行时 `torch` 的钉版版本。
3. 在 `pyproject.toml` 第 24 行确认 `dependencies = []`，回答：装完 `sglang-kernel` 后，`pip` 会自动帮你装 torch 吗？

**需要观察的现象**：

- 构建期要求 `torch>=2.8.0`，运行时要求 `torch == 2.11.0`，两者数值不同但语义自洽。
- `dependencies` 为空，意味着 torch 需要你自行安装。

**预期结果**：你能用一句话解释「构建期下限 vs 运行时钉版」的区别，并知道装这个包不会自动带 torch。

> 待本地验证：torch 版本号会随上游升级而变化；本讲引用的是当前 HEAD 的数值。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `dependencies = []` 而不是把 torch 列进去？

> **答案**：因为发布 wheel 对 torch 是精确钉版（`==2.11.0`，出于 ABI 兼容）。如果把 torch 列进 `dependencies`，`pip` 会按自己的依赖解析去装一个「它能找到的」torch 版本，可能与 wheel 编译时使用的 torch ABI 不匹配，导致扩展加载失败。空依赖强制让用户显式安装正确版本的 torch。

**练习 2**：`wheel.packages = ["python/sgl_kernel"]` 这一行如果不写会怎样？

> **答案**：scikit-build 默认不知道该把哪个目录当 Python 包打进 wheel。显式指定后，只有 `python/sgl_kernel/` 下的 `.py` 与编译产物会被打进 wheel；`csrc/`、`tests/`、`benchmark/` 等都不会进，从而保持 wheel 体积可控。

---

### 4.3 version 与 wheel 打包

#### 4.3.1 概念说明

Python 包的「版本号」看似简单，实则牵涉到三处必须保持一致的地方：

1. **`pyproject.toml` 里的 `version`**：PEP 621 规定的包元数据版本，`pip show` 与 PyPI 页面显示的就是它。
2. **`python/sgl_kernel/version.py` 里的 `__version__`**：运行时 `sgl_kernel.__version__` 读到的字符串。
3. **git tag / 发布工具链**：CI 打 wheel 时往往按某个 tag 来。

在 sgl-kernel 里，前两处目前是**手动保持同步**的（都写成 `0.4.5`），项目还提供了一个 `make update <新版本>` 目标来批量替换多处文件里的版本字符串，避免人工漏改。

版本号的暴露链路是：

```
python/sgl_kernel/version.py  (__version__ = "0.4.5")
        │  被 __init__.py 第 4 行 import
        ▼
sgl_kernel/__init__.py  →  对外暴露 sgl_kernel.__version__
```

所以用户 `import sgl_kernel; print(sgl_kernel.__version__)` 拿到的值，追根溯源来自 `version.py` 这个单行文件。

#### 4.3.2 核心流程

版本号的「定义 → 暴露 → 同步」流程：

```
version.py 定义 __version__
        │
        ▼
__init__.py re-export
        │
        ▼  用户读取 sgl_kernel.__version__

发版时：make update <X.Y.Z>  ──►  批量改 version.py / pyproject.toml / pyproject_rocm.toml / pyproject_cpu.toml
```

`make update` 会改四个文件（见 `Makefile` 的 `FILES_TO_UPDATE`），保证 NVIDIA / ROCm / CPU 三个变体的 `pyproject` 与 `version.py` 版本号同时更新。

#### 4.3.3 源码精读

版本号的最源头——一个只有一行的文件：

- [python/sgl_kernel/version.py:1-1](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/version.py#L1-L1)：`__version__ = "0.4.5"`，整个文件只有这一行。

这个版本号如何被对外暴露：

- [python/sgl_kernel/__init__.py:4-4](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py#L4-L4)：`from sgl_kernel.version import __version__  # noqa: F401`——`noqa: F401` 表示「虽然看起来没在本文件用到，但这是有意 re-export，不要报未使用告警」。从此用户访问 `sgl_kernel.__version__` 即可拿到版本字符串。

包元数据里的版本必须和上面一致：

- [pyproject.toml:10-11](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/pyproject.toml#L10-L11)：`name = "sglang-kernel"`、`version = "0.4.5"`，与 `version.py` 的值手动保持同步。

批量更新版本号的 Makefile 目标——它列出了所有需要同步改的文件：

- [Makefile:70-73](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/Makefile#L70-L73)：`FILES_TO_UPDATE` 包含 `python/sgl_kernel/version.py`、`pyproject.toml`、`pyproject_rocm.toml`、`pyproject_cpu.toml` 四个文件，说明项目为 NVIDIA / AMD ROCm / CPU 三种后端各维护了一份 `pyproject`。

构建并安装 wheel 的封装命令：

- [Makefile:45-52](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/Makefile#L45-L52)：`build` 目标先装格式化依赖、初始化 submodule，再用 `uv build --wheel ... --no-build-isolation` 编译 wheel，最后 `pip3 install dist/*whl --force-reinstall --no-deps` 安装（`--no-deps` 呼应了「不自动装依赖」的设计）。

#### 4.3.4 代码实践

**实践目标**：从 PyPI 安装 sgl-kernel 并打印版本号，验证「装得上、能 import、版本号对得上」。

**操作步骤**：

1. 确认你的环境已装好 `torch == 2.11.0`（具体以你本地 README 要求为准）。
2. 执行安装：

   ```bash
   pip3 install sglang-kernel --upgrade
   ```

3. 进入 Python 解释器，导入并打印版本号：

   ```python
   import sgl_kernel
   print(sgl_kernel.__version__)
   ```

**需要观察的现象**：

- `pip3 install` 指令里写的是 `sglang-kernel`（连字符），但 `import` 写的是 `sgl_kernel`（下划线），两者不是笔误。
- 打印出的版本号字符串应当与 `version.py` 里的 `__version__` 一致。

**预期结果**：屏幕上打印出形如 `0.4.5` 的版本号（具体数字取决于你安装时 PyPI 上的最新版本）。

> 待本地验证：本实践依赖一台装有匹配 torch 与 NVIDIA GPU 驱动的机器。若你的环境不满足，打印出的版本号或安装是否成功需以本地实际结果为准。若没有 GPU，可以只做「源码阅读型实践」：阅读 `version.py` 与 `__init__.py:4`，口头复述版本号是如何从前者流到 `sgl_kernel.__version__` 的。

#### 4.3.5 小练习与答案

**练习 1**：如果不小心只改了 `pyproject.toml` 的 `version`、忘了改 `version.py`，会出现什么不一致？

> **答案**：`pip show sglang-kernel` 显示的是 `pyproject.toml` 的版本（包元数据），而 `sgl_kernel.__version__` 显示的是 `version.py` 的版本（运行时）。两者会打架，给排查问题带来困扰。所以项目用 `make update` 一次性同步多处文件。

**练习 2**：`# noqa: F401` 这个注释去掉会怎样？

> **答案**：`F401` 是「imported but unused」告警。`__init__.py` 第 4 行 import 了 `__version__` 但本文件体内并没有直接用它（它的用途是暴露给外部），所以 linter 会报未使用。加 `# noqa: F401` 是告诉 linter「这是有意 re-export，别报警」。

**练习 3**：为什么 `make update` 要同时改 `pyproject.toml`、`pyproject_rocm.toml`、`pyproject_cpu.toml` 三个文件？

> **答案**：项目为 NVIDIA CUDA、AMD ROCm、CPU 三种后端各维护了一份 `pyproject`（见仓库根目录的三个 `.toml` 文件）。发版时三者的版本号必须一致，否则不同后端的 wheel 会发布成不同版本，造成混乱。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「读 + 装 + 验」的小任务：

1. **读**：通读 `README.md` 与 `pyproject.toml`，用一句话写下 sgl-kernel 的定位（参考第 4.1 节）。
2. **填表**：仿照下表，把你从源码里读到的真实数值填进去（不要凭记忆）：

   | 项目 | 值 | 出处（文件:行） |
   | --- | --- | --- |
   | PyPI 包名 |  |  |
   | Python 导入名 |  |  |
   | 运行时 torch 要求 |  |  |
   | 构建期 torch 下限 |  |  |
   | requires-python |  |  |
   | 构建后端 |  |  |
   | 当前版本号 |  |  |
   | 开源协议 |  |  |

3. **装 + 验**（需要 GPU 与匹配 torch 的环境）：执行 `pip3 install sglang-kernel --upgrade`，然后 `python3 -c "import sgl_kernel; print(sgl_kernel.__version__)"`，确认版本号与你在 `version.py` 读到的一致。
4. **源码阅读型替代实践**（无 GPU 环境）：阅读 `Makefile` 的 `build` 目标（第 45–52 行）与 `update` 目标（第 70–90 行），用自己的话写出「从源码构建一个 wheel 并安装」与「批量更新版本号」分别发生了什么。

> 完成后，你应该能不查文档就回答：这个项目叫什么、装什么、怎么 import、版本号从哪里来。

## 6. 本讲小结

- sgl-kernel 是一个**独立的 GPU 算子库**，为 LLM/VLM 推理引擎（主要是 SGLang）提供高性能计算原语，可独立于引擎安装使用。
- 三个名字要分清：**目录 `sgl-kernel/`、PyPI 包 `sglang-kernel`、导入名 `sgl_kernel`**；包历史上由 `sgl-kernel` 改名而来。
- 构建后端是 **`scikit-build-core`**（不是 setuptools），它驱动 CMake + NVCC 把 CUDA 扩展编译进 wheel。
- 运行时要求 **`torch == 2.11.0`**（ABI 钉版），构建期下限是 **`torch>=2.8.0`**；`dependencies = []` 意味着 pip 不会自动装 torch。
- 版本号源头是单行文件 **`python/sgl_kernel/version.py`**，经 `__init__.py` 第 4 行 re-export 为 `sgl_kernel.__version__`，并需与 `pyproject.toml` 手动同步（可用 `make update` 批量改）。
- wheel 只打包 **`python/sgl_kernel/`** 一个目录（`wheel.packages`），`csrc/`、`tests/` 等不进 wheel；协议为 **Apache-2.0**。

## 7. 下一步学习建议

本讲只看了「项目大门」。接下来建议：

- **下一篇（u1-l2）目录结构与内核流水线总览**：进入 `csrc/`、`include/`、`python/sgl_kernel/`、`tests/`、`benchmark/` 五大目录，建立「一个算子从 CUDA 到 Python」的六步流水线认知。这是后续所有讲义的主线索。
- **u1-l3 构建系统**：深入 `CMakeLists.txt`，理解为什么同一份源码要编译出 `sm90`/`sm100` 两个变体。
- 如果你想立刻看到代码运行，可以先跳到 **u2-l1（Python 入口与架构自适应加载）**，看 `import sgl_kernel` 之后到底加载了哪个 `.so`。

> 阅读建议：本系列讲义按 `depends_on` 形成依赖链，建议尽量按 u1 → u2 → u3 → … 的顺序阅读；每篇末尾的「下一步学习建议」都会给出衔接路径。
