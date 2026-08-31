# 从 main() 到 AppWindow：GUI 启动流程

## 1. 本讲目标

学完本讲，你应该能够：

1. 完整跟踪 `main.cpp` → `AppWindow` 构造函数 → 事件循环的启动序列，并说出每一步做了什么。
2. 列出 `QCommandLineParser` 支持的全部命令行选项，解释 `--no-gui` 无头模式的实现机制（`noGUIset` / `InformationBox::setGUI()` / `showGUI()`）。
3. 在 `appwindow.cpp` 中快速定位设备连接、模式创建/切换、SCPI 命令树、TCP 服务器、流式服务器各自的初始化位置。
4. 理解 UNIX 信号处理（SIGINT）为什么对无头模式必不可少。

本讲承接 u1-l3 的结论：「main.cpp 只有 34 行，初始化全部在 AppWindow 构造函数里」。本讲就把这个构造函数彻底拆开。

## 2. 前置知识

- **Qt 事件循环**：Qt 程序的 `main()` 通常只做三件事——创建 `QApplication`、创建主窗口、调用 `app->exec()` 进入事件循环。`exec()` 不会返回，直到有人调用 `quit()`。此后所有工作都由「事件 → 信号 → 槽」驱动。
- **信号与槽（signals/slots）**：Qt 的回调机制。`connect(发送者, 信号, 接收者, 槽)` 建立绑定；发送者 `emit` 信号时槽被调用。本讲会大量出现 `connect(...)`，可以把它读作「把 UI 事件接到处理函数上」。
- **QMainWindow 与 .ui 文件**：Qt 主窗口自带菜单栏、工具栏、状态栏、停靠区。界面可以用 Qt Designer 的 `.ui` XML 文件描述，构建时由 `uic` 工具生成 `ui_xxx.h` 头文件。LibreVNA 的主窗口界面在 `main.ui`（在 `.pro` 的 `FORMS` 中登记，见 [Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro:L347](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L347) 和 [L421](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L421)），`appwindow.cpp` 里 `#include "ui_main.h"` 用的 `ui_main.h` 就是它的产物（生成到构建目录，仓库里看不到）。
- **QCommandLineParser**：Qt 自带的命令行解析器。`addOption()` 注册选项，`process()` 解析并处理——注意 `process()` 遇到 `--help`/`--version` 或非法选项时会**直接打印并退出程序**。
- **单例（singleton）**：全局唯一对象。`Preferences::getInstance()` 返回整个应用共享的偏好设置对象。
- **无头模式（headless / `--no-gui`）**：程序不显示任何窗口，但事件循环照常运行，SCPI/TCP 远程控制仍然可用。难点在于：代码里所有「弹对话框」的地方都必须被拦下来，否则无界面环境下模态对话框会把程序挂死。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Software/PC_Application/LibreVNA-GUI/main.cpp` | 程序入口：创建 `QApplication` 与 `AppWindow`，注册 SIGINT 处理，进入事件循环 |
| `Software/PC_Application/LibreVNA-GUI/appwindow.h` | `AppWindow` 类声明：成员变量勾勒出它管理的子系统（模式、设备、SCPI、TCP、流式服务器、状态栏标签） |
| `Software/PC_Application/LibreVNA-GUI/appwindow.cpp` | 本讲主战场：构造函数装配整个应用；`SetupMenu`/`SetupStatusBar`/`CreateToolbars`/`SetupSCPI`/`SetInitialState`/`ConnectToDevice` 等全部在此 |
| `Software/PC_Application/LibreVNA-GUI/main.ui` | 主窗口的 XML 界面描述（菜单栏、动作），`uic` 生成 `ui_main.h` |
| `Software/PC_Application/LibreVNA-GUI/CustomWidgets/informationbox.cpp` | `--no-gui` 抑制对话框的另一半机制：`InformationBox::setGUI()` / `has_gui` |
| `Software/PC_Application/LibreVNA-GUI/Util/app_common.h` | 一行宏 `qlibrevnaApp`，取全局应用实例 |
| `Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp`、`SpectrumAnalyzer/spectrumanalyzer.cpp` | 提交 c4276df 补上 `showGUI()` 判断的两处（校准/归一化进度对话框） |

## 4. 核心概念与源码讲解

### 4.1 main.cpp 启动

#### 4.1.1 概念说明

LibreVNA-GUI 的入口刻意写得极薄：`main()` 不做任何业务初始化，只负责「造应用 → 造窗口 → 进事件循环」三步，把所有初始化责任推给 `AppWindow` 构造函数。这种「薄入口 + 厚构造函数」的写法让启动序列集中在一处，读代码时只需盯住一个函数。

此外 `main.cpp` 还承担两件小事：

1. **统一日志格式**：`qSetMessagePattern` 给每条 `qDebug`/`qInfo` 输出加上「进程启动以来的时间戳」。这个时间戳是本讲综合实践里重建启动时间线的关键工具。
2. **优雅退出**：UNIX 下把 SIGINT（Ctrl+C）接到 `tryExitGracefully()`。没有它，Ctrl+C 会直接杀死进程，`AppWindow::closeEvent()` 里的清理逻辑（保存设置、断开设备）一条都不会执行——对无头模式来说这是唯一的「关机按钮」。

#### 4.1.2 核心流程

`main()` 的执行顺序：

1. 设置日志格式（带进程时间戳）。
2. `new QApplication`，登记组织名与应用名（供 `QSettings` 持久化定位用）。
3. `new AppWindow` —— **构造函数内完成 99% 的初始化**（见 4.2）。
4. 用窗口里带的版本号 + git 哈希前 9 位设置应用版本。
5. UNIX 下注册 SIGINT 处理器。
6. `app->exec()` 进入事件循环，直到退出。

伪代码：

```text
main():
    日志格式 = "进程时间: [级别] 消息"
    app    = new QApplication(argc, argv)
    登记组织名/应用名          # QSettings 的存储定位
    window = new AppWindow     # ★ 全部初始化发生在这里
    设置应用版本 = window.版本 + "-" + git哈希前9位
    [UNIX] SIGINT -> tryExitGracefully
    return app.exec()          # 事件循环，阻塞至此
