# 仓库目录结构

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 Squirrel 仓库顶层每个目录（`sources/`、`resources/`、`data/`、`package/`、`scripts/` 等）各自承担什么职责。
- 区分「源码目录」「资源/数据目录」「构建产物目录」三类，并知道哪些内容会进版本库、哪些是构建时才生成的。
- 识别 `librime`、`plum`、`Sparkle` 这三个 git 子模块在构建链路中扮演的角色。
- 对照 [Makefile](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile) 看懂依赖产物的路径约定，为下一讲「构建与运行」打好基础。

承接上一讲：你已经知道 Squirrel 是 Rime 的 macOS 前端，按键→候选词的转换交给引擎 `librime`。本讲我们要回答一个具体问题——**这些代码、配置、依赖在仓库里到底是怎么摆放的？** 只有先把地图看清楚，后面读源码、改配置、做打包才不会迷路。

## 2. 前置知识

在开始之前，先用通俗语言澄清几个概念：

- **源码（source code）**：程序员写的、需要被编译器翻译成程序的文本文件。Squirrel 的源码是 Swift（`.swift`）。
- **资源（resources）**：程序运行时需要读取、但本身不是代码的文件，比如图标、配置描述（`Info.plist`）、权限声明（`.entitlements`）、本地化字符串（`.xcstrings`）。
- **数据（data）**：输入法运行需要的「素材」，比如默认配置 `squirrel.yaml`、输入方案、词库、简繁转换词典。这些通常体积较大或会频繁更新。
- **构建产物（build artifacts）**：编译后生成的二进制文件（动态库、可执行程序），一般不进版本库，而是用 `.gitignore` 忽略，由构建脚本现场生成。
- **git 子模块（submodule）**：把另一个独立的 git 仓库「嵌」进当前仓库的某个子目录里。克隆时默认不会拉取子模块内容，需要 `git submodule update --init`。
- **包管理器（package manager）**：这里是 macOS 安装包 `.pkg` 的打包脚本与流程，不是指 Homebrew 那种工具。

> 提示：本讲大量出现路径。如果你手边有仓库，建议边读边对照 `ls` 看真实文件，印象会更深。

## 3. 本讲源码地图

本讲涉及的关键文件与目录如下：

| 路径 | 类型 | 作用 |
| --- | --- | --- |
| [README.md](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/README.md) | 文档 | 项目说明、授权、跨平台发行版关系、引用的开源组件 |
| [Makefile](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile) | 构建脚本 | 用 `make` 编译依赖与 Squirrel.app，是目录路径约定的「真源」 |
| [sources/](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources) | 源码目录 | 全部 Swift 源码 + C 桥接头 |
| [resources/](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources) | 资源目录 | `Info.plist`、权限、本地化、图标素材 |
| [data/](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data) | 数据目录 | 默认配置 `squirrel.yaml`（`data/plum`、`data/opencc` 为构建产物） |
| [package/](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package) | 打包脚本 | 生成 `.pkg` 安装包、签名、本地化欢迎页 |
| [scripts/](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/scripts) | 安装脚本 | `postinstall`：装包后注册并启用输入法 |
| [.gitmodules](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.gitmodules) | 子模块清单 | 声明 `librime`、`plum`、`Sparkle` 三个子模块 |

## 4. 核心概念与源码讲解

### 4.1 顶层一览：先看「骨架」

打开仓库根目录，你能看到大致这样的结构（仅列关键项，省略部分细节）：

```
rime-squirrel/
├── sources/          # Swift 源码（程序本体）
├── resources/        # Info.plist、权限、本地化、图标
├── data/             # 默认配置 squirrel.yaml（+ 构建产物子目录）
├── package/          # 打包成 .pkg 的脚本与资源
├── scripts/          # postinstall 等安装期脚本
├── bin/              # 构建产物：命令行工具（gitignore）
├── lib/              # 构建产物：动态库（gitignore）
├── Frameworks/       # 构建产物：Sparkle.framework（gitignore）
├── librime/          # 【子模块】Rime 引擎
├── plum/             # 【子模块】方案/词库配置管理器
├── Sparkle/          # 【子模块】自动更新框架
├── Assets.xcassets/  # 资源目录（图标等）
├── Rime.icon/        # 输入法图标资源
├── Squirrel.xcodeproj/  # Xcode 工程文件
├── .github/          # CI 工作流、Issue 模板
├── README.md / INSTALL.md / SKILL.md / LICENSE.txt / CHANGELOG.md
├── Makefile          # 构建总入口
├── action-build.sh / action-install.sh / action-changelog.sh
├── cliff.toml / .swiftlint.yml / .periphery.yml
└── .gitmodules / .gitignore
```

