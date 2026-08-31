# Mode 系统与模式切换

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Mode` 基类通过三重继承（QObject + Savable + SCPINode）统一提供了哪些通用能力：界面元素的显示/隐藏、SCPI 命令子树、JSON 设置保存、与设备的联动。
2. 跟踪一次完整的模式切换：从用户点击标签页，经 `ModeWindow` → `ModeHandler::setCurrentIndex()` → 旧模式 `deactivate()` → 新模式 `activate()`，直到中央 `QStackedWidget` 换页和设备重新配置。
3. 独立写出新增一种自定义模式需要继承并实现的全部接口，并指出在 `appwindow.cpp` 的哪个位置注册生效。

本讲承接上一讲（u2-l1）：你已经知道 AppWindow 构造函数的装配顺序是「命令行与偏好 → SCPI/流式服务器 → setupUi → 状态栏/工具栏 → **ModeHandler 与中央 QStackedWidget** → SetupSCPI → SetInitialState」。本讲就把中间加粗的这一站彻底拆开。

## 2. 前置知识

本讲需要的概念都在前几讲出现过，这里快速回顾并补充两个新术语：

- **S 参数 / 频谱 / 信号源**：LibreVNA 的三种用途。VNA 模式测 S 参数（S11/S21），频谱仪（SA）模式看频域功率，信号源（SG/Generator）模式输出单一频率的连续波。它们对应设备端固件的三种工作状态（u1-l2 的「三站数据链路」中固件侧的三兄弟）。
- **信号与槽（signals/slots）**：Qt 的事件机制。对象 A `emit` 一个信号，connect 到该信号的槽函数就会被调用。本讲中 `ModeHandler` 的三个信号（ModeCreated/ModeClosed/CurrentModeChanged）驱动 `ModeWindow` 的界面更新。
- **QStackedWidget**：一个「多页签容器」，可以装多个子 widget，但同一时刻只显示一个。每个 Mode 的中央界面都注册进来，切换模式就是切换显示哪一页。
- **QSettings**：Qt 提供的跨平台「键值对」持久化工具（Linux 下写入 `~/.config/` 的 ini 文件）。Mode 用它保存工具栏/停靠窗口的布局，注意这与保存整个工作区的 `.setup` 文件（JSON）是两套不同的东西。
- **Savable 接口**：上一讲的 .setup 体系的基础，约定 `toJSON()`/`fromJSON()` 两个函数。`Mode` 继承了它，所以每个模式都能把自己的设置序列化进 `.setup` 文件。
- **SCPINode**：SCPI 命令树上的一个节点（详见 u10-l1，本讲只需要知道「继承它就能挂到命令树上」）。

一个需要先建立的整体直觉：

> **一个 Mode = 一种「完整的仪器人格」**。它自带中央界面、工具栏、停靠窗口、菜单动作、SCPI 命令子树和设置持久化。切换模式时，旧人格把自己的一切从主窗口上「摘下来」，新人格把一切「挂上去」。主窗口 AppWindow 只是舞台，人格们轮流登台。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `Software/PC_Application/LibreVNA-GUI/mode.h` | `Mode` 抽象基类声明：三重继承、Type 枚举、全部虚接口 |
| `Software/PC_Application/LibreVNA-GUI/mode.cpp` | `Mode` 实现：构造/析构、`activate()`/`deactivate()`、`finalize()`、类型名转换 |
| `Software/PC_Application/LibreVNA-GUI/modehandler.h` | `ModeHandler` 声明：模式容器、激活状态、信号 |
| `Software/PC_Application/LibreVNA-GUI/modehandler.cpp` | `ModeHandler` 实现：工厂 `createNew()`、激活/去激活、关闭模式、名字查重 |
| `Software/PC_Application/LibreVNA-GUI/modewindow.cpp` | `ModeWindow`：菜单栏角落的标签页 + "+" 按钮 + Mode 菜单，纯 UI 层 |
| `Software/PC_Application/LibreVNA-GUI/appwindow.cpp` | 集成点：创建 handler/window、`SetInitialState()` 注册三种模式、SaveSetup/LoadSetup |
| `Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp` | 内置模式一：矢量网络分析仪 |
| `Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp` | 内置模式二：信号发生器 |
| `Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp` | 内置模式三：频谱分析仪 |
| `Software/PC_Application/LibreVNA-GUI/savable.h` | `Savable` 接口（toJSON/fromJSON 纯虚） |

分层关系一句话：`ModeWindow`（纯 UI）和 `AppWindow`（集成）都只跟 `ModeHandler`（管理者）打交道，`ModeHandler` 持有并调度所有 `Mode`（被管理者）。UI 从不直接 new 一个模式。

## 4. 核心概念与源码讲解

### 4.1 Mode 基类

#### 4.1.1 概念说明

`Mode` 是三种测量模式（以及你未来自己写的模式）的公共基类。它要解决的问题只有一个：**把「一种仪器模式」所需要的一切资源打包成同一个生命周期**。

如果没有这个基类，AppWindow 就要为每种模式写一套「显示自己的工具栏、隐藏别人的工具栏、注册自己的 SCPI 命令、保存自己的设置」的重复代码。有了它，AppWindow 只需面对统一的 `Mode*` 指针调用 `activate()`/`deactivate()`。

`Mode` 的能力来自三重继承：

```cpp
class Mode : public QObject, public Savable, public SCPINode
```

| 基类 | 提供的能力 | 在 Mode 中的体现 |
|---|---|---|
| `QObject` | 信号与槽、父子对象树 | `statusbarMessage` 信号；以 AppWindow 为父对象自动回收 |
| `Savable` | `toJSON()`/`fromJSON()` 纯虚接口 | 模式设置可存入 `.setup` 文件（u2-l3 详讲） |
| `SCPINode` | SCPI 命令树节点 | 构造时自动挂到 AppWindow 的命令树下，形成 `:VNA:...`、`:SA:...` 等子树 |

#### 4.1.2 核心流程

一个 Mode 的完整生命周期：

```text
构造 Mode(window, name, SCPIname)
   │  自动把自己挂到 SCPI 命令树
   │  子类构造：创建中央界面/工具栏/停靠窗口，收集到 actions/toolbars/docks 集合
   │  子类构造末尾调用 finalize(centralWidget)
   │      ├─ central 注册进 AppWindow 的 QStackedWidget
   │      └─ 所有模式专属 GUI 元素先隐藏（未激活不抢界面）
   ▼
