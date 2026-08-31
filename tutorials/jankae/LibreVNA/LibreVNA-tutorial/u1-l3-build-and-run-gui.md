# 构建与运行 PC GUI 应用

## 1. 本讲目标

学完本讲，你应该能够：

1. 在自己的电脑上（以 Linux/Ubuntu 为主，兼顾 Windows/macOS）安装 Qt6 与 libusb 依赖，用 `qmake6` + `make` 从源码编译出 `LibreVNA-GUI` 可执行文件。
2. 读懂 `LibreVNA-GUI.pro` 这份 qmake 工程文件：它如何声明源码、链接库、Qt 模块、资源文件，以及版本号和 git 哈希是如何在编译期注入的。
3. 理解 `main.cpp` 中 `QApplication` 的启动入口，以及它把后续工作交棒给 `AppWindow` 的方式。
4. 在**没有 LibreVNA 硬件**的情况下验证 GUI 可用：导入仓库自带的 Touchstone 示例测量并显示 Smith 图。
5. 理解 udev 规则的作用：为什么没有它，普通用户无法访问 USB 设备。

承接上一讲（u1-l2）：我们已经知道仓库分为 GUI、固件、FPGA 三大子树。本讲只动 GUI 子树，**不碰任何硬件**。

## 2. 前置知识

- **qmake 与 .pro 文件**：qmake 是 Qt 自带的构建系统生成器。它读取一份 `.pro` 工程描述文件，生成平台对应的 Makefile，之后由 `make` 完成实际编译。可以把 `.pro` 文件理解为"这个工程包含哪些文件、依赖哪些库"的清单。
- **Qt 模块**：Qt 按功能拆成模块。本工程用到 `widgets`（按钮、窗口等传统控件）、`network`（TCP，用于远程控制）、`svg`（矢量图标渲染）。
- **libusb**：一个跨平台的用户态 USB 库。GUI 通过它直接和 LibreVNA 的 USB 设备通信，不需要内核专属驱动。编译时需要开发头文件（Ubuntu 包名 `libusb-1.0-0-dev`），运行时只需要运行库（`libusb-1.0-0`）。
- **udev 规则**：Linux 的设备管理服务。USB 设备默认只有 root 可读写；udev 规则可以按"厂商 ID + 产品 ID"把某类设备的权限放开给所有用户（`MODE:="0666"`）。
- **Touchstone 文件（.s2p/.s1p）**：射频领域的标准测量数据交换格式，存的是每个频点上的 S 参数复数值。`.s2p` 表示双端口（含 S11/S21/S12/S22 四条曲线）。第 8 单元会精读它的解析代码，本讲只需把它当作"可以导入 GUI 的数据文件"。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Documentation/DeveloperInfo/BuildAndFlash.md](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/BuildAndFlash.md) | 官方构建说明：PC 应用、MCU 固件、FPGA bitstream 三部分的工具链 |
| [Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro) | GUI 的 qmake 工程文件，本讲的"文件索引"主角 |
| [Software/PC_Application/LibreVNA-GUI/main.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp) | 整个 GUI 的 `main()` 入口，只有 34 行 |
| [README.md](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md) | 发布版安装说明（含 Ubuntu 的 udev 规则安装步骤） |
| [Software/PC_Application/51-vna.rules](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/51-vna.rules) | udev 规则文件本体，共 3 行 |
| [.github/workflows/Build.yml](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/.github/workflows/Build.yml) | 官方 CI 构建脚本——一份"保证能编译通过"的权威依赖清单 |
| [Documentation/Measurements/](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/Measurements/Measurements.md) | 示例测量（Touchstone `.s2p` 文件与截图），无硬件验证的数据来源 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：①依赖安装与编译流程；②`.pro` 工程结构；③`main.cpp` 启动入口；④无硬件运行与 udev 规则。

### 4.1 依赖安装与 qmake6/make 编译流程

#### 4.1.1 概念说明

编译一个 Qt 应用的前置条件只有两类：**编译工具链**（编译器 + qmake + make）和**开发库**（Qt 头文件、libusb 头文件）。LibreVNA GUI 的特殊之处在于它除了 Qt 之外只依赖一个外部库 libusb——其余所有功能（JSON 解析、数学表达式解析、zip 压缩）都以源码形式直接 vendored 在工程里（如 `json.hpp`、`Traces/Math/parser/`、`Util/QMicroz/`），所以依赖清单非常短。

