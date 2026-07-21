# 方案配置、App 选项与 inline preedit

## 1. 本讲目标

上一讲（u4-l2）我们走通了 `RimeWithWeaselHandler` 的按键主链路：`ProcessKeyEvent → rime_api->process_key → _Respond → _UpdateUI`。但你可能会问：**不同输入方案为什么能呈现不同的配色和图标？为什么在 `cmd.exe` 里默认就是英文，而在浏览器里又能内联显示拼音？这些「外观与行为差异」是在哪里、什么时候被决定下来的？**

本讲就回答这些问题。读完本讲你应当能够：

- 说清楚 **方案专属配置（schema-specific settings）** 是怎么按输入方案逐套加载并覆盖基础样式的。
- 掌握 **应用级选项（app_options）** 的配置格式、加载时机，以及它如何让指定应用（如 `cmd.exe`、游戏）默认进入英文或特定开关状态。
- 理解 **inline preedit（内联写作串）** 的三级优先级（app_options > 方案 > `weasel.yaml`），以及它在会话创建、运行时切方案、深色模式切换等场景下如何动态切换。
- 能够动手设计一份 `app_options` 配置，并追踪它从 YAML 一路生效到 librime `set_option` 的完整链路。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**（1）配置的三层来源。** Weasel 的外观与部分行为由三处 YAML 共同决定：

| 层级 | 文件 | 作用域 | 何时加载 |
|---|---|---|---|
| 全局基础 | `%AppData%\Rime\weasel.yaml` | 所有方案、所有应用 | `Initialize()` 启动时一次性加载，缓存为 `m_base_style` |
| 方案专属 | `<schema_id>.schema.yaml` 里的 `style:` 段 | 仅当前方案 | 每次进入/切换方案时叠加 |
| 应用专属 | `weasel.yaml` 里的 `app_options:` | 仅指定应用 | 会话创建时按应用名套用 |

层级越高越优先。本讲的核心就是后两层，以及它们如何叠加在第一层之上。

**（2）「样式」在内存里是什么。** 回顾 u2-l4：`weasel::UIStyle` 是一个约 80 字段的结构体，描述候选窗口的全部外观（字体、配色、布局、`inline_preedit` 等）。Weasel 用一个 `m_base_style` 保存全局基础样式，每个会话再用自己的 `SessionStatus::style` 副本承载「基础 + 方案叠加 + 应用微调」后的最终样式。

**（3）librime 的「选项（option）」机制。** librime 引擎内部维护一组布尔开关（如 `ascii_mode`、`inline_preedit`、`vim_mode`、`soft_cursor`），可通过 `rime_api->set_option(session_id, name, bool)` 设置、用 `get_option` 读取。这些选项既影响引擎行为（如 `ascii_mode` 决定是否直接上屏英文字母），也影响前端呈现（如 `inline_preedit` 决定拼音是否写进应用文档）。`app_options` 的本质，就是在会话刚创建时，用 `set_option` 批量套用一组预设开关。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，辅以公共头与一份真实配置示例：

| 文件 | 作用 |
|---|---|
| [RimeWithWeasel/RimeWithWeasel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp) | 本讲主战场：`_LoadSchemaSpecificSettings`、`_ReadClientInfo`、`_LoadAppInlinePreeditSet`、`_UpdateInlinePreeditStatus`、`_LoadAppOptions` 全在此 |
| [include/RimeWithWeasel.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h) | `AppOptions`/`AppOptionsByAppName` 类型定义、`CaseInsensitiveCompare`、`m_app_options` 成员 |
| [include/WeaselUI.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUI.h) | `ClientCapabilities` 枚举（`INLINE_PREEDIT_CAPABLE`），inline preedit 向前端 TSF 传递能力的桥梁 |
| [output/data/weasel.yaml](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/data/weasel.yaml) | 真实出厂配置示例，含 `app_options:` 段，便于对照格式 |

---

## 4. 核心概念与源码讲解

### 4.1 方案专属配置加载

#### 4.1.1 概念说明

不同的 Rime 输入方案（如双拼、注音、明月拼音）往往希望呈现不同的视觉风格：双拼方案可能想要一套冷色调配色和特定图标，全屏游戏方案可能想要横向布局。如果把这些都写死在全局 `weasel.yaml` 里，切方案时就要手动改样式，非常笨拙。

Weasel 的做法是：**每个方案可以在自己的 `.schema.yaml` 里写一个 `style:` 段，描述该方案期望的外观**；当用户切到这个方案时，Weasel 自动把这段配置「叠加」到基础样式之上。这就是 `_LoadSchemaSpecificSettings` 的职责。