一个核心心智模型：**这个仓库把「我们自己写的代码」和「别人写好的依赖」清晰分开了。** 自己写的在 `sources/`、`resources/`、`data/`、`package/`、`scripts/`；别人写好的依赖则以子模块形式放在 `librime/`、`plum/`、`Sparkle/`，或在构建时被拷进 `bin/`、`lib/`、`Frameworks/`。

下面按四个最小模块逐一展开。

### 4.2 模块一：源码目录 sources/

#### 4.2.1 概念说明

`sources/` 是 Squirrel 的「大脑」——全部 Swift 源码都在这里。Squirrel 是一个规模适中的项目：12 个 Swift 文件加 1 个 C 桥接头，合计约 3555 行。这意味着你不必被吓到，**整个前端的核心逻辑是可以在合理时间内读完的**。

文件命名已经透露了各自的职责：

| 文件 | 行数 | 职责（一句话） |
| --- | --- | --- |
| `Main.swift` | 176 | 程序入口：命令行命令 + 正常启动 |
| `SquirrelApplicationDelegate.swift` | 445 | 应用委托：持有全局状态、初始化 librime、注册通知 |
| `SquirrelInputController.swift` | 614 | 输入控制器：处理键盘事件、消费 librime 状态 |
| `SquirrelPanel.swift` | 566 | 候选词面板模型与定位 |
| `SquirrelView.swift` | 756 | 面板自绘视图（Core Graphics） |
| `SquirrelConfig.swift` | 148 | 类型化配置读取门面 |
| `SquirrelTheme.swift` | 347 | 主题（配色/字体/布局）加载 |
| `MacOSKeyCodes.swift` | 232 | macOS 按键 → Rime 按键映射 |
| `InputSource.swift` | 123 | 系统输入源（TIS）注册 |
| `ReservedProperty.swift` | 58 | 插件→前端保留属性协议 |
| `BridgingFunctions.swift` | 83 | Swift/C 桥接工具 |
| `Squirrel-Bridging-Header.h` | 7 | 引入 librime C 头 |

#### 4.2.2 核心流程

读源码的推荐顺序（也是后续讲义的展开顺序）：

1. 先看入口 `Main.swift`，弄清程序怎么起来的。
2. 再看 `SquirrelApplicationDelegate.swift`，理解全局状态与 librime 初始化。
3. 接着读 `SquirrelInputController.swift`，跟着一次按键走完整条主链路。
4. 最后横向看配置（`SquirrelConfig`/`SquirrelTheme`）和界面（`SquirrelPanel`/`SquirrelView`）。

#### 4.2.3 源码精读

入口文件用 `@main` 标注，表明这是整个程序的起点：

```swift
@main
struct SquirrelApp {
```

参见 [sources/Main.swift:11-12](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L11-L12)，这两行声明了 Squirrel 的程序入口结构体。Swift 的 `@main` 属性告诉编译器：把 `SquirrelApp.main()` 作为可执行文件的入口。

注意入口里还定义了几个关键目录常量，它们揭示了 Squirrel 运行时读写文件的位置：

```swift
static let userDir = ... "Library", "Rime"            // 用户配置目录
static let logDir = FileManager.default.temporaryDirectory.appending(component: "rime.squirrel", ...)
```

参见 [sources/Main.swift:13-21](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L13-L21)。这里 `userDir` 指向 `~/Library/Rime`（用户级配置与方案），`logDir` 指向系统临时目录下的 `rime.squirrel`（运行日志）。这些路径与本讲的「仓库目录」不同——它们是**运行时**目录，但同样重要，后面讲 librime 初始化时会再用到。

> 小结：`sources/` 是源码的唯一聚集地，目录扁平、文件按职责命名、入口明确，非常适合循序阅读。

### 4.3 模块二：资源与数据目录（resources/、data/）

#### 4.3.1 概念说明

macOS 应用有两类「非代码」内容必须随程序一起分发：

