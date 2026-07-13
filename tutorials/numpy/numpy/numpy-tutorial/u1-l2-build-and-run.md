# 构建、安装与运行方式

## 1. 本讲目标

上一讲我们认识了 NumPy 是什么，以及它的元信息文件 `pyproject.toml` 长什么样。本讲要回答一个更实际的问题:**这份由 C/C++/Cython/Python 混合写成的源码,到底怎么变成可以 `import numpy` 的东西?**

学完本讲你应该能够:

- 说清楚 NumPy 的构建后端是 `meson-python`,以及它和 Meson、Ninja、Cython 的关系。
- 区分两种「从源码得到 NumPy」的路径:面向终端用户的 `pip install`,面向开发者的 `spin`。
- 看懂 `.spin/cmds.py` 里 `build` / `test` / `docs` / `bench` 这些开发命令是如何在 Meson 之上封装出来的。
- 知道构建对编译器、Python 版本、BLAS/LAPACK 的要求,并能用 `meson.options` 里的开关调整构建行为。
- 完成一次从源码构建 NumPy 并运行测试的完整实践。

## 2. 前置知识

在进入源码之前,先理解三个概念,它们是本讲的「基础设施」。

**构建后端(build backend)与 PEP 517。**
当你执行 `pip install xxx` 时,pip 并不会自己知道怎么把源码变成 wheel。它先读 `pyproject.toml` 里的 `[build-system]` 段,找到 `build-backend` 字段,然后把「怎么编译」这件事完全交给这个后端去做。这是一种「插件式」的设计:pip 只负责调度,真正干活的是后端。NumPy 的后端叫 `mesonpy`(即 `meson-python`)。

**Meson 与 Ninja。**
Meson 是一个构建「描述语言」(用类似 Python 的语法写 `meson.build`),它本身不编译任何东西,而是生成一份 Ninja 的构建文件;Ninja 才是真正跑在底层、调度编译器的「执行器」。可以类比为:Meson 是设计师,Ninja 是包工头,GCC/Clang/MSVC 是搬砖工人。Ninja 比传统 Make 更快,尤其在大项目的增量编译上。

**Cython。**
NumPy 里大量 `.pyx` 文件(例如 `numpy/random/_generator.pyx`)是 Cython 源码。Cython 把这种「Python 风格但能直接调 C」的代码翻译成 C,再交给 C 编译器。所以构建 NumPy 时,Cython 必须先于 C 编译器工作:它是一次「代码生成」,再是一次「编译」。

一句话串起来:

> pip → meson-python(后端)→ Meson(描述)+ Cython(把 .pyx 翻译成 .c)→ Ninja(调度)→ GCC/Clang/MSVC(编译)→ 可 `import` 的扩展模块。

## 3. 本讲源码地图

本讲涉及的关键文件如下:

| 文件 | 作用 |
|------|------|
| [pyproject.toml](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/pyproject.toml) | 包的「身份证」。声明构建后端、运行依赖、命令行入口,以及 `spin` 的命令注册表。 |
| [meson.build](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/meson.build) | 顶层 Meson 构建脚本。定义项目、检测编译器、进入各子目录。 |
| [meson.options](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/meson.options) | 所有可配置的构建开关(BLAS、CPU/SIMD、线程等)。 |
| [.spin/cmds.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/.spin/cmds.py) | 开发者命令行工具 `spin` 的命令实现(build/test/docs/bench 等)。 |
| [INSTALL.rst](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/INSTALL.rst) | 面向用户的安装说明:前置依赖、基本安装、编译器与 BLAS 选择。 |
| [building_with_meson.md](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/building_with_meson.md) | 面向开发者的 Meson 构建速查。 |

## 4. 核心概念与源码讲解

### 4.1 构建后端与构建依赖

#### 4.1.1 概念说明

「构建后端」回答的是:**谁来把源码树编译成可安装的产物**。NumPy 选了 `meson-python`(`mesonpy`),原因有三:

1. NumPy 是混合语言项目(C/C++/Cython),需要一个能驾驭多语言编译的构建系统,Meson 比老的 `distutils/setuptools` 强得多。
2. Meson 生成 Ninja 文件,增量编译极快,这对每天反复构建的开发者至关重要。
3. `meson-python` 是连接 pip(PEP 517)和 Meson 的桥,让 `pip install .` 这种标准命令能直接驱动 Meson 构建。

构建「依赖」是指**构建时**需要的工具(注意区别于运行时依赖):NumPy 构建时必须先有 `meson-python` 和 `Cython`。

