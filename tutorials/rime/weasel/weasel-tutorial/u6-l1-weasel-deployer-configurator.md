# WeaselDeployer 配置器

## 1. 本讲目标

学完本讲，读者应该能够：

- 说清 `WeaselDeployer.exe` 这个独立进程在 Weasel 架构中扮演的角色，以及它为什么必须是「另一个进程」而不是 `WeaselServer` 的一部分。
- 理解 librime 的「levers」模块与 `rime_levers_api`：它如何用 `.custom.yaml` 覆盖层来管理用户配置，又如何加载、保存这些覆盖。
- 掌握三类设置对话框（方案切换 `SwitcherSettingsDialog`、UI 样式 `UIStyleSettingsDialog`、用户词典 `DictManagementDialog`）各自写入哪个文件、调用哪些 levers 接口。
- 跟踪 `/deploy`、`/sync`、`/dict` 三种命令行模式的触发链路，理解它们都遵循「申请维护模式 → 操作用户数据 → 恢复服务」的统一骨架。

本讲是「部署、安装与系统集成」单元的第一讲，承接 u1-l2（解决方案结构）中学过的「WeaselDeployer 是一个独立的设置/部署 EXE」，向下打通 u4（RimeWithWeaselHandler 维护模式）与 u2（IPC 客户端）。

## 2. 前置知识

在进入源码前，先建立两个直觉。

### 2.1 为什么要单独做一个 Deployer 进程

回忆 u1-l1 的多进程架构：`WeaselServer.exe` 是常驻后台的服务进程，托管 librime 引擎、候选窗口 UI 与托盘；`WeaselTSF.dll` 是驻留每个应用进程的瘦客户端。

「改配置」这件事和「打字」是冲突的：

- 改完输入方案后，librime 需要**重新部署**（redeploy）：重新编译二进制词典（`.table.bin`）、重载方案、重建用户数据。这期间引擎不可用。
- 重新部署要安全地改写用户目录下的文件，而此时 `WeaselServer` 可能正持有这些文件（用户词典、快照）。

所以 Weasel 把「配置 + 部署」拆到一个**独立的 EXE** `WeaselDeployer.exe` 里。它的工作方式是：

1. 通过命名管道 IPC 通知正在运行的 `WeaselServer`「**进入维护模式**」（暂停处理按键、清空会话）。
2. 安全地操作用户数据（写 `.custom.yaml`、编译词典、同步）。
3. 通知 Server「**退出维护模式**」（重新 `Initialize` 引擎，恢复打字）。

这套「申请维护 → 操作 → 恢复」骨架是本讲反复出现的模式，记住它，后面三段命令行代码就是同一个模板的三份拷贝。

### 2.2 librime 的两套 API 与「custom.yaml 覆盖层」

librime 对外暴露一张函数指针表 `RimeApi`（见 u4-l1），用 `rime_get_api()` 取得。但配置管理另有一套更高层的工具叫 **levers**（杠杆），它是一个可加载模块，通过核心 API 取出：

```
RimeLeversApi* api = (RimeLeversApi*)rime_get_api()->find_module("levers")->get_api();
```

levers 的核心思想是 **覆盖层（overlay）**：

- 引擎随发行版带一套默认配置，例如 `weasel.yaml`（UI 默认样式）、`default.yaml`（默认方案列表、切换键）。
- 用户的个性化不直接改这些发行版文件（升级时会被覆盖），而是写一个 **`<name>.custom.yaml`**，里面用 `__patch:` 指令描述「把默认配置里的某个路径改成某个值」。
- 引擎加载时把 `.custom.yaml` 的补丁合并（merge）到默认 `.yaml` 之上，得到最终生效配置。

`RimeCustomSettings` 就是 levers 里「一个 `.custom.yaml` 文件」的抽象。本讲会看到两个实例：

| levers 对象 | 对应文件 | 作用 |
|---|---|---|
| `RimeSwitcherSettings`（一种 `RimeCustomSettings`） | `default.custom.yaml` | 选用哪些输入方案、切换键 |
| `RimeCustomSettings("weasel", "Weasel::UIStyleSettings")` | `weasel.custom.yaml` | UI 配色方案 |

> 术语提示：`RimeSwitcherSettings` 是 levers 专门为「方案切换器」做的特化版本，本质仍是 `RimeCustomSettings`，代码里直接强转：`(RimeCustomSettings*)switcher_settings`。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `WeaselDeployer/` 子工程内，另有两处跨工程的协作点。

| 文件 | 作用 |
|---|---|
| [WeaselDeployer/WeaselDeployer.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/WeaselDeployer.cpp) | `_tWinMain` 入口与命令行分发；单实例互斥体 |
| [WeaselDeployer/Configurator.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp) | `Configurator` 类：初始化 levers、弹设置对话框、deploy/sync/dict 三种操作 |
| [WeaselDeployer/SwitcherSettingsDialog.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/SwitcherSettingsDialog.cpp) | 方案切换对话框（写 `default.custom.yaml`） |
| [WeaselDeployer/UIStyleSettings.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/UIStyleSettings.cpp) / [UIStyleSettingsDialog.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/UIStyleSettingsDialog.cpp) | UI 样式（配色）设置模型与对话框（写 `weasel.custom.yaml`） |
| [WeaselDeployer/DictManagementDialog.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/DictManagementDialog.cpp) | 用户词典的导出/导入/备份/还原对话框 |
| [WeaselServer/WeaselServerApp.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp) | 托盘菜单如何用不同命令行参数拉起 Deployer（入口反向） |
| [RimeWithWeasel/RimeWithWeasel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp) | Server 端 `_IsDeployerRunning` / `StartMaintenance` / `EndMaintenance`（协作反向） |

