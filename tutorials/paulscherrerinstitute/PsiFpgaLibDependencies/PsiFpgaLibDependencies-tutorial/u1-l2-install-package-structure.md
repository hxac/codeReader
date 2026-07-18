# 安装、打包与目录结构

## 1. 本讲目标

本讲承接上一讲（u1-l1 项目定位），把视角从「这个工具是干什么的」推进到「它怎么被装到电脑上、怎么被打包发布、仓库里每个文件又是做什么的」。

学完本讲，读者应该能够：

- 用 `pip3 install` 把 `dist/` 目录里的归档安装到本地 Python 环境，并验证导入成功。
- 看懂 `setup.py` 里 `setuptools.setup(...)` 的每一项配置，知道版本号写在哪里、包名怎么映射到磁盘目录。
- 说出仓库根目录下每一个文件（源码、文档、许可、配置、产物）的职责，并能解释「根目录本身就是包目录」这一特殊布局。
- 独立完成一次模拟打包：修改版本号 → `python3 setup.py sdist` → 检查 `dist/` 产物名与版本号是否一致。

---

## 2. 前置知识

在开始之前，读者需要具备以下基础概念。已经熟悉的同学可以快速浏览。

- **Python 包（package）与模块（module）**：一个 `.py` 文件就是一个模块；一个含有 `__init__.py` 的目录就是一个包。导入 `PsiFpgaLibDependencies` 时，Python 实际上是去导入某个目录里的 `__init__.py`。
- **pip**：Python 的包管理器，`pip3 install xxx` 会把一个包安装到当前 Python 环境的 `site-packages` 里，之后就能在任意地方 `import` 它。
- **归档（archive / sdist）**：把整个 Python 项目打成一个 `.tar.gz` 压缩包，里面包含源码和元数据（包名、版本、作者等）。别人拿到这个归档就能 `pip install`。
- **setuptools 与 setup.py**：`setuptools` 是 Python 传统的打包工具，`setup.py` 是它的配置脚本，里面调用 `setuptools.setup(...)` 描述「这个包叫什么、版本多少、包含哪些文件、依赖什么」。
- **语义化版本号**：形如 `主版本.次版本.修订号`（major.minor.bugfix），例如 `2.1.0`。本项目的版本策略会在本讲末尾的「版本号策略」里说明，更深入的版本号模型讲解在 u2-l2。

> 小提醒：本讲提到的「依赖」可能指两种不同的东西。一种是 **PSI FPGA 库依赖**（即上一讲说的、README 里声明、由本工具解析的那些库），本项目自身的此类依赖声明为 `None`；另一种是 **Python 包依赖**（即 `setup.py` 里 `install_requires` 声明的、运行本工具所需的第三方 Python 模块，本项目只有 `typing`）。两者不要混淆。

---

## 3. 本讲源码地图

本讲主要围绕下面几个文件展开。源码类文件中，只有 `setup.py` 和 `__init__.py` 与「打包/目录」直接相关；其余源码文件本讲只在「目录结构」里点到为止，深入讲解放在第二、三单元。

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [README.md](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md) | 文档 | `# Installation`、`# Packaing` 两节给出的安装与打包指令 |
| [setup.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py) | 打包配置 | `setuptools.setup(...)` 全部字段、`CustomSdist` 打包前清理逻辑 |
| [Changelog.md](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md) | 文档 | 版本变更记录，是「版本号该递增哪一位」的判断依据 |
| [.gitignore](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/.gitignore) | 配置 | 哪些产物（`*.egg-info`、`dist` 等）不该进版本库 |
| [__init__.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/__init__.py) | 源码（包入口） | 包对外导出哪些名字，验证「根目录即包目录」 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：先讲「怎么装」，再讲「怎么打包」，最后讲「整个仓库长什么样」。

### 4.1 安装方式

#### 4.1.1 概念说明

`PsiFpgaLibDependencies` 是一个**库（library）**，而不是带界面的应用程序。要让客户端代码（例如 `psi_common` 等项目）能 `import PsiFpgaLibDependencies`，必须先把包安装到本地 Python 环境。

本项目的安装思路非常朴素，分两步：

