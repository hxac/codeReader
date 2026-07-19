# GUI V7 架构与启动

## 1. 本讲目标

AERIS-10 雷达的上位机程序是一套用 PyQt6 写的桌面 GUI，代号 **V7**。前面几讲我们一直在讲 FPGA 与 STM32——它们是雷达的「手和眼」，而 GUI 是「脸和脑」：它把 FPGA 送上来的 Range-Doppler 数据画成图、把检测点聚成目标、在地图上标出航迹，还要让操作员能用旋钮调节 FPGA 的每一个寄存器。

本讲不深入任何算法，只解决三个「读代码前的地图」问题。学完之后你应当能够：

1. 说出 V7 GUI 的 `v7` 包由哪 9 个模块组成、每个模块一句话职责是什么，以及它们之间如何分层；
2. 说出主窗口 `RadarDashboard` 的六个标签页各负责什么，并能从源码里定位每个标签页的构建函数；
3. 解释 `models.py` 里数据类、枚举与「可选依赖标志」的作用，并能说清当 `scipy` / `sklearn` 等可选库未安装时，GUI 是如何「优雅降级」而不是崩溃的。

本讲对应的最小模块有三个：**包结构**、**六标签页**、**优雅降级**。

## 2. 前置知识

在开始之前，你需要已经具备以下认知（这些是前面几讲建立的，本讲直接承接，不再重复）：

- **三层固件分工（u2-l3）**：FPGA 做逐样本的高速信号处理，STM32 做电源/时钟/外设管理，GUI 在 PC 上做可视化、聚类、跟踪等重浮点计算。本讲讲的正是「GUI 这一层」的内部结构。
- **关键入口文件（u1-l3）**：已经知道 `GUI_V7_PyQt.py` 是一个「薄启动器」，真正的实现都在 `v7` 包里；`v7/dashboard.py` 才是主窗口。本讲会把这层关系彻底展开。
- **主机命令协议（u6-l2）**：GUI 与 FPGA 之间的硬契约是 4 字节命令 `{opcode, addr, value_hi, value_lo}` 与 26 字节状态包（`0xBB` 头 + 6 个 32 位字 + `0x55` 尾），`Opcode` 枚举与 Verilog 的 `case` 表逐项对应。本讲会看到 GUI 在「FPGA Control」标签页里如何把这些 opcode 暴露成可点的按钮和旋钮。
- **工具链与本地运行（u1-l4）**：GUI 用 Python 3.12 + uv 管理依赖，依赖清单写在 `requirements_v7.txt`。本讲会解释为什么这份清单把依赖分成「必需」和「可选」两组。

如果你对「数据类（dataclass）」「枚举（Enum）」「后台线程（QThread）」「信号槽（signal/slot）」这些 Python/Qt 概念完全陌生，建议先花十分钟了解它们的含义——本讲会在用到时给出最简短的解释，但不会从零讲 Python 语法。

## 3. 本讲源码地图

本讲涉及的关键文件，按它们在分层中的位置排列：

