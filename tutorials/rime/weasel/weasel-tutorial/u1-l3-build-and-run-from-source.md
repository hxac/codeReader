# 构建系统与从源码运行调试

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `build.bat all` 这一条命令背后到底依次做了哪几件事。
- 理解版本号（`VERSION_MAJOR` / `PRODUCT_VERSION` 等）是如何从环境变量一路注入到最终的 `weasel.dll` 资源文件里的。
- 区分仓库里两套并存的构建体系——官方的 Visual Studio 解决方案（`weasel.sln` + `.vcxproj` + MSBuild）与替代的 `xmake.lua`——它们各自的角色与共同依赖。
- 把编译产物安装到 `output\` 目录，并掌握在 Visual Studio 里调试 `WeaselServer.exe` 与驻留在应用进程内的 `weasel.dll` 的正确方法。

本讲是「能跑起来」的关键一讲：没有把工程构建出来并装进系统，后续所有源码阅读都只能是纸上谈兵。

## 2. 前置知识

在进入源码前，先用通俗语言铺垫几个概念。

- **批处理脚本（`.bat`）**：Windows 上的命令行脚本，本仓库的构建总入口 `build.bat` 就是一个批处理。它通过 `set` 设置变量、用 `if` 分支、用 `call :标签` 调用脚本内部的子过程（subroutine）。
- **环境变量（environment variable）**：构建所需的「配置参数」，例如 `BOOST_ROOT` 指向 Boost 库源码目录。`env.bat` 就是用来集中设置这些变量的文件。
- **MSBuild / `.sln` / `.vcxproj`**：Visual Studio 的官方构建系统。`.sln`（solution）是一个「解决方案」，里面登记了多个 `.vcxproj`（project，即一个子工程）；MSBuild 负责按依赖关系把它们逐个编译。
- **xmake**：一个用 Lua 描述的国产跨平台构建工具。仓库里的 `xmake.lua` 是一套与 MSBuild 并行存在的、更轻量的构建描述。
- **Boost**：Weasel 依赖的 C++ 第三方库（用于序列化、正则、线程等）。它需要先被 `b2` 编译成静态库，Weasel 才能链接。
- **librime**：Rime 引擎本体，作为 git 子模块存在于 `librime/` 目录，产出 `rime.dll` / `rime.lib`。Weasel 通过 C 接口调用它。
- **版本号注入**：把「当前版本」这个动态信息，从脚本变量传递到编译器的预处理器宏，最终写进 `.dll` 的「文件属性 → 详细信息」里。本仓库用一个小巧的模板渲染脚本 `render.js` 完成这件事。

如果你对上一讲（u1-l2）里「八大子工程、`include/` 公共头、WTL」的结构还有印象，本讲就是回答「这些子工程到底怎么被串起来编译」。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `INSTALL.md` | 官方构建说明，列出前置工具与从源码构建的步骤。 |
| `build.bat` | **构建总入口**，编排 boost / librime / 数据 / weasel / 安装包的全部流程。 |
| `env.bat.template` | 环境变量模板，复制为 `env.bat` 后填写本机路径。 |
| `render.js` | 极简模板渲染器（基于 WSH 的 JScript），把 `$变量` 替换成环境变量值。 |
| `weasel.props.template` | MSBuild 属性表模板，被 `render.js` 渲染成 `weasel.props`。 |
| `weasel.props`（生成物） | 真正被各 `.vcxproj` `Import` 的属性表，承载版本号与 Boost 路径。 |
| `xmake.lua` | 替代构建体系的入口脚本。 |
| `output/install.bat` | 把 `output\` 里的产物注册为系统输入法并启动服务。 |
| `arm64x_wrapper/build.bat` | 生成 ARM64X 跨架构 `weasel.dll` 的链接脚本。 |
| `include/WeaselConstants.h` | 用宏把 `VERSION_MAJOR` 等拼成 C 代码里的版本字符串。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. 构建脚本与版本号注入（`build.bat` + `render.js` + `weasel.props`）。
2. VC++ 解决方案与 xmake 双轨（`weasel.sln` + `.vcxproj` 与 `xmake.lua`）。
3. 安装到 `output` 与调试方法（`output/install.bat` + Debug 配置）。

### 4.1 构建脚本与版本号注入

#### 4.1.1 概念说明

`build.bat` 是整个仓库的「总指挥」。它不是简单地调用一次编译器，而是按顺序完成一条很长的流水线：

1. 加载本机环境变量（`env.bat`）。
2. 计算当前版本号（必要时用 git 提交信息补全）。
3. 校验 Boost 是否就位（没有 Boost 直接报错退出）。
4. 解析命令行参数（`all` / `boost` / `rime` / `weasel` / `installer` 等），决定要构建哪些部分。
5. 依次构建：Boost → librime（x64 与 Win32）→ 数据文件 → Weasel 各子工程 → 安装包。

版本号注入是这条流水线里很容易被忽略、但又贯穿全局的一环：脚本算出版本号后，用一个模板渲染器把它写进 `weasel.props`，再由 `.vcxproj` 引用，最终作为预处理器宏进入资源编译器（`.rc`）和 C 源码。

#### 4.1.2 核心流程

`build.bat` 的版本计算逻辑可以用下面这段伪代码概括：

```
读取 env.bat
WEASEL_ROOT = 当前目录
VERSION_MAJOR/MINOR/PATCH 取默认值（除非 env.bat 覆盖）
WEASEL_VERSION = MAJOR.MINOR.PATCH
PRODUCT_VERSION = WEASEL_VERSION.BUILD

