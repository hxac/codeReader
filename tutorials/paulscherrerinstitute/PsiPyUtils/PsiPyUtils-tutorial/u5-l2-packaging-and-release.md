# 打包、发布与版本管理

> 本讲是第 5 单元「XML 工具与进阶主题」第 2 篇，依赖 u1-l2 建立的「扁平布局 + `__init__.py` 重导出」认知。前置讲义里你已经知道：8 个模块摊在仓库根目录、靠 `setup.py` 的 `package_dir={"PsiPyUtils":"."}` 才成为合法包，且导入只依赖标准库。本讲把镜头对准 `setup.py` 这个文件本身——它是 PsiPyUtils 从「一堆 `.py`」变成「可 `pip install` 的包」的关键，也是 3.0.0 才正式登场的新机制。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 [`setup.py`](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py) 里 `setuptools.setup(...)` 的关键字段（`name`/`version`/`package_dir`/`packages`/`install_requires`/`cmdclass`）各自管什么。
- 解释 `package_dir = {"PsiPyUtils" : "."}` 如何把**扁平布局**映射成一个合法的顶层包，并据此判断「哪些文件会进分发包、哪些不会」。
- 看懂 `CustomSdist` 如何通过 `cmdclass` 替换默认的 `sdist` 命令、在打包前先清理旧产物。
- 用证据（`grep`）判断 `install_requires=["lxml"]` 这条依赖声明在当前代码里是否**冗余**，并理解「声明的依赖」与「实际 `import`」可能不一致这件事。
- 区分 PsiPyUtils 的两种分发方式——**pip 包**与 **git submodule**——各自适用场景与取舍。

## 2. 前置知识

本讲几乎不涉及 Python 语法细节，但有几个打包领域的名词先对齐：

- **发行物（distribution / sdist）**：把一个 Python 项目打成一个可安装的压缩包（`.tar.gz`）。「sdist」即 **source distribution**，里面装的是**源码**（区别于 wheel 这种已编译的二进制发行物）。PsiPyUtils 是纯 Python，所以用最简单的 sdist 即可。
- **setuptools**：Python 生态事实上的打包工具链，提供 `setup()` 函数与一系列「命令」（如 `sdist`、`install`、`bdist_wheel`）。`python3 setup.py sdist` 就是调用其中的 `sdist` 命令。
- **egg-info**：setuptools 打包时生成的一个**元信息目录**（`PsiPyUtils.egg-info/`），记录包名、版本、依赖等。它是构建过程的**中间产物**，不是最终发行物。
- **install_requires**：声明「这个包运行时依赖哪些第三方包」。`pip install PsiPyUtils` 时，pip 会自动把这里列的依赖一并装上。
- **cmdclass**：`setup()` 的一个参数，是一个「命令名 → 命令类」的字典，用来**用自己的子类替换 setuptools 内置命令**。本讲里它替换 `sdist`，从而插入一段自定义的清理逻辑。
- **git submodule**：把另一个 git 仓库「嵌」进当前仓库的某个子目录，作为子模块。PsiPyUtils 在 3.0.0 之前就是被别的项目当 submodule 使用的。

> 一个一句话总览：`setup.py` 是一张「这个包叫什么、版本号多少、代码在哪、依赖什么、打包时额外做什么」的**登记表**，外加一个「替换默认打包命令」的小钩子。本讲就是逐行读懂这张表。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [setup.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py) | 38 行 | 本讲主角。声明包元信息、用 `package_dir` 映射扁平布局、声明 `lxml` 依赖、用 `CustomSdist` 替换 `sdist` 命令。 |
| [README.md](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md) | 50 行 | 给出「安装」与「打包」两节的使用说明，以及版本号语义（major.minor.bugfix）。 |
| [Changelog.md](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md) | 43 行 | 版本演进记录；3.0.0 条目写明「Added packaging script and distribute as PIP package」。 |
| [__init__.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/__init__.py) | 13 行 | 包的入口；它决定了「装上 PsiPyUtils 后 `import PsiPyUtils` 会执行什么」。本讲用它核对分发包的内容。 |
| [.gitignore](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/.gitignore) | 11 行 | 告诉 git 忽略哪些打包中间产物（`*.egg-info`）。 |

## 4. 核心概念与源码讲解

本讲拆成 3 个最小模块，正好对应规格要求的三块知识：**①`setuptools.setup` 元信息**、**②`package_dir` 映射**、**③`CustomSdist(cmdclass)`**。依赖偏差（`lxml`）与分发方式（pip vs submodule）会作为这两块的延伸自然带出。