#### 4.1.2 核心流程

编译的整体流程是三条命令：

```text
安装依赖（一次性）:
  sudo apt-get install libusb-1.0-0-dev qt6-tools-dev qt6-base-dev
                          (+ libqt6svg6-dev libgl-dev)

编译（每次改代码后）:
  cd Software/PC_Application/LibreVNA-GUI
  qmake6            # 读 LibreVNA-GUI.pro，生成 Makefile
  make -j9          # 并行编译，产出可执行文件 LibreVNA-GUI
```

`qmake6` 与 `make` 的分工：

1. `qmake6` 解析 `.pro` 文件，把"源码清单 + 库 + Qt 模块"翻译成平台相关的 Makefile；
2. `make` 按 Makefile 依次调用编译器和链接器；
3. 链接阶段把 `-lusb-1.0` 指定的 libusb 动态库接进来；
4. 最终在工程目录下得到可执行文件 `LibreVNA-GUI`（Windows 上在 `release/` 子目录）。

#### 4.1.3 源码精读

官方构建说明写明了依赖与命令，见 [Documentation/DeveloperInfo/BuildAndFlash.md:L12-L26](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/BuildAndFlash.md#L12-L26)：这一段给出了 `apt-get install` 依赖清单，以及"命令行 qmake6/make"和"Qt Creator 打开 .pro 文件"两条等价路线。

注意文档开头还标注了最低 Qt 版本要求（Qt 5.9 起、推荐 Qt6），见 [Documentation/DeveloperInfo/BuildAndFlash.md:L4-L6](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/BuildAndFlash.md#L4-L6)。

比文档更权威的是 CI 脚本——它在每次提交时都被强制执行，依赖列表不会过期。[.github/workflows/Build.yml:L17-L21](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/.github/workflows/Build.yml#L17-L21) 是 Ubuntu 任务安装依赖的步骤，可以看到比 BuildAndFlash.md 多了 `libqt6svg6-dev`（SVG 模块开发包）和 `libgl-dev`（OpenGL 链接需要）；[.github/workflows/Build.yml:L36-L42](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/.github/workflows/Build.yml#L36-L42) 则是实际编译命令：设置 `QT_SELECT=qt6` 后 `qmake LibreVNA-GUI.pro` 再 `make -j9`。

同一份 Build.yml 还证明了跨平台可编译性：Windows 任务（[.github/workflows/Build.yml:L90-L131](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/.github/workflows/Build.yml#L90-L131)）用 MinGW 版 Qt 6.8.3 并手动下载 libusb 1.0.25 静态库；macOS 任务（[.github/workflows/Build.yml:L177-L184](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/.github/workflows/Build.yml#L177-L184)）用 `brew install qt@6` 后 `make` 并 `macdeployqt` 打包。

#### 4.1.4 代码实践

1. **实践目标**：在本机从源码编译出 GUI 可执行文件。
2. **操作步骤**（Ubuntu/Debian 系；其他平台对照 Build.yml 对应任务）：
   1. `sudo apt-get install -y libusb-1.0-0-dev qt6-tools-dev qt6-base-dev libqt6svg6-dev libgl-dev`
   2. `cd Software/PC_Application/LibreVNA-GUI`
   3. `qmake6`
   4. `make -j$(nproc)`（首次全量编译需要数分钟，源码约 300 个文件）
   5. `ls -lh LibreVNA-GUI` 确认产物存在。
3. **需要观察的现象**：`qmake6` 之后目录里多出 `Makefile`；`make` 结尾有链接命令，最后出现可执行文件。
4. **预期结果**：得到一个大小约几十 MB 的 `LibreVNA-GUI` 可执行文件；暂时不急着运行，下一模块先读懂工程文件。（编译输出随环境不同，具体报错信息待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么文档说只需要 `qt6-tools-dev qt6-base-dev` 两个 Qt 包，CI 却额外装了 `libqt6svg6-dev`？

**答案**：`.pro` 文件里声明了 `QT += widgets network svg`（见 4.2.3），其中 `svg` 模块的头文件和库在 `qt6-base-dev` 之外，属于独立的 `libqt6svg6-dev` 包。缺了它，编译用到 SVG 图标的源文件时会报"找不到 QSvgRenderer 头文件"之类的错误。

**练习 2**：如果不小心只安装了运行库 `libusb-1.0-0` 而没装开发包 `libusb-1.0-0-dev`，编译会在哪个阶段失败？

**答案**：在 `make` 阶段失败（`qmake6` 仍能成功，因为 `.pro` 里只写了 `-lusb-1.0` 链接选项）。典型报错是链接器找不到 `-lusb-1.0`，或编译包含 `libusb.h` 的源文件（如 `librevnausbdriver.cpp`）时找不到头文件——开发包提供的正是头文件和 `.so` 符号链接。

### 4.2 LibreVNA-GUI.pro 工程结构

#### 4.2.1 概念说明

`.pro` 文件是这个工程的"单一事实来源"：新增一个 `.cpp` 文件必须同时登记进 `SOURCES`，新增 `.h` 登记进 `HEADERS`，新增 Qt 设计师对话框登记进 `FORMS`。上一讲说过它是"活的文件索引"，本节正式拆解它的五个区块，并关注两个容易被忽略的细节：**跨子树引用固件协议文件**和**编译期版本注入**。

#### 4.2.2 核心流程

qmake 变量的语义一览：

| 变量 | 含义 | 本工程中的规模 |
| --- | --- | --- |
| `HEADERS` | 头文件清单（供 moc 与 IDE 索引） | 约 170 项 |
| `SOURCES` | 源文件清单（参与编译链接） | 约 160 项 |
| `FORMS` | Qt 设计师 `.ui` 界面文件（由 uic 转成 `ui_*.h`） | 约 75 项 |
| `RESOURCES` | `.qrc` 资源包（图标等，编进可执行文件） | 2 项 |
| `LIBS` / `QT` | 外部库与 Qt 模块 | libusb-1.0；widgets/network/svg |

特殊的编译期注入流程：

```text
git rev-parse HEAD ──(qmake $$system())──> REVISION ──> 宏 GITHASH
FW_MAJOR/MINOR/PATCH/SUFFIX（写死在 .pro）──────────> 宏 FW_*
        │
        └─> appwindow.cpp 顶部拼成 APP_VERSION / APP_GIT_HASH
             └─> 窗口标题、SCPI *IDN? 查询、--version 输出
```

#### 4.2.3 源码精读

**① 跨子树引用固件协议文件**——这是上一讲"协议两端定义同源"的落地处。[Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro:L1-L3](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L1-L3) 把固件子树的 `Protocol.hpp`/`PacketConstants.h` 用相对路径 `../../VNA_embedded/...` 登记进 GUI 的头文件清单；[Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro:L174-L175](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L174-L175) 同样把 `Protocol.cpp` 编进 GUI。也就是说 GUI 和固件编译的是**同一份** USB 协议定义，从机制上杜绝了两端协议漂移。

**② 外部库与平台分支**：[Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro:L332-L337](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L332-L337) 声明 `LIBS += -lusb-1.0`（所有平台通用），随后 `win32:`/`osx:` 前缀给出平台特定的库搜索路径；[Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro:L339-L343](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L339-L343) 在 mac 平台改用 pkg-config 查找 libusb。

**③ Qt 模块**：[Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro:L345](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L345) 一行 `QT += widgets network svg` 说明全部 Qt 依赖：widgets 是界面主体，network 支撑第 10 单元的 SCPI TCP 服务器，svg 用于渲染矢量图标。

**④ 资源文件**：[Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro:L426-L431](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L426-L431) 把 `icons.qrc` 和 `resources/librevna.qrc` 编进二进制，并为 Windows/macOS 指定应用图标。

**⑤ 编译期版本注入**：[Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro:L433-L439](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L433-L439) 是全文件最值得读的五行：`CONFIG += c++17` 指定 C++ 标准；`REVISION = $$system(git rev-parse HEAD)` 在 qmake 阶段执行 shell 命令读取当前 commit 哈希；`DEFINES += GITHASH=\\"\"$$REVISION\\"\\"` 把它变成字符串宏；`FW_MAJOR=1 FW_MINOR=6 FW_PATCH=5` 是手写的应用版本号（1.6.5）。这些宏在 [Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L61-L64](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L61-L64) 被拼成 `APP_VERSION` 与 `APP_GIT_HASH` 两个静态字符串，供窗口标题和版本查询使用。**推论**：换一个 commit 重新 `qmake6` 才会刷新 GITHASH——只 `make` 不重跑 qmake 时，旧哈希可能残留。

#### 4.2.4 代码实践

1. **实践目标**：验证 `.pro` 是"单一事实来源"，并亲手确认协议文件同源机制。
2. **操作步骤**：
   1. 在 `Software/PC_Application/LibreVNA-GUI/` 下新建空文件 `test.cpp`（内容随意，如 `int test(){return 0;}`），**不要**改 `.pro`，重新 `make`，观察它是否被编译；
   2. 把 `test.cpp` 追加到 `SOURCES` 末尾，重新 `qmake6 && make`，观察区别；
   3. 做完后删除 `test.cpp` 并还原 `.pro`（本手册禁止修改源码，这只是临时实验，务必还原）；
   4. 打开固件子树的 `Software/VNA_embedded/Application/Communication/Protocol.hpp` 看一眼，再用文本搜索确认 GUI 侧驱动代码 `#include "Protocol.hpp"` 时用的正是这份共享文件。
3. **需要观察的现象**：步骤 2 中未登记的文件被构建系统完全忽略；登记后参与编译。
4. **预期结果**：理解"新增源文件必须改 .pro"；确认 GUI 与固件共用一份 `Protocol.hpp`。若你做了步骤 1/2，`git status` 应显示工作区干净（已还原）。

#### 4.2.5 小练习与答案

**练习 1**：假设你想给 GUI 新增一个"关于"对话框类 `about2.cpp/about2.h/about2dialog.ui`，需要在 `.pro` 的哪几个变量里各加一行？

**答案**：`about2.cpp` 加入 `SOURCES`，`about2.h` 加入 `HEADERS`，`about2dialog.ui` 加入 `FORMS`。参考现有 `about.cpp/about.h/aboutdialog.ui` 在清单中的登记位置（[Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro:L315-L320](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L315-L320) 与 [Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro:L420](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L420)）。

**练习 2**：`REVISION = $$system(git rev-parse HEAD)` 在什么时刻执行？如果在一个打了 zip 包、没有 `.git` 目录的源码树上编译会发生什么？

**答案**：在 **qmake 生成 Makefile 的时刻**执行（不是 make 编译时刻），结果被写死进 Makefile 的 `DEFINES`。在没有 `.git` 的目录里 `git rev-parse HEAD` 会失败，`REVISION` 为空，GITHASH 宏变成空字符串——程序仍能编译运行，但"关于"窗口/版本查询里将看不到 commit 哈希。（空值的具体表现待本地验证。）

### 4.3 main.cpp：QApplication 的启动入口

#### 4.3.1 概念说明

Qt 应用的人口惯例是：创建 `QApplication` 对象 → 创建主窗口 → 进入事件循环。LibreVNA 的 `main.cpp` 刻意保持极简（34 行），把所有实质工作（菜单、工具栏、设备连接、SCPI、模式切换）都推迟到 `AppWindow` 构造函数里——那是下一讲（u2-l1）的主菜。本节只看这 34 行如何完成"点火"。

#### 4.3.2 核心流程

```text
main()
 ├─ qSetMessagePattern(...)          # 统一 qDebug 等日志的输出格式
 ├─ app = new QApplication(...)      # Qt 应用对象（必须最先创建）
 ├─ 设置组织名/应用名                 # 决定 QSettings 的存储位置
 ├─ window = new AppWindow           # ★ 全部初始化都在这个构造函数里
 │    └─（内部：解析命令行、搭菜单、创建模式、起 SCPI/TCP...）
 ├─ setApplicationVersion(版本+git哈希)
 ├─ signal(SIGINT, tryExitGracefully) # 仅 Unix：Ctrl+C 也能优雅退出
 └─ app->exec()                       # 进入事件循环，直到 quit()
```

#### 4.3.3 源码精读

完整入口见 [Software/PC_Application/LibreVNA-GUI/main.cpp:L18-L34](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp#L18-L34)。逐段拆解：

- [Software/PC_Application/LibreVNA-GUI/main.cpp:L20](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp#L20)：`qSetMessagePattern` 让所有 qDebug/qWarning 带上进程时间和级别前缀，编译后第一次运行时看日志会非常直观。
- [Software/PC_Application/LibreVNA-GUI/main.cpp:L22-L24](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp#L22-L24)：创建 `QApplication` 并设置组织名/应用名——这两个字符串决定 `QSettings`（全局偏好设置）在磁盘上的落点，第 2 单元讲 Preferences 时会再遇到。
- [Software/PC_Application/LibreVNA-GUI/main.cpp:L25-L27](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp#L25-L27)：`new AppWindow` 一行触发了整个应用的初始化；随后把 4.2 节讲的 `getAppVersion()`/`getAppGitHash()`（定义为 [Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L1278-L1286](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1278-L1286)）拼成应用版本字符串。
- [Software/PC_Application/LibreVNA-GUI/main.cpp:L10-L16](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp#L10-L16) 与 [Software/PC_Application/LibreVNA-GUI/main.cpp:L29-L31](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp#L29-L31)：Unix 下注册 SIGINT 处理器，让终端里 Ctrl+C 也能走 `window->close()` + `app->quit()` 的干净退出路径，而不是直接杀进程——这对 `--no-gui` 无头运行尤其重要。

提前剧透一点 u2-l1 的内容以便你定位：命令行参数（`--port`、`--no-gui`、`--setup`、`--cal` 等）并不是在 main.cpp 解析的，而是在 `AppWindow` 构造函数里用 `QCommandLineParser` 完成的，见 [Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L87-L97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L87-L97)。

#### 4.3.4 代码实践

1. **实践目标**：运行你编译出的 GUI，验证启动链路与版本注入。
2. **操作步骤**：
   1. 在工程目录执行 `./LibreVNA-GUI`（无硬件也能启动）；
   2. 观察窗口标题——它由 [Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L178](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L178) 设置为应用名 + `v` + 版本号；
   3. 对照 `.pro` 里的 `FW_MAJOR=1 FW_MINOR=6 FW_PATCH=5` 与 `git rev-parse HEAD` 的前 9 位，核对标题/关于对话框显示的版本与哈希是否一致；
   4. 在终端按 Ctrl+C（需从终端启动），观察程序是否优雅退出。
3. **需要观察的现象**：窗口正常出现（可能弹出"无设备连接"之类的提示，属正常）；日志按 `时间: [级别] 消息` 格式打印。
4. **预期结果**：版本字符串 = `1.6.5-<HEAD 前 9 位>`；无硬件时 GUI 不崩溃，只是设备列表为空。（各平台对话框细节待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `QApplication` 必须在创建任何窗口部件之前创建？

**答案**：`QApplication` 负责初始化 Qt 的 GUI 子系统（事件循环、字体、剪贴板、窗口系统连接等）。Qt 官方约定任何 `QWidget` 都必须存在于 `QApplication` 创建之后，否则行为未定义。这也是 main.cpp 把 `new QApplication` 放在 `new AppWindow` 之前的原因。

**练习 2**：`app->exec()` 返回后程序就结束了。那么用户点窗口右上角的 × 时，是谁让 `exec()` 返回的？

**答案**：关闭最后一个窗口会使 Qt 停止事件循环（默认 `quitOnLastWindowClosed` 行为），`exec()` 随即返回非零状态码，main 返回。而 Ctrl+C 路径则是 `tryExitGracefully` 主动调用 `app->quit()` 达到同样效果。

### 4.4 无硬件运行与 udev 规则

#### 4.4.1 概念说明

README 明确承诺：GUI 可以脱离硬件使用——"PCB 只是射频前端"，校准、绘图、数学运算全在 PC 端，所以导入示例测量后整套显示/分析功能都能离线体验。而当你将来真的接上设备时，Linux 上还差最后一步：**权限**。USB 设备节点默认属 root，普通用户的 GUI 打不开它，udev 规则就是解决这个问题的官方方案。

#### 4.4.2 核心流程

无硬件验证路径：

```text
启动 GUI → 确认处于 VNA 模式 → 菜单 File > Import > Touchstone/CSV
  → 选择 Documentation/Measurements/*.s2p → 勾选要导入的 S 参数
  → Trace 出现 → 右键图区新建 Smith 图 → 查看曲线
```

导入菜单的动态生成逻辑：`AppWindow::UpdateImportExportMenus()` 先清空菜单，再向**当前激活模式**索要它的导入/导出动作列表——所以切换模式后 Import 菜单内容会跟着变（VNA 模式提供 Touchstone/CSV 导入，频谱模式提供自己的选项）。

有硬件时的权限路径：

```text
插入设备 → 内核创建 /dev/bus/usb/... 节点（默认仅 root 可写）
  → udev 读取 /etc/udev/rules.d/51-vna.rules
  → 匹配 idVendor/idProduct → MODE:="0666"（所有用户可读写）
  → GUI 的 libusb 枚举才能成功
```

#### 4.4.3 源码精读

- README 的"无硬件体验"承诺见 [README.md:L67-L68](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L67-L68)："You can try out the application without the PCB... you can import provided example measurements"。示例测量的索引页在 [README.md:L65](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L65) 指向 `Documentation/Measurements/`，那里有 4 个 `.s2p` 双端口测量（隔离度、两只 Mini-Circuits 衰减器、Murata 403 MHz 带通滤波器），见 [Documentation/Measurements/Measurements.md:L14-L30](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/Measurements/Measurements.md#L14-L30)。
- 导入动作的注册处：[Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp:L627-L629](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L627-L629) 创建名为 **"Touchstone/CSV"** 的菜单动作并连接到导入对话框。
- 菜单动态装配：[Software/PC_Application/LibreVNA-GUI/appwindow.cpp:L1170-L1189](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1170-L1189) 每次模式切换都重建 Import/Export 菜单——这段代码解释了"为什么菜单内容随模式变化"。
- 文件解析入口：[Software/PC_Application/LibreVNA-GUI/Traces/tracewidget.cpp:L248-L256](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewidget.cpp#L248-L256) 按扩展名分流：`.csv` 走 `CSV::fromFile`，否则按 Touchstone 处理，调用 `Touchstone::fromFile` 并由 `Trace::createFromTouchstone` 生成一组 Trace。导入完成后若已有校准/去嵌入数据，还会弹出选项询问是否应用到导入数据（[Software/PC_Application/LibreVNA-GUI/Traces/tracewidget.cpp:L271-L297](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewidget.cpp#L271-L297)）。
- udev 规则本体：[Software/PC_Application/51-vna.rules:L1-L3](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/51-vna.rules#L1-L3) 共三行，按 USB `idVendor:idProduct` 匹配三组 ID 并设 `MODE:="0666"`：`0483:564e`（0483 是 STMicroelectronics，正常运行的 LibreVNA）、`0483:4121` 与 `1209:4121`（4121 是固件升级阶段使用的 USB ID，1209 是 pidcode 开源 USB 厂商号）。
- 安装步骤见 [README.md:L26-L35](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L26-L35)：下载规则文件复制到 `/etc/udev/rules.d`，再 `udevadm control --reload-rules` + `udevadm trigger` 生效。

#### 4.4.4 代码实践

1. **实践目标**：不接任何硬件，用示例测量完整走一遍"导入 → 显示"链路，证明 GUI 可用。
2. **操作步骤**：
   1. 启动 `./LibreVNA-GUI`，确认顶部模式选择停在 **VNA**；
   2. 菜单 **File > Import > Touchstone/CSV**，选择 `Documentation/Measurements/Murata_RF1419D.s2p`（403 MHz 带通滤波器测量）；
   3. 在导入对话框勾选 S11 与 S21（也可以全选四个）；
   4. 在绘图区新建/切换一个 **Smith 图**（Trace 波形区右键或图表菜单），选中 S11；再新建一个 **XY 图**显示 S21 的 dB 曲线；
   5. 对照 [Documentation/Measurements/Murata_RF1419D.png](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/Measurements/Murata_RF1419D.png) 官方截图，看通带位置是否一致。
3. **需要观察的现象**：导入后左侧 Trace 列表出现带 `Murata_RF1419D_` 前缀的曲线；Smith 图上 S11 在 403 MHz 附近绕近圆心（插损小），远离通带则贴单位圆（全反射）；S21 的 dB 曲线在 403 MHz 出现通带凸起。
4. **预期结果**：与官方截图的趋势一致，说明编译出的 GUI 数据管线（解析 → Trace → 绘图）工作正常。Smith 图上的具体轨迹随窗函数/设置略有差异，属正常。（截图请自行保存，本手册无法替你运行。）

#### 4.4.5 小练习与答案

**练习 1**：为什么导入菜单在频谱分析仪模式下和 VNA 模式下内容不同？请用一行代码说明原因。

**答案**：因为 Import 菜单是每次模式切换时由当前激活模式动态提供的，[appwindow.cpp:L1179](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1179) 的 `for(auto a : active->getImportOptions())` 向当前模式索要动作列表；VNA 模式在 [vna.cpp:L627-L629](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L627-L629) 注册了 "Touchstone/CSV"，频谱模式注册的是自己的动作。

**练习 2**：udev 规则里为什么有三个 USB ID？删掉 `0483:4121` 和 `1209:4121` 两行会有什么后果？

**答案**：设备在**固件升级**前后使用不同的 USB ID（升级阶段固件以另一种枚举身份出现，`4121`；`1209` 是开源项目常用的 pidcode 厂商号，`0483` 是 ST）。删掉这两行后，正常测量仍可用，但 GUI 执行固件升级时将因无权限访问升级态的 USB 设备而失败——所以规则把三种身份都放开了。

**练习 3**：不用 udev 规则，还有什么临时办法让 GUI 访问设备？各有什么缺点？

**答案**：用 `sudo` 运行 GUI（缺点：以 root 跑图形程序有安全风险，且环境变量/显示转发常出问题）；或手动 `chmod 666 /dev/bus/usb/<bus>/<dev>`（缺点：重新插拔后失效，设备号会变）。udev 规则是唯一"一次配置、永久生效"的方案。

## 5. 综合实践

**任务：从零构建 + 离线复现一次官方测量。**

1. 按模块 4.1 安装依赖并编译，记录 `make` 最后的链接命令行（截图或复制文本）。
2. 运行 `./LibreVNA-GUI`，记录窗口标题中的版本号与 git 哈希，用 `git rev-parse HEAD` 核对（模块 4.3）。
3. 导入 `Documentation/Measurements/Mini-circuits_VAT-10+.s2p`（10 dB 衰减器测量），显示 S11 与 S21。
4. 验证物理合理性：S21 应在全部频段接近 −10 dB（衰减器的标称值），S11 应低于约 −20 dB（良好匹配）。对照官方截图 [Documentation/Measurements/Mini-circuits_VAT-10+.png](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/Measurements/Mini-circuits_VAT-10+.png)。
5. 写一段 200 字以内的记录：编译平台、Qt 版本、遇到的报错（如有）、以及"为什么无硬件也能验证 GUI 大部分功能"（提示：上一讲的架构结论）。

这个任务串起本讲全部四个模块：依赖安装、工程文件、启动入口、无硬件运行。完成后你就拥有了一个可反复实验的 GUI 开发环境——后续所有改代码、加日志的实践都基于它。

## 6. 本讲小结

- 编译 GUI 只需两类依赖：Qt6（含 svg 模块）与 libusb 开发包；`qmake6` + `make -j` 两条命令完成构建，CI 的 [Build.yml](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/.github/workflows/Build.yml) 是最权威的依赖清单。
- `LibreVNA-GUI.pro` 是工程的单一事实来源：`HEADERS/SOURCES/FORMS/RESOURCES` 四张清单 + `LIBS`/`QT` 两项外部依赖；新增文件必须登记，否则构建系统视而不见。
- `.pro` 用相对路径 `../../VNA_embedded/...` 把固件侧的 `Protocol.hpp/Protocol.cpp` 直接编进 GUI，实现 USB 协议两端定义同源；版本号（1.6.5）与 git 哈希在 qmake 阶段注入宏，最终显示在窗口标题里。
- `main.cpp` 只有 34 行：创建 `QApplication` → 创建 `AppWindow`（真正的初始化都在它的构造函数里）→ 进入事件循环；Unix 下还注册了 Ctrl+C 的优雅退出。
- GUI 完全可以无硬件运行：`File > Import > Touchstone/CSV` 导入 `Documentation/Measurements/` 下的示例测量即可体验绘图与分析；导入菜单内容由当前模式动态提供。
- 将来接真设备时，Linux 需要 [51-vna.rules](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/51-vna.rules) udev 规则放开 USB 设备权限，规则覆盖正常态与固件升级态共三组 USB ID。

## 7. 下一步学习建议

- **下一讲（u2-l1）**：钻进 `AppWindow` 构造函数，看菜单/工具栏如何搭建、`QCommandLineParser` 支持哪些选项（包括 `--no-gui` 无头模式）、设备连接的初始状态——这是理解 GUI 一切行为的地图。
- **再往后（u2-l2/u2-l3）**：Mode 系统与本讲"导入菜单随模式变化"的现象直接呼应；Savable/Preferences 会解释 main.cpp 里设置组织名/应用名的意义。
- **源码预读**：有余力可以打开 `appwindow.h` 浏览一遍成员声明，建立"这个类管了哪些东西"的粗印象，不必逐行理解。
- 若你的目标是改协议或写驱动：记住本讲的结论——改 `Protocol.hpp` 一处，GUI 与固件两端同时生效，但要分别重新编译两个工程。