需要注意「叠加」与「重置」的配合：函数先把样式重置回 `m_base_style`（全局基础），再把方案配置作为一层覆盖上去——方案里没提到的字段保持基础值，方案里提到的字段覆盖基础值。

#### 4.1.2 核心流程

`_LoadSchemaSpecificSettings(ipc_id, schema_id)` 的执行步骤：

1. 打开方案配置：`rime_api->schema_open(schema_id, &config)`，失败直接返回。
2. 读取该方案的 `show_notifications` 设置（控制提示是否显示）。
3. **重置到基础样式**：`m_ui->style() = m_base_style`。
4. **叠加方案样式**：`_UpdateUIStyle(&config, m_ui, false)`——注意第三个参数 `initialize=false`，含义见下文。
5. 把叠加结果存进当前会话：`session_status.style = m_ui->style()`。
6. 加载方案专属配色（区分深/浅色模式）：先查方案内的 `preset_color_schemes/<name>`，找不到则回退到 `weasel.yaml` 的同名配色。
7. 加载方案图标：`schema/icon`、`schema/ascii_icon`、`schema/full_icon`、`schema/half_icon`，优先用户目录、其次共享目录。

#### 4.1.3 源码精读

函数入口与方案配置打开：

[LoadSchemaSpecificSettings 入口与 schema_open](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L555-L562) —— 失败则直接 `return`，不做任何样式改动，保证调用方现状不变。

「重置 + 叠加」是本函数的灵魂：

[重置到 base 再叠加方案样式](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L563-L568) —— `m_ui->style() = m_base_style` 清掉上一个方案的痕迹，随后 `_UpdateUIStyle(&config, m_ui, false)` 把当前方案的 `style:` 段覆盖上去，最后写回 `session_status.style`。

`initialize=false` 的精妙之处在于 `_RimeGetBool` 的实现：

[_RimeGetBool 的 cond 短路逻辑](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L1046-L1056) —— `if (config_get_bool(...) || cond)`：当 `cond`（即 `initialize`）为 `false` 时，只有配置里**确实存在该键**才会改写 `value`；为 `true` 时则无条件改写（缺失就取 `falseValue`）。所以全局加载（`initialize=true`）会落实所有默认值，而方案叠加（`initialize=false`）只覆盖方案明确写出的字段。这就是「叠加而非重置」的底层实现。

方案专属配色查找（含深色模式分支与回退）：

[配色查找：方案内优先，回退 weasel.yaml](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L587-L590) —— 根据 `m_current_dark_mode` 选择 `style/color_scheme_dark` 或 `style/color_scheme` 键，找到配色名后交由上面的 `update_color_scheme` 闭包解析。闭包里先用 `config_begin_map` 在方案内查 `preset_color_schemes/<name>`，查不到再 `config_open("weasel", ...)` 回退到全局配置。

方案图标加载：

[图标加载与用户/共享目录回退](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L591-L616) —— `load_icon` lambda 接受主键与备键（如 `schema/icon` 与旧的 `schema/zhung_icon`），读取后先看 `WeaselUserDataPath()` 下是否存在该文件，否则查 `WeaselSharedDataPath()`。结果写入 `style.current_zhung_icon` 等四个字段，供托盘与语言栏绘制使用。

#### 4.1.4 代码实践

**实践目标：** 通过阅读一份真实方案配置，理解方案专属样式如何写。

**操作步骤：**

1. 在仓库里找到 `output/data/` 下的任意一个方案文件（如 `luna_pinyin.schema.yaml`，若不存在则任选一个 `*.schema.yaml`），用编辑器打开。
2. 查找顶层 `style:` 段，观察它定义了哪些字段（如 `color_scheme`、`icon`、`horizontal`）。
3. 对照本讲 4.1.3 的三处源码，自问：如果方案里写了 `style/color_scheme: mystyle`，而 `mystyle` 既不在该 schema 的 `preset_color_schemes` 里、也不在 `weasel.yaml` 里，`update_color_scheme` 闭包会走到哪个分支？结果是什么？

**需要观察的现象 / 预期结果：**

- 方案的 `style:` 段是**可选**的；很多方案根本不写，此时 `_LoadSchemaSpecificSettings` 仍会执行，但叠加后样式与 `m_base_style` 几乎一致（仅图标/配色按方案默认走）。
- 若配色名找不到，`_UpdateUIStyleColor` 内的 `config_get_string` 与 `config_begin_map` 都失败，样式颜色字段保持 `m_base_style` 的值，**不会崩溃**。