---

### 4.1 `setuptools.setup`：包的登记表

#### 4.1.1 概念说明

`setuptools.setup(...)` 是一张「登记表」。你告诉它这个包叫什么名字、是第几版、作者是谁、代码放在哪个目录、运行时依赖哪些别的包……它就会据此生成元信息（`PKG-INFO`、`egg-info`），并驱动后续的 `sdist` / `install` 等命令。理解 `setup.py` 的关键是：**它本身只是一个普通的 Python 脚本**——`python3 setup.py sdist` 执行这个脚本、调用 `setup()`、`setup()` 再根据参数决定怎么打包。所以你能在 `setup()` 调用**之前**写任意 Python 代码（PsiPyUtils 正是利用这一点定义了 `CustomSdist` 类）。

#### 4.1.2 核心流程

执行 `python3 setup.py sdist` 时发生的事：

1. Python 解释器从上到下执行 `setup.py`：先 `import` 三个模块、再定义 `CustomSdist` 类（此时只是定义，不执行）。
2. 调用 `setuptools.setup(...)`，把登记表交给 setuptools。
3. setuptools 解析命令行参数 `sdist`，查 `cmdclass` 发现 `sdist` 被替换成了 `CustomSdist`，于是实例化并运行 `CustomSdist`。
4. `CustomSdist.run()` 先清理旧产物，再调用父类 `sdist.run(self)` 收集源码、生成 `dist/PsiPyUtils-<version>.tar.gz` 与 `PsiPyUtils.egg-info/`。
5. 产出落在 `dist/` 目录。

#### 4.1.3 源码精读

`setup.py` 的前几行是导入，关键是第 4 行从 setuptools 拿到内置的 `sdist` 命令类（给 4.3 节替换用）：

> [setup.py:L1-L4](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L1-L4) —— 导入 `setuptools`、`shutil`（清理用）、`os`，以及内置的 `sdist` 命令类。`CustomSdist` 就要继承它。

登记表本体是 `setup()` 调用，先看它的「元信息字段」：

> [setup.py:L18-L25](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L18-L25) —— `name`、`version="3.0.1"`、`author`、`description`、`license="PSI HDL Library License, Version 1.0"`、`url`。这些会写进包的 `PKG-INFO`，在 PyPI 页面与 `pip show PsiPyUtils` 里都能看到。

`version="3.0.1"`（[setup.py:L20](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L20)）是**包名的版本**，与 git tag 一一对应（见 4.1.4 的核对练习）。`license` 字段写的「PSI HDL Library License」即 README 里说明的「LGPL2.1 + 固件/二进制使用例外」（[README.md:L9-L10](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L9-L10)）。

接下来三个字段（`package_dir` / `packages` / `install_requires` / `classifiers`）是本讲的重点，先一句话定位，4.2 节与 4.1 延伸再展开：

> [setup.py:L26-L34](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L26-L34) —— `package_dir` 把当前目录 `.` 映射成包 `PsiPyUtils`；`packages=["PsiPyUtils"]` 只声明顶层包；`install_requires=["lxml"]` 声明运行时依赖；`classifiers` 是 PyPI 的分类标签（Python 3、OS 无关）。

**依赖偏差（重点延伸）**：`install_requires` 声明了 `lxml`——

> [setup.py:L28-L30](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L28-L30) —— 运行时依赖只有 `lxml` 一项。

但只要对全仓库 `grep` 一次（见 4.1.4 与综合实践），就会发现**没有任何一个模块真的 `import lxml`**。唯一与 XML 相关的模块 `XmlToolbox` 用的是 Python 标准库：

> [XmlToolbox.py:L7-L8](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L7-L8) —— 只 `import os` 和标准库 `xml.etree.ElementTree as ET`，并不依赖 `lxml`。

这意味着：对**当前代码**而言，`lxml` 这条声明是冗余的——即使机器上没装 `lxml`，`import PsiPyUtils` 也照样成功（u1-l2 已用 `--no-deps` 安装验证过）。可能的成因是早期计划用 `lxml`（功能更强的第三方 XML 库）、后来改用标准库 `ElementTree` 却忘了同步 `install_requires`。这是一条**「声明与实现不一致」**的典型样本，也是本讲要训练的核对习惯：**`install_requires` 不会自动反映真实 `import`，需要人工核对**。

