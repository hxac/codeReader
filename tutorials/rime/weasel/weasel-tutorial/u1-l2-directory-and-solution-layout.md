# 源码目录与解决方案结构

## 1. 本讲目标

在上一篇里，我们已经建立了 Weasel（小狼毫）「多进程 + 命名管道 IPC」的全局地图。本讲把镜头拉近，回答一个初学者最先遇到的问题：

> 这个仓库里这么多文件夹，到底哪个是干什么的？我该从哪里开始读？

学完本讲，你应当能够：

1. 说出 Weasel 解决方案 `weasel.sln` 里包含的全部子工程，以及每个子工程的产出类型（EXE / DLL / 静态库）。
2. 画出子工程之间的依赖关系（谁链接谁），并能用 `.vcxproj` 里的 `ProjectReference` 加以验证。
3. 理解 `include/` 公共头目录的作用、它与 Boost / WTL / librime 等外部头文件的关系。
4. 识别 `test/`、`resource/`、`output/`、`update/`、`arm64x_wrapper/` 等辅助目录的职责。

本讲是「看懂地图」的一讲，不涉及具体算法实现；它的产物是一张你可以随时查阅的「目录地图」，后续每一讲都会在这张地图上定位。

## 2. 前置知识

在开始前，请确认你大致了解以下概念（不必精通）：

- **解决方案（Solution）与工程（Project）**：在 Visual Studio / MSBuild 体系里，一个 `.sln` 文件把若干 `.vcxproj` 工程组织在一起；每个工程编译出一种产物。
- **ConfigurationType（配置类型）**：`.vcxproj` 里决定产出类型的关键字段，常见取值：
  - `Application` → 生成 `.exe` 可执行程序。
  - `DynamicLibrary` → 生成 `.dll` 动态链接库。
  - `StaticLibrary` → 生成 `.lib` 静态库（编译期被链进别的工程，自身不独立运行）。
- **ProjectReference（工程引用）**：声明「我依赖另一个工程」，MSBuild 会保证被引用工程先编译，并把它的产物链接进来。
- **命名空间（namespace）与头文件（header）**：C++ 里用 `#include` 共享声明；公共头放在一个公共目录，多个工程都去 include 它。
- **WTL（Windows Template Library）**：建立在 ATL 之上的轻量 C++ Windows GUI 框架，全部以头文件形式提供（`<wtl/atlapp.h>` 等）。Weasel 的窗口、对话框、托盘图标都用它实现。

不需要你写过 Windows 程序，但知道「一个输入法其实是由若干个 EXE/DLL 协同组成的」会有帮助——上一篇已经讲过这个分工。

## 3. 本讲源码地图

本讲涉及的「源码」更多是项目结构文件，而非具体算法代码：

| 文件 / 目录 | 作用 |
|---|---|
| `weasel.sln` | Visual Studio 解决方案文件，列出全部子工程与配置矩阵 |
| `WeaselTSF/ReadMe.txt` | TSF 子工程的自动生成说明，确认它是 DLL |
| `include/WeaselUI.h` | UI 子系统的公共接口（`weasel::UI` 类、`DirectWriteResources`） |
| `include/WeaselIPCData.h` | IPC 数据结构定义（`Context` / `Status` / `UIStyle` 等） |
| 各子工程的 `.vcxproj` | 每个工程的 `ConfigurationType` 与 `ProjectReference` |
| `test/`、`resource/`、`output/`、`update/`、`arm64x_wrapper/` | 辅助目录 |
| `.gitmodules` | 声明 `librime`、`plum` 两个子模块 |

## 4. 核心概念与源码讲解

### 4.1 根目录与子工程清单

#### 4.1.1 概念说明

Weasel 不是「一个程序」，而是一组协同工作的程序与库。上一篇已经讲过分工：

- **WeaselTSF**：被 Windows 加载进每个应用进程的瘦客户端（DLL），负责抓键、上屏。
- **WeaselServer**：全局唯一的后台服务（EXE），托管 librime 引擎、候选窗口 UI、托盘图标。
- 其它工程：要么是「库」（被链进上面两者），要么是「工具」（安装、部署、设置）。

这些工程由一个 Visual Studio 解决方案 `weasel.sln` 统一管理。读懂这张清单，就等于读懂了 Weasel 的模块划分。

#### 4.1.2 核心流程：如何读懂 weasel.sln

打开 `weasel.sln`，关注两类条目：

