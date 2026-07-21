# WeaselSetup 安装与 IME 注册

## 1. 本讲目标

本讲聚焦 Weasel 的「安装器」`WeaselSetup.exe`。它是用户与系统打交道的第一个 Weasel 程序，负责把 `weasel.dll`（TSF 文本服务）落地到 Windows 系统目录、向 TSF 框架注册输入法、写入注册表配置，以及反向卸载。

读完本讲，你应当能够：

- 说清 `WeaselSetup.exe` 在不同命令行参数下的行为（安装简体/繁体、卸载、修改、设语言/更新通道等）。
- 画出「复制文件 → `regsvr32` → `enable_profile` → `InstallLayoutOrTip` → 写注册表 → 配 WER」这条安装调用序列，并理解每一步对应的系统机制。
- 解释卸载为什么必须**先停掉 `WeaselServer`**，以及 `.old.N` + `MOVEFILE_DELAY_UNTIL_REBOOT` 这套文件占用兜底机制。
- 理解 `WeaselSetup`（安装器进程）与 `weasel.dll`（被安装的文本服务）之间通过 `regsvr32` + `DllRegisterServer` 形成的**跨进程分工**，并把它与 [u3-l1](u3-l1-tsf-registration-and-lifecycle.md) 讲过的 TSF 三层注册对应起来。

## 2. 前置知识

在进入源码前，先建立几个 Windows 输入法安装的关键概念。本讲默认你已经学过 [u3-l1（TSF IME 的注册与生命周期）](u3-l1-tsf-registration-and-lifecycle.md)，那里讲了 `weasel.dll` 内部的 `DllRegisterServer` 做了哪三层注册。本讲讲的是**谁来调用** `DllRegisterServer`、以及调用前后还要做哪些系统级动作。

### 2.1 TSF 文本服务（TIP）与「自注册」

Windows 的 TSF（Text Services Framework）里，一个输入法叫 **TIP（Text Input Processor）**，本质是一个 COM 进程内服务器（DLL）。DLL 要被系统识别为输入法，需要：

1. **COM 类注册**：在 `HKEY_CLASSES_ROOT\CLSID\{...}\InprocServer32` 写入 DLL 路径和线程模型（`Apartment`）。这正是 `DllRegisterServer` 的活儿。
2. **文本服务 Profile 注册**：通过 `ITfInputProcessorProfileMgr::RegisterProfile` 把「这个 CLSID + 这个语言（LANGID）+ 这个 Profile GUID」三元组登记进 TSF，输入法才会出现在系统「语言首选项」列表里。
3. **能力类别注册**：通过 `ITfCategoryMgr::RegisterCategory` 声明这个 TIP 是键盘类、支持 UIElement、支持沉浸式等。

这三件事，u3-l1 已经讲过是在 `WeaselTSF/Register.cpp` 里完成的。关键在于：**它们由 DLL 自己实现，但需要外部触发**。Windows 提供的标准触发工具就是 `regsvr32.exe`——它 `LoadLibrary` 目标 DLL，找到导出函数 `DllRegisterServer`（注册）或 `DllUnregisterServer`（注销，`regsvr32 /u`）并调用。本讲的核心之一，就是 `WeaselSetup` 如何调用 `regsvr32` 来驱动这套自注册。

### 2.2 LANGID：简体与繁体

Windows 用 **LANGID（Language Identifier）** 标识语言。本讲反复出现两个值：

- `0x0804`：简体中文（`MAKELANGID(LANG_CHINESE, SUBLANG_CHINESE_SIMPLIFIED)`，中国大陆）。
- `0x0404`：繁体中文（`MAKELANGID(LANG_CHINESE, SUBLANG_CHINESE_TRADITIONAL)`，中国台湾）。