#### 4.1.4 代码实践

**实践：核对 `version` 与 `install_requires` 是否与代码 / git 一致。**

1. **目标**：用三条只读命令验证三件事实。
2. **操作步骤**：
   - 打开 `setup.py`，记下 `version="3.0.1"`（[setup.py:L20](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L20)）。
   - 执行 `git tag --list`，确认存在 `3.0.1` 这个 tag（应能看到 `2.0.0 / 2.1.0 / 3.0.0 / 3.0.1`）。
   - 执行（在仓库根目录）：
     ```
     grep -rn "lxml" --include="*.py" .
     ```
3. **需要观察的现象**：`grep` 只会在 `setup.py:29`（即 `install_requires` 那一行）命中，其余 `.py` 文件全部无命中。
4. **预期结果**：`lxml` 仅作为**声明**出现，没有任何模块**使用**它。结论一句话——「该依赖声明对当前代码冗余」。
5. 待本地验证：如果你机器上没装 `lxml`，可顺带 `python3 -c "import PsiPyUtils"` 复核导入不报错。

#### 4.1.5 小练习与答案

**练习 1**：若把 `install_requires=["lxml"]` 直接删掉，`python3 setup.py sdist` 还能成功吗？`import PsiPyUtils` 还能成功吗？

> **参考答案**：都能。`install_requires` 只影响「pip 安装本包时**额外**拉取哪些依赖」，不影响打包与导入本身。删掉它，`sdist` 照常生成 `.tar.gz`，`import PsiPyUtils` 也只用到标准库。

**练习 2**：`classifiers` 里写了 `Programming Language :: Python :: 3`（[setup.py:L31-L34](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L31-L34)）。这个字段对代码运行有影响吗？

> **参考答案**：没有。`classifiers` 纯粹是 PyPI 上的**分类标签**（给人看、给搜索筛选用），pip 不会据此强制版本。它不约束解释器版本，写 Python 3 只是一个声明。

---

### 4.2 `package_dir` 映射：把扁平布局变成合法包

#### 4.2.1 概念说明

setuptools 默认假设「包名 = 目录名」：要打一个叫 `PsiPyUtils` 的包，它就去找一个叫 `PsiPyUtils/` 的子目录。但 PsiPyUtils 用的是**扁平布局**——8 个模块（`FileWriter.py`、`ExtAppCall.py`、…）直接摊在仓库根目录，没有同名子目录（见 u1-l2）。`package_dir` 就是用来打破「包名 = 目录名」这个默认假设的：它是一个「**包名 → 实际目录**」的映射字典，告诉 setuptools「包 `PsiPyUtils` 的根目录其实是当前目录 `.`」。

#### 4.2.2 核心流程

`package_dir = {"PsiPyUtils" : "."}` 的效果：

1. setuptools 看到「包 `PsiPyUtils` 的根目录是 `.`（即 `setup.py` 所在目录）」。
2. 配合 `packages=["PsiPyUtils"]`，它把 `.` 当作包根，收集其中的 `.py` 文件作为包内容。
3. 安装时，`.` 下的模块会被安装到 `site-packages/PsiPyUtils/` 下——**安装后**目录结构不再是扁平的，而是规整成「`PsiPyUtils/` 包里装着 8 个模块」，与「包名 = 目录名」的常规布局外观一致。
4. 因此用户代码可以 `from PsiPyUtils import FileWriter`（新式）或 `from PsiPyUtils.FileWriter import FileWriter`（旧式）——两种写法在「安装后的包」上都成立。

一句话：**打包/安装时把扁平布局「规整」成正常包布局的，正是 `package_dir`**。

#### 4.2.3 源码精读

关键就一行：

> [setup.py:L26](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L26) —— `package_dir = {"PsiPyUtils" : "."}`：把当前目录映射为包 `PsiPyUtils` 的根。

紧跟着的 `packages` 字段声明「**哪些**包要打进去」：

> [setup.py:L27](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L27) —— `packages = ["PsiPyUtils"]`：只声明了顶层包，**没有** `PsiPyUtils.Tests`。

这条引出一个重要事实：`Tests/` 目录**不会**进入分发包——因为（a）它不在 `packages` 列表里，（b）它也没有 `__init__.py`（u1-l3 已说明它是命名空间包）。所以 `pip install` 装上 PsiPyUtils 后，你是拿不到 `Tests/` 的；测试只在源码仓库里。