if 非正式发布构建 and git 可用:
    找到与 WEASEL_VERSION 匹配的最新 tag
    BUILD = HEAD 超过该 tag 的提交数
    PRODUCT_VERSION = WEASEL_VERSION.BUILD.<short-commit-hash>

校验 %BOOST_ROOT%\boost 存在，否则退出
解析命令行 → 设置各 build_* 开关
调用 render.js 生成 weasel.props
msbuild weasel.sln ...
```

这套设计的精妙之处在于：**版本号的「构建号（BUILD）」不是写死的，而是用「自上次版本 tag 以来的提交数」动态算出来的**。这样每次构建都能得到一个随提交推进而递增、且带有 commit hash 的唯一版本串，方便定位「某个安装包对应哪一次提交」。

#### 4.1.3 源码精读

脚本开头会确保 `env.bat` 存在并加载它：

[build.bat:5-7](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/build.bat#L5-L7) —— 若 `env.bat` 不存在则从模板复制一份，随后 `call env.bat` 把本机的 `BOOST_ROOT` 等变量加载进当前进程。

紧接着设定项目根与版本号默认值：

[build.bat:9-16](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/build.bat#L9-L16) —— `WEASEL_ROOT` 默认取当前目录；`VERSION_MAJOR/MINOR/PATCH` 给出 `0.17.4` 的默认值，并允许 `env.bat` 提前覆盖。

随后是版本号的「git 增强」逻辑：

[build.bat:21-36](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/build.bat#L21-L36) —— 仅当未定义 `RELEASE_BUILD` 且 git 可用时，找到匹配当前 `WEASEL_VERSION` 的最新 tag，用 `git rev-list <tag>..HEAD --count` 算出构建号，再用 `git rev-parse --short HEAD` 取短 commit hash，拼成 `PRODUCT_VERSION=版本.构建号.hash`。

Boost 是硬性前置依赖，脚本会强制检查：

[build.bat:53-60](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/build.bat#L53-L60) —— 只有当 `%BOOST_ROOT%\boost` 目录真实存在时才放行，否则打印错误并 `exit /b 1` 中止。这是新手最常踩的坑之一。

命令行参数解析采用「逐个 shift + 标签跳转」的经典批处理写法：

[build.bat:87-119](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/build.bat#L87-L119) —— 把每个参数映射到一个 `build_*` 开关。其中 `all` 一次性把 boost / data / opencc / rime / weasel / installer / arm64 全部打开。

如果调用者一个构建目标都没指定，脚本给出一个友好默认：

[build.bat:121-127](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/build.bat#L121-L127) —— 裸跑 `build.bat`（不带参数）时，默认只构建 weasel 本体。

正式调用 MSBuild 之前，脚本会把版本号「渲染」进属性表：

[build.bat:187-195](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/build.bat#L187-L195) —— 定义 `WEASEL_PROJECT_PROPERTIES` 为一组变量名，然后 `cscript.exe render.js weasel.props <变量名...>` 生成最终的 `weasel.props`。

渲染器本体 `render.js` 非常短，核心是两个部分。先看它的字符串模板扩展：

[render.js:3-12](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/render.js#L3-L12) —— 给 `String` 原型挂了一个 `template` 方法，用正则 `/\$\w+/g` 匹配所有 `$单词` 形式的占位符并替换。这是一个约 10 行的「迷你模板引擎」。

再看渲染主流程：

[render.js:46-54](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/render.js#L46-L54) —— 第一个命令行参数是目标文件名，其余参数是环境变量名；逐个从进程环境读取值，构造 `{变量名: 值}` 映射后调用 `Render`，从 `<file>.template` 读入、替换、写回 `<file>`。

被渲染的模板长这样：

[weasel.props.template:3-11](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/weasel.props.template#L3-L11) —— `<UserMacros>` 里用 `$BOOST_ROOT`、`$VERSION_MAJOR` 等占位符，渲染后就被替换成实际值。

属性表还把这些宏送进资源编译器，让 `.rc` 文件能拿到版本号：

[weasel.props.template:22-29](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/weasel.props.template#L22-L29) —— `ResourceCompile` 的 `PreprocessorDefinitions` 把 `VERSION_MAJOR/MINOR/PATCH/PRODUCT_VERSION/FILE_VERSION` 作为宏传给 `.rc`，最终写进 DLL/EXE 的版本信息资源；同时全局开启 `/utf-8`。

最后，C 源码侧也有对应的宏拼接：

[include/WeaselConstants.h:7-9](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselConstants.h#L7-L9) —— 用 `STRINGIZE` + `VERSION_STR` 两层宏技巧，把 `VERSION_MAJOR.VERSION_MINOR.VERSION_PATCH`（由 `weasel.props` / 编译器命令行注入）展开成字符串形式的 `WEASEL_VERSION`。

至此，版本号的完整旅程是：

```
env.bat / 默认值
  → build.bat 算出 PRODUCT_VERSION
    → render.js 渲染 weasel.props.template → weasel.props
      → .vcxproj Import weasel.props
        → 资源编译器(.rc) 与 C 编译器拿到 VERSION_* 宏
          → 写进 DLL/EXE 文件属性 与 WeaselConstants.h 的 WEASEL_VERSION