## 4. 核心概念与源码讲解

### 4.1 Configurator 与 rime_levers_api

#### 4.1.1 概念说明

`Configurator` 是 Deployer 的「大脑」，一个普通的 C++ 类（非窗口、非 COM），负责：

1. 初始化 librime（部署器视角的轻量初始化）。
2. 弹出设置对话框，把用户选择写进 `.custom.yaml`。
3. 执行 deploy / sync / dict 三类「重操作」。

它和 librime 打交道时同时用到**两套 API**：

- 核心 `RimeApi`（`rime_get_api()`）：引擎初始化、`deploy()`、`sync_user_data()`、`deploy_config_file()`。
- levers `RimeLeversApi`（`find_module("levers")->get_api()`）：加载/保存 `.custom.yaml`、枚举方案、管理用户词典。

注意 `Configurator::Initialize` 用的是 `deployer_initialize(NULL)`，而不是 Server 那边用的 `initialize()`。`deployer_initialize` 是 librime 提供给部署工具的轻量初始化路径——它准备好读写配置所需的环境，但不会像 `initialize` 那样把整套打字引擎跑起来。这是合理的：Deployer 不需要打字，只需要改配置和编译词典。

#### 4.1.2 核心流程

`Configurator::Run` 的判分逻辑可以用下面的状态机描述：

```
       ┌── installing 且 不是首次运行? ──┐
       │      (is_first_run == false)    │
       │  是 → skip_switcher_settings   │
       │  否 → 弹方案设置对话框          │
       │       用户 OK 且 save 成功       │
       │       → reconfigured = true     │
       ├─────────────────────────────────┤
       │ installing 且 不是首次运行?     │
       │  是 → skip_ui_style_settings    │
       │  否 → 弹 UI 样式对话框          │
       │       用户 OK 且 save 成功       │
       │       → reconfigured = true     │
       └──────────────┬──────────────────┘
                      ▼
          installing || reconfigured ?
                是 → UpdateWorkspace(reconfigured)   // 触发重新部署
                否 → 直接返回 0                       // 用户只是看了看，没改
```

关键点：「**首次运行**」(`is_first_run`) 决定安装时是否强制弹设置向导。安装 (`/install`) 是首次部署，如果用户从没配置过，就引导他选方案、选配色；如果已经配置过（重装/升级），就跳过对话框，直接 `UpdateWorkspace`。这是典型的「首次安装向导」逻辑。

#### 4.1.3 源码精读

先看入口初始化 [Configurator.cpp:34-53](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L34-L53)：构造 `RimeTraits`，填入共享目录、用户目录、发行版身份（`WEASEL_CODE_NAME` / `WEASEL_VERSION`）、日志目录，然后 `setup` + `deployer_initialize`。这段和 u4-l1 里 `RimeWithWeaselHandler::_Setup` 几乎一致，区别只在最后一步用 `deployer_initialize`。

构造函数里还有个小细节 [Configurator.cpp:29-32](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L29-L32)：用 `CreateFileIfNotExist` 确保 `default.custom.yaml` 和 `weasel.custom.yaml` 存在（不存在就建空文件），这样后续 levers 加载时不会因为缺文件报错。

接着是 `Run` 的判分主体 [Configurator.cpp:85-114](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L85-L114)。它先取 levers 模块 [Configurator.cpp:86-91](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L86-L91)：

```cpp
RimeModule* levers = rime_get_api()->find_module("levers");
RimeLeversApi* api = (RimeLeversApi*)levers->get_api();
```

然后初始化两份设置 [Configurator.cpp:95-96](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L95-L96)：`switcher_settings_init()` 返回管理方案切换的 `RimeSwitcherSettings`；`UIStyleSettings` 是 Deployer 自己的薄封装（见 4.2），内部用 `custom_settings_init("weasel", "Weasel::UIStyleSettings")` 绑定到 `weasel.custom.yaml`。

判分的核心是这段短路的 `&&` 链 [Configurator.cpp:103-106](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L103-L106)：

```cpp
(skip_switcher_settings ||
 configure_switcher(api, switcher_settings, &reconfigured)) &&
    (skip_ui_style_settings ||
     configure_ui(api, &ui_style_settings, &reconfigured));
```

`A || B` 表示「跳过，否则执行 B」；`A && B` 表示「前一步通过才执行下一步」。于是它的语义是：先（按需）弹方案对话框，再（按需）弹 UI 样式对话框，两步顺序执行。任何一个对话框里 `save_settings` 成功就把 `reconfigured` 置真。