activate()   ← 由 ModeHandler 调用（用户切换到这个模式）
   │  显示 toolbars / docks / actions
   │  恢复 QSettings 里保存的布局（windowState_<模式名>）
   │  若已连接设备 → initializeDevice() 重新配置硬件
   │  emit statusbarMessage(...)
   ▼
（运行中……用户点击别的模式标签）
   │
deactivate() ← 由 ModeHandler 调用
   │  把 dock/toolbar 可见性、窗口布局存回 QSettings
   │  隐藏所有模式专属 GUI 元素
   │  若已连接设备 → device->setIdle()（设备停止测量）
   ▼
析构 ~Mode()
   │  从 SCPI 树移除、从 QStackedWidget 移除、删除 docks/toolbars
```

注意 `activate()`/`deactivate()` 是**受保护的（protected）虚函数**，且约定子类必须调用基类版本——注释里写得很清楚：派生类在做任何事之前先调 `Mode::activate`，在返回之前调 `Mode::deactivate`。外部代码不能直接调用它们，只能通过 `ModeHandler`，这保证了「同一时刻只有一个活动模式」的不变量。

#### 4.1.3 源码精读

**三重继承与 Type 枚举**。[mode.h:L16-L26](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.h#L16-L26) 声明了类本身和类型枚举：`VNA`、`SG`（Signal Generator）、`SA`（Spectrum Analyzer）、`Last`。`Last` 不代表任何模式，它是个哨兵值，用于「遍历所有合法类型」的循环边界（后面 ModeWindow 会用到）。`friend class ModeHandler` 让管理者能访问受保护的 `activate()`/`deactivate()`。

**必须实现的纯虚接口**。Mode 自身声明了 4 个纯虚函数，加上从 Savable 继承的 2 个，一个具体模式共需实现 6 个：

| 函数 | 声明位置 | 含义 |
|---|---|---|
| `Type getType() = 0` | [mode.h:L37](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.h#L37) | 报告自己是 VNA/SG/SA 中的哪一种 |
| `void initializeDevice() = 0` | [mode.h:L41](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.h#L41) | 激活时（或设备接入时）把硬件配置成本模式需要的状态 |
| `void setAveragingMode(Averaging::Mode) = 0` | [mode.h:L46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.h#L46) | 全局平均方式变化时通知模式 |
| `void preset() = 0` | [mode.h:L48](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.h#L48) | 恢复出厂默认设置（菜单里的 Preset） |
| `nlohmann::json toJSON() = 0` | [savable.h:L18](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/savable.h#L18) | 序列化设置 |
| `void fromJSON(nlohmann::json) = 0` | [savable.h:L19](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/savable.h#L19) | 反序列化设置 |

其余虚函数都有默认空实现，按需覆盖：`shutdown()`（程序退出前）、`deviceDisconnected()`（设备拔出）、`deviceInfoUpdated()`（设备能力变化）、`resetSettings()`、`getImportOptions()`/`getExportOptions()`（填充菜单栏的 Import/Export 菜单）、`saveSreenshot()`。完整清单见 [mode.h:L28-L55](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.h#L28-L55)。

**构造函数：自动挂上 SCPI 树**。[mode.cpp:L19-L28](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L19-L28)：

```cpp
Mode::Mode(AppWindow *window, QString name, QString SCPIname)
    : QObject(window),
      SCPINode(SCPIname),
      isActive(false),
      ...
{
    window->getSCPI()->add(this);
}
```

第三行是关键：每个模式诞生时就把自己作为节点挂到 AppWindow 的 SCPI 命令树根部。所以远程控制时 `:VNA:FREQuency:STARt` 里的 `VNA` 这一段，就是这个模式对象自己。析构函数 [mode.cpp:L30-L41](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L30-L41) 对称地从树上摘除，并清理 central、docks、toolbars——资源进出完全配对。

**activate()：登台**。[mode.cpp:L43-L88](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L43-L88) 依次做四件事：

1. 显示本模式的所有工具栏和停靠窗口，并把它们的开关项加进主菜单的 Toolbars/Docks 子菜单（L48-L55）；
2. 把主窗口中央的 `QStackedWidget` 切到自己的页面，并用 `QSettings` 里按模式名保存的字节流恢复窗口布局（L60-L63，键名 `windowState_<模式名>`）；
3. 逐个恢复每个 dock/toolbar 的显隐状态（L66-L81）；
4. **如果设备已连接，调用 `initializeDevice()`**（L83-L85）——这就是「切到 VNA 标签页，设备就开始扫描」的机制源头。

**deactivate()：谢幕**。[mode.cpp:L90-L121](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L90-L121) 是反向操作：先把 dock/toolbar 可见性与整个窗口布局写入 QSettings（L94-L100），再隐藏全部模式专属元素、从菜单移除开关项（L102-L113），最后**让设备进入空闲**（L117-L119 的 `window->getDevice()->setIdle()`）。把这个 setIdle 和 activate 末尾的 initializeDevice 连起来看，就得到一个重要结论：**切换模式 = 设备先停机，再按新模式的配置重启测量**。硬件上从不会有两种模式同时跑。

**finalize()：构造收尾**。[mode.cpp:L148-L169](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L148-L169) 在子类构造函数末尾调用：把中央控件注册进 `QStackedWidget`（L151），给每个 dock/toolbar 设置 `objectName = 标题+模式名`（L153-L158——Qt 的 saveState/restoreState 靠 objectName 辨认部件，所以名字必须含模式名以避免不同模式的同名工具栏互相覆盖），然后**把所有元素隐藏**（L159-L168）。新建的模式处于「潜伏」状态，等待第一次 activate。

**类型 ↔ 名称转换**。[mode.cpp:L123-L141](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L123-L141) 提供枚举值与人类可读名（如 `Type::VNA` ↔ `"Vector Network Analyzer"`）的双向转换。`TypeFromName` 找不到时返回 `Type::Last`——LoadSetup 就靠这个哨兵值过滤损坏的 setup 文件条目。

#### 4.1.4 代码实践

**实践目标**：不写代码，纯靠阅读 `mode.h`，产出一份「Mode 接口清单」，为下一节的 HelloMode 骨架做准备。

**操作步骤**：

1. 打开 [mode.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.h)，从上往下扫一遍，把所有 `virtual` 函数抄成三列的表：函数签名 / 是纯虚（`= 0`）还是有默认实现 / 你猜测的调用时机。
2. 打开 [savable.h:L14-L19](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/savable.h#L14-L19)，补上从 `Savable` 继承的两个纯虚函数。
3. 验证你的清单：在 `VNA/vna.h`、`Generator/generator.h`、`SpectrumAnalyzer/spectrumanalyzer.h` 中搜索 `toJSON`、`initializeDevice`、`preset`，确认三个具体类都实现了它们。

**需要观察的现象**：`mode.h` 本体只有 4 个纯虚函数，但任何具体模式都必须实现 6 个——多出来的两个来自 Savable 基类。这是 C++ 多重继承的典型陷阱：只看派生列表最直接的基类会漏掉间接契约。

**预期结果**：得到一张 6 行的「必选接口」表（getType、initializeDevice、setAveragingMode、preset、toJSON、fromJSON）和一张约 9 行的「可选覆盖」表（shutdown、resetSettings、deviceDisconnected、saveSreenshot、getImportOptions、getExportOptions、deviceInfoUpdated、activate、deactivate）。待本地验证的部分：无，纯静态阅读即可完成。

#### 4.1.5 小练习与答案

**练习 1**：`Mode` 为什么把 `activate()`/`deactivate()` 设计成 protected，而 `initializeDevice()` 设计成 public 纯虚？

**答案**：activate/deactivate 只应由 `ModeHandler` 统一调度（它靠 `friend` 声明获得访问权），若对外公开，任何代码都能绕过管理者直接切换，破坏「同一时刻只有一个活动模式」的不变量。`initializeDevice()` 则相反，它除了在 activate 内部被调用外，还需要在**设备刚接入**时被 AppWindow 调用（u2-l1 讲过的 `ConnectToDevice` 流程末尾），所以必须公开。

**练习 2**：Mode 用 QSettings 保存布局时，键名都拼上了模式名（如 `windowState_<name>`）。如果两个模式恰好同名，会发生什么？

**答案**：它们的布局会写进同一批 QSettings 键，互相覆盖——后退出的模式的布局会覆盖先退出的。这正是 `ModeHandler::nameAllowed()` 强制模式名唯一的原因之一（下一节精读）。另外 `finalize()` 里 `setObjectName(标题+模式名)` 也依赖名字唯一，重名会导致 restoreState 恢复到错误的部件。

**练习 3**：看 [mode.cpp:L187-L194](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L187-L194) 的 `updateGraphColors()`：为什么只对 VNA 和 SA 类型刷新绘图颜色，Generator 不需要？

**答案**：Generator（信号发生器）模式没有 Trace 曲线图——它只有频率/功率控制界面，没有需要按主题刷新颜色的 `TracePlot`。VNA 和 SA 都以曲线图为主体，所以主题色变化时要遍历 `TracePlot::getPlots()` 刷新。这也侧面说明：**模式的类型决定了它拥有哪类界面资产**。

### 4.2 ModeHandler 与 ModeWindow

#### 4.2.1 概念说明

- **ModeHandler** 是「模式管家」：一个 `std::vector<Mode*>` 容器加上「哪个模式正在活动」的状态。所有创建、激活、切换、关闭、重命名、查重的逻辑都集中在这里。它不包含任何界面代码。
- **ModeWindow** 是「模式开关面板」：位于主窗口菜单栏右上角的一排标签页（QTabBar）加一个 "+" 按钮，外加菜单栏里的 Mode 菜单。它不含任何业务逻辑，只做两件事：把用户的点击翻译成对 ModeHandler 的调用；监听 ModeHandler 的信号刷新界面。

这种「管理者 + 视图」的拆分带来一个好处：**AppWindow 和 SCPI 系统都能切换模式，而不必知道标签页的存在**。SCPI 命令、标签页点击、菜单选择，最终都汇入同一个 `setCurrentIndex()`。

#### 4.2.2 核心流程

**模式切换的完整链路**（用户点击标签页）：

```text
用户点击 tabBar 上某个标签
  → QTabBar::currentChanged(index)            [modewindow.cpp L23 连接]
  → ModeHandler::setCurrentIndex(index)       [modehandler.cpp L84]
      ├─ getMode(index)->activate 之前：
      │    若已有活动模式 → ModeHandler::deactivate(旧)
      │        └─ 旧Mode::deactivate()：保存布局、隐藏元素、设备 setIdle()
      ├─ activeMode = 新模式；新Mode::activate()
      │        └─ 显示元素、恢复布局、设备 initializeDevice()
      └─ emit CurrentModeChanged(index)
           ├─ ModeWindow::CurrentModeChanged：同步 tabBar 选中项 + 菜单勾选
           └─ AppWindow::UpdateImportExportMenus：用新模式重填 Import/Export 菜单