1. 从仓库的 `dist/` 目录里拿到对应版本的**归档文件**（一个 `.tar.gz`）。
2. 用 `pip3 install <归档路径>` 把它装进 Python 环境。

仓库当前 `dist/` 里已经自带了一个归档 `PsiFpgaLibDependencies-2.1.0.tar.gz`，所以读者无需先打包就能直接体验安装。

#### 4.1.2 核心流程

安装一条命令，但背后发生了一连串事情，理解它们有助于后面排错：

```text
拿到归档 dist/PsiFpgaLibDependencies-2.1.0.tar.gz
        │
        ▼
pip3 install <归档路径>
        │  1. 解压归档，读取其中的 PKG-INFO / setup.py 元数据
        │  2. 读取 install_requires（本项目为 ["typing"]）
        │     → 若 typing 缺失则一并安装
        │  3. 按 package_dir / packages 把源码复制到 site-packages
        ▼
PsiFpgaLibDependencies 出现在 site-packages
        │
        ▼
任意目录下：import PsiFpgaLibDependencies  ✓ 成功
```

关键点：归档里自带了 `setup.py` 与元数据，所以 pip 知道「包名是 `PsiFpgaLibDependencies`、要装哪些 `.py` 文件、依赖谁」——这些信息全部来自下一节要讲的 `setup.py`。

#### 4.1.3 源码精读

安装指令本身写在 README 里：

[README.md:37-42](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L37-L42) —— `# Installation` 一节，明确说明：**先到 `dist` 目录下载归档，再用 `pip3 install <archive>` 安装**。其中 `<archive>` 是占位符，实际使用时替换成归档的真实路径，例如：

```bash
pip3 install dist/PsiFpgaLibDependencies-2.1.0.tar.gz
```

> 术语解释：`pip3` 中的 `3` 强制使用 Python 3 的 pip。在某些系统里 `pip` 默认指向 Python 2，所以项目文档统一写 `pip3` 以避免歧义。

[README.md:15-16](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L15-L16) —— `## Dependencies` 写着 `None`。注意：这里说的「依赖为空」是指 **PSI FPGA 库依赖**（即本工具要解析的那些库），并不是说这个 Python 包没有任何运行时依赖。Python 包层面的依赖写在下文的 `setup.py` 里（`install_requires = ["typing"]`）。

#### 4.1.4 代码实践

**1. 实践目标**：亲手把现有的归档安装到一个干净的虚拟环境里，确认 `import` 成功，从而建立「归档 → 可导入的包」的直观认知。

**2. 操作步骤**：

```bash
# a) 在仓库根目录创建一个隔离的虚拟环境，避免污染系统 Python
python3 -m venv /tmp/psidep_venv

# b) 激活虚拟环境
source /tmp/psidep_venv/bin/activate

# c) 用现有归档安装本包
pip3 install dist/PsiFpgaLibDependencies-2.1.0.tar.gz

# d) 在任意目录下验证导入
python3 -c "import PsiFpgaLibDependencies; print('import OK')"
```

**3. 需要观察的现象**：步骤 (c) 中 pip 会打印它正在安装的包名、版本以及它顺带安装的 `typing` 依赖；步骤 (d) 应当输出 `import OK`。

**4. 预期结果**：归档被成功安装，`import PsiFpgaLibDependencies` 不报错。可在虚拟环境的 `site-packages` 目录里看到 `PsiFpgaLibDependencies/` 目录。

**5. 待本地验证**：上述输出依赖读者的本地环境，若 pip 提示找不到 `typing` 或网络受限导致 `typing` 安装失败，请检查网络或换用离线 wheel。本讲不在此处假装已运行该命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 要写成 `pip3 install <archive>` 而不是 `pip3 install PsiFpgaLibDependencies`（从 PyPI 装）？

> **参考答案**：本项目没有发布到公共 PyPI 仓库，而是以 `dist/` 里的归档形式随仓库分发。因此必须「指向归档文件路径」安装，而不是按包名从 PyPI 拉取。`<archive>` 这个占位符正是为了让读者填入实际归档路径。

**练习 2**：README 里 `## Dependencies` 写 `None`，但 `setup.py` 里又有 `install_requires`，二者矛盾吗？