最后 [Configurator.cpp:110-113](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L110-L113)：只要是在安装，或者用户确实改了配置，就调用 `UpdateWorkspace(reconfigured)` 触发重新部署。注意 `UpdateWorkspace` 的形参 `report_errors` 在「安装」这条路径上默认是 `false`（因为 `reconfigured` 传入），而在 `/deploy` 命令行路径上是 `false`（默认参数），仅在「改了配置」时是 `true`——这控制了冲突时是否弹「将在重启后生效」的提示框。

#### 4.1.4 代码实践

**实践目标**：把 `Configurator::Run` 里出现的每一个 librime API 调用整理成一张表，区分核心 API 与 levers API。

**操作步骤**：

1. 打开 [Configurator.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp)。
2. 用搜索定位 `rime_get_api()`、`find_module("levers")`、`api->`、`rime->` 的全部出现点。
3. 仿照下表填写（已给出示例行）：

| 调用 | 所属 API | 所在函数 | 作用 |
|---|---|---|---|
| `rime_get_api()->setup(&weasel_traits)` | 核心 | `Initialize` | 登记 traits（目录/身份） |
| `rime->deployer_initialize(NULL)` | 核心 | `Initialize` | 部署器轻量初始化 |
| `find_module("levers")->get_api()` | 核心→levers | `Run` | 取 levers 模块 API |
| `api->switcher_settings_init()` | levers | `Run` | 建 `default.custom.yaml` 句柄 |
| `api->is_first_run(...)` | levers | `Run` | 是否首次运行（决定是否弹向导） |
| `api->load_settings(...)` / `save_settings(...)` | levers | `configure_switcher`/`configure_ui` | 读/写覆盖层 |
| `api->custom_settings_destroy(...)` | levers | `Run` | 释放 settings 对象 |

**需要观察的现象**：注意 levers 的设置对象（`RimeSwitcherSettings*`）必须手动 `custom_settings_destroy` [Configurator.cpp:108](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L108)，是 C 风格的所有权；而 `UIStyleSettings` 是 C++ 对象，作用域结束自动析构。

**预期结果**：得到一张完整映射表，能清楚说出「哪些操作走核心 API、哪些走 levers」。

**待本地验证**：若想在运行时验证 `is_first_run` 的取值，可在 `Configurator::Run` 第 98-101 行附近临时加一行 `LOG(INFO) << "first_run switcher=" << !skip_switcher_settings;`（需Debug版），实际行为待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Configurator::Initialize` 调 `deployer_initialize` 而 `RimeWithWeaselHandler::Initialize` 调 `initialize`？两者能否互换？

**答案**：`deployer_initialize` 是给部署工具的轻量初始化，只准备读写配置与编译词典所需环境；`initialize` 会把完整打字引擎跑起来（建会话、加载方案到内存）。Deployer 不打字，用前者更轻；Server 要打字，必须用后者。互换会让 Deployer 白白加载整套引擎资源（浪费），或让 Server 没把引擎跑起来（不能打字），都不合理。

**练习 2**：`Run` 结尾的 `api->custom_settings_destroy((RimeCustomSettings*)switcher_settings)` 为什么只销毁 `switcher_settings`，不销毁 `ui_style_settings`？

**答案**：`switcher_settings` 是 levers 返回的裸 C 指针，所有权在调用方，必须手动销毁；`ui_style_settings` 是 C++ 栈对象 `UIStyleSettings`，它内部持有的 `RimeCustomSettings*` 应由它自己的析构逻辑负责（其 settings 句柄随对象生命周期管理），因此这里不直接销毁。

**练习 3**：如果把第 103-106 行的 `&&` 改成 `||`，「先弹方案对话框再弹样式对话框」的顺序保证还能成立吗？

**答案**：不能。`&&` 的短路保证左操作数（方案对话框）求值完后才求值右操作数（样式对话框），且当左侧为假时跳过右侧。改成 `||` 后，当 `skip_switcher_settings` 为真（左侧为真）就会短路跳过样式对话框，破坏「两步顺序」语义。这里 `&&` 是刻意的顺序控制，不是逻辑与。

### 4.2 方案/UI/词典三类设置对话框

#### 4.2.1 概念说明

Deployer 用三个 WTL 对话框（都继承自 `CDialogImpl<>`）给用户提供 GUI：

- **`SwitcherSettingsDialog`**：方案切换。列出所有可用输入方案，用复选框让用户勾选启用哪些；展示方案描述；提供「获取更多方案」按钮。结果写入 `default.custom.yaml`。
- **`UIStyleSettingsDialog`**：UI 样式（目前主要是配色方案）。下拉选配色，右边实时预览配色效果图。结果写入 `weasel.custom.yaml` 的 `style/color_scheme` 字段。
- **`DictManagementDialog`**：用户词典管理。对每个用户词典（用户打字积累的 `.userdb`）做导出（成 txt）、导入、备份（成快照）、还原。

三个对话框的共同模式：**用 levers 接口枚举数据 → 用 WTL 控件呈现 → 用户操作后用 levers 接口写回**。也就是说对话框本身不直接读写 YAML 文件，全部委托给 levers，这保证了覆盖层合并逻辑的一致性。

#### 4.2.2 核心流程

以**方案切换对话框**为例，它的「加载—编辑—保存」三段式如下：

```
[加载 Populate]
  api->get_available_schema_list()   所有可用方案
  api->get_selected_schema_list()    当前已选用方案
  → 已选用项排前面并打勾，其余排后面