```

**创建一个新模式**：

```text
ModeWindow 的 "+" 按钮弹出类型菜单（遍历 Type 枚举自动生成）
  → 用户选类型、输入名字
  → handler->nameAllowed(名字)？否 → 报错弹窗
  → ModeHandler::createMode(name, type)
      ├─ createNew(aw, name, type)   ← 工厂函数，switch 分发到 new VNA / new Generator / new SpectrumAnalyzer
      └─ addMode(mode)
           ├─ modes.push_back(mode)
           ├─ connect(mode 的 statusbarMessage → 自己的过滤转发槽)
           └─ emit ModeCreated(index) → ModeWindow 加标签页 + 加菜单项
```

**关闭一个模式**（点标签上的 ×）：`closeMode(index)` 先断开信号连接，若关的是活动模式则先切到相邻索引，再 `delete` 并从容器擦除，最后 `emit ModeClosed(index)` 让界面摘掉标签。

#### 4.2.3 源码精读

**工厂函数：Type 枚举是扩展点也是闸门**。[modehandler.cpp:L38-L46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modehandler.cpp#L38-L46)：

```cpp
Mode *ModeHandler::createNew(AppWindow *aw, QString name, Mode::Type t)
{
    switch(t) {
    case Mode::Type::VNA: return new VNA(aw, name);
    case Mode::Type::SG:  return new Generator(aw, name);
    case Mode::Type::SA:  return new SpectrumAnalyzer(aw, name);
    default: return nullptr;
    }
}
```

这是全程序唯一一个「Mode::Type → 具体类」的映射表。**新增一种模式类型，这个 switch 必须加一个 case**，否则 `createMode` 会拿到 nullptr 并在后续崩溃。`createMode`/`addMode` 的完整链条见 [modehandler.cpp:L23-L36](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modehandler.cpp#L23-L36)：`addMode` 把新模式压入容器、连接状态栏消息信号、发出 `ModeCreated`。

**activate/deactivate：一次切换的两半**。[modehandler.cpp:L58-L77](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modehandler.cpp#L58-L77)：

```cpp
void ModeHandler::activate(Mode * mode)
{
    if (getActiveMode() == mode) {
        return;                    // 已经是活动模式，无事可做
    } else if (getActiveMode()) {
        deactivate(getActiveMode());  // 先让旧的谢幕
    }
    activeMode = mode;
    mode->activate();              // 再让新的登台
}
```

配合 [modehandler.cpp:L84-L92](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modehandler.cpp#L84-L92) 的 `setCurrentIndex()`（取模式、activate、发信号），就完成了上一小节流程图里的主干。顺序很重要：**先 deactivate 再 activate**，保证界面元素不会同屏、设备先停再启。

**状态栏消息的过滤转发**。[modehandler.cpp:L174-L180](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modehandler.cpp#L174-L180)：所有模式的状态栏消息都连到这一个槽，但槽里先检查 `sender()`（Qt 提供的信号发送者）是不是当前活动模式，只有活动模式的消息才向上转发给 AppWindow 显示。后台模式（比如正在后台扫描的模式）发消息不会打扰用户。

**名字唯一性**。[modehandler.cpp:L182-L197](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modehandler.cpp#L182-L197) 的 `nameAllowed()` 遍历容器查重，`ignoreIndex` 参数供重命名时跳过自己。为什么名字如此重要？除了练习 2 里说的 QSettings 布局键，还有一个更硬的理由：**SaveSetup 用名字记录哪个模式是活动模式**（见 4.3.3 的 LoadSetup），重名会让恢复时找不到唯一目标。

**ModeWindow：信号接线板**。[modewindow.cpp:L12-L30](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modewindow.cpp#L12-L30) 的构造函数把两边的信号全部接好：

```cpp
connect(handler, &ModeHandler::ModeCreated,       this, &ModeWindow::ModeCreated);
connect(handler, &ModeHandler::ModeClosed,        this, &ModeWindow::ModeClosed);
connect(handler, &ModeHandler::CurrentModeChanged,this, &ModeWindow::CurrentModeChanged);