| 文件 | 所处层 | 一句话作用 |
|------|--------|-----------|
| [`GUI_V7_PyQt.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/GUI_V7_PyQt.py) | 启动器 | 薄入口：建 `QApplication`、显示 `RadarDashboard`，本身不含业务逻辑。 |
| [`v7/__init__.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/__init__.py) | 包门面 | 把各子模块的公开类/函数集中转出口，并用 `try/except ImportError` 实现分层加载。 |
| [`v7/models.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/models.py) | 数据层 | 数据类、`TileServer` 枚举、深色主题颜色常量、5 个可选依赖可用性标志。 |
| [`v7/hardware.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/hardware.py) | 硬件层 | 封装 STM32 USB CDC（仅 GPS），并从 `radar_protocol.py` 转出口 FT2232H/FT601 连接类。 |
| [`v7/processing.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py) | 处理层 | `RadarProcessor`：双 CPI 融合、多 PRF 解模糊、DBSCAN 聚类、Kalman 跟踪。 |
| [`v7/workers.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/workers.py) | 线程层 | QThread 后台线程（数据采集 / GPS / 演示），用 PyQt 信号把结果送回主线程。 |
| [`v7/map_widget.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/map_widget.py) | 地图层 | 用 QWebEngineView 内嵌 Leaflet.js 地图。 |
| [`v7/replay.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/replay.py) | 回放层 | `ReplayEngine` 自动识别三种数据源并逐帧产出 `RadarFrame`。 |
| [`v7/software_fpga.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/software_fpga.py) | 软件镜像 | 比特精确复刻 FPGA 信号链，供回放/离线分析。 |
| [`v7/agc_sim.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/agc_sim.py) | 离线分析 | 与 `rx_gain_control.v` 逐位一致的 AGC 仿真，独立脚本使用，不在 `__init__` 导出。 |
| [`v7/dashboard.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py) | 表现层 | `RadarDashboard(QMainWindow)` 主窗口，六标签页，把上述模块接线成完整 GUI。 |
| [`requirements_v7.txt`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/requirements_v7.txt) | 依赖清单 | 把依赖分成 Core（必需）与多组 optional（可选）。 |

> 说明：本讲引用的所有行号基于当前 HEAD `749bd0f`。`v7/` 目录下还有别的辅助脚本（如 `__init__.py` 本身），但「九大功能模块」就是上表中 `models / hardware / processing / workers / map_widget / replay / software_fpga / agc_sim / dashboard` 这 9 个。

---

## 4. 核心概念与源码讲解

### 4.1 包结构：薄启动器、九大模块与分层

#### 4.1.1 概念说明

一个成熟的桌面 GUI 代码量往往很大，如果全塞在一个文件里会无法维护。V7 采用的是 Python 标准的「**包（package）**」组织方式：把功能按职责拆成多个 `.py` 模块文件，放进同一个 `v7/` 目录，目录里的 `__init__.py` 充当「门面」统一对外转出口。

这套设计有两个关键点：

- **薄启动器 + 包实现**：用户运行的 `GUI_V7_PyQt.py` 极短，只负责创建 Qt 应用程序对象并显示主窗口；所有真正的逻辑都在 `v7` 包内部。好处是「入口」与「实现」分离，便于测试（测试可以直接 `from v7 import RadarDashboard`，也可以只测不含 Qt 的纯算法模块）。
- **分层依赖**：模块之间有清晰的依赖方向——数据层（`models`）在最底，被所有人依赖；硬件层、处理层依赖数据层；线程层依赖硬件层和处理层；表现层（`dashboard`）在最顶，依赖前面所有层。依赖只能「从上往下」，不能反过来，否则会循环依赖。

#### 4.1.2 核心流程

启动一个 GUI 程序的调用链是这样的：

```
python GUI_V7_PyQt.py
   └─ main()                                  # 启动器
       └─ QApplication(argv)                  # 创建 Qt 事件循环
       └─ from v7 import RadarDashboard       # 触发 v7/__init__.py 执行
           └─ v7/__init__.py 逐个 import 子模块，转出口公开符号
       └─ RadarDashboard()                    # 构造主窗口（见 4.2）
       └─ window.show(); app.exec()           # 进入事件循环
```

注意第三步：`from v7 import RadarDashboard` 这一句会**首次**执行 `v7/__init__.py`。该文件在执行时，会按依赖顺序逐个 import 子模块——如果某个可选模块（依赖 PyQt6 或 golden_reference）不存在，就用 `try/except ImportError` 静默跳过，不影响其它模块。这就是后面 4.3 要讲的「优雅降级」在包加载阶段的体现。

#### 4.1.3 源码精读

**启动器极短，只有 40 行**——这是「薄入口」的最佳证据。[GUI_V7_PyQt.py:26-36] 中 `main()` 做的三件事一目了然：

[GUI_V7_PyQt.py:26-36](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/GUI_V7_PyQt.py#L26-L36) — 启动器 `main()`：建 `QApplication`、设字体、`RadarDashboard()` 构造主窗口、`show()` 后进入 `app.exec()` 事件循环。没有任何业务逻辑，全部委托给 `v7` 包。

**`v7/__init__.py` 是包的门面与分层加载器**。它先无风险地转出口数据层与硬件层，再对依赖较重的模块用 `try/except` 包裹：

[v7/__init__.py:48-80](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/__init__.py#L48-L80) — 这段是分层加载的核心：`software_fpga`、`replay`、以及 `workers / map_widget / dashboard` 三个 PyQt6 模块各自被 `try/except ImportError` 包裹。`__init__.py` 的注释点明了原因——「tests/CI environments without PyQt6 can still access models/hardware/processing」（没有装 PyQt6 的 CI 环境仍能使用数据/硬件/处理层）。这意味着在没有图形界面的 CI 服务器上，`import v7` 不会因为缺 PyQt6 而失败，纯算法模块照样可测。

**`models.py` 是整个包共享的数据层**。它定义了贯穿 GUI 各模块的数据结构，最重要的几个数据类（`@dataclass`）：

[v7/models.py:79-130](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/models.py#L79-L130) — 三个核心数据类：`RadarTarget`（一个检测到的目标，含距离/速度/方位/俯仰/SNR/航迹号）、`RadarSettings`（显示与物理单位换算配置）、`GPSData`（GPS 位置与姿态）。它们用 `dataclass` 而非普通类，是为了免费获得构造、相等比较、`asdict()` 序列化等能力——`RadarTarget.to_dict()` 就直接用 `asdict(self)` 把目标转成字典供 JSON 序列化。

> 数据类（`@dataclass`）是 Python 的语法糖：你只声明字段（如 `range: float`），解释器自动帮你生成 `__init__`、`__repr__` 等方法。`RadarTarget` 里 `latitude: float = 0.0` 这种带默认值的字段可以省略传参。

除了数据类，`models.py` 还放了两个枚举/配置：

[v7/models.py:182-188](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/models.py#L182-L188) — `TileServer(Enum)`：地图瓦片服务器枚举（OSM / Google / Google Satellite / ESRI 等）。用枚举而不是字符串常量，是为了让「选择地图源」只能是这几个合法值，拼写错误在编译期就能暴露。

[v7/models.py:195-251](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/models.py#L195-L251) — `WaveformConfig`：把雷达波形物理参数（采样率 100 MSPS、带宽 20 MHz、长 chirp 30 µs、PRI 167 µs、载频 10.5 GHz）封装起来，并用 `@property` 自动推导 `range_resolution_m`、`velocity_resolution_mps`、`max_range_m` 等分辨率。这让「bin → 物理单位」的换算集中在一处，而不是散落在 GUI 各处硬编码。

把数据类、枚举、主题常量都集中在 `models.py`，是为了让所有模块共享同一份「语言」——`dashboard` 创建 `RadarTarget`，`processing` 聚类后产出 `RadarTarget` 列表，`map_widget` 接收 `RadarTarget` 列表画点，全程不需要重复定义或转换数据结构。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你亲手把九大模块的职责梳理一遍。

1. **实践目标**：列出 `v7/` 包的 9 个功能模块，为每个写一句话职责。
2. **操作步骤**：
   - 打开 [`v7/__init__.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/__init__.py)，看它在从哪些模块转出口哪些符号——这能告诉你模块间的依赖关系。
   - 依次打开 `models.py / hardware.py / processing.py / workers.py / map_widget.py / replay.py / software_fpga.py / agc_sim.py / dashboard.py`，**只读每个文件最开头的模块 docstring**（即三引号注释块），那里通常一句话写明了模块职责。
   - 把结果填进一张「模块名 → 一句话职责 → 依赖谁」的三列表。
