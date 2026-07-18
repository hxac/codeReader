# PsiPyUtils 是什么：定位、用途与版本策略

## 1. 本讲目标

本讲是整本学习手册的第一篇。读完本讲后，你应该能够：

- 说清楚 PsiPyUtils 是一个**什么样的库**——它解决什么问题、不解决什么问题；
- 看懂它的**许可证**（LGPL + PSI 例外）意味着什么；
- 理解它的**版本号语义**（major / minor / bugfix 何时各加一位）；
- 从 `README.md` 和 `Changelog.md` 中读出版本演进，特别是 **3.0.0 的破坏性变更**和 **3.0.1 的修复**；
- 知道这个库**如何被安装、如何被打包**。

本讲不涉及任何具体模块的源码细节，只建立全局认知。后续每一篇讲义都会建立在「这是个通用工具库」这个前提之上。

## 2. 前置知识

阅读本讲前，建议你具备以下基础（都是通俗概念，不深入）：

- **什么是 Python 库**：一段被多个项目复用的、打包好的 Python 代码。PsiPyUtils 就是这样一段代码。
- **什么是 `pip install`**：Python 官方的包安装命令，从一个 `.tar.gz`（源码分发包）或 PyPI 上把库装进你的环境。
- **什么是 `setup.py`**：Python 打包的入口脚本，里面写着包名、版本号、作者、依赖等信息。`setuptools` 是它背后的事实标准工具。
- **什么是 git submodule**：把另一个 git 仓库「嵌」进当前仓库的某个子目录。PsiPyUtils 历史上曾以这种方式被使用，现在改为 pip 包，但两种方式都仍然支持。
- **什么是语义化版本号**：形如 `3.0.1` 的三段式版本号，三段分别叫主版本（major）、次版本（minor）、修订号（bugfix）。本讲会讲清这个库里这三段各自的递增规则。

> 如果你已经熟悉以上概念，可以直接跳到第 4 节。如果你对「破坏性变更（不向后兼容）」这个说法陌生，简单理解就是：升级后，**以前能跑的代码可能要改才能继续跑**。

## 3. 本讲源码地图

本讲只读三个「元信息」文件，它们不实现任何功能逻辑，却决定了这个库**是什么、怎么装、怎么演进**：

| 文件 | 作用 | 本讲用来讲什么 |
| --- | --- | --- |
| `README.md` | 项目说明：维护者、许可证、收录原则、打标签策略、安装与打包方式 | 库的定位、许可证、版本号语义 |
| `Changelog.md` | 版本变更日志，从 V1.00 到 3.0.1 | 版本演进史、破坏性变更与修复 |
| `setup.py` | 打包脚本（setuptools） | 包名、版本号、依赖、自定义打包流程 |

这三个文件之外，仓库根目录还有 8 个功能模块（`FileWriter.py`、`TempWorkDir.py`、`ExtAppCall.py` 等），它们是后续讲义的主题，本讲暂不展开。

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**README 定位说明**、**Changelog 版本历史**、**setup.py 元信息**。

### 4.1 README 定位说明

#### 4.1.1 概念说明

很多开源项目会犯一个错误：把「业务专用代码」和「通用工具代码」混在同一个库里。PsiPyUtils 在 README 里**显式地划定了边界**，明确声明这个库**只收录与具体问题无关的通用 Python 功能**。

这条原则很重要，因为它解释了为什么这个库这么小（只有 8 个模块）、为什么它的内容看起来「东一块西一块」（临时目录、外部进程、XML、文本替换……）——因为它的收录标准是「**是否通用**」，而不是「是否属于某个领域」。

同时，README 还给出了**许可证**和**版本号语义**两条关键元信息，这两条决定了「能不能用、怎么用」和「升级会不会出问题」。

#### 4.1.2 核心流程

可以把 README 想象成一份「入库清单 + 使用契约」，它的逻辑顺序是：

1. **谁维护**：列出 maintainer 与 author。
2. **能不能用**：许可证（LGPL + PSI 例外）。
3. **能收录什么**：通用功能入库原则（belong / not belong 两份清单）。
4. **版本怎么变**：major.minor.bugfix 三段各自的递增规则。
5. **怎么装**：pip 包 与 git submodule 两种方式。
6. **怎么打包**：改版本号 + `python3 setup.py sdist`。

其中第 3、4 步是本讲重点：第 3 步定义了**库的边界**，第 4 步定义了**版本号怎么读**。

#### 4.1.3 源码精读