> **参考答案**：不矛盾，它们说的是两种不同层次的依赖。README 的 `None` 指 **PSI FPGA 库依赖**（本工具领域意义上的、要被解析的那些库自身为空）；`setup.py` 的 `install_requires` 指 **Python 运行时依赖**（本工具运行所需的第三方 Python 模块 `typing`）。

---

### 4.2 setup.py 打包配置

#### 4.2.1 概念说明

`setup.py` 是整个项目的「出厂说明书」。它用 `setuptools` 告诉打包工具：

- 这个包叫什么名字、版本号是多少、作者是谁；
- 源码在磁盘的哪个目录、要打进包里哪些目录；
- 运行时依赖哪些第三方模块；
- 用 `sdist` 命令打源码归档时，要不要做额外的前置清理。

本项目还自定义了一个 `CustomSdist` 命令，**在每次打包前自动删除旧的 `dist` 与 `egg-info` 产物**，保证归档干净、不留旧版本残留。

#### 4.2.2 核心流程

打包流程可以用下面的伪代码描述：

```text
开发者改动代码
   │
   ▼
修改 setup.py 中的 version="..."    ← 唯一需要人改的版本来源
   │
   ▼
python3 setup.py sdist
   │
   ▼  触发 cmdclass["sdist"] = CustomSdist
CustomSdist.run():
   ├── shutil.rmtree("dist", ignore_errors=True)            # 清旧归档目录
   ├── shutil.rmtree("PsiFpgaLibDependencies.egg-info", ...) # 清旧元数据
   └── sdist.run(self)                                       # 正式打包
   │
   ▼
dist/PsiFpgaLibDependencies-<version>.tar.gz   ← 产物名由 name + version 拼出
```

重点：**归档文件名 = 包名 + 版本号**。所以改了 `setup.py` 的 `version` 后，新生成的归档名会自动跟着变——这正是本讲代码实践要验证的现象。

#### 4.2.3 源码精读

[setup.py:7-10](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L7-L10) —— 导入打包所需的模块：`setuptools` 提供打包能力，`shutil` 用于删除目录，`os` 是通用系统接口，`from setuptools.command.sdist import sdist` 引入官方的 `sdist` 命令类（后面要被 `CustomSdist` 继承）。

[setup.py:12-20](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L12-L20) —— `CustomSdist` 类。它继承自官方 `sdist`，重写了 `run()`：先删掉旧的 `dist` 与 `PsiFpgaLibDependencies.egg-info`（`ignore_errors=True` 表示目录不存在也不报错），再调用 `sdist.run(self)` 执行真正的打包。这段是「打包前清理」逻辑。

[setup.py:22-43](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L22-L43) —— 整个 `setuptools.setup(...)` 调用，逐项说明：

- `name="PsiFpgaLibDependencies"`（[L24](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L24)）：包名，也是 `import` 时用的名字和归档名前缀。
- `version="2.1.0"`（[L25](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L25)）：**全项目唯一的版本号来源**，与 `Changelog.md`、`dist/` 里归档名的版本必须保持一致。
- `author` / `author_email` / `description` / `license` / `url`（[L26-L30](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L26-L30)）：写入归档元数据，供 PyPI / pip 展示。
- `package_dir = {"PsiFpgaLibDependencies" : "."}`（[L31](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L31)）：**本讲最关键的一行**。它把包名 `PsiFpgaLibDependencies` 映射到磁盘的当前目录 `.`，也就是说——**仓库根目录本身就是这个包的目录**。这就是为什么 `Actions.py`、`Dependency.py` 等源码文件直接躺在根目录里。
- `packages = ["PsiFpgaLibDependencies"]`（[L32](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L32)）：声明要打进归档的包列表。
- `install_requires = ["typing"]`（[L33-L35](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L33-L35)）：Python 运行时依赖，pip 安装时会顺带装上 `typing`。
- `classifiers`（[L36-L39](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L36-L39)）：分类标签，声明这是 Python 3、跨平台。
- `cmdclass = {"sdist" : CustomSdist}`（[L40-L42](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L40-L42)）：把默认的 `sdist` 命令替换成上面定义的 `CustomSdist`，于是每次 `python3 setup.py sdist` 都会先清理再打包。

打包指令写在 README 里：