3. **需要观察的现象**：你会发现依赖方向是单向的——`models` 谁都不依赖（除了标准库），`dashboard` 依赖几乎所有其它模块；`agc_sim.py` 比较特殊，它**没有**出现在 `__init__.py` 的转出口列表里。
4. **预期结果**：参考答案见本讲 4.1.5。`agc_sim` 的特殊性在于它是一个独立离线分析工具（被 `adi_agc_analysis.py` 这类脚本直接 `import`），不属于 GUI 运行时主链路，所以不进 `__init__`。
5. **待本地验证**：如果你想确认 `agc_sim` 的使用方，可以在仓库里搜索 `agc_sim` 关键字（本讲写作时确认它仅被 `9_Firmware/9_3_GUI/adi_agc_analysis.py` 引用）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `GUI_V7_PyQt.py` 要写成一个 40 行的薄文件，而不是把全部代码放进去？
> **答案**：分离「入口」与「实现」便于测试和复用。薄入口只负责启动 Qt 事件循环；真正的逻辑在 `v7` 包里，测试代码可以直接 `from v7 import ...` 拿到任意子模块，而不必先启动整个 GUI。

**练习 2**：`from v7 import RadarProcessor` 这一句在没有装 PyQt6 的 CI 环境里会失败吗？为什么？
> **答案**：不会。因为 `v7/__init__.py` 把 `RadarProcessor`（来自 `processing`，属于纯算法层）放在 `try/except` **之前**无风险地转出口；只有依赖 PyQt6 的 `workers / map_widget / dashboard` 才被 `try/except ImportError` 包裹。所以即使 PyQt6 缺失，纯算法模块仍可正常导入和测试。

**练习 3**：`models.py` 为什么要集中放主题颜色常量（如 `DARK_BG`）？
> **答案**：GUI 有多个模块都要画界面（dashboard、map_widget 等），若各自硬编码颜色会导致风格不一致、改主题要改多处。集中定义后，所有模块 `from .models import DARK_BG, ...` 引用同一份常量，换肤只需改一处。

---

### 4.2 六标签页主窗口