connect(tabBar, &QTabBar::currentChanged,    handler, &ModeHandler::setCurrentIndex);
connect(tabBar, &QTabBar::tabCloseRequested, handler, &ModeHandler::closeMode);
connect(tabBar, &QTabBar::tabMoved,          handler, &ModeHandler::currentModeMoved);
```

前三行是「数据 → 界面」，后三行是「界面 → 数据」。任何一侧变化，另一侧通过信号自动跟上——这就是典型的 Qt 双向同步模式。注意 `tabMoved` 额外接了一个 lambda 同步菜单项顺序（L26-L29），因为拖动标签只改 tabBar 自己的顺序，菜单里的项要单独换位（`currentModeMoved` 负责容器内交换，见 [modehandler.cpp:L94-L100](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modehandler.cpp#L94-L100)）。

**界面生成的「免维护」设计**。[modewindow.cpp:L107-L119](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modewindow.cpp#L107-L119) 用一个循环遍历 `0 .. Type::Last` 生成 "+" 按钮和 Create new 子菜单里的所有类型项：

```cpp
for(unsigned int i=0;i<(int) Mode::Type::Last;i++) {
    auto type = (Mode::Type) i;
    auto action = new QAction(Mode::TypeToName(type));
    ...
}
```

这段代码**不认识任何具体模式**。你将来在 Type 枚举里加了 `Hello`、在 `TypeToName` 里返回 "Hello Mode"、在 `createNew` 工厂里加了 case，这里就自动多出一个菜单项——UI 层零修改。同文件 [L65-L80](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modewindow.cpp#L65-L80) 的 `createNew` lambda 处理「弹对话框取名 → 查重 → createMode → 切换过去」。

**防止反馈循环的 blockSignals**。[modewindow.cpp:L145-L170](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modewindow.cpp#L145-L170) 的 `ModeCreated` 里，`tabBar->insertTab()` 被包在 `blockSignals(true)/（false）` 之间。原因：insertTab 会触发 tabBar 的 `currentChanged`，而这个信号又连着 `handler->setCurrentIndex`——不屏蔽的话，程序化加标签会被误当成用户点击，引发一次本不想要的模式切换。`ModeClosed` 里 removeTab 同理（[modewindow.cpp:L172-L180](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modewindow.cpp#L172-L180)）。

**AppWindow 侧的接线**。[appwindow.cpp:L162-L174](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L162-L174)：创建 handler 和 window、把 Mode 菜单插进菜单栏（Help 菜单之前）、创建中央 `QStackedWidget`、把状态栏消息接到标签、把 `CurrentModeChanged` 接到 `UpdateImportExportMenus`（后者用活动模式的 `getImportOptions()/getExportOptions()` 重填 Import/Export 菜单，见 [appwindow.cpp:L1170-L1189](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1170-L1189)）。这就是 u2-l1 装配顺序里「ModeHandler 与中央 QStackedWidget」一站的全部内容。

#### 4.2.4 代码实践

**实践目标**：亲眼看到模式切换时 activate/deactivate 的执行顺序，验证「先谢幕后登台」。

**操作步骤**：

1. 你已经编译过 GUI（u1-l3）。直接运行 GUI，找到主窗口内嵌的日志面板（Log dock；若看不到，在 Window/Toolbars 相关菜单里勾选）。
2. 点击菜单栏右上角的标签页，在 "Vector Network Analyzer"、"Spectrum Analyzer"、"Generator" 之间来回切换几次。
3. 观察日志中成对出现的两行输出。

**需要观察的现象**：每次切换你会看到类似 `Deactivated mode "Vector Network Analyzer"` 和 `Activating mode "Spectrum Analyzer"` 的日志——它们来自 [mode.cpp:L115](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L115) 和 [mode.cpp:L46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L46) 的 `qDebug()`。注意 deactivate 总是先于 activate 出现，且切换到「当前已经活动的模式」时不会产生任何新日志（`activate()` 开头的 early return）。

**预期结果**：日志顺序证明切换链路是 deactivate(旧) → activate(新)；重复点击同一标签无输出。若你的构建把 qDebug 输出到终端，也可以从命令行启动观察 stderr。无硬件也能完成本实践——initializeDevice 只在已连接设备时才被调用，不影响日志出现。

#### 4.2.5 小练习与答案

**练习 1**：`closeMode`（[modehandler.cpp:L107-L160](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modehandler.cpp#L107-L160)）里，为什么删除前要执行那段寻找 `getCurrentIndex()-1` / `+1` 的逻辑？

**答案**：如果用户关闭的恰好是当前活动模式的标签，删除后活动模式就悬空了。这段逻辑在删除前先把当前索引切到相邻的存活模式（优先左边，左边没有则右边），保证任何时刻界面都指向一个有效模式。`closeModes()`（L162-L172）也遵守类似原则：活动模式留到最后关，因为关掉活动模式引发的切换需要其他模式还在场。

**练习 2**：为什么 `ModeWindow` 不是一个 QMainWindow 的子类，而只是一个挂在菜单栏角落的 QWidget？

**答案**：因为它只承担「切换器」职责，不是窗口。真正的模式界面在中央 QStackedWidget 里，工具栏和停靠窗口挂在 AppWindow 上。ModeWindow 只有 tabBar + "+" 按钮 + Mode 菜单，把职责限制在输入/输出切换意图，让主窗口保持唯一的窗体权威——这也是它能被 `setCornerWidget` 塞进菜单栏角落的前提。

**练习 3**：如果不经过 ModeWindow，还有哪些途径能把模式切到 SA？（提示：本讲源码里至少还有两条路。）

**答案**：① AppWindow 注册的 SCPI 命令：`MODE` 命令回调里 `findFirstOfType(Mode::Type::SA)` + `setCurrentIndex`（[appwindow.cpp:L687-L707](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L687-L707)），远程脚本用它可以切模式；② LoadSetup 恢复 setup 文件时按 `activeMode` 名字找到对应模式并 `setCurrentIndex`（[appwindow.cpp:L1242-L1250](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1242-L1250)）。此外 ModeWindow 自身的 Mode 菜单里的勾选项也走 `tabBar->setCurrentIndex`（[modewindow.cpp:L158-L163](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modewindow.cpp#L158-L163)）。所有路径最终都汇入 `ModeHandler::setCurrentIndex`。

### 4.3 三种内置模式

#### 4.3.1 概念说明

三种内置模式是 `Mode` 抽象的三份「标准答卷」，复杂度递减：

- **VNA**（矢量网络分析仪）：最重的一个。拥有 TraceModel（曲线数据模型）、TileWidget（图块布局）、校准、去嵌入、扫描设置，界面元素和业务逻辑都最多。
- **SpectrumAnalyzer**（频谱仪）：结构与 VNA 类似（同样有 traceModel + tiles + 滚动区域），但按频谱语义组织设置（SPAN/RBW 等）。
- **Generator**（信号源）：最轻的一个。中央界面就是一个 `SignalgeneratorWidget`，没有曲线模型——证明 Mode 抽象并不强制你拥有绘图系统。

对比三份答卷是学习「哪些接口必须实现、哪些可以省略」的最快方式。

#### 4.3.2 核心流程

三种模式共同的构造套路（以 Generator 为例，它最短）：

```text
Generator::Generator(window, name)
  ├─ Mode(window, name, "GENerator")   ← 基类构造：挂 SCPI 树
  ├─ central = new SignalgeneratorWidget(...)   ← 创建中央界面
  ├─ 从 Preferences/QSettings 恢复初始频率、功率
  ├─ setupSCPI()                        ← 在自己的 SCPI 子树下注册具体命令
  └─ finalize(central)                  ← 注册进 QStackedWidget，隐藏自己