#### 4.1.2 核心流程

构建的发生顺序可以这样描述:

1. pip 读 `pyproject.toml` 的 `[build-system]`,确定后端是 `mesonpy`。
2. pip 在一个隔离环境里安装 `requires` 列出的构建依赖(`meson-python`、`Cython`)。
3. 后端调用 Meson;Meson 读 `meson.build`,检测编译器(C/C++/Cython)和 Python 头文件。
4. Cython 先把 `.pyx` 翻译成 `.c`;Meson 把所有 C/C++ 源组织成 Ninja 任务。
5. Ninja 调度编译器,生成 `_multiarray_umath`、`_umath` 等 `.so`/`.pyd` 扩展。
6. 后端把这些产物连同 Python 文件打包,安装到当前环境。

注意一个细节:NumPy 还在仓库里自带了一份 Meson(`vendored-meson/meson/`),通过 `pyproject.toml` 的 `[tool.meson-python]` 段指定优先使用它。这样能让所有开发者用「同一版本」的 Meson,避免「我这能编你那不能编」的问题。

#### 4.1.3 源码精读

构建后端与依赖的声明只有寥寥几行,但信息量很大:

这段声明后端为 `mesonpy`,并列出两个构建依赖,还特意提醒 Cython 版本要和 `meson.build` 里的检查保持同步:
[pyproject.toml:1-6](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/pyproject.toml#L1-L6) — 中文说明:这是整个构建链路的「入口契约」,`build-backend = "mesonpy"` 决定了 pip 把编译工作交给谁。

这里指定构建时优先使用仓库自带的 Meson,以及 `meson-python install` 阶段要安装哪些 tag(runtime/python-runtime/tests/devel):
[pyproject.toml:242-246](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/pyproject.toml#L242-L246) — 中文说明:`vendored-meson` 让构建行为可复现;`--tags=runtime,python-runtime,tests,devel` 决定了 `pip install` 时除了运行时文件,还会装上测试数据和开发头文件。

顶层 `meson.build` 定义项目本身,启用 `c/cpp/cython` 三种语言,版本号由 `gitversion.py` 在构建时生成:
[meson.build:1-14](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/meson.build#L1-L14) — 中文说明:`run_command(['numpy/_build_utils/gitversion.py'])` 解释了为什么源码树里没有真实的 `version.py`(见上一讲),版本号是构建期动态生成的。

同文件末尾把构建分派到两个子目录:
[meson.build:83-84](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/meson.build#L83-L84) — 中文说明:`subdir('meson_cpu')` 处理 CPU 特性检测(SIMD/分发),`subdir('numpy')` 进入主源码目录,`numpy/meson.build` 再进一步 `subdir` 到各子模块。

#### 4.1.4 代码实践

这是一个「阅读型」实践,目标是让你亲眼确认构建链路的入口。

1. 实践目标:确认本机(或你想象中的构建机)上,构建 NumPy 需要哪些前置工具。
2. 操作步骤:
   - 打开 `pyproject.toml`,找到 `[build-system]` 段,记下 `build-backend` 和 `requires`。
   - 用一条命令检查本机是否已装齐构建依赖(以下为示例命令):

   ```bash
   python -c "import mesonpy" 2>/dev/null && echo "meson-python OK" || echo "meson-python 缺失"
   python -c "import Cython; print('Cython', Cython.__version__)"
   meson --version  # 或使用仓库自带的: python vendored-meson/meson/meson.py --version
   ```
3. 需要观察的现象:`build-backend` 应显示为 `mesonpy`;Cython 版本应 ≥ 3.1.0。
4. 预期结果:三项都满足时,构建链路的「上半段」(后端 + 构建依赖)就绪。若某项缺失,后续 `pip install .` 会先去隔离环境自动安装它们。
5. 如果无法在本机运行,明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `meson-python` 和 `Cython` 放在 `[build-system].requires` 里,而不是放在 `[project].dependencies` 里?

> 参考答案:前者是**构建期**依赖(只有从源码编译时才需要,装完即可丢弃),后者是**运行期**依赖(用户 `import numpy` 时还要用)。pip 会在隔离的 build 环境里安装构建依赖,不污染运行环境。Cython 只负责把 `.pyx` 翻译成 `.c`,编译产物里不再需要它。

**练习 2**:`meson.build` 第 4 行用 `run_command(...)` 取版本号,而不是在 `pyproject.toml` 写死版本。这种做法的好处是什么?

> 参考答案:这样版本号可以从 git tag/提交状态动态推导,避免「改了 git 标签却忘记改文件」的不一致。这也是为什么源码树里只有存根 `numpy/version.pyi`,真实版本号在构建期才生成。

---

### 4.2 spin 命令体系

#### 4.2.1 概念说明

`spin` 是 SciPy/NumPy 社区为「混合语言科学库开发者」量身打造的命令行工具。它的定位是:**比 `pip install -e .`(可编辑安装)更适合日常开发**。

为什么需要 spin?因为 NumPy 是编译型项目,你改了一行 `.pyx` 或 `.c`,需要「重新编译 + 重新让 Python 找到新产物」。如果用普通 pip 安装,每次改完都要重装;`spin` 把「构建到 `build-install/` 目录 + 设置好 `PYTHONPATH` + 启动一个能立刻 `import numpy` 的解释器」封装成一条命令,开发体验顺滑得多。

`spin` 本身是一个基于 `click` 的通用框架,每个项目通过仓库根目录的 `.spin/cmds.py` 注册自己的命令。NumPy 在 `pyproject.toml` 的 `[tool.spin.commands]` 段把这些命令分组。

#### 4.2.2 核心流程

`spin` 的执行流程:

1. 你在仓库根目录敲 `spin build`。
2. `spin` 读 `pyproject.toml` 的 `[tool.spin]`,知道 `package = 'numpy'`,以及 Meson CLI 是仓库自带的 `vendored-meson/meson/meson.py`。
3. `spin` 按 `[tool.spin.commands]` 的映射,把 `build` 这个子命令分派到 `.spin/cmds.py:build` 函数。
4. 该函数对 Meson 参数做项目特有的修饰(例如关掉字节码编译以加速),再调用父命令 `spin.cmds.meson.build` 真正去跑 Meson/Ninja。
5. 产物落在 `build/`(编译中间产物)和 `build-install/`(可 `import` 的安装树)。
6. 之后 `spin ipython` / `spin test` 会自动把 `build-install/` 加到 `PYTHONPATH`,让它们用上你刚编译的 NumPy。

`.spin/cmds.py` 开头还有一个「防呆」检查:如果 `vendored-meson/meson` 这个 git 子模块没初始化,就直接报错并提示 `git submodule update --init`。

#### 4.2.3 源码精读

命令注册表在 `pyproject.toml`,把命令分成 Build / Environments / Documentation / Metrics 四组:
[pyproject.toml:249-279](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/pyproject.toml#L249-L279) — 中文说明:`"Build"` 组里有 `build/test/mypy/pyrefly/stubtest/config_openblas/lint`;`"Documentation"` 组里有 `docs/changelog/notes/check_docs/check_tutorials`;`"Metrics"` 组里是 `bench`。每个值是 `模块:函数`,spin 据此做分派。

`.spin/cmds.py` 开头检查 Meson 子模块是否存在,这是「我能否构建」的第一道关卡:
[.spin/cmds.py:13-19](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/.spin/cmds.py#L13-L19) — 中文说明:如果 `vendored-meson/meson/mesonbuild` 目录不存在,立即抛出 `RuntimeError` 并告诉用户运行 `git submodule update --init`。这是克隆仓库后最常见的第一个错误。

`build` 命令在父命令之上加了一步:用 `-Dpython.bytecompile=-1` 关掉每次重装都做的字节码编译(很慢),从而加速增量构建:
[.spin/cmds.py:79-87](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/.spin/cmds.py#L79-L87) — 中文说明:`@spin.util.extend_command(spin.cmds.meson.build)` 表示「扩展」而非「替换」父命令;`parent_callback(...)` 把加工后的参数交给父命令真正去构建。它还支持 `--with-scipy-openblas` 一键配置 OpenBLAS。

`test` 命令把 pytest 的 marker 默认设为 `not slow`,只有显式 `-m full` 才跑全套;并支持 `pytest-run-parallel` 多线程跑测试:
[.spin/cmds.py:153-173](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/.spin/cmds.py#L153-L173) — 中文说明:不传任何测试参数时自动补 `--pyargs numpy`;`-m full` 是「跑全部」的约定。这解释了为什么日常 `spin test` 很快、而 `spin test -m full` 很慢。

`docs` 命令在构建 Sphinx 文档前,先跑一次 `towncrier` 把 release notes 片段合并进来,并清理一批生成目录:
[.spin/cmds.py:90-127](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/.spin/cmds.py#L90-L127) — 中文说明:`clean_dirs` 列出每次都要清掉的自动生成目录,避免脏数据;默认 `SPHINXOPTS="-W"` 会把警告升级为错误。

`bench` 命令封装了 `asv`(airspeed-velocity)基准测试,默认拿当前代码先 `build` 再跑,`--compare` 模式则对比 `main` 与 `HEAD` 两个提交:
[.spin/cmds.py:411-500](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/.spin/cmds.py#L411-L500) — 中文说明:无 `--compare` 时先 `ctx.invoke(build)`,再以 `--dry-run --show-stderr` 跑 asv;`--compare` 时用 `asv continuous --factor` 对比两个提交,并对未提交改动给出红色警告。

#### 4.2.4 代码实践

1. 实践目标:看懂 `spin` 的命令分派机制,确认一条命令最终调到哪个函数。
2. 操作步骤:
   - 在 `pyproject.toml` 的 `[tool.spin.commands]` 里找到 `test` 对应的映射(应为 `.spin/cmds.py:test`)。
   - 在 `.spin/cmds.py` 里定位 `test` 函数,阅读它对 `pytest_args` 的三段加工(默认补 `--pyargs numpy`、注入 marker、注入并行参数)。
   - 在仓库根目录尝试列出所有可用命令(示例命令):

   ```bash
   spin --help
   ```
3. 需要观察的现象:`spin --help` 会按 Build / Environments / Documentation / Metrics 分组列出命令,与 `pyproject.toml` 的注册表一致。
4. 预期结果:你能说出 `spin test` 默认带上了 `-m 'not slow'`,而 `spin test -m full` 才跑全部测试。
5. 若本机未安装 `spin`,标注「待本地验证」,可改为纯阅读 `.spin/cmds.py` 的 `test` 函数完成本任务。

#### 4.2.5 小练习与答案

**练习 1**:`spin build` 与 `pip install -e . --no-build-isolation` 都能让你在本机用上源码版 NumPy,开发场景下为什么更推荐前者?

> 参考答案:`spin build` 把产物装到 `build-install/`,再用 `spin ipython`/`spin test` 自动设置 `PYTHONPATH`,改完 `.c`/`.pyx` 只需重跑 `spin build` 增量编译即可,不会污染 site-packages;可编辑安装会把构建产物放进真实环境,反复重装更慢且容易和已装版本混淆。

**练习 2**:`spin test` 默认 marker 是 `not slow`。如果你只想跑线性代数相关、且包含 slow 的测试,应该怎么写命令?

> 参考答案:类似 `spin test -m full numpy/linalg`,或更精确地 `spin test -m "slow" numpy/linalg`。前者放开 slow 限制只跑 linalg 子目录,后者只跑带 slow 标记的 linalg 测试。

---

### 4.3 安装路径、前置依赖与 BLAS 选择

#### 4.3.1 概念说明

NumPy 的「安装」其实分两条路径,对应两类人:

- **终端用户**:不关心源码,只想拿到一个能用的 NumPy。官方推荐直接 `pip install numpy` 装 PyPI 上的预编译 wheel,根本不会触发本地编译。
- **从源码构建者**(开发者 / 需要特殊 BLAS 的用户):需要在本机跑 Meson 编译链路。

`INSTALL.rst` 开篇就强调:**对大多数用户来说,从源码构建并不是推荐做法**。本讲面向的是愿意读源码、需要在本地构建的你,所以重点放在第二条路径。

构建 NumPy 需要:C/C++ 编译器、Cython(开发版必需)、Python ≥ 3.12 及其开发头文件;**不需要** Fortran 编译器(NumPy 自身用 `lapack_lite`/`pocketfft` 等内置实现,只有 `f2py` 的测试在缺 Fortran 时会被跳过)。

BLAS/LAPACK 是另一个重点:它是线性代数运算(`np.dot`/`np.linalg`)的后端,选哪个会显著影响性能。常见选择是 OpenBLAS、Apple Accelerate(macOS)、Intel MKL;实在没有也能用内置慢速回退(由 `allow-noblas` 控制)。

#### 4.3.2 核心流程

从源码安装的完整步骤:

1. 克隆仓库后,初始化 git 子模块(获取 `vendored-meson`):`git submodule update --init`。
2. 确认前置依赖:C/C++ 编译器、Python 3.12+ 开发头、(可选)pytest、Hypothesis。
3. 选择安装方式:
   - 终端安装:`pip install .`
   - 可编辑安装:`pip install -e . --no-build-isolation`
   - 开发构建:`spin build`
4. (可选)通过 `--config-settings` 调整 BLAS、CPU 分发等 Meson 选项。
5. 验证:`python -c "import numpy; print(numpy.__version__)"`,或跑测试。

BLAS 的选择通过 Meson 选项暴露,核心开关在 `meson.options`:`blas`(库名)、`allow-noblas`(无 BLAS 时是否回退到慢速内置实现)、`use-ilp64`(是否用 64 位整数接口)、`blas-order`(自动探测时的优先顺序)。

#### 4.3.3 源码精读

`INSTALL.rst` 列出四项前置依赖,并明确区分「必需」与「测试专用」:
[INSTALL.rst:12-35](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/INSTALL.rst#L12-L35) — 中文说明:Python 3.12+ 与 Cython ≥ 3.0.6 是必需;pytest、Hypothesis 只在跑测试时需要。注意此处还提醒:开发 NumPy 本身请用 `spin`。

`INSTALL.rst` 给出两条「从源码得到 NumPy」的命令,并解释了 `spin` 与可编辑安装的关系:
[INSTALL.rst:50-76](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/INSTALL.rst#L50-L76) — 中文说明:`pip install .` 编译并安装;`spin build` 装到 `build-install/`,再用 `spin ipython` 进入能 `import numpy` 的解释器;`pip install -e . --no-build-isolation` 是可编辑安装。

BLAS 的可配置开关集中在 `meson.options`,默认 `allow-noblas=true`(即没有 BLAS 也能编出来,只是线性代数很慢):
[meson.options:1-18](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/meson.options#L1-L18) — 中文说明:`blas`/`lapack` 默认 `auto`(按 `blas-order` 自动探测);`use-ilp64` 控制是否用 64 位整数 BLAS 接口(超大矩阵才需要);`mkl-threading` 控制 MKL 的线程后端。`numpy/meson.build` 在 macOS 默认偏好 Accelerate、在 x86_64 默认偏好 MKL,否则 OpenBLAS。

`building_with_meson.md` 给出最小化的开发流程:装构建工具 → `spin build` → `spin test`/`spin ipython`:
[building_with_meson.md:7-31](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/building_with_meson.md#L7-L31) — 中文说明:`spin build` 在 `build/` 编译、装到 `build-install/`;也可手动 `export PYTHONPATH=...` 后 `pytest --pyargs numpy`。

#### 4.3.4 代码实践(本讲主实践)

这是贯穿本讲的完整实践:从源码构建 NumPy 并运行测试。**请按你本机的实际环境执行,不要假装已经跑过。**

1. 实践目标:亲自走完一次「源码 → 编译 → 安装 → 测试」全链路,理解每一阶段产物在哪。
2. 操作步骤:

   ```bash
   # 0. 进入仓库根目录
   cd /path/to/numpy

   # 1. 初始化子模块(拿到 vendored-meson;若已初始化可跳过)
   git submodule update --init

   # 2a. 方式一:spin 开发构建(推荐)
   spin build

   # 2b. 方式二:可编辑安装(不依赖 spin)
   pip install -e . --no-build-isolation

   # 3. 跑测试(任选其一)
   spin test                       # 默认 -m 'not slow',较快
   python -c "import numpy; numpy.test('full')"   # 直接调用,跑全套
   ```

3. 需要观察的现象:
   - 第 1 步会拉取 `vendored-meson/meson`。
   - 第 2 步会在 `build/` 下生成大量 Ninja 中间产物,首次编译可能耗时几分钟;成功后在 `build-install/` 出现可 `import` 的 NumPy。
   - 第 3 步会输出 pytest 的进度点与统计行。
4. 预期结果:`import numpy` 成功且 `numpy.__version__` 以 `2.6.0.dev0` 开头;`numpy.test('full')` 报告「N passed, X skipped」,其中 f2py 相关测试在缺 Fortran 编译器时会被跳过(属正常现象)。
5. 如果你的环境不允许完整编译或运行测试(例如受限的沙箱),请明确写「待本地验证」,并改为阅读 `INSTALL.rst` 与 `building_with_meson.md`,口头复述三条命令的用途。

#### 4.3.5 小练习与答案

**练习 1**:`meson.options` 里 `allow-noblas` 默认是 `true`。如果你要为生产环境构建一个高性能 NumPy,应该怎么改?为什么 PyPI wheel 的构建反而设成 `-Dallow-noblas=false`?

> 参考答案:高性能场景应显式指定 BLAS(如 `-Dblas=openblas`)并保留 `allow-noblas=true` 作为兜底,或干脆 `-Dallow-noblas=false` 强制要求找到真 BLAS,避免「意外编出一个线性代数极慢的 NumPy」。PyPI wheel 在 `pyproject.toml` 的 cibuildwheel 配置里设 `-Dallow-noblas=false`(见 `tool.cibuildwheel.config-settings`),就是为了保证分发给用户的 wheel 一定链接了真正的 BLAS。

**练习 2**:构建 NumPy 时报错 `Cannot compile Python.h`。根据本讲源码,最可能的原因和修复办法是什么?

> 参考答案:缺少 Python 开发头文件。`meson.build` 在第 41-43 行用 `cc.has_header('Python.h', dependencies: py_dep)` 检测,失败就报这个错。修复:Debian/Ubuntu 装 `python3-dev`;Windows/macOS 通常随 Python 自带。`INSTALL.rst` 第 17-21 行也说明了这一点。

## 5. 综合实践

把本讲三块知识串起来,完成一个「构建画像」任务:

1. 克隆仓库并 `git submodule update --init`,确认 `vendored-meson/meson/mesonbuild` 目录已存在(对应 `.spin/cmds.py:13-19` 的检查)。
2. 阅读 `pyproject.toml` 的 `[build-system]` 与 `[tool.spin.commands]`,在一张表里列出:`build` / `test` / `docs` / `bench` 四个命令分别映射到 `.spin/cmds.py` 的哪个函数。
3. 选定一种构建方式(`spin build` 或 `pip install -e . --no-build-isolation`),从源码构建 NumPy,记录首次编译耗时与产物目录(`build/`、`build-install/`)。
4. 用 `python -c "import numpy; print(numpy.__version__, numpy.__file__)"` 确认加载到的是你刚编译的本地版本(路径应指向 `build-install/...`),而不是 PyPI 装的版本。
5. 运行 `spin test`(或 `numpy.test('full')`),记录 passed / failed / skipped 的数量,并用一句话解释为什么会有 skipped(提示:f2py 测试与平台/编译器相关)。
6. 进阶(可选):用 `numpy.show_config()` 查看构建实际链接的 BLAS 后端,并和你在 `meson.options` 里看到的选择逻辑对应起来。

完成后,你应该能在不查文档的情况下,向别人解释「一条 `pip install .` 在 NumPy 仓库里到底触发了什么」。

## 6. 本讲小结

- NumPy 的 PEP 517 构建后端是 `meson-python`(`mesonpy`),构建依赖是 `meson-python` 与 `Cython`(≥3.1.0),运行期不需要它们。
- 构建链路是:pip → meson-python → Meson(描述)+ Cython(`.pyx`→`.c`)→ Ninja(调度)→ C/C++ 编译器 → 扩展模块。
- 仓库自带一份 `vendored-meson`,通过 `[tool.meson-python]` 指定,保证所有人用同一版本 Meson;若子模块未初始化,`.spin/cmds.py` 会在最早期就报错。
- `spin` 是为开发者设计的 CLI,命令在 `pyproject.toml` 的 `[tool.spin.commands]` 注册、实现在 `.spin/cmds.py`,每组命令都「扩展」了底层 `spin.cmds.meson.*` 父命令。
- `spin test` 默认带 `-m 'not slow'`,`spin test -m full` 才跑全套;`spin bench` 封装了 asv,支持 `--compare` 对比两个提交。
- 从源码安装有两条路径:`pip install .`(终端)与 `spin build`/`pip install -e . --no-build-isolation`(开发);BLAS/LAPACK 的选择集中在 `meson.options`,PyPI wheel 强制 `-Dallow-noblas=false`。

## 7. 下一步学习建议

下一讲 **u1-l3 顶层目录结构与模块导出** 将带你进入 `numpy/` 目录,看 `_core`、`lib`、`linalg` 等子包如何通过 `numpy/__init__.py` 的再导出汇聚到 `np.` 命名空间——那正好接在本讲「产物装到 `build-install/` 后被 `import`」的下游。

继续阅读建议:
- 想深入构建配置:通读 `meson.options` 与 `numpy/meson.build`,理解 CPU 分发(`cpu-baseline`/`cpu-dispatch`)与 SIMD 开关(`disable-svml`/`disable-highway`)。
- 想理解 wheel 如何打包:`pyproject.toml` 里 `[tool.cibuildwheel.*]` 段,以及 `tools/wheels/` 下的脚本。
- 想看构建期版本号是怎么算出来的:`numpy/_build_utils/gitversion.py`。