#### 4.2.1 概念说明

`RadarDashboard` 继承自 `QMainWindow`（Qt 的主窗口基类），是整个 GUI 的「壳」。它的内部用 `QTabWidget`（标签页容器）组织成六个标签页——这是一种常见的复杂界面组织方式：把功能按主题分组，每组的控件放进一个标签页，用户点标签切换，避免一屏塞满几百个旋钮。

六个标签页对应雷达操作的六类任务：

| # | 标签页 | 职责 |
|---|--------|------|
| 1 | **Main View** | Range-Doppler 热力图（64×32）、设备选择、启动/停止、目标表 |
| 2 | **Map View** | 内嵌 Leaflet 地图 + 侧边栏（雷达位置/覆盖/演示） |
| 3 | **FPGA Control** | 全部 FPGA 寄存器控制（27 个 opcode，含 AGC，带位宽校验） |
| 4 | **AGC Monitor** | 实时 AGC 曲线（增益/峰值/饱和） |
| 5 | **Diagnostics** | 连接状态、包统计、**可选依赖状态**、自测试结果、日志 |
| 6 | **Settings** | 主机端 DSP 参数 + 关于信息 |

这套设计把「看数据」「看地图」「调 FPGA」「看健康」「改设置」清晰分离开。注意一个重要区别：**FPGA Control 标签页调的是 FPGA 内部的寄存器**（通过 u6-l2 讲的 4 字节命令），而 **Settings 标签页调的是主机端（PC 上）的 DSP 参数**（如 DBSCAN 聚类），两者作用对象不同。

#### 4.2.2 核心流程

主窗口的构建流程是一个标准的「构造核心对象 → 构建界面 → 启动定时器」三段式：

```
RadarDashboard.__init__()
  ├─ 1. 创建核心对象（不建界面）
  │     ├─ RadarSettings / GPSData（数据）
  │     ├─ FT2232HConnection / STM32USBInterface / DataRecorder（硬件）
  │     ├─ RadarProcessor / USBPacketParser / ProcessingConfig（处理）
  │     └─ 各种状态变量（worker 句柄、帧计数、AGC 历史…）
  ├─ 2. 构建界面
  │     ├─ _apply_dark_theme()       # 套深色 QSS 样式
  │     ├─ _setup_ui()               # 建 QTabWidget，依次加 6 个标签页
  │     ├─ _setup_statusbar()        # 底部状态栏
  │     └─ 创建 6 个 _create_*_tab() 方法
  └─ 3. 启动刷新机制
        ├─ QTimer(100ms) → _refresh_gui()   # 定时刷新界面
        └─ 日志桥 _QtLogHandler → 信号 → 日志文本框
```

界面刷新不靠每个控件自己轮询，而是靠一个 **100 ms 的 `QTimer`** 周期性调用 `_refresh_gui()`，统一更新 GPS 标签、Range-Doppler 图、目标表、诊断数值。后台采集线程（4.1 提到的 `workers`）通过 PyQt **信号（signal）** 把新帧/新目标送到主线程的槽函数，再由定时器把最新数据「刷」到屏幕上——这就是「采集线程写数据 + 定时器读数据刷新」的解耦模式（详见 u8-l2）。

#### 4.2.3 源码精读

**模块 docstring 直接列出了六个标签页**，是理解主窗口最好的入口：

[v7/dashboard.py:1-24](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L1-L24) — 文件头 docstring 逐条说明了六个标签页的职责，并点明 GUI 用「production `radar_protocol.py`」与 FPGA 通信：FT2232H 走量产板（USB 2.0）、FT601 走高端板（USB 3.0），可在界面里选；还支持 Mock 模式（`FT2232HConnection(mock=True)`）用于无硬件开发。

**构造函数分三段**，把「建对象」和「建界面」分开：

[v7/dashboard.py:134-209](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L134-L209) — `__init__`。前半段（L139-189）创建所有核心对象与状态变量：连接对象 `self._connection`、STM32 接口、处理器 `RadarProcessor()`、各种 worker 句柄（初始为 `None`，按需创建）、AGC 历史环形缓冲（`deque(maxlen=256)`）。后半段（L193-209）调用 `_apply_dark_theme()` / `_setup_ui()` / `_setup_statusbar()` 构建界面，并启动 100 ms 的 `_gui_timer`。这种「先建对象后建界面」的顺序，让界面构建函数能直接引用已存在的核心对象。

**`_setup_ui` 是六标签页的总装入口**：

