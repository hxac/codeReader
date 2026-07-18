# 打包、版本与发布流程

## 1. 本讲目标

本讲是专家层（u3）的收尾篇，回答一个工程化问题：**改完代码之后，怎么把它打成可分发的归档、版本号该怎么动、什么时候该发新版本？**

学完后你应当能够：

1. 读懂 `setup.py` 中 `CustomSdist` 的打包前清理逻辑，说清楚它为什么每次打包前都要删掉 `dist/` 和 `egg-info`。
2. 掌握 `python3 setup.py sdist` 的完整执行流程：命令如何被 `cmdclass` 分发到 `CustomSdist`、归档名怎么由 `name`+`version` 拼出来、产物落在哪里、哪些产物会被提交、哪些会被忽略。
3. 理解 README 中的语义化版本 Tagging 策略（major / minor / bugfix 各自在什么条件下递增），并能对照 `Changelog.md` 中的历史版本给出正确判断。

> 承接提醒：本讲依赖 u1-l2 中已建立的「`setup.py` 打包配置、`package_dir` 把仓库根目录映射成包目录、`dist/` 随版本库分发」等认知，不再重复展开，只在此基础上补全**打包前清理、归档生成、版本策略**三块拼图。

## 2. 前置知识

阅读本讲前，建议先了解以下几个朴素概念（不熟悉的术语会在用到时再解释）：

- **Python 包（package）**：一个可被 `import` 的代码目录。本项目的「包名」是 `PsiFpgaLibDependencies`，但它的源码并不在子目录里，而是直接散落在仓库根目录——这是 u1-l2 讲过的 `package_dir = {"PsiFpgaLibDependencies": "."}` 的效果。
- **打包（packaging）**：把源码 + 元数据（名字、版本、依赖、作者……）组装成一个可分发的归档文件（`.tar.gz`）。本项目用的是 `sdist`（**s**ource **dist**ribution，源码分发）。
- **setuptools 命令（command）**：`python3 setup.py <命令>` 中的「命令」是一个可被替换的构建步骤。`sdist` 就是其中之一，还有 `build`、`install`、`bdist_wheel` 等。setuptools 允许你用 `cmdclass` 把内置命令替换成自己的子类——这是理解 `CustomSdist` 的关键。
- **语义化版本号（Semantic Versioning）**：形如 `major.minor.bugfix`（如 `2.1.0`），三段数字各有含义。这是 u2-l2 里 `VersionNr` 类所解析的格式，也是本包对外发布版本号的唯一格式。

## 3. 本讲源码地图

本讲聚焦三个文件，各司其职：

| 文件 | 在本讲中的角色 |
| --- | --- |
| [setup.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py) | 打包脚本。定义 `CustomSdist`（打包前清理）与 `setup()`（包元数据 + 命令注册）。全项目唯一的版本号来源。 |
| [Changelog.md](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md) | 变更日志。记录每个版本新增了哪些 Features / Changes，是判断「该递增哪一位版本号」的事实依据。 |
| [README.md](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md) | 项目说明。其中的 `## Tagging Policy` 段定义了 major/minor/bugfix 的递增规则，`# Packaing` 段给出打包命令。 |

> 备注：README 中章节标题写作 `# Packaing`（少了一个 `c`，应为 Packaging），这是源码里就存在的拼写问题；引用时按原文保留。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应一次真实发布的三个步骤：**先清理 → 再打包 → 最后定版本号**。

### 4.1 CustomSdist 打包前清理

#### 4.1.1 概念说明

`CustomSdist` 解决的问题很具体：**默认的 `sdist` 命令在打包时不会清理上一次构建留下的产物**。如果版本号变了，旧的归档（比如 `PsiFpgaLibDependencies-2.0.0.tar.gz`）会一直留在 `dist/` 里；如果版本号没变，旧的 `egg-info` 元数据也可能干扰新一次打包。

本项目把 `dist/` 提交进了版本库（详见 4.2 节），这就让「残留产物」问题更突出——**提交进库的 `dist/` 应当永远只包含当前版本的唯一一个归档**。`CustomSdist` 通过「每次打包前先把旧产物全删掉」来保证这个不变式。