1. **`Project(...) = "名字", "路径", "{GUID}"`**：每一条就是一个子工程。名字是工程名，路径指向它的 `.vcxproj`，GUID 是全局唯一标识。
2. **`GlobalSection(ProjectConfigurationPlatforms)`**：列出每个工程在哪些「平台 × 配置」组合下编译（如 `Debug|x64`、`Release|ARM64`）。

`weasel.sln` 里一共声明了 10 个**编译型**子工程（外加 1 个仅用于组织文件的虚拟工程）。它们是：

[weasel.sln:24-43](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/weasel.sln#L24-L43) ← 连续 10 个 `Project(...)` 声明，依次为 WeaselTSF、WeaselUI、WeaselIPC、WeaselServer、TestWeaselIPC、WeaselIPCServer、TestResponseParser、RimeWithWeasel、WeaselDeployer、WeaselSetup。

> 说明：第 6 行还有一个 GUID 为 `{2150E333...}` 的「Solution Items」虚拟工程，它不产出二进制，只用来在解决方案资源管理器里把一批公共头文件归拢显示，见 [weasel.sln:6-23](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/weasel.sln#L6-L23)。

每个工程真正「产出什么」，要看它 `.vcxproj` 里的 `ConfigurationType`。下表汇总了全部工程的类型（行号均指向对应 `.vcxproj` 第一个配置块）：

| 子工程 | ConfigurationType | 产出 | 证据 |
|---|---|---|---|
| WeaselTSF | `DynamicLibrary` | `weasel.dll`（Win32）/ `weasel$(Platform)` 如 `weaselx64` | [WeaselTSF.vcxproj:45](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.vcxproj#L45)、[TargetName L119/L123](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.vcxproj#L119-L123) |
| WeaselServer | `Application` | `WeaselServer.exe` | [WeaselServer.vcxproj:45](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.vcxproj#L45) |
| WeaselDeployer | `Application` | `WeaselDeployer.exe` | [WeaselDeployer.vcxproj:29](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/WeaselDeployer.vcxproj#L29) |
| WeaselSetup | `Application` | `WeaselSetup.exe` | [WeaselSetup.vcxproj:21](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/WeaselSetup.vcxproj#L21) |
| TestWeaselIPC | `Application`（测试） | `TestWeaselIPC.exe` | [test/TestWeaselIPC/TestWeaselIPC.vcxproj:45](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.vcxproj#L45) |
| TestResponseParser | `Application`（测试） | `TestResponseParser.exe` | [test/TestResponseParser/TestResponseParser.vcxproj:45](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.vcxproj#L45) |
| WeaselIPC | `StaticLibrary` | `WeaselIPC.lib` | [WeaselIPC.vcxproj:45](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselIPC.vcxproj#L45) |
| WeaselIPCServer | `StaticLibrary` | `WeaselIPCServer.lib` | [WeaselIPCServer.vcxproj:45](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselIPCServer.vcxproj#L45) |
| WeaselUI | `StaticLibrary` | `WeaselUI.lib` | [WeaselUI.vcxproj:45](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselUI.vcxproj#L45) |
| RimeWithWeasel | `StaticLibrary` | `RimeWithWeasel.lib` | [RimeWithWeasel.vcxproj:45](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.vcxproj#L45) |

> 关于命名：除 WeaselTSF 自定义了 `TargetName` 为 `weasel`（Win32）或 `weasel$(Platform)`（其它平台）外，其余工程的 `TargetName` 未被覆盖，MSBuild 默认取「工程名」，扩展名按类型取 `.exe`/`.dll`/`.lib`。WeaselTSF 的 DLL 改名是为了让 TSF 文本服务以 `weasel.dll` 注册进系统。

#### 4.1.3 源码精读：谁链接谁（ProjectReference）

光知道产出类型还不够，还要知道依赖关系。`.vcxproj` 里的 `<ProjectReference>` 声明「我直接依赖哪个工程」。把它们汇总，就能得到依赖图：

| 子工程（引用方） | 直接依赖（被引用的工程） | 证据 |
|---|---|---|
| WeaselTSF | WeaselIPC、WeaselUI | [WeaselTSF.vcxproj:505-511](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.vcxproj#L505-L511) |
| WeaselServer | RimeWithWeasel、WeaselIPCServer、WeaselIPC | [WeaselServer.vcxproj:510-521](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.vcxproj#L510-L521) |
| RimeWithWeasel | WeaselUI | [RimeWithWeasel.vcxproj:286-289](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.vcxproj#L286-L289) |
| WeaselDeployer | WeaselIPC | [WeaselDeployer.vcxproj:218-221](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/WeaselDeployer.vcxproj#L218-L221) |
| TestWeaselIPC | RimeWithWeasel、WeaselIPCServer、WeaselIPC、WeaselUI | [TestWeaselIPC.vcxproj:350-363](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.vcxproj#L350-L363) |
| TestResponseParser | WeaselIPC | [TestResponseParser.vcxproj:342-345](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.vcxproj#L342-L345) |
| WeaselUI / WeaselIPC / WeaselIPCServer / WeaselSetup | 无 | （各自 `.vcxproj` 内无 `ProjectReference` 指向其它子工程） |

把这张表画成箭头图（A → B 表示 A 依赖 B）：

```text
WeaselSetup ──────────────────────────────── (独立)

WeaselIPC ◄── WeaselDeployer
   ▲
   │
WeaselUI ◄── RimeWithWeasel ◄── WeaselServer ──► WeaselIPCServer
   ▲                                 ▲
   │                                 │
WeaselTSF ───────────────────────────┘  (WeaselTSF 还直接依赖 WeaselIPC)

测试：
TestResponseParser ──► WeaselIPC
TestWeaselIPC ──► {RimeWithWeasel, WeaselIPCServer, WeaselIPC, WeaselUI}
```

从图里可以读出几个关键结论：

- **三个「叶子库」**：`WeaselUI`、`WeaselIPC`、`WeaselIPCServer` 不依赖任何其它子工程，是整个工程的底层积木。
- **WeaselTSF（前端 DLL）依赖 IPC 客户端 + UI**：这印证了上一篇的描述——前端是「瘦客户端」，它需要 IPC 客户端去和 Server 通信，但**也**直接链了一份 UI 库（用于在需要时本地绘制，如内联预编辑）。
- **WeaselServer（服务端 EXE）是依赖最重的工程**：它链了 RimeWithWeasel（引擎桥接）、WeaselIPCServer（IPC 服务端实现）、WeaselIPC（IPC 公共部分）。注意 RimeWithWeasel 又传递性地把 WeaselUI 带了进来——服务端负责画候选窗口。
- **WeaselDeployer（设置工具）只需要 WeaselIPC**：它通过 IPC 与运行中的 Server 通信来触发重新部署等操作，自己不直接碰 librime。

最后看一眼 `WeaselTSF/ReadMe.txt`，它是 AppWizard 自动生成的说明，但明确点出了 WeaselTSF 是一个 **DLL**，主源文件是 `WeaselTSF.cpp`：[WeaselTSF/ReadMe.txt:1-9](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/ReadMe.txt#L1-L9) 与 [WeaselTSF/ReadMe.txt:17-18](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/ReadMe.txt#L17-L18)。

#### 4.1.4 代码实践：制作「子工程 → 产出类型 → 直接依赖」对照表

**实践目标**：亲手核对本讲给出的依赖表，建立对解决方案结构的肌肉记忆。

**操作步骤**：

1. 用任意文本编辑器打开仓库根目录的 `weasel.sln`，找到第 24–43 行的 10 个 `Project(...)` 声明，把工程名抄下来。
2. 对每个工程，打开它的 `.vcxproj`，搜索 `ConfigurationType`，记录取值；再搜索 `ProjectReference Include`，记录它引用了哪些其它 `.vcxproj`。
3. （快捷方式）在仓库根目录用只读命令一次性扫描全部 `.vcxproj`（示例命令，仅供参考，可在本地 PowerShell / bash 里运行）：
   - PowerShell：`Select-String -Path "**\*.vcxproj" -Pattern "<ConfigurationType>|<ProjectReference Include"`
   - 或 ripgrep：`rg -n "<ConfigurationType>|<ProjectReference Include" -g "*.vcxproj"`
4. 把结果整理成一张三列表：`子工程 | 产出类型 | 直接依赖`。

**需要观察的现象**：

- 三个叶子库（WeaselUI / WeaselIPC / WeaselIPCServer）的 `ProjectReference` 段为空或不存在。
- WeaselServer 的依赖项最多。
- 测试工程 TestWeaselIPC 依赖几乎全部库，说明它是端到端集成测试。

**预期结果**：与本讲 4.1.3 的依赖表完全一致。如果你发现某个工程的依赖项与本表不符，说明仓库版本与本讲所基于的 HEAD（`f9203ca`）不同，请以你本地仓库为准。

> 待本地验证：由于本环境无法运行 MSBuild，上表中的 `.exe`/`.dll`/`.lib` 文件名是依据 `ConfigurationType` 与默认 `TargetName` 规则推断的；实际产物名请在本地完成一次编译后到输出目录核对。

#### 4.1.5 小练习与答案

**练习 1**：WeaselServer.exe 间接依赖了 WeaselUI 吗？为什么？

**参考答案**：是的。WeaselServer 直接依赖 RimeWithWeasel（[WeaselServer.vcxproj:510](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServer.vcxproj#L510)），而 RimeWithWeasel 又依赖 WeaselUI（[RimeWithWeasel.vcxproj:286](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.vcxproj#L286)）。MSBuild 的 `ProjectReference` 是传递的，因此 WeaselServer.exe 最终把 WeaselUI.lib 也链了进来——这正是 Server 进程能够绘制候选窗口的基础。

**练习 2**：为什么 WeaselTSF 这个 DLL 既要链接 WeaselIPC 又要链接 WeaselUI，而不是像 Server 那样通过 RimeWithWeasel 间接拿到 UI？

**参考答案**：因为 TSF 前端**不包含** RimeWithWeasel（引擎桥接只在 Server 端），它不会传递性地引入 UI。前端需要 IPC 客户端与 Server 通信，同时又需要一份 UI 能力来处理「内联预编辑（inline_preedit）」等本地绘制场景，所以必须显式同时依赖 WeaselIPC 和 WeaselUI（见 [WeaselTSF.vcxproj:505-511](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/WeaselTSF.vcxproj#L505-L511)）。

**练习 3**：WeaselSetup 工程没有任何 `ProjectReference`，它能独立编译吗？它的工作（安装/注册输入法）依赖运行时是否已存在其它产物？

**参考答案**：从编译角度看它独立。但它在运行时需要把 WeaselTSF 产出的 `weasel.dll` 注册进系统，因此逻辑上仍依赖 WeaselTSF 的产物——只是这个依赖发生在「安装阶段」而非「编译链接阶段」。

---

### 4.2 公共头 include 与 WTL

#### 4.2.1 概念说明

Weasel 有 10 个子工程，它们之间需要共享大量类型与接口声明（例如 IPC 的命令枚举、数据结构、UI 接口）。如果每个工程各存一份头文件，会造成维护噩梦。解决办法是设立一个**公共头目录 `include/`**，所有工程都把它加进「附加包含目录（AdditionalIncludeDirectories）」，于是可以直接 `#include <WeaselIPC.h>`。

除了项目自有的公共头，Weasel 还依赖两个**外部**头文件库：

- **Boost**：用于序列化（`boost::serialization`）、字符串流缓冲等，路径由环境变量 `$(BOOST_ROOT)` 提供。
- **WTL（Windows Template Library）**：轻量 Windows GUI 框架，全部以头文件形式提供，窗口、对话框、托盘、控件都靠它。
- **librime 的 C API**：`rime_api.h` 等，路径为 `$(SolutionDir)\librime\include`，只有 RimeWithWeasel 和 WeaselDeployer 需要。

#### 4.2.2 核心流程：include/ 如何被共享

打开任一工程的 `.vcxproj`，都能看到类似下面这一行，把公共头、Boost、（必要时）librime 头一起加入包含路径：

[WeaselIPC.vcxproj:151](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselIPC.vcxproj#L151) ← `$(SolutionDir)\include;$(BOOST_ROOT);...`

需要 librime 的工程（如 RimeWithWeasel）则多一项：

[RimeWithWeasel.vcxproj:135](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.vcxproj#L135) ← `$(SolutionDir)\include;$(BOOST_ROOT);$(SolutionDir)\librime\include`

`include/` 目录下共有 18 个文件，其中最关键的一组被 `weasel.sln` 的「Solution Items」虚拟工程集中展示（[weasel.sln:6-23](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/weasel.sln#L6-L23)），可以视为「官方推荐先读清单」：

| 公共头 | 作用 | 后续讲义 |
|---|---|---|
| `WeaselIPC.h` | IPC 接口：Client/Server、命令枚举、RequestHandler | u2-l1 |
| `WeaselIPCData.h` | IPC 数据结构：Context/Status/UIStyle 等 | u2-l4 |
| `PipeChannel.h` | 命名管道通道模板 | u2-l2 |
| `ResponseParser.h` | 服务端响应解析器 | u2-l5 |
| `RimeWithWeasel.h` | 引擎桥接 Handler | u4-l1 |
| `WeaselUI.h` | UI 接口与 DirectWrite 资源 | u5 |
| `WeaselUtility.h` | 路径、编码等工具函数 | u7-l2 |
| `WeaselConstants.h` | 版本号、路径常量 | u7-l2 |
| `KeyEvent.h` | 按键事件与转换 | u3-l2 |
| `VersionHelpers.hpp` | Windows 版本判定辅助 | 工具 |

下面挑两个最有代表性的头文件展开。

#### 4.2.3 源码精读：两个代表性公共头

**(a) `include/WeaselUI.h` —— UI 子系统的接口入口**

整个文件包在 `namespace weasel` 里：[WeaselUI.h:13](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUI.h#L13)。核心是抽象接口类 `UI`，它定义了「输入法界面」应具备的能力——创建/销毁、显隐、按超时显隐、刷新、跟随光标、更新内容：

[WeaselUI.h:28-94](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUI.h#L28-L94) ← `class UI`，含 `Create`/`Destroy`/`Show`/`Hide`/`ShowWithTimeout`/`Refresh`/`UpdateInputPosition`/`Update(Context const&, Status const&)` 等虚函数。

同文件还声明了 `DirectWriteResources`，封装 Direct2D / DirectWrite 的工厂、文本格式、画刷、文本布局等绘图资源：[WeaselUI.h:96-167](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUI.h#L96-L167)。

注意 `UI` 类里持有的成员 `Context ctx_`、`Status status_`、`UIStyle style_`（[WeaselUI.h:86-90](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUI.h#L86-L90)）——这三种类型就定义在下一个头文件里。

**(b) `include/WeaselIPCData.h` —— 跨进程共享的数据结构**

这个头是「数据契约」的核心。它定义了在前端 TSF、Server、UI 之间流转的全部数据结构，并用 `boost::serialization` 给它们配上序列化模板，使其能被打包进命名管道传输。

- `Context`：一次输入的上下文，包含预编辑串 `preedit`、提示串 `aux`、候选信息 `cinfo`：[WeaselIPCData.h:121-146](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L121-L146)。
- `Status`：输入法当前状态（方案名、是否中文/全角/写作中/维护模式）：[WeaselIPCData.h:150-186](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L150-L186)。
- `UIStyle`：外观样式的全集，先定义一组枚举（布局类型 `LayoutType` 含竖直/水平/竖排文字/全屏等）：[WeaselIPCData.h:206-213](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L206-L213)，再定义几十个字段（字体、颜色、边距、圆角、阴影……）。
- 序列化模板：文件末尾在 `namespace boost::serialization` 里为每个结构体提供 `serialize` 模板，例如 `UIStyle` 的：[WeaselIPCData.h:429-503](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L429-L503)。这一段是 UIStyle 能通过 IPC 传输的关键。

> 这就是「公共头」的价值：`UI` 接口、`Context`/`Status`/`UIStyle` 这些类型被 WeaselUI、WeaselIPC、RimeWithWeasel、WeaselServer、WeaselTSF 多个工程共同 `#include`，保证大家对「数据长什么样」达成一致。后续 u2-l4、u5 会深入这些结构。

#### 4.2.4 源码精读：WTL 在哪里被引入

WTL 不是项目自带的代码，而是一个**外部头文件库**，通过各工程的预编译头 `stdafx.h` 引入。例如 WeaselUI 工程：

[WeaselUI/stdafx.h:12-18](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/stdafx.h#L12-L18) ← 先包含 ATL（`<atlbase.h>`、`<atlwin.h>`），再包含 WTL（`<wtl/atlapp.h>`、`<wtl/atlframe.h>`、`<wtl/atlgdi.h>`、`<wtl/atlmisc.h>`）。

其它带界面的工程同样如此，例如 WeaselSetup：[WeaselSetup/stdafx.h:13-22](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/stdafx.h#L13-L22)。可以看到 WTL 提供了 `atlapp`（应用框架）、`atlframe`（窗口框架）、`atlctrls`（控件）、`atldlgs`（对话框）、`atlmisc`（杂项工具）等一系列模块。

> 小贴士：WTL 必须出现在编译器的包含路径上。它通常由构建环境（与 Boost 类似的外部依赖）提供，具体如何配置属于构建系统话题，留到 u1-l3 讲。

#### 4.2.5 代码实践：追踪一个公共头的「消费者」

**实践目标**：体会公共头是如何被多个工程共享的。

**操作步骤**：

1. 在仓库根目录搜索谁包含了 `WeaselUI.h`（示例命令）：`rg -n '#include\s*<WeaselUI\.h>'`。
2. 再搜索谁包含了 `WeaselIPCData.h`：`rg -n '#include\s*<WeaselIPCData\.h>'`。
3. 记录命中文件分别属于哪些子工程。

**需要观察的现象**：

- `WeaselUI.h` 的消费者集中在 WeaselUI、WeaselServer（经 RimeWithWeasel）、WeaselTSF、WeaselIPC 等工程。
- `WeaselIPCData.h` 的消费者更广，因为数据结构几乎处处都用。

**预期结果**：多个不同子工程都能 `#include` 同一个头，这正是 `$(SolutionDir)\include` 统一包含路径带来的效果。如果你愿意，可以进一步打开 `WeaselUI.h` 顶部，注意它自己也 `#include <WeaselIPCData.h>`（[WeaselUI.h:3](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUI.h#L3)），这说明公共头之间也存在层次依赖。

> 待本地验证：搜索结果取决于本地仓库实际内容，请以本地输出为准。

#### 4.2.6 小练习与答案

**练习 1**：为什么 `UIStyle` 需要 `boost::serialization` 的 `serialize` 模板？

**参考答案**：因为 `UIStyle` 要从前端/Server 通过命名管道传到对方进程（例如 Server 把当前样式推给前端）。管道只能传字节流，`serialize` 模板（[WeaselIPCData.h:429-503](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L429-L503)）把结构体的每个字段依次写入/读出字节流，从而实现跨进程序列化。

**练习 2**：一个新手把 `Context` 的定义从 `include/WeaselIPCData.h` 搬到了 `WeaselIPC/Context.h`，但只改了 WeaselIPC 工程。会发生什么？

**参考答案**：WeaselUI、WeaselServer、WeaselTSF 等工程仍从 `include/WeaselIPCData.h` 引入旧的 `Context` 定义，于是会出现「同一个名字、两种定义」的混乱，可能导致编译错误或更隐蔽的字段错位。这正是要把公共类型集中放在 `include/` 的原因——单一事实来源（single source of truth）。

**练习 3**：WTL 和 ATL 是什么关系？为什么 `stdafx.h` 里先包含 ATL 再包含 WTL？

**参考答案**：WTL 构建在 ATL（Active Template Library）之上，是对 ATL 的高层封装。ATL 提供基础的 COM/窗口模板，WTL 在其上补足完整的 GUI 控件与对话框框架。因此必须先包含 ATL 的头（如 `<atlbase.h>`），WTL 的头（如 `<wtl/atlapp.h>`）才能找到它所依赖的 ATL 定义。

---

### 4.3 辅助目录：test / resource / output / update / arm64x_wrapper 及子模块

#### 4.3.1 概念说明

除了 10 个编译子工程，仓库里还有一组「辅助目录」。它们不直接是 Weasel 的运行逻辑，但分别承担**测试、资源、打包、自动更新、跨架构构建、外部依赖**等职责。看懂它们，才能完整理解「从源码到可安装产品」的全链路。

#### 4.3.2 核心流程：各目录职责一览

| 目录 | 职责 | 关键内容 |
|---|---|---|
| `test/` | 单元 / 集成测试 | `TestResponseParser`（响应解析单元测试）、`TestWeaselIPC`（端到端 IPC 测试），均基于 Google Test |
| `resource/` | 图标等编译期资源 | `weasel.ico`、`zh.ico`、`en.ico`、`full.ico`、`half.ico`、`reload.ico`，被各工程的 `.rc` 资源文件引用 |
| `output/` | 安装暂存 / 打包目录 | 运行时依赖（`7z.exe`、`curl.exe`、`WinSparkle.dll`）、安装脚本、`data/weasel.yaml` 默认配置、`data/preview/` 配色预览图、`archives/` 最终安装包输出 |
| `update/` | 自动更新与版本管理 | `appcast.xml`（WinSparkle 更新源）、`bump-version.ps1/.sh`（升版本号）、`write-release-notes.sh` |
| `arm64x_wrapper/` | ARM64X 跨架构 DLL 封装 | `build.bat`、`WeaselTSF_arm64.def`、`WeaselTSF_x64.def`、`dummy.c`，用于把 WeaselTSF 打包成同时支持 x64 与 ARM64 的单一 DLL |
| `lib/`、`lib64/` | 预编译库搜索路径 | 作为 `AdditionalLibraryDirectories` 出现在 `.vcxproj` 中（如 [WeaselDeployer.vcxproj:104](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/WeaselDeployer.vcxproj#L104)） |
| `librime/` | git 子模块：Rime 引擎 | 引擎本体，C API 头在 `librime/include`，被 RimeWithWeasel、WeaselDeployer 引用 |
| `plum/` | git 子模块：输入方案下载器 | 用于获取/管理 Rime 输入方案（朙月拼音、仓颉等） |
| `.github/workflows/` | 持续集成 | `ci.yml`（构建 CI）、`update-appcast.yml`（更新分发源） |

子模块的声明在 [.gitmodules:1-5](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/.gitmodules#L1-L5)——`librime` 和 `plum` 各占一条。

#### 4.3.3 源码精读：output/ 与 test/ 的内部结构

**(a) `output/`：安装暂存目录**

`output/` 是 Weasel「装到本地」的目标目录雏形。构建脚本（`build.bat`）会把各子工程产物和运行时依赖复制到这里，安装程序再从这里打包。可以看到：

- **运行时第三方依赖**：`7z.exe`/`7z.dll`（自解压/打包）、`curl.exe`/`curl-ca-bundle.crt`（下载方案）、`WinSparkle.dll` 及 `Win32/WinSparkle.dll`（自动更新）。
- **安装/服务脚本**：`install.bat`、`uninstall.bat`、`start_service.bat`、`stop_service.bat`、`install.nsi`（NSIS 安装脚本）、`sudo.js`、`check_windows_version.js`。
- **默认配置与资源**：`data/weasel.yaml`（Weasel 默认设置）、`data/preview/color_scheme_*.png`（Deployer 里供预览的配色截图）。
- **打包产物输出**：`archives/`（含 `.placeholder` 占位），最终安装压缩包落在这里。

> 这些文件大多是提交进仓库的二进制或脚本，便于「checkout 后即可打包安装」，无需额外联网下载第三方工具。

**(b) `test/`：两个 Google Test 工程**

`test/` 下有两个独立的测试工程，已经在 `weasel.sln` 里注册（[weasel.sln:32](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/weasel.sln#L32) 与 [weasel.sln:36](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/weasel.sln#L36)）：

- `test/TestResponseParser/`：针对 IPC 响应解析协议做单元测试，依赖 WeaselIPC（[TestResponseParser.vcxproj:342](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.vcxproj#L342)）。
- `test/TestWeaselIPC/`：端到端地启动 Client/Server 验证整条 IPC 通道，依赖 RimeWithWeasel、WeaselIPCServer、WeaselIPC、WeaselUI（[TestWeaselIPC.vcxproj:350-363](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.vcxproj#L350-L363)）。

测试的深入讲解放在 u7-l1，本讲只需记住「测试也是解决方案里的一等公民，和正式工程同样有 `.vcxproj`」。

#### 4.3.4 代码实践：盘点 output/ 会被哪些文件填充

**实践目标**：理解 `output/` 既包含「提交进仓库的现成文件」，又会被构建过程「动态填充」。

**操作步骤**：

1. 列出 `output/` 目录现有文件（`ls output/`、`ls output/data/`）。
2. 打开 `build.bat`（或 `output/install.bat`），搜索其中把产物复制到 `output` 的命令（示例：`rg -n "output" build.bat`）。
3. 对照 4.1 的产出表，推断哪些 `.exe`/`.dll` 在编译后会被拷进 `output/`。

**需要观察的现象**：

- `output/` 里已经存在 `7z.exe`、`curl.exe`、`WinSparkle.dll`、`data/weasel.yaml` 等非源码文件。
- `output/archives/` 当前只有 `.placeholder`，说明真正的安装包由构建流程生成。

**预期结果**：你会得到一张「output/ 内容来源表」——一部分是仓库直接提交的第三方工具与脚本，一部分是 Weasel 各子工程编译产物（WeaselServer.exe、WeaselDeployer.exe、WeaselSetup.exe、weasel.dll 等），还有一部分是构建脚本联网或本地生成的安装包。具体的复制步骤留到 u1-l3 讲构建系统时验证。

> 待本地验证：`build.bat` 的确切复制目标与顺序请以本地文件为准。

#### 4.3.5 小练习与答案

**练习 1**：`resource/` 里的 `.ico` 文件是如何进入最终程序的？

**参考答案**：它们被各工程的资源脚本（`.rc` 文件，如 `WeaselServer.rc`、`WeaselSetup.rc`）通过资源编译器（`rc.exe`）编译进 `.exe`/`.dll`，运行时再用 `LoadIcon` 等 API 取出，用于托盘图标、窗口图标等。

**练习 2**：为什么 WeaselTSF 需要 `arm64x_wrapper/`，而其它工程不需要？

**参考答案**：WeaselTSF 产出的是被系统加载的输入法 DLL，Windows 要求在 ARM64 设备上以 ARM64X（同时包含 x64 与 ARM64 代码的单一二进制）形式提供，才能在 x64 应用与 ARM64 应用里都被正确加载。`arm64x_wrapper/` 里的 `.def` 文件和 `build.bat` 就是用来生成这种合并 DLL 的；而 WeaselServer 等普通 EXE 不需要这种特殊处理。

**练习 3**：`librime/` 为什么是 git 子模块而不是直接拷进来的源码？

**参考答案**：librime 是独立维护的引擎项目（跨 macOS/Linux/Windows 共用）。用子模块既能锁定特定版本，又能跟随上游升级，避免在 Weasel 仓库里维护一份会过时的副本。其它平台前端（鼠须管、fcitx5-rime）也各自以子模块或依赖方式引用同一个 librime，体现了「引擎与前端分离」的设计。

## 5. 综合实践

**任务：绘制一张完整的 Weasel 仓库「目录地图」海报。**

把本讲三个最小模块的内容整合成一张图（可以用纸笔、Markdown 表格或任意画图工具）：

1. **中央**画出 10 个子工程及其依赖箭头（复用 4.1.3 的图）。
2. **左侧**标注每个工程的产出类型（EXE/DLL/lib）与产物文件名。
3. **右侧**画出 `include/` 公共头目录，并用线连到「谁在用它们」的工程。
4. **底部**列出辅助目录（`test`、`resource`、`output`、`update`、`arm64x_wrapper`、`librime`、`plum`）及其一句话职责。
5. **顶部**写一句「数据流总览」（来自 u1-l1）：应用进程抓键 → IPC → Server → librime → UI → 上屏。

完成后，对照下图自检：你能指着地图说出「一次按键从哪个工程的哪个目录开始、最后回到哪个工程的哪个目录」吗？如果可以，本讲目标达成。

> 进阶挑战：把这张地图保存进你的学习笔记，后续每学一篇讲义，就在对应模块旁补一句「关键文件 + 行号」，到本手册学完时，你会得到一份带永久链接的 Weasel 全景索引。

## 6. 本讲小结

- Weasel 由 `weasel.sln` 统管的 **10 个编译子工程** 组成：4 个静态库（WeaselIPC、WeaselUI、WeaselIPCServer、RimeWithWeasel）、1 个 DLL（WeaselTSF，产物 `weasel.dll`）、3 个应用（WeaselServer / WeaselDeployer / WeaselSetup）、2 个测试应用（TestWeaselIPC / TestResponseParser）。
- 子工程类型由 `.vcxproj` 的 `ConfigurationType` 决定，依赖关系由 `ProjectReference` 决定；三个叶子库是 WeaselUI / WeaselIPC / WeaselIPCServer，WeaselServer 依赖最重。
- `include/` 是公共头目录，通过 `$(SolutionDir)\include` 加入全部工程的包含路径；`WeaselIPCData.h`、`WeaselUI.h`、`WeaselIPC.h` 是最核心的几张「数据契约」。
- Weasel 的 GUI 基于 **WTL/ATL**（外部头文件库），通过各工程的 `stdafx.h` 引入。
- 辅助目录分工明确：`test/` 测试、`resource/` 图标、`output/` 安装暂存与打包、`update/` 自动更新、`arm64x_wrapper/` 跨架构 DLL、`librime` 与 `plum` 为 git 子模块。

## 7. 下一步学习建议

本讲建立了目录与依赖地图，但还没有回答「这些东西怎么编译出来」。建议下一步：

1. **阅读 u1-l3《构建系统与从源码运行调试》**：搞懂 `build.bat`、`env.bat`、`weasel.sln` 与 `xmake.lua` 两套构建体系、版本号注入与 `BOOST_ROOT` 等环境变量，把本讲的「产出类型」落实到「真的产出文件」。
2. 在动手编译前，先浏览 `INSTALL.md` 和 `build.bat`，对照本讲的依赖表，思考「为什么构建前要先准备好 Boost、librime、WTL」。
3. 想提前感受真实代码，可以打开 `include/WeaselIPC.h` 浏览 IPC 命令枚举（为 u2 单元 IPC 骨架预热），但不必现在就读懂细节。

当你能在本讲的地图上指出「下一篇要讲的构建脚本，会把我图里的哪些工程编译出来、放到 `output/` 的哪里」时，就可以进入 u1-l3 了。