```

#### 4.1.3 源码精读

**入口全貌**——[Software/PC_Application/LibreVNA-GUI/main.cpp:L18-L34](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp#L18-L34)：`main()` 的全部 17 行。注意 `window = new AppWindow;` 这一行执行时构造函数里已经把菜单、状态栏、三种模式、SCPI 树、设备列表全部装配完毕。

**日志格式与全局指针**——[Software/PC_Application/LibreVNA-GUI/main.cpp:L7-L8](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp#L7-L8) 和 [L20](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp#L20)：`app`、`window` 是文件级静态指针，唯一目的是让信号处理函数能拿到它们（信号处理函数没有参数可传上下文）。

**SIGINT 处理**——[Software/PC_Application/LibreVNA-GUI/main.cpp:L10-L16](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp#L10-L16)：`tryExitGracefully()` 先 `window->close()`（触发 `closeEvent` 做完整清理），再 `app->quit()`（让 `exec()` 返回）。整段被 `#ifdef Q_OS_UNIX` 包裹，Windows 下没有这个优雅退出路径。

**版本号来源**——[Software/PC_Application/LibreVNA-GUI/main.cpp:L26-L27](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp#L26-L27)：应用版本来自 `window->getAppVersion()`，而它的值在构造函数初始化列表里就绪——[Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L61-L64](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L61-L64) 用 `FW_MAJOR/MINOR/PATCH` 拼出版本、`GITHASH` 宏给出 git 哈希（这两个宏由 qmake 在编译时注入，见 u1-l3）。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到「薄入口」和带时间戳的日志格式。
2. **操作步骤**：
   - 在已编译的构建目录里运行 `./LibreVNA-GUI`（无需连接硬件）。
   - 观察终端最先打印的一行 `Application start`（它其实来自 [appwindow.cpp:L83](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L83)，证明 `main()` 打印的第一条业务日志已经是构造函数内部的输出）。
   - 在普通模式下用鼠标关闭窗口退出；再启动一次，这次在终端按 Ctrl+C。
3. **需要观察的现象**：每行日志前面的时间戳（如 `0.012: [debug] Application start`）；Ctrl+C 后进程是否正常退出（退出码 0）。
4. **预期结果**：两种退出方式都走 `closeEvent`（Ctrl+C 经由 `tryExitGracefully`）。若 Ctrl+C 导致进程被直接杀死且无清理日志，说明当前平台不是 UNIX 分支（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tryExitGracefully()` 里要先 `window->close()` 再 `app->quit()`，而不是只调用 `quit()`？

**答案**：`quit()` 只让事件循环返回，不会触发窗口的 `closeEvent()`。而保存窗口几何、关闭模式、断开设备、删除驱动、存储偏好设置等清理逻辑全写在 [AppWindow::closeEvent](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L270-L294) 里。只调 `quit()` 会跳过全部清理，设备可能停留在测量状态、设置不会被保存。

**练习 2**：`main.cpp` 里 `QCoreApplication::setOrganizationName/setApplicationName` 影响什么？

**答案**：它们决定 `QSettings` 的存储位置（如 Linux 下 `~/.config/LibreVNA/LibreVNA-GUI.conf`）。构造函数里的 `restoreGeometry(settings.value("geometry"))`（[appwindow.cpp:L185-L188](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L185-L188)）和 `InformationBox` 的「不再显示」记录（[informationbox.cpp:L118-L121](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/informationbox.cpp#L118-L121)）都依赖这两个名字。

### 4.2 AppWindow 构造与菜单/工具栏/状态栏

#### 4.2.1 概念说明

`AppWindow` 是整个 GUI 的「总装车间」：它继承 `QMainWindow`，把界面骨架（来自 `main.ui`）、三大测量模式（VNA/信号源/频谱仪）、设备驱动连接、SCPI 命令树、TCP/流式服务器、设备日志全部装配到一个对象里。[appwindow.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.h#L115-L176) 的私有成员区就是这个子系统的清单：`modeHandler`、`device`、`deviceList`、`scpi`、`server`、五个 `StreamingServer*`、一组状态栏 `QLabel`、`QCommandLineParser parser`。

理解本讲的关键是抓住构造函数的**装配顺序**——它体现了几个依赖关系：

- 偏好设置必须最先加载（后面 TCP 端口、是否自动连接都从它读）。
- SCPI/TCP 服务器在界面装配之前就启动（无头模式下没有界面，但服务必须在）。
- `ui->setupUi(this)` 必须先于一切使用 `ui->` 成员的代码（状态栏、菜单都挂在 `ui` 上）。
- 模式创建（`SetInitialState`）必须晚于 `ModeHandler` 和中央 `QStackedWidget`。
- 设备枚举放在最后，此时一切就绪，自动连接可以立即工作。

#### 4.2.2 核心流程

`AppWindow::AppWindow()`（[appwindow.cpp:L68-L217](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L68-L217)）的装配流水线，按执行顺序：

| 步骤 | 行号 | 内容 |
| --- | --- | --- |
| 0 | L70-L79 | 初始化列表：`ui(new Ui::MainWindow)`、五个流式服务器指针置空、版本号/哈希就绪 |
| 1 | L83-L85 | 打印首条日志、设置窗口图标 |
| 2 | L87-L97 | **命令行解析**（详见 4.3），`parser.process()` 可能在此时直接退出程序 |
| 3 | L99-L105 | 加载偏好设置（或按 `--reset-preferences` 重置） |
| 4 | L107-L109 | `device = nullptr`、`modeHandler = nullptr`：初始即「未连接任何设备」 |
| 5 | L111-L122 | 启动 SCPI 的 TCP 服务器（`-p` 优先，否则看偏好设置） |
| 6 | L124-L138 | 按偏好设置创建最多 5 个流式数据服务器（VNA Raw/Cal/Deemb、SA Raw/Norm） |
| 7 | L140 | `ui->setupUi(this)`：实例化 `main.ui` 描述的菜单栏/动作/状态栏 |
| 8 | L142-L143 | 装状态栏 + 立即显示「No device connected」 |
| 9 | L145 | `CreateToolbars()`：参考输入/输出工具栏 |
| 10 | L147-L160 | 「Device Log」停靠窗口；扫描全部 dock/toolbar 填充 Window 菜单 |
| 11 | L162-L167 | 创建 `ModeHandler` + `ModeWindow`（模式切换 UI）；中央区设为 `QStackedWidget` |
| 12 | L169-L174 | 把模式的状态栏消息、模式切换信号接到本窗口 |
| 13 | L176 | `SetupMenu()`：给 `.ui` 里的动作接槽 |
| 14 | L178-L188 | 窗口标题、四角停靠策略、恢复上次窗口几何 |
| 15 | L190 | `SetupSCPI()`：装配 SCPI 命令树 |
| 16 | L192 | `SetInitialState()`：创建三个内置模式并激活 VNA |
| 17 | L194-L200 | `UpdateDeviceList()` 枚举设备；若偏好允许且列表非空，自动连接第一台 |
| 18 | L202-L208 | 处理 `--setup` / `--cal` 启动加载 |
| 19 | L209-L216 | 有 GUI：`show()` 显示窗口；`--no-gui`：设置 `noGUIset` 并跳过显示 |

其中步骤 8/9/13 的展开：

- `SetupStatusBar()`（定义在 [appwindow.cpp:L1293-L1327](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1293-L1327)）依次摆放：连接状态、设备信息、Setup 文件名、模式信息、三个默认隐藏的红色告警标签（ADC overload / Unlevel / Unlock）。
- `CreateToolbars()`（[appwindow.cpp:L501-L519](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L501-L519)）创建 Reference 工具栏（外部参考输入类型、参考输出频率两个下拉框），并用一个 100ms 单次 `QTimer` 给 `setExtRef` 的下发做防抖。
- `SetupMenu()`（[appwindow.cpp:L225-L268](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L225-L268)）把 `main.ui` 中定义的动作接到槽：刷新设备列表、断开、退出、保存/加载 setup、截图、Preset、Preferences、About。

#### 4.2.3 源码精读

**构造函数开头：解析命令行、加载偏好**——[Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L83-L105](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L83-L105)：`parser.process()` 在构造函数内部执行——这意味着 `--help`、`--version`、非法选项都会在窗口创建前就让进程结束；随后 `Preferences::getInstance().load()`（或 `setDefault()`）保证后续每一步都能读到配置。

**无界面也先起服务**——[Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L111-L138](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L111-L138)：TCP 服务器与流式服务器的创建完全不看 `--no-gui`，只看 `-p` 和偏好设置——这正是无头模式能当「SCPI 服务」用的原因。`StartTCPServer` 的实现只有三行：[appwindow.cpp:L793-L798](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L793-L798)，把 `TCPServer::received` 接到 `SCPI::input`、把 `SCPI::output` 接回 `TCPServer::send`，形成命令回路。

**界面骨架与模式容器**——[Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L140-L176](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L140-L176)：`ui->setupUi(this)` 生成菜单栏（File/Device/Window/View/Help，定义见 [main.ui:L26-L94](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.ui#L26-L94)）；`central = new QStackedWidget` 把中央区变成一个「多页签容器」，之后每种模式的界面各占一页，由 `ModeHandler` 切换（u2-l2 专题）。

**初始状态 = 三种内置模式**——[Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L296-L309](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L296-L309)：`SetInitialState()` 先 `closeModes()` 清场；若偏好设置指定了启动 setup 文件则改为加载它，否则依次 `createMode("Vector Network Analyzer", VNA)`、`createMode("Signal Generator", SG)`、`createMode("Spectrum Analyzer", SA)` 并把 VNA 设为当前模式。

**设备枚举与自动连接**——[Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L194-L200](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L194-L200)：`UpdateDeviceList()`（定义在 [L906-L950](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L906-L950)）遍历 `DeviceDriver::getDrivers()` 收集每个驱动发现的序列号，填进 `menuConnect_to` 菜单；若 `Startup.ConnectToFirstDevice` 为真且有设备，直接 `ConnectToDevice(deviceList[0].serial)`。连接过程本身（驱动匹配、信号接线、`connectDevice` 调用）在 [ConnectToDevice，L325-L442](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L325-L442)，细节留给 u3 驱动层讲。

**SCPI 命令树装配点**——[Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L521-L537](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L521-L537)：`SetupSCPI()` 的开头注册了 `*IDN`（返回序列号+版本）与 `*RST`（回到 `SetResetState`），随后构建 `DEVice` 子树（连接/断开/列表/偏好/setup/参考/模式/状态/信息/限制）。整棵树约 270 行，是 u10 的主角，本讲只需知道它在这里被装配。

**状态栏两个状态**——[Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L1329-L1344](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1329-L1344)：`UpdateStatusBar` 只有 Connected / Disconnected 两种；构造时传入 Disconnected，于是左下角显示「No device connected / No status information available yet」。

#### 4.2.4 代码实践

1. **实践目标**：不看运行结果，仅凭源码写出 `AppWindow` 构造函数的执行顺序表，并用日志验证。
2. **操作步骤**：
   - 通读 [appwindow.cpp:L68-L217](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L68-L217)，按 4.2.2 的表格逐行打钩，标出每一步触发的 `qDebug` 输出（至少有三处：L83 `Application start`、L948 `Updated device list, found N`、无设备时没有连接日志）。
   - 运行 `./LibreVNA-GUI`，对照终端的时间戳日志，把每条日志映射回表格步骤。
   - 用 `grep -n "connect(" appwindow.cpp` 统计构造函数区间的信号接线数量，确认 `SetupMenu` 的接线都在 `ui->setupUi` 之后。
3. **需要观察的现象**：日志顺序与源码顺序一致；`Updated device list, found 0`（无硬件时）出现在窗口显示前后。
4. **预期结果**：能产出一张「步骤 → 行号 → 日志证据」三列对照表。若某条日志缺失，检查是否依赖硬件（如连接成功的 `qInfo`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SetupStatusBar()`、`CreateToolbars()` 必须在 `ui->setupUi(this)` 之后调用，而 TCP 服务器可以在它之前启动？

**答案**：`setupUi` 才会创建 `ui->statusbar`、菜单栏等成员，之前访问 `ui->statusbar->addWidget(...)` 是空指针解引用。TCP/流式服务器只依赖 `Preferences` 与 `TCPServer`/`StreamingServer` 自身，不碰 `ui`，而且无头模式下根本不需要界面，所以放在最前面（[appwindow.cpp:L111-L138](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L111-L138)）。

**练习 2**：`SetInitialState()` 和 `SetResetState()`（[appwindow.cpp:L311-L323](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L311-L323)）都创建三种模式，差别在哪？

**答案**：`SetInitialState` 会优先加载偏好设置里指定的 setup 文件（恢复上次工作区）；`SetResetState` 无视任何已保存状态，创建模式后还逐个调用 `m->resetSettings()` 把设置归零。后者正是 SCPI `*RST` 命令的落点（[appwindow.cpp:L533-L537](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L533-L537)）。

**练习 3**：状态栏的三个红色告警标签（ADC overload / Unlevel / Unlock）初始为什么不可见？何时变可见？

**答案**：`SetupStatusBar` 里对每个标签 `setVisible(false)`（[appwindow.cpp:L1312-L1325](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1312-L1325)）。设备连接后驱动发出 `FlagsUpdated` 信号，`DeviceFlagsUpdated()` 按标志位切换可见性（[appwindow.cpp:L1058-L1063](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1058-L1063)）。

### 4.3 命令行参数与无头模式

#### 4.3.1 概念说明

命令行解析不在 `main()` 里，而在 `AppWindow` 构造函数早期（[appwindow.cpp:L87-L97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L87-L97)），因为多数选项要驱动后续装配步骤（`-p` 决定端口、`--setup` 决定加载哪个文件、`--no-gui` 决定是否 `show()`）——解析必须先于使用。

`--no-gui`（无头模式）的实现分两层：

1. **窗口层**：构造函数末尾不走 `show()`，只置静态标志 `noGUIset = true`（[appwindow.cpp:L209-L216](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L209-L216)）；静态函数 `AppWindow::showGUI()` 返回 `!noGUIset`（[L1288-L1291](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1288-L1291)），供全程序任何地方查询。
2. **对话框层**：`InformationBox::setGUI(false)` 关掉所有消息框/询问框——`ShowMessage`、`ShowError`、`AskQuestion` 开头都有 `if(!has_gui) return;`（[informationbox.cpp:L9-L48](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/informationbox.cpp#L9-L48)、`AskQuestion` 在 [L50-L54](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/informationbox.cpp#L50-L54) 直接返回默认答案）。

为什么不只靠 `InformationBox` 一层？因为程序里还有大量**非** `InformationBox` 的弹窗——各种编辑对话框、进度对话框（`QProgressDialog`）。它们遍布 30 多处 `if(AppWindow::showGUI())` 守卫（用 `grep "showGUI()"` 可以全部找出来，从 VNA 校准到去嵌入、从眼图到中值滤波）。这正是 HEAD 提交 c4276df 的内容：补上最后两处漏网之鱼——VNA 校准进度与 SA 归一化进度对话框。

#### 4.3.2 核心流程

**选项注册与处理**（[appwindow.cpp:L87-L97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L87-L97)）：

```text
parser.setApplicationDescription(...)   # 描述
parser.addHelpOption()                  # -h / --help
parser.addVersionOption()               # -v / --version
addOption({p,  port},  ...)             # SCPI TCP 端口，带值
addOption({d,  device}, ...)            # 只允许连接指定序列号的设备，带值
addOption(no-gui, ...)                  # 关闭图形界面
addOption(cal, ...)                     # 启动时加载校准文件，带值
addOption(setup, ...)                   # 启动时加载 setup 文件，带值
addOption(reset-preferences, ...)       # 偏好设置恢复默认
parser.process(QCoreApplication::arguments())   # ★ 解析；--help/--version/非法选项直接退出
```

**无头模式的判定链**：

```text
命令行带 --no-gui
   └─> 构造函数末尾 [L209-L216]
         ├─ InformationBox::setGUI(false)   # 拦截所有 InformationBox 消息框
         ├─ noGUIset = true                 # AppWindow::showGUI() 从此返回 false
         └─ 跳过 resize(1280,800) 与 show() # 不创建可见窗口
之后任何弹窗代码：
   ├─ InformationBox::ShowError(...) → has_gui==false → 直接 return [informationbox.cpp L42-L44]
   └─ QProgressDialog 等 → 外层 if(window->showGUI()) 包裹 → 不创建/不更新
事件循环照常运行：SCPI TCP、流式服务器、设备测量全部可用
退出：SIGINT → tryExitGracefully → closeEvent 清理 → quit
```

**带值选项的消费时机**（解析一次、多处使用）：

- `port`：构造函数 L111-L122 决定 SCPI 服务器端口；
- `device`：`UpdateDeviceList()` 里过滤设备（[appwindow.cpp:L916-L919](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L916-L919)）；
- `setup` / `cal`：构造函数 L202-L208 启动加载；
- `reset-preferences`：L99-L103 决定 `setDefault()` 还是 `load()`。

#### 4.3.3 源码精读

**选项注册**——[Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L87-L97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L87-L97)：七个自定义/内置选项全部在这里；`QCommandLineOption({"p","port"}, "描述", "port")` 第三个参数是「值的名字」，表示该选项需要附带参数。

**分流：显示窗口还是无头**——[Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L209-L216](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L209-L216)：构造函数的最后一步。正常路径 `InformationBox::setGUI(true)` + `resize(1280,800)` + `show()`；无头路径 `setGUI(false)` + `noGUIset = true`。注意即使无头，前面 16 个步骤（偏好、服务器、模式、设备列表）已经全部完成。

**全局开关的存储**——[Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L66](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L66) 与 [L1288-L1291](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1288-L1291)：文件级静态布尔 `noGUIset` + 静态成员函数 `showGUI()`，任何类无需持有 `AppWindow` 指针即可查询（例如 [VNA/Deembedding/deembedding.cpp:L15](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/deembedding.cpp#L15)）。

**对话框拦截器**——[Software/PC_Application/LibreVNA-GUI/CustomWidgets/informationbox.cpp:L40-L48](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/informationbox.cpp#L40-L48) 与 [L81-L84](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/informationbox.cpp#L81-L84)：`ShowError` 遇 `has_gui == false` 立即返回。这解释了一个行为：无头模式下 `ConnectToDevice` 失败不会弹「Failed to connect」框阻塞程序，只走 SCPI 返回错误（调用点在 [appwindow.cpp:L391-L395](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L391-L395)）。

**c4276df 补上的两处守卫**——[Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp:L186-L192](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L186-L192)：VNA 校准测量时进度对话框的 `setValue` 现在被 `if(window->showGUI())` 包住；[Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp:L824-L833](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L824-L833)：SA 归一化测量连对话框的创建/信号接线都被守卫（另一处 `setValue` 在 [spectrumanalyzer.cpp:L563-L569](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L563-L569)）。此前无头模式下执行校准/归一化会试图弹模态进度框，在没有窗口系统的环境里造成问题。

**`qlibrevnaApp` 宏**——[Software/PC_Application/LibreVNA-GUI/Util/app_common.h:L6](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/app_common.h#L6)：构造函数里 `parser.setApplicationDescription(qlibrevnaApp->applicationName())`、窗口标题也取自它——本质是 `QCoreApplication::instance()` 的别名，避免到处传指针。

#### 4.3.4 代码实践

1. **实践目标**：整理完整的命令行选项表；用 `--no-gui` 实际启动一次，从日志与行为上验证「窗口与对话框被抑制」。
2. **操作步骤**：
   - **第一步（制表）**：只读 [appwindow.cpp:L87-L97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L87-L97)，整理出全部选项（含 `addHelpOption`/`addVersionOption` 两个内置项），列成「短名 / 长名 / 是否带值 / 描述 / 消费位置」五列表格（预期结果见下）。
   - **第二步（验证帮助）**：运行 `./LibreVNA-GUI --help`，把实际输出与你的表格逐行核对（`--help` 由 `process()` 处理，打印后进程直接退出）。
   - **第三步（无头启动）**：运行 `./LibreVNA-GUI --no-gui -p 19526`（`-p` 让 SCPI 服务器监听 19526 端口，便于观察）。确认：没有任何窗口出现；终端持续打印带时间戳的日志（`Application start`、`Updated device list, found ...`）；进程不退出（事件循环在跑）。
   - **第四步（验证对话框抑制）**：另开终端，`nc localhost 19526` 后输入 `*IDN?` 应返回 `LibreVNA,LibreVNA-GUI,Not connected,<版本>`（对应 [appwindow.cpp:L523-L532](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L523-L532)）。再发送 `:DEVice:CONNect "不存在的序列号"`：普通模式下这会先弹「Failed to connect」错误框（`InformationBox::ShowError`，[appwindow.cpp:L391-L395](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L391-L395)）；无头模式下该框被 [informationbox.cpp:L42-L44](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/informationbox.cpp#L42-L44) 拦截，SCPI 客户端只会收到错误应答，程序不弹窗、不阻塞——这就是「对话框被抑制」的直接证据。
   - **第五步（优雅退出）**：对无头进程按 Ctrl+C，确认它执行清理后正常退出。
3. **需要观察的现象**：无窗口、日志持续、`*IDN?` 有应答、非法连接不弹窗、Ctrl+C 干净退出。
4. **预期结果**（选项表，可直接与你整理的对照）：

| 短名 | 长名 | 带值 | 描述（源码原文翻译） | 消费位置 |
| --- | --- | --- | --- | --- |
| `-h` | `--help` | 否 | 显示帮助（内置） | `parser.process()` 直接退出 |
| `-v` | `--version` | 否 | 显示版本（内置） | `parser.process()` 直接退出 |
| `-p` | `--port` | 是 | SCPI 命令监听端口 | 构造函数 L111-L122 |
| `-d` | `--device` | 是 | 只允许连接指定设备 | `UpdateDeviceList` L916-L919 |
| 无 | `--no-gui` | 否 | 禁用图形界面 | 构造函数 L209-L216 |
| 无 | `--cal` | 是 | 启动时加载校准文件 | 构造函数 L205-L208 |
| 无 | `--setup` | 是 | 启动时加载 setup 文件 | 构造函数 L202-L204 |
| 无 | `--reset-preferences` | 否 | 偏好设置恢复默认 | 构造函数 L99-L103 |

   第四步中 `*IDN?` 返回的具体格式、以及有硬件时校准/归一化进度框在 `--no-gui` 下不再出现（c4276df 的原始场景）：**待本地验证**（后者需要连接真实设备并触发一次校准或归一化测量）。

#### 4.3.5 小练习与答案

**练习 1**：`--no-gui` 模式下程序靠什么退出？为什么这条路径对无头部署至关重要？

**答案**：靠 SIGINT → `tryExitGracefully()`（[main.cpp:L10-L16](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp#L10-L16)，仅 UNIX）。没有窗口就没有关闭按钮，若不捕获 SIGINT，Ctrl+C 或系统关机信号会直接杀死进程，`closeEvent` 中的断开设备、保存设置、删除驱动等清理全部跳过。

**练习 2**：为什么 `InformationBox` 的拦截不够，还需要遍布各处的 `if(AppWindow::showGUI())`？

**答案**：`InformationBox` 只能拦住经过它封装的消息框/询问框；项目里还有大量直接创建的 `QDialog`、`QProgressDialog`（如 VNA 校准进度框、SA 归一化进度框、各种设置对话框）。这些必须在调用点用 `showGUI()` 守卫——c4276df 正是给漏掉的两处进度框补上守卫（[vna.cpp:L188](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L188)、[spectrumanalyzer.cpp:L824](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L824)）。这是一个「防御要覆盖所有 UI 创建点」的架构权衡：无头支持是逐点修补出来的，不是集中式开关。

**练习 3**：细读 [main.cpp:L25-L27](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp#L25-L27)：`QCoreApplication::setApplicationVersion()` 在 `new AppWindow` **之后**才调用，而 `--version` 选项是在构造函数**内部**的 `parser.process()` 处理的。推测 `./LibreVNA-GUI --version` 会输出什么？

**答案**：`--version` 打印的是 `QCoreApplication::applicationVersion()`，而它此刻还没被设置（`new AppWindow` 尚未返回），所以很可能输出「LibreVNA-GUI + 空版本号」。真正的版本信息在 `appVersion` 成员里，但那要等构造完成才可通过 `getAppVersion()` 取到。这属于初始化顺序上的小瑕疵，具体输出**待本地验证**——本题的意义在于训练「谁先谁后」的顺序敏感度。

## 5. 综合实践

**任务：重建 LibreVNA-GUI 的启动时间线。**

把本讲三个模块串起来，产出一份有日志证据支撑的启动流程文档：

1. **源码侧**：通读 [main.cpp:L18-L34](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp#L18-L34) 与 [appwindow.cpp:L68-L217](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L68-L217)，画一张时序图（main、AppWindow、Preferences、TCPServer、ModeHandler、DeviceDriver 六条生命线），标注每个 `connect`/创建/调用发生的时刻。
2. **运行侧**：分别以 `./LibreVNA-GUI` 和 `./LibreVNA-GUI --no-gui -p 19526` 启动，利用 `main.cpp` 设置的 `%{time process}` 时间戳，把每条日志对齐到时序图的对应步骤；再用 `nc localhost 19526` 发 `*IDN?` 与一次注定失败的 `:DEVice:CONNect "0"`（观察应答与「不弹窗」）。
3. **对比侧**：两份日志逐行对比，回答：哪些步骤只在 GUI 模式出现？（预期：窗口 `show()` 相关；其余日志应一致。）Ctrl+C 退出无头实例，确认清理路径执行。
4. **产出**：`时间线表格（步骤/行号/日志/仅 GUI？）` + 时序图 + 一段 200 字总结「如果要在构造函数里插入一个新的初始化步骤（比如加载一个插件目录），应该插在哪一行、为什么」。

无硬件时全部步骤均可完成（设备列表为 0 是正常现象）；涉及真实设备连接、校准/归一化进度框对比的部分标注「待本地验证」。

## 6. 本讲小结

- `main.cpp` 是刻意的薄入口：创建 `QApplication` 与 `AppWindow`、设置版本、注册 SIGINT、进入事件循环；所有初始化集中在 `AppWindow` 构造函数（appwindow.cpp:68-217）。
- 构造函数的装配顺序体现依赖关系：命令行 → 偏好设置 → TCP/流式服务器 → `setupUi` → 状态栏/工具栏 → ModeHandler 与中央 `QStackedWidget` → SCPI 树 → 三大模式 → 设备枚举与自动连接 → `--setup/--cal` → `show()` 或无头分流。
- 菜单/动作来自 `main.ui`（`uic` 生成 `ui_main.h`），`SetupMenu` 负责接线，`SetupStatusBar` 摆放连接状态与三个告警标签，`CreateToolbars` 创建 Reference 工具栏。
- 8 个命令行选项中，`-p`、`-d`、`--cal`、`--setup`、`--no-gui`、`--reset-preferences` 是自定义的，解析发生在构造函数早期，`process()` 遇到 `--help/--version` 会直接退出。
- `--no-gui` 是两层机制：`noGUIset` 静态标志 + `AppWindow::showGUI()` 查询（窗口层），`InformationBox::setGUI(false)`（消息框层）；其余 30 多处直接创建的对话框靠分散的 `if(showGUI())` 守卫，HEAD 提交 c4276df 补齐了校准/归一化进度框两处。
- 无头模式的退出依赖 SIGINT → `tryExitGracefully` → `closeEvent` 完整清理，这是部署为无人值守 SCPI 服务的前提。

## 7. 下一步学习建议

- 下一讲 **u2-l2（Mode 系统与模式切换）**：本讲只看到 `SetInitialState()` 创建三种模式并把它们装进 `QStackedWidget`；下一讲深入 `mode.h`/`modehandler.cpp`，弄清 Mode 基类提供了哪些通用能力、模式如何切换与持久化。
- 之后 **u2-l3（设置体系）**：解释本讲反复出现的 `Preferences::getInstance()` 与 `LoadSetup`/`SaveSetup` 背后的 Savable/JSON 机制。
- 提前浏览但不深入：[appwindow.cpp 的 SetupSCPI](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L521-L791) 留给 u10；[ConnectToDevice](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L325-L442) 留给 u3 驱动层。
- 动手练习建议：把综合实践的时间线文档保留下来，学完 u2-l2 后回头补充「模式创建/激活」在时序图中的精确位置，检验自己的理解是否随学习深入而细化。