它实现这一点的手段是 Python 的**继承 + 方法重写（override）**：

- `CustomSdist` 继承自 setuptools 内置的 `sdist` 命令类；
- 它重写了 `run()` 方法（即「执行这个命令时要做的事」）；
- 在 `run()` 里先做自己的清理，再调用父类的 `sdist.run(self)` 把真正的打包工作交给 setuptools。

这是一种典型的**模板方法（template method）**思想：不重写整个打包逻辑，只在标准流程前后插入自己的钩子。

#### 4.1.2 核心流程

`CustomSdist.run()` 的执行顺序：

1. `shutil.rmtree("dist", ignore_errors=True)` —— 删除整个 `dist/` 目录。
2. `shutil.rmtree("PsiFpgaLibDependencies.egg-info", ignore_errors=True)` —— 删除上一次打包留下的 egg-info 元数据目录。
3. `sdist.run(self)` —— 调用父类的 `run()`，执行标准 sdist 打包流程。

用伪代码表示：

```text
CustomSdist.run():
    删除 dist/                 # 旧归档清零
    删除 *.egg-info/           # 旧元数据清零
    调用 sdist.run()           # 交给 setuptools 重新生成
```

> **为什么要 `ignore_errors=True`？** 第一次打包时 `dist/` 和 `egg-info` 可能根本不存在。`ignore_errors=True` 让 `rmtree` 在目标目录缺失时不报错（否则首次打包就会抛 `FileNotFoundError`）。这是一种「幂等」写法——无论目录在不在，清理这一步都不会失败。

#### 4.1.3 源码精读

[setup.py:12-20](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L12-L20) 定义了 `CustomSdist` 类：

```python
#Cleanup before sdist
class CustomSdist(sdist):
    def run(self):
        #Cleanup before building
        shutil.rmtree("dist", ignore_errors=True)
        shutil.rmtree("PsiFpgaLibDependencies.egg-info", ignore_errors=True)

        #Build from directory above
        sdist.run(self)
```

逐行说明：

- 第 13 行 `class CustomSdist(sdist):` —— 继承 setuptools 的 `sdist` 命令（`sdist` 在文件开头第 10 行 `from setuptools.command.sdist import sdist` 导入）。
- 第 14 行 `def run(self):` —— 重写 `run()`。setuptools 执行命令时调用的就是这个方法。
- 第 16-17 行 —— 两步清理。注意第 17 行硬编码了 `PsiFpgaLibDependencies.egg-info`，这是因为 egg-info 目录名 = `{包名}.egg-info`，而这个包名在第 24 行写死为 `PsiFpgaLibDependencies`。
- 第 20 行 `sdist.run(self)` —— 把 `self` 传给父类的 `run()`，恢复标准打包流程。注释 `Build from directory above` 是作者留下的备注。

#### 4.1.4 代码实践

**实践目标**：在不真正触发一次完整 sdist 的前提下，单独验证 `CustomSdist` 的清理行为，理解「目录在/不在都能安全清理」。

**操作步骤**（源码阅读型 + 本地小验证，**在仓库副本或临时目录中进行，不要污染正式 `dist/`**）：

1. 阅读上面的 [setup.py:12-20](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L12-L20)。
2. 在一个临时目录里手写一个最小复刻脚本（**示例代码，非项目原有文件**）：

   ```python
   import shutil, os
   os.makedirs("demo/dist", exist_ok=True)
   open("demo/dist/old.txt", "w").close()
   # 复刻 CustomSdist.run 的清理两行
   shutil.rmtree("dist", ignore_errors=True)
   shutil.rmtree("PsiFpgaLibDependencies.egg-info", ignore_errors=True)
   ```

3. 把它放在一个临时子目录里运行，观察 `demo/dist` 是否被删、以及 egg-info 不存在时是否报错。

**需要观察的现象**：
- `dist` 被整个删除。
- 第二个 `rmtree` 指向一个不存在的目录，但因 `ignore_errors=True` 不抛异常。
- 若把 `ignore_errors=True` 去掉，第二个 `rmtree` 会抛 `FileNotFoundError`。

