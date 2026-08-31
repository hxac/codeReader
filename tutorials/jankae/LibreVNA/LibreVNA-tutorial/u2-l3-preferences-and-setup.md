# 设置体系：Preferences、Savable 与 JSON Setup

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 LibreVNA-GUI 中**两级持久化**的分工：全局 Preferences（跟随这台电脑的用户）与工作区 Setup（跟随一份 `.setup` 文件）。
2. 解释 `nlohmann::json` 在设置体系中的角色：它是所有可保存对象统一的「中间表示」，`toJSON()`/`fromJSON()` 是进出这个表示的唯一通道。
3. 读懂 `Savable` 接口与 `SettingDescription` 声明式描述表，并能为一个自定义类实现 `toJSON()`/`fromJSON()`。
4. 跟踪一次 `.setup` 文件的保存与加载在 `AppWindow` 中走过的完整代码路径（菜单、命令行 `--setup`、SCPI 三种入口）。
5. 动手修改一个 `.setup` 文件中的参数并重新加载，验证自己对文件格式的理解。

本讲不需要连接任何 LibreVNA 硬件，所有实践都可以在纯 GUI / 纯文本层面完成。

## 2. 前置知识

### 2.1 为什么需要「两级」设置

回忆一下日常使用场景：你希望 GUI **每次启动都自动连接第一台设备**、**SCPI 端口固定为 19542**——这些偏好和「这次测的是一个 1–2 GHz 的滤波器」无关，它们属于**这台电脑的这个用户**。而「VNA 扫 501 个点、Smith 图上有一条 S11 迹线、Marker 1 开着峰值搜索」描述的是一个**测量工作区**，你希望把它存成文件、发给同事、或明天接着用。

LibreVNA 把这两类数据彻底分开：

| 维度 | Preferences（偏好） | Setup（工作区） |
|---|---|---|
| 回答的问题 | 「这个用户希望程序怎么表现」 | 「这次测量长什么样」 |
| 存储介质 | `QSettings`（Linux 下是 `~/.config/LibreVNA/LibreVNA-GUI.conf`） | 用户指定的 `.setup` 文件（JSON 文本） |
| 生命周期 | 程序启动时读、退出时写 | 显式保存/加载，或配置成自动 |
| 主要内容 | 启动行为、图形外观默认值、服务器端口、各驱动全局选项 | 模式列表、每个模式的全部设置、活动模式、参考源状态 |
| 代码入口 | `Preferences` 单例（`preferences.h`） | `AppWindow::SaveSetup()/LoadSetup()`（`appwindow.cpp`） |

### 2.2 nlohmann::json：C++ 的 JSON 中间表示