```

#### 4.1.4 代码实践

**实践目标**：不动手编译，仅通过阅读源码，复现「版本号注入」这条链路上每一步的输入与输出。

**操作步骤**：

1. 打开 `env.bat.template`，确认它没有设置任何 `VERSION_*` 变量（因此会走 `build.bat` 的默认值 `0.17.4`）。
2. 在 `build.bat` 第 21–36 行追踪：假设当前 git HEAD 超过最近 tag `0.17.4` 有 5 个提交，且短 hash 是 `f9203ca`，写出最终 `PRODUCT_VERSION` 的值。
3. 对照 `weasel.props.template` 第 3–11 行，写出渲染后 `weasel.props` 中 `<PRODUCT_VERSION>` 标签的实际内容。
4. 对照 `weasel.props.template` 第 26–28 行，写出 `.rc` 资源编译器收到的 `PRODUCT_VERSION` 宏定义。
5. 对照 `include/WeaselConstants.h` 第 9 行，说明 `WEASEL_VERSION` 宏最终展开成的字符串。

**需要观察的现象**：版本号在五个环节中保持一致地传递；其中 `PRODUCT_VERSION`（带构建号与 hash）只进资源文件，而 C 代码里的 `WEASEL_VERSION` 只含三段主版本号。

**预期结果**：步骤 2 得到 `0.17.4.5.f9203ca`；步骤 3 得到 `<PRODUCT_VERSION>0.17.4.5.f9203ca</PRODUCT_VERSION>`；步骤 4 得到 `PRODUCT_VERSION=0.17.4.5.f9203ca`；步骤 5 得到 `"0.17.4"`。

> 说明：以上为依据源码逻辑推导的结果，未在本机实跑命令，实际数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果希望构建出一个「干净的发布版本」（不带 commit hash），应该怎么做？

**答案**：在调用 `build.bat` 前定义环境变量 `RELEASE_BUILD=1`。这样 `build.bat` 第 21 行的 `if not defined RELEASE_BUILD` 分支不会进入，`PRODUCT_VERSION` 保持为 `WEASEL_VERSION.WEASEL_BUILD` 的简洁形式，不去查询 git。

**练习 2**：为什么 `render.js` 渲染的是 `weasel.props.template` 而不是直接修改各 `.vcxproj`？

**答案**：因为版本号与 Boost 路径是「每次构建都可能变化」的动态信息，而 `.vcxproj` 是纳入版本管理的静态工程文件。用一个独立的、被 `.gitignore` 忽略的生成物 `weasel.props` 承载这些动态值，可以避免污染版本库、也避免不同开发者本机路径互相冲突。

**练习 3**：`build.bat` 第 53–54 行为什么检查的是 `%BOOST_ROOT%\boost` 这个子目录，而不是 `BOOST_ROOT` 本身？

**答案**：`BOOST_ROOT` 指向的是 Boost 的**源码根目录**，解压后其下必有一个 `boost/` 子目录（即头文件的顶级命名空间目录）。检查它的存在可以同时验证「路径存在」和「这确实是一个 Boost 源码树」，比单纯检查路径更可靠。

---

### 4.2 VC++ 解决方案与 xmake 双轨

#### 4.2.1 概念说明

仓库里并存两套构建描述，服务于不同人群：

- **官方主线：`weasel.sln` + 各 `*.vcxproj` + MSBuild**。这是 `build.bat` 实际驱动的体系，也是发布安装包时使用的体系，功能最全（含 ARM/ARM64/ARM64X、安装包、数据文件等）。
- **替代体系：`xmake.lua`**。用 Lua 描述、由 `xmake` 工具执行，更轻量、跨平台友好，适合只关心 x86/x64 主程序、想快速迭代的开发者。

两套体系都依赖同一个环境变量 `BOOST_ROOT`，也都会消费 `VERSION_*` 系列变量，产出的目标布局也一致（`output\` 放 DLL/EXE，`lib` / `lib64` 放静态库）。

#### 4.2.2 核心流程

`build.bat` 在准备好版本号后，按平台依次调用 MSBuild 编译解决方案：

```
（若 build_arm64）
  msbuild weasel.sln /p:Platform=ARM
  msbuild weasel.sln /p:Platform=ARM64