**预期结果**：脚本正常退出，验证了「幂等清理」的安全性。

> **待本地验证**：上述复刻脚本在临时目录中的实际运行输出请自行执行确认。务必不要在仓库根目录直接删 `dist/`——那是被版本库追踪的正式产物。

#### 4.1.5 小练习与答案

**练习 1**：如果 `CustomSdist` 漏掉了对 `egg-info` 的清理（只删 `dist`），最坏会带来什么后果？

**参考答案**：上一次打包生成的 `egg-info/SOURCES.txt` 等元数据可能过期，新一次打包若复用了旧元数据，归档内的文件清单可能与实际源码不符；严重时会把已删除的源文件继续打进归档。清理掉它能让 setuptools 重新扫描源码生成最新清单。

**练习 2**：为什么第 17 行写死字符串 `"PsiFpgaLibDependencies.egg-info"` 而不是用变量？

**参考答案**：egg-info 目录名由包名决定，而包名在第 24 行写死为 `"PsiFpgaLibDependencies"`，两者是同一个常量的两处字面量。这种写法的代价是「改名要改两处」，是一种可维护性上的小瑕疵；好处是简单直接、无依赖。

---

### 4.2 sdist 打包流程与产物

#### 4.2.1 概念说明

`CustomSdist` 只是「钩子」，真正的打包由 `sdist.run(self)` 完成。`sdist`（source distribution）会做三件事：

1. 收集应当打进归档的文件（由 `packages`、`package_dir` 决定，外加 `MANIFEST.in` 若有的话）。
2. 在项目根目录写出一个 `{包名}.egg-info/` 元数据目录（含 `PKG-INFO`、`SOURCES.txt`、依赖声明等）。
3. 把源码连同元数据打成 `dist/{name}-{version}.tar.gz` 归档。

关键认知有三个，初学者最容易混淆：

- **命令分发靠 `cmdclass`**：`setup()` 里的 `cmdclass = {"sdist": CustomSdist}` 告诉 setuptools，「执行 `sdist` 命令时不要用默认实现，改用 `CustomSdist`」。没有这一行，`CustomSdist` 类写了也不会被调用。
- **归档名 = `name` + `version`**：归档文件名完全由 `setup()` 的 `name` 和 `version` 两个字段拼出来，与 git tag、Changelog 无直接耦合。
- **版本号有且只有一个来源**：`setup.py` 里的 `version="2.1.0"` 是全项目唯一的版本号真值。Changelog 的标题、git tag、归档名里的版本，都应当与它对齐，但它本身不自动同步——需要发布者手动维护。

另一个重要事实（u1-l2 已建立，这里复核）：`dist/` **没有**被 `.gitignore` 忽略，而 `*.egg-info` **被**忽略了。