- **resources/**：应用级别的资源，描述「这个 App 是什么、能干什么、有哪些权限」。Squirrel 这里放的是：
  - `Info.plist`：应用元数据，也是 macOS 注册输入法的依据（声明连接名、输入模式 ID 等）。
  - `Squirrel.entitlements`：沙盒与权限声明（沙盒应用能调用哪些系统 API）。
  - `Localizable.xcstrings` / `InfoPlist.xcstrings`：本地化字符串（中英文界面文案）。
  - `rime.pdf`：联机帮助文档（系统输入法菜单里「在线文档」对应素材之一）。

- **data/**：输入法运行所需的「素材」。Squirrel 仓库里**实际签入版本库的只有 `squirrel.yaml`**——它是前端的全局默认配置（键盘布局、状态栏图标、候选面板样式、配色方案、各 App 的输入行为等）。

> 注意区分：`data/plum/` 和 `data/opencc/` 这两个子目录虽然在运行时存在，但**它们是构建产物**，被 `.gitignore` 忽略，不会出现在版本库里。下一节会解释它们从哪来。

#### 4.3.2 核心流程

`data/` 目录在构建前后的变化：

```
构建前（git 签入）：        构建后（make data 之后）：
data/                      data/
└── squirrel.yaml          ├── squirrel.yaml      （原有，前端默认配置）
                           ├── plum/              （构建产物：方案、词库、essay.txt）
                           └── opencc/            （构建产物：简繁转换词典）
```

`data/plum/` 里的内容来自子模块 `plum/`（方案与词库），`data/opencc/` 里的内容来自 `librime` 自带的 OpenCC 词典加上 `plum` 输出。这两者最终都会被打包进 `Squirrel.app` 的 `SharedSupport`，供 librime 引擎运行时读取。

#### 4.3.3 源码精读

`data/squirrel.yaml` 的存在决定了前端的默认外观与行为，它是后续「配置与主题」单元（u3）的核心研究对象。本讲只需记住它的位置：

- [data/squirrel.yaml](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml) ——前端默认配置（顶层 `style`、`preset_color_schemes`、`app_options` 等都在这里）。

`resources/Info.plist` 则是 macOS 识别 Squirrel 为「输入法」的入口凭证，下一单元的 IMK 基础（u1-l5）会精读它。

> 小结：`resources/` 管「App 是什么」，`data/` 管「输入法要用的素材」；签入版本库的只有 `squirrel.yaml`，其余数据目录在构建时生成。

### 4.4 模块三：构建与打包目录（package/、scripts/、bin/、lib/、Frameworks/）

#### 4.4.1 概念说明

这一组目录与「把源码变成可安装的 `.pkg`」直接相关：

- **package/**：打包脚本与安装包资源。包含 `make_package`（调用 `pkgbuild`/`productbuild`）、`sign_app`（签名）、`add_data_files`（把数据文件加入打包清单）、`Distribution`/`Squirrel-component.plist`（productbuild 配置）、`bump_version`（版本号管理）、`make_archive`（归档发布）、`conclusion.html` 与各语言 `*.lproj`（安装结束页的本地化页面）。
- **scripts/**：目前只有 `postinstall`——`.pkg` 安装完成后由系统自动执行的脚本，负责注册输入源、部署、启用并选中 Squirrel。
- **bin/、lib/、Frameworks/**：**纯构建产物**，被 `.gitignore` 忽略。三者分别存放：
  - `bin/`：从 `librime/dist/bin` 拷贝来的命令行工具 `rime_deployer`、`rime_dict_manager`，以及从 `plum` 拷来的 `rime-install`。
  - `lib/`：引擎动态库 `librime.1.dylib` 及 `rime-plugins`。
  - `Frameworks/`：自动更新框架 `Sparkle.framework`。

#### 4.4.2 核心流程

`bin/`、`lib/`、`Frameworks/` 的内容来源，可以用 [Makefile](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile) 顶部的一组变量精确描述。Makefile 第 6-26 行集中定义了所有依赖产物路径：

```makefile
RIME_BIN_DIR = librime/dist/bin
RIME_LIB_DIR = librime/dist/lib
...
RIME_LIBRARY = lib/$(RIME_LIBRARY_FILE_NAME)
...
PLUM_DATA = bin/rime-install data/plum/default.yaml data/plum/symbols.yaml data/plum/essay.txt
OPENCC_DATA = data/opencc/TSCharacters.ocd2 data/opencc/TSPhrases.ocd2 data/opencc/t2s.json
SPARKLE_FRAMEWORK = Frameworks/Sparkle.framework
PACKAGE = package/Squirrel.pkg
DEPS_CHECK = $(RIME_LIBRARY) $(PLUM_DATA) $(OPENCC_DATA) $(SPARKLE_FRAMEWORK)
```

参见 [Makefile:6-26](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L6-L26)。这里的 `DEPS_CHECK` 是一个「依赖就绪检查清单」——它把构建 Squirrel 之前必须存在的四类产物列在一起：

| 变量 | 产物 | 来源 |
| --- | --- | --- |
| `RIME_LIBRARY` | `lib/librime.1.dylib` | 编译子模块 `librime` 后拷入 `lib/` |
| `PLUM_DATA` | `bin/rime-install` + `data/plum/*.yaml|txt` | 构建子模块 `plum` 后拷入 |
| `OPENCC_DATA` | `data/opencc/*.ocd2|json` | librime 的 opencc 依赖 + plum 输出 |
| `SPARKLE_FRAMEWORK` | `Frameworks/Sparkle.framework` | 编译子模块 `Sparkle` 后拷入 |

`release` 和 `debug` 目标都依赖 `$(DEPS_CHECK)`（见 [Makefile:102](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L102) 与 [Makefile:107](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L107)），这就是为什么「先 `make deps`、再 `make release`」会成为标准节奏——缺任何一项，make 都会先去补齐。

#### 4.4.3 源码精读

安装期脚本 [scripts/postinstall](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/scripts/postinstall) 是连接「打包目录」与「真实安装」的桥梁——`.pkg` 装完后它会调用 Squirrel 的命令行参数（如 `--register-input-source`、`--build`）来完成收尾。这些命令行参数的入口正是 [sources/Main.swift:30-118](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L30-L118) 里的 `switch args[1]` 分支，例如：

```swift
case "--register-input-source", "--install":
  installer.register()
  return true
```

参见 [sources/Main.swift:40-42](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L40-L42)。这条线索把 `scripts/`、`package/` 与 `sources/` 三处串了起来：打包脚本生成的 `.pkg` → 安装时 `postinstall` 调用 → `Squirrel.app` 自身的命令行分支。这一条链路会在 u5（系统集成）单元详细展开。

> 小结：`package/` 与 `scripts/` 是「发行与安装」的脚本聚集地；`bin/`、`lib/`、`Frameworks/` 是构建产物暂存区，由 [Makefile](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile) 顶部的路径变量统一管理。

#### 4.4.4 代码实践

1. **实践目标**：亲手确认 `bin/`、`lib/`、`data/plum/`、`data/opencc/` 是否真的被版本库忽略。
2. **操作步骤**：
   - 打开 [.gitignore](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.gitignore)。
   - 查找 `bin/*`、`lib/*`、`data/opencc/`、`data/plum/`、`Frameworks/*` 这几行。
3. **需要观察的现象**：这几条忽略规则确实存在；同时 `data/squirrel.yaml` **没有**被忽略（它要进版本库）。
4. **预期结果**：你会理解为什么在一个全新 `git clone`（未执行 `--recursive`、未 `make`）的仓库里，`bin/`、`lib/` 是空的、`data/` 下只有 `squirrel.yaml`。这是正常现象，不是克隆出错。
5. 待本地验证：如果你在本机有构建环境，可以执行 `make deps` 后再 `ls bin lib data/plum data/opencc`，对比前后差异。

### 4.5 模块四：git 子模块（librime、plum、Sparkle）

#### 4.5.1 概念说明

Squirrel 不重复造轮子，而是把三个独立的开源项目以 **git 子模块** 的方式挂进仓库。子模块的好处是：版本固定、边界清晰、各自独立升级。三个子模块分别是：

| 子模块 | 路径 | 上游仓库 | 作用 |
| --- | --- | --- | --- |
| `librime` | `librime/` | `rime/librime` | **Rime 引擎本体**（C++），做按键→候选词的转换 |
| `plum` | `plum/` | `rime/plum` | **東風破**：输入方案与词库的配置管理器 |
| `Sparkle` | `Sparkle/` | `sparkle-project/Sparkle` | macOS 应用的**自动更新框架** |

回顾上一讲：Squirrel 是「前端」，引擎是 `librime`——这里的子模块清单正是那一认知在仓库层面的落地。

#### 4.5.2 核心流程

子模块的声明集中在根目录的 `.gitmodules` 文件里：

```
[submodule "librime"]
	path = librime
	url = https://github.com/rime/librime.git
	ignore = dirty
[submodule "plum"]
	path = plum
	url = https://github.com/rime/plum.git
[submodule "Sparkle"]
	path = Sparkle
	url = https://github.com/sparkle-project/Sparkle
```

参见 [.gitmodules](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/.gitmodules)。每一段声明一个子模块的「挂载路径」与「上游 URL」。

子模块的取用流程：

1. 普通克隆 `git clone https://github.com/rime/squirrel.git` 时，子模块目录**是空的**（只有一个指针）。
2. 需要 `git submodule update --init --recursive` 才会真正拉取子模块内容。
3. 官方安装文档 [INSTALL.md](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/INSTALL.md) 推荐直接 `git clone --recursive`，一步到位。

构建时，`Sparkle` 子模块的拉取甚至被写进了 [Makefile:114-116](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L114-L116)：

```makefile
$(SPARKLE_FRAMEWORK):
	git submodule update --init --recursive Sparkle
	$(MAKE) sparkle
```

也就是说，如果检测到 `Frameworks/Sparkle.framework` 不存在，make 会先自动初始化 `Sparkle` 子模块再编译——这是「按需拉取子模块」的典型写法。

#### 4.5.3 源码精读

子模块的存在也解释了 [README.md](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/README.md) 致谢清单里的几条「本品引用了以下开源軟件」：

```
* librime  (New BSD License)
* plum / 東風破 (GNU Lesser General Public License 3.0)
* Sparkle  (MIT License)
```

参见 [README.md:90-93](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/README.md#L90-L93)。注意三者的**授权协议不同**：`librime` 是 BSD、`plum` 是 LGPL、`Sparkle` 是 MIT，而 Squirrel 本体是 GPL v3（见 [README.md:19](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/README.md#L19)）。这一点在做二次开发或再分发时必须留意。

> 小结：三个子模块 = 引擎 + 方案库 + 更新框架；它们是构建 `bin/`、`lib/`、`data/plum`、`data/opencc`、`Frameworks/` 这些产物的「上游原料」。

#### 4.5.4 代码实践

1. **实践目标**：验证子模块在「未初始化」与「已初始化」两种状态下的差异。
2. **操作步骤**：
   - 在仓库根目录运行只读命令 `git submodule status`（本环境允许只读 git 命令）。
   - 观察输出，每行形如 `<commit> <path>`，前缀符号反映状态（空格=已检出、`-`=未初始化、`+`=版本不同）。
3. **需要观察的现象**：能看到 `librime`、`plum`、`Sparkle` 三个条目及其对应的上游 commit。
4. **预期结果**：你会直观看到「子模块只是一个指向某 commit 的指针」，从而理解为什么必须 `--init --recursive`。
5. 待本地验证：若你有完整构建环境，对比 `git submodule update --init` 前后 `ls librime plum Sparkle` 的输出差异。

#### 4.5.5 小练习与答案

**练习 1**：为什么一个全新克隆的 Squirrel 仓库里，`lib/` 和 `bin/` 是空的、`data/` 下只有 `squirrel.yaml`？请用本讲学到的两类原因回答。

> **参考答案**：两个独立原因叠加。其一，`lib/`、`bin/` 是构建产物，被 `.gitignore` 忽略，需要 `make` 后才会由 [Makefile](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile) 从子模块产物拷入；其二，`data/plum/` 与 `data/opencc/` 同样是构建产物（来自 `plum`、`librime`），也被忽略，只有前端默认配置 `data/squirrel.yaml` 被签入版本库。

**练习 2**：下列哪些目录是 git 子模块？哪些是构建产物？哪些是签入版本库的源码/资源？请分类：`sources/`、`librime/`、`lib/`、`resources/`、`Sparkle/`、`Frameworks/`、`plum/`、`data/squirrel.yaml`。

> **参考答案**：
> - 子模块：`librime/`、`plum/`、`Sparkle/`。
> - 构建产物（gitignore）：`lib/`、`Frameworks/`、以及构建后的 `bin/`、`data/plum/`、`data/opencc/`。
> - 签入版本库的源码/资源：`sources/`、`resources/`、`data/squirrel.yaml`。

**练习 3**：如果你只想看懂 Squirrel 前端自己的代码（不碰引擎内部），应该重点读哪个目录？它大约多少行？

> **参考答案**：重点读 `sources/`，它是 12 个 Swift 文件加 1 个桥接头，合计约 3555 行，规模可控；引擎 `librime` 是独立子模块，读前端时不必深入。

## 5. 综合实践

**任务：为 Squirrel 仓库绘制一张「职责 + 类型 + 来源」三维目录树。**

请你依据本讲内容，结合仓库实际文件（建议在本地用 `ls` 对照），产出一张 Markdown 目录树，要求每个关键目录或文件后面用注释标注三件事：

1. **职责**：它干什么用（一句话）。
2. **类型**：源码 / 资源 / 数据 / 构建脚本 / 构建产物 / 子模块 / 文档。
3. **来源**（如适用）：签入版本库 / 来自哪个子模块 / 由哪条 Makefile 目标生成。

参考骨架（你需要补全注释）：

```
rime-squirrel/
├── sources/            # 职责：?  类型：?  来源：?
├── resources/          # 职责：?  类型：?  来源：?
├── data/
│   ├── squirrel.yaml   # 职责：?  类型：?  来源：?
│   ├── plum/           # 职责：?  类型：?  来源：?
│   └── opencc/         # 职责：?  类型：?  来源：?
├── package/            # 职责：?  类型：?  来源：?
├── scripts/            # 职责：?  类型：?  来源：?
├── bin/                # 职责：?  类型：?  来源：?
├── lib/                # 职责：?  类型：?  来源：?
├── Frameworks/         # 职责：?  类型：?  来源：?
├── librime/            # 职责：?  类型：?  来源：?
├── plum/               # 职责：?  类型：?  来源：?
├── Sparkle/            # 职责：?  类型：?  来源：?
└── Makefile            # 职责：?  类型：?  来源：?
```

完成后再回答一个拔高问题：**当你执行 `make release` 时，Makefile 顶部 `DEPS_CHECK`（[Makefile:26](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L26)）会检查哪四个产物？它们各自对应哪个最终目录？**

> 参考思路：四项是 `RIME_LIBRARY`→`lib/`、`PLUM_DATA`→`bin/` 与 `data/plum/`、`OPENCC_DATA`→`data/opencc/`、`SPARKLE_FRAMEWORK`→`Frameworks/`。把它们和上面的目录树对应起来，你就把「仓库目录」与「构建依赖」彻底打通了。

## 6. 本讲小结

- Squirrel 仓库结构清晰：自己写的代码在 `sources/`、应用资源在 `resources/`、输入法数据在 `data/`、打包脚本在 `package/`、安装脚本在 `scripts/`。
- `sources/` 是扁平的 12 个 Swift 文件加 1 个桥接头（约 3555 行），入口是 [Main.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift)。
- `data/` 签入版本库的只有 `squirrel.yaml`；`data/plum/`、`data/opencc/` 以及 `bin/`、`lib/`、`Frameworks/` 都是构建产物，被 `.gitignore` 忽略。
- [Makefile](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile) 顶部的 `DEPS_CHECK` 用一组路径变量集中描述了所有依赖产物及其目标目录，是路径约定的「真源」。
- 三个 git 子模块 `librime`（引擎）、`plum`（方案库）、`Sparkle`（自动更新）是构建产物的上游原料，需 `git submodule update --init --recursive` 才会拉取。
- 子模块与 Squirrel 本体授权协议不同（BSD / LGPL / MIT vs GPL v3），再分发时需留意。

## 7. 下一步学习建议

下一讲 **u1-l3 构建与运行** 会把本讲的目录地图「跑起来」：精读 [Makefile](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile) 与 [INSTALL.md](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/INSTALL.md)，弄清 `make release` 背后的依赖链（`librime` → `data` → `sparkle` → `xcodebuild`）以及 `ARCHS`、`BUILD_UNIVERSAL`、`DEV_ID` 等关键环境变量。

如果你迫不及待想看代码，也可以跳到 **u1-l4 程序入口与启动流程**，精读 [sources/Main.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift)，看看 `@main` 之后的代码是怎么把 Squirrel 启动起来的。

建议继续阅读的源码：先通读 [Makefile](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile) 全文（仅 197 行），把本讲提到的所有路径变量在脑子里串成一条「依赖 → 产物 → 目录」的链路。