先看许可证。README 指出本库采用 **PSI HDL Library License**，本质是 **LGPL 加上一条针对固件/硬件二进制的额外例外**：

> [README.md:9-10](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L9-L10) —— 说明许可证是 LGPL2.1 加上为固件开发场景澄清条款的 PSI 例外。

简单理解：LGPL 允许你**以二进制形式**（包括 FPGA 比特流、flash 镜像等）链接、使用本库而不公开你自己的代码；但如果你**修改了本库的源码**并重新分发，则需要按 LGPL 条款开源你的修改。这个「例外」正是为了让硬件/固件工程师安心使用。

再看最关键的**入库边界**：

> [README.md:15-24](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L15-L24) —— 明确「应收录」的是通用、与具体问题无关的 Python 功能（如临时切换工作目录、执行外部程序并记录输出、语言扩展）；「不应收录」的是针对某个具体问题或程序专用的代码。

这条原则直接决定了仓库里 8 个模块的形态——它们都是**可以在任何 Python 项目里复用**的通用工具，没有一处是绑定某个特定业务系统的。

然后是**版本号语义（Tagging Policy）**：

> [README.md:26-31](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L26-L31) —— 稳定版用 major.minor.bugfix 标签；不向后兼容时升 major，加新功能时升 minor，仅修 bug 时升 bugfix。

可以用一张表把这三段总结清楚：

| 变化类型 | 升哪一段 | 例子 | 对使用者的含义 |
| --- | --- | --- | --- |
| 仅修 bug，无功能变化 | bugfix | 3.0.0 → 3.0.1 | 放心升级，代码不用改 |
| 新增功能，向后兼容 | minor | 2.0.0 → 2.1.0 | 可以升级，老代码不受影响 |
| 不向后兼容（破坏性） | major | 2.x → 3.0.0 | 升级前必须检查代码 |

最后是**安装方式**，README 给出 pip 包与 git submodule 两条路：

> [README.md:33-40](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L33-L40) —— `pip install` 安装打包好的 `.tar.gz`；或继续把仓库当作 git submodule 直接引用，保持对旧项目的向后兼容。

> [README.md:42-47](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L42-L47) —— 打包流程：先在 `setup.py` 里改版本号，再执行 `python3 setup.py sdist`。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是亲手验证「通用工具库」这条定位。

1. **实践目标**：用事实确认这个库收录的模块都符合「通用、非业务专用」原则。
2. **操作步骤**：
   - 在仓库根目录列出所有 `.py` 模块文件（不含 `Tests/`）。
   - 对每个模块，读它的文件头注释或类名，判断它是否「与某个具体业务问题无关」。
3. **需要观察的现象**：你应该看到模块名都偏「机制性」——`TempWorkDir`（临时目录）、`TempFile`（临时文件）、`FileWriter`（写文件）、`ExtAppCall`（外部进程）、`FileOperations`（文件查找）、`EnvVariables`（环境变量）、`TextReplace`（文本替换）、`XmlToolbox`（XML）。
4. **预期结果**：8 个模块**无一**绑定某个特定业务系统，全部是可以在任意 Python 项目里复用的通用功能，符合 README 的收录原则。
5. **如果无法确定运行结果**：列出文件名这一步是确定性的，可直接执行 `git ls-files '*.py'`（排除 `Tests/`）得到结果。

#### 4.1.5 小练习与答案

**练习 1**：假如有人提议给 PsiPyUtils 加一个「解析某台示波器专有数据格式」的模块，按 README 原则该不该收录？为什么？

> **参考答案**：不该收录。该模块是「针对某个具体问题或程序」的专用代码，违反 [README.md:23-24](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L23-L24) 的「不应收录」清单。它应当放在示波器项目自己的仓库里。

**练习 2**：某次提交只修了一个函数的 bug，没有改任何对外行为。按 Tagging Policy 应该升版本号的哪一段？

> **参考答案**：升 bugfix 段（如 3.0.1 → 3.0.2）。见 [README.md:31](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L31)。

### 4.2 Changelog 版本历史

#### 4.2.1 概念说明

`Changelog.md` 是这个库的「编年史」，记录每个版本**改了什么**。对一个会被多个项目依赖的工具库来说，Changelog 是使用者判断「能不能安全升级」的主要依据——尤其是其中标注为 **Non-reverse compatible changes**（不向后兼容的变更）的条目，这些条目对应着 major 版本号的跃升。

PsiPyUtils 当前的版本线是 **3.0.1**。理解从 2.x 到 3.0.x 的变化，是本讲实践任务的核心。