> 由于本仓库 `output/data/` 的具体方案文件随发布版本变化，若你本地没有 `luna_pinyin.schema.yaml`，可改读任意存在的 `*.schema.yaml`——这是「待本地验证」的实践，重点在于对照源码理解字段流向，而非特定文件名。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `_LoadSchemaSpecificSettings` 在叠加方案样式前要先把 `m_ui->style()` 重置为 `m_base_style`？如果不重置会怎样？

**参考答案：** 因为同一个会话可能先后加载多个方案（用户运行时切方案）。若不重置，上一个方案叠加留下的字段会「污染」当前方案，导致配色/布局错乱。重置保证每次都从干净的全局基础出发再叠加，结果只取决于「基础 + 当前方案」。

**练习 2：** 方案 A 的 `style:` 段只写了 `color_scheme: dark_aqua`，没写 `font_face`。加载完方案 A 后，`session_status.style.font_face` 的值是什么？

**参考答案：** 仍是 `m_base_style.font_face`（即 `weasel.yaml` 里 `style/font_face` 的值，如 `Microsoft YaHei`）。因为 `initialize=false` 下 `_RimeGetIntStr`/`_RimeGetBool` 只在配置存在对应键时才改写，方案没写 `font_face` 就保持基础值。

---

### 4.2 应用级 AppOptions

#### 4.2.1 概念说明

有些应用对输入法有特殊诉求：

- `cmd.exe`、`conhost.exe`：命令行里几乎只敲英文命令，默认进入英文模式（`ascii_mode: true`）更顺手。
- 某些全屏游戏：内联写作串会干扰画面，希望关掉 `inline_preedit`。
- Firefox 早期版本有输入法兼容问题，需要强制开启 `inline_preedit` 作为 workaround。

这些诉求与输入方案无关，只与「当前前台应用是谁」有关。把它们写进每个方案不现实，于是 Weasel 提供了 `app_options`：在 `weasel.yaml` 里按应用可执行文件名声明一组开关，会话创建时自动套用。

`app_options` 的数据结构在头文件里定义：