`package_dir` 映射出来的「包内容」由 `__init__.py` 决定入口：

> [__init__.py:L6-L13](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/__init__.py#L6-L13) —— 把 8 个子模块的名字统一搬到包顶层（5 个类 + 3 个模块）。这是「安装后 `import PsiPyUtils` 会执行的内容」。

#### 4.2.4 代码实践

**实践：预测并核对「分发包里有什么」。**

1. **目标**：在动手打包前，先凭 `package_dir` / `packages` / `__init__.py` 三处信息，预测 sdist 会收集哪些文件。
2. **操作步骤**：
   - 列出仓库根目录的 `.py` 文件（`ls *.py`）。
   - 回答两个问题：`Tests/TestFileWriter.py` 会进包吗？`Changelog.md` / `License.txt` 会进 sdist 的 `.tar.gz` 吗？
3. **需要观察的现象**：
   - `Tests/*.py` **不进** `PsiPyUtils` 包（`packages` 没声明、且无 `__init__.py`）。
   - 但 `Changelog.md`、`README.md`、`License.txt`、`LGPL2_1.txt` 这类**非代码文件通常会进 sdist 的 `.tar.gz`**——sdist 默认会把 `setup.py` 同目录下的 `README*`、`Changelog*`、`LICENSE*` 等打包进去（setuptools 的 `MANIFEST` 默认规则）。
4. **预期结果**：包模块 = 8 个根目录 `.py` + `__init__.py`；sdist 压缩包还附带若干文档与许可证文件；`Tests/` 不在其中。精确清单以综合实践里「解压 `dist/PsiPyUtils-3.0.1.tar.gz`」为准。
5. 待本地验证：sdist 对非代码文件的收集行为受 setuptools 版本与是否有 `MANIFEST.in` 影响；本仓库**没有** `MANIFEST.in`，故走默认规则，实际清单请以本地解压结果为准。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `packages = ["PsiPyUtils"]` 改成 `packages = []`（同时保留 `package_dir`），打包会怎样？

> **参考答案**：`package_dir` 只是「目录映射」，真正决定「收集哪些包」的是 `packages`。`packages=[]` 表示**不打包任何 Python 模块**，生成的 sdist 里只剩元信息与文档，没有 `.py` 源码——`pip install` 后 `import PsiPyUtils` 会 `ModuleNotFoundError`。两者必须配合使用。

**练习 2**：为什么扁平布局「能跑」却仍要写 `package_dir`？不写会怎样？

> **参考答案**：不写 `package_dir`，setuptools 默认按「包名找同名目录」的规则去找 `./PsiPyUtils/`，而本仓库没有这个目录，于是收集不到任何模块（或安装后包是空的）。`package_dir={"PsiPyUtils":"."}` 是扁平布局得以成立的**前提**。

---

### 4.3 `CustomSdist(cmdclass)`：打包前先清理

#### 4.3.1 概念说明

`cmdclass` 是 `setup()` 的一个参数：一个「命令名 → 命令类」的字典。setuptools 内置了一批命令（`sdist`、`build`、`install`、`bdist_wheel`…），当你把 `cmdclass={"sdist": CustomSdist}` 传进去，就是在说：「以后执行 `sdist` 命令时，别用内置的那个了，用我写的 `CustomSdist`」。

`CustomSdist` 继承自内置的 `sdist`（[setup.py:L4](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L4) 导入），只**重写** `run` 方法、在「真正打包」之前**插入一段清理逻辑**，最后再 `super` 式地调用父类 `sdist.run(self)` 完成原本的打包。这是一种典型的**模板方法**思路：复用父类全部打包逻辑，只在入口处加料。

为什么需要清理？因为 `dist/`（上次打包的产物）和 `PsiPyUtils.egg-info/`（元信息中间产物）可能**残留旧版本**。如果上次打的是 3.0.0、这次改成了 3.0.1，不清理的话 `dist/` 里会同时躺着新旧两个 `.tar.gz`，容易把旧的误发出去。`CustomSdist` 每次**从干净状态**重新构建，保证 `dist/` 里只有本次产物。

#### 4.3.2 核心流程

`CustomSdist.run()` 的执行过程：

1. `shutil.rmtree("dist", ignore_errors=True)` —— 删除整个 `dist/` 目录；`ignore_errors=True` 表示「目录不存在也不报错」。
2. `shutil.rmtree("PsiPyUtils.egg-info", ignore_errors=True)` —— 同样删除 egg-info 中间产物。
3. `sdist.run(self)` —— 调用父类的 `run`，正式收集源码、生成新的 `dist/PsiPyUtils-<version>.tar.gz` 与新的 `PsiPyUtils.egg-info/`。

一句话：**先清场，再建**。

#### 4.3.3 源码精读

`CustomSdist` 的定义与 `cmdclass` 的注册：

> [setup.py:L8-L15](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L8-L15) —— `class CustomSdist(sdist):` 继承内置 `sdist`，重写 `run`：先 `shutil.rmtree` 清掉 `dist` 与 `PsiPyUtils.egg-info`（均 `ignore_errors=True`），再 `sdist.run(self)` 执行真正的打包。

> [setup.py:L35-L37](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L35-L37) —— `cmdclass = {"sdist" : CustomSdist}`：把 `sdist` 命令替换为 `CustomSdist`，使前述清理逻辑在每次 `python3 setup.py sdist` 时自动生效。

两个值得留意的细节（批判性观察，不必深究）：

- 代码注释 `#Build from directory above`（[setup.py:L14](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L14)）含义**不明确**——紧随其后的是 `sdist.run(self)`，它就在**当前工作目录**打包，并不「从上级目录」构建。该注释疑似历史遗留（待确认），与实际行为对不上，读源码时不要被它带偏。
- 清理用的是 `shutil.rmtree("dist", ...)` 而非 `os.remove`，因为 `dist/` 是**目录**；`ignore_errors=True` 让「首次打包时 `dist/` 还不存在」这种情况也不报错，是一种幂等写法。

`.gitignore` 也佐证了「egg-info 是中间产物」这一身份：

> [.gitignore:L9-L11](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/.gitignore#L9-L11) —— `#Packaging artifacts` 下只忽略了 `*.egg-info`；注意 `dist/` **没有**被忽略，所以本仓库的 `dist/PsiPyUtils-3.0.1.tar.gz` 是**被 git 跟踪**的（`git ls-files` 可见）。把发行物提交进 git 是较少见的做法（多数项目会 `.gitignore` 掉 `dist/`），但 PsiPyUtils 选择把它留在仓库里，便于使用者不打包也能直接 `pip install dist/...tar.gz`。

#### 4.3.4 代码实践

**实践：亲手打一次包，观察清理前后的 `dist/`。**

1. **目标**：触发 `CustomSdist` 的清理-构建流程，验证 `dist/` 被「先删后建」。
2. **操作步骤**：
   - 先看现状：`ls dist/`（仓库里已有 `PsiPyUtils-3.0.1.tar.gz`）。
   - 在 `dist/` 里**手动放一个假文件**，例如复制一份旧名：`cp dist/PsiPyUtils-3.0.1.tar.gz dist/PsiPyUtils-0.0.0.fake.tar.gz`，确认 `ls dist/` 能看到两个文件。
   - 执行 `python3 setup.py sdist`。
3. **需要观察的现象**：打包完成后 `ls dist/`——那个 `.fake.tar.gz` **不见了**，只剩本次生成的 `PsiPyUtils-3.0.1.tar.gz`。
4. **预期结果**：印证 `CustomSdist.run` 里 `shutil.rmtree("dist", ...)` 先清空了整个 `dist/`，再由父类重新生成，所以任何手动塞进去的「脏文件」都会被清掉。
5. 待本地验证：实际命令输出与产物清单以本地运行为准（本讲不假装已运行）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `CustomSdist` 重写的是 `run` 而不是 `__init__`？

> **参考答案**：`run` 是命令执行的入口（setuptools 实例化命令对象后调用 `run()`）。在 `run` 开头插清理逻辑、再 `sdist.run(self)` 转交父类，能精确地「在打包开始前」清场。重写 `__init__` 则太早（实例化时机不等于打包时机），且要处理父类构造参数，没必要。

**练习 2**：如果把 `cmdclass` 这一项从 `setup()` 里删掉，`python3 setup.py sdist` 还能跑吗？行为有何变化？

> **参考答案**：能跑，但用的是 setuptools **内置** `sdist`——它不会先清理 `dist/` 与 `egg-info/`，于是 `dist/` 里会**累积**多个版本的 `.tar.gz`。`cmdclass` 的作用就是用 `CustomSdist` 替换默认行为以避免这种累积。

---

### 4.4 分发方式与版本管理：pip 包 vs git submodule

本节把 `setup.py` 放回项目语境，谈「怎么发出去」与「版本号怎么演进」。它不是新的最小模块，而是 4.1–4.3 的应用与延伸。

#### 4.4.1 两种分发方式

README 的「Installation」一节给出两种用法：

> [README.md:L33-L40](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L33-L40) —— 一是 `pip install <root>\dist\PsiPyUtils-<version>.tar.gz`（用本讲生成的 sdist）；二是直接当 **git submodule** 使用，「allows for being reverse compatible and do not break projects that depend on using the package as submodule」。

两者取舍：

| 维度 | pip 包（3.0.0 起主推） | git submodule（3.0.0 前的旧法，仍支持） |
| --- | --- | --- |
| 安装 | `pip install xxx.tar.gz`，装进 `site-packages` | 把仓库嵌进你的项目子目录 |
| 版本锁定 | 靠 `.tar.gz` 里的版本号 / PyPI 版本 | 靠 submodule 指向的 commit |
| 依赖处理 | pip 自动拉 `install_requires`（即 `lxml`） | 不触发 `install_requires`，需自行管理 |
| 能否改源码 | 不便（装进 site-packages） | 方便（源码就在你仓库里，可改可调试） |
| 对旧项目 | 需要 `from PsiPyUtils import X` 新式写法 | 两种 import 写法都兼容 |

README 之所以保留 submodule 说法，正是为了「**不破坏**历史上把它当 submodule 依赖的项目」——这也呼应了 u1-l2 的结论：3.0.0 的 import 改动虽标注「不向后兼容」，但旧式 `from PsiPyUtils.FileWriter import FileWriter` 在 3.x 仍然能跑。

#### 4.4.2 版本号语义与「Changelog ≠ git 历史」

版本号语义在 README 里写得很清楚：

> [README.md:L26-L31](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L26-L31) —— 稳定版以 *major*.*minor*.*bugfix* 打 tag：破坏兼容升 major、加新功能升 minor、仅修 bug 升 bugfix。

`setup.py` 的 `version="3.0.1"`（[setup.py:L20](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L20)）必须与 git tag 手动保持一致——setuptools 不会替你检查。打包流程在 README 的「Packaing」节（原文有拼写笔误）：

> [README.md:L42-L47](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L42-L47) —— 「update the version number in *setup.py* and run `python3 setup.py sdist`」。即：**先改 `setup.py` 的版本号，再跑 sdist**。

这里有一个**值得警惕的真实不一致**，正好训练「批判性读源码」。Changelog 现在从 `2.1.0` 直接跳到 `3.0.0`，并把「打包脚本与 pip 分发」归功于 3.0.0：

> [Changelog.md:L8-L14](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md#L8-L14) —— 3.0.0 条目「Added packaging script and distribute as PIP package」。

但 git 历史与 tag 讲了另一个故事：仓库里存在 `2.2.0` 这个 tag，且对应提交信息明确写着「First release with PIP package」——也就是说，**第一个 pip 包其实是 2.2.0**：

```
ba49b4e RELEASE: 2.2.0 - First release with PIP package
5c8f168 CLEANUP: removed wrong entry in Changelog.md
1983dbc RELEASE: 3.0.0 with correct package
```

进一步 `git show 5c8f168` 可以看到，这次「Changelog 清理」**删除**的正是当初 2.2.0 那段空白条目（`## 2.2.0` 下只有空的「New Features」）。于是 Changelog 里 2.2.0 整段消失了，打包功劳被「挪」到了 3.0.0 名下（3.0.0 提交信息也叫「with correct package」，暗示 2.2.0 那次打包「不对」、3.0.0 才是修正版）。

教训：**Changelog 是人工维护的叙述，可能与 git tag / 提交历史不一致**；要确认「某个功能究竟在哪个版本引入」，最可靠的证据是 `git tag` + `git log`，其次才是 Changelog。这条经验会在 u5-l3「批判性读源码」里系统复用。

## 5. 综合实践

把三个最小模块串起来，做一次「**打一个包，并审一遍它的依赖声明**」的小任务。

**任务**：

1. 执行 `python3 setup.py sdist`，生成 `dist/PsiPyUtils-3.0.1.tar.gz`（观察 4.3.4 描述的「先清后建」）。
2. 解压查看内容结构：
   ```
   tar -tzf dist/PsiPyUtils-3.0.1.tar.gz | head -40
   ```
   核对：压缩包顶层目录名是否为 `PsiPyUtils-3.0.1/`？里面是否含 8 个模块的 `.py` 与 `__init__.py`？是否含 `README.md`/`Changelog.md`/`License.txt`/`LGPL2_1.txt`？是否**不含** `Tests/`？
3. 核对 `install_requires` 声明的 `lxml` 是否真被任何模块 `import`：
   ```
   grep -rn "lxml" --include="*.py" .
   ```
4. 写**一句话结论**：该依赖声明是否冗余，以及你建议如何处理（删除？保留以备将来用 `lxml` 重写 `XmlToolbox`？）。

**参考答案要点**（结论，非运行日志）：

- 步骤 2：分发包应是 `PsiPyUtils-3.0.1/` 目录形态，含 8 个模块 + `__init__.py` + `setup.py` + 若干文档/许可证；`Tests/` 不在其中（因 `packages` 未声明、且无 `__init__.py`）。精确清单以本地解压为准——待本地验证。
- 步骤 3：`grep` 仅在 `setup.py:29` 命中，无任何模块 `import lxml`。
- 步骤 4 结论：**对当前代码冗余**。建议要么删除该声明（保持依赖最小化），要么若计划用 `lxml` 增强 `XmlToolbox`（如支持 XPath 1.0 完整语法、XSLT），则保留并在代码里真正用上——「声明了却不用」是最该避免的中间态。

## 6. 本讲小结

- [`setup.py`](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py) 是一张「登记表 + 一个钩子」：`setup()` 声明 `name`/`version="3.0.1"`/`license`/`install_requires`/`classifiers` 等元信息（[L18-L34](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L18-L34)），`cmdclass` 提供替换内置命令的入口。
- `package_dir = {"PsiPyUtils":"."}`（[L26](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L26)）把**扁平布局**映射成合法包根，配合 `packages=["PsiPyUtils"]`（[L27](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L27)）决定收集哪些模块；`Tests/` 不进包。
- `CustomSdist`（[L8-L15](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L8-L15)）通过 `cmdclass={"sdist":CustomSdist}`（[L35-L37](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L35-L37)）替换默认 `sdist`，在打包前 `shutil.rmtree` 清掉 `dist/` 与 `egg-info/`，保证每次从干净状态构建。
- `install_requires=["lxml"]`（[L28-L30](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L28-L30)）与实际代码不符——全仓库无任何 `import lxml`，`XmlToolbox` 用的是标准库 `ElementTree`（[XmlToolbox.py:L7-L8](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L7-L8)），该声明对当前代码冗余。
- 两种分发方式：**pip 包**（3.0.0 起主推，靠 sdist）与 **git submodule**（旧法仍支持，便于改源码、保持旧 import 兼容）；`setup.py` 的 `version` 必须与 git tag **手动**对齐。
- **Changelog ≠ git 历史**：Changelog 把「首个 pip 包」归到 3.0.0，但 `2.2.0` tag 与提交 `ba49b4e "First release with PIP package"` 证明首个 pip 包其实是 2.2.0（其条目后被 `5c8f168` 当作「wrong entry」删掉）——确认功能引入版本应以 git 为准。

## 7. 下一步学习建议

- **本单元内**：去 u5-l3「测试组织与批判性读源码」。本讲点出的「`install_requires` 与实际 import 不一致」「Changelog 与 git 历史不一致」「`#Build from directory above` 注释含义不明」三处，都会在那里被系统归入「不被文档/注释/签名误导、逐行核对实现」的读源码训练；同时 u5-l3 会盘点 `setup.py`/打包流程**本身没有测试**这一覆盖缺口。
- **动手延伸**：尝试给本仓库补一个 `MANIFEST.in`（本仓库当前没有），对比有/无它时 sdist 收集的非代码文件清单差异；再尝试写一个最小的 `CustomInstall(cmdclass={"install":...})`，仿照 `CustomSdist` 的模板方法风格，理解 `cmdclass` 这一扩展点还能用在哪里。
- **标准库延伸**：若想理解 sdist 内部到底做了什么，可读 setuptools 文档的「Building and Distributing Packages」与 `setuptools.command.sdist` 的源码，重点看 `run` 默认实现如何遍历 `packages` + `package_dir` 收集文件、以及 `MANIFEST` / `MANIFEST.in` 的默认规则——这将让你看清 `CustomSdist` 复用的那套机制的全貌。