```

`"GENerator"` 这种大小写混拼不是笔误，而是 SCPI 的助记符惯例：全大写部分（`GEN`）是短助记符，整个单词是长助记符，两种写法远程控制时都接受（解析细节在 u10-l1 详讲）。对应地 VNA 用 `"VNA"`、SA 用 `"SA"`。

而 deactivate 的覆盖则展示了「子类在基类流程前/后插入自己的收尾」模式：

```text
VNA::deactivate()            Generator::deactivate()
  ├─ StoreSweepSettings()      ├─ 把当前频率/功率存入 QSettings
  ├─ 停止 configurationTimer   └─ Mode::deactivate()   ← 必须调基类
  └─ Mode::deactivate()
```

#### 4.3.3 源码精读

**三份构造函数的开头**，注意各自传给基类的 SCPI 名：

- VNA：[vna.cpp:L57-L63](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L57-L63)，`: Mode(window, name, "VNA")`，中央是包着 `TileWidget` 的 `QScrollArea`；
- Generator：[generator.cpp:L7-L10](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L7-L10)，`: Mode(window, name, "GENerator")`，中央直接是一个 `SignalgeneratorWidget`；
- SpectrumAnalyzer：[spectrumanalyzer.cpp:L44-L50](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L44-L50)，`: Mode(window, name, "SA")`，同样是 scrollArea + tiles。

**构造函数末尾的 finalize**。三处完全一致的模式：VNA 在 [vna.cpp:L688](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L688)、Generator 在 [generator.cpp:L26](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L26)、SpectrumAnalyzer 在 [spectrumanalyzer.cpp:L339](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L339)。mode.h 里 `finalize` 的注释写着 "call once the derived class is fully initialized"——因为 finalize 会遍历 `docks`/`toolbars`/`actions` 三个集合给它们设置 objectName 并隐藏，如果子类还没把这些集合填满就调用，晚注册的元素就不会被正确管理。

**deactivate 覆盖：先做私事，再调基类**。[vna.cpp:L761-L767](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L761-L767) 在调 `Mode::deactivate()` 之前先持久化扫描设置、停掉配置定时器；[generator.cpp:L30-L38](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L30-L38) 先把当前频率/功率写进 QSettings（下次激活时构造函数会读回来，形成「记住上次状态」的体验）；[spectrumanalyzer.cpp:L342-L346](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L342-L346) 同型。有趣的是**没有一个内置模式覆盖 `activate()`**——基类的默认 activate 已经足够（显示元素、恢复布局、initializeDevice），需要「激活时做额外事」的逻辑大多放在 `initializeDevice()` 里。

**initializeDevice：能力协商**。[generator.cpp:L40-L47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L40-L47) 展示了模式与设备驱动的能力协商：

```cpp
void Generator::initializeDevice()
{
    if(!window->getDevice()->supports(DeviceDriver::Feature::Generator)) {
        InformationBox::ShowError("Unsupported", "The connected device does not support generator mode");
        return;
    }
    updateDevice();
}
```

由于 GUI 也能连接第三方仪器（u3 单元的 DeviceDriver 生态），不是每台设备都支持所有模式；激活模式时先问驱动 `supports()`，不支持就弹窗告知。这是 Mode 体系与驱动体系的第一处交点。

**模式集合的诞生与持久化**。三种默认模式在 `AppWindow::SetInitialState()` 里创建（[appwindow.cpp:L296-L309](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L296-L309)）：

```cpp
auto vnaIndex = modeHandler->createMode("Vector Network Analyzer", Mode::Type::VNA);
modeHandler->createMode("Signal Generator", Mode::Type::SG);
modeHandler->createMode("Spectrum Analyzer", Mode::Type::SA);
modeHandler->setCurrentIndex(vnaIndex);
```

注意用户后续可以随意增删模式实例（同一个类型可以有多个标签页）。保存工作区时，`SaveSetup()`（[appwindow.cpp:L1122-L1144](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1122-L1144)）把**当前模式列表**（每项的 type/name/settings）连同活动模式名一起写进 JSON：

```cpp
for(auto m : modeHandler->getModes()) {
    jmode["type"] = Mode::TypeToName(m->getType()).toStdString();
    jmode["name"] = m->getName().toStdString();
    jmode["settings"] = m->toJSON();      // ← Savable 接口在这里兑现
}
j["activeMode"] = modeHandler->getActiveMode()->getName().toStdString();
```

`LoadSetup()`（[appwindow.cpp:L1207-L1254](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1207-L1254)）则先 `closeModes()` 清场，再按 JSON 重建每个模式（`TypeFromName` 解析类型、`createMode` 落地、`fromJSON` 灌设置），最后按 `activeMode` 名字切换到保存时的活动模式；如果没找到（setup 文件损坏），兜底激活第一个模式防止界面悬空（L1252-L1254）。注意它加载前会先断开设备、加载完重连（L1200-L1240）——避免删旧建新模式的过程中每个模式都去配置一遍设备。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：写出 `HelloMode` 的完整伪代码骨架——一个只显示空 QWidget 的最小模式，并指出注册点。这是「新增一种自定义模式」的全流程演练。

**操作步骤**：

1. **先列接口清单**（4.1.4 的产出）：必选 6 个——`getType`、`initializeDevice`、`setAveragingMode`、`preset`、`toJSON`、`fromJSON`。
2. **写骨架**（示例代码，非项目原有代码）：

```cpp
// Hellomode.h —— 示例代码
class HelloMode : public Mode
{
    Q_OBJECT
public:
    HelloMode(AppWindow *window, QString name)
        : Mode(window, name, "HELLo")     // SCPI 子树名，遵循大写短助记符惯例
    {
        auto *central = new QWidget();    // 只显示一个空界面
        finalize(central);                // 构造收尾：注册进 QStackedWidget 并隐藏
    }