msbuild weasel.sln /p:Platform=x64
msbuild weasel.sln /p:Platform=Win32
（若 build_arm64）
  arm64x_wrapper\build.bat  → 产出 weaselARM64X.dll，拷到 output
（若 build_installer）
  makensis output\install.nsi  → 产出 output\archives 下的安装包
```

而每个 `.vcxproj` 都通过 `<Import Project="..\weasel.props" />` 共享同一份版本号与 Boost 路径配置。`xmake.lua` 则用一段 Lua 把这些等价信息（编译选项、架构库目录、版本号传给 `.rc`）描述出来。

#### 4.2.3 源码精读

先看 MSBuild 侧：`build.bat` 调用 MSBuild 编译多平台：

[build.bat:199-213](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/build.bat#L199-L213) —— `build_arm64` 时先编 `ARM` 与 `ARM64`，再始终编 `x64` 与 `Win32`。`/fl1`～`/fl6` 是把各平台日志分别写入 `msbuild*.log` 文件；`/p:Configuration` 与 `/p:Platform` 决定 Debug/Release 与目标架构。

ARM64 场景下还需要一个「跨架构合并」步骤：

[build.bat:215-223](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/build.bat#L215-L223) —— 进入 `arm64x_wrapper` 跑它自己的 `build.bat`，把生成的 `weaselARM64X.dll` 拷贝到 `output`。ARM64X 是一种同时包含 ARM64 与 x64 导出表的 DLL，让一个 `weasel.dll` 能在两种架构下被加载。

`arm64x_wrapper/build.bat` 用链接器手工拼装这个特殊 DLL：

[arm64x_wrapper/build.bat:14-18](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/arm64x_wrapper/build.bat#L14-L18) —— 先用 `link /lib` 由 `.def` 文件生成两份导入库，再用 `link /dll /machine:arm64x` 把 ARM64 原生与 x64（arm64ec）两份导出合并进同一个 `weaselARM64X.dll`。

安装包由 NSIS 打包：

[build.bat:225-232](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/build.bat#L225-L232) —— 调用 `makensis.exe`，用 `/D` 把 `WEASEL_VERSION` 等传入 `output/install.nsi` 脚本，生成最终安装程序（位于 `output\archives`）。

回到 `.vcxproj`：它们统一引用渲染好的 `weasel.props`：

[WeaselIPC/WeaselIPC.vcxproj:42](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselIPC.vcxproj#L42) —— 在引入 MSBuild 默认属性之前就 `Import` 了 `..\weasel.props`，这样后续整个工程都能使用其中的 `BOOST_ROOT`、`PRODUCT_VERSION` 等宏。

并且把 Boost 与公共头加入包含路径：

[WeaselIPC/WeaselIPC.vcxproj:151](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselIPC.vcxproj#L151) —— `AdditionalIncludeDirectories` 为 `$(SolutionDir)\include;$(BOOST_ROOT)`，前者对应上一讲提到的公共头目录，后者让编译器找到 Boost 头文件。

现在看替代体系 `xmake.lua`。它同样从环境读取 Boost：

[xmake.lua:14-20](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/xmake.lua#L14-L20) —— `os.getenv("BOOST_ROOT")` 取 Boost 根，`stage/lib` 作为库目录，并设置了一长串与 MSBuild 等价的编译/链接选项（`/utf-8`、`/MT` 静态运行时等）。

它按架构选择不同的静态库目录（对应 `lib` 与 `lib64`）：

[xmake.lua:35-45](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/xmake.lua#L35-L45) —— x64 链接 `lib64`，x86 链接 `lib`，这正好对应 `build.bat` 把 librime 的 `rime.lib` 分别拷到 `lib64` 与 `lib` 的做法。

它按架构决定编译哪些子工程：

[xmake.lua:49-57](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/xmake.lua#L49-L57) —— `WeaselIPC/WeaselUI/WeaselTSF` 在所有架构都编；`RimeWithWeasel/WeaselIPCServer/WeaselServer/WeaselDeployer` 仅 x64/x86；`WeaselSetup` 仅 x86。这反映了安装器只在 x86、服务端不在 ARM 上构建的现实约束。

它还用一个自定义 `rule` 把版本号传给 `.rc`：

[xmake.lua:76-85](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/xmake.lua#L76-L85) —— `add_rcfiles` 规则把 `VERSION_MAJOR/MINOR/PATCH/FILE_VERSION/PRODUCT_VERSION`（同样来自环境变量）作为宏注入 `.rc` 文件。这与 MSBuild 侧 `weasel.props` 的 `ResourceCompile` 段异曲同工。

#### 4.2.4 代码实践

**实践目标**：对照两套构建体系，确认它们「消费相同的输入、产出相同的目标布局」。

**操作步骤**：

1. 在 `build.bat` 第 199–213 行找出 MSBuild 实际编译的平台列表与配置项（`/p:Configuration`、`/p:Platform`）。
2. 在 `xmake.lua` 第 49–57 行列出 xmake 在 x64 架构下 `includes` 的子工程。
3. 对比 `weasel.props.template` 第 26–28 行（MSBuild 侧把版本号传给 `.rc`）与 `xmake.lua` 第 76–85 行（xmake 侧把版本号传给 `.rc`），说明两者传给资源编译器的宏是否一致。
4. 在 `xmake.lua` 第 35–45 行确认 x64 与 x86 分别链接 `lib64` 与 `lib`，再到 `build.bat` 第 162–165 行确认 librime 的 `rime.lib` 正是被分别拷到这两个目录。

**需要观察的现象**：两套体系在「输入（BOOST_ROOT、VERSION_*）」与「产物位置（output、lib/lib64）」上完全对齐，差异只在于描述语言与覆盖范围（MSBuild 覆盖 ARM/安装包，xmake 更精简）。

**预期结果**：得到一张两列对照表，左列 MSBuild、右列 xmake，行依次为「Boost 来源」「版本号注入方式」「x64 链接目录」「ARM 支持」「安装包支持」。

> 说明：本实践为源码阅读型，不涉及实际编译；如需在本机验证，可分别跑 `build.bat weasel` 与 `xmake f && xmake` 对比产物，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `xmake.lua` 在 ARM/ARM64 架构下不编译 `WeaselServer`？

**答案**：见 `xmake.lua` 第 51–53 行，`RimeWithWeasel/WeaselIPCServer/WeaselServer/WeaselDeployer` 仅在 `x64` 或 `x86` 下 `includes`。服务端进程（`WeaselServer.exe`）只需要在主流桌面架构运行；ARM 设备上 Weasel 主要以 `weasel.dll`（TSF 前端）形式被加载，服务端可由其他途径提供，故 xmake 不在 ARM 下构建它。

**练习 2**：`build.bat` 编译时 `/fl1`、`/fl2` 这些参数的作用是什么？

**答案**：它们是 MSBuild 的 `/fl<n>`（file logger）选项，把不同平台/配置的构建日志分别写到 `msbuild1.log`、`msbuild2.log` 等文件。脚本在调用前还有 `del msbuild*.log`（第 197 行）清空旧日志。这样多平台并行编译时各自的日志不会互相覆盖，便于排错。

**练习 3**：如果只用 xmake 构建，`weasel.props` 还会被用到吗？

**答案**：不会。`weasel.props` 是 MSBuild 体系专属的属性表，只有 `.vcxproj` 会 `Import` 它。xmake 体系完全不经过 `render.js` 与 `weasel.props`，而是直接在 `xmake.lua` 里用 `os.getenv` 读取 `BOOST_ROOT`、`VERSION_*`。所以两套体系各自独立地获取这些动态参数。

---

### 4.3 安装到 output 与调试方法

#### 4.3.1 概念说明

编译完成不等于「能用」。Weasel 是一个 TSF 输入法，它的 `weasel.dll` 必须被注册到 Windows 的文本服务框架里、`WeaselServer.exe` 必须作为后台服务运行，系统才会真正调用它。`output/install.bat` 就是完成「注册 + 启动」这最后一步的脚本。

调试方面，需要先理解 Weasel 的多进程结构（见 u1-l1）带来的一个特殊难点：**`weasel.dll` 不是独立进程，而是被 Windows 加载进每一个启用输入法的应用进程里**。因此调试前端代码要「附加到应用进程」，调试服务端/引擎/UI 代码则要「附加到 `WeaselServer.exe`」。

#### 4.3.2 核心流程

`output/install.bat` 的执行流程：

```
根据参数选择注册为简体(/s)或繁体(/t)键盘布局
stop_service.bat        停止旧版服务
WeaselDeployer.exe /install   写入预设输入方案
检测是否管理员权限；不是则用 sudo.js 提权重跑
check_windows_version.js   判断系统版本
  → win7/win7_x64/xp 分支