[编辑]
  用户勾选/取消 → OnSchemaListItemChanged → modified_ = true
  用户点条目 → ShowDetails (api->get_schema_name/author/description)

[保存 OnOK]
  遍历勾选项 → 收集 schema_id 数组
  count == 0 → 报错「至少选用一项」
  api->select_schemas(settings, selection, count)   写入覆盖层
  → 由 configure_switcher 里的 api->save_settings 落盘到 default.custom.yaml
```

UI 样式对话框的流程更短：`Populate` 读 `style/color_scheme` 当前值 + 枚举 `preset_color_schemes`，用户在下拉里换选 → `SelectColorScheme`（`customize_string`）写值，`DoModal` 返回 IDOK 后同样由外层 `save_settings` 落盘。

#### 4.2.3 源码精读

**方案切换对话框**的加载逻辑 [SwitcherSettingsDialog.cpp:17-62](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/SwitcherSettingsDialog.cpp#L17-L62)：先取已选用方案，逐个在可用列表里找到对应条目，插到列表前部并打勾（`SetCheckState(k, TRUE)`），用一个 `std::set<RimeSchemaInfo*> recruited` 去重；再把剩余可用方案追加在后部。注意它把 `RimeSchemaInfo*`（levers 返回的方案信息句柄）存进列表项的 `ItemData`，方便后面 `ShowDetails` 和 `OnOK` 取用。

`OnOK` 的保存 [SwitcherSettingsDialog.cpp:159-184](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/SwitcherSettingsDialog.cpp#L159-L184)：遍历所有勾选项，用 `api->get_schema_id(info)` 收集 schema_id 字符串数组，至少要选一项（否则弹 `IDS_STR_ERR_AT_LEAST_ONE_SEL`），最后 `api->select_schemas(settings, selection, count)` 把选用清单交给 levers。注意这里**并没有直接 `save_settings`**——保存由外层 `configure_switcher` 在 `DoModal() == IDOK` 后统一做（见 4.1.3 引用的 [Configurator.cpp:61-66](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L61-L66)）。这是一种合理的职责分层：对话框只负责「改内存里的 settings 对象」，落盘由调用方决定。

> 「获取更多方案」按钮 `OnGetSchemata` [SwitcherSettingsDialog.cpp:113-157](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/SwitcherSettingsDialog.cpp#L113-L157) 走的是另一条路：从注册表 `WeaselRoot` 读出安装目录，`ShellExecuteExW` 跑 `rime-install.bat`（plum 方案下载器，见 u1-l2 提到的 `plum` 子模块），等它结束后 `load_settings` 重新刷新列表。

**UI 样式**的模型层 [UIStyleSettings.cpp:5-8](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/UIStyleSettings.cpp#L5-L8) 在构造时绑定到 `weasel.custom.yaml`：

```cpp
settings_ = api_->custom_settings_init("weasel", "Weasel::UIStyleSettings");
```

第一个参数 `"weasel"` 决定文件名 `weasel.custom.yaml`，第二个是 levers 内部用于区分定制器类别的标签。读配色列表 [UIStyleSettings.cpp:10-39](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/UIStyleSettings.cpp#L10-L39) 用核心 API 的 `config_begin_map`/`config_next` 迭代 `preset_color_schemes` 映射表（配色定义在 `weasel.yaml` 里），逐项取 `name`/`author`。

写配色 [UIStyleSettings.cpp:71-75](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/UIStyleSettings.cpp#L71-L75) 就一行：

```cpp
api_->customize_string(settings_, "style/color_scheme", color_scheme_id.c_str());
```

`customize_string` 是 levers 的关键写接口：它不直接覆盖 `weasel.yaml`，而是在 `weasel.custom.yaml` 里追加一条 `__patch` 指令，描述「把 `style/color_scheme` 改成这个值」。预览图 [UIStyleSettings.cpp:49-59](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/UIStyleSettings.cpp#L49-L59) 优先从用户目录、其次从共享目录找 `preview/color_scheme_<id>.png`。

**词典管理**对话框 [DictManagementDialog.cpp:66-75](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/DictManagementDialog.cpp#L66-L75) 用 levers 的用户词典迭代器 `user_dict_iterator_init`/`next_user_dict` 列出所有用户词典。四个操作按钮分别对应：

| 按钮 | 处理函数 | levers 接口 | 产物 |
|---|---|---|---|
| 备份 | `OnBackup` [DictManagementDialog.cpp:100-134](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/DictManagementDialog.cpp#L100-L134) | `backup_user_dict` | 同步目录下的 `<dict>.userdb.txt` 快照 |
| 还原 | `OnRestore` [DictManagementDialog.cpp:136-168](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/DictManagementDialog.cpp#L136-L168) | `restore_user_dict` | 从快照文件恢复 |
| 导出 | `OnExport` [DictManagementDialog.cpp:170-219](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/DictManagementDialog.cpp#L170-L219) | `export_user_dict` | 可读 txt（带记录数报告） |
| 导入 | `OnImport` [DictManagementDialog.cpp:221-264](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/DictManagementDialog.cpp#L221-L264) | `import_user_dict` | 从 txt 导入（带记录数报告） |

#### 4.2.4 代码实践

**实践目标**：跟踪 UI 配色「下拉换选 → 写入文件」的全链路，确认它最终落进 `weasel.custom.yaml` 的 `style/color_scheme` 字段。

**操作步骤**：

1. 阅读 [UIStyleSettingsDialog.cpp:57-64](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/UIStyleSettingsDialog.cpp#L57-L64) 的 `OnColorSchemeSelChange`：用户在下拉换选 → 取 `color_scheme_id` → 调 `settings_->SelectColorScheme(...)`。
2. 跟进 [UIStyleSettings.cpp:71-75](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/UIStyleSettings.cpp#L71-L75)：`customize_string(settings_, "style/color_scheme", ...)` 在内存 settings 上写补丁。
3. 回到外层 [Configurator.cpp:70-83](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L70-L83) 的 `configure_ui`：`DoModal() == IDOK` 后 `api->save_settings(settings)` 把补丁落盘。
4. （运行态）在装好 Weasel 的机器上，打开「小狼毫」托盘 → 用户设定 → UI 样式，换一个配色，确定。然后到用户数据目录打开 `weasel.custom.yaml`，应能看到形如：

```yaml
# weasel.custom.yaml
__patch:
  style/color_scheme: <你选的配色 id>