    // ---- 必选接口 ----
    Type getType() override          { return Type::Hello; }   // 需要先扩展枚举
    void initializeDevice() override {}    // 无硬件需求，空实现
    void setAveragingMode(Averaging::Mode) override {}
    void preset() override           {}
    nlohmann::json toJSON() override { return {}; }
    void fromJSON(nlohmann::json) override {}
};
```

3. **注册生效需要动四个文件**（这是本实践最重要的发现——「插入一行」是不够的，因为注册被工厂模式拆成了四处）：

   | # | 文件与位置 | 改什么 |
   |---|---|---|
   | 1 | [mode.h:L21-L26](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.h#L21-L26) 的 `enum class Type` | 在 `Last` 前加 `Hello` |
   | 2 | [mode.cpp:L123-L131](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L123-L131) 的 `TypeToName` | 加 `case Type::Hello: return "Hello Mode";`（`TypeFromName` 靠它反查，setup 文件也靠它存类型名） |
   | 3 | [modehandler.cpp:L38-L46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modehandler.cpp#L38-L46) 的 `createNew` | 加 `case Mode::Type::Hello: return new HelloMode(aw, name);`（**漏掉这里 createMode 会返回 nullptr 并崩溃**） |
   | 4 | [appwindow.cpp:L304-L307](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L304-L307) 的 `SetInitialState()` | 在 `createMode("Spectrum Analyzer", ...)` 之后、`setCurrentIndex(vnaIndex)` 之前加一行 `modeHandler->createMode("Hello Mode", Mode::Type::Hello);` |

   完成后重新编译。ModeWindow 的 "+" 菜单和 Mode 菜单**不需要任何修改**就会自动出现 "Hello Mode"（4.2.3 讲过的枚举遍历），新建一个 Hello 标签页就能看到空白的中央区域。

**需要观察的现象**：启动 GUI 后，"+" 按钮的弹出菜单里多出 "Hello Mode"；创建后主界面变成空白页（空 QWidget），原三种模式仍可正常切换；日志里出现 `Activating mode "Hello Mode"`。

**预期结果**：如上。若跳过第 3 步（不改 `createNew`），点 "Hello Mode" 菜单项后程序会对 nullptr 调用成员函数而崩溃——这恰好验证了工厂 switch 是注册的强制闸门。本实践的编译运行部分**待本地验证**（骨架代码为示例代码，未包含头文件 include 与 .pro 登记；若真正编译，还需把 `hellomode.h/cpp` 加入 `LibreVNA-GUI.pro` 的 HEADERS/SOURCES，u1-l3 讲过未登记的文件不参与编译）。

#### 4.3.5 小练习与答案

**练习 1**：三个内置模式为什么都不覆盖 `activate()`，却都覆盖 `deactivate()`？

**答案**：基类 `activate()` 的通用流程（显示元素、恢复布局、若有设备则 initializeDevice）对三者都够用，模式特有的「激活时配置」可以放进 `initializeDevice()`。而 `deactivate()` 需要各自不同的收尾：VNA 要存扫描设置并停配置定时器，Generator 要记住当前频率/功率，SA 也有自己的清理——这些没有通用形态，只能各自覆盖。

**练习 2**：`LoadSetup` 里为什么要先 `DisconnectDevice()` 再 `closeModes()`，最后重连（[appwindow.cpp:L1200-L1240](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1200-L1240)）？

**答案**：关闭活动模式会触发 `deactivate()` → `setIdle()`，随后每个新模式创建/激活时又可能触发 `initializeDevice()` → 配置设备。如果不先断开，清场重建的一两秒内设备会被反复下发互相矛盾的配置。先断开、重建完按原序列号重连，设备只经历一次「断开 → 重连 → 按最终模式配置」。代码注释原话也点明了这是为了 "prevent excessive and unnecessary configuration of the device"。

**练习 3**：同一个 .setup 文件里可以保存两个都叫 "Spectrum Analyzer" 的 SA 模式实例吗？两个不同名的 SA 实例呢？

**答案**：可以有两个 SA 实例，但名字必须不同。`SaveSetup` 的 `Modes` 数组逐项存 type/name/settings，不限制类型重复；但 `createMode` 之前 GUI 侧有 `nameAllowed()` 查重（重名直接拒绝创建），且 `activeMode` 靠名字定位，所以两个不同名（例如 "SA" 和 "SA-2"）的 SA 实例完全合法——这也是这个多标签页设计的目的：同时保留两套频谱设置来回切换。

## 5. 综合实践

**任务：画出「从点击标签到设备重启测量」的完整时序图，并在三个层面各找到一个证据。**

把本讲三个模块串起来：

1. **UI 层**：运行 GUI，来回切换 VNA/SA/Generator 标签，从日志面板抄下 `Activating mode` / `Deactivated mode` 的输出顺序。
2. **逻辑层**：对照 [modewindow.cpp:L23](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modewindow.cpp#L23)（tabBar → setCurrentIndex）、[modehandler.cpp:L58-L92](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modehandler.cpp#L58-L92)（先 deactivate 后 activate）和 [mode.cpp:L83-L85](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L83-L85) / [L117-L119](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L117-L119)（initializeDevice / setIdle），手绘一张时序图，参与者五列：用户、ModeWindow、ModeHandler、旧 Mode、新 Mode、（设备可画第六列）。
3. **持久层**：切换到某个模式后拖动一个停靠窗口的位置，再切走又切回——观察停靠窗口位置被记住了。对照 [mode.cpp:L92-L100](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L92-L100)（deactivate 时写 QSettings）和 [L60-L81](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L60-L81)（activate 时读回），在图上标注这对读写发生在切换链路的哪个环节。

**验收标准**：时序图上能准确标出（a）deactivate 先于 activate；（b）`setIdle` 在旧模式 deactivate 内、`initializeDevice` 在新模式 activate 内；（c）布局的存/取分别位于 deactivate 开头与 activate 中段。无硬件可完成全部三项（initializeDevice 分支不执行，不影响观察（a）和（c））。

## 6. 本讲小结

- `Mode` 是「一种仪器人格」的抽象，三重继承 `QObject + Savable + SCPINode`，把界面元素管理、SCPI 命令子树、JSON 持久化打包进同一生命周期；构造时自动挂上 SCPI 树，析构时对称摘除。
- 实现一个具体模式需要 6 个必选函数：`getType`、`initializeDevice`、`setAveragingMode`、`preset`（Mode 自身）加 `toJSON`、`fromJSON`（Savable 继承）；`activate()`/`deactivate()` 是 protected，约定子类覆盖时必须调用基类版本。
- 模式切换的铁律在 `ModeHandler`：**先 `deactivate`（存布局、隐藏元素、设备 setIdle）再 `activate`（显元素、恢复布局、设备 initializeDevice）**，同一时刻只有一个活动模式。
- `ModeHandler::createNew()` 是 Type 枚举到具体类的唯一工厂；新增模式类型必须同时扩枚举、`TypeToName`、工厂 switch 和 `SetInitialState` 四处，而 ModeWindow 的菜单因枚举遍历免修改自动生效。
- `ModeWindow` 是纯 UI 的「切换器」（菜单栏角落的 tabBar + "+" 按钮），与 handler 之间靠三个信号双向同步，程序化改动界面时用 `blockSignals` 防反馈循环。
- 工作区持久化以「模式列表」为单位：SaveSetup 逐模式存 type/name/settings + activeMode 名字，LoadSetup 清场重建后按名字恢复活动模式；名字唯一性（`nameAllowed`）是这套机制的前提。

## 7. 下一步学习建议

本讲搞定了 GUI 的「多人格骨架」，下一讲 u2-l3 会向下挖一层：`Savable` 接口背后的完整设置体系——全局 Preferences 与工作区 Setup 的两级持久化如何分工。之后建议两条路线：

- **横向（先走 GUI 数据面）**：进入单元 7/8，看三种模式如何把 UI 设置翻译成 `DeviceDriver` 扫描配置、测量数据如何流入 TraceModel——届时回看本讲的 `initializeDevice()`，你会看到它调用的 `ConfigureDevice()` 全貌。
- **纵向（先走控制面）**：如果你对「模式如何配置硬件」更好奇，可跳到单元 3 的 `devicedriver.h`，理解 `Feature::Generator` 这类能力协商的另一半；远程切换模式的 SCPI `MODE` 命令则在 u10-l2 展开。

建议现在就做一件事巩固：把 4.3.4 的 HelloMode 骨架真的编译跑起来（记得登记 .pro），第一次让自己的类型出现在 "+" 菜单里，是对本讲最好的验收。