WeaselSetup[.exe|x64.exe]  向 TSF 注册文本服务
reg add ...Run /v WeaselServer   设置开机自启
start WeaselServer.exe    启动服务
```

调试的推荐路径：

```
build.bat debug weasel      （产出带 PDB 的 Debug 版到 output\）
cd output && install.bat    （注册并启动 Debug 版服务）
用 Visual Studio 附加到 WeaselServer.exe    （调试引擎/IPC/UI）
用 Visual Studio 附加到 目标应用(如 notepad.exe) （调试驻留的 weasel.dll）
```

#### 4.3.3 源码精读

`output/install.bat` 开头根据参数决定键盘布局：

[output/install.bat:4-5](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/install.bat#L4-L5) —— 默认 `/s`（简体中文键盘布局），传 `/t` 则注册为繁体布局。这个值最终会传给 `WeaselSetup.exe`。

它会先停掉旧服务、写入预设方案：

[output/install.bat:12-16](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/install.bat#L12-L16) —— 调 `stop_service.bat` 停止可能正在运行的旧版 `WeaselServer.exe`，再跑 `WeaselDeployer.exe /install` 把预设输入方案配置好。这一步对应 u6 单元会详讲的 Deployer。

注册 IME 需要管理员权限，脚本会自动提权：

[output/install.bat:18-24](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/install.bat#L18-L24) —— 用 `net session` 探测权限，失败则借助 `sudo.js`（一个用 `ShellExecute` 触发 UAC 的 JScript）以管理员身份重新执行自身并附加 `/register` 参数。

随后按 Windows 版本走不同注册分支：

[output/install.bat:30-48](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/install.bat#L30-L48) —— `check_windows_version.js` 返回不同 errorlevel，据此跳到 `win7_install`（32 位 `WeaselSetup.exe`）、`win7_x64_install`（`WeaselSetupx64.exe`）或 `xp_install`。真正调用 TSF 注册 API 的工作发生在 `WeaselSetup` 内部（详见 u6-l2）。

最后设置开机自启并启动服务：

[output/install.bat:50-53](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/install.bat#L50-L53) —— 往 `HKLM\...\Run` 写入 `WeaselServer` 键值实现开机自启，然后 `start WeaselServer.exe` 立即拉起服务进程。

回到构建侧：`build.bat` 在每次重新构建前会主动退出正在运行的服务，避免 EXE/DLL 被占用导致链接失败：

[build.bat:130-133](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/build.bat#L130-L133) —— 若 `output\weaselserver.exe` 存在，则执行 `weaselserver.exe /q`（quit）让它优雅退出。这就是为什么改代码重编时经常需要先关掉服务——脚本已经替你做了。

Debug 配置由命令行参数触发：

[build.bat:89-93](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/build.bat#L89-L93) —— 传 `debug` 参数会把 `build_config` 设为 `Debug`，同时让 Boost 与 librime 也走 debug 变体，产出带调试符号（PDB）的产物。

#### 4.3.4 代码实践

**实践目标**：基于源码，规划一次「从源码到可调试运行」的完整流程，并标注每一步对应脚本里的位置。

**操作步骤**：

1. 在 `INSTALL.md` 第 70–75 行确认官方建议的安装命令是 `cd output` 后运行 `install.bat`。
2. 在 `build.bat` 第 89–93 行确认如何产出 Debug 版（`build.bat debug weasel`）。
3. 在 `build.bat` 第 130–133 行理解为什么重编前服务会被自动关闭。
4. 在 `output/install.bat` 第 30–53 行追踪注册到启动的完整链路，标注：权限提升在哪一步、TSF 注册由哪个 EXE 完成、服务最终如何被启动。
5. 规划调试策略：要调试候选窗口绘制（u5 内容），应附加到哪个进程？要调试按键抓取（u3 内容），又应附加到哪个进程？

**需要观察的现象**：理解「服务端代码在 `WeaselServer.exe` 进程」「前端代码在被输入的应用进程」这一关键区别，并能据此选择正确的附加目标。

**预期结果**：得到一份带步骤编号的「调试清单」，每一步都引用了具体源码行号；明确写出：候选窗口/UI/引擎/IPC 服务端代码 → 附加到 `WeaselServer.exe`；按键抓取/上屏（`weasel.dll`）代码 → 附加到目标应用（如 `notepad.exe`）。

> 说明：实际附加调试器的操作依赖 Windows 与 Visual Studio 环境，本实践侧重源码侧的流程梳理，运行行为**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么调试 `weasel.dll`（TSF 前端）时，要附加到 `notepad.exe` 而不是 `WeaselServer.exe`？

**答案**：因为 `weasel.dll` 是一个被 Windows TSF 框架加载进**每个应用进程**的 DLL（见 u1-l1、u3-l1）。当你把输入法切到小狼毫并在记事本里打字时，`weasel.dll` 的代码实际运行在 `notepad.exe` 的进程空间内。`WeaselServer.exe` 只托管引擎、IPC 服务端和 UI，并不包含前端按键抓取代码。所以要调试前端，必须附加到正在接收按键的那个应用进程。

**练习 2**：`output/install.bat` 为什么要先 `stop_service.bat` 再注册？

**答案**：如果旧版 `WeaselServer.exe` 还在运行，它会持有 `output\` 下某些 DLL/EXE 文件的句柄，导致注册或文件覆盖失败；同时旧服务可能还在用旧的注册信息。先停服务能保证注册过程干净、文件可写。这与 `build.bat` 第 131–133 行重编前先 `weaselserver.exe /q` 是同一类防护。

**练习 3**：非管理员用户直接双击 `install.bat` 会发生什么？

**答案**：见 `output/install.bat` 第 18–24 行，脚本用 `net session` 探测到当前不是管理员后，会调用 `sudo.js` 触发 UAC 提权，并以管理员身份重新执行自身、附加 `/register` 参数完成注册。所以非管理员也可以运行，只是会看到一个 UAC 确认框。

---

## 5. 综合实践

**综合任务**：对照 `INSTALL.md` 与 `build.bat`，整理出从「干净 checkout」到「生成 `output\archives` 安装包」所需的**全部步骤**与**环境变量**，并标注**容易踩坑的点**。最终产出一张「步骤卡」。

建议步骤卡的格式：

| 步骤 | 命令/操作 | 涉及的环境变量 | 对应源码位置 | 易踩坑点 |
| --- | --- | --- | --- | --- |
| 1 | 安装 VS2017（含 ATL/MFC/XP 支持） | — | `INSTALL.md` 第 5–7 行 | 缺 ATL 组件会导致 WTL/ATL 头找不到 |
| 2 | 安装 git / cmake / boost | `BOOST_ROOT` | `INSTALL.md` 第 9–12 行 | Boost 需是源码包，不是预编译包 |
| 3 | `git clone --recursive ...` | — | `INSTALL.md` 第 23–25 行 | 忘记 `--recursive` 会缺少 librime/plum 子模块 |
| ... | ... | ... | ... | ... |

请在卡中至少覆盖以下要点，并逐一标注源码依据：

- `env.bat` 必须存在且 `BOOST_ROOT` 指向真实路径（`build.bat` 第 5–7、53–60 行）。
- `build.bat all` 会构建 boost、librime、数据、weasel、installer、arm64 全部目标（`build.bat` 第 108–116 行）。
- 安装包最终落在 `output\archives`（`INSTALL.md` 第 57 行、`build.bat` 第 225–232 行）。
- 若已有预编译的 librime，可改用 `build.bat boost data opencc` + `build.bat weasel`（`INSTALL.md` 第 59–68 行）。
- 重编前服务会被自动退出（`build.bat` 第 130–133 行），避免文件占用。
- ARM64 需要额外的 ARM64EC 工具链与 `arm64x_wrapper` 合并步骤（`arm64x_wrapper/build.bat` 第 33–43 行）。

完成步骤卡后，再回答一个延伸问题：如果只想快速验证自己对某个 `.cpp` 的小改动，**最短的构建命令**是什么？（提示：参考 `build.bat` 第 121–127 行的默认行为，以及 `build.bat debug weasel`。）

## 6. 本讲小结

- `build.bat` 是构建总入口，它先加载 `env.bat`、计算版本号、校验 Boost，再按命令行参数（`all`/`boost`/`rime`/`weasel`/`installer` 等）驱动一条 boost→librime→数据→weasel→安装包的流水线。
- 版本号通过 `render.js` 把 `weasel.props.template` 渲染成 `weasel.props`，再由各 `.vcxproj` `Import`，最终作为宏进入资源编译器与 C 源码；非发布构建还会用 git 提交数与短 hash 补全 `PRODUCT_VERSION`。
- Boost 是硬依赖，`build.bat` 会检查 `%BOOST_ROOT%\boost` 是否存在，缺失即报错退出——这是新手最常见的第一道坎。
- 仓库并存两套构建体系：官方的 `weasel.sln` + MSBuild（`build.bat` 实际驱动，覆盖 ARM/安装包）与替代的 `xmake.lua`（更精简，仅 x86/x64）；两者都依赖 `BOOST_ROOT` 与 `VERSION_*`，产物布局一致。
- `output/install.bat` 负责把产物注册为系统输入法并启动 `WeaselServer.exe`，含权限提升与按 Windows 版本分支的逻辑；`build.bat` 重编前会自动退出旧服务以防文件占用。
- 调试时要区分进程：服务端/引擎/UI/IPC 服务端代码附加到 `WeaselServer.exe`；前端按键抓取与上屏代码（`weasel.dll`）附加到正在接收输入的应用进程。

## 7. 下一步学习建议

本讲让你把项目「跑起来」。接下来建议：

- **进入 IPC 骨架（u2 单元）**：从 `include/WeaselIPC.h` 的命令枚举与 `RequestHandler` 抽象基类开始，理解 `WeaselServer.exe` 与驻留应用内的 `weasel.dll` 之间到底在传什么。这是后续所有讲义的中枢。
- **若侧重前端**：直接跳到 u3-l1（TSF 注册与生命周期），结合本讲的「附加到应用进程」调试技巧，亲手调试一次按键抓取。
- **若侧重引擎桥接**：先读 u4-l1（RimeWithWeaselHandler 与引擎初始化），并尝试在 `build.bat debug weasel` 后附加 `WeaselServer.exe`，在 `Initialize` 处下断点观察 librime 的加载。
- **延伸阅读源码**：`render.js` 是一个不到 60 行的迷你模板引擎，值得通读；`arm64x_wrapper/build.bat` 则是理解 Windows ARM64X 跨架构 DLL 的绝佳实例。