```

**需要观察的现象**：文件里只有一条 `__patch`，而不是整份 `weasel.yaml` 的拷贝——这正是覆盖层的体现。

**预期结果**：能画出「下拉换选 → SelectColorScheme → customize_string → save_settings → weasel.custom.yaml 的 `__patch`」这条链。

**待本地验证**：第 4 步需要在 Windows 上实际运行 WeaselDeployer 验证文件内容，本环境无法运行，标注为待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：方案切换对话框的 `OnOK` 里调了 `select_schemas`，却没调 `save_settings`。保存到底在哪里发生？为什么这样设计？

**答案**：保存在外层 `configure_switcher` 里，当 `DoModal() == IDOK` 时调 `api->save_settings`（[Configurator.cpp:62-66](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L62-L66)）。这样设计让对话框只改内存中的 settings 对象，落盘时机由调用方控制——好处是如果将来复用对话框做「预览不保存」之类场景，对话框代码不用改。

**练习 2**：`UIStyleSettings` 构造时 `custom_settings_init("weasel", "Weasel::UIStyleSettings")` 的第一个参数换成 `"default"`，会发生什么？

**答案**：levers 会去绑定 `default.custom.yaml` 而不是 `weasel.custom.yaml`，于是配色补丁会写进错误的文件。第一个参数直接决定 `.custom.yaml` 的文件名前缀，这是 levers 的命名约定。

**练习 3**：词典管理的「备份」和「导出」都产出文件，二者产物有何本质区别？

**答案**：「备份」`backup_user_dict` 产出的是 levers/引擎内部格式的快照（`<dict>.userdb.txt`，放在同步目录），用于「还原」原样恢复整份用户库；「导出」`export_user_dict` 产出的是人类可读的 txt 词表（用户自选保存位置），常用于备份查看或迁移到别的工具。一个是内部快照，一个是可读导出。

### 4.3 命令行模式 deploy/sync/dict

#### 4.3.1 概念说明

Deployer 是个命令行驱动的程序。`_tWinMain` 解析命令行参数后分派到 `Configurator` 的不同方法。先看入口的分派表：

| 命令行 | 处理 | 含义 |
|---|---|---|
| 无参数 | `configurator.Run(false)` | 弹设置对话框（方案 + UI 样式） |
| `/install` | `configurator.Run(true)` | 安装向导（首次运行才弹对话框），结束后部署 |
| `/deploy` | `configurator.UpdateWorkspace()` | 重新部署工作区（编译词典、重建配置） |
| `/dict` | `configurator.DictManagement()` | 词典管理对话框 |
| `/sync` | `configurator.SyncUserData()` | 同步用户数据 |
| `/?` `/help` | 显示帮助 | 列出以上命令 |

这些命令行由谁触发？答案在 `WeaselServer` 的托盘菜单。回顾 u6 单元的入口反向：托盘菜单项通过 `WeaselServerApp::execute` 用 `ShellExecuteW` 拉起 `WeaselDeployer.exe` 并带上不同参数 [WeaselServerApp.cpp:48-63](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L48-L63)，`execute` 的实现是薄薄一层 `ShellExecuteW` [WeaselServerApp.h:20-23](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.h#L20-L23)。例如托盘的「重新部署」菜单项 `ID_WEASELTRAY_DEPLOY` 绑到 `execute(dir/L"WeaselDeployer.exe", L"/deploy")`。

`/deploy`、`/sync`、`/dict` 三个方法共享同一套骨架（见 2.1）：**互斥体 → 连 Server 进维护 → 操作 → 释放互斥体 → 连 Server 退维护**。

#### 4.3.2 核心流程

三个方法的共同骨架可以用伪代码描述（以 `UpdateWorkspace` 为例）：

```
UpdateWorkspace(report_errors):
  hMutex = CreateMutex("WeaselDeployerMutex")          # 操作级互斥
  if 已存在:                                              # 别的 deployer 在跑
      提示「正在执行另一项部署任务」(仅 report_errors 时)
      return 1

  client = weasel::Client()
  if client.Connect():                                   # 连到运行中的 Server
      client.StartMaintenance()                          # IPC: 让 Server 进维护

  {
      rime->deploy()                                     # 编译/部署方案与默认配置
      rime->deploy_config_file("weasel.yaml",            # 重新生成 weasel 配置
                               "config_version")
  }

  CloseHandle(hMutex)                                    # 先放锁，再退维护
  if client.Connect():
      client.EndMaintenance()                            # IPC: 让 Server 退出维护
  return 0