[v7/dashboard.py:334-349](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L334-L349) — `_setup_ui()`：建一个 `QTabWidget`，然后依次调用六个 `_create_*_tab()` 方法，每个方法构建一个标签页的控件树并 `self._tabs.addTab(tab, "标签名")` 挂上去。这六个方法一一对应表中的六个标签页，行号依次是 Main View L355、Map View L510、FPGA Control L616、AGC Monitor L884、Diagnostics L999、Settings L1097。

**FPGA Control 标签页是 opcode 契约的可视化**——它把 u6-l2 讲的 opcode 直接变成旋钮。以 CFAR 参数为例：

[v7/dashboard.py:757-765](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L757-L765) — CFAR 参数行：每行 `(名称, opcode, 默认值, 位宽, 提示)`，例如 `("CFAR Alpha (Q4.4)", 0x23, 48, 8, "0-255, rst=0x30=3.0")`。这里的 `0x23` 正是 u6-l2 讲的「设置 CFAR alpha」opcode，默认值 48（=0x30）对应 Q4.4 定点的 3.0，与 FPGA 端 `cfar_ca` 的复位默认值一致。点「Set」按钮会调用 `_send_fpga_validated` 把值钳制到位宽后用 `RadarProtocol.build_command(opcode, value)` 打包成 4 字节命令发出去。

**Diagnostics 标签页里有「可选依赖状态」面板**——这是 4.3 优雅降级的可视化呈现：

[v7/dashboard.py:1051-1072](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L1051-L1072) — 遍历 `("pyusb", USB_AVAILABLE)` 等 5 个依赖，根据标志位显示绿色「Available」或黄色「Missing」。这样操作员一打开 Diagnostics 就能看到哪些可选功能可用、哪些被禁用了，不必去翻日志。

#### 4.2.4 代码实践

这是一个**源码阅读 + 行号定位**实践。