Weasel 安装时让用户选简体或繁体，本质就是决定把文本服务注册到哪个 LANGID 下。这两个值的宏定义在 [WeaselTSF/Globals.h:7-15](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Globals.h#L7-L15)。

### 2.3 两个关键的注册 GUID

整个 Weasel 只有一个文本服务 CLSID 和一个 Profile GUID，它们被**重复定义**在两处（因为 `WeaselSetup.exe` 和 `weasel.dll` 是两个独立编译单元，不共享内部头）：

- 文本服务 CLSID `c_clsidTextService = {A3F4CDED-B1E9-41EE-9CA6-7B4D0DE6CB0A}`
- Profile GUID `c_guidProfile = {3D02CAB6-2B8E-4781-BA20-1C9267529467}`

在 [WeaselSetup/imesetup.cpp:11-23](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L11-L23) 与 [WeaselTSF/Globals.cpp:10-22](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Globals.cpp#L10-L22) 里完全一致。修改它们等同于换了一个新输入法，必须**同步改两处**。

### 2.4 UAC 提权与系统目录

安装输入法要写 `C:\Windows\System32`（系统目录）和 `HKEY_LOCAL_MACHINE`（机器级注册表），两者都需要**管理员权限**。所以 `WeaselSetup` 在动手前会先自检权限，不够就用 Shell 的 `runas` 动词重新拉起自己（UAC 弹窗）。`GetSystemDirectoryW` 返回的路径会因进程位数（32/64）和 WOW64 文件系统重定向而指向不同目录，这是本讲文件落点逻辑最绕的地方，我们会在模块 3 详细拆解。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [WeaselSetup/WeaselSetup.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/WeaselSetup.cpp) | 安装器主程序：`_tWinMain` 入口、命令行派发 `Run`、`CustomInstall` 交互式安装流程、`IsProcAdmin`/`RestartAsAdmin` 权限处理。 |
| [WeaselSetup/InstallOptionsDlg.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/InstallOptionsDlg.cpp) | 安装选项对话框：选简/繁体、选默认/自定义用户目录、卸载按钮。 |
| [WeaselSetup/InstallOptionsDlg.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/InstallOptionsDlg.h) | 对话框类定义，以及公共工具 `SetRegKeyValue`（带 WOW64 重定向开关的注册表读写）和一组消息框宏。 |
| [WeaselSetup/imesetup.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp) | 安装/卸载的「重活」核心：文件复制 `install_ime_file`/删除 `uninstall_ime_file`、TSF 注册 `register_text_service`、Profile 启停 `enable_profile`、顶层 `install`/`uninstall`、键盘布局登记 `InstallLayoutOrTip`、WER 崩溃转储配置。 |
| [WeaselTSF/Register.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Register.cpp) | u3-l1 已讲：`weasel.dll` 被 `regsvr32` 加载时，由 `DllRegisterServer` 调用的三层注册实现。本讲把它作为「被驱动方」引用。 |
| [WeaselTSF/Globals.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Globals.h) | LANGID 宏、文本服务描述等常量。 |
| [include/WeaselConstants.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselConstants.h) | `WEASEL_REG_KEY`、`RIME_REG_KEY` 等注册表路径常量。 |
| [output/install.bat](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/install.bat) / [output/uninstall.bat](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/uninstall.bat) / [output/stop_service.bat](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/stop_service.bat) | 发行包里的安装/卸载脚本，展示了「先停服务再卸载」的顺序约束。 |

## 4. 核心概念与源码讲解

### 4.1 安装选项对话框：让用户选简繁体与用户目录

#### 4.1.1 概念说明

`WeaselSetup.exe` 不带参数运行时，弹出一个图形对话框，让用户做三件事：

1. 选**简体**还是**繁体**（决定文本服务注册到 LANGID `0x0804` 还是 `0x0404`）。
2. 选用户数据目录是**默认**（`%APPDATA%\Rime`）还是**自定义**。
3. 如果已经装过，提供**卸载/修改**入口。

这个对话框是 WTL 的 `CDialogImpl<>` 子类，把 UI 选择的结果存进三个公有成员 `bool hant`、`std::wstring user_dir`、`bool installed`，供调用方读取。它本身不做任何系统修改（除了「卸载」按钮），真正的安装动作在调用方 `CustomInstall` 里。

#### 4.1.2 核心流程

对话框的生命周期：

```
CustomInstall() 读取注册表旧值 → 构造 InstallOptionsDialog(hant, user_dir, installed)
        → dlg.DoModal()                          // 模态显示，阻塞直到用户点 OK/取消
            ├─ OnInitDialog: 根据状态初始化单选按钮、启用/禁用控件
            ├─ 用户交互:
            │    ├─ 选「自定义目录」→ OnUseCustomDir: 弹文件夹选择器
            │    ├─ 选「默认目录」  → OnUseDefaultDir: 禁用目录输入框
            │    └─ 点「卸载」     → OnRemove: 调 uninstall(false)，并刷新按钮
            └─ OnOK: 把单选结果写回 hant / user_dir，EndDialog(IDOK)
        → 回到 CustomInstall，读取 hant / user_dir 继续安装
```

#### 4.1.3 源码精读

初始化时，根据是否已安装来切换 UI 形态——没装时简/繁单选可选、卸载按钮灰掉；已装时简/繁灰掉（避免改语言）、卸载按钮可用、OK 按钮文字变成「修改」。见 [WeaselSetup/InstallOptionsDlg.cpp:14-46](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/InstallOptionsDlg.cpp#L14-L46)，关键几句：

```cpp
CheckRadioButton(IDC_RADIO_CN, IDC_RADIO_TW,
                 (hant ? IDC_RADIO_TW : IDC_RADIO_CN));          // 默认勾选简/繁
cn_.EnableWindow(!installed);                                    // 已装则锁定语言
tw_.EnableWindow(!installed);
remove_.EnableWindow(installed);                                 // 已装才允许卸载
```

点确定时把 UI 状态回写到成员变量，见 [WeaselSetup/InstallOptionsDlg.cpp:53-64](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/InstallOptionsDlg.cpp#L53-L64)：

```cpp
hant = (IsDlgButtonChecked(IDC_RADIO_TW) == BST_CHECKED);        // 繁体?
if (IsDlgButtonChecked(IDC_RADIO_CUSTOM_DIR) == BST_CHECKED) {
  dir_.GetWindowTextW(text);
  user_dir = text;                                               // 自定义目录
} else {
  user_dir.clear();                                              // 默认目录用空串表示
}
```

> 注意「默认目录」用 `user_dir` 为空串来表示，这个约定会一直传到 `CustomInstall` 里，由它兜底展开成 `%APPDATA%\Rime`。

自定义目录用现代的 `CShellFileOpenDialog` + `FOS_PICKFOLDERS` 弹出文件夹选择器，并把当前输入框里的路径设为初始定位，见 [WeaselSetup/InstallOptionsDlg.cpp:86-116](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/InstallOptionsDlg.cpp#L86-L116)。

对话框的命令路由用 WTL 的消息映射宏集中声明，这是 WTL 的典型写法，见 [WeaselSetup/InstallOptionsDlg.h:82-90](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/InstallOptionsDlg.h#L82-L90)。

#### 4.1.4 代码实践

**实践目标**：理解「已安装」与「未安装」两种状态下对话框的 UI 差异，以及「修改」流程如何不重复注册。

**操作步骤**（源码阅读型实践）：

1. 打开 [WeaselSetup/WeaselSetup.cpp:57-141](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/WeaselSetup.cpp#L57-L141) 的 `CustomInstall` 函数。
2. 找到读取旧配置的注册表读取段（`RegOpenKey(HKEY_CURRENT_USER, ...)` 读取 `RimeUserDir` 和 `Hant`）。
3. 注意这段逻辑：当读到 `Hant` 旧值且 `installing==true` 时，会把 `silent` 置为 `true`。
4. 注意 `if (!_has_installed) if (0 != install(hant, silent)) return 1;`——只有没装过才真正调 `install`。

**需要观察的现象**：

- 「修改」模式下（已安装），`_has_installed` 为真，`install` **不会被再次调用**，因此不会重复 `regsvr32`、重复注册 Profile。
- 那么修改时做了什么？看 [WeaselSetup/WeaselSetup.cpp:123-138](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/WeaselSetup.cpp#L123-L138)：它在一个 detach 的线程里依次 `ShellExecuteW` 启动 `WeaselServer.exe /q`（退出旧服务）、再启动 `WeaselServer.exe`（拉起新服务）、最后 `WeaselDeployer.exe /deploy`（重新部署）。

**预期结果**：能复述「修改」走的是「写回注册表 + 重启服务 + 重新部署」三步，而不是重装文件。

> 待本地验证：在真实 Windows 上装一次 Weasel，再双击 `WeaselSetup.exe`，观察对话框 OK 按钮文字是否变为「修改」，且点击后不出现注册进度而直接弹出「修改成功」提示。

#### 4.1.5 小练习与答案

**练习 1**：对话框里「简体/繁体」单选按钮在已安装时被禁用（`EnableWindow(!installed)`），这是为什么？如果允许用户在「修改」时切换简繁，需要改动哪些地方？

**参考答案**：因为简繁对应不同的 LANGID（`0x0804`/`0x0404`），切换语言等于要把文本服务在另一个语言下重新注册、并把旧语言的 Profile 移除——`CustomInstall` 在「修改」分支里根本不调 `install`，所以即便 UI 允许选，也不会生效。要支持「修改时切语言」，需要在「修改」分支里也走一次卸载（`enable_profile(FALSE, 旧hant)` + `regsvr32 /u`）再用新 `hant` 重新 `install`，并且要更新 `HKCU\Software\Rime\Weasel\Hant`。

**练习 2**：`user_dir` 为空串代表「默认目录」。请追踪这个空串最终在哪里被展开成真实路径 `%APPDATA%\Rime`。

**参考答案**：在 [WeaselSetup/WeaselSetup.cpp:103-108](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/WeaselSetup.cpp#L103-L108)，`CustomInstall` 里 `if (user_dir.empty())` 分支用 `ExpandEnvironmentStringsW(L"%APPDATA%\\Rime", ...)` 展开，再写回注册表 `RimeUserDir`。

---

### 4.2 命令行派发与权限提升

#### 4.2.1 概念说明

`WeaselSetup.exe` 既能弹对话框（交互式安装），也能纯命令行静默安装/配置。它支持一长串参数，由 `Run(LPTSTR lpCmdLine)` 集中派发。又因为安装/卸载需要管理员权限，`Run` 在执行任何「重活」之前都会先 `IsProcAdmin()` 检查，不够就 `RestartAsAdmin` 重新以 `runas` 提权拉起自己。

理解这一层很重要：发行脚本 `install.bat`/`uninstall.bat` 调用的就是 `WeaselSetup.exe /s`、`WeaselSetup.exe /u` 这些命令行形态。

#### 4.2.2 核心流程

`Run` 的派发是一个线性的 `if-else` 链，**顺序敏感**——先处理不需要管理员权限的「轻量配置」命令（它们只写 `HKEY_CURRENT_USER`），最后兜底到需要提权的「重活」：

```
Run(lpCmdLine):
  /? /help          → 显示帮助，返回
  /u                → 卸载：需管理员，IsProcAdmin? 否则 RestartAsAdmin
  /userdir:<dir>    → 写 HKCU RimeUserDir           ┐
  /ls /lt /le       → 写 HKCU Language              │ 这些只写 HKCU，
  /eu /du           → 写 HKCU Updates\CheckForUpdates│ 当前用户权限即可
  /toggleime ...    → 写 HKCU ToggleImeOnOpenClose  │
  /testing /release → 写 HKCU UpdateChannel         ┘
  ─── 到这里仍未返回，说明是要装/卸载重活 ───
  if (!IsProcAdmin()) return RestartAsAdmin(lpCmdLine);   // 提权
  /s                → install(false=简体, silent)
  /t                → install(true =繁体, silent)
  /i 或无参         → CustomInstall(installing)
```

注意一个细节：`/u`（卸载）在权限检查**之前**就判断了，因为卸载和安装一样需要管理员，所以它在分支内自己做 `IsProcAdmin`/`RestartAsAdmin`；而 `/s`/`/t`/`/i` 这些安装命令则统一落到函数末尾的 `if (!IsProcAdmin()) return RestartAsAdmin(lpCmdLine);` 一处提权。

#### 4.2.3 源码精读

完整的帮助文本列出了所有命令行参数，是最好的速查表，见 [WeaselSetup/WeaselSetup.cpp:152-179](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/WeaselSetup.cpp#L152-L179)。注意 `/s`（简体）/`/t`（繁体）这两个最常用的安装参数：

```cpp
bool hans = !wcscmp(L"/s", lpCmdLine);
if (hans) return install(false, silent);   // silent=true，全程不弹消息框
bool hant = !wcscmp(L"/t", lpCmdLine);
if (hant) return install(true, silent);
bool installing = !wcscmp(L"/i", lpCmdLine);
return CustomInstall(installing);          // 无参也走这里，弹对话框
```

见 [WeaselSetup/WeaselSetup.cpp:234-241](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/WeaselSetup.cpp#L234-L241)。

`IsProcAdmin` 用 `CheckTokenMembership` 检查当前进程令牌是否属于 `Administrators` 组，这是 Win32 判断管理员权限的标准做法，见 [WeaselSetup/WeaselSetup.cpp:245-261](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/WeaselSetup.cpp#L245-L261)。

权限不够时 `RestartAsAdmin` 用 `ShellExecuteEx` + `lpVerb = "runas"` 触发 UAC，并以 `SEE_MASK_NOCLOSEPROCESS` + `WaitForSingleObject` 等待提权后的子进程结束、取其退出码作为本进程退出码，见 [WeaselSetup/WeaselSetup.cpp:263-283](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/WeaselSetup.cpp#L263-L283)：

```cpp
execInfo.lpVerb = _T("runas");                 // UAC 提权动词
execInfo.fMask = SEE_MASK_NOASYNC | SEE_MASK_NOCLOSEPROCESS;
...
::WaitForSingleObject(execInfo.hProcess, INFINITE);
::GetExitCodeProcess(execInfo.hProcess, &dwExitCode);
```

> 这种「自我重启提权」是 Windows 安装器的通用范式：主进程以普通权限跑轻逻辑，遇到需要管理员的操作就用 `runas` 拉起一个提权的自己，原进程等待并接力退出码。

#### 4.2.4 代码实践

**实践目标**：核对命令行参数与实际行为，建立「参数 → 行为 → 是否需提权」的完整心智表。

**操作步骤**（源码阅读型实践）：

1. 阅读帮助文本 [WeaselSetup/WeaselSetup.cpp:159-176](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/WeaselSetup.cpp#L159-L176)。
2. 对照 `Run` 函数体，为每个参数标注：写到哪个注册表键、是否需要管理员、是否静默。
3. 特别对比 `/u` 与 `/s` 两条路径：它们都在 `IsProcAdmin` 失败时 `RestartAsAdmin`，但代码组织方式不同——`/u` 在自己的分支内提权，`/s` 依赖函数末尾统一提权。

**需要观察的现象 / 预期结果**：整理出形如下表的对照（节选）：

| 参数 | 行为 | 注册表目标 | 需管理员 |
| --- | --- | --- | --- |
| `/s` | 静默装简体 | 复制文件到 System32 + `HKLM\Software\Rime\Weasel` | 是 |
| `/t` | 静默装繁体 | 同上，LANGID=0x0404 | 是 |
| `/u` | 卸载 | 删 System32 文件 + 删 `HKLM` 键 | 是 |
| `/ls` `/lt` `/le` | 设 UI 语言 | `HKCU\Software\Rime\weasel\Language` | 否 |
| `/userdir:<dir>` | 设用户数据目录 | `HKCU\Software\Rime\weasel\RimeUserDir` | 否 |
| `/eu` `/du` | 开/关自动更新检查 | `HKCU\Software\Rime\weasel\Updates\CheckForUpdates` | 否 |

> 注意一个**易踩坑点**：轻量配置命令写的是 `HKCU\Software\Rime\weasel`（小写 w），而 `CustomInstall` 读写用户目录用的是 `HKCU\Software\Rime\Weasel`（大写 W）。注册表键名在 Windows 上不区分大小写，所以两者指向同一键，但代码里这种大小写不一致是个历史遗留，读代码时不要被迷惑。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `/ls`、`/userdir:` 这些命令不需要管理员权限，而 `/s`、`/u` 需要？

**参考答案**：前者只写 `HKEY_CURRENT_USER`（当前用户 hive，普通用户即可写），后者要写 `HKEY_LOCAL_MACHINE`（机器级，需管理员）并把文件复制进 `C:\Windows\System32`（系统目录，需管理员）。`Run` 正是据此把轻量命令排在提权检查之前，重活排在之后。

**练习 2**：`RestartAsAdmin` 用 `WaitForSingleObject(..., INFINITE)` 等待提权后的子进程。如果用户在 UAC 弹窗点了「否」，会发生什么？

**参考答案**：`ShellExecuteEx` 会返回失败（`hProcess` 为 NULL），函数走 `else` 返回 `-1`，`Run` 把 `-1` 作为程序退出码返回。即「提权被拒」表现为安装器以非零退出码结束，安装不会进行。

---

### 4.3 TSF 文本服务的注册与注销

这是本讲的核心模块。`imesetup.cpp` 里的 `install`/`uninstall`/`register_text_service`/`enable_profile` 共同完成了「把 weasel.dll 变成系统输入法」的全套系统调用。

#### 4.3.1 概念说明

安装一个 TSF 输入法，需要**两类、共四个**系统层面的动作：

**A. 文件层面**：把 `weasel.dll` 复制到系统目录（`GetSystemDirectoryW`）。

**B. 注册层面（三件事）**：

1. **DLL 自注册**：调 `regsvr32.exe /s weasel.dll`。`regsvr32` 会 `LoadLibrary(weasel.dll)` 并调用其导出的 `DllRegisterServer`——这正是 [u3-l1](u3-l1-tsf-registration-and-lifecycle.md) 讲的、由 `WeaselTSF/Register.cpp` 实现的 `RegisterServer`（写 CLSID/InprocServer32）+ `RegisterProfiles`（`ITfInputProcessorProfileMgr::RegisterProfile`）+ `RegisterCategories`（`ITfCategoryMgr::RegisterCategory`）。
2. **Profile 启停**：`WeaselSetup` 自己用 `ITfInputProcessorProfiles` 的 `EnableLanguageProfile`/`EnableLanguageProfileByDefault`/`RemoveLanguageProfile` 把上一步登记的 Profile 设为启用/默认（安装）或移除（卸载）。注意这里用的是 `ITfInputProcessorProfiles`（较细粒度的启停接口），和 `Register.cpp` 里登记用的 `ITfInputProcessorProfileMgr`（注册器）是配套的两层。
3. **键盘布局登记**：调 `input.dll` 的 `InstallLayoutOrTip`，用形如 `"0804:{CLSID}{ProfileGUID}"` 的字符串把输入法登记进系统的输入方法列表，让它在「语言首选项」里可见。

理解关键：**`WeaselSetup`（安装器进程）与 `weasel.dll`（被安装的 DLL）是跨进程协作**。`WeaselSetup` 不直接调用 `RegisterProfile`，而是通过 `regsvr32` 把 DLL 加载进 `regsvr32` 的进程空间，让 DLL 自己完成 COM 自注册。`WeaselSetup` 只负责 regsvr32 前后的「协调」：先禁用旧 Profile、设好 `TEXTSERVICE_PROFILE` 环境变量告诉 DLL 装简还是繁、regsvr32 完成后再启用新 Profile。

> 为什么要用环境变量 `TEXTSERVICE_PROFILE` 传简繁？因为 `regsvr32` 调用 `DllRegisterServer` 是无参的，`weasel.dll` 无法从函数参数知道用户选了简还是繁。`WeaselSetup` 在启动 `regsvr32` 前设 `TEXTSERVICE_PROFILE=hans|hant`，`DllRegisterServer` 内部的 `RegisterProfiles` 读取这个环境变量来决定启用哪个语言的 Profile（见 [WeaselTSF/Register.cpp:54-62](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Register.cpp#L54-L62)）。这是「无参导出函数 + 进程环境变量」传参的经典手法。

#### 4.3.2 核心流程

安装的注册部分调用序列（省略文件复制，那是模块 4.4）：

```
register_text_service(path, register_ime=true, ...):
  1. enable_profile(FALSE, hant)                  // 先移除可能存在的旧 Profile，保证幂等
  2. SetEnvironmentVariable("TEXTSERVICE_PROFILE", hant?"hant":"hans")
  3. params = " /s \"<path>\""                     // regsvr32 静默注册
  4. ShellExecuteEx("regsvr32.exe", params)        // 加载 weasel.dll → DllRegisterServer
       └─ DLL 内部（u3-l1）:
            RegisterServer()    写 HKCR\CLSID\{...}\InprocServer32 = path, Apartment
            RegisterProfiles()  读 TEXTSERVICE_PROFILE → RegisterProfile(HANS/HANT)
            RegisterCategories() 注册能力类别
  5. WaitForSingleObject(regsvr32 进程)            // 同步等待注册完成
  6. enable_profile(TRUE, hant)                    // 启用并设为默认 Profile

enable_profile(fEnable, hant):
  CoCreateInstance(CLSID_TF_InputProcessorProfiles)
  lang_id = hant ? 0x0404 : 0x0804
  if (fEnable):
      EnableLanguageProfile(CLSID, lang_id, ProfileGUID, TRUE)
      EnableLanguageProfileByDefault(CLSID, lang_id, ProfileGUID, TRUE)
  else:
      RemoveLanguageProfile(CLSID, lang_id, ProfileGUID)
```

卸载是镜像的反向过程：

```
uninstall(silent):
  1. 读 HKCU\Software\Rime\Weasel\Hant，决定用 0804 还是 0404
  2. InstallLayoutOrTip(PSZTITLE_*, ILOT_UNINSTALL)        // 反向移除键盘布局登记
  3. uninstall_ime_file(".dll", ...):
       register_text_service(register_ime=false)           // 即 regsvr32 /u weasel.dll
                                                            //   → DllUnregisterServer（u3-l1）
       delete_file(weasel.dll)
  4. RegDeleteKey(HKLM, WEASEL_REG_KEY)                    // 删 Software\Rime\Weasel
  5. RegDeleteKey(HKLM, RIME_REG_KEY)                      // 删 Software\Rime
  6. RegDeleteKeyEx(HKLM, WEASEL_WER_KEY, ...)             // 删崩溃转储配置
```

#### 4.3.3 源码精读

**`register_text_service`**：注意它**先禁后启**的幂等设计——`register_ime=true`（注册）时，先 `enable_profile(FALSE)` 移除旧 Profile，跑完 regsvr32 再 `enable_profile(TRUE)` 启用；`register_ime=false`（注销）时，只做前置的 `enable_profile(FALSE)`，见 [WeaselSetup/imesetup.cpp:302-360](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L302-L360)。核心几行：

```cpp
if (!register_ime)
  enable_profile(FALSE, hant);                       // 注销前先摘 Profile
std::wstring params = L" \"" + tsf_path + L"\"";
if (!register_ime) {
  params = L" /u " + params;                          // /u = 注销
}
{ params = L" /s " + params; }                        // /s = 静默

if (!SetEnvironmentVariable(L"TEXTSERVICE_PROFILE",
                            hant ? L"hant" : L"hans"))   // 传简繁给 DLL
  throw std::runtime_error("SetEnvironmentVariable failed");
...
shExInfo.lpFile = app.c_str();                        // regsvr32.exe
shExInfo.lpParameters = params.c_str();
if (ShellExecuteExW(&shExInfo)) {
  WaitForSingleObject(shExInfo.hProcess, INFINITE);   // 同步等 regsvr32 完成
  ...
}
if (register_ime)
  enable_profile(TRUE, hant);                         // 注册后启用 Profile
```

> 一个要点：`ShellExecuteExW` 启动的是**独立的 `regsvr32.exe` 进程**，`SetEnvironmentVariable` 设的环境变量会被子进程继承，这就是 DLL 能读到 `TEXTSERVICE_PROFILE` 的原因。

**`enable_profile`**：用 `ITfInputProcessorProfiles` 这套 TSF COM 接口做 Profile 启停，见 [WeaselSetup/imesetup.cpp:277-299](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L277-L299)：

```cpp
LANGID lang_id = hant ? 0x0404 : 0x0804;
if (fEnable) {
  pProfiles->EnableLanguageProfile(c_clsidTextService, lang_id,
                                   c_guidProfile, fEnable);
  pProfiles->EnableLanguageProfileByDefault(c_clsidTextService, lang_id,
                                            c_guidProfile, fEnable);
} else {
  pProfiles->RemoveLanguageProfile(c_clsidTextService, lang_id,
                                   c_guidProfile);
}
```

**`InstallLayoutOrTip`**：这是 input.dll 里未公开文档化的输入法安装 API，签名靠 `LoadLibrary` + `GetProcAddress` 动态取得，见 [WeaselSetup/imesetup.cpp:34](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L34) 的类型定义和 [WeaselSetup/imesetup.cpp:397-409](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L397-L409) 的调用。它的字符串参数 `"0804:{CLSID}{ProfileGUID}"` 把语言和文本服务绑死，定义在宏里：

```cpp
#define PSZTITLE_HANS  L"0804:{A3F4CDED-...}{3D02CAB6-...}"   // 简体
#define PSZTITLE_HANT  L"0404:{A3F4CDED-...}{3D02CAB6-...}"   // 繁体
```

见 [WeaselSetup/imesetup.cpp:27-32](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L27-L32)。卸载时传 `ILOT_UNINSTALL`（=1）标志反向移除，见 [WeaselSetup/imesetup.cpp:448-461](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L448-L461)。

**`install` 顶层**：串起文件复制、注册表、LayoutOrTip、WER 四件事，见 [WeaselSetup/imesetup.cpp:362-433](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L362-L433)。其中两处机器级注册表写入决定了 Weasel 的「自我定位」：

```cpp
SetRegKeyValue(HKEY_LOCAL_MACHINE, WEASEL_REG_KEY, L"WeaselRoot",
               rootDir.c_str(), REG_SZ);                    // 安装根目录
SetRegKeyValue(HKEY_LOCAL_MACHINE, WEASEL_REG_KEY, L"ServerExecutable",
               L"WeaselServer.exe", REG_SZ);                // 服务可执行名
```

`WEASEL_REG_KEY` 即 `Software\Rime\Weasel`（见 [include/WeaselConstants.h:4](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselConstants.h#L4)）。这两个值是其他组件（如 `WeaselServer` 启动、`WeaselDeployer` 定位）查找安装位置的依据。

`install` 末尾还配置了 **WER（Windows Error Reporting）崩溃转储**，让 `WeaselServer.exe` 崩溃时自动在 `WeaselLogPath()` 落一份 minidump，见 [WeaselSetup/imesetup.cpp:411-424](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L411-L424)：

```cpp
SetRegKeyValue(HKEY_LOCAL_MACHINE, WEASEL_WER_KEY, L"DumpFolder", dmpPathW.c_str(), REG_SZ, true);
SetRegKeyValue(HKEY_LOCAL_MACHINE, WEASEL_WER_KEY, L"DumpType", 0, REG_DWORD, true);   // 自定义转储
SetRegKeyValue(HKEY_LOCAL_MACHINE, WEASEL_WER_KEY, L"CustomDumpFlags", 0, REG_DWORD, true); // MiniDumpNormal
SetRegKeyValue(HKEY_LOCAL_MACHINE, WEASEL_WER_KEY, L"DumpCount", 10, REG_DWORD, true);
```

> 注意这些 WER 写入传了 `disable_reg_redirect=true`（`SetRegKeyValue` 的最后一个参数），因为 WER 配置必须写到真实的 64 位注册表视图，不能被 WOW64 重定向。`SetRegKeyValue` 据此加 `KEY_WOW64_64KEY` 标志，见 [WeaselSetup/InstallOptionsDlg.h:34-68](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/InstallOptionsDlg.h#L34-L68)。

#### 4.3.4 代码实践

**实践目标**：把「注册文本服务的系统调用序列」画成时序图，并解释卸载为什么必须先停 `WeaselServer`。

**操作步骤**（源码阅读型实践）：

1. 画出安装时 `WeaselSetup`、`regsvr32`、`weasel.dll`、TSF 框架四者的时序，标注每一步调用的 API：`SetEnvironmentVariable` → `ShellExecuteEx(regsvr32)` →（DLL 内）`DllRegisterServer` → `RegisterProfile`/`RegisterCategory` →（回到 Setup）`EnableLanguageProfile` → `InstallLayoutOrTip`。
2. 阅读卸载路径 [WeaselSetup/imesetup.cpp:435-485](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L435-L485)，注意它的顺序是「`InstallLayoutOrTip` 反注册 → `uninstall_ime_file`（含 regsvr32 /u + `delete_file`）→ 删注册表」。
3. 打开 [output/uninstall.bat](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/uninstall.bat)，注意第一行有效操作是 `call stop_service.bat`，而 [output/stop_service.bat](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/stop_service.bat) 里是 `weaselserver.exe /q`——**先停服务，再 `WeaselSetup.exe /u`**。

**需要观察的现象 / 预期结果**：

- 卸载脚本严格遵循「停服务 → 卸载」顺序。
- 为什么必须先停 `WeaselServer`？因为 `weasel.dll` 在运行期间会被 `WeaselServer.exe`（以及所有正在接受输入的应用进程）加载占用。`uninstall_ime_file` 最终要 `DeleteFile(weasel.dll)`（见 [WeaselSetup/imesetup.cpp:237](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L237)），如果文件还被占用，`DeleteFile` 会失败。Weasel 的兜底机制见 `delete_file`：删除失败时把文件改名成 `weasel.dll.old.0`、`.old.1`… 并 `MOVEFILE_DELAY_UNTIL_REBOOT` 标记重启时删除，见 [WeaselSetup/imesetup.cpp:55-67](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L55-L67)。但最干净的做法仍是先停掉服务、让文件句柄释放。

> 待本地验证：在真实 Windows 上观察「不停服务直接卸载」的现象——`weasel.dll` 通常会因被占用而走入 `.old.N` 改名 + 延迟删除分支，需要重启才能真正清除。

#### 4.3.5 小练习与答案

**练习 1**：`register_text_service` 在注册（`register_ime=true`）时，为什么要先调一次 `enable_profile(FALSE)`，跑完 `regsvr32` 再调 `enable_profile(TRUE)`？只调后面那一次不行吗？

**参考答案**：为了**幂等**。如果重复安装，旧的 Profile 登记可能已经存在且处于某种状态；先 `RemoveLanguageProfile` 清掉，再让 `regsvr32` 触发 DLL 重新 `RegisterProfile` 干净登记，最后 `EnableLanguageProfile` + `EnableLanguageProfileByDefault` 把它明确启用并设为默认。否则残留的旧 Profile 可能导致启用状态不一致。

**练习 2**：卸载流程里，`regsvr32 /u weasel.dll` 触发的 `DllUnregisterServer`（u3-l1 讲过）做了什么？它和 `WeaselSetup` 自己删注册表（`RegDeleteKey(HKLM, WEASEL_REG_KEY)`）是同一回事吗？

**参考答案**：不是同一回事。`DllUnregisterServer` 删的是 **COM/TSF 注册**：`UnregisterServer` 删 `HKCR\CLSID\{...}`、`UnregisterProfiles` 调 `UnregisterProfile`、`UnregisterCategories` 调 `UnregisterCategory`（见 [WeaselTSF/Register.cpp:92-141](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Register.cpp#L92-L141) 和 [WeaselTSF/Register.cpp:244-259](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Register.cpp#L244-L259)）。而 `WeaselSetup` 删的 `WEASEL_REG_KEY`(`Software\Rime\Weasel`)、`RIME_REG_KEY`(`Software\Rime`)、WER 键是 **Weasel 自己的应用配置**。两层注册要分别清理，缺一不可。

**练习 3**：`enable_profile` 用 `ITfInputProcessorProfiles`，而 `Register.cpp` 的 `RegisterProfiles` 用 `ITfInputProcessorProfileMgr`。这两个接口是什么关系？

**参考答案**：`ITfInputProcessorProfileMgr`（Profile Manager）是「注册器」，负责把一个 TIP 的 Profile 三元组（CLSID+LANGID+ProfileGUID）**登记进系统**，是创建动作；`ITfInputProcessorProfiles`（Profiles）是「管理器」，对已登记的 Profile 做**启用/禁用/设默认/移除**等细粒度操作。安装时先用 ProfileMgr 登记再用 Profiles 启用，两者配套。

---

### 4.4 安装目录结构与多架构文件落点

#### 4.4.1 概念说明

这一模块讲「文件到底被复制到了哪里」。Weasel 是一个**同时支持 x86/x64/ARM64/ARM32 多架构**的输入法，安装器要根据当前系统的位数和架构，把对应架构的 `weasel.dll` 放到正确的系统目录。这块逻辑由 `install_ime_file`/`uninstall_ime_file` 承担，是 `imesetup.cpp` 里最绕的部分。

核心难点是 **WOW64（Windows-on-Windows 64）文件系统重定向**：32 位进程调用 `GetSystemDirectoryW` 在 64 位 Windows 上得到的是 `C:\Windows\SysWOW64`（32 位系统目录，名字里有 WOW64 反而存 32 位 DLL），而真正的 64 位目录 `C:\Windows\System32` 被重定向隐藏了。要写 64 位 DLL 必须先 `Wow64DisableWow64FsRedirection` 关掉重定向。

#### 4.4.2 核心流程

`install_ime_file` 的落点逻辑（设 WeaselSetup.exe 自身所在目录为源目录 `srcDir`）：

```
install_ime_file(ext=".dll", hant, silent, register_text_service):
  srcPath  = srcDir + "\weasel.dll"            // 源文件（与 WeaselSetup.exe 同目录）
  destPath = GetSystemDirectoryW() + "\weasel.dll"
  copy_file(srcPath → destPath)                 // 复制「本进程位数」版本
  register_text_service(destPath, true, ...)

  if (is_wow64()):                              // 32 位 Setup 跑在 64 位系统
    Wow64DisableWow64FsRedirection()            // 关重定向，下面才能写真 64 位目录
    if (is_arm64_machine()):
      // ARM64 系统：需要 ARM32(可选) + ARM64 + x64 + ARM64X 四套
      // 1) 若支持 ARM32 WOW（Win11 24H2 之前），装 ARM32 版到 sysarm32
      // 2) 装 x64 版 weaselx64.dll
      // 3) 装 ARM64 版 weaselARM64.dll
      // 4) ARM64X 重定向器 weaselARM64X.dll 覆盖到 destPath
    else:
      // 普通 x64 系统：装 x64 版 weaselx64.dll 到真 System32
      srcPath  ← weaselx64.dll
    copy_file(srcPath → destPath)               // 复制 64 位版本
    register_text_service(destPath, true, ...)
    Wow64RevertWow64FsRedirection()
```

> ARM64X 是 Windows 11 引入的「跨架构重定向 DLL」机制：一个 `weasel.dll`（ARM64X）在加载时会被系统自动重定向到 `weaselARM64.dll`（ARM64 进程）或 `weaselx64.dll`（x64 进程）。所以 ARM64 上总共要落地三个文件 + 一个重定向器。代码注释见 [WeaselSetup/imesetup.cpp:180-184](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L180-L184)。

#### 4.4.3 源码精读

`install_ime_file` 主体见 [WeaselSetup/imesetup.cpp:121-226](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L121-L226)。源/目标路径计算：

```cpp
GetModuleFileNameW(GetModuleHandle(NULL), path, _countof(path));   // WeaselSetup.exe 全路径
...
srcPath = std::wstring(drive) + dir + L"weasel" + ext;            // 源 = 同目录的 weasel.dll
GetSystemDirectoryW(path, _countof(path));                        // 系统目录
destPath = std::wstring(path) + L"\\weasel" + ext;                // 目标 = 系统目录
```

WOW64 分支里关重定向、按架构选文件，是全文最复杂的段落，见 [WeaselSetup/imesetup.cpp:149-224](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L149-L224)。ARM64 检测靠 `IsWow64Process2` 看 `nativeMachine == IMAGE_FILE_MACHINE_ARM64`，见 [WeaselSetup/imesetup.cpp:70-86](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L70-L86)。

文件复制工具 `copy_file` 自带「占用兜底」：复制失败时把目标改名 `.old.0/.old.1…` 并标记重启删除，再复制一次，见 [WeaselSetup/imesetup.cpp:40-53](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L40-L53)。这是「文件被占用时也能升级」的关键，对应用正在运行时升级 Weasel 尤其重要。

`has_installed` 用 `C:\Windows\System32\weasel.dll` 是否存在来判断是否已安装，见 [WeaselSetup/imesetup.cpp:487-494](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselSetup/imesetup.cpp#L487-L494)，这个判断驱动了 `CustomInstall` 与 `InstallOptionsDialog` 的「已安装/未安装」UI 分支。

#### 4.4.4 代码实践

**实践目标**：搞清在 x64 与 ARM64 两种系统上，`weasel*.dll` 各落地到哪些目录。

**操作步骤**（源码阅读型实践）：

1. 假设发行包目录（`output/`）下含 `weasel.dll`(32位)、`weaselx64.dll`、`weaselARM64.dll`、`weaselARM64X.dll`、`WeaselSetup.exe`(32位)。
2. 追踪 `install_ime_file` 在「32 位 Setup + x64 系统」下的两次复制：
   - 第一次（未关重定向）：`GetSystemDirectoryW` 返回 `SysWOW64`，复制 `weasel.dll` → `C:\Windows\SysWOW64\weasel.dll`。
   - 第二次（关重定向后）：复制 `weaselx64.dll` → 真 `C:\Windows\System32\weasel.dll`。
3. 追踪 ARM64 系统下的四次复制（含可选的 ARM32）。

**预期结果**：整理出落点对照表：

| 系统 | 源文件 | 目标目录 | 目标文件名 |
| --- | --- | --- | --- |
| x64（32位 Setup） | `weasel.dll` | `C:\Windows\SysWOW64` | `weasel.dll` |
| x64（32位 Setup） | `weaselx64.dll` | `C:\Windows\System32` | `weasel.dll` |
| ARM64 | `weasel.dll` | `sysarm32`（可选） | `weasel.dll` |
| ARM64 | `weaselx64.dll` | `C:\Windows\System32` | `weaselx64.dll` |
| ARM64 | `weaselARM64.dll` | `C:\Windows\System32` | `weaselARM64.dll` |
| ARM64 | `weaselARM64X.dll` | `C:\Windows\System32` | `weasel.dll` |

> 待本地验证：在 x64 Windows 上装一次 Weasel，用 `GetSystemDirectoryW` 的视角核对 `SysWOW64\weasel.dll` 与 `System32\weasel.dll` 两个文件是否都存在、各自的位数。

#### 4.4.5 小练习与答案

**练习 1**：`install_ime_file` 第二次复制前为什么要 `Wow64DisableWow64FsRedirection`？不调会怎样？

**参考答案**：32 位 `WeaselSetup.exe` 在 64 位系统上写 `C:\Windows\System32` 会被 WOW64 文件系统重定向悄悄改写到 `SysWOW64`，结果 `weaselx64.dll`（64 位 DLL）被错误地放进了 32 位目录。`Wow64DisableWow64FsRedirection` 暂时关掉重定向，让路径写进真正的 64 位 `System32`，写完再 `Wow64RevertWow64FsRedirection` 恢复。

**练习 2**：`copy_file` 在复制失败时把目标改名成 `.old.N` 并 `MOVEFILE_DELAY_UNTIL_REBOOT`。这套机制解决的是什么问题？它和「先停 WeaselServer 再卸载」是什么关系？

**参考答案**：解决「目标文件正在被占用、无法直接覆盖/删除」的问题（升级时旧 `weasel.dll` 仍被进程加载）。改名腾出原文件名后即可写入新文件，旧文件标记为重启时删除。它与「先停 WeaselServer」是**两道互补的防线**：正常卸载脚本主动停服务让文件释放；即便忘了停或停不掉，`.old.N` + 延迟删除也能保证安装/升级不因占用而硬失败。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，画出 `WeaselSetup.exe /s`（静默安装简体）从进程启动到返回 0 的**完整调用链与系统副作用清单**。

要求覆盖：

1. **入口与权限**：`_tWinMain` → `Run("/s")` → `IsProcAdmin` 检查（不足则 `RestartAsAdmin`）。
2. **文件落点**：`install(false, silent=true)` → `install_ime_file(".dll", ...)` → 在 x64 系统上复制 `weasel.dll` 到 `SysWOW64`、`weaselx64.dll` 到 `System32`（标出 WOW64 重定向开关的位置）。
3. **TSF 注册**：`register_text_service` 的「`enable_profile(FALSE)` → 设 `TEXTSERVICE_PROFILE` → `regsvr32 /s weasel.dll`（触发 DLL 内 `RegisterServer`+`RegisterProfiles`+`RegisterCategories`）→ `enable_profile(TRUE)`」四步，并标出 regsvr32 是独立进程、靠环境变量传简繁。
4. **系统登记**：`InstallLayoutOrTip(PSZTITLE_HANS)` 把输入法登记进系统输入法列表。
5. **应用配置**：写 `HKLM\Software\Rime\Weasel\WeaselRoot` 与 `ServerExecutable`、写 WER 崩溃转储配置（标出 `disable_reg_redirect=true` 的原因）。
6. **反向对照**：在同一张图旁标注 `WeaselSetup.exe /u` 卸载的镜像步骤，并解释 `uninstall.bat` 为什么要先 `call stop_service.bat`。

产出一张「调用链时序图 + 副作用清单（文件/注册表/TSF Profile 三栏）」，并在卸载分支上标注 `.old.N` + `MOVEFILE_DELAY_UNTIL_REBOOT` 兜底与「先停服务」两道防线的位置。

> 这一步如果在本机做，可对照 `output/install.bat` 与 `output/uninstall.bat` 的实际命令核对你的清单是否完整；无法运行时，对照源码行号说明每一步即可，标注「待本地验证」。

## 6. 本讲小结

- `WeaselSetup.exe` 是 Weasel 的安装/卸载器，由 `Run` 做命令行派发：`/s`/`/t`/`/i` 安装、`/u` 卸载，外加 `/ls`/`/userdir:`/`/eu` 等只写 `HKCU` 的轻量配置命令。
- 安装/卸载需要管理员权限，靠 `IsProcAdmin` + `RestartAsAdmin`（`runas`）自我提权，原进程等待提权子进程的退出码。
- 交互式安装走 `CustomInstall` + `InstallOptionsDialog`：用户选简/繁与用户目录；「修改」模式不重复 `install`，而是写回注册表 + 重启服务 + `WeaselDeployer /deploy`。
- TSF 注册是**跨进程分工**：`WeaselSetup` 用 `regsvr32.exe` 加载 `weasel.dll` 触发其 `DllRegisterServer`（u3-l1 的三层注册），简繁通过 `TEXTSERVICE_PROFILE` 环境变量传递；前后再用 `ITfInputProcessorProfiles` 的 `enable_profile` 做幂等的 Profile 启停。
- 键盘布局登记用 `input.dll` 的 `InstallLayoutOrTip`，字符串 `"0804/0404:{CLSID}{ProfileGUID}"` 绑定语言与文本服务；卸载传 `ILOT_UNINSTALL` 反向移除。
- 文件落点要处理 WOW64 重定向与 ARM64/ARM64X 多架构：x64 上 `weasel.dll`→`SysWOW64`、`weaselx64.dll`→`System32`；`copy_file`/`delete_file` 用 `.old.N` + 延迟删除做占用兜底。
- 卸载必须**先停 `WeaselServer`**（`stop_service.bat` 的 `weaselserver.exe /q`）再 `WeaselSetup.exe /u`，否则 `weasel.dll` 被占用无法直接删除。

## 7. 下一步学习建议

- 本讲把「安装器如何驱动 `weasel.dll` 自注册」讲透了，但 DLL 内部 `DllRegisterServer` → `RegisterServer`/`RegisterProfiles`/`RegisterCategories` 的细节属于 [u3-l1（TSF IME 的注册与生命周期）](u3-l1-tsf-registration-and-lifecycle.md)，建议（回顾）对照阅读 [WeaselTSF/Register.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Register.cpp) 与本讲的 `register_text_service`，体会「安装器」与「被安装 DLL」两条线如何咬合。
- 安装完成后，系统托盘与服务进程如何启动、`WeaselServerApp` 如何组装，见下一讲 [u6-l3（系统托盘、服务进程与自动更新）](u6-l3-tray-icon-server-and-update.md)。
- 想理解「修改」模式里 `WeaselDeployer.exe /deploy` 做的部署动作，见 [u6-l1（WeaselDeployer 配置器）](u6-l1-weasel-deployer-configurator.md)。
- 对构建出 `output/` 目录里这些 `weasel*.dll`/`WeaselSetup.exe` 的过程感兴趣，回顾 [u1-l3（构建系统与从源码运行调试）](u1-l3-build-and-run-from-source.md)，留意 `build.bat` 如何产出多架构二进制。
