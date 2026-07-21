# 工具函数、常量与按键映射

> 本讲对应大纲 `u7-l2`，承接 `u3-l2 按键事件捕获：KeyEventSink`。`u3-l2` 讲的是「为什么要翻译按键、翻译完发到哪」，本讲把镜头拉近，专门讲「翻译用的那些公共工具长什么样」。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `WeaselUserDataPath / WeaselSharedDataPath / WeaselLogPath` 这三条路径各自指向哪里、由谁决定、为什么这么设计。
- 解释 `WEASEL_VERSION`、`WEASEL_CODE_NAME` 这些常量是「从哪里冒出来的」——它们并不写在源码里，而是由构建系统在编译期注入。
- 读懂 `ConvertKeyEvent` + `TranslateKeycode` 如何把一个 Windows 虚拟键（`VK_*`）翻译成 Rime/librime 认识的 ibus keycode，并能徒手整理一张常用键映射表。
- 理解为什么 Weasel 要在「Windows 按键模型」和「ibus 按键模型」之间做一层翻译，以及 `expand_ibus_modifier` 这一步在 Server 端补了什么。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 Windows 与 ibus 是两套不同的「按键编号体系」

Windows 用「虚拟键码（Virtual Key, `VK_*`）」标识按键：`VK_RETURN = 0x0D`、`VK_LEFT = 0x25`、`'A' = 0x41`……这套编号是 Microsoft 定义的，记在 `winuser.h` 里。

而 Rime 的引擎 librime 脱胎于 Linux 输入法框架 **IBus**，它内部用的是 **X11/IBus keycode**：回车是 `0xFF0D`、左方向键是 `0xFF51`、修饰键 `Shift_L` 是 `0xFFE1`……这套编号记在本讲的 [include/KeyEvent.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/KeyEvent.h) 的 `ibus::Keycode` 枚举里。

两套体系毫无关系。`VK_LEFT (0x25)` ≠ `ibus::Left (0xFF51)`。所以「抓到 Windows 按键后，必须翻译成 ibus 编号，librime 才看得懂」——这就是 `ConvertKeyEvent` 存在的全部理由。

### 2.2 配置/日志/共享数据需要「可发现、可迁移」的稳定路径

输入法要把用户词典、方案配置、日志放在固定位置，卸载重装不能丢，多用户共用一台机器不能互相串。Windows 上有三类典型位置：

- **程序自带数据**（方案、默认配置）：跟 EXE 放一起的 `data\` 目录。
- **用户数据**（个人词典、个性化配置）：`%AppData%\Rime`，或用户自指定的目录。
- **临时日志**：`%TEMP%\rime.weasel`。

本讲的路径工具就是把这三个位置稳定地算出来。

### 2.3 版本号是「编译期注入」的，不是写死在代码里

你会在源码里看到 `WEASEL_VERSION` 这个宏，但全仓库搜不到 `0.17.4` 这样的字面量。这是因为版本号由构建脚本（`build.bat` / `xbuild.bat`）计算后，作为**预处理器宏**在编译期注入。理解这条链路，是看懂「为什么改版本不用改源码」的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/WeaselUtility.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUtility.h) | 公共工具头：路径、用户名、编码转换、转义、调试流、语言判定。绝大部分是 `inline` 函数与模板，被各子工程直接 `#include`。 |
| [RimeWithWeasel/WeaselUtility.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/WeaselUtility.cpp) | 三个非内联函数的实现：`WeaselUserDataPath`、`WeaselSharedDataPath`、`GetCustomResource`。 |
| [include/WeaselConstants.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselConstants.h) | 全仓库最小的头之一：版本号、注册表键名等少数几个编译期常量。 |
| [include/KeyEvent.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/KeyEvent.h) | `KeyInfo`（拆 LPARAM 位域）、`weasel::KeyEvent`（32 位按键+掩码）、`ibus::Keycode` / `ibus::Modifier` 两张编号表。 |
| [WeaselTSF/KeyEvent.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp) | `ConvertKeyEvent`（算掩码 + 翻译 + 兜底）与 `TranslateKeycode`（巨型 `switch` 查表）。 |

辅助参考（用于把工具「用在哪」讲清楚）：

- [include/WeaselIPC.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h) 的 `GetPipeName()`：消费 `getUsername()`。
- [RimeWithWeasel/RimeWithWeasel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp)：消费 `expand_ibus_modifier` 与 `WEASEL_VERSION`。
- [weasel.props.template](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/weasel.props.template)：版本号注入的落点。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**路径与编码工具**、**版本与路径常量**、**按键映射**。

### 4.1 路径与编码工具

#### 4.1.1 概念说明

Weasel 是一个会被装进系统、长期运行、跨多个进程（TSF 前端、Server、Deployer）的程序。这些进程都需要在**约定好的固定位置**找到数据：Server 要读用户词典，Deployer 要写配置，TSF 前端要把崩溃日志写到固定目录。如果每个进程各自硬编码路径，就会各写各的、互相找不到。

`WeaselUtility.h` / `WeaselUtility.cpp` 就是把这些「位置怎么算」「字符串怎么转」「用户是谁」收敛成一组**全局可复用的工具函数**。除了路径，它还顺手收了几类跨工程都要用的杂项工具：编码转换（UTF-8 ↔ 宽字符）、字符串转义、调试输出、HRESULT 报错、当前用户名。

#### 4.1.2 核心流程

三条数据路径的决策逻辑：

```
WeaselSharedDataPath() ──> 取 EXE 自身所在目录 ──> 追加 "data"
                            （程序自带方案/默认配置就放这）

WeaselUserDataPath() ──> 先查注册表 HKCU\Software\Rime\Weasel\RimeUserDir
                     ├─ 命中且非空 ──> 用用户自指定目录
                     └─ 未命中       ──> 退回默认 %AppData%\Rime

WeaselLogPath() ──> 展开 %TEMP%\rime.weasel ──> 不存在则创建
```