[README.md:44-49](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L44-L49) —— `# Packaing` 一节（注：原文标题拼写为 `Packaing`，缺少一个 `k`，但这是仓库里的真实写法，保留原样以便读者对照源码），说明打包前要先在 `setup.py` 改版本号，再执行 `python3 setup.py sdist`。

#### 4.2.4 代码实践

**1. 实践目标**：验证「改 `setup.py` 的 `version` → 归档名随之变化」，并观察 `CustomSdist` 的清理行为。

> 注意：本练习需要修改 `setup.py`。请读者在自己的**工作副本**上操作，练习结束后用 `git checkout -- setup.py` 还原，避免把临时的练习版本号提交进版本库。

**2. 操作步骤**：

```bash
# a) 备份并改版本号（例如改成 2.1.1）
cp setup.py /tmp/setup.py.bak
#     手工把 setup.py 里 version="2.1.0" 改成 version="2.1.1"

# b) 打包
python3 setup.py sdist

# c) 查看 dist 目录产物
ls dist/

# d) 还原 setup.py
git checkout -- setup.py      # 或：cp /tmp/setup.py.bak setup.py
```

**3. 需要观察的现象**：步骤 (b) 中终端会先看到旧 `dist/`、`egg-info` 被删除的迹象（或至少不报错），随后输出 `Creating tar archive` 与产物路径；步骤 (c) 的 `ls` 列出的归档名应当反映新版本号。

**4. 预期结果**：`dist/` 下出现新归档，文件名为 `PsiFpgaLibDependencies-2.1.1.tar.gz`（即 `name-version.tar.gz` 格式，版本号已跟随改动）。改回原版本号后再次打包，又会得到 `PsiFpgaLibDependencies-2.1.0.tar.gz`，与仓库现有归档同名。

**5. 待本地验证**：不同 setuptools 版本输出措辞略有差异，但归档文件名规则（`name-version.tar.gz`）稳定。若读者环境未安装 `setuptools`/`wheel`，可先 `pip3 install setuptools`。

#### 4.2.5 小练习与答案

**练习 1**：如果不重写 `CustomSdist`，直接用默认 `sdist`，打包还能成功吗？那为什么项目还要加这段清理逻辑？

> **参考答案**：默认 `sdist` 也能成功打包。加 `CustomSdist` 的目的是**保证归档干净**：旧的 `dist/` 里可能残留历史版本归档，旧的 `PsiFpgaLibDependencies.egg-info` 里可能残留旧元数据。打包前先删掉它们，可以避免把过期产物重新打包进去或造成版本号混淆。

**练习 2**：`package_dir = {"PsiFpgaLibDependencies" : "."}` 这一行如果不写会怎样？

> **参考答案**：`setuptools` 默认认为「名为 `PsiFpgaLibDependencies` 的包」对应磁盘上的 `PsiFpgaLibDependencies/` 子目录。本项目把源码直接放在仓库根目录，没有这个子目录，所以必须用 `package_dir` 显式把包名映射到 `.`（根目录）。不写的话，打包工具找不到对应的子目录，会把源码漏掉或直接报错。

**练习 3**：归档文件名 `PsiFpgaLibDependencies-2.1.0.tar.gz` 里的 `2.1.0` 是从哪里来的？

> **参考答案**：来自 [setup.py:25](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L25) 的 `version="2.1.0"`。`sdist` 用 `name` 和 `version` 自动拼出归档名，没有其它独立的版本来源。

---

### 4.3 目录结构

#### 4.3.1 概念说明

理解目录结构，等于拿到了整个仓库的「索引」。本项目目录非常扁平——所有源码、文档、许可、打包配置都直接放在仓库根目录，唯一的子目录是 `dist/`（产物）和 `PsiFpgaLibDependencies-tutorial/`（本学习手册）。

这种「扁平 + 根目录即包目录」的布局，直接由上一节 `package_dir = {"PsiFpgaLibDependencies" : "."}` 决定。

#### 4.3.2 核心流程

仓库可按职责分成四组：