#### 4.2.2 核心流程

Changelog 的阅读顺序是**从新到旧**（最新版本在最上面）。每个版本块下分三类条目：

- **Non-reverse compatible changes**：破坏性变更，升级前必须处理（对应升 major）。
- **New Features**：新功能，向后兼容（对应升 minor，或在新 major 里一并引入）。
- **Bugfixes**：仅修 bug（对应升 bugfix）。

梳理本库的关键版本节点：

```
V1.00            首次发布
   ↓
1.1.0 / 1.1.1    文件通配操作、AbsPathLinuxStyle、ExtAppCall 跨平台修复
   ↓
1.2.0            FileWriter 增强（修改末行、空行、可选不覆盖）
   ↓
2.0.0            开源首发；从 Utils 改名为 PsiPyUtils（破坏性）
   ↓
2.1.0            新增 TextReplace 模块
   ↓
3.0.0            __init__.py 重导出（import 写法变化）；开始作为 pip 包分发（破坏性）
   ↓
3.0.1            TextReplace 两处 bug 修复
```

#### 4.2.3 源码精读

最新的 **3.0.1** 是一个纯 bugfix 版本，修了 `TextReplace.TaggedReplace()` 两处问题：

> [Changelog.md:1-6](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md#L1-L6) —— 3.0.1：让标签替换对行尾不敏感；并把匹配改为非贪婪（修复同文件多组标签时替换错误）。

这两个修复的原理会在后续 u3-l3（TextReplace）讲义里详细展开，本讲只需知道：**3.0.1 相对 3.0.0 没有破坏性变更，可以安全升级**。

真正需要使用者改代码的是 **3.0.0**，它是最近的破坏性版本：

> [Changelog.md:8-14](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md#L8-L14) —— 3.0.0 的「不向后兼容变更」：修改 `__init__.py`，使类可以不带文件名直接导入。旧写法 `from PsiPyUtils.FileWriter import FileWriter`，新写法 `from PsiPyUtils import FileWriter`。同时新增打包脚本、开始以 pip 包形式分发。

这就是从 2.x 升到 3.x 时**使用者必须改的代码**：把所有 `from PsiPyUtils.<模块> import <类>` 改成 `from PsiPyUtils import <类>`（旧写法在 3.0.x 里是否仍兼容，会在 u1-l2 通过读 `__init__.py` 验证）。

更早的破坏性变更是 **2.0.0** 的改名：

> [Changelog.md:20-23](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md#L20-L23) —— 2.0.0：首次开源发布；把包名从 `Utils` 改为 `PsiPyUtils` 以避免命名冲突。

#### 4.2.4 代码实践

这是本讲的**主实践任务（源码阅读 + 文档撰写型）**。

1. **实践目标**：通过阅读 Changelog，总结 3.0.0 的破坏性变更与 3.0.1 的修复，并给出从 2.2.0 升级到 3.0.1 的 import 调整清单。
2. **操作步骤**：
   - 打开 [Changelog.md](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md)，重点读 `## 3.0.0` 与 `## 3.0.1` 两块。
   - 写一段 3–5 行的总结，覆盖两点：(a) 3.0.0 相对 2.x 有哪些不向后兼容的变更；(b) 3.0.1 修复了什么。
3. **需要观察的现象**：你会注意到 3.0.0 同时包含一条「不向后兼容变更」和一条「新功能」（pip 包分发）；3.0.1 只有 bugfix，没有新功能。
4. **预期结果（参考答案）**：

   ```
   3.0.0 不向后兼容：__init__.py 改为重导出，import 写法从
     from PsiPyUtils.FileWriter import FileWriter
   变为
     from PsiPyUtils import FileWriter
   另新增打包脚本，开始以 pip 包分发。
   3.0.1 修复：TextReplace.TaggedReplace() 对行尾不敏感、改为非贪婪匹配。
   从 2.2.0 升级到 3.0.1：把所有 from PsiPyUtils.<模块> import <类>
   改写为 from PsiPyUtils import <类>。
   ```

5. **如果无法确定运行结果**：升级是否破坏旧写法的 `from PsiPyUtils.<模块> import ...`，需要读 `__init__.py` 才能确认，本讲标注为「待 u1-l2 验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 3.0.1 没有把 major 号升到 4？

> **参考答案**：因为 3.0.1 只修 bug、没有破坏性变更。按 [README.md:31](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L31)，仅修 bug 时只升 bugfix 段。

**练习 2**：2.1.0 引入 TextReplace 时为什么只升 minor（2.0.0 → 2.1.0），而不是升 major？

> **参考答案**：新增功能且向后兼容时升 minor。见 [README.md:30](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L30) 与 [Changelog.md:16-18](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md#L16-L18)。

### 4.3 setup.py 元信息

#### 4.3.1 概念说明

`setup.py` 是 Python 打包的事实标准入口。它调用 `setuptools.setup(...)`，把一组**元信息**（包名、版本、作者、依赖、分类器等）告诉打包工具，工具据此生成可分发的 `.tar.gz`（源码包）。

PsiPyUtils 的 `setup.py` 里有两处值得专门讲：一是 **`package_dir` 把仓库根目录映射成 `PsiPyUtils` 包**（这是它扁平布局能成立的关键）；二是 **`CustomSdist` 自定义了打包前的清理流程**。

> 本讲只讲元信息与打包流程；`setup.py` 还牵涉一个「依赖声明与实际 import 可能不一致」的小细节，留到 u5-l2（打包与发布）专门讨论。

#### 4.3.2 核心流程

`setup.py` 的执行流程：

1. 导入 `setuptools`、`shutil`、`os`，以及标准 `sdist` 命令类。
2. 定义 `CustomSdist`：在标准 `sdist` 执行前，先删掉旧的 `dist/` 和 `PsiPyUtils.egg-info/`，保证产物干净。
3. 调用 `setuptools.setup(...)`，传入：
   - 包名 `PsiPyUtils`、版本 `3.0.1`、作者、描述、许可证、URL；
   - `package_dir={"PsiPyUtils": "."}` —— **把当前目录当作 `PsiPyUtils` 包**；
   - `packages=["PsiPyUtils"]`；
   - `install_requires=["lxml"]` —— 声明运行时依赖；
   - `classifiers`、`cmdclass={"sdist": CustomSdist}`。

其中第 2 步（`package_dir`）是理解「为什么根目录的 `FileWriter.py` 在安装后变成了 `PsiPyUtils.FileWriter`」的钥匙。

#### 4.3.3 源码精读

版本号就在元信息里，和 Changelog 的 3.0.1 对应：

> [setup.py:18-27](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L18-L27) —— `setuptools.setup(...)`：包名 `PsiPyUtils`，版本 `3.0.1`，`package_dir={"PsiPyUtils": "."}` 把当前目录映射为包根，`packages=["PsiPyUtils"]`。

关键是 `package_dir={"PsiPyUtils": "."}` 这一行。它的含义是：**包 `PsiPyUtils` 的源码就在当前目录（`.`）**。也就是说，仓库根目录里的 `FileWriter.py`、`TempWorkDir.py` 等文件，安装后都会成为 `PsiPyUtils` 包下的子模块（`PsiPyUtils.FileWriter` 等）。这就是为什么这个项目采用**扁平布局**（所有模块直接放根目录，没有 `src/` 子目录）却仍能作为一个正常包被导入。

> [setup.py:28-30](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L28-L30) —— `install_requires=["lxml"]`，声明运行时依赖为 lxml。

> [setup.py:31-34](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L31-L34) —— `classifiers` 声明这是 Python 3、OS 无关的包。

再看自定义的打包命令 `CustomSdist`：

> [setup.py:8-15](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L8-L15) —— `CustomSdist`：在标准 `sdist.run()` 之前，用 `shutil.rmtree` 清理 `dist/` 和 `PsiPyUtils.egg-info/`，避免旧产物混入新包。

最后通过 `cmdclass={"sdist": CustomSdist}` 把这个自定义命令注册进去（见 [setup.py:35-37](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L35-L37)），这样执行 `python3 setup.py sdist` 时就会走清理-构建流程。

#### 4.3.4 代码实践

这是一个**可运行型实践**（需要本机有 Python 3 与 setuptools）。

1. **实践目标**：亲手打包出 `dist/PsiPyUtils-3.0.1.tar.gz`，并验证版本号来自 `setup.py`。
2. **操作步骤**：
   - 在仓库根目录执行：`python3 setup.py sdist`
   - 查看 `dist/` 目录下生成的文件名。
3. **需要观察的现象**：`CustomSdist` 会先删掉旧的 `dist/`，再重新构建；产物文件名应包含 `3.0.1`。
4. **预期结果**：生成 `dist/PsiPyUtils-3.0.1.tar.gz`（仓库里其实已经提交了一份同样的文件可供对照）。把 `setup.py` 里的 `version` 改成别的字符串（仅用于观察，**改完务必还原，不要提交**），再跑一次 `sdist`，文件名会随之变化——这就验证了版本号确实由 `setup.py:20` 的 `version="3.0.1"` 控制。
5. **如果无法确定运行结果**：若本机没有 setuptools，可改为只读 `dist/PsiPyUtils-3.0.1.tar.gz` 的文件名确认版本；或标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果删除 `package_dir={"PsiPyUtils": "."}` 这一行，安装后还能用 `from PsiPyUtils import FileWriter` 吗？为什么？

> **参考答案**：不能正常工作（或找不到包内容）。`package_dir` 把仓库根目录映射成 `PsiPyUtils` 包；删掉后，setuptools 会在默认位置（通常是与包同名的 `PsiPyUtils/` 子目录）找源码，而本仓库是扁平布局、没有这个子目录，于是打包出来的包会是空的或找不到模块。

**练习 2**：`CustomSdist` 为什么要先删 `dist/` 再构建？

> **参考答案**：避免上一次打包留下的旧 `.tar.gz` 或中间产物混入本次发布，保证分发包干净、版本唯一。见 [setup.py:10-12](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L10-L12)。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个贯穿性小任务：

**场景**：你是某个依赖 PsiPyUtils 的项目的维护者，团队当前锁定在 **2.2.0**，现在要升级到 **3.0.1**。请产出一份「升级说明」。

要求：

1. **判断升级性质**：依据 [README.md:26-31](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L26-L31) 的版本号语义，指出 2.2.0 → 3.0.1 跨越了一个 major 号，属于**破坏性升级**。
2. **列出必须改的代码**：依据 [Changelog.md:8-14](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md#L8-L14)，给出 import 改写规则（`from PsiPyUtils.<模块> import <类>` → `from PsiPyUtils import <类>`）。
3. **说明附带收益**：3.0.1 相对 3.0.0 还修复了 TextReplace 的两处 bug（[Changelog.md:1-6](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md#L1-L6)），如果项目用了标签替换，升级后行为更正确。
4. **给出安装方式**：依据 [README.md:33-40](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L33-L40)，可以用 `pip install dist/PsiPyUtils-3.0.1.tar.gz`，其中包的版本号由 [setup.py:20](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L20) 决定。

把以上四点整理成一段给团队同事看的中文说明（不超过 150 字）即为完成。

## 6. 本讲小结

- PsiPyUtils 是 PSI 出品的**通用 Python 工具库**，只收录与具体问题无关的通用功能，不收录业务专用代码（[README.md:15-24](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L15-L24)）。
- 许可证是 **PSI HDL Library License = LGPL2.1 + 固件/二进制使用例外**，允许以二进制形式链接使用而无需开源自有代码（[README.md:9-10](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L9-L10)）。
- 版本号遵循 **major.minor.bugfix**：破坏性变更升 major、新功能升 minor、仅修 bug 升 bugfix（[README.md:26-31](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L26-L31)）。
- 当前版本 **3.0.1**：3.0.0 是破坏性升级（import 写法变化 + 开始作为 pip 包分发），3.0.1 是纯 bugfix（[Changelog.md:1-14](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md#L1-L14)）。
- `setup.py` 用 `package_dir={"PsiPyUtils":"."}` 让扁平布局成立，并用 `CustomSdist` 在打包前清理旧产物（[setup.py:8-27](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L8-L27)）。
- 读源码要养成「**文档/Changelog 与实际实现相互印证**」的习惯——本讲的 import 变更是否完全破坏旧写法，就需要在下一篇读 `__init__.py` 才能定论。

## 7. 下一步学习建议

本讲只建立了全局认知，还没有进入任何功能模块。建议按以下顺序继续：

1. **下一篇 u1-l2（包结构与导入方式）**：读 `__init__.py`，弄清 3.0.0 的 import 变更到底是怎么实现的、旧写法是否仍然兼容——这是本讲留下的悬念。
2. **u1-l3（运行测试套件）**：学会用 `Tests/RunAll.py` 跑通整套测试，为后续每个模块的「源码 + 测试」对照阅读打好基础。
3. 进入 u2 单元后，开始接触库的**核心范式——上下文管理器**（`TempWorkDir`、`TempFile`、`FileWriter`），这是理解整个 PsiPyUtils 设计思想的关键。

如果想先对全库有个手感，可以直接浏览根目录的 8 个模块文件名，对照本讲的「通用工具库」定位，确认它们的命名是否符合「机制性、可复用」的特征。