```

这里有三个**协作信号**值得专门理解：

1. **`WeaselDeployerExclusiveMutex`**（进程级，在 `_tWinMain` [WeaselDeployer.cpp:43-54](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/WeaselDeployer.cpp#L43-L54)）：整个 Deployer 进程生命周期持有，保证全局只有一个 Deployer 实例。
2. **`WeaselDeployerMutex`**（操作级，在三个方法里）：只在 deploy/sync/dict 的临界区内持有。它**同时被 Server 端探测**——`RimeWithWeaselHandler::_IsDeployerRunning` [RimeWithWeasel.cpp:509-514](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L509-L514) 用 `CreateMutex` 探测这个同名互斥体是否存在，从而知道「Deployer 正在改用户数据」。这是 Deployer 与 Server 跨进程握手的桥（u4-l1 提到过）。
3. **维护模式 IPC**：`StartMaintenance` / `EndMaintenance` 是 `weasel::Client` 的方法 [WeaselIPC.h:118](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L118) 与 [WeaselIPC.h:120](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L120)，经命名管道发给 Server；Server 端 `RimeWithWeaselHandler::StartMaintenance` [RimeWithWeasel.cpp:475-479](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L475-L479) 清空会话表并 `Finalize`（卸载引擎），`EndMaintenance` [RimeWithWeasel.cpp:481-485](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L481-L485) 再 `Initialize` 重新加载。注意 `StartMaintenance` 是幂等自愈的：若 Server 处于禁用态时有新会话进来，`AddSession` 会尝试 `EndMaintenance` 恢复 [RimeWithWeasel.cpp:166-172](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L166-L172)。

三个方法的差异只在「中间那段操作」：

| 方法 | 中间的核心操作 |
|---|---|
| `UpdateWorkspace` [Configurator.cpp:141-147](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L141-L147) | `deploy()` + `deploy_config_file("weasel.yaml", "config_version")` |
| `SyncUserData` [Configurator.cpp:221-228](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L221-L228) | `sync_user_data()` + `join_maintenance_thread()` |
| `DictManagement` [Configurator.cpp:180-187](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L180-L187) | `run_task("installation_update")` + 弹 `DictManagementDialog` |

#### 4.3.3 源码精读

入口分派在 [WeaselDeployer.cpp:61-100](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/WeaselDeployer.cpp#L61-L100)：`Run` 依次用 `wcscmp` 比对命令行，先 `/?`/`/help`，再 `/deploy`、`/dict`、`/sync`、`/install`，最后无参走 `Run(false)`。注意 `/install` 是用 `configurator.Run(installing)` 走「带安装标志」的对话框路径（见 4.1），而 `/deploy` 才是「纯重新部署」。

`UpdateWorkspace` 全貌 [Configurator.cpp:116-156](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L116-L156)：注意 [Configurator.cpp:149](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L149) 的注释 `// should be closed before resuming service.`——互斥体必须在 `EndMaintenance` **之前**关闭，否则 Server 一退出维护、重新持有用户数据文件，可能与仍在临界区的下一个操作冲突。顺序是刻意的。

`SyncUserData` [Configurator.cpp:198-237](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L198-L237) 多了 `join_maintenance_thread()`——`sync_user_data` 在后台线程执行（同步可能涉及网络/盘操作），要 `join` 等它结束再退维护，避免同步还没完就恢复打字。

`DictManagement` [Configurator.cpp:158-196](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L158-L196)：中间用 `RIME_API_AVAILABLE(rime, run_task)` 做版本探测——只有支持的 librime 版本才调 `run_task("installation_update")`（它会建立/修正用户数据同步目录），这是 u4-l1 提到的 ABI 版本协商 `RIME_API_AVAILABLE` 宏的又一处实例。随后弹 `DictManagementDialog`（模态，阻塞直到用户关掉）。