[AppOptions 与 AppOptionsByAppName 类型定义](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/RimeWithWeasel.h#L21-L23) —— `AppOptions` 是「开关名 → 布尔」的 map；`AppOptionsByAppName` 是「应用名 → AppOptions」的 map，**用 `CaseInsensitiveCompare` 作为比较器**，所以应用名匹配大小写不敏感（`CMD.EXE` 与 `cmd.exe` 等价）。

#### 4.2.2 核心流程

应用级选项有两个阶段：**启动时一次性解析** 与 **每次会话创建时套用**。

阶段一（启动时）——`Initialize()` 里调用 `_LoadAppOptions(&config, m_app_options)`：

1. `ForEachRimeMap` 遍历 `weasel.yaml` 的 `app_options:` 这一 map，每个条目的 key 是应用名、path 指向该应用的子配置。
2. 对每个应用，再嵌套遍历其内部的 map，读取每个布尔开关，存入 `m_app_options[app_key][opt_key]`。
3. 结果保存在成员 `m_app_options` 里，供后续所有会话复用，直到下次重新部署。

阶段二（会话创建时）——`AddSession()` 调 `_ReadClientInfo(ipc_id, buffer)`：

1. 从 IPC 请求缓冲里逐行解析 `session.client_app=<应用名>`，并把应用名转小写。
2. 用 `rime_api->set_property(session_id, "client_app", app_name)` 把应用名登记到引擎，供后续 `_RefreshTrayIcon`、`_LoadAppInlinePreeditSet` 复用。
3. 在 `m_app_options` 里查找该应用名；命中则遍历其开关，逐个 `rime_api->set_option(session_id, key, value)` 写入引擎。
4. 最后根据当前样式同步 `inline_preedit` 与 `soft_cursor` 两个开关。

`client_app` 这个字符串是怎么来的？它由 IPC 客户端在 `StartSession` 时写进请求正文：

[WeaselClientImpl 把 client_app 写入请求](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L187) —— TSF 端在建立会话前，把自己所在进程的可执行文件名（如 `cmd.exe`）以 `session.client_app=` 前缀写进管道缓冲，服务端 `_ReadClientInfo` 再解析出来。

#### 4.2.3 源码精读

`_LoadAppOptions` 用两层 `ForEachRimeMap` 嵌套解析 YAML：

[_LoadAppOptions 双层 map 遍历](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L1419-L1433) —— 外层遍历 `app_options` 拿到每个应用名 `app_key`，内层遍历该应用的配置项 `opt_key`，仅当能读到布尔值时才记录。注意 `app_options.clear()`：每次（重新）部署后重建，避免残留已删除的应用条目。

它在 `Initialize()` 里被调用：

[Initialize 中加载 app_options](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L143) —— 与 `_UpdateUIStyle`、`global_ascii`、`show_notifications_time` 等全局设置一起，在打开 `weasel` 配置后读取。这是「启动时一次性解析」的唯一入口。

`_ReadClientInfo` 解析请求并套用应用选项：

[解析 client_app 与套用应用选项](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L420-L442) —— 第 421 行定义前缀常量 `session.client_app=`；第 422-425 行用 `starts_with` 匹配并 `to_lower` 转小写后 `wtou8` 转 UTF-8；第 432 行 `set_property` 登记；第 434-441 行在 `m_app_options` 里查找并逐个 `set_option`。

末尾同步 inline 相关开关：

[同步 inline_preedit / soft_cursor](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L443-L447) —— 注意此处读取的是 `session_status.style.inline_preedit`，而此刻样式还是 `m_base_style`（见 `AddSession` 第 189 行先赋基础样式），所以这里的值反映的是「基础样式 + 此前 `_ReadClientInfo` 未对 inline 做应用级覆盖」的初值。应用级 `inline_preedit` 的真正覆盖发生在稍后的 `_LoadAppInlinePreeditSet`（见 4.3）。

`AddSession` 里三者的调用顺序是理解全局的关键：

[AddSession 中 ReadClientInfo → 方案加载 → inline 套用的顺序](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L188-L199) —— 先 `style = m_base_style`（189），再 `_ReadClientInfo`（191）登记应用并套用**非 inline** 的应用选项，再 `_LoadSchemaSpecificSettings`（197）叠加方案样式，最后 `_LoadAppInlinePreeditSet`（198）+ `_UpdateInlinePreeditStatus`（199）单独处理 inline preedit 的应用级覆盖。这个顺序正是三级优先级的来源。

#### 4.2.4 代码实践

**实践目标：** 设计一个 `app_options` 配置，并追踪它如何影响某个应用的按键行为。

**操作步骤：**

1. 打开 [output/data/weasel.yaml](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/data/weasel.yaml#L6-L12) 查看真实出厂示例：

   ```yaml
   app_options:
     cmd.exe:
       ascii_mode: true
     conhost.exe:
       ascii_mode: true
     firefox.exe:
       inline_preedit: true # workaround for #946
   ```

2. 假设你要让某个游戏 `mygame.exe` 默认英文且关闭 vim 模式，在用户目录的 `weasel.custom.yaml`（部署后会合并进 `weasel.yaml`）里追加：

   ```yaml
   # 示例配置（非仓库原有文件，需用户自行创建 weasel.custom.yaml）
   app_options:
     mygame.exe:
       ascii_mode: true
       vim_mode: false
   ```

3. 追踪生效路径（纯源码阅读，无需运行）：
   - 重新部署后，`Initialize()` → `_LoadAppOptions`（[L143](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L143)）把 `mygame.exe → {ascii_mode:true, vim_mode:false}` 装进 `m_app_options`。
   - 当 `mygame.exe` 获得焦点，TSF 端发起 `StartSession`，请求正文含 `session.client_app=mygame.exe`（[WeaselClientImpl.cpp:187](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L187)）。
   - 服务端 `AddSession` → `_ReadClientInfo`（[L191](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L191)）解析出应用名，命中 `m_app_options`，于是 [L437-L440](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L437-L440) 对该会话执行 `set_option("ascii_mode", true)` 与 `set_option("vim_mode", false)`。

**需要观察的现象 / 预期结果：**

- 该会话的第一次按键，引擎读到的 `ascii_mode` 就是 `true`，因此字母键会**直接上屏英文**而**不进入拼音组合**——这就是「游戏里默认英文」的效果。
- `vim_mode` 关闭后，即便方案定义了 vim 切换键，u4-l2 讲过的 [vim 补丁逻辑](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L282-L286) 里 `get_option(session_id, "vim_mode")` 返回 false，不会触发自动切英文。
- 应用名大小写不敏感：写成 `MYGAME.EXE` 同样命中（`CaseInsensitiveCompare` + `_ReadClientInfo` 里 `to_lower`）。

> 以上为源码追踪型实践，未执行真实部署。若要本地验证，需在 Windows 上安装 Weasel、写入 `weasel.custom.yaml` 后用「小狼毫【重新部署】」触发合并。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `_ReadClientInfo` 里要用 `to_lower` 把应用名转小写，而 `m_app_options` 又用 `CaseInsensitiveCompare` 做大小写不敏感比较？两者是否冗余？

**参考答案：** 不完全冗余，是双重保险。`to_lower` 让 `set_property("client_app", ...)` 登记进引擎的名字是规范小写，便于后续 `_LoadAppInlinePreeditSet` 用 `get_property` 读回后比较；`CaseInsensitiveCompare` 则保证即便用户在 YAML 里写了 `CMD.EXE`、`AppOptionsByAppName::find` 也能命中。两者共同确保「无论用户怎么写、无论客户端传什么大小写」，应用名都能正确匹配。

**练习 2：** 如果同一个开关既出现在方案的 `__switches`（librime 引擎层默认值），又出现在 `app_options`，会话创建后最终值由谁决定？

**参考答案：** 由 `app_options` 决定。因为 `AddSession` 里 `create_session`（引擎按方案建默认开关）先执行（[L173](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L173)），随后 `_ReadClientInfo` 的 `set_option`（[L439](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L439)）覆盖之。`app_options` 的优先级高于方案默认。

---

### 4.3 inline preedit 动态切换

#### 4.3.1 概念说明

`inline_preedit`（内联写作串）是 Weasel 一个重要的呈现开关，回顾 u3-l3：

- **开启（true）**：拼音写作串（preedit）被**直接写进应用文档**（如记事本里能看到你敲的拼音），候选窗口只显示候选列表。优点是所见即所得，缺点是部分应用（游戏、远程桌面）对此兼容不好。
- **关闭（false）**：写作串只显示在**独立的候选窗口**顶部，不触碰应用文档；同时 `soft_cursor`（软光标）开启，在窗口里用 `|` 标示编辑位置。

`inline_preedit` 最特殊的地方在于它有**三级优先级**（CHANGELOG 原文：app_options 优先级高于方案内设定，高于 `weasel.yaml`）：

| 优先级 | 来源 | 读取处 |
|---|---|---|
| 1（最高） | `app_options[app].inline_preedit` | `_LoadAppInlinePreeditSet` |
| 2 | 方案 schema 的 `style/inline_preedit` | `_LoadSchemaSpecificSettings` → `_UpdateUIStyle` |
| 3（最低） | `weasel.yaml` 的 `style/inline_preedit` | `Initialize` → `m_base_style` |

之所以单独搞一个 `_LoadAppInlinePreeditSet`（而不是在 `_ReadClientInfo` 里顺手处理），是因为 inline preedit 不仅是一个 librime option，还**绑定到 `UIStyle` 字段并影响前端能力位**（`client_caps`），需要在样式定稿后单独同步，并且要在「运行时切方案」「切深色模式」等动态场景下重新评估。

#### 4.3.2 核心流程

inline preedit 的「动态」体现在它被**三个时机**反复重算：

1. **会话创建**（`AddSession`）：`_LoadSchemaSpecificSettings` → `_LoadAppInlinePreeditSet(ignore=true)` → `_UpdateInlinePreeditStatus`。
2. **运行时切方案**（`_GetStatus` 检测到 `schema_id` 变化）：同上三连。
3. **深色模式切换**（`UpdateColorTheme`）：对所有已存在会话重跑这三连。

`_LoadAppInlinePreeditSet(ipc_id, ignore_app_name)` 的内部决策：

1. 读取当前会话的 `client_app` 属性。
2. 若 `!ignore_app_name && m_last_app_name == app_name`，直接返回（同一应用不重复处理，避免每次按键都重算）。
3. 记住 `m_last_app_name = app_name`，保存进入时的 `inline_preedit` 旧值。
4. **查 app_options**：若该应用配了 `inline_preedit`，则同时更新 rime option 与 `session_status.style.inline_preedit`，标记 `found=true`。
5. **未命中则回退**：先把样式字段重置为 `m_base_style.inline_preedit`，再尝试从方案 schema 的 `style/inline_preedit` 读取（读到则覆盖）。
6. 若最终值与旧值不同，调用 `_UpdateInlinePreeditStatus` 重同步 librime 的 `inline_preedit`/`soft_cursor` 两个 option。

`_UpdateInlinePreeditStatus` 则是简单的「把样式字段写回引擎 option」：`set_option("inline_preedit", style值)` 且 `set_option("soft_cursor", !style值)`。

最后，前端能力位在 `_UpdateUI` 里根据引擎 option 刷新：

`_UpdateUI` 读回 `get_option("inline_preedit")`，据此设置 `session_status.style.client_caps` 的 `INLINE_PREEDIT_CAPABLE` 位。这个位最终经 IPC（`config.inline_preedit=` 与 `style=` 整块序列化）传给 TSF 前端，前端据此决定是否走内联编辑会话（详见 u3-l3）。

#### 4.3.3 源码精读

`_LoadAppInlinePreeditSet` 的 app_options 命中分支：

[在 app_options 中查找 inline_preedit 并同时更新 option 与样式](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L633-L647) —— 注意第 639-641 行**同时**调用 `set_option` 与修改 `session_status.style.inline_preedit`：前者影响引擎当下行为，后者保证后续 `_Respond` 把正确的 `config.inline_preedit=` 传给前端。`found=true` 后 `break`，跳过下面的回退逻辑。

未命中时的「base + schema」回退：

[未命中 app_options 时回退到 base 再读方案](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L648-L665) —— 先重置为 `m_base_style.inline_preedit`（最低优先级），再 `schema_open` 试图读方案的 `style/inline_preedit`（第二优先级），读到才覆盖。这就实现了「方案 > 全局」的覆盖关系。

变化检测与触发同步：

[inline_preedit 变化时触发 _UpdateInlinePreeditStatus](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L666-L667) —— 用进入函数时保存的旧值 `inline_preedit`（第 631 行）与最终值比较，不同才同步，避免无谓的 option 写入。

`_UpdateInlinePreeditStatus` 本体：

[_UpdateInlinePreeditStatus 同步两个 option](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L1501-L1511) —— `inline_preedit` 与 `soft_cursor` 永远互补：内联时不显示软光标，非内联时在独立窗口显示软光标。

`_UpdateUI` 把引擎 option 翻译成前端能力位：

[_UpdateUI 设置 client_caps 的 INLINE_PREEDIT_CAPABLE 位](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L534-L537) —— 这里读的是引擎 **option**（而非样式字段），因为 option 是 librime 在按键过程中实际查询的真相源。`INLINE_PREEDIT_CAPABLE = 1` 定义在 [include/WeaselUI.h:15-17](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUI.h#L15-L17)。

「同应用跳过」优化与 client_app 读取：

[get_property 读取 client_app 与同应用跳过](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L624-L630) —— 用 `get_property("client_app", ...)` 读回 `_ReadClientInfo` 登记的应用名（注意是静态缓冲 `_app_name[50]`，有长度上限）。若与 `m_last_app_name` 相同且非强制模式，直接返回——这就是为什么切焦点到同应用的另一个窗口不会触发重算。

运行时切方案的动态重算入口在 `_GetStatus`：

[_GetStatus 检测 schema 变化并重算 inline_preedit](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L1451-L1463) —— 当 `get_status` 返回的 `schema_id` 与 `m_last_schema_id` 不同时，先保存旧 `inline_preedit` 值，再依次调 `_LoadSchemaSpecificSettings` 与 `_LoadAppInlinePreeditSet(ignore=true)`，若前后值变化再 `_UpdateInlinePreeditStatus`。注释「in case of inline_preedit set in schema」点明了为何方案切换后要重新评估 inline。

> 全屏布局会强制关闭 inline preedit，作为一条「优先级之外的硬规则」：[_UpdateUIStyle 中全屏布局强制 inline_preedit=false](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L1273-L1277)。这是因为全屏候选窗本身要占据屏幕，再内联写作串会冲突。

#### 4.3.4 代码实践

**实践目标：** 追踪一次「应用级 inline_preedit 覆盖方案设置」的完整数据流，验证三级优先级。

**操作步骤（源码阅读型）：**

1. 假设配置如下：
   - `weasel.yaml` 的 `style/inline_preedit: false`（基础：关闭）。
   - 某方案 `luna_pinyin.schema.yaml` 的 `style/inline_preedit: true`（方案：开启）。
   - `weasel.yaml` 的 `app_options/firefox.exe/inline_preedit: false`（应用：关闭）。

2. 想象在 Firefox 里建立会话，按 `AddSession` 的代码顺序（[L188-L199](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L188-L199)）逐步推演 `session_status.style.inline_preedit` 与引擎 option 的取值：

   | 步骤 | 代码行 | `style.inline_preedit` | 引擎 option | 说明 |
   |---|---|---|---|---|
   | 0 初始 | L189 `style = m_base_style` | `false` | — | 取基础值 |
   | 1 `_ReadClientInfo` | L444-L445 | `false` | `false` | 把当前（基础）值写进 option |
   | 2 方案加载 | L197 → `_UpdateUIStyle` L1189 | `true` | `false`（未同步） | 方案覆盖样式字段，但 option 暂未变 |
   | 3 app 套用 | L198 → L639-L641 | `false` | `false` | app_options 命中，样式与 option 都被覆盖回 false |
   | 4 最终同步 | L199 `_UpdateInlinePreeditStatus` | `false` | `false` | 再次确认 option 与样式一致 |

3. 验证结论：最终 Firefox 会话的 inline preedit 为 **false**，即 app_options（优先级 1）压过了方案（优先级 2）。

**需要观察的现象 / 预期结果：**

- 三级优先级确实由 `AddSession` 的调用顺序 + `_LoadAppInlinePreeditSet` 内部「命中则覆盖、未命中才回退」的分支共同保证。
- 若把第 3 步的 `firefox.exe` 从 app_options 移除，则第 3 步走「未命中」分支（[L648-L665](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L648-L665)）：重置为 base（false）再读方案（true），最终为 **true**——方案生效。
- 第 2 步之后样式字段与引擎 option 短暂不一致，靠第 4 步 `_UpdateInlinePreeditStatus` 统一，这正是该函数存在的意义。

> 这是纯静态推演，标注为「待本地验证」：真实运行需要 Windows 环境与对应 YAML 配置，可用 `DLOG(INFO)` 日志（[L438](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L438) 已有 `set app option` 日志）核对。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `_LoadAppInlinePreeditSet` 在 `AddSession` 里被传入 `ignore_app_name=true`，而它在 `_GetStatus` 里也被传入 `true`？什么场景下会用 `false`？

**参考答案：** 在 `AddSession` 与切方案时，无论应用名是否与上次相同都必须重算（因为样式来源——方案——变了），所以传 `true` 跳过「同应用跳过」优化。`ignore_app_name=false`（默认值）适用于「仅因焦点切换、应用可能变了」的常规刷新场景：若前后应用相同就跳过，避免无谓重算。当前代码库中实际调用都传 `true`，`false` 分支是留作通用刷新的扩展点。

**练习 2：** `_UpdateInlinePreeditStatus` 把 `soft_cursor` 设为 `!inline_preedit`。请结合 u3-l3 解释：为什么内联时要关掉软光标？

**参考答案：** 内联模式下，写作串与编辑光标由**应用文档自身**呈现（TSF 的 composition + DisplayAttribute 虚线下划线，见 u3-l4），Weasel 不需要在候选窗口里再画一份拼音和光标，所以 `soft_cursor` 关闭、候选窗口只显示候选。非内联模式下，写作串整体搬到独立候选窗口，此时需要 `soft_cursor`（一个 `|` 字符）在窗口顶部标示引擎的编辑位置，故开启。

**练习 3：** 全屏布局（`LAYOUT_HORIZONTAL_FULLSCREEN`）下，即便 app_options 配了 `inline_preedit: true`，最终 inline 是否生效？

**参考答案：** 不生效。`_LoadAppInlinePreeditSet` 确实会先把 `style.inline_preedit` 置为 `true`，但全屏布局的样式重算会经 `_UpdateUIStyle` 的 [L1273-L1277](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L1273-L1277) 硬性置回 `false`。这条「全屏强制关闭」是高于三级优先级的硬规则。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个端到端的配置设计任务。

**任务：** 假设你是一个经常在 `powershell.exe` 里敲命令、同时又在 `chrome.exe` 里用双拼方案打字的用户。你希望：

- 在 `powershell.exe` 里默认英文（少打中文命令），并且不要内联写作串（避免 PowerShell 输入框错乱）。
- 在 `chrome.exe` 里启用内联写作串（所见即所得），且不受方案默认值影响。
- 全局基础样式下 `inline_preedit` 默认关闭。
- 当前双拼方案的 `style/inline_preedit: true`。

**要求：**

1. 写出满足上述诉求的 `weasel.custom.yaml` 片段（`app_options` 段）。
2. 用一张表分别预测 `powershell.exe` 与 `chrome.exe` 会话创建后，`style.inline_preedit` 与引擎 `inline_preedit` option 的最终值，并指出由哪一级优先级决定。
3. 写出这两个应用会话在 `AddSession` 中经历的 4 个关键步骤（对应 [L188-L199](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L188-L199)）各自的取值变化。

**参考答案要点：**

1. 配置片段（示例，非仓库原有文件）：

   ```yaml
   app_options:
     powershell.exe:
       ascii_mode: true
       inline_preedit: false
     chrome.exe:
       inline_preedit: true
   ```

2. 预测表：

   | 应用 | 最终 inline | 决定层级 | 理由 |
   |---|---|---|---|
   | powershell.exe | `false` | app_options（优先级 1） | app_options 显式 `false`，压过方案的 `true` |
   | chrome.exe | `true` | app_options（优先级 1） | app_options 显式 `true`，与方案一致但由 app 决定 |

3. 4 步取值变化（以 powershell.exe 为例）：初始 `false`（base）→ `_ReadClientInfo` 同步 option `false` → 方案加载把样式字段改为 `true`（option 暂不变）→ `_LoadAppInlinePreeditSet` 命中 app_options 把样式与 option 都改回 `false` → `_UpdateInlinePreeditStatus` 最终确认 `false`。chrome.exe 同理，只是第 3 步最终落在 `true`。

完成本任务后，你应当能独立为新应用设计配置并准确预测其行为。

## 6. 本讲小结

- **方案专属配置**由 `_LoadSchemaSpecificSettings` 加载：先重置到 `m_base_style`，再用 `initialize=false` 的 `_UpdateUIStyle` 把方案的 `style:` 段**叠加**覆盖，最后单独加载方案配色（含深色模式回退）与图标。`_RimeGetBool` 的 `cond` 短路逻辑是「叠加而非重置」的底层实现。
- **应用级 AppOptions** 用 `AppOptionsByAppName`（`CaseInsensitiveCompare` 大小写不敏感）存储；`_LoadAppOptions` 在 `Initialize` 时一次性解析 `weasel.yaml` 的 `app_options` map，`_ReadClientInfo` 在每次 `AddSession` 时按 `client_app` 套用并 `set_option`。应用名由 TSF 端写入请求正文，经 IPC 传到服务端。
- **inline preedit 有三级优先级**：app_options > 方案 > `weasel.yaml`，外加「全屏布局强制关闭」一条硬规则。`_LoadAppInlinePreeditSet` 负责应用级覆盖与回退，`_UpdateInlinePreeditStatus` 负责把样式字段同步回引擎 option（`inline_preedit` 与 `soft_cursor` 互补）。
- 三类配置都是**动态重算**的：会话创建、运行时切方案（`_GetStatus` 检测 `schema_id` 变化）、深色模式切换（`UpdateColorTheme`）三个时机都会重新走一遍样式与 inline 的加载链。
- inline preedit 最终通过 `_UpdateUI` 把引擎 option 翻译成 `client_caps` 的 `INLINE_PREEDIT_CAPABLE` 位，经 IPC 传给 TSF 前端，前端据此决定是否走内联编辑会话——这就把本讲与 u3-l3 的上屏机制接上了。

## 7. 下一步学习建议

- **继续 u4 单元**：本讲聚焦「配置加载」，下一讲 u4-l4「UI 更新、消息通知与维护/主题」会讲 `_UpdateUI` 如何把 `Context/Status/Style` 推送给 UI 并控制显隐，以及 `OnNotify`/`_ShowMessage` 的提示消息机制——那是 `_LoadSchemaSpecificSettings` 里 `_UpdateShowNotifications` 配置项的真正消费方。
- **回顾 u3-l3**：理解 inline preedit 如何影响 TSF 的 EditSession 与上屏流程，建议重读 u3-l3 的「内联 preedit 模式 vs 独立候选窗口模式」对照表，与本讲的 `INLINE_PREEDIT_CAPABLE` 能力位传递呼应。
- **延伸阅读**：
  - 真实出厂配置 [output/data/weasel.yaml](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/data/weasel.yaml) 完整版，看 `style:` 段都有哪些字段可配。
  - [CHANGELOG.md](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/CHANGELOG.md) 中关于 `app_options` 大小写不敏感与 `inline_preedit` 优先级的历史记录。
  - 进入 u7-l3「配色方案与样式定制实战」做一次完整的 `weasel.custom.yaml` 配色定制，把本讲的配置机制与 UIStyle 颜色字段、颜色格式转换串成实战。