三条路径的返回类型都是 `std::filesystem::path`（`WeaselUtility.h` 第 8 行 `namespace fs = std::filesystem;` 给了别名），调用方拿到后可以安全地继续 `append`、拼接，不用操心分隔符。

> 注意分工：`WeaselSharedDataPath` / `WeaselUserDataPath` 是**声明在头里、实现在 .cpp 里**的非内联函数（`WeaselUtility.h:35-36`），因为它们用了 `<filesystem>` 较重的逻辑；而 `WeaselLogPath` 是**纯内联**（`WeaselUtility.h:37-46`），因为它足够简单且想避免重复链接。这是 C++ 工具头常见的取舍。

#### 4.1.3 源码精读

**用户数据路径：注册表优先，默认兜底。**

[include/WeaselUtility.h:35-36](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUtility.h#L35-L36) 只有两行声明，真正逻辑在 [RimeWithWeasel/WeaselUtility.cpp:6-25](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/WeaselUtility.cpp#L6-L25)：

```cpp
fs::path WeaselUserDataPath() {
  WCHAR _path[MAX_PATH] = {0};
  const WCHAR KEY[] = L"Software\\Rime\\Weasel";
  HKEY hKey;
  LSTATUS ret = RegOpenKey(HKEY_CURRENT_USER, KEY, &hKey);
  if (ret == ERROR_SUCCESS) {
    DWORD len = sizeof(_path); DWORD type = 0; DWORD data = 0;
    ret = RegQueryValueEx(hKey, L"RimeUserDir", NULL, &type, (LPBYTE)_path, &len);
    RegCloseKey(hKey);
    if (ret == ERROR_SUCCESS && type == REG_SZ && _path[0]) {
      return fs::path(_path);          // 用户自指定目录命中
    }
  }
  ExpandEnvironmentStringsW(L"%AppData%\\Rime", _path, _countof(_path));
  return fs::path(_path);              // 默认 %AppData%\Rime
}
```

这段代码说明：安装时（见 `u6-l2`）或用户在设置里改目录时，会把自定义路径写进注册表 `HKCU\Software\Rime\Weasel\RimeUserDir`；这里读取时三重校验——`ERROR_SUCCESS`（键值存在）、`type == REG_SZ`（类型对）、`_path[0]`（非空串），任一不满足就走默认值。这种「能读就读，读不到就退回安全默认」是输入法这类常驻程序的典型防御式写法。

**共享数据路径：跟着 EXE 走。**

[RimeWithWeasel/WeaselUtility.cpp:27-31](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/WeaselUtility.cpp#L27-L31)：

```cpp
fs::path WeaselSharedDataPath() {
  wchar_t _path[MAX_PATH] = {0};
  GetModuleFileNameW(NULL, _path, _countof(_path));   // NULL = 当前进程主 EXE 全路径
  return fs::path(_path).remove_filename().append("data");
}
```

`GetModuleFileNameW(NULL, ...` 取的是「调用者所在进程的主 EXE 路径」。由于这个函数实现在 `RimeWithWeasel` 静态库里，它会被链接进 `WeaselServer.exe`、`WeaselDeployer.exe` 等不同进程，于是在不同进程里取到的 EXE 路径不同——但共同点是「EXE 同级的 `data\` 目录」，正好是安装时程序自带方案所在。`.remove_filename()` 去掉文件名、`.append("data")` 拼上子目录，全程用 `filesystem::path` 跨平台安全拼接。

**日志路径：内联实现，按需建目录。**

[include/WeaselUtility.h:37-46](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUtility.h#L37-L46)：把 `%TEMP%\rime.weasel` 展开后，若目录不存在就 `create_directories`，保证返回的路径一定可写。

**用户名：管道与单实例互斥体的共用原料。**

[include/WeaselUtility.h:14-32](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUtility.h#L14-L32) 的 `getUsername()` 用 `GetUserName` 两段式调用（第一次探长度、第二次取串）拿到当前 Windows 用户名。它的产物被两处关键代码消费：

- 命名管道名 [include/WeaselIPC.h:170-177](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L170-L177)，拼成 `\\.\pipe\<用户名>\WeaselNamedPipe`，实现按用户隔离（详见 `u2-l1`）。
- Server 单实例互斥体名 [WeaselIPCServer/WeaselServerImpl.cpp:142-151](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L142-L151)，拼成 `(WEASEL)Furandōru-Sukāretto-<用户名>`，保证每个用户只起一个 Server。

**编码转换：UTF-8 ↔ 宽字符的统一宏。**

librime 的 C API 内部用 UTF-8（`std::string`），而 Windows API 普遍用宽字符（`std::wstring`）。[include/WeaselUtility.h:64-100](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUtility.h#L64-L100) 提供了 `string_to_wstring` / `wstring_to_string`，并配了一组短宏 [include/WeaselUtility.h:243-247](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUtility.h#L243-L247)：

```cpp
#define wtou8(x)  wstring_to_string(x, CP_UTF8)   // 宽 -> UTF-8
#define wtoacp(x) wstring_to_string(x, CP_ACP)    // 宽 -> 系统ANSI
#define u8tow(x)  string_to_wstring(x, CP_UTF8)   // UTF-8 -> 宽
#define acptow(x) string_to_wstring(x, CP_ACP)
#define u8toacp(x) wtoacp(u8tow(x))               // UTF-8 -> ANSI（经宽字符中转）
```

这两个函数都显式限制只支持 `CP_ACP` 和 `CP_UTF8` 两种代码页（`code_page != 0 && code_page != CP_UTF8` 直接返回空串），是个刻意的收口，避免误用。

**字符串转义：给 IPC 行协议用。**

[include/WeaselUtility.h:113-184](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUtility.h#L113-L184) 用模板特化同时支持 `char` 和 `wchar_t`，把换行符转成 `\n`、制表符转成 `\t`、反斜杠转成 `\\`（`escape_string`），`unescape_string` 反向还原。这组函数配合 `u2-l5` 的响应解析：上屏文字 `commit` 里如果含换行，必须转义成单行才能塞进「一行一个 `key=value`」的响应协议，到客户端再用 `unescape_string` 还原。

**调试与错误工具。**

[include/WeaselUtility.h:249-321](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUtility.h#L249-L321) 提供了三类辅助：

- `DebugStream` + `DEBUG` 宏：析构时把累积内容一次性 `OutputDebugString` 输出，带时间戳、文件名、行号，可用 DebugView 之类工具观察。
- `HRESULTToString`：把 `HRESULT` 翻成系统错误描述文本。
- `HR(result)` 宏（`HR_Impl`）：`S_OK != result` 时记录并抛 `ComException`，是处理 COM 调用失败的统一断言风格。

#### 4.1.4 代码实践

**实践目标**：搞清「我的用户数据和日志到底落在哪」，并验证注册表覆盖机制。

**操作步骤**（源码阅读型 + 可选的本地验证）：

1. 阅读 [RimeWithWeasel/WeaselUtility.cpp:6-31](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/WeaselUtility.cpp#L6-L31)，在纸上画出 `WeaselUserDataPath` 的判定分支。
2. 用 `Grep` 在仓库里搜 `WeaselUserDataPath(` 和 `WeaselSharedDataPath(` 的调用点，记录它们分别被 Server、Deployer 哪段逻辑用到了（例如引擎初始化时设置 `shared_data_dir` / `user_data_dir`）。
3. **（待本地验证，需 Windows）** 运行 `regedit`，查看 `HKEY_CURRENT_USER\Software\Rime\Weasel` 下是否存在 `RimeUserDir`。若不存在，预测 `%AppData%\Rime` 会被作为用户目录；若手动新建一个字符串值 `RimeUserDir=C:\MyRime`，重启 WeaselServer 后，观察用户词典是否改读到新目录。
4. **（待本地验证）** 用 DebugView 抓取 Weasel 运行时由 `DEBUG` 宏输出的日志（落在 `WeaselLogPath()` 即 `%TEMP%\rime.weasel`），确认路径工具算出的目录与实际写入位置一致。

**需要观察的现象**：注册表值的有无直接改变 `WeaselUserDataPath` 的返回值；日志目录在首次运行时被自动创建。

**预期结果**：能准确说出三条路径的默认落点（`<EXE>\data`、`%AppData%\Rime`、`%TEMP%\rime.weasel`），并解释注册表如何覆盖用户目录。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `WeaselSharedDataPath` 用 `GetModuleFileNameW(NULL, ...)`，而不是写死 `C:\Program Files\Weasel\data`？

**参考答案**：写死路径会在「装到 D 盘」「绿色版」「ARM64/X64 不同目录」等场景下失效；而 `NULL` 取当前进程主 EXE 的真实路径，无论装在哪、由哪个进程调用，都能正确指向「与可执行文件同级的 `data\`」，是最稳妥的自定位方式。

**练习 2**：`string_to_wstring` 为什么只允许 `CP_ACP` 和 `CP_UTF8` 两种代码页、其余直接返回空串？

**参考答案**：项目里真正需要的窄→宽转换只有这两类（系统 ANSI 与 UTF-8）。显式收口可以在编译期/调用期就把误用（如误传 `CP_UTF7`、未初始化的代码页号）变成「安全失败返回空串」，而不是悄悄用错误编码解码出乱码——这是输入法处理多语言文本时的重要防御。

---

### 4.2 版本与路径常量

#### 4.2.1 概念说明

`include/WeaselConstants.h` 是全仓库最短的头文件之一，只定义了五个常量。它单独成文件，是因为这些常量要被「许多互不依赖的子工程」共享——如果某个子工程只为一个版本字符串去 `#include` 一个庞大头，会拖慢编译。把少数真正全局共享的常量抽出来，是 C++ 项目里常见的「轻量公共头」做法。

这里的关键不是常量本身，而是**版本号是怎么被填进去的**：源码里只写了宏的「拼装规则」，真正的数字来自构建系统。

#### 4.2.2 核心流程

版本号的注入链路：

```
build.bat / xbuild.bat
   ├─ 设环境变量 VERSION_MAJOR / VERSION_MINOR / VERSION_PATCH （默认 0 / 17 / 4）
   ├─ 算出 WEASEL_VERSION = MAJOR.MINOR.PATCH
   └─ render.js 把 weasel.props.template 渲染成 weasel.props
                              │
                              ▼
weasel.props（MSBuild UserMacros + ResourceCompile 预处理定义）
   └─ 向资源编译器注入 VERSION_MAJOR / VERSION_MINOR / VERSION_PATCH 宏
                              │
                              ▼
include/WeaselConstants.h
   └─ #define WEASEL_VERSION VERSION_STR(VERSION_MAJOR.VERSION_MINOR.VERSION_PATCH)
                              │
                              ▼
RimeWithWeasel.cpp / Configurator.cpp
   └─ weasel_traits.distribution_version = WEASEL_VERSION;  （登记给 librime）
```

核心结论：**版本号是构建期变量，不是源码字面量**。所以你想升级版本，改的是 `xbuild.bat` / `weasel.props`（或用 `update/bump-version.sh` 脚本），而不是 C++ 代码。

#### 4.2.3 源码精读

**常量本体。**

[include/WeaselConstants.h:1-10](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselConstants.h#L1-L10) 全文很短：

```cpp
#define WEASEL_CODE_NAME "Weasel"
#define WEASEL_REG_KEY L"Software\\Rime\\Weasel"
#define RIME_REG_KEY L"Software\\Rime"

#define STRINGIZE(x) #x
#define VERSION_STR(x) STRINGIZE(x)
#define WEASEL_VERSION VERSION_STR(VERSION_MAJOR.VERSION_MINOR.VERSION_PATCH)
```

两点要点：

1. `WEASEL_CODE_NAME` 是写死的 `"Weasel"`（注意它**不是**用户在 UI 上看到的「小狼毫」中文名，中文名由 `get_weasel_ime_name()` 按界面语言动态返回，见 `u7-l2` 4.1 节与 [include/WeaselUtility.h:189-201](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselUtility.h#L189-L201)）。它用作登记给 librime 的 `distribution_code_name`。
2. `WEASEL_VERSION` 用了**两层宏展开**：`VERSION_STR(VERSION_MAJOR.VERSION_MINOR.VERSION_PATCH)` 先把 `VERSION_MAJOR` 等宏替换成数字得到 `0.17.4` 这样的「预处理记号序列」，再由 `STRINGIZE`（`#x`）整体字符串化成 `"0.17.4"`。两层是因为 `#` 会让参数**不展开**，必须先在外层展开、再内层字符串化。

> 注意：`VERSION_MAJOR` / `VERSION_MINOR` / `VERSION_PATCH` 在本头里**没有定义**。它们必须由编译器命令行（`/D`）或 MSBuild 预处理定义提供，否则 `WEASEL_VERSION` 会展开成一串未定义符号、编译失败。这正是「版本号外部注入」的落点。

**注入端：weasel.props.template。**

[weasel.props.template:6-9](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/weasel.props.template#L6-L9) 把三个版本变量声明为 MSBuild 的 UserMacro（占位符 `$VERSION_MAJOR` 等，由 `render.js` 在构建时替换为真实值），并在 [weasel.props.template:26-28](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/weasel.props.template#L26-L28) 把它们塞进 `<ResourceCompile>` 的 `PreprocessorDefinitions`：

```xml
<PreprocessorDefinitions>...;VERSION_MAJOR=$(VERSION_MAJOR);VERSION_MINOR=$(VERSION_MINOR);VERSION_PATCH=$(VERSION_PATCH);...</PreprocessorDefinitions>
```

也就是说，资源编译器（`.rc` → `.res`）编译时，这三个宏就有了值。但 `WEASEL_VERSION` 在普通 C++ 里也被用到——那是因为各 `.vcxproj` 同样引用了 `weasel.props`（或等价的 `/D`），让 C++ 编译器也拿到这些宏（详见 `u1-l3` 关于 `build.bat` 驱动双轨构建的说明）。

**版本号默认值。**

`xbuild.bat` 给出兜底默认（与 `build.bat` 同理）：

```
xbuild.bat:22  if not defined VERSION_MAJOR set VERSION_MAJOR=0
xbuild.bat:23  if not defined VERSION_MINOR set VERSION_MINOR=17
xbuild.bat:24  if not defined VERSION_PATCH set VERSION_PATCH=4
xbuild.bat:26  if not defined WEASEL_VERSION set WEASEL_VERSION=%VERSION_MAJOR%.%VERSION_MINOR%.%VERSION_PATCH%
```

所以本 HEAD 的默认版本是 `0.17.4`。发布构建里 `WEASEL_BUILD` 还会加上 git 提交数、`PRODUCT_VERSION` 还会加上短 hash（见 `u1-l3`）。

**消费端：登记给 librime。**

`WEASEL_VERSION` 与 `WEASEL_CODE_NAME` 最终在引擎初始化时写进 `RimeTraits`，让 librime 知道自己是被哪个发行版、哪个版本加载的：

- [RimeWithWeasel/RimeWithWeasel.cpp:98-99](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L98-L99)：

```cpp
weasel_traits.distribution_code_name = WEASEL_CODE_NAME;
weasel_traits.distribution_version   = WEASEL_VERSION;
```

- Deployer 侧完全对称：[WeaselDeployer/Configurator.cpp:43-44](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/Configurator.cpp#L43-L44)。

- 另外 [WeaselTSF/Globals.h:17](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/Globals.h#L17) 把 `WEASEL_CODE_NAME` 作为 TSF 文本服务的英文描述 `TEXTSERVICE_DESC_A`。

> 这条登记链很重要：librime 会把 `distribution_code_name` / `distribution_version` 写进用户数据目录的 `installation.yaml`，用于判断「是否同一发行版」「是否需要重新部署」。所以版本号不只是给人看的，也参与引擎的部署决策。

#### 4.2.4 代码实践

**实践目标**：亲手追踪一个版本号从「脚本变量」走到「librime 登记值」的完整路径。

**操作步骤**（源码阅读型）：

1. 打开 [include/WeaselConstants.h:7-9](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselConstants.h#L7-L9)，手动模拟两层宏展开：假设 `VERSION_MAJOR=0`、`VERSION_MINOR=17`、`VERSION_PATCH=4`，写出 `WEASEL_VERSION` 展开成 `"0.17.4"` 的中间过程（提示：先得到记号序列 `0.17.4`，再 `#` 字符串化）。
2. 用 `Grep` 在 `*.bat`、`*.props*`、`*.vcxproj` 里搜 `VERSION_MAJOR`，确认它只在构建/工程文件里被赋值，从不在 `.cpp/.h` 里赋值。
3. 用 `Grep` 在 `*.cpp` 里搜 `WEASEL_VERSION` 与 `WEASEL_CODE_NAME`，列出全部消费点（应得到 `RimeWithWeasel.cpp`、`Configurator.cpp`、`Globals.h` 三处）。
4. **（待本地验证，需 Windows + 编译环境）** 修改 `xbuild.bat` 里 `VERSION_PATCH` 的默认值（例如改成 `5`），重新构建，观察产物 EXE 的文件版本号（右键→属性→详细信息）与用户目录 `installation.yaml` 里 `distribution_version` 是否同步变成 `0.17.5`。

**需要观察的现象**：版本号的唯一「真相源」是构建脚本；改源码常量定义（`WeaselConstants.h` 的拼装规则）并不会改变最终数字。

**预期结果**：能画出「脚本变量 → MSBuild 宏 → 预处理符号 → C++ 字符串字面量 → librime traits → installation.yaml」的完整传递图。

#### 4.2.5 小练习与答案

**练习 1**：`#define WEASEL_VERSION VERSION_STR(VERSION_MAJOR.VERSION_MINOR.VERSION_PATCH)` 为什么不能直接写成 `#define WEASEL_VERSION #VERSION_MAJOR`？

**参考答案**：`#` 运算符不会先展开其参数宏，直接 `#VERSION_MAJOR` 只会得到字符串 `"VERSION_MAJOR"`，而不是 `"0"`。必须先用一层宏（`VERSION_STR`）让 `VERSION_MAJOR` 等展开成数字、再用内层 `STRINGIZE`（`#x`）字符串化，所以需要「两层宏」。这是 C 预处理里经典的「字符串化必须双层」技巧。

**练习 2**：`WEASEL_REG_KEY`（`L"Software\\Rime\\Weasel"`）这个字符串在 4.1 节的 `WeaselUserDataPath` 里也出现过（硬编码在 .cpp 里）。这两处为什么不统一？

**参考答案**：这是仓库里的一处**轻微不一致**——`WeaselConstants.h` 定义了 `WEASEL_REG_KEY`，但 `WeaselUtility.cpp:8` 里又把同样的字符串 `L"Software\\Rime\\Weasel"` 写死了一遍，没有复用宏。功能上没问题（值相同），但理想情况下 `.cpp` 应直接用 `WEASEL_REG_KEY` 以避免将来改名时漏改。这正是一个适合新手练手的「重构小练习」。

---

### 4.3 按键映射

#### 4.3.1 概念说明

这是本讲最核心的模块。`ConvertKeyEvent` 是把「Windows 世界」翻译成「Rime 世界」的翻译器。回顾 `u3-l2`：TSF 前端在 `OnKeyDown` 里抓到一个按键（`WPARAM` 是虚拟键、`LPARAM` 是按键的位域详情、`keyState` 是当前全键盘状态），要先翻译成 `weasel::KeyEvent`，才能经命名管道发给 Server、最终交给 librime 的 `process_key`。

翻译要解决三个问题：

1. **修饰键**：Shift/Ctrl/Alt/Caps Lock 按没按？要按 ibus 的位掩码编码进 `mask`。
2. **主键码**：这个键对应 ibus 的哪个 keycode？大多数特殊键有一一对应表；但字母、数字、标点没有，要靠 Windows 的 `ToUnicodeEx` 解码出 Unicode 字符直接当 keycode。
3. **时序补丁**：Caps Lock 在 Windows 与 Rime 里的「状态变化时机」不一致，需要反转掩码来对齐。

#### 4.3.2 核心流程

一次 `ConvertKeyEvent(vkey, kinfo, keyState, result)` 的执行：

```
1. result.mask = 0
2. 读 keyState 算修饰位：
     VK_SHIFT 按下      -> SHIFT_MASK
     VK_CAPITAL 激活     -> LOCK_MASK
     VK_CONTROL 按下     -> CONTROL_MASK
     VK_MENU(Alt) 按下   -> ALT_MASK
     kinfo.isKeyUp       -> RELEASE_MASK
3. Caps Lock 时序补丁：若 vkey==VK_CAPITAL 且是按下，反转 LOCK_MASK
4. code = TranslateKeycode(vkey, kinfo)   // 查表
     ├─ 命中（非 Null） -> result.keycode = code，返回 true
     └─ 未命中         -> 走 ToUnicodeEx 兜底
5. ToUnicodeEx 兜底：临时清掉 Ctrl/Alt 状态解码出字符
     ├─ 解出 1 个字符   -> result.keycode = 该字符的 Unicode，返回 true
     └─ 解不出/死键     -> result.keycode = 0，返回 false（前端将放行该键）
```

`weasel::KeyEvent` 最终被压成一个 32 位整数经管道传输（见 `u2-l1`、`u3-l2`），其位布局为：

\[
\text{KeyEvent} = \underbrace{\text{keycode}}_{\text{低 16 位}} \;+\; \underbrace{\text{mask}}_{\text{高 16 位}} \ll 16
\]

`mask` 本身是个**压缩过的 16 位值**（`KeyEvent.h` 的 `ibus::Modifier` 枚举注释明说 "modified to fit a UINT16"）。但 librime 期望的是标准 X11 修饰位（Super 在 bit 26、Release 在 bit 30 等），所以 Server 端在调 `process_key` 前，还要用 `expand_ibus_modifier` 把压缩掩码还原：

\[
\text{expand}(m) = (m \,\&\, \texttt{0xff}) \;\big|\; \big((m \,\&\, \texttt{0xff00}) \ll 16\big)
\]

即「低 8 位原样保留（Shift/Lock/Ctrl/Alt/MOD2-5 这些在两套体系里位置一致），bit 8–15 整体左移 16 位到 bit 24–31」。对照 [include/KeyEvent.h:234-242](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/KeyEvent.h#L234-L242) 的注释 `HANDLED_MASK = 1 << 8 // 24`、`IGNORED_MASK = 1 << 9 // 25`、`RELEASE_MASK = 1 << 14 // 30`——后面的 `// 24 / 25 / 30` 正是展开后在标准 X11 里的真实位号。

#### 4.3.3 源码精读

**数据结构：把 LPARAM 拆成位域。**

Windows 把按键的细节塞进 `LPARAM`，[include/KeyEvent.h:3-15](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/KeyEvent.h#L3-L15) 用一个位域结构体把它拆开：

```cpp
struct KeyInfo {
  UINT repeatCount : 16;
  UINT scanCode : 8;
  UINT isExtended : 1;
  UINT reserved : 4;
  UINT contextCode : 1;
  UINT prevKeyState : 1;
  UINT isKeyUp : 1;
  KeyInfo(LPARAM lparam) { *this = *reinterpret_cast<KeyInfo*>(&lparam); }
};
```

其中 `scanCode`（区分左/右 Shift、左/右 Ctrl）、`isExtended`（区分小键盘回车、右 Ctrl）会直接决定翻译成 `Shift_L` 还是 `Shift_R`。这种 `reinterpret_cast` 复制是 Win32 编程里读 LPARAM 的惯用法。

**承载结构：32 位按键。**

[include/KeyEvent.h:18-27](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/KeyEvent.h#L18-L27) 的 `weasel::KeyEvent` 用位域把 `keycode` 与 `mask` 各占 16 位，并提供与 `UINT32` 的互转构造/转换，使得它可以当作一个 32 位整数直接塞进 IPC 消息（见 `u2-l1` 的 `PipeMessage`）。

**两张编号表。**

- [include/KeyEvent.h:38-217](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/KeyEvent.h#L38-L217) `ibus::Keycode`：从 `space = 0x020` 到 `Hyper_R = 0xFFEE`，覆盖功能键、方向键、小键盘、F 键、修饰键、日文 IME 专用键等。
- [include/KeyEvent.h:221-245](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/KeyEvent.h#L221-L245) `ibus::Modifier`：`SHIFT_MASK=1<<0` …… `RELEASE_MASK=1<<14`，外加 `MODIFIER_MASK = 0x2fff` 作为掩码。

**翻译主函数：算掩码 + 查表 + 兜底。**

[WeaselTSF/KeyEvent.cpp:4-59](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L4-L59)。先算掩码（`KEY_DOWN = 0x80` 判按下、`TOGGLED = 0x01` 判切换态）：

```cpp
if ((keyState[VK_SHIFT] & KEY_DOWN) != 0)   result.mask |= ibus::SHIFT_MASK;
if ((keyState[VK_CAPITAL] & TOGGLED) != 0)  result.mask |= ibus::LOCK_MASK;
if ((keyState[VK_CONTROL] & KEY_DOWN) != 0) result.mask |= ibus::CONTROL_MASK;
if ((keyState[VK_MENU] & KEY_DOWN) != 0)    result.mask |= ibus::ALT_MASK;
if (kinfo.isKeyUp)                          result.mask |= ibus::RELEASE_MASK;
```

注意 `VK_SHIFT` 用「按下」位、`VK_CAPITAL` 用「切换态」位——因为 Caps Lock 是**开关型**按键（按一下锁定、再按一下解除），其状态语义是「是否处于大写锁定」，用 `TOGGLED` 判定。

**Caps Lock 时序补丁**，[WeaselTSF/KeyEvent.cpp:30-35](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L30-L35)：

```cpp
if (vkey == VK_CAPITAL && !kinfo.isKeyUp) {
  // rime assumes XK_Caps_Lock to be sent before modifier changes,
  // while VK_CAPITAL has the modifier changed already.
  result.mask ^= ibus::LOCK_MASK;   // 异或=反转
}
```

这段注释点出一个关键时序差：librime 假设「Caps Lock 按键事件先到、状态后变」，而 Windows 在送达 `VK_CAPITAL` 事件时**键盘状态已经翻转**了。于是上面第 2 步读 `keyState[VK_CAPITAL]` 得到的是「翻转后」的值，与 librime 期望的相反——所以这里用异或 `^=` 把 `LOCK_MASK` 反转一次，人工对齐 librime 的假设。（`u3-l2` 讲过配合抬键时 `SendInput` 模拟两次击键的进一步补丁。）

**查表**：[WeaselTSF/KeyEvent.cpp:38-42](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L38-L42) 调 `TranslateKeycode`，命中则直接用。`TranslateKeycode` 是个覆盖近 80 个 `case` 的巨型 `switch`，[WeaselTSF/KeyEvent.cpp:61-254](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L61-L254)。挑几个有代表性的看：

- 方向键 [KeyEvent.cpp:121-128](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L121-L128)：`VK_LEFT → ibus::Left`、`VK_UP → ibus::Up` …
- 左右区分 [KeyEvent.cpp:75-86](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L75-L86)：`VK_SHIFT` 按 `scanCode==0x36` 分 `Shift_R/Shift_L`，`VK_CONTROL` 按 `isExtended` 分右/左。
- 回车区分 [KeyEvent.cpp:69-74](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L69-L74)：`VK_RETURN` 扩展键（小键盘回车）→ `KP_Enter`，否则 `Return`。
- 小键盘 [KeyEvent.cpp:149-180](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L149-L180)：`VK_NUMPAD0 → ibus::KP_0`、`VK_DIVIDE → ibus::KP_Divide` 等。
- 修饰键专用码 [KeyEvent.cpp:235-246](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L235-L246)：`VK_LSHIFT → Shift_L`、`VK_RMENU → Alt_R` 等（这是支持 USB 键盘直接发左右修饰键码的情况）。

**ToUnicodeEx 兜底**，[WeaselTSF/KeyEvent.cpp:44-55](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L44-L55)：

```cpp
static BYTE table[256];
memcpy(table, keyState, sizeof(table));
table[VK_CONTROL] = 0;     // 临时清掉 Ctrl、Alt，
table[VK_MENU] = 0;        // 让 ToUnicodeEx 返回「干净」的字符
int ret = ToUnicodeEx(vkey, UINT(kinfo), table, buf, buf_len, 0, NULL);
if (ret == 1) {
  result.keycode = UINT(buf[0]);   // 直接用 Unicode 码点当 keycode
  return true;
}
```

这是翻译器最巧妙的一步：**字母、数字、标点键根本不在 `TranslateKeycode` 的表里**（你翻遍 `switch` 也找不到 `VK_A`、`'0'`）。它们走兜底——`ToUnicodeEx` 根据当前键盘布局把虚拟键解码成实际 Unicode 字符（例如美式布局下 `'A'` 键解码出 `U+0061` 的 `'a'`，按 Shift 时出 `U+0041` 的 `'A'`），然后**直接拿这个 Unicode 码点当 ibus keycode**。这之所以能工作，是因为 ibus keycode 的低位区间（`0x00–0xFF`）正好就是 Latin-1/ASCII 字符的 Unicode 码点（见 [KeyEvent.h:213-215](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/KeyEvent.h#L213-L215) 的 `XK_c = 0x0063` 就等于 `'c'`）。

> 临时清零 Ctrl/Alt 是为了避开「快捷键组合」：否则 `Ctrl+C` 会被解码成不可见控制字符，而不是 `'c'`。清掉之后解码出的是「裸字符」，组合信息已经单独存在 `mask` 里了。

**返回 false 的语义**：[KeyEvent.cpp:57-58](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L57-L58) 解码失败时 `result.keycode = 0; return false;`。调用方 [WeaselTSF/KeyEventSink.cpp:26-29](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L26-L29) 据此把 `*pfEaten = FALSE`，即「不认识就放行给应用」，保证不会卡键（详见 `u3-l2`）。

**Server 端的掩码还原。**

到 Server 侧，[RimeWithWeasel/RimeWithWeasel.cpp:33-35](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L33-L35) 定义：

```cpp
int expand_ibus_modifier(int m) {
  return (m & 0xff) | ((m & 0xff00) << 16);
}
```

并在调 librime 时使用，[RimeWithWeasel/RimeWithWeasel.cpp:272-273](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L272-L273)：

```cpp
Bool handled = rime_api->process_key(session_id, keyEvent.keycode,
                                     expand_ibus_modifier(keyEvent.mask));
```

这就完成了「前端压缩成 16 位 → 管道传输 → Server 还原成标准 X11 位」的完整闭环。`keycode` 不用动（低位区间在两套体系里一致），只有 `mask` 需要这一步还原。

#### 4.3.4 代码实践

**实践目标**：整理一份「Windows 虚拟键 → ibus keycode」常用键映射表，并标出每条在 `ConvertKeyEvent` 里走的是「查表」还是「ToUnicodeEx 兜底」分支。这正是本讲规格里指定的实践任务。

**操作步骤**（源码阅读型，无需运行）：

1. 打开 [WeaselTSF/KeyEvent.cpp:61-254](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L61-L254) 的 `TranslateKeycode`。
2. 对照 [include/KeyEvent.h:38-217](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/KeyEvent.h#L38-L217) 的 `ibus::Keycode` 取值，填写下表（参考答案见「预期结果」）。
3. 对字母、数字这类**不在表里**的键，确认它们走 [KeyEvent.cpp:44-55](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L44-L55) 的 ToUnicodeEx 兜底，keycode 就是其 Unicode 码点。
4. 对修饰键，单独记录它们的 `mask` 位（来自 [KeyEvent.cpp:15-28](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L15-L28)）。

**需要观察的现象**：方向键、功能键、小键盘、Caps Lock 走查表；字母数字走兜底；修饰键既影响 `mask` 又（在按下事件里）可能查表得到 `Shift_L/Control_L` 等。

**预期结果**（参考映射表）：

| 类别 | Windows 虚拟键 | 值 | ibus keycode | 值 | ConvertKeyEvent 分支 |
|------|----------------|----|--------------|----|----------------------|
| 字母 | `'A'`…`'Z'` | 0x41–0x5A | `XK_a`…（Unicode） | 0x61/0x41 等 | ToUnicodeEx 兜底 |
| 数字 | `'0'`…`'9'` | 0x30–0x39 | Unicode `'0'`… | 0x30–0x39 | ToUnicodeEx 兜底 |
| 回车 | `VK_RETURN` | 0x0D | `Return` / `KP_Enter`（扩展键） | 0xFF0D / 0xFF8D | 查表（KeyEvent.cpp:69-74） |
| 退格 | `VK_BACK` | 0x08 | `BackSpace` | 0xFF08 | 查表（KeyEvent.cpp:63-64） |
| 方向 | `VK_LEFT/UP/RIGHT/DOWN` | 0x25–0x28 | `Left/Up/Right/Down` | 0xFF51–0xFF54 | 查表（KeyEvent.cpp:121-128） |
| 空格 | `VK_SPACE` | 0x20 | `space` | 0x020 | 查表（KeyEvent.cpp:111-112） |
| Esc | `VK_ESCAPE` | 0x1B | `Escape` | 0xFF1B | 查表（KeyEvent.cpp:101-102） |
| 功能 | `VK_F1`…`VK_F12` | 0x70–0x7B | `F1`…`F12` | 0xFFBE–0xFFC9 | 查表（KeyEvent.cpp:181-204） |
| 小键盘 | `VK_NUMPAD0`…`VK_NUMPAD9` | 0x60–0x69 | `KP_0`…`KP_9` | 0xFFB0–0xFFB9 | 查表（KeyEvent.cpp:149-168） |
| Caps Lock | `VK_CAPITAL` | 0x14 | `Caps_Lock` | 0xFFE5 | 查表（KeyEvent.cpp:91-92）+ LOCK_MASK 反转补丁（KeyEvent.cpp:30-35） |
| Shift | `VK_SHIFT`/`VK_LSHIFT`/`VK_RSHIFT` | 0x10/0xA0/0xA1 | `Shift_L`/`Shift_R` | 0xFFE1/0xFFE2 | 查表（KeyEvent.cpp:75-80, 235-238）；同时置 SHIFT_MASK 位 |
| Ctrl | `VK_CONTROL`/`VK_LCONTROL`/`VK_RCONTROL` | 0x11/0xA2/0xA3 | `Control_L`/`Control_R` | 0xFFE3/0xFFE4 | 查表（KeyEvent.cpp:81-86, 239-242）；同时置 CONTROL_MASK 位 |
| Alt | `VK_MENU`/`VK_LMENU`/`VK_RMENU` | 0x12/0xA4/0xA5 | `Alt_L`/`Alt_R` | 0xFFE9/0xFFEA | 查表（KeyEvent.cpp:87-88, 243-246）；同时置 ALT_MASK 位 |

> 表中「值」一列的 ibus 数值请以 `KeyEvent.h` 枚举为准；本表用于建立直觉，精确值以源码为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么字母键 `'A'` 在 `TranslateKeycode` 的 `switch` 里找不到对应的 `case`？

**参考答案**：因为字母键的 keycode 就是它的 Unicode 码点（`'a' = 0x61`），而 ibus keycode 的低位区间恰好与 ASCII/Latin-1 一致，所以不必查表——交给 `ToUnicodeEx` 解码出字符、直接当 keycode 即可。显式给 26 个字母各写一个 `case` 既冗余又无法处理不同键盘布局，不如统一走 Unicode 兜底。

**练习 2**：`expand_ibus_modifier` 的公式 `(m & 0xff) | ((m & 0xff00) << 16)` 里，为什么低 8 位不动、bit 8–15 却要左移 16 位？

**参考答案**：压缩掩码里，低 8 位（Shift/Lock/Ctrl/Alt/MOD2–5）在标准 X11 修饰位里**位置相同**（都在 bit 0–7），所以原样保留；而 HANDLED/IGNORED/RELEASE 等在压缩形式里被「挤」到了 bit 8–15 以省空间，但标准 X11 里它们本应位于 bit 24/25/30，所以要把这一段整体左移 16 位还原。`KeyEvent.h` 里 `// 24 / 25 / 30` 的注释就是还原后的真实位号。

**练习 3**：如果用户按了 `Ctrl+C`，`ConvertKeyEvent` 产出的 `keycode` 和 `mask` 分别是什么？librime 收到时 `mask` 又会被还原成什么？

**参考答案**：`mask` 含 `CONTROL_MASK`（bit 2）；`keycode` 因为兜底逻辑先清掉了 Ctrl 状态，`ToUnicodeEx` 解码出裸字符 `'c'`（`0x63`）。librime 收到时 `expand_ibus_modifier` 把 `CONTROL_MASK` 原样保留在低位（因为 `0x04 & 0xff = 0x04` 不变），于是 librime 看到「`keycode=0x63` + Ctrl 修饰」的组合，即可按「Ctrl+C」处理。

---

## 5. 综合实践

把本讲三个模块串起来，追踪「一次 `Shift+左方向键` 按键」从 Windows 到 librime 的完整数据流，并标注每一步用到的工具：

1. **抓键**：TSF 前端在 `OnKeyDown` 得到 `wParam = VK_LEFT (0x25)`、`lParam`、`keyState`（其中 `keyState[VK_SHIFT]` 的 `0x80` 位置 1）。调用点 [WeaselTSF/KeyEventSink.cpp:24-26](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEventSink.cpp#L24-L26)。
2. **翻译**（本讲 4.3）：`ConvertKeyEvent` 算出 `mask = SHIFT_MASK`，`TranslateKeycode(VK_LEFT)` 命中 [KeyEvent.cpp:121-122](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselTSF/KeyEvent.cpp#L121-L122) 得 `keycode = ibus::Left (0xFF51)`。产出 `weasel::KeyEvent`，压成 32 位整数。
3. **传输**（依赖 `u2` 系列）：该整数作为 `WEASEL_IPC_PROCESS_KEY_EVENT` 命令经命名管道 `\\.\pipe\<用户名>\WeaselNamedPipe` 发往 Server——其中用户名由 `getUsername()`（本讲 4.1）算出。
4. **登记身份**（本讲 4.2）：Server 启动时已用 `WEASEL_VERSION` / `WEASEL_CODE_NAME` 通过 [RimeWithWeasel.cpp:98-99](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L98-L99) 登记了发行版身份，librime 据此加载对应方案配置（配置文件位于 `WeaselUserDataPath()`，本讲 4.1）。
5. **还原并处理**（本讲 4.3）：Server 用 `expand_ibus_modifier`（[RimeWithWeasel.cpp:272-273](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L272-L273)）把 `mask` 还原成标准 X11 位，调 `rime_api->process_key`，引擎据此决定是否翻候选页。

**交付物**：一张时序图，标注每一步对应的源码文件与行号，并指出「哪些信息由本讲的工具函数提供」（用户名、路径、版本身份、按键翻译、掩码还原）。

## 6. 本讲小结

- Weasel 的三类数据路径分工明确：`WeaselSharedDataPath` 跟 EXE 走（自带数据）、`WeaselUserDataPath` 注册表优先并退回 `%AppData%\Rime`（用户数据）、`WeaselLogPath` 落在 `%TEMP%\rime.weasel`（日志）。
- `WeaselUtility.h` 还集中了跨工程复用的杂项工具：`getUsername`（管道名/互斥体）、编码转换宏（`u8tow`/`wtou8` 等）、字符串转义（服务 IPC 行协议）、`DebugStream` 与 `HR` 错误处理。
- 版本号是**构建期注入**：`build.bat`/`xbuild.bat` 设变量 → `weasel.props` 渲染 → 资源/C++ 预处理定义 `VERSION_MAJOR/MINOR/PATCH` → `WeaselConstants.h` 用两层宏拼成 `WEASEL_VERSION` 字符串 → 登记给 librime 的 `distribution_version`。改版本只动构建脚本，不动源码。
- `ConvertKeyEvent` 是 Windows→ibus 的翻译器：先用 `keyState` 算修饰掩码（含 Caps Lock 时序补丁），再用 `TranslateKeycode` 查表得特殊键码，查不到则用 `ToUnicodeEx` 把字母/数字/标点解码成 Unicode 码点直接当 keycode。
- `weasel::KeyEvent` 是「keycode:16 + mask:16」的 32 位压缩结构；mask 在传输时是压缩 16 位，Server 端用 `expand_ibus_modifier` 把 bit 8–15 左移 16 位还原成标准 X11 修饰位。
- 翻译失败（`return false`）会让前端把键放行给应用，保证不卡键；这是输入法「宁可放行不可吞键」的安全设计。

## 7. 下一步学习建议

- 想看「翻译完的按键如何被引擎消费、如何影响候选页」，继续 `u4-l2 会话管理与按键处理`，重点读 `RimeWithWeasel.cpp` 的 `ProcessKeyEvent` 与 `_Respond`。
- 想看「按键翻译之前的去重与 Caps Lock 抬键补丁」，回看 `u3-l2 按键事件捕获：KeyEventSink`，对照本讲的 `ConvertKeyEvent` 形成完整前端图景。
- 想做配色/样式二次开发，进入 `u7-l3 配色方案与样式定制实战`，那里会用到本讲的 `WeaselUserDataPath`（定位 `weasel.custom.yaml`）与编码转换工具。
- 若对「全仓库还有哪些扩展点」感兴趣，`u7-l4 扩展点与架构权衡总结` 会把本讲提到的工具函数与按键映射纳入整体架构评价。