[nlohmann::json](https://json.nlohmann.me/) 是一个只有单个头文件的现代 C++ JSON 库，LibreVNA 把它直接内置在 [Software/PC_Application/LibreVNA-GUI/json.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/json.hpp) 里（回忆 u1-l3：GUI 的第三方依赖全以源码形式随仓库携带）。

它的用法像写「类 JSON 字面量」：

```cpp
nlohmann::json j;
j["Modes"] = nlohmann::json::array();   // 数组
j["version"] = "1.9.2";                 // 字符串
j["Reference"]["Mode"] = "Internal";    // 自动创建嵌套对象
double f = j["frequency"].get<double>(); // 带类型的读取
```

关键特性（本讲会反复用到）：

- `j["a"]["b"]["c"] = x` 会**自动逐级创建**嵌套对象；
- `j.contains("key")` 判断键是否存在；
- `j.value("key", 默认值)` 在键缺失时返回默认值——这是 `fromJSON()` 实现「向后兼容」的惯用法；
- `file >> j` / `file << j` 直接与流交互，解析失败会抛 C++ 异常；
- `setw(4)` 配合输出流可得到缩进 4 空格的漂亮格式（等价于 Python `json.dumps(indent=4)`）。

### 2.3 Qt 的 QVariant 与 QSettings

- **QVariant** 是 Qt 的「类型擦除容器」：一个 QVariant 可以装 int、double、bool、QString、QColor……并携带类型信息（`metaType().id()`）。设置体系用它实现「一张表统一处理各种类型的设置项」。
- **QSettings** 是 Qt 的键值对持久化 API，按「组织名/应用名」定位存储位置。LibreVNA 在 `main.cpp` 里设定了这两个名字：

  [Software/PC_Application/LibreVNA-GUI/main.cpp:L23-L24](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/main.cpp#L23-L24)：设置组织名为 "LibreVNA"、应用名为 "LibreVNA-GUI"，因此在 Linux 上所有 `QSettings` 数据都落在 `~/.config/LibreVNA/LibreVNA-GUI.conf`（Windows 是注册表、macOS 是 plist，代码不用关心）。

- **单例模式**：`Preferences` 把构造函数设为 private，只通过静态的 `getInstance()` 暴露全局唯一实例，任何代码都能拿到同一份设置。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `Software/PC_Application/LibreVNA-GUI/savable.h` | 定义 `Savable` 接口、`SettingDescription`、`parseJSON()`/`createJSON()` 静态工具 | 整个持久化体系的基石 |
| `Software/PC_Application/LibreVNA-GUI/savable.cpp` | `openFromFileDialog()`/`saveToFileDialog()` 两个文件对话框帮助函数 | JSON 与文件的衔接 |
| `Software/PC_Application/LibreVNA-GUI/Util/qpointervariant.h` | `QPointerVariant`：能「指向任意成员变量并读写」的小工具 | `SettingDescription` 的技术支撑 |
| `Software/PC_Application/LibreVNA-GUI/preferences.h` | `Preferences` 单例：设置树结构体 + 声明式描述表 `descr` | 设置项如何声明 |
| `Software/PC_Application/LibreVNA-GUI/preferences.cpp` | `Preferences` 的 load/store/setDefault/toJSON/fromJSON 与 `PreferencesDialog` | QSettings 与 JSON 两条通道 |
| `Software/PC_Application/LibreVNA-GUI/appwindow.cpp` | `SaveSetup()`/`LoadSetup()` 两个重载及其三种触发入口 | 工作区聚合保存 |
| `Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp` | 一个具体 `Savable` 实现者的手写示例 | 学习如何手写 toJSON/fromJSON |
| `Software/PC_Application/LibreVNA-GUI/mode.cpp` | `Mode::activate()/deactivate()` 中对 QSettings 的使用 | 第三类零散持久化（布局） |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**Savable 接口**、**Preferences 系统**、**Setup 保存/加载**。它们的关系可以用一句话概括：`Savable` 定义契约，`Preferences` 与各 `Mode` 都是契约的实现者，而 `AppWindow` 把所有 Mode 的 JSON 聚合成 `.setup` 文件。

### 4.1 Savable 接口

#### 4.1.1 概念说明

GUI 里有大量「需要把自己存起来、再原样恢复」的对象：三种测量模式、信号源控件、校准件、去嵌入选项……如果每个对象各自发明一套文件格式，代码会迅速失控。`Savable` 用两个纯虚函数规定了统一契约：

- `toJSON()`：把当前状态序列化成一棵 `nlohmann::json` 树；
- `fromJSON(j)`：从一棵 JSON 树恢复状态。

这样「对象 ↔ JSON」的转换知识归属对象自己，而「JSON ↔ 磁盘」的通用逻辑（文件对话框、打开文件、解析异常处理、缩进写出）可以只写一遍。这就是典型的**序列化中间表示**设计：JSON 是所有对象的公共语言。

注意 `Savable` 并不规定「存到哪」。同一个类的 JSON 既可以写进 `.setup` 工作区文件，也可以写进别的文件——存储位置由调用者决定。

#### 4.1.2 核心流程

**写盘路径**（`saveToFileDialog`）：

```text
用户点「保存」
  └─ QFileDialog 选文件名（可自动补扩展名）
      └─ ofstream 打开文件
          └─ file << setw(4) << toJSON()    ← 对象自己序列化，缩进 4 空格
```

**读盘路径**（`openFromFileDialog`）：

```text
用户点「打开」
  └─ QFileDialog 选文件
      └─ ifstream 打开文件
          └─ file >> j            ← 流式解析，语法错误抛异常
              └─ 异常？→ InformationBox 报错并放弃
              └─ 正常？→ fromJSON(j)        ← 对象自己恢复状态
```

**声明式描述表的运作**：对于「一堆标量成员」这类最常见的场景，`Savable` 还提供了免手写的捷径——`SettingDescription` 表。每个条目是一个三元组：

```text
(成员变量地址, "点分路径名", 默认值)
```

`createJSON(descr)` 把表变成嵌套 JSON 树：名字按 `.` 切分，逐级下钻创建对象。例如名字 `"Graphs.SweepIndicator.triangle"` 会生成：

```json
{ "Graphs": { "SweepIndicator": { "triangle": true } } }
```

`parseJSON(j, descr)` 反向执行：按同样的路径下钻取值，根据**目标成员变量的实际类型**（通过 `metaType().id()` 查询）转换后写回。若路径中途断掉（文件里没这一项），打一条 `qWarning` 并把成员设为默认值——所以「新版本程序读旧版本文件」不会崩溃，缺的项自动回退。

#### 4.1.3 源码精读

**契约本身**只有寥寥数行：

[savable.h:L14-L22](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/savable.h#L14-L22)：`Savable` 抽象类声明两个纯虚函数 `toJSON()`/`fromJSON()`，外加两个已实现的文件对话框帮助函数。任何想被保存的类继承它并实现这两个函数即可。

[savable.cpp:L38-L55](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/savable.cpp#L38-L55)：`saveToFileDialog()` 弹出保存对话框、自动补扩展名，然后用 `file << setw(4) << toJSON() << endl` 写出缩进格式化的 JSON。注意序列化这一步完全委托给了虚函数——本函数对「对象里有什么」一无所知。

[savable.cpp:L24-L34](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/savable.cpp#L24-L34)：`openFromFileDialog()` 中 `file >> j` 被 try/catch 包住：文件不是合法 JSON 时 nlohmann 抛异常，这里捕获后用 `InformationBox::ShowError` 弹窗并放弃加载，程序不会崩溃。成功则调用 `fromJSON(j)`。

**声明式描述表**：

[savable.h:L24-L30](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/savable.h#L24-L30)：`SettingDescription` 保存三个字段——`var`（指向目标成员变量的 `QPointerVariant`）、`name`（点分路径字符串）、`def`（默认值）。

[savable.h:L31-L49](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/savable.h#L31-L49)：`parseJSON()` 的路径下钻循环：把名字按 `.` 切开后逐级 `contains(key)` 检查并进入子对象；任何一级缺失就标记 `entry_exists = false`，随后（L45-L49）打警告并把默认值写入成员变量。这就是「文件缺项 → 用默认值补齐」的向后兼容机制。

[savable.h:L53-L71](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/savable.h#L53-L71)：类型分派 switch。nlohmann::json 不认识 Qt 类型，所以这里按目标成员的 `QMetaType` 逐个处理：`Double/Int/UInt/LongLong/Bool/QString` 直接 `get<T>()`；`QColor` 特殊处理——JSON 里存的是 `QColor::name()` 产生的 `"#rrggbb"` 字符串，读回时用 `QColor(s)` 构造。遇到未实现的类型直接抛 `std::runtime_error`（这会在 `Preferences::set()` 里被捕获，见 4.2.3）。

[savable.h:L74-L97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/savable.h#L74-L97)：`createJSON()` 是镜像操作：同样按 `.` 下钻，但用的是 `json_entry = &(*json_entry)[key]`——nlohmann 的 `operator[]` 在键不存在时会**自动创建子对象**，因此不需要 exists 检查。末尾按类型把 QVariant 值写进叶子节点。

**支撑工具 QPointerVariant**：

[qpointervariant.h:L6-L28](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/qpointervariant.h#L6-L28)：`QPointerVariant` 同时记住「成员变量的原始内存地址 `ptr`」和「一个记录了类型的 QVariant 模板」。关键在 `setValue()`（L11-L20）：先把传入值转换为目标类型，再用 `QMetaType::construct(ptr, variant.constData())` **把数据直接构造到目标地址上**——即直接改写宿主对象的成员变量。`value()` 则反过来从 `ptr` 处读一份 QVariant 出来。正是这个「指哪写哪」的能力，让 `parseJSON()` 一张表就能驱动上百个成员变量的读写。

**一个手写实现的对照示例**（不用描述表、直接操作 JSON 的风格）：

[signalgenwidget.cpp:L152-L166](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L152-L166)：`SignalgeneratorWidget::toJSON()` 手工构造 JSON：顶层写 `frequency`/`power`/`port` 三个键，再把扫描参数收进嵌套对象 `sweep`（含 `span/steps/dwell/enabled`）。

[signalgenwidget.cpp:L168-L183](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L168-L183)：`fromJSON()` 全部使用 `j.value("key", 当前值)` 的写法——**键缺失时保持当前值不变**，这与 `SettingDescription` 的「缺失用默认值」略有不同（这里用运行时值当默认，更宽松）。注意 `sweep` 子对象先 `contains` 再处理，完全缺失时显式关闭扫描开关。

两种风格的取舍：描述表适合「几十上百个扁平标量」（如 Preferences），一行一个条目、自动双向；手写适合「结构复杂、有嵌套语义、需要联动逻辑」的对象（如含 sweep 子结构的信号源控件）。

#### 4.1.4 代码实践

**实践目标**：不写代码，通过「预测 → 验证」掌握 `SettingDescription` 与 JSON 的双向映射。

**操作步骤**：

1. 打开 [preferences.h:L256-L292](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.h#L256-L292)，任选 3 条描述表条目，例如 `{&Startup.DefaultSweep.points, "Startup.DefaultSweep.points", 501}`。
2. 在纸上画出每条对应的 JSON 嵌套结构（提示：名字里有几个 `.` 就有几层嵌套）。
3. 对照 4.1.3 讲的 `createJSON()` 逻辑，回答：这条会生成 `{"Startup": {"DefaultSweep": {"points": 501}}}` 中的哪一部分？
4. 反向思考：如果 JSON 文件里 `Startup` 键存在但 `DefaultSweep` 不存在，`parseJSON()` 走到哪一行放弃？成员变量最终是什么值？（答案见 savable.h L36-L49。）

**需要观察的现象**：自己画的树与步骤 3 的推导一致；能准确说出缺项时的回退行为。

**预期结果**：能对任意一条描述表条目默写出它的 JSON 路径。本实践为纯源码阅读，无需运行程序。

#### 4.1.5 小练习与答案

**练习 1**：`parseJSON()` 遇到文件中缺失的条目时会抛异常吗？行为是什么？

<details><summary>答案</summary>

不会。它打一条 `qWarning() << "Entry" << e.name << "not present in file"`，然后把默认值 `e.def` 写入成员变量并 `continue`（[savable.h:L45-L50](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/savable.h#L45-L50)）。这保证了旧文件读入新版本程序时的兼容性。
</details>

**练习 2**：为什么 `createJSON()` 里不需要像 `parseJSON()` 那样做 `contains` 检查？

<details><summary>答案</summary>

因为方向不同。写入时 `(*json_entry)[key]` 的 `operator[]` 在键不存在时会自动创建子对象（[savable.h:L79-L81](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/savable.h#L79-L81)）；而读取时对不存在的键使用 `operator[]` 是未定义/错误行为，必须先 `contains` 确认（[savable.h:L38-L44](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/savable.h#L38-L44)）。
</details>

**练习 3**：`QColor` 是 Qt 类型，nlohmann::json 并不认识它，设置体系是如何存的？

<details><summary>答案</summary>

存成 `QColor::name()` 的 `"#rrggbb"` 字符串。写入在 [savable.h:L91](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/savable.h#L91)，读回在 [savable.h:L64-L68](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/savable.h#L64-L68) 用 `QColor(s)` 从字符串构造。
</details>

### 4.2 Preferences 系统

#### 4.2.1 概念说明

`Preferences` 是全局偏好的**唯一权威来源**（single source of truth）。它的设计有三个要点：

1. **单例**：构造函数 private，静态 `getInstance()` 返回同一实例，全程序任何角落读到的都是同一份数据。
2. **双通道持久化**：
   - 常规通道是 `QSettings`（`load()`/`store()`），程序启动时读、正常退出时写，用户无感；
   - 备份通道是 JSON 文件（`toJSON()`/`fromJSON()`），对应偏好对话框里的 Save/Open 按钮，产出 `.vnapref` 文件，用于在机器之间搬迁配置。
3. **声明式设置项**：约 160 个设置项不是散落在代码里的 `settings.value(...)` 调用，而是集中一张 `descr` 表（`SettingDescription` 向量）。增删一个设置项 = 增删一行表项 + 一个结构体成员。

设置内容按主题分组为多个嵌套结构体：`Startup`（启动行为与默认扫描参数）、`Acquisition`（采集行为）、`Graphs`（图形外观，含每 种 Y 轴的默认量程）、`Marker`（游标默认行为）、`SCPIServer`/`StreamingServers`（网络服务端口）、`Debug`、`UISettings`（各文件对话框记住的路径等）。

此外 `Preferences` 还为**设备驱动**留了扩展点：每个 `DeviceDriver` 可以通过 `driverSpecificSettings()` 贡献自己的设置项，`Preferences` 在 load/store/toJSON/fromJSON 时都会遍历所有驱动一并处理——所以偏好对话框里会出现 "Device Drivers" 分组（见 [preferences.cpp:L158-L170](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L158-L170)，为每个驱动的 `createSettingsWidget()` 动态加页面）。

#### 4.2.2 核心流程

**启动加载**（AppWindow 构造函数早期，见 u2-l1 的装配顺序）：

```text
AppWindow 构造
  └─ 命令行带 --reset-preferences？
       ├─ 是 → Preferences::setDefault()   （全部设回默认值）
       └─ 否 → Preferences::load()         （从 QSettings 读入）
```

**正常退出保存**：

```text
AppWindow::closeEvent
  └─ （若启用 setup 自动保存，先 SaveSetup，见 4.3）
  └─ modeHandler->shutdown() / deactivate   ← 模式布局写入 QSettings
  └─ pref.store()                            ← 偏好写回 QSettings
```

**用户编辑偏好**：

```text
菜单 Edit→Preferences（actionPreferences）
  └─ p.edit() → new PreferencesDialog → dialog->exec()   （模态对话框）
       ├─ RestoreDefaults → p->setDefault() + 界面刷新
       ├─ OK / Apply → updateFromGUI() + emit p->updated()
       ├─ Save  → .vnapref 文件（JSON 通道，setw(1) 紧凑格式）
       └─ Open  → 读 .vnapref → p->fromJSON(j) + emit updated()
  └─ AppWindow::preferencesChanged() 响应 updated() 信号
```

注意 `emit p->updated()` 这个 Qt 信号：设置改变后，所有关心它的对象（TCP 服务器、流式服务器、图形控件……）通过连接该信号自行刷新，`Preferences` 不需要认识它们——典型的观察者模式解耦。

#### 4.2.3 源码精读

**类骨架与单例**：

[preferences.h:L49-L68](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.h#L49-L68)：`Preferences` 同时继承 `QObject`（为了 `updated()` 信号）和 `Savable`（为了 JSON 通道）；拷贝构造被 `= delete`，实例在 [preferences.cpp:L20](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L20) 以静态成员方式创建一次。

[preferences.h:L70-L104](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.h#L70-L104)：`Startup` 结构体是「设置树」的缩影——布尔开关、字符串、数值、再嵌套 `DefaultSweep`/`Generator`/`SA` 子结构体。这些成员就是 `descr` 表中 `QPointerVariant` 指向的目标。

**声明式描述表**：

[preferences.h:L256-L292](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.h#L256-L292)：`descr` 表的开头部分。每一行 `{&成员地址, "点分键名", 默认值}` 同时完成三件事：声明持久化键、绑定内存位置、给定默认值。例如 `Startup.ConnectToFirstDevice` 默认 `true`（连接第一台设备）、`Startup.DefaultSweep.points` 默认 `501`。整张表一直延续到 L418。

**QSettings 通道**：

[preferences.cpp:L499-L509](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L499-L509)：`load()` 先加载自己的 `descr`，再遍历所有设备驱动加载它们的 `driverSpecificSettings()`。

[preferences.cpp:L511-L521](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L511-L521)：具体读取就是一个循环：`d.var.setValue(settings.value(d.name, d.def))`——**键名就是点分路径本身**，所以 Linux 下打开 `~/.config/LibreVNA/LibreVNA-GUI.conf` 能看到 `Startup\DefaultSweep\points=501` 这样的条目（QSettings 把 `.` 存为节分隔符）。键不存在时 `settings.value` 的第二参数提供默认值。

[preferences.cpp:L523-L539](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L523-L539)：`store()` 镜像写回：`settings.setValue(d.name, d.var.value())`。

**启动时的入口**：

[appwindow.cpp:L99-L103](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L99-L103)：AppWindow 构造函数中，`--reset-preferences` 分支调用 `setDefault()`，否则 `load()`。这发生在任何其他初始化（TCP 服务器、界面装配）之前——偏好是后续决策的输入。

[appwindow.cpp:L289](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L289)：`closeEvent()` 末尾的 `pref.store()` 是 QSettings 通道的写回点（回忆 u2-l1：无头模式下靠 SIGINT → `tryExitGracefully` → `closeEvent` 保证这里也会执行）。

**JSON 备份通道（.vnapref）**：

[preferences.cpp:L574-L581](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L574-L581) 与 [preferences.cpp:L596-L605](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L596-L605)：`fromJSON()`/`toJSON()` 复用 `Savable::parseJSON`/`createJSON` 处理自家 `descr`，再遍历驱动设置。由于多个驱动的键可能与主表产生嵌套冲突，`toJSON()` 用辅助函数 `merge()`（[preferences.cpp:L583-L594](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L583-L594)）做深度合并：两个 JSON 各自 `flatten()` 成点分单层键值、后者覆盖前者、再 `unflatten()` 还原成嵌套树。

[preferences.cpp:L205-L218](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L205-L218)：偏好对话框 Save 按钮——弹出保存对话框（默认扩展名 `.vnapref`，记住上次目录到 `UISettings.Paths.pref`），`updateFromGUI()` 先把界面值刷进结构体，再 `file << setw(1) << p->toJSON()` 写出**紧凑格式**（对比 `.setup` 文件的 `setw(4)`）。Open 按钮在 [preferences.cpp:L219-L232](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L219-L232)：读文件 → `p->fromJSON(j)` → 刷新界面 → `emit updated()`。

**对话框与结构体的桥接**：

[preferences.cpp:L188-L204](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L188-L204)：RestoreDefaults 按钮先确认再 `p->setDefault()`；OK/Apply 都调用 `updateFromGUI()` 后 `emit p->updated()`。

[preferences.cpp:L374-L398](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L374-L398)：`updateFromGUI()` 的开头——逐控件把界面状态抄进 `p->Startup.*` 结构体成员（函数一直延续到 L492，覆盖全部分组）。`setInitialGUIState()`（L245 起）则是启动时反方向的抄写。这是典型的「对话框 ↔ 数据」手工桥接代码，量大但直白。

**按名读写（SCPI 支撑）**：

[preferences.cpp:L607-L642](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L607-L642)：`set(name, value)` 在 `descr` 与所有驱动设置里按名字查找，找到则通过 `QPointerVariant::setValue` 写入（类型不匹配抛的 `runtime_error` 在此被捕获并返回 false）。这为 SCPI 命令 `:DEVice:PREFerence SET` 提供了通用后端——远程脚本可以按点分键名改任何偏好（对应的 `get()` 在 L644-L660）。

**第三类零散持久化——界面布局**：除偏好与工作区外，还有一类小数据直接走 QSettings：模式专属工具栏/停靠窗口的布局。回忆 u2-l2 的切换铁律：

[mode.cpp:L90-L100](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L90-L100)：`Mode::deactivate()` 把本模式的 `windowState`、各 dock/toolbar 可见性写入 QSettings（键名带模式名前缀）；[mode.cpp:L60-L75](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L60-L75)：`activate()` 恢复。主窗口几何同理存在 `geometry` 键下（[appwindow.cpp:L185-L188](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L185-L188) 与 [appwindow.cpp:L277-L278](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L277-L278)）。这类数据不适合放 JSON（是二进制 `QByteArray`），QSettings 恰好擅长。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「QSettings 通道」的存储位置与键名格式。

**操作步骤**：

1. 启动 GUI（无硬件亦可），`Edit → Preferences`，把 SCPI 端口从默认 19542 改成 19543，点 OK，然后正常退出程序。
2. 在终端查看配置文件（Linux）：

   ```bash
   grep -n "port" ~/.config/LibreVNA/LibreVNA-GUI.conf | head
   ```

3. 对照 [preferences.h:L387-L388](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.h#L387-L388) 的两条描述表条目，确认文件里出现了 `SCPIServer\port=19543`。
4. 再次启动 GUI，验证端口记住的是 19543（说明 `load()` 生效）。
5. 附加实验：用 `--reset-preferences` 启动一次（[appwindow.cpp:L99-L100](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L99-L100)），观察端口是否回到 19542；再正常退出，回头检查 conf 文件是否被 `store()` 覆盖。

**需要观察的现象**：conf 文件中的键名与 `descr` 表的点分路径一一对应；`--reset-preferences` 只影响本次运行的内存值，退出时才写回文件。

**预期结果**：能列出「改一个偏好 → 文件中哪个键变化」的完整因果链。本实践需要本地运行 GUI，当前讲义编写环境未执行，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`--reset-preferences` 之后 conf 文件里的旧值会立刻消失吗？

<details><summary>答案</summary>

不会立刻消失。[appwindow.cpp:L99-L103](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L99-L103) 只调用了 `setDefault()` 把**内存中**的成员设回默认值；持久化文件要等程序退出时 `closeEvent()` 里的 `pref.store()`（[appwindow.cpp:L289](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L289)）才被覆盖。
</details>

**练习 2**：`Preferences::store()` 和 `Preferences::toJSON()` 都能导出设置，区别是什么？

<details><summary>答案</summary>

`store()` 写 QSettings（平台相关键值存储，程序自动加载，见 [preferences.cpp:L532-L539](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L532-L539)）；`toJSON()` 产出 `nlohmann::json` 树，由偏好对话框的 Save 按钮写成 `.vnapref` 文件用于跨机器备份（[preferences.cpp:L205-L218](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L205-L218)）。前者是常规通道，后者是备份通道。
</details>

**练习 3**：为什么 `Preferences::toJSON()` 需要 `merge()`，而 `parseJSON()` 不需要？

<details><summary>答案</summary>

`createJSON()` 每次从空树开始构建，主表和某驱动的表各自生成的树可能在同一前缀下都有内容（例如都以 `LibreVNADriver.` 开头），直接赋值会互相覆盖整棵子树，所以写入侧要 `flatten()/unflatten()` 深合并（[preferences.cpp:L583-L605](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L583-L605)）。读取侧是对同一棵完整的 j 分别按各自路径取值，天然只读不写，不存在覆盖问题。
</details>

### 4.3 Setup 保存/加载

#### 4.3.1 概念说明

`.setup` 文件是**工作区的快照**：它保存的不是「程序怎么表现」，而是「现在有哪些模式、每个模式配置成什么样、哪个模式处于活动状态、参考源怎么设置」。它的核心特点：

1. **聚合者不是某个 Savable 对象，而是 AppWindow**。单个 Mode 只知道自己那棵子树，把所有 Mode 的子树拼成一个文件的活儿由 `AppWindow::SaveSetup()` 完成。注意 `AppWindow` 并没有继承 `Savable`——聚合逻辑足够特殊（要遍历 ModeHandler、处理兼容格式），手写比套接口更清晰。`Savable` 的两个文件对话框帮助函数主要服务于「单一对象自我存取」的场景（如校准文件）。
2. **多种触发入口共享同一实现**：菜单、命令行 `--setup`、SCPI 远程命令、启动自动加载、退出自动保存，最终都收敛到 `SaveSetup()/LoadSetup()` 两个函数。
3. **JSON 是人可读的中间格式**，这带来一个工程红利：可以用任何脚本语言生成或修改工作区，实现「参数扫描」「批量配置」等自动化（本讲综合实践就要利用这一点）。

#### 4.3.2 核心流程

**保存**（`AppWindow::SaveSetup()`）构造出的 JSON 顶层结构：

```json
{
    "Modes": [
        {
            "type": "Vector Network Analyzer",
            "name": "Vector Network Analyzer",
            "settings": { "...": "该模式自己的 toJSON() 结果" }
        },
        {
            "type": "Signal Generator",
            "name": "Signal Generator",
            "settings": {
                "frequency": 1000000000.0,
                "power": -10.0,
                "port": 0,
                "sweep": { "span": 0, "steps": 0, "dwell": 0, "enabled": false }
            }
        }
    ],
    "activeMode": "Vector Network Analyzer",
    "Reference": { "Mode": "Internal", "Output": "Off" },
    "version": "1.9.2"
}
```

（`type` 字符串来自 `Mode::TypeToName()`，见 [mode.cpp:L123-L131](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L123-L131)；Signal Generator 的 settings 内容即 4.1.3 引用的 `SignalgeneratorWidget::toJSON()` 输出。）

**加载**（`AppWindow::LoadSetup(j)`）的重建流程：

```text
读取 Reference（若存在）
  └─ 记住当前设备序列号并 DisconnectDevice     ← 防止重建模式时反复配置硬件
      └─ modeHandler->closeModes()             ← 清空现有全部模式
          └─ 兼容旧格式：顶层若有 "VNA"/"Generator"/"SpectrumAnalyzer" 键
          │    → 各创建一个模式并 fromJSON(对应子树)
          └─ 新格式：遍历 "Modes" 数组
               → TypeFromName(type) → createMode(name, type) → m->fromJSON(settings)
      └─ 若之前有设备 → ConnectToDevice(原序列号)  ← 重新接上
      └─ 按 "activeMode" 名字找到并激活对应模式
```

**五种触发入口**：

| 入口 | 代码位置 | 说明 |
|---|---|---|
| 菜单 File→Save setup / Load setup | [appwindow.cpp:L231-L248](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L231-L248) | 文件对话框；把目录记入 `UISettings.Paths.setup` |
| 命令行 `--setup <file>` | [appwindow.cpp:L202-L204](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L202-L204) | 启动时加载，优先于模式默认创建 |
| SCPI `:DEVice:SETUP:SAVE <file>` / `:DEVice:SETUP:LOAD <file>` | [appwindow.cpp:L602-L622](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L602-L622) | 远程脚本切换工作区 |
| 启动自动加载 | [appwindow.cpp:L296-L308](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L296-L308) | `Startup.UseSetupFile` 为真时 `SetInitialState()` 改为 `LoadSetup(Startup.SetupFile)` |
| 退出自动保存 | [appwindow.cpp:L273-L275](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L273-L275) | `UseSetupFile && AutosaveSetupFile` 时 `closeEvent()` 自动保存 |

#### 4.3.3 源码精读

[appwindow.cpp:L1109-L1120](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1109-L1120)：`SaveSetup(filename)`——自动补 `.setup` 后缀、`ofstream` 打开、`file << setw(4) << SaveSetup() << endl` 把聚合 JSON 以 4 空格缩进写出，最后在状态栏标签 `lSetupName` 上显示当前 setup 文件名，让用户随时知道自己在哪个工作区。

[appwindow.cpp:L1122-L1144](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1122-L1144)：`SaveSetup()`（JSON 版重载）是聚合的核心：遍历 `modeHandler->getModes()`，每个模式产出 `{type, name, settings}` 三键对象压入数组；随后写入 `activeMode`、参考源工具栏状态（`toolbars.reference` 两个下拉框的当前文本）和 `version`（取自 `qlibrevnaApp->applicationVersion()`，即 u1-l3 提到的由 qmake 注入的版本宏）。注意每个模式的 `settings` 完全来自虚函数 `m->toJSON()`——AppWindow 不需要知道 VNA 模式存了什么。

[appwindow.cpp:L1146-L1168](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1146-L1168)：`LoadSetup(filename)`——打开文件、`file >> j` 解析（同样的 try/catch + `InformationBox::ShowError` 模式，与 `Savable::openFromFileDialog` 如出一辙），成功后转交 `LoadSetup(j)` 并更新状态栏标签。

[appwindow.cpp:L1191-L1205](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1191-L1205)：`LoadSetup(j)` 开头——先恢复参考源设置（用 `j["Reference"].value("Mode", "Internal")` 带默认值的读法），然后一个重要细节：**先取设备序列号、断开设备**。代码注释写明原因：接下来要删除并重建所有模式，若保持连接，每个模式创建/销毁过程都会触发一次设备配置，纯属浪费。加载完成后（L1238-L1240）再按原序列号重连。

[appwindow.cpp:L1209-L1225](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1209-L1225)：**旧格式兼容分支**。早期版本的 setup 文件把模式设置放在顶层 `VNA`/`Generator`/`SpectrumAnalyzer` 键下（且每种至多一个实例）。这段代码保证旧文件仍能加载：检测到顶层键就创建对应模式并 `fromJSON` 子树。向后兼容正是依靠 `contains()` 探测 + `value(key, 默认)` 的宽松读取风格实现的。

[appwindow.cpp:L1226-L1235](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1226-L1235)：**新格式主循环**。遍历 `Modes` 数组，`Mode::TypeFromName()` 把字符串翻译回类型枚举（无法识别时返回 `Type::Last`，被跳过——这是对未知模式类型的防御），`createMode(name, type)` 走 u2-l2 讲过的 ModeHandler 工厂，最后 `m->fromJSON(jm["settings"])` 恢复模式状态。

[appwindow.cpp:L296-L308](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L296-L308)：`SetInitialState()` 的分叉——`Startup.UseSetupFile` 为真时整个初始状态来自 setup 文件（工作区优先）；否则创建默认的 VNA/SG/SA 三个模式并激活 VNA（偏好里的 `DefaultSweep` 等默认值此时才派上用场）。这体现了两级设置的协作：Preferences 决定「启动时用不用 setup 文件」，Setup 决定「工作区内容」。

[appwindow.cpp:L602-L622](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L602-L622)：SCPI 侧的 `SETUP` 命令节点。`SAVE` 是命令（无返回查询回调，参数即文件名，直接调用 `SaveSetup(params[0])`）；`LOAD` 注册为查询形式，用 `SCPI::Result::True/False` 报告成败。远程自动化切工作区的正门就在这里（配合 u10 单元的 TCP 服务器使用）。

#### 4.3.4 代码实践

**实践目标**：验证「setup 文件里的一个数字 → GUI 里的一个旋钮」的映射。

**操作步骤**：

1. 启动 GUI（无硬件可），切到 Signal Generator 模式，把输出电平设为一个好认的值（如 -7 dBm）。
2. `File → Save setup` 保存为 `test.setup`。
3. 用文本编辑器打开 `test.setup`，在 `Modes` 数组中找到 `"type": "Signal Generator"` 的那一项，其 `settings` 里应有 `"power": -7.0`（对应 [signalgenwidget.cpp:L157](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L157) 的 `j["power"] = s.dBm`）。
4. 回到源码回答：这个 `-7.0` 从屏幕上的 SpinBox 到文件，依次经过了 `updateFromGUI` 风格的控件读取 → `getDeviceStatus()` → `toJSON()` → `SaveSetup()` 聚合 → `setw(4)` 写盘。把这条链上每个函数名抄下来。
5. 把文件中 `"power"` 手工改成 `-3.5`，保存；回到 GUI `File → Load setup` 重新加载，观察信号源电平是否变成 -3.5 dBm。

**需要观察的现象**：加载后电平与手改的 JSON 值一致；状态栏出现 `Setup: test.setup`（[appwindow.cpp:L1166](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1166)）。

**预期结果**：确认 `.setup` 是纯文本、可直接编辑的工作区快照。需要本地运行 GUI，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`.setup` 文件顶层的 `version` 字段有什么用？

<details><summary>答案</summary>

它记录保存工作区时的应用程序版本（[appwindow.cpp:L1142](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1142)）。当前 `LoadSetup` 并未拿它做版本拦截，真正的兼容手段是「旧格式顶层键分支 + `contains()/value()` 宽松读取」；但该字段为将来按版本迁移格式留下了信息。
</details>

**练习 2**：为什么 `LoadSetup(j)` 在重建模式前要先 `DisconnectDevice()`、结束后再重连？

<details><summary>答案</summary>

见 [appwindow.cpp:L1200-L1205](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1200-L1205) 的注释：删除和重建模式的过程中，各模式会对所连设备做多余的、不必要的重复配置。先断开可避免这些浪费；记下序列号再重连，对用户而言设备连接无缝保持。
</details>

**练习 3**：如果一份 `.setup` 文件里某个模式的 `"type"` 是 `"TDR Analyzer"`（程序不认识的类型），加载会发生什么？

<details><summary>答案</summary>

`Mode::TypeFromName()` 遍历枚举匹配失败，返回 `Type::Last`（[mode.cpp:L133-L141](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L133-L141)）；[appwindow.cpp:L1229](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1229) 判断 `type != Mode::Type::Last` 不成立，直接跳过这一项，其余模式照常加载，不会崩溃。
</details>

## 5. 综合实践

**任务**：完整走一遍「GUI 保存 → 人工对照源码解读 → 脚本修改 → 重新加载验证」的闭环，把本讲三个模块的知识串起来。

**步骤**：

1. **准备**：按 u1-l3 编译并启动 GUI（无需硬件）。在偏好对话框 `Startup` 页确认当前是「默认值」模式（不要用 setup 文件启动），避免与后续实验互相干扰。
2. **制造可辨识的状态**：切换到 Signal Generator 模式，设置频率 2 GHz、电平 -5 dBm；再切到 VNA 模式，把扫描点数改成 301。
3. **保存**：`File → Save setup` 存为 `play.setup`。
4. **解读**：用文本编辑器打开 `play.setup`，对照 [appwindow.cpp:L1122-L1144](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L1122-L1144) 逐项核对顶层四个键（`Modes`/`activeMode`/`Reference`/`version`）都在；再找到 Signal Generator 项里的 `frequency: 2e9` 与 `power: -5.0`（来源：[signalgenwidget.cpp:L152-L166](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L152-L166)）。写下一张「我改过的 UI 值 → JSON 键路径」对照表。
   - 思考题：VNA 模式 `settings` 里的点数存在哪个键？提示：答案要靠阅读 `VNA/vna.cpp` 的 `toJSON()` 找到，而不是猜——如果暂时不读，就在表里标「待确认」。
5. **脚本修改**：用下面的 Python 脚本（示例代码，非项目原有）把信号源频率改掉：

   ```python
   #!/usr/bin/env python3
   # modify_setup.py —— 修改 .setup 文件中信号源模式的频率（示例代码）
   import json

   PATH = "play.setup"
   with open(PATH) as f:
       j = json.load(f)

   for mode in j["Modes"]:
       if mode["type"] == "Signal Generator":
           mode["settings"]["frequency"] = 3e9
           print("updated:", mode["settings"]["frequency"])

   with open(PATH, "w") as f:
       json.dump(j, f, indent=4)   # 与 C++ 侧 setw(4) 一致的缩进
   ```

6. **验证**：GUI 中 `File → Load setup` 重新加载 `play.setup`，确认信号源频率变为 3 GHz、电平仍是 -5 dBm（脚本没动的键应原样保留）。若愿意，可进一步用命令行 `./LibreVNA-GUI --setup play.setup` 从启动就进入这个工作区（[appwindow.cpp:L202-L204](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L202-L204)）。
7. **收尾思考**：这个闭环正是「JSON 中间表示」的价值——你没用一行 C++ 就改写了程序状态。想一想：如果要把 11 GHz 改成「1 到 6 GHz 等步进 6 份」批量生成 6 个 setup 文件分别加载测量，脚本该怎么扩展？（提示：`json.load` 之后循环改 VNA 设置、`json.dump` 到不同文件名即可。）

**预期结果**：一张完整的 UI↔JSON 对照表 + 一次成功的脚本改写验证。步骤 3–6 需要本地运行 GUI，**待本地验证**。

## 6. 本讲小结

- LibreVNA 的持久化分两级：**Preferences**（全局偏好，`QSettings` 自动存取，位于 `~/.config/LibreVNA/LibreVNA-GUI.conf`，另有 `.vnapref` JSON 备份通道）与 **Setup**（工作区快照，用户可见的 `.setup` JSON 文件）；此外还有第三类零散数据（模式布局、窗口几何）直接走 QSettings。
- `Savable` 接口用 `toJSON()`/`fromJSON()` 两个纯虚函数把「对象 ↔ JSON」的知识封装在各对象内部；`nlohmann::json` 是所有持久化数据的统一中间表示。
- `SettingDescription` + `QPointerVariant` 提供**声明式**双向映射：一行 `{&成员, "点分路径", 默认值}` 同时定义存储键、内存位置和回退值；`parseJSON()` 遇缺项自动补默认值，`createJSON()` 靠 `operator[]` 自动建嵌套树，这是向后兼容的基础。
- `Preferences` 是单例 + 声明式描述表（`descr`，约 160 项）+ 观察者（`updated()` 信号），并为设备驱动留有 `driverSpecificSettings()` 扩展点；`--reset-preferences` 只改内存，退出时 `store()` 才落盘。
- `.setup` 文件由 `AppWindow::SaveSetup()` 聚合：`Modes` 数组（每项 `type/name/settings`）+ `activeMode` + `Reference` + `version`；加载时先断开设备、清空并按文件重建模式，兼容旧版顶层键格式。
- setup 有五种触发入口（菜单、`--setup` 命令行、SCPI `:DEVice:SETUP:SAVE/LOAD`、启动自动加载、退出自动保存），全部收敛到同一对函数；文件是人可读 JSON，可用脚本直接生成/修改，是自动化配置的正门。

## 7. 下一步学习建议

- **下一讲 u3-l1（DeviceDriver 抽象）**：本讲两次遇到 `DeviceDriver::getDrivers()`（偏好对话框动态加页面、偏好加载驱动设置），下一讲正面拆解这个驱动抽象接口。
- 若想继续深挖本讲主题，建议阅读：
  - `VNA/vna.cpp` 的 `toJSON()`/`fromJSON()`——最复杂的 Savable 实现者（扫描设置、迹线、校准、去嵌入全都入 JSON），是理解「一个模式到底由哪些状态构成」的最佳索引；
  - `Calibration/calkit.cpp`——`Savable::saveToFileDialog/openFromFileDialog` 帮助函数的典型使用者（校准件套件文件）；
  - `appwindow.cpp` 的 `SetupSCPI()`（L560 附近起）——看 `:DEVice:PREFerence SET/GET` 与 `:DEVice:SETUP:SAVE/LOAD` 如何把本讲的两级设置暴露给远程脚本，为 u10 单元的 SCPI 专题预热。