`_tWinMain` 的进程级单实例 [WeaselDeployer.cpp:43-54](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/WeaselDeployer.cpp#L43-L54)：`CreateMutex(... L"WeaselDeployerExclusiveMutex")`，若 `GetLastError() == ERROR_ALREADY_EXISTS` 直接返回，不进 `Run`。这防止用户连点托盘菜单拉起多个 Deployer。

#### 4.3.4 代码实践

**实践目标**：把 `UpdateWorkspace`、`SyncUserData`、`DictManagement` 三个函数对照阅读，提取它们的「同与不同」，验证它们确实共享同一套维护模式骨架。

**操作步骤**：

1. 同时打开 [Configurator.cpp:116-156](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L116-L156)（UpdateWorkspace）、[Configurator.cpp:158-196](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L158-L196)（DictManagement）、[Configurator.cpp:198-237](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L198-L237)（SyncUserData）。
2. 用三种颜色的高亮笔（或注释）分别标出每段里的：① 互斥体创建与冲突检查；② `client.StartMaintenance`；③ 核心操作；④ `CloseHandle(hMutex)`；⑤ `client.EndMaintenance`。
3. 把标出的内容填进对照表：

| 步骤 | UpdateWorkspace | DictManagement | SyncUserData |
|---|---|---|---|
| ① 互斥体名 | WeaselDeployerMutex | WeaselDeployerMutex | WeaselDeployerMutex |
| ② StartMaintenance | 有 | 有 | 有 |
| ③ 核心操作 | deploy + deploy_config_file | run_task + 对话框 | sync_user_data + join |
| ④ CloseHandle 在 EndMaint 之前 | 是 | 是 | 是 |
| ⑤ EndMaintenance | 有 | 有 | 有 |

**需要观察的现象**：三段代码几乎是「复制粘贴后替换中间一段」的结构——这是识别「可抽象骨架」的典型信号。

**预期结果**：得到上表，并意识到这三段可以重构成一个接受「核心操作 lambda」的模板方法（见综合练习）。

**待本地验证**：如需验证互斥体行为，可在 Windows 上同时跑两次 `WeaselDeployer.exe /deploy`，第二次应弹「正在执行另一项部署任务」——待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`UpdateWorkspace` 里 `CloseHandle(hMutex)` 为什么必须放在 `client.EndMaintenance()` 之前？

**答案**：互斥体临界区保护的是「改写用户数据」的窗口。`EndMaintenance` 会让 Server 重新 `Initialize` 并恢复持有用户数据文件；若先 `EndMaintenance` 再 `CloseHandle`，存在一个 Server 已恢复打字、而本 Deployer 仍声称在临界区的窗口，可能让别的操作误判。先放锁再退维护，保证 Server 恢复时临界区已结束。源码注释 `// should be closed before resuming service.` 明确说明了这一点。

**练习 2**：Server 端 `RimeWithWeaselHandler::_IsDeployerRunning` 是怎么「看见」Deployer 在跑的？两边没有任何 IPC 调用。

**答案**：通过同名 Windows 互斥体 `WeaselDeployerMutex`。Deployer 在临界区用 `CreateMutex` 创建它，Server 用 `CreateMutex` 同名探测，若 `GetLastError() == ERROR_ALREADY_EXISTS` 就知道 Deployer 持有它。这是 Win32 常见的「用命名内核对象做跨进程存在性信号」手法，不需要专门的 IPC 往返。

**练习 3**：`/deploy` 和「在设置对话框里改完配置点确定」都会触发重新部署，二者走的的是同一个 `UpdateWorkspace` 吗？

**答案**：是。设置对话框路径 `Run` 结尾在 `installing || reconfigured` 时调 `UpdateWorkspace(reconfigured)`（[Configurator.cpp:110-112](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L110-L112)），`/deploy` 命令行路径直接调 `UpdateWorkspace()`（默认 `report_errors=false`）。区别只是入口和 `report_errors` 实参：前者是因为用户刚改了配置、需要提示「重启后生效」；后者是用户主动点「重新部署」、冲突时不必再额外提示。

## 5. 综合实践

**综合任务**：完整追踪「用户在托盘点【用户设定】→ 修改 UI 样式 → 保存」的端到端流程，画出从托盘点击到 `weasel.custom.yaml` 落盘、再到提示重新部署的全链路，并标出每一步对应的源码位置。

**操作步骤**：

1. **托盘点击**：用户点托盘「用户设定」菜单项 `ID_WEASELTRAY_SETTINGS` → `WeaselServerApp::SetupMenuHandlers` 绑定的回调 `execute(dir/L"WeaselDeployer.exe", std::wstring())`（无参数）[WeaselServerApp.cpp:55-57](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.cpp#L55-L57)。
2. **拉起进程**：`execute` 调 `ShellExecuteW` 启动 `WeaselDeployer.exe`（无参数）[WeaselServerApp.h:20-23](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselServer/WeaselServerApp.h#L20-L23)。
3. **单实例检查**：`_tWinMain` 检查 `WeaselDeployerExclusiveMutex`，通过后进 `Run(lpCmdLine)` [WeaselDeployer.cpp:43-49](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/WeaselDeployer.cpp#L43-L49)。
4. **命令分派**：无参数 → `configurator.Run(false)` [WeaselDeployer.cpp:99](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/WeaselDeployer.cpp#L99)。
5. **初始化**：`Configurator::Initialize` 设 traits、`deployer_initialize` [Configurator.cpp:34-53](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L34-L53)。
6. **取 levers + 建设置对象**：`Run` 取 levers 模块，建 `switcher_settings` 与 `UIStyleSettings`（后者构造时绑定 `weasel.custom.yaml`）[Configurator.cpp:86-96](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L86-L96)。
7. **弹方案对话框**（用户通常直接确定）→ `configure_switcher` [Configurator.cpp:55-68](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L55-L68)。
8. **弹 UI 样式对话框**：`configure_ui` → `load_settings` → `UIStyleSettingsDialog::DoModal` [Configurator.cpp:70-83](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L70-L83)。
9. **用户换配色**：`OnColorSchemeSelChange` → `SelectColorScheme` → `customize_string("style/color_scheme", id)` 写补丁到内存 settings（[UIStyleSettingsDialog.cpp:57-64](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/UIStyleSettingsDialog.cpp#L57-L64) → [UIStyleSettings.cpp:71-75](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/UIStyleSettings.cpp#L71-L75)）。
10. **点确定**：`DoModal` 返回 IDOK → `configure_ui` 调 `api->save_settings` → **补丁落盘到 `weasel.custom.yaml`**，`reconfigured = true`（[Configurator.cpp:77-80](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L77-L80)）。
11. **触发重新部署**：`installing(false) || reconfigured(true)` 为真 → `UpdateWorkspace(true)` [Configurator.cpp:110-112](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L110-L112)。
12. **维护模式握手**：`UpdateWorkspace` 连 Server → `StartMaintenance`（IPC）→ `deploy()` + `deploy_config_file("weasel.yaml", ...)` → `EndMaintenance`（IPC）[Configurator.cpp:116-156](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L116-L156)。
13. **若冲突**：若另一个 Deployer 已在临界区（`WeaselDeployerMutex` 已存在）且 `report_errors=true`，弹「将在输入法再次启动后生效」提示（`IDS_STR_DEPLOYING_RESTARTREQ`）[Configurator.cpp:122-132](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L122-L132)。

**预期产出**：一张包含上述 13 步的流程图，标注每步的源码文件与行号，并能指出「`weasel.custom.yaml` 的补丁在第 10 步落盘」「引擎真正读到新配色是在第 12 步 `deploy_config_file` 之后、Server 退出维护重新 Initialize 时」。

**待本地验证**：第 12 步之后，可在用户数据目录确认 `weasel.yaml`（由 `deploy_config_file` 生成的合并后配置）已包含新配色——需在实际 Windows 环境验证。

## 6. 本讲小结

- `WeaselDeployer.exe` 是独立的「配置 + 部署」进程，与常驻的 `WeaselServer.exe` 分离，原因是改配置需要重新部署、而部署期间引擎不可用，必须由独立进程通过 IPC 协调 Server 进/出维护模式。
- 它同时用到 librime 的两套 API：核心 `RimeApi`（`setup`/`deployer_initialize`/`deploy`/`sync_user_data`）与 levers `RimeLeversApi`（管理 `.custom.yaml` 覆盖层、枚举方案、管理词典）。
- levers 的核心是「`.custom.yaml` 覆盖层」：用户的个性化以 `__patch` 形式叠加在发行版默认 `.yaml` 之上，`customize_string` 写补丁、`load/save_settings` 读写、`is_first_run` 控制安装向导。
- 三个设置对话框对应三份配置：`SwitcherSettingsDialog`→`default.custom.yaml`（方案）、`UIStyleSettingsDialog`→`weasel.custom.yaml`（配色，经 `UIStyleSettings` 封装）、`DictManagementDialog`→用户词典的导出/导入/备份/还原。
- `/deploy`、`/sync`、`/dict` 三种命令行模式共享同一套骨架：`WeaselDeployerMutex` 临界区 → IPC `StartMaintenance` → 核心操作 → 放锁 → IPC `EndMaintenance`，差异只在中间那段操作。
- Deployer 与 Server 的跨进程握手通过两个命名互斥体实现：`WeaselDeployerExclusiveMutex`（进程级单实例）与 `WeaselDeployerMutex`（操作级，被 Server 端 `_IsDeployerRunning` 探测）；维护状态则经命名管道 IPC 的 `StartMaintenance`/`EndMaintenance` 传递。

## 7. 下一步学习建议

- **u6-l2（WeaselSetup 安装与 IME 注册）**：本讲的 `/install` 路径与安装向导相关，下一讲讲 `WeaselSetup` 如何在系统层把 `weasel.dll` 注册成 TSF 文本服务，是「安装」的另一半。
- **u6-l3（系统托盘、服务进程与自动更新）**：本讲多次提到托盘菜单拉起 Deployer，下一讲系统讲解 `WeaselServerApp` 如何组装 Server/Handler/UI/托盘并处理命令行与单实例重启。
- **回顾 u4-l1 / u4-l4**：维护模式（`StartMaintenance`/`EndMaintenance`、`m_disabled`、`_IsDeployerRunning`）的 Server 端实现集中在 `RimeWithWeaselHandler`，建议对照本讲重温，理解「Deployer 申请—Server 执行」的完整闭环。
- **延伸阅读**：librime 仓库的 `rime_levers_api.h` 头文件列出了 levers 全部接口签名，可作为本讲 API 表的权威补充；`plum` 子模块（`rime-install.bat` 背后的方案下载器）则是「获取更多方案」按钮的真正实现。