- 查看 [.gitignore:9-11](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/.gitignore#L9-L11)：

  ```text
  #Packaging artifacts
  *.egg-info
  ```

  只忽略了 `*.egg-info`，没有 `dist/`。

- 因此当前仓库里追踪了 `dist/PsiFpgaLibDependencies-2.1.0.tar.gz` 这一个归档（可用 `git ls-files dist` 验证）。

这就解释了 `CustomSdist` 为什么非要在打包前清空 `dist/`：**因为 `dist/` 是要提交的，它必须始终只代表「当前版本」**。如果不清空，多次打包后 `dist/` 会堆积多个版本的归档，全部被提交进库，造成混乱。而 `egg-info` 是被忽略的临时产物，清理它只是为了打包正确性，不是为了版本库整洁。

#### 4.2.2 核心流程

执行 `python3 setup.py sdist` 时的完整链路：

```text
python3 setup.py sdist
        │
        ▼
setuptools 读取 setup() 调用
        │  发现 cmdclass={"sdist": CustomSdist}
        ▼
分发到 CustomSdist.run()
        │
        ├─ rmtree("dist")              ← 4.1 模块的清理
        ├─ rmtree("*.egg-info")
        │
        ▼
sdist.run(self)  ← setuptools 标准流程
        │
        ├─ 扫描 packages/package_dir 收集源码
        ├─ 写出 PsiFpgaLibDependencies.egg-info/  （被 git 忽略）
        └─ 生成 dist/{name}-{version}.tar.gz      （被 git 追踪）
```

归档命名规则可写作：

\[ \text{归档名} = \text{name} \text{ + "-" } \text{ + version} \text{ + ".tar.gz"} \]

代入本项目即 `PsiFpgaLibDependencies` + `-` + `2.1.0` + `.tar.gz` = `PsiFpgaLibDependencies-2.1.0.tar.gz`。

#### 4.2.3 源码精读

先看 `setup()` 的元数据与命令注册。 [setup.py:23-43](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L23-L43)：

```python
setuptools.setup(
    name="PsiFpgaLibDependencies",
    version="2.1.0",
    ...
    package_dir = {"PsiFpgaLibDependencies" : "."},
    packages = ["PsiFpgaLibDependencies"],
    install_requires = [
        "typing"
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent"
    ],
    cmdclass = {
        "sdist" : CustomSdist
    }
)
```

关键点对照：

- [setup.py:24-25](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L24-L25) `name` 与 `version` —— 决定归档名，`version` 是全项目唯一版本号来源。
- [setup.py:31-32](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L31-L32) `package_dir` 把包名 `PsiFpgaLibDependencies` 映射到当前目录 `.`，`packages` 声明要打包的就是这一个包——所以根目录下的 `__init__.py`、`Actions.py` 等会被打进归档（详见 u1-l2）。
- [setup.py:33-35](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L33-L35) `install_requires=["typing"]` —— 这是 **Python 运行时依赖**，别和 README 里的 `Dependencies: None`（指 PSI FPGA 库依赖为空）混淆，u1-l2 已强调过。
- [setup.py:40-42](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L40-L42) `cmdclass` —— 把 `sdist` 命令替换成 `CustomSdist`，这是 4.1 节清理逻辑得以触发的「开关」。

再看 README 中给出的打包命令。 [README.md:44-49](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L44-L49)：

```text
# Packaing
To package the project after making changes, update the version number in *setup.py* and run

python3 setup.py sdist
```

这段官方说明点出了发布的两步：**先改 `setup.py` 里的版本号，再跑 `sdist`**。它没提「清理 dist」——因为那已由 `CustomSdist` 自动完成，发布者无需手动删。

最后看产物落地。归档落在 `dist/` 目录：

- 现存归档：`dist/PsiFpgaLibDependencies-2.1.0.tar.gz`（可由 `git ls-files dist` 确认被追踪）。
- 安装方式见 [README.md:37-42](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L37-L42)：`pip3 install <archive>`。

#### 4.2.4 代码实践

**实践目标**：在仓库**副本**中触发一次 sdist，观察产物生成、命名与清理行为的真实表现。

**操作步骤**：

1. 先确认当前 `dist/` 内容（只读，不改任何东西）：

   ```bash
   ls dist
   git ls-files dist
   ```

   预期看到唯一一个归档 `PsiFpgaLibDependencies-2.1.0.tar.gz`。

2. 把整个仓库复制一份到临时目录（避免污染正式 `dist/`），在副本里执行：

   ```bash
   python3 setup.py sdist
   ls dist
   ls -d *.egg-info
   ```

**需要观察的现象**：
- 打包前 `dist/` 与 `egg-info` 被 `CustomSdist` 清空（若副本里本来就没有，也不报错）。
- 打包后 `dist/` 重新生成 `PsiFpgaLibDependencies-2.1.0.tar.gz`。
- 项目根目录多出 `PsiFpgaLibDependencies.egg-info/` 目录（**未被清理**，会被 `.gitignore` 忽略）。

**预期结果**：归档名 = `name-version.tar.gz`，与 [setup.py:24-25](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L24-L25) 两个字段一致。

> **待本地验证**：sdist 在你本机的实际产物清单（如归档内的文件列表）请用 `tar tzf dist/PsiFpgaLibDependencies-2.1.0.tar.gz` 自行核对。注意：不要在正式仓库根目录直接跑 sdist，否则会改写被追踪的 `dist/`。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `setup()` 里的 `cmdclass` 那一行删掉，`CustomSdist.run()` 还会被执行吗？为什么？

**参考答案**：不会。`cmdclass` 是「命令名 → 实现类」的注册表，删掉它后，setuptools 会退回内置的默认 `sdist` 实现，`CustomSdist` 类虽然定义了但无人调用，打包前清理也就不会发生。

**练习 2**：为什么 `egg-info` 被 `.gitignore` 忽略，而 `dist/` 没有？

**参考答案**：`egg-info` 是构建过程的中间元数据，可由 sdist 随时重新生成，属于临时产物，故忽略。`dist/` 里的归档是本项目的**分发载体**——README 明确要求用户「下载 `dist` 里的归档再 `pip3 install`」，所以归档必须随版本库分发，故不忽略。这也正是 `CustomSdist` 每次清空 `dist/` 的意义：保证被提交的 `dist/` 永远只含当前版本的一个归档。

**练习 3**：归档文件名由哪些字段决定？若只改了 `Changelog.md` 而忘了改 `setup.py` 的 `version`，归档名会怎样？

**参考答案**：归档名只由 `setup()` 的 `name` 和 `version` 决定，与 Changelog、git tag 无自动关联。若忘了改 `version`，新打的归档名仍会是旧版本号（如仍是 `...-2.1.0.tar.gz`），内容却是新代码——这正是 README 反复强调「先改 setup.py 版本号」的原因。

---

### 4.3 语义版本号与 Tagging 策略

#### 4.3.1 概念说明

「该发新版本了吗？版本号怎么动？」这两个问题的答案分别由两份文件给出：

- **规则**在 README 的 `## Tagging Policy` 段：它定义了 `major.minor.bugfix` 三段各自的递增条件。
- **事实**在 `Changelog.md`：它记录「这次改了什么」，套用规则就能推出「该递增哪一位」。

PSI 的版本号格式与 u2-l2 中 `VersionNr` 类解析的完全一致：三段整数 `major.minor.bugfix`。三段的语义递增规则（见 [README.md:18-23](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L18-L23)）可以浓缩成一张决策表：

| 本次变更性质 | 递增哪一段 | 低位段如何处理 | 语义 |
| --- | --- | --- | --- |
| 不完全向后兼容（breaking change） | major | minor、bugfix 归零 | 大版本，可能破坏旧调用方 |
| 新增功能（new feature） | minor | bugfix 归零 | 向后兼容的能力扩充 |
| 仅修 bug、无功能变化 | bugfix | 不动其它位 | 向后兼容的修复 |

一个细节：递增高位段时，低位段归零是语义化版本的通用约定。比如 `2.1.0` 新增功能后应变为 `2.2.0`（而不是 `2.1.1`），bugfix 段归零。

> 与 u2-l2、u3-l3 的呼应：`VersionNr` 类负责**解析与比较**版本号（如 `1.10.0 > 1.2.0`），而本节讲的是**如何决定下一个版本号是什么**——前者是运行时比较，后者是发布时的人为决策，二者共用同一套三段格式。

#### 4.3.2 核心流程

发布一个新版本的标准决策流程：

```text
1. 写代码、改完功能
2. 在 Changelog.md 新增一条记录（标注 Features / Changes / Bugfix）
3. 按这条记录的「性质」查决策表 → 决定递增 major / minor / bugfix
4. 算出新版本号（注意低位归零规则）
5. 把新版本号写进 setup.py 的 version 字段
6. python3 setup.py sdist 重新打包
```

把决策表套到本项目真实的历史版本上，能验证规则的正确性（见 4.3.3）。

#### 4.3.3 源码精读

先看规则定义。 [README.md:18-23](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L18-L23)：

```text
## Tagging Policy
Stable releases are tagged in the form *major*.*minor*.*bugfix*. 

* Whenever a change is not fully backward compatible, the *major* version number is incremented
* Whenever new features are added, the *minor* version number is incremented
* If only bugs are fixed (i.e. no functional changes are applied), the *bugfix* version is incremented
```

三条 bullet 正好对应决策表的三行。

再看事实记录。 [Changelog.md:1-10](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md#L1-L10)：

```text
## 2.1.0
* Features
  * Added semantic version check for dependencies that are already checked out

## 2.0.0
* Changes for open-sourcing
  * Renamed Library to make the name specific to the FPGA libraries

## 1.0.0
* First Release
```

把规则套到这两次版本跃迁上，**完美自洽**：

- `1.0.0` → `2.0.0`： [Changelog.md:5-7](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md#L5-L7) 记录的是「Renamed Library」（为开源把库名改了）。**改名是不向后兼容的破坏性变更**（旧 import 路径会失效），命中决策表第一行 → 递增 major：`1.x.x` → `2.0.0`。✅
- `2.0.0` → `2.1.0`： [Changelog.md:1-3](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md#L1-L3) 明确标 `Features`（新增了对已检出依赖的语义版本检查）。**新增功能且向后兼容**，命中决策表第二行 → 递增 minor：`2.0.x` → `2.1.0`，bugfix 归零。✅

这就是「事实 + 规则 → 版本号」的完整推理链。

#### 4.3.4 代码实践

**实践目标**：用规则预测两个假设场景下的新版本号，训练「读 Changelog → 定版本号」的肌肉记忆。

**操作步骤**（纯推理型，不改任何源码）：

1. 假设场景 A：你在 `Checkout` 里修了一个会导致 `latest_release` 模式偶发选错 tag 的 bug，没有改任何对外接口。
2. 假设场景 B：你给 `CheckCompatibility` 新增了一个「major 越界直接 fail（不只是 WARNING）」的可选开关参数，且不破坏既有调用。

**需要观察的现象 / 推理**：
- 场景 A 是「仅修 bug」，命中决策表第三行 → 递增 bugfix：当前 `2.1.0` → 新版本 `2.1.1`。
- 场景 B 是「新增功能」，命中决策表第二行 → 递增 minor：当前 `2.1.0` → 新版本 `2.2.0`（bugfix 归零）。

**预期结果**：A → `2.1.1`，B → `2.2.0`。

> **待本地验证**：此为规则套用题，无运行输出。建议你额外自造一个「破坏性变更」场景（如把 `Dependency` 的字段名改了），按规则应得 `3.0.0`，自行核对。

#### 4.3.5 小练习与答案

**练习 1**：当前版本 `2.1.0`。如果下一次发布**只**修了一个 bug，新版本号是什么？如果连修三个 bug 一起发呢？

**参考答案**：只修 bug → 递增 bugfix → `2.1.1`。连修三个 bug一起发，仍然是只递增 bugfix 一次 → `2.1.1`（版本号不按 bug 数量累加，一次发布只动一位）。

**练习 2**：为什么 `2.0.0` 那次「改名」要跳 major 而不是 minor？

**参考答案**：改名会让所有 `import` 旧包名的客户端代码失效，属于「不向后兼容」的破坏性变更，按 [README.md:21](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L21) 的规则必须递增 major，以警告调用方「需要迁移」。递增 minor 会误导用户以为可以无痛升级。

**练习 3**：`VersionNr` 类（u2-l2）和本节的 Tagging Policy 都用 `major.minor.bugfix`，它们各自负责什么？

**参考答案**：`VersionNr` 负责把版本号字符串**解析成可比较的对象**（如判断 `1.10.0 > 1.2.0`），是运行时工具，被 `CheckCompatibility` 用来比较实际版本与最低要求。Tagging Policy 负责**决定下一个发布版本号该是多少**，是发布时的人为规则，由人根据 Changelog 判断。两者共享三段格式，但一个是「比较」、一个是「生成」。

---

## 5. 综合实践

把三个模块串起来，模拟一次完整的发布流程。**请在仓库副本中进行，不要污染正式 `dist/` 与被追踪文件。**

**任务**：假设你要发布一个新版本，它既新增了一个 `-dry_run` 命令行开关（只打印将执行的 git 命令、不真正执行），又顺手修了 README 里的一个拼写错误。

**操作步骤**：

1. **写 Changelog**（在副本里）：在 [Changelog.md](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md) 顶部新增一段，标题留空待填，内容分 `Features`（新增 `-dry_run`）和 `Bugfix`（修文档拼写）两组。
2. **定版本号**：本次同时含「新增功能」与「修 bug」。查决策表——有新功能就递增 minor（修 bug 不额外动版本号，见练习 1）。当前 `2.1.0` → 新版本应为 **`2.2.0`**（bugfix 归零）。
3. **改 setup.py**：把 [setup.py:25](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L25) 的 `version="2.1.0"` 改为 `version="2.2.0"`。
4. **补全 Changelog 标题**：把第 1 步的标题写成 `## 2.2.0`，使其与 `setup.py` 对齐。
5. **打包**：在副本里执行 `python3 setup.py sdist`。

**需要观察的现象**：
- `CustomSdist` 先清空副本里的 `dist/` 与 `egg-info`（若存在）。
- 打包完成后 `dist/` 里出现新归档 `PsiFpgaLibDependencies-2.2.0.tar.gz`，旧版本归档不再存在（被清理掉）。
- 归档名中的 `2.2.0` 与 [setup.py:25](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L25) 的 `version`、Changelog 标题三者一致。

**预期结果**：`dist/PsiFpgaLibDependencies-2.2.0.tar.gz` 生成成功，且 `dist/` 中只有这一个归档。

> **待本地验证**：sdist 的实际输出、归档内文件清单请在本机副本中执行后用 `tar tzf` 核对。若本机未装 `setuptools`，需先 `pip3 install setuptools`。

## 6. 本讲小结

- `CustomSdist` 继承 setuptools 的 `sdist` 命令，在 `run()` 里先 `rmtree` 掉 `dist/` 与 `egg-info`（`ignore_errors=True` 保证幂等），再调用 `sdist.run(self)` 完成标准打包——典型的模板方法钩子。
- `cmdclass = {"sdist": CustomSdist}` 是让这个钩子生效的「开关」；没有它，`CustomSdist` 写了也不会被调用。
- 归档名由 `setup()` 的 `name`+`version` 拼出（`PsiFpgaLibDependencies-2.1.0.tar.gz`），`version` 字段是全项目唯一的版本号来源，需手动维护。
- `dist/` 被 git 追踪（归档随库分发），`*.egg-info` 被 `.gitignore` 忽略（临时元数据）；`CustomSdist` 清空 `dist/` 正是为了让被提交的 `dist/` 永远只含当前版本的一个归档。
- Tagging Policy 规定：破坏性变更递增 major、新增功能递增 minor、仅修 bug 递增 bugfix；递增高位段时低位归零。
- 历史版本完美自洽：`1.0.0`→`2.0.0` 是改名的 major 跳变，`2.0.0`→`2.1.0` 是加 Features 的 minor 递增。

## 7. 下一步学习建议

本讲是 u3（专家层）的最后一篇，也是整套学习手册的收尾。到这里你已经能独立完成「改代码 → 定版本 → 打包」的全流程。建议：

1. **回顾发布全链路**：把本讲的打包流程与 u1-l2（安装与目录结构）、u1-l3（库式集成）连起来看——你打的归档正是下游客户端（如 `psi_common`）用 `pip3 install` 安装的同一个文件。
2. **动手改进**：可以尝试把分散在 `setup.py:25`（version）、`Changelog.md` 标题、git tag 三处的版本号做一次「单一来源」改造练习（例如让 `setup.py` 从一个 `version.py` 读取），体会本项目「手动维护、多处对齐」的取舍。
3. **回看版本语义闭环**：结合 u2-l2（`VersionNr` 解析与比较）、u3-l3（`CheckCompatibility` 版本校验），完整理解「发布时定版本号 → README 声明 minVersion → 检出后用 `VersionNr` 比较」这条贯穿全包的版本语义主线。