```text
仓库根目录
├── 源码（5 个，构成 PsiFpgaLibDependencies 包）
│     ├── __init__.py     包入口，导出 Actions / Dependency / Parse
│     ├── Actions.py      动作执行：list / check / checkout + 命令行入口
│     ├── Dependency.py   依赖数据模型
│     ├── Parse.py        README 依赖解析
│     └── VersionNr.py    语义版本号模型
├── 文档
│     ├── README.md       说明 / 安装 / 打包 / 依赖声明
│     └── Changelog.md    版本变更记录
├── 打包与配置
│     ├── setup.py        setuptools 打包配置（含 CustomSdist）
│     └── .gitignore      忽略 IDE/产物文件
├── 许可
│     ├── License.txt     PSI HDL Library License
│     └── LGPL2_1.txt     LGPL 全文
├── 产物（由 setup.py 生成，部分被 .gitignore 忽略）
│     └── dist/PsiFpgaLibDependencies-2.1.0.tar.gz
└── PsiFpgaLibDependencies-tutorial/   本学习手册
```

#### 4.3.3 源码精读

[.gitignore:1-12](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/.gitignore#L1-L12) —— Git 忽略规则，能反推出项目运行/打包会生成哪些产物：

- [.gitignore:1-2](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/.gitignore#L1-L2)：忽略 PyCharm 的 `.idea/` 目录，说明开发者用 PyCharm。
- [.gitignore:3](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/.gitignore#L3)：忽略 `*.pyc`（Python 编译缓存）。
- [.gitignore:5-7](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/.gitignore#L5-L7)：忽略 `Example/**/*` 但保留 `Example/*.py`，暗示项目可能有示例输出目录（当前 HEAD 下 `Example/` 未提交进版本库）。
- [.gitignore:9-11](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/.gitignore#L9-L11)：忽略 `*.egg-info`（setuptools 生成的包元数据目录）。注意 `dist/` **没有**出现在 `.gitignore` 里，所以归档 `dist/PsiFpgaLibDependencies-2.1.0.tar.gz` 是被纳入版本库、随仓库分发的。

> 小观察：`CustomSdist` 在打包时会删 `egg-info`，而 `.gitignore` 也忽略 `egg-info`——二者一致，都是不让旧的包元数据混入仓库或归档。但 `dist/` 不被忽略，因为项目正是靠 `dist/` 里的归档来分发的。

[Changelog.md:1-11](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md#L1-L11) —— 变更记录，是判断「下个版本号该递增哪一位」的依据：

- [Changelog.md:1-3](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md#L1-L3)：`## 2.1.0` 新增了对已检出依赖的语义版本检查（Features）。新增功能 → 次版本号 `minor` 递增，正好对应 `2.0.0 → 2.1.0`。
- [Changelog.md:5-6](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md#L5-L6)：`## 2.0.0` 是开源化改造并改了库名。改库名属于不向下兼容的变更 → 主版本号 `major` 递增，对应 `1.x → 2.0.0`。
- [Changelog.md:9-10](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md#L9-L10)：`## 1.0.0` 首次发布。

[README.md:18-23](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L18-L23) —— `## Tagging Policy`，PSI 的语义化版本策略，明确写出三条递增规则：

- 不完全向后兼容 → 递增 `major`；
- 新增功能 → 递增 `minor`；
- 仅修 bug、无功能变化 → 递增 `bugfix`。

这条策略与 `Changelog.md` 的「Features / Changes / Bugfix」分类一一对应，是把「改动性质」翻译成「版本号动作」的桥梁。具体的版本号模型（如何解析、比较）在 u2-l2 详解。

[__init__.py:6-8](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/__init__.py#L6-L8) —— 包入口导出三个名字：`Actions`（整个模块）、`Dependency`（从 `Dependency.py`）、`Parse`（从 `Parse.py`）。注意 `VersionNr` 没有在这里直接导出，但 `Dependency` 内部会用到它。这行代码也佐证了「根目录即包目录」——三个 `.py` 都和 `__init__.py` 同级，靠相对导入 `.Dependency`、`.Parse` 就能找到。

#### 4.3.4 代码实践

**1. 实践目标**：把目录结构与 `setup.py`、`.gitignore` 对应起来，亲手验证「根目录即包目录」「产物归档随仓库分发」这两点。

**2. 操作步骤**：

```bash
# a) 列出仓库跟踪的全部文件（只看 git 跟踪的，过滤掉本地产物）
git ls-files

# b) 对照下表，把每个文件归类到「源码 / 文档 / 打包 / 许可 / 产物」

# c) 验证「根目录即包目录」：在仓库根目录直接当作包导入
python3 -c "import __init__; print('直接执行 __init__.py 中的导入')" 2>/dev/null \
  || python3 -c "import sys; sys.path.insert(0,'.'); import PsiFpgaLibDependencies as P; print('以包名导入成功:', P.__name__)"
```

> 说明：步骤 (c) 第二条命令把当前目录加入 `sys.path` 后以包名 `PsiFpgaLibDependencies` 导入——这等价于「安装后」的导入方式，能直观证明根目录里的几个 `.py` 文件确实组成了这个包。

**3. 需要观察的现象**：步骤 (a) 列出的源码文件正好是 `__init__.py / Actions.py / Dependency.py / Parse.py / VersionNr.py` 这 5 个；产物 `dist/PsiFpgaLibDependencies-2.1.0.tar.gz` 也在跟踪列表里（因为 `dist/` 未被 `.gitignore` 忽略）；而 `*.egg-info` 不在列表里（被忽略）。

**4. 预期结果**：导入命令输出成功信息，确认根目录 5 个 `.py` 共同构成 `PsiFpgaLibDependencies` 包。文件职责对照表如下：

| 文件 | 归类 | 职责 |
| --- | --- | --- |
| `__init__.py` | 源码 | 包入口，导出 `Actions` / `Dependency` / `Parse` |
| `Actions.py` | 源码 | 三类动作执行 + 命令行入口 `ExecMain` |
| `Dependency.py` | 源码 | 依赖数据模型 |
| `Parse.py` | 源码 | README 依赖解析 |
| `VersionNr.py` | 源码 | 语义版本号模型 |
| `README.md` | 文档 | 说明、安装、打包、依赖声明 |
| `Changelog.md` | 文档 | 版本变更记录 |
| `setup.py` | 打包 | `setuptools` 配置 + `CustomSdist` |
| `.gitignore` | 配置 | 忽略 IDE / `*.pyc` / `*.egg-info` / 示例输出 |
| `License.txt` | 许可 | PSI HDL Library License |
| `LGPL2_1.txt` | 许可 | LGPL 全文 |
| `dist/PsiFpgaLibDependencies-2.1.0.tar.gz` | 产物 | 可直接 `pip3 install` 的源码归档 |

**5. 待本地验证**：步骤 (c) 的导入是否成功取决于当前工作目录与 Python 环境；若 `typing` 缺失可能报 `ModuleNotFoundError: typing`（较新 Python 已内置 `typing`，通常不会出现）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dist/PsiFpgaLibDependencies-2.1.0.tar.gz` 出现在 `git ls-files` 里，而 `PsiFpgaLibDependencies.egg-info` 不会出现？

> **参考答案**：`.gitignore` 忽略了 `*.egg-info`（见 [.gitignore:10](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/.gitignore#L10)），所以 `egg-info` 不会被 Git 跟踪。而 `dist/` 没有被忽略——项目刻意把归档纳入版本库随仓库分发，让使用者不用自己打包就能 `pip3 install`，所以归档出现在跟踪列表里。

**练习 2**：根据 Tagging Policy，如果下一个版本只修了一个 bug、没有新功能，版本号应该怎么变？

> **参考答案**：递增 `bugfix`，即 `2.1.0 → 2.1.1`。依据是 [README.md:23](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L23)「If only bugs are fixed … the *bugfix* version is incremented」。

**练习 3**：仓库里一共有几个源码 `.py` 文件？哪个是「对外导入入口」？

> **参考答案**：5 个源码文件：`__init__.py`、`Actions.py`、`Dependency.py`、`Parse.py`、`VersionNr.py`。对外导入入口是 `__init__.py`（见 [__init__.py:6-8](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/__init__.py#L6-L8)），它决定了使用者 `import PsiFpgaLibDependencies` 后能直接拿到哪些名字。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「从源码到可安装归档」的完整流程。建议在虚拟环境中进行。

**任务**：模拟一次小版本发布，把版本号从 `2.1.0` 提升到 `2.1.1`，生成归档，安装并验证。

**步骤**：

1. **写变更记录**：在 `Changelog.md` 顶部新增一节（示例，仅用于练习）：
   ```markdown
   ## 2.1.1
   * Bugfix
     * Example fix description for the tutorial exercise
   ```
   按 Tagging Policy 判断：仅修 bug → 递增 `bugfix` → `2.1.0 → 2.1.1`（依据 [README.md:23](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L23)）。

2. **改版本号**：把 [setup.py:25](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L25) 的 `version="2.1.0"` 改成 `version="2.1.1"`。

3. **打包**：执行 `python3 setup.py sdist`，观察 `CustomSdist` 先清理 `dist/` 与 `egg-info`、再生成新归档（参考 [setup.py:12-20](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L12-L20)）。

4. **核对产物**：`ls dist/` 应当只出现 `PsiFpgaLibDependencies-2.1.1.tar.gz`（旧的 `2.1.0` 归档已被清理逻辑删掉）。

5. **安装验证**：
   ```bash
   python3 -m venv /tmp/rel_venv && source /tmp/rel_venv/bin/activate
   pip3 install dist/PsiFpgaLibDependencies-2.1.1.tar.gz
   pip3 show PsiFpgaLibDependencies | grep -E "Name|Version"
   ```
   `pip3 show` 输出的 `Version` 应当是 `2.1.1`。

6. **还原**：练习完成后用 `git checkout -- setup.py Changelog.md` 还原两个被改动的文件（本练习的所有改动仅限读者本地工作副本，**不要提交**）。

**自检问题**：
- 第 4 步为什么看不到旧的 `2.1.0` 归档？（答：`CustomSdist.run()` 里 `shutil.rmtree("dist", ignore_errors=True)` 先删了整个 `dist/`。）
- 第 5 步 `pip3 show` 的版本号是从归档里哪里读到的？（答：`setup.py` 的 `version` 字段，写入归档元数据 `PKG-INFO`。）

> 待本地验证：以上命令的实际输出依赖读者本地环境与网络（`typing` 是否能装上），但版本号与归档名的对应关系是稳定的。

---

## 6. 本讲小结

- 本包通过 `dist/` 里的归档 + `pip3 install <archive>` 安装，README 的 [Installation](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L37-L42) 一节给出了官方指令。
- `setup.py` 是全项目唯一的版本号来源（`version="2.1.0"`），归档名 `PsiFpgaLibDependencies-2.1.0.tar.gz` 由 `name + version` 自动拼出。
- `package_dir = {"PsiFpgaLibDependencies" : "."}` 是本项目的关键布局决定：**仓库根目录本身就是包目录**，所以源码 `.py` 文件直接散落在根目录。
- `CustomSdist` 在每次 `python3 setup.py sdist` 前自动清理 `dist/` 与 `egg-info`，保证归档干净；README 原文标题拼写为 `Packaing`（缺一个 `k`），属仓库真实写法。
- README 的 `Dependencies: None` 指 PSI FPGA 库依赖为空，而 `setup.py` 的 `install_requires = ["typing"]` 才是 Python 运行时依赖，二者是两个层次的概念，不可混淆。
- `.gitignore` 忽略 `*.egg-info` 但不忽略 `dist/`，所以归档随版本库分发；版本号怎么递增由 [Tagging Policy](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L18-L23) 与 `Changelog.md` 共同决定。

---

## 7. 下一步学习建议

到目前为止，读者已经知道这个包「装得上、打得出来、文件各自干嘛」，但还没有真正看过源码内部。下一讲 **u1-l3《包入口与客户端集成方式》** 会打开 `__init__.py` 与 `Actions.py`，讲清楚：

- `__init__.py` 导出的 `Actions / Dependency / Parse` 三个名字分别代表什么；
- 为什么本包没有独立 `main`，命令行入口 `Actions.ExecMain` 是如何被客户端（如 `psi_common`）在自己的脚本里调用的；
- 如何用「库式调用」而非「命令行直接运行」来使用本包。

完成 u1-l3 后，第一单元（入门）就结束了，读者即可进入第二单元，从 `Dependency` 数据模型开始深入源码。