1. **实践目标**：能在 `dashboard.py` 里快速定位任何一个标签页的构建代码。
2. **操作步骤**：
   - 打开 [`v7/dashboard.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py)。
   - 搜索 `def _create_` 找到六个标签页构建方法，记录每个方法的起止行号。
   - 在每个方法末尾找到 `self._tabs.addTab(tab, "XXX")`，确认标签页显示的名字。
3. **需要观察的现象**：每个 `_create_*_tab` 方法都是「建一个 `QWidget` → 摆布局 → `addTab`」的同构模式；FPGA Control 标签页因为有大量旋钮，所以包了一层 `QScrollArea`（可滚动）。
4. **预期结果**：你会得到一张「方法名 → 标签名 → 行号」对照表，例如 `_create_main_tab → "Main View" → L355`。以后想改某个标签页的控件，直接跳到对应方法即可。
5. **待本地验证**：如果你想实际看到这六个标签页，需要安装 PyQt6 依赖（见 `requirements_v7.txt` 的 Core 组）后运行 `python GUI_V7_PyQt.py`；无图形界面的环境无法弹出窗口。

#### 4.2.5 小练习与答案

**练习 1**：用户在「FPGA Control」标签页调节 CFAR alpha，和在「Settings」标签页调节 DBSCAN eps，这两者作用的对象有何不同？
> **答案**：前者通过 4 字节 opcode 命令（`0x23`）写到 **FPGA 内部** `cfar_ca` 模块的寄存器，改变的是硬件流水线的检测门限；后者只改 **主机端（PC）** `ProcessingConfig` 的聚类参数，影响的是 GUI 收到 FPGA 数据后做的软件后处理，FPGA 完全感知不到。

**练习 2**：为什么界面刷新用一个 100 ms 的 `QTimer` 统一驱动，而不是每个控件各自更新？
> **答案**：集中刷新便于节流（避免高频重绘卡死界面）、便于异常隔离（`_refresh_gui` 用 `try/except` 兜底），也让「数据生产」（后台线程）和「数据显示」（定时器）解耦——线程只管往共享变量写最新帧，定时器定期把最新帧画出来。

**练习 3**：`RadarDashboard.__init__` 为什么先创建核心对象（L139-189），再调用 `_setup_ui()`（L193）？
> **答案**：因为界面构建函数要引用这些核心对象——例如建 worker 需要传 `self._connection`、建 Settings 标签页需要读 `self._processing_config` 的初值。若先建界面再建对象，界面代码会因为引用不到对象而报 `AttributeError`。

---

### 4.3 可选依赖与优雅降级

#### 4.3.1 概念说明

GUI 依赖的 Python 库可以分成两类：

- **必需依赖（Core）**：没有它 GUI 根本跑不起来，如 PyQt6（界面框架）、numpy（数值计算）、matplotlib（画图）。
- **可选依赖（optional）**：没有它 GUI 仍能跑，只是某些高级功能用不了，如 `scipy`（高级 DSP）、`scikit-learn`（DBSCAN 聚类）、`filterpy`（Kalman 跟踪）、`pyusb`/`pyftdi`（USB 硬件访问）。

「**优雅降级（graceful degradation）**」是指：当某个可选依赖缺失时，程序不应当崩溃（`ImportError` 直接退出），而应当自动禁用依赖它的那个功能，并让其余功能正常工作。这对一个开源项目很重要——不是所有用户都愿意/能够装齐全部科学计算库，尤其在没有硬件、只看演示或回放的场景。

实现优雅降级的标准三步法：

1. **探针**：在模块加载时用 `try: import xxx except ImportError` 探测库是否存在，把结果存进一个布尔标志（如 `SKLEARN_AVAILABLE`）。
2. **条件导入**：真正用到该库的地方，用 `if 标志: from xxx import yyy` 条件导入，避免在缺库时直接 `ImportError`。
3. **运行时短路 + UI 禁用**：调用处用 `if not 标志: return 空结果` 提前返回；界面上把对应的控件 `setEnabled(False)` 并加提示。

#### 4.3.2 核心流程

以「sklearn 未安装 → 聚类被禁用」为例，完整的降级链路贯穿三个模块：

```
① models.py 加载时探测
   try: from sklearn.cluster import DBSCAN
   except ImportError: SKLEARN_AVAILABLE = False   # 探针置 False
        │
② processing.py 条件导入
   if SKLEARN_AVAILABLE: from sklearn.cluster import DBSCAN   # 缺库时不导入
   def clustering(...):
       if not SKLEARN_AVAILABLE: return []                     # 运行时短路
        │
③ dashboard.py Settings 标签页
   if not SKLEARN_AVAILABLE:
       self._cluster_check.setEnabled(False)                  # UI 禁用
       self._cluster_check.setToolTip("Requires scikit-learn")
        │
④ dashboard.py Diagnostics 标签页
   ("sklearn", SKLEARN_AVAILABLE) → 显示 "Missing"             # 可视化呈现
```

结果是：缺 sklearn 时，`import v7` 不报错、GUI 能启动、能采数据能画图，只是聚类复选框变灰、聚类函数返回空列表（目标不聚类，但仍能显示），Diagnosis 面板提示 sklearn Missing。整个 GUI 不崩溃。

`requirements_v7.txt` 把这种「必需 vs 可选」的划分写成了文档：

[requirements_v7.txt:4-22](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/requirements_v7.txt#L4-L22) — 依赖清单分两组：Core（PyQt6 / PyQt6-WebEngine / numpy / matplotlib，L4-8）是必需；其余按功能分注释段——Hardware interfaces（pyusb/pyftdi）、Signal processing（scipy）、Tracking/clustering（sklearn/filterpy）、CRC（crcmod）都是 optional，注释明确写「GUI degrades gracefully」。这份清单是 4.3.1 分类的事实来源。

#### 4.3.3 源码精读

**第①步：探针集中在 `models.py`**。这里有 5 个并列的 `try/except ImportError` 块，分别探测 pyusb、pyftdi、scipy、sklearn、filterpy：

[v7/models.py:16-55](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/models.py#L16-L55) — 5 个可选依赖可用性标志。以 sklearn 为例（L43-48）：`try: from sklearn.cluster import DBSCAN ... SKLEARN_AVAILABLE = True`，失败则置 `False` 并 `logging.warning("sklearn not available. Clustering will be disabled.")`。注意 `# noqa: F401` 注释——这些 import 只是为了「探测」，符号本身不被使用，`noqa` 告诉 linter 不要报「未使用导入」。把这些标志集中放在数据层 `models.py`，是因为所有模块都依赖 `models`，能保证大家读到的是同一份探测结果。

**第②步：`processing.py` 条件导入 + 运行时短路**：

[v7/processing.py:26-27](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py#L26-L27) — `if SKLEARN_AVAILABLE: from sklearn.cluster import DBSCAN`。缺库时这行被跳过，`DBSCAN` 这个名字根本不存在于命名空间——所以后续任何使用都必须先判标志，否则会 `NameError`。

[v7/processing.py:286-294](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py#L286-L294) — `clustering()` 方法的第一行就是 `if not SKLEARN_AVAILABLE or len(detections) == 0: return []`。这就是运行时短路：缺 sklearn 时直接返回空列表（「不聚类」），调用方拿到空聚类结果也不会崩。`scipy` 和 `filterpy` 用完全相同的模式保护（`SCIPY_AVAILABLE` / `FILTERPY_AVAILABLE`）。

**第③步：`dashboard.py` 把控件禁用**：

[v7/dashboard.py:1123-1128](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L1123-L1128) — Settings 标签页建「DBSCAN Clustering」复选框后，`if not SKLEARN_AVAILABLE: self._cluster_check.setEnabled(False); setToolTip("Requires scikit-learn")`。这样用户看到的是一个灰色、不可勾选的复选框，鼠标悬停还有原因提示，体验远好过「勾上之后报错」。紧随其后 L1155-1160 对 `filterpy`（Kalman 跟踪）复选框做了同样处理。

**第④步：Diagnostics 面板把状态显示出来**（见 4.2.3 引用的 [v7/dashboard.py:1051-1072]），这里不再重复。

> 一个细节：`hardware.py` 也用同样的模式保护 `pyusb`——[v7/hardware.py:18-23](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/hardware.py#L18-L23) 里 `from .models import USB_AVAILABLE` 然后 `if USB_AVAILABLE: import usb.core`，缺 pyusb 时 USB 枚举方法直接返回空列表。所以「优雅降级」是贯穿整个 `v7` 包的统一约定，不是某一处的临时处理。

#### 4.3.4 代码实践

这是一个**可本地运行的实验型实践**（需要 Python 环境，不需要雷达硬件）。

1. **实践目标**：亲眼观察 sklearn 缺失时 GUI 不崩溃、聚类被禁用。
2. **操作步骤**：
   - 先确认你装了 PyQt6、numpy、matplotlib（Core 组），但**故意不装** scikit-learn。可以用 venv：`python -m venv venv && source venv/bin/activate && pip install PyQt6 PyQt6-WebEngine numpy matplotlib`（不要装 scikit-learn）。
   - 在 `9_Firmware/9_3_GUI/` 目录下运行 `python -c "from v7 import models; print('SKLEARN_AVAILABLE =', models.SKLEARN_AVAILABLE)"`。
   - 再运行 `python -c "from v7.processing import RadarProcessor; p=RadarProcessor(); print(p.clustering([]))"`。
3. **需要观察的现象**：
   - 第一条命令应打印 `SKLEARN_AVAILABLE = False`，并在 stderr 看到一条 `WARNING ... sklearn not available. Clustering will be disabled.`
   - 第二条命令应正常返回 `[]`，**不抛 `ImportError` / `NameError`**。
4. **预期结果**：证明即使缺 sklearn，`import v7` 与调用 `clustering()` 都不崩溃——优雅降级生效。随后 `pip install scikit-learn` 重跑，标志变 `True`，聚类恢复正常。
5. **待本地验证**：本讲写作时未在你的机器上执行上述命令，结果为「预期」；不同操作系统/Python 版本的 PyQt6 安装细节可能有差异，以本地实际输出为准。若无图形界面，至少前两条 `python -c` 命令（不弹窗）可验证降级逻辑。

#### 4.3.5 小练习与答案

**练习 1**：为什么探测可选依赖的 `try/except ImportError` 要集中在 `models.py`，而不是每个用到该库的模块各自探测？
> **答案**：集中探测保证「同一份探测结果被所有模块共享」。若各模块各自探测，可能出现一个模块判 `True`、另一个判 `False` 的不一致；且 `models` 是公共底座，所有模块都已依赖它，把标志放这里零额外成本。

**练习 2**：在 `processing.py` 里，为什么 `from sklearn.cluster import DBSCAN` 必须包在 `if SKLEARN_AVAILABLE:` 里，而不是直接写在文件顶部？
> **答案**：若写在顶部，缺 sklearn 时整个 `processing.py` 会在 `import` 阶段就 `ImportError`，导致 `RadarProcessor` 等不依赖 sklearn 的功能也用不了——降级就不「优雅」了。条件导入确保只在「真要用 DBSCAN 且库存在」时才导入。

**练习 3**：除了「运行时短路 + UI 禁用」，Diagnostics 标签页的依赖状态面板（4.2.3）对运维有什么额外价值？
> **答案**：它让操作员在 GUI 里**一眼**看到哪些可选功能可用，不必翻日志或查 `pip list`。在现场调试时，如果发现「目标不聚类」，直接看 Diagnostics 就能判断是 sklearn 没装（功能降级）还是数据真的没目标（算法问题），加速定位。

---

## 5. 综合实践

把本讲三个最小模块串起来的综合任务：

**任务：绘制 V7 GUI 的「模块分层图 + 标签页功能图 + 降级链路图」三合一速查卡。**

具体要求：

1. **模块分层图**：参考 4.1.4 的实践结果，画出 `v7` 包的九大模块，按依赖方向从下到上排列（`models` 在最底、`dashboard` 在最顶），用箭头标出「谁 import 谁」。在 `agc_sim` 旁边标注「独立离线分析，不在 `__init__` 导出」。

2. **六标签页功能图**：参考 4.2，画一个主窗口框，内含六个标签页方格，每格写「标签名 + 一句话职责 + 它操作的对象（FPGA 寄存器 / 主机端 DSP / 纯显示）」。重点标出 **FPGA Control**（操作 FPGA）和 **Settings**（操作主机端）的区别。

3. **降级链路图**：参考 4.3，画出 sklearn 缺失时的四步链路（`models` 探针 → `processing` 条件导入 + 短路 → `dashboard` UI 禁用 → Diagnostics 显示），并在每一步标注对应的源码文件与行号。

4. **验证**：对照本讲给出的永久链接，逐条核对图上标注的行号是否与源码一致；若发现不一致（例如 HEAD 已更新），记录差异。

完成这张速查卡后，你就建立起了 V7 GUI 的完整心智模型：**包怎么分、界面怎么组、缺库怎么办**。后续 u8-l2（数据采集与帧组装）将深入 4.1 里提到的 `workers.py` + `radar_protocol.py` 的采集线程细节，u8-l3 将深入 `processing.py` 的双 CPI 融合、DBSCAN 聚类与 Kalman 跟踪——它们都建立在本讲的架构地图之上。

## 6. 本讲小结

- V7 GUI 采用「薄启动器 `GUI_V7_PyQt.py` + `v7` 包实现」结构，`v7/__init__.py` 作为门面集中转出口，并用 `try/except ImportError` 分层加载，使无 PyQt6 的 CI 环境也能用纯算法模块。
- `v7` 包由 9 个功能模块组成：`models`（数据层）、`hardware`（硬件层）、`processing`（处理层）、`workers`（线程层）、`map_widget`（地图层）、`replay`（回放层）、`software_fpga`（FPGA 软件镜像）、`agc_sim`（AGC 离线仿真，不在 `__init__` 导出）、`dashboard`（表现层），依赖方向单向。
- `models.py` 是全包共享的数据层，集中放数据类（`RadarTarget`/`RadarSettings`/`GPSData`/`ProcessingConfig`/`WaveformConfig`）、`TileServer` 枚举、主题颜色常量，以及 5 个可选依赖可用性标志。
- 主窗口 `RadarDashboard(QMainWindow)` 用 `QTabWidget` 组织成六个标签页：Main View、Map View、FPGA Control、AGC Monitor、Diagnostics、Settings；界面由 100 ms 的 `QTimer` 统一刷新。
- 「FPGA Control」标签页把 u6-l2 的 opcode 契约可视化成旋钮（如 CFAR alpha=`0x23`），作用对象是 FPGA 寄存器；「Settings」标签页调的是主机端 DSP（如 DBSCAN），两者作用对象不同。
- 优雅降级用标准三步法实现：`models.py` 探针置标志 → `processing.py` 条件导入 + 运行时短路返回空 → `dashboard.py` 禁用控件 + Diagnostics 显示状态；缺 scipy/sklearn/filterpy 时 GUI 不崩溃，只是对应功能变灰。

## 7. 下一步学习建议

- **u8-l2 数据采集线程与帧组装**：本讲只点到 `workers.py` 是「后台线程 + 队列」的采集/显示解耦模式。下一讲将深入 `RadarDataWorker` 如何从 USB 读字节流、用 `find_packet_boundaries` 定位 11 字节数据包、把样本按 range×doppler 写入 `RadarFrame`，凑齐 2048 单元后整帧入队。这是理解「FPGA 数据如何变成屏幕热力图」的关键一环。
- **u8-l3 信号处理、聚类与目标跟踪**：本讲把 `processing.py` 的 `RadarProcessor` 当黑盒。下一讲将拆开它的双 CPI 融合、多 PRF 解模糊、DBSCAN 聚类、Kalman 跟踪，以及 GPS/IMU 俯仰修正。学完后你能把本讲 4.3 提到的「聚类函数」彻底看懂。
- **延伸阅读**：如果想理解 `software_fpga.py` 和 `agc_sim.py` 这两个「软件镜像」为何能做到与 FPGA **逐位一致**，可以回头看 u4 系列（FPGA 接收信号处理链）与 u11-l1（FPGA 回归测试与 cosim），它们解释了 `golden_reference.py` 这套黄金参考模型如何同时服务于 FPGA 仿真和 GUI 回放。
