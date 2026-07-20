# Windows 批处理安装器

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `rime-install.bat` 这个**纯批处理（.bat）安装器**的整体结构：它如何初始化、如何把一个命令行参数「路由」到不同的安装分支。
- 画出传入一个 GitHub `.zip` 链接时的完整调用流程（`download_package` → `install_zip_package`），并解释它安装了哪些文件、又**故意不安装**哪些文件。
- 理解 7z 与 git 这两个外部依赖是如何被「按需探测 + 自动下载安装」的，以及 `/needed` 这个内部开关的作用。
- 说清 `use_plum` 机制：批处理在什么条件下会放弃自己的 ZIP 安装逻辑、转而把工作委托给 plum 的 bash 脚本（u1-l3 学过的那套）。
- 认识两个辅助脚本 `rime-install-bootstrap.bat`（首次安装/建快捷方式）与 `rime-install-config.bat`（配置模板）的职责。

本讲面向 **advanced** 读者，前置是 u1-l3（你已经知道 bash 版 `rime-install` 怎么用、`rime_dir` 是什么）。本讲不再重复 Rime 的 schema/dictionary/package 概念，而是专注「Windows 上没有 bash 时，plum 如何用一份纯 .bat 实现一套能用的安装器」。

## 2. 前置知识

在进入源码前，先用通俗语言交代几个 Windows 批处理与本项目特有的概念。

### 2.1 为什么要单独写一份 .bat

plum 的「正主」是 bash 脚本（`rime-install` + `scripts/*.sh`），它依赖 `git`、`bash`、`source`、数组等 Unix 概念。但 Windows 用户开箱并**没有** bash，只有 `cmd.exe` 和批处理文件。为了让一个刚装好小狼毫（Weasel，Windows 版 Rime 前端）的用户也能一行命令装方案，plum 额外提供了一份用纯批处理写成的 `rime-install.bat`。

它的能力是 bash 版的**子集**：能下载 GitHub 仓库的 ZIP 压缩包、用 7z 解压、把里面的 `.yaml`/`.txt`/`opencc/*` 拷到 Rime 用户目录。它**不能**执行配方（recipe，见 u2-l6）、不能做 `patch_files` 那种文件改写。所以一旦系统里检测到 git-bash，它会自动「升级」为调用 bash 版——这就是 `use_plum` 的由来。

### 2.2 批处理的几个关键特性（不熟悉 .bat 的读者必读）

本讲的源码大量用到下面这些 .bat 技巧，先建立直觉再看代码会顺畅很多：

- **标签与 `call :label`**：批处理用 `:label` 定义一个「子程序」，`call :install_package` 类似其它语言里的函数调用，执行到 `exit /b` 或 `goto :eof` 返回。`exit /b` 后可跟返回码（`exit /b 1` 表示失败）。
- **`errorlevel` 与 `%errorlevel%`**：`errorlevel` 是上一条命令/上一次 `exit /b` 的返回码。`if errorlevel 1` 意为「返回码 ≥ 1」（注意是**大于等于**）。`exit /b %errorlevel%` 用于透传返回码。
- **`setlocal enabledelayedexpansion` 与 `!var!`**：批处理默认在解析整条语句时一次性展开 `%var%`，这在 `for` / `if` 块内会导致「读到的是进入块之前的旧值」。开启延迟展开后，用 `!var!` 就能拿到「当前最新值」。本讲源码里 `%var%` 与 `!var!` 混用，区别就在这里。
- **变量子串替换 `%var:find=replace%`**：把 `var` 里所有的 `find` 替换成 `replace`。这是 .bat 里**唯一**能做的字符串处理，本讲会用它来模拟「判断是否以 `.zip` 结尾」「是否以 `https://github.com/` 开头」等操作。
- **`where /q xxx`**：安静模式查找可执行文件是否在 `PATH` 里，找到返回 0、找不到返回 1，配合 `%errorlevel%` 用来做依赖探测。

### 2.3 三个脚本如何分工

| 文件 | 角色 | 何时被用到 |
| --- | --- | --- |
| `rime-install-bootstrap.bat` | 首次安装引导：补齐 `rime-install.bat`、建桌面快捷方式 | 用户第一次下载 bootstrap 压缩包后双击运行 |
| `rime-install-config.bat` | 配置模板：设定 `rime_dir`、`plum_dir`、`use_plum` 等 | 被 `rime-install.bat` 在启动时 `call` 进来 |
| `rime-install.bat` | 主安装器：参数路由 + ZIP 安装 + 依赖管理 + 转调 bash | 每次实际安装方案时运行 |

## 3. 本讲源码地图

- [rime-install.bat](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat) — 主安装器，433 行，是本讲的绝对主角。它的逻辑全部用「标签 + `call`/`goto`」组织成若干「子程序」。
- [rime-install-config.bat](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install-config.bat) — 配置模板，19 行。真正被运行的逻辑只有「从注册表读小狼毫的 `RimeUserDir`」这一段。
- [rime-install-bootstrap.bat](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install-bootstrap.bat) — 引导脚本，56 行。负责补齐主脚本并创建快捷方式。
- 参考：bash 版 [rime-install](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install)（u1-l3 已学），`use_plum` 转调的最终目标就是它。

## 4. 核心概念与源码讲解

### 4.1 启动与配置：bootstrap、config 与主入口初始化

#### 4.1.1 概念说明

在主安装器开始干活之前，有两件「准备」要先做：

1. **脚本本身从哪来**：一个全新 Windows 用户手里可能只有 bootstrap 压缩包，里面**不一定**有最新的 `rime-install.bat`。`rime-install-bootstrap.bat` 负责把它补齐，并顺手建一个快捷方式方便以后双击。
2. **配置从哪来**：`rime_dir`（装到哪）、`use_plum`（用不用 bash 版）这些开关，不希望写死在主脚本里，而是放到一个同目录的 `rime-install-config.bat` 让用户改。主脚本启动时把它 `call` 进来即可。

主入口 `rime-install.bat` 的初始化则负责：加载配置 → 设默认值 → 探测 7z/git-bash/下载器三项依赖 → **决定 `use_plum`** → 进入主循环。

#### 4.1.2 核心流程

主入口初始化的流程可以用下面这段伪代码概括：

```
启动 rime-install.bat
 ├─ call rime-install-config.bat      # 用户配置（可能已 set rime_dir / use_plum）
 ├─ 给 rime_dir / download_cache_dir 设默认值
 ├─ 探测 arch（32/64）
 ├─ find_7z        → has_7z
 ├─ find_git_bash  → has_git_bash
 ├─ find_downloader → downloader(curl 或 powershell)
 ├─ 若 use_plum 未定义 且 has_git_bash==1 → 自动 set use_plum=1
 └─ 进入 :process_arguments 主循环
```

最关键的一行决策是「**有 git-bash 就默认走 bash 版**」，它决定了后面绝大多数 target 会不会被转调。

#### 4.1.3 源码精读

**配置加载与默认值**——主脚本一开始就把同目录的 config `call` 进来，再补默认值：

[rime-install.bat:10-15](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L10-L15)

```bat
set config_file=%~dp0\rime-install-config.bat
if exist "%config_file%" call "%config_file%"

if not defined rime_dir set rime_dir=%APPDATA%\Rime
if not defined download_cache_dir set download_cache_dir=%TEMP%
```

`%~dp0` 是「本脚本所在目录」（带尾部反斜杠）。注意 `if not defined` 的写法——只有当 config **没有**设置过 `rime_dir` 时才填默认值，这就保证了用户在 config 里的设置优先。

**config.bat 真正运行的逻辑**——只有从注册表读小狼毫用户目录这一段：

[rime-install-config.bat:14-16](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install-config.bat#L14-L16)

```bat
set key=HKEY_CURRENT_USER\SOFTWARE\Rime\Weasel
set name=RimeUserDir
for /f "tokens=2*" %%a in ('reg query "%key%" /v "%name%"') do set rime_dir=%%b
```

`reg query` 查注册表，`for /f` 解析输出，`tokens=2*` 取第二列及其后全部（即路径，`%%b` 对应第二组 `*`）。这样只要用户装了小狼毫且设过用户目录，`rime_dir` 就自动指向它，无需手填。文件里其余都是注释掉的示例（`rem set plum_dir=...`、`rem set use_plum=0`），是给用户照着改的模板。

**`use_plum` 的自动决策**——探测完三项依赖后：

[rime-install.bat:26-32](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L26-L32)

```bat
if defined ProgramFiles(x86) (set arch=64) else (set arch=32)

call :find_7z
call :find_git_bash
call :find_downloader

if not defined use_plum if "%has_git_bash%" == "1" set use_plum=1
```

`if not defined use_plum` 是关键守卫：如果用户在 config 里**显式** `set use_plum=0`（强制只用批处理），这里就不会覆盖；只有「用户没表态」且「检测到 git-bash」时，才自动启用 bash 版。这是一个很典型的「自动升级、但允许显式退回」的设计。

**bootstrap.bat 的补齐逻辑**——它先判主脚本在不在，不在就下载，最后建快捷方式：

[rime-install-bootstrap.bat:12-49](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install-bootstrap.bat#L12-L49)

核心两步：① 用 curl（优先）或 powershell 下载 `rime-install.bat` 与 config 模板；② 若已存在则跳过（`if exist ... goto end_download`），保证可重复运行不覆盖用户改过的配置。下载地址指向 `raw.githubusercontent.com/rime/plum/master/...`。

[rime-install-bootstrap.bat:53-56](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install-bootstrap.bat#L53-L56) 用一段 powershell COM 调用 `WScript.Shell` 创建一个 `.lnk` 快捷方式，目标为 `%ComSpec%`（即 `cmd.exe`）带 `/k rime-install.bat`，于是用户双击快捷方式就能打开一个停留在安装器界面的 cmd 窗口。

#### 4.1.4 代码实践

**实践目标**：理解 config 的「优先级」与 bootstrap 的「幂等性」。

**操作步骤（源码阅读型，无需运行）**：

1. 打开 [rime-install-config.bat](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install-config.bat)，找到第 16 行。
2. 假设用户在 config 里手写了 `set rime_dir=D:\MyRime`，同时注册表里 `RimeUserDir=E:\WeaselRime`。回答：最终 `rime_dir` 是哪个？为什么？
3. 看 [rime-install-bootstrap.bat:42](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install-bootstrap.bat#L42) 的 `if exist "%config_path%" goto end_download`，回答：第二次运行 bootstrap 时，用户改过的 config 会被覆盖吗？

**需要观察的现象 / 预期结果**：

1. 第 2 问：`rime_dir` 为 `D:\MyRime`。因为 config 里 `set rime_dir=D:\MyRime` 先执行；随后注册表那段 `for /f ... do set rime_dir=%%b` **无条件**覆盖——等等，注意这里其实会覆盖。**待本地验证（Windows 环境）**：实际行为取决于注册表键是否存在。若键存在，注册表值会覆盖用户的手填值；若键不存在（`reg query` 失败），`for /f` 不执行，保留 `D:\MyRime`。这正是一个值得在真机上验证的边界。
2. 第 3 问：不会被覆盖。`goto end_download` 跳过了下载，保护了用户配置。

> 说明：本环境为 Linux，无法运行 `.bat`，以上第 1 点标注「待本地验证」；结论以你在 Windows 上的实际运行为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把 `rime_dir` 直接写死在 `rime-install.bat` 里，而要放到单独的 config 文件？
**答案**：因为 `rime-install.bat` 会被 bootstrap 从 GitHub 重新下载覆盖（见 4.1.3），写死在里面会被冲掉；而 config 文件存在时 bootstrap **不覆盖**它，所以用户配置放在 config 里才稳定。

**练习 2**：`if not defined use_plum if "%has_git_bash%" == "1" set use_plum=1` 这行若删掉 `if not defined use_plum`，会发生什么？
**答案**：即使用户在 config 里 `set use_plum=0` 强制只用批处理，也会被这行强制改回 `1`，用户失去「退回批处理」的能力。

---

### 4.2 参数路由与 ZIP 离线安装

#### 4.2.1 概念说明

这是本讲最核心的模块。`rime-install.bat` 把命令行上每个空格分隔的参数（或者交互模式下手动输入的一行）当作一个 **package**，交给子程序 `:install_package` 去「路由」——根据 package 的形态，决定调用哪个子程序去真正安装它。

路由的对象大致有这么几种形态（与 u1-l3 学过的 target 概念对应）：

| package 形态 | 例子 | 路由去向 |
| --- | --- | --- |
| 特殊关键字 | `7z` / `git` / `plum` | 安装/更新对应工具或 plum 自身 |
| `.zip` 结尾的 GitHub archive URL | `https://github.com/rime/rime-prelude/archive/master.zip` | `download_package` → `install_zip_package` |
| 本地 `.zip` 文件路径 | `C:\downloads\foo.zip` | `install_zip_package` |
| 预设集合 / `.bat` 清单 | `:preset` / `preset-packages.bat` | `call` 清单 → `install_package_group` |
| `user/repo` 或裸包名 | `rime/rime-prelude` / `luna-pinyin` | `download_package`（拼出 archive URL） |

注意一个**重要例外**：当 `use_plum=1`（默认情况，见 4.1）时，除 `.zip` 和特殊关键字以外的 package，会**优先**被转调给 bash 版（4.4 讲）。也就是说，**`.zip` 安装永远走批处理自带的 ZIP 逻辑**，无论 `use_plum` 是否开启。这是因为 bash 版并不处理「用户拖进来的本地 zip」这种离线场景。

#### 4.2.2 核心流程

`:install_package` 的决策树（`use_plum=1` 时的实际路径用虚线标出）：

```
:install_package(package)
 ├─ package == "7z"      → :install_7z
 ├─ package == "git"     → :install_git
 ├─ package == "plum"    → :install_with_plum plum
 ├─ package 以 .zip 结尾？
 │    ├─ 是 GitHub URL   → 解析出 repo+branch → :download_package
 │    └─ 是本地文件      → :install_zip_package
 ├─ :prefer_plum_installer
 │    └─ use_plum==1     - - > :install_with_plum  （转调 bash，见 4.4）
 └─ :fallback_to_builtin_installer  （use_plum==0 时才走到）
      ├─ GitHub /tree/ URL → 解析 repo+branch → :download_package
      ├─ xxx-packages.bat  → call 它 → :install_package_group
      ├─ :preset           → call preset-packages.bat → :install_package_group
      ├─ 含 @ 的 user/repo@branch → :download_package
      └─ 裸包名            → 补 rime/rime- 前缀 → :download_package
```

`:download_package` 与 `:install_zip_package` 的流水线：

```
:download_package
 ├─ 确保 7z 就位（:install_7z /needed）
 ├─ branch 为空？→ 查 GitHub API 取 default_branch
 ├─ 拼 archive URL：https://github.com/<repo>/archive/<branch>.zip
 ├─ 缓存文件名：<repo 去掉 owner>/<branch>.zip（如 rime-prelude-master.zip）
 ├─ no_update==1 且缓存存在 → 跳过下载
 ├─ 下载到 download_cache_dir
 └─ :install_zip_package
      ├─ 确保 7z 就位
      ├─ 7z x 解压到 %TEMP%
      ├─ 创建 rime_dir（若不存在）
      └─ 仅拷贝 *.yaml / *.txt / opencc\*.json|*.ocd|*.txt 到 rime_dir
```

最后一步「仅拷贝固定几类文件」是批处理 ZIP 安装器的根本局限——它**看不懂** recipe，所以只挑最通用的方案/词典/文本文件。

#### 4.2.3 源码精读

**主循环 `:next`**——逐个处理参数，交互模式下还会提示输入：

[rime-install.bat:44-56](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L44-L56)

```bat
:next
if "%batch_interactive%" == "1" (
  set package=
  echo. && (set /p package=Enter package name, URL, user/repo or downloaded ZIP to install: )
) else (
  set package=%1
  shift
)
if "%package%" == "" goto finish

call :install_package
if errorlevel 1 exit /b %errorlevel%
goto next
```

`%1` 取第一个参数，`shift` 后 `%1` 变成原来的第二个，于是 `goto next` 循环即可遍历所有参数。参数为空（`batch_interactive` 标记来自 [L35](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L35) 的 `if "%1" == "" set batch_interactive=1`）时进入交互式 `set /p` 提示。`if errorlevel 1 exit /b %errorlevel%` 保证任一包失败立刻终止。

**路由器 `:install_package`**——先处理特殊关键字与 `.zip`：

[rime-install.bat:58-80](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L58-L80)

```bat
:install_package
if "%package%" == "7z" (
  call :install_7z
  exit /b %errorlevel%
) else if "%package%" == "git" (
  call :install_git
  exit /b %errorlevel%
) else if "%package%" == "plum" (
  call :install_with_plum plum
  exit /b %errorlevel%
) else if "%package:.zip=%.zip" == "%package%" (
  if "https://github.com/%package:https://github.com/=%" == "%package%" (
     ...call :download_package
  ) else (
    set package_file=%package%
    call :install_zip_package
  )
  goto :after_install_package
)
```

这里出现了 2.2 节预告的「替换+比较」技巧，值得拆开看：

- `"%package:.zip=%.zip"`：先把 `package` 里所有 `.zip` 删掉，再补一个 `.zip` 回来。若 `package` 本身就以 `.zip` 结尾（且只有一处），删了再补等于原值，比较成立。这就是 .bat 版的「`endsWith(.zip)`」。
- `"https://github.com/%package:https://github.com/=%"`：先把前缀 `https://github.com/` 删掉，再补回去。若 `package` 以该前缀开头，比较成立——即 .bat 版的「`startsWith(...)`」。

两条组合，就把「GitHub archive URL」与「本地 zip 文件」区分开了。注意末尾 `goto :after_install_package`，**跳过**了下面的 `:prefer_plum_installer`——这正是「`.zip` 永远不转调 bash」的代码体现。

**`use_plum` 优先分支与回退分支**：

[rime-install.bat:82-119](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L82-L119)

```bat
:prefer_plum_installer
if "%use_plum%" == "1" (
  call :install_with_plum %package%
  goto after_install_package
)
:fallback_to_builtin_installer
...（GitHub /tree/ URL、.bat 清单、:preset、user/repo@branch、裸包名）
```

回退分支里有两处同样用替换技巧的判断：`"%package:-packages.bat=%-packages.bat"` 判断是否以 `-packages.bat` 结尾、`":%package::=%"` 判断是否以 `:` 开头（预设集合）。后者尤其巧妙——`%package::=%` 把 `package` 里的冒号删掉，前面补一个冒号；若原值是 `:preset`，删掉冒号剩 `preset`、补回冒号得 `:preset`，相等即成立。命中后 `call "%package::=%-packages.bat"`（即 `call preset-packages.bat`）把清单里的 `package_list` 加载进来，再 `:install_package_group` 批量安装。

**下载 `:download_package`**——拼 URL、查默认分支、下载：

[rime-install.bat:124-149](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L124-L149)

```bat
:download_package
if not defined downloader ( ... goto error )
call :install_7z /needed
if not defined branch (
  for /f "tokens=2 usebackq delims=:, " %%g in (`
    %downloader% https://api.github.com/repos/%package_repo% ^| findstr default_branch
  `) do set branch=%%~g
)
set package_url=https://github.com/%package_repo%/archive/%branch%.zip
...
set package_file=%download_cache_dir%\%package_repo:*/=%-%branch%.zip
if "%no_update%" == "1" if exist "%package_file%" goto skip_download_package
%downloader% "%package_url%" %save_to% "%package_file%"
...
:skip_download_package
call :install_zip_package
```

三个细节：

1. `call :install_7z /needed`：解压前先把 7z 准备好（见 4.3）。
2. `branch` 为空时，调 GitHub API 的 `default_branch` 字段拿默认分支——这处理了「用户只给了 `rime/rime-prelude`，没给分支」的情况。
3. `%package_repo:*/=%`：删掉「第一个 `/` 及其之前」的内容，即去掉 owner，`rime/rime-prelude` → `rime-prelude`，于是缓存文件叫 `rime-prelude-master.zip`，不同包不会撞名。

**解压与拷贝 `:install_zip_package`**——核心是那段固定的文件清单：

[rime-install.bat:151-197](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L151-L197)

```bat
for %%f in (
    *.yaml
    *.txt
    opencc\*.json
    opencc\*.ocd
    opencc\*.txt
) do (
  ...
  set target_file=%rime_dir%\%%f
  for %%t in (!target_file!) do set target_dir=%%~dpt
  if not exist "!target_dir!" mkdir "!target_dir!"
  copy /y "%%f" "!target_file!"
  ...
)
```

`for %%f in (...)` 是固定 glob 列表；`%%~dpt` 取目标文件的「盘符+路径」，用来在拷贝前 `mkdir` 出子目录（比如 `opencc\` 子目录）。这就是批处理安装器的「安装」动作——**逐个 `copy`**，没有 recipe、没有 patch、没有增量 diff（对比 bash 版 `install_files` 用 `diff -q` 做增量，见 u2-l3）。它只认 `.yaml`/`.txt`/`opencc/*` 这几类，包里其它文件（如 `README`、`*.custom.yaml` 模板、脚本）一律忽略。

**批量安装 `:install_package_group`**——把清单里的每个包再扔回路由器：

[rime-install.bat:199-209](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L199-L209)

```bat
:install_package_group
if not defined package_list ( ... goto error )
for %%p in (%package_list%) do (
  set package=%%p
  call :install_package
  if errorlevel 1 exit /b !errorlevel!
)
```

`%package_list%` 是清单 `.bat`（如 [preset-packages.bat](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/preset-packages.bat)）里 `set package_list=...` 设出的**空格分隔字符串**（.bat 没有数组，用字符串模拟）。`for %%p` 逐个取出，重设 `package` 后 `call :install_package` 递归——于是 `:preset` 展开后的每个包又会走一遍完整路由（通常又被 `use_plum` 转调到 bash）。

#### 4.2.4 代码实践

**实践目标**：把传入一个 GitHub zip 链接时的调用链亲手走一遍，验证「`.zip` 不转调 bash」。

**操作步骤（源码追踪型 + 可选本地验证）**：

1. 设想命令 `rime-install.bat https://github.com/rime/rime-prelude/archive/master.zip`（且系统装了 git-bash，即 `use_plum=1`）。
2. 在 [rime-install.bat:58](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L58) 起的 `:install_package` 里，逐行判断：
   - L59-67：`package` 不是 `7z`/`git`/`plum`，跳过。
   - L68：`"%package:.zip=%.zip" == "%package%"` 是否成立？手算：删 `.zip` 得 `https://github.com/rime/rime-prelude/archive/master`，补 `.zip` 得回原值 → 成立，进入 `.zip` 分支。
   - L69：`"https://github.com/" + 删前缀` 是否等于原值？成立 → 走 `:download_package`。
   - L70-73：`user_repo_path=rime/rime-prelude/archive/master.zip`、`archive_name=master.zip`、`branch=master`、`package_repo=rime/rime-prelude`。
   - L74 → `:download_package` → L148 `:install_zip_package`。
3. **回答关键问题**：这条路径有没有经过 L82-86 的 `:prefer_plum_installer`？为什么 `use_plum=1` 没有让它转调 bash？

**预期结果**：

- 调用链：`:install_package` → `:download_package` → `:install_zip_package`。
- **没有**经过 `:prefer_plum_installer`，因为 L79 的 `goto :after_install_package` 直接跳过了 L82。所以 `.zip` 始终用批处理自带的下载+解压+拷贝，即便 `use_plum=1`。
- **可选本地验证（Windows 环境）**：在装了 git-bash 的 Windows 上运行上述命令，观察输出里有没有 `Downloading rime-install ...` / bash 相关字样（应没有），并确认 `%TEMP%\rime-prelude-master.zip` 被生成、`%APPDATA%\Rime` 下多了 `*.yaml`。

#### 4.2.5 小练习与答案

**练习 1**：用户运行 `rime-install.bat luna-pinyin`，系统**未装** git-bash。请追踪它会走到哪个分支、最终下载哪个 URL。
**答案**：`use_plum` 未被自动置 1（`has_git_bash` 不为 1）。`:install_package` 里前几个分支都不命中，落到 `:fallback_to_builtin_installer` 最后一个 else（[L112-119](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L112-L119)）：`user_repo_path=luna-pinyin`，`package_repo=rime/rime-luna-pinyin`（补了 `rime/rime-` 前缀），`branch` 为空 → `:download_package` 会查 API 拿默认分支，下载 `https://github.com/rime/rime-luna-pinyin/archive/<default>.zip`。

**练习 2**：为什么 `:install_zip_package` 只拷 `*.yaml`/`*.txt`/`opencc/*`，而不直接把整个解压目录复制过去？
**答案**：ZIP 包里常有 `README.md`、构建脚本、`.git` 残留等不该进 Rime 用户目录的文件；固定白名单只挑 Rime 真正需要的方案/词典/文本，避免污染用户目录。代价是不够通用（比如非标准目录布局的包会漏装），这正是有了 git-bash 就优先转调 bash 版的原因之一。

**练习 3**：`for /f "tokens=2 usebackq delims=:, " %%g in ('\%downloader% ... ^| findstr default_branch')` 为什么 `delims` 里要同时列 `:`、`,`、空格？
**答案**：GitHub API 返回形如 `"default_branch": "master",`，用 `:`/`,`/空格 一起做分隔，才能把第二列干净地切出来为 `master`（带引号，再用 `%%~g` 去引号）。

---

### 4.3 7z / git 依赖的自动探测与安装

#### 4.3.1 概念说明

ZIP 安装链路依赖 **7z** 来解压；转调 bash 又依赖 **git（含 git-bash）**。但 Windows 上这两个工具都不是标配。plum 的策略是「**用到时再装**」：每次需要 7z/git 时，先探测是否已在 `PATH`，没有就**自动下载官方安装包并以静默参数安装**。

这里有一个精巧的设计：同一个安装子程序（`:install_7z` / `:install_git`）服务两种调用场景：

- **作为依赖被内部调用**：`call :install_7z /needed`——带 `/needed` 开关，意为「我只确认它就位，没的话你装，有的话别废话」。
- **作为用户显式目标**：用户敲 `rime-install 7z` 或 `rime-install git`——不带 `/needed`，会多打印一句 `Found 7z` 之类的提示，让用户看到当前状态。

#### 4.3.2 核心流程

```
:install_7z / :install_git（带可选 /needed）
 ├─ where /q 7z（或 git）
 │    └─ 找到：
 │         ├─带了 /needed → 静默 exit /b（仅作依赖检查）
 │         └─没带 /needed → echo "Found 7z" 后退出
 ├─ 没找到：
 │    ├─ 先 where /q <安装包文件名>，看本地是否已有安装包
 │    │    └─ 有 → 直接 goto run_xxx_installer
 │    ├─ no_update==1 且缓存存在 → 跳过下载，用缓存的安装包
 │    ├─ 没下载器 → 报 "TODO: please download and install ..." 错误
 │    ├─ 下载官方安装包到 download_cache_dir
 │    └─ run_xxx_installer：以静默参数运行安装包
 │         ├─ 7z：  "<installer>" /S
 │         └─ git： "<installer>" /SILENT
```

注意三个「先就近、再联网」的层次：① 已装好；② 本地已有安装包；③ 缓存里有；都没有才真去下载。这让离线/重复环境也能工作。

#### 4.3.3 源码精读

**探测函数 `:find_7z` / `:find_git_bash`**——把常见安装路径塞进 `PATH` 再 `where`：

[rime-install.bat:356-399](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L356-L399)

```bat
:find_git_bash
set search_path=^
%ProgramFiles%\Git\cmd;^
%ProgramFiles%\Git\mingw%arch%\bin;^
%ProgramFiles%\Git\usr\bin;
...（ProgramW6432、ProgramFiles(x86) 等 32/64 位补救路径）
set PATH=%search_path%%PATH%

where /q git
if %errorlevel% equ 0 set has_git=1
where /q bash
if %errorlevel% equ 0 set has_bash=1
if "%has_git%" == "1" if "%has_bash%" == "1" set has_git_bash=1
```

关键点：`has_git_bash` 要求 **git 与 bash 同时存在**——因为转调 bash 版既要用 `git clone`，又要 `bash` 解释器，缺一不可。行尾的 `^` 是 .bat 的续行符，把多行拼成一个 `set`。`mingw%arch%\bin` 里的 `%arch%`（64/32）在 32 位 cmd.exe 里找 64 位 Git 时尤其重要（见 [L378-388](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L378-L388) 的注释）。

**`:install_7z` 的 `/needed` 双语义**：

[rime-install.bat:237-261](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L237-L261)

```bat
:install_7z
where /q 7z
if not errorlevel 1 (
   if "%1" == "/needed" exit /b
   echo Found 7z
   exit /b
)
...
```

`if not errorlevel 1` 即「返回码 < 1」，亦即 `where` 成功（7z 在 PATH）。此时若带了 `/needed` 就静默返回（依赖已满足），否则打印 `Found 7z`。这一处 `"%1"` 读取的是**调用者传给子程序的参数**——`call :install_7z /needed` 时 `%1=/needed`，而 `call :install_7z`（来自路由器 L60，用户敲 `7z`）时 `%1` 为空。

**本地安装包优先 + 下载**（接上文 [L253-286](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L253-L286)）：

```bat
set _7z_installer=7z%_7z_version:.=%%_7z_arch%.exe
rem find local 7z installer
where /q %_7z_installer%
if not errorlevel 1 (
   set _7z_installer_path=%_7z_installer%
   goto run_7z_installer
)
set _7z_installer_path=%download_cache_dir%\%_7z_installer%
if "%no_update%" == "1" if exist "%_7z_installer_path%" goto run_7z_installer
...（下载 https://www.7-zip.org/a/<installer>）
```

安装包文件名是动态拼出来的：`7z` + 版本（去掉点）+ 可选的 `-x64` + `.exe`，例如 `7z1801-x64.exe`。`%_7z_version:.=%` 把版本号 `18.01` 的点去掉变 `1801`，`%%_7z_arch%` 在 64 位时展开为 `-x64`、32 位为空。先 `where /q` 找本地是否已有同名安装包，再考虑缓存，最后才下载——三层就近。

**静默安装**（[L288-294](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L288-L294) 与 git 的 [L348-354](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L348-L354)）：

```bat
"%_7z_installer_path%" /S          # 7z 用 /S
"%git_installer_path%" /SILENT     # git 用 /SILENT
```

这两个参数是各自官方安装器（Inno Setup / Git for Windows）支持的静默安装开关，免去了用户点「下一步」。

`:install_git`（[L296-354](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L296-L354)）与 `:install_7z` 结构完全对称，只是下载源换成 `github.com/git-for-windows/git/releases`、版本变量是 `git_version`/`git_release`。

#### 4.3.4 代码实践

**实践目标**：理解 `/needed` 双语义，以及「就近」优先级。

**操作步骤（源码阅读型）**：

1. 在 [rime-install.bat:129](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L129)（`:download_package` 里）和 [L152](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L152)（`:install_zip_package` 里）各有一处 `call :install_7z /needed`。回答：为什么解压前要调两次？每次调用若 7z 已存在，会打印 `Found 7z` 吗？
2. 设想 `no_update=1` 且 `%TEMP%` 里已有 `7z1801-x64.exe`，但系统 `PATH` 里没有 7z。追踪 `:install_7z`（不带 `/needed`，即用户敲 `rime-install 7z`）会走哪条路。
3. 看 [L268-274](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L268-L274)，回答：若机器上既没有 7z、又没有 curl/powershell（`downloader` 未定义），会发生什么？

**预期结果**：

1. 两次都带 `/needed`，故 7z 已存在时**都静默返回**，不会打印 `Found 7z`（`/needed` 抑制了提示）。调两次是为了「下载前」和「解压前」各确认一次，防御性编程。
2. `where /q 7z` 失败 → 不走「Found」分支；`where /q 7z1801-x64.exe` 也失败（它在 TEMP 不在 PATH）→ 走到缓存检查：`no_update==1` 且 `%TEMP%\7z1801-x64.exe` 存在 → `goto run_7z_installer`，**不下载**，直接静默安装缓存的安装包。
3. 走到 `:download_7z_installer`，`if not defined downloader` 成立 → 打印 `TODO: please download and install 7z: <url>` 并 `goto error`，安装失败退出。

> 第 2、3 点结论可由静态阅读确定；若要在真机复现「`no_update` 用缓存」的行为，需 Windows 环境（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `:install_7z` 既被「用户敲 `7z`」调用，又被「内部解压前」调用，却能共用同一份代码？
**答案**：靠 `%1` 是否为 `/needed` 区分两种语义：内部调用带 `/needed` 走静默路径，用户显式调用不带则多打印一句状态。一个开关复用了同一套「探测-就近-下载-静默安装」逻辑。

**练习 2**：`has_git_bash` 为什么要同时要求 `has_git` 和 `has_bash`？
**答案**：转调 bash 版需要 `git clone` 拉取 plum 与各包，也需要 `bash` 执行 `rime-install` 脚本；只有 git 没有 bash（或反之）都无法完成转调，故两者齐备才算「可用」。

**练习 3**：`%_7z_version:.=%` 这个替换在这里起什么作用？
**答案**：把版本号 `18.01` 里的点删掉变成 `1801`，因为 7z 安装包文件名是 `7z1801-x64.exe` 这种「无点」形式，需要从带点的版本号转换出来。

---

### 4.4 use_plum：转调 bash 版 rime-install

#### 4.4.1 概念说明

前面三节其实都在讲「批处理自己怎么装」。但 plum 真正强大的能力（recipe、patch_files、增量更新）全在 bash 版里。批处理安装器对此的态度是：**既然你机器上有 git-bash，那这些复杂活我就不干了，交给 bash 版的 `rime-install` 去做**。这就是 `use_plum`。

`use_plum` 有三个状态来源：

1. 用户在 config 里显式 `set use_plum=1` 或 `set use_plum=0`。
2. 主入口在探测到 git-bash 时自动 `set use_plum=1`（4.1.3）。
3. 完全没设：等价于「没装 git-bash 时的 0」。

当 `use_plum=1` 时，绝大多数 package（除了 `.zip` 与 `7z`/`git`/`plum` 特殊关键字）在 `:install_package` 里会命中 `:prefer_plum_installer`（[L82-86](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L82-L86)），转而调用 `:install_with_plum`，由它找到并执行 bash 版 `rime-install`。

#### 4.4.2 核心流程

```
:install_with_plum <原参数...>
 ├─ call :install_git /needed    # 确保 git 就位（转调必需）
 ├─ set WSLENV=plum_dir:rime_dir # 把这两个变量导出给 bash/WSL 子进程
 ├─ 按优先级找 bash 脚本：
 │    ① plum_dir 已定义 且 其下有 rime-install → bash "<plum_dir>/rime-install" %*
 │    ② 当前目录有 plum/rime-install           → bash plum/rime-install %*
 │    ③ 当前目录有 rime-install                 → bash rime-install %*
 │    ④ 都没有 → 下载 raw 版 rime-install 到缓存 → bash 缓存版 %*
 └─ exit /b %errorlevel%         # 透传 bash 的返回码
```

`%*` 是「传给本 .bat 的**所有**原始参数」，原样转发，于是 `--select`、`:preset`、多个包名等都能正确传到 bash 版。注意 `:install_with_plum plum`（[L65-67](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L65-L67)）专门处理「更新 plum 自身」——bash 版会把 `plum` 这个 target 解释成 `git pull`（见 u1-l3 的 [rime-install:54-57](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L54-L57)）。

#### 4.4.3 源码精读

**转调主体 `:install_with_plum`**：

[rime-install.bat:211-235](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L211-L235)

```bat
:install_with_plum
call :install_git /needed
if errorlevel 1 exit /b %errorlevel%

set WSLENV=plum_dir:rime_dir

if defined plum_dir if exist "%plum_dir%"/rime-install (
   bash "%plum_dir%"/rime-install %*
   exit /b !errorlevel!
)
if exist plum/rime-install (
  bash plum/rime-install %*
) else if exist rime-install (
  bash rime-install %*
) else (
  echo Downloading rime-install ...
  set script_url=https://raw.githubusercontent.com/rime/plum/master/rime-install
  curl -fsSL "!script_url!" -o "%download_cache_dir%"/rime-install
  ...
  bash "%download_cache_dir%"/rime-install %*
)
exit /b %errorlevel%
```

三个要点：

1. **先确保 git**：转调后 bash 版第一件事就是 `git clone` plum（见 [rime-install:16-18](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L16-L18)），没有 git 必失败，所以这里 `call :install_git /needed` 提前兜底。
2. **`WSLENV=plum_dir:rime_dir`**：这是 Windows 为 WSL/bash 子进程导出环境变量的机制。冒号分隔的变量名会被透传到 bash 侧，于是 bash 版能读到 `plum_dir`（plum 装在哪）和 `rime_dir`（方案装到哪），与批处理侧保持一致。没有这行，bash 子进程会拿到空的 `rime_dir`，进而落到「猜目录」逻辑（u2-l4）。
3. **四级查找 bash 脚本**：优先用用户在 config 里指定的 `plum_dir`；其次看当前目录下有没有已克隆的 `plum/rime-install`；再次看当前目录直接有没有 `rime-install`（即用户就在 plum 工作副本里）；都没有就**联网下载 raw 单文件版**到缓存再跑。这是一种渐进降级——尽量复用本地、实在不行才下载。

**`--select` 的提前转调**——交互选择模式必须用 bash 版（批处理没实现菜单）：

[rime-install.bat:37-40](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L37-L40)

```bat
if "%1" == "--select" if "%use_plum%" == "1" (
  call :install_with_plum %*
  exit /b !errorlevel!
)
```

`--select` 作为**第一个参数**时立即整体转调（见 u3-l1 对 `--select` 位置敏感的说明）。这也是为什么 4.1.3 强调「有 git-bash 就默认 `use_plum=1`」——否则 `--select` 在纯批处理下无处可去。

**回退分支如何被「绕过」**：当 `use_plum=1`，[L82-86](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L82-L86) 的 `:prefer_plum_installer` 命中并 `goto after_install_package`，于是 [L87](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L87) 的 `:fallback_to_builtin_installer`（那套 GitHub /tree/ URL、`@branch`、裸包名的下载逻辑）基本不会被触发。这套回退逻辑是给「没装 git-bash」的机器兜底用的。

#### 4.4.4 代码实践

**实践目标**：验证「`use_plum=1` 时普通包名被整体转调」，并理解 `WSLENV` 的作用。

**操作步骤（源码追踪 + 可选本地验证）**：

1. 设想命令 `rime-install.bat luna-pinyin`，系统装了 git-bash（`use_plum=1`）。
2. 在 `:install_package` 追踪：L59-80 全不命中（`luna-pinyin` 不是特殊关键字、不以 `.zip` 结尾），落到 [L82-86](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L82-L86) `:prefer_plum_installer`，`use_plum==1` 成立 → `call :install_with_plum luna-pinyin`。
3. 在 `:install_with_plum` 里：`call :install_git /needed`（git 已在）→ `set WSLENV=plum_dir:rime_dir` → 假设 `plum_dir` 未定义且当前目录无 `plum/rime-install`、无 `rime-install` → 走 L226-234，**下载 raw 版 rime-install** 再 `bash ... rime-install luna-pinyin`。
4. 回答：bash 子进程拿到的 `rime_dir` 是哪个？若删掉 `set WSLENV=plum_dir:rime_dir` 这行会怎样？

**预期结果**：

- 调用链：`:install_package` → `:prefer_plum_installer` → `:install_with_plum` →（下载 raw）→ `bash rime-install luna-pinyin`。
- bash 子进程通过 `WSLENV` 拿到批处理侧的 `rime_dir`（即 config 里从注册表读到的 Weasel 目录）。若删掉 `WSLENV` 那行，bash 侧 `rime_dir` 为空，bash 版 `rime-install` 会调用 u2-l4 的 `guess_rime_user_dir`，在 Windows 上猜出 `weasel` 前端的 `$APPDATA\Rime`——多数情况下殊途同归，但若用户在 config 里自定义了 `rime_dir`，删行后就会**丢失**这个自定义值。

> **可选本地验证（Windows + git-bash）**：运行 `set use_plum=1 && rime-install.bat luna-pinyin`，观察输出出现 `Downloading rime-install ...`（若本地无脚本）以及 bash 版的 `Installing ...` 字样，证明确实转调。本环境为 Linux，无法实际运行 `.bat`，以上为静态追踪结论。

#### 4.4.5 小练习与答案

**练习 1**：`use_plum=1` 时，用户敲 `rime-install.bat https://github.com/rime/rime-prelude/archive/master.zip`，会转调 bash 吗？为什么？
**答案**：不会。`.zip` 包在 [L68-80](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L68-L80) 就被处理掉并 `goto :after_install_package`，根本到不了 `:prefer_plum_installer`。bash 版不处理本地/在线 zip 离线安装，所以这类 target 始终归批处理。

**练习 2**：`set WSLENV=plum_dir:rime_dir` 中，为什么偏偏要导出这两个变量、而不是 `package` 或 `downloader`？
**答案**：`plum_dir` 和 `rime_dir` 是「跨进程需要保持一致」的**路径配置**——bash 版要靠它们知道 plum 在哪、方案装到哪。而 `package`/`downloader` 是批处理侧的临时状态：`package` 已通过 `%*` 命令行参数传过去了，`downloader` 是批处理专用（bash 版自己会用 curl/git），都不需要导出。

**练习 3**：`:install_with_plum` 为什么要 `call :install_git /needed` 而不是 `call :install_git`？
**答案**：`/needed` 抑制了「Found git」提示，因为这是内部依赖检查、不该向用户多输出噪音；同时若 git 已就位则静默返回，未就位才触发下载安装。用户显式敲 `rime-install git` 时才不带 `/needed`、打印状态。

## 5. 综合实践

**任务**：把本讲的三条主线（参数路由、依赖管理、转调 bash）串起来，完成一次完整的「端到端调用链」追踪与一张流程图。

**背景命令**（请在阅读源码时假设它被运行）：

```bat
set use_plum=1
rime-install.bat :preset
```

请完成：

1. **初始化阶段**：从 [rime-install.bat:7](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L7) 到 [L32](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L32)，列出：加载了哪个 config、`rime_dir` 最终值（假设 config 从注册表读到 `E:\WeaselRime`）、`use_plum` 最终值（注意用户显式设了 1，自动决策行还会改它吗？）。
2. **路由阶段**：`:preset` 这个 package 在 `:install_package` 里命中哪条分支？`call` 了哪个 `.bat` 清单？该清单里 `package_list` 大致有哪几个包（参考 [preset-packages.bat](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/preset-packages.bat)）？
3. **批量 + 转调阶段**：`:install_package_group` 会把每个包扔回 `:install_package`。以其中 `luna-pinyin` 为例，它最终走 `:prefer_plum_installer` → `:install_with_plum`。请写出从 `:install_with_plum` 到 bash 版开始执行的完整调用链，并指出 `WSLENV` 传递了什么。
4. **画图**：把上述全流程画成一张流程图（可用文字/伪代码框图），标注清楚哪些步骤是「批处理自己干」、哪些是「转调 bash 干」。

**预期结果（自检）**：

1. config = 同目录 `rime-install-config.bat`；`rime_dir=E:\WeaselRime`（注册表值，且在 L13 默认值之前执行）；`use_plum=1`（用户已显式设，L32 的 `if not defined use_plum` 守卫使其不被改动）。
2. `:preset` 命中 [L103-105](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L103-L105) 的冒号分支 → `call preset-packages.bat` → 清单含 `prelude essay luna-pinyin terra-pinyin bopomofo stroke cangjie quick` 共 8 个包 → `:install_package_group`。
3. `:install_package_group`（设 `package=luna-pinyin`）→ `:install_package`（不命中特殊/zip）→ `:prefer_plum_installer`（`use_plum=1`）→ `:install_with_plum luna-pinyin` → `:install_git /needed` → `set WSLENV=plum_dir:rime_dir` →（按四级查找找到 bash 脚本）→ `bash .../rime-install luna-pinyin`。`WSLENV` 把 `plum_dir` 与 `rime_dir=E:\WeaselRime` 透传给 bash 侧。
4. 流程图中，「加载 config / 路由 / 清单展开 / 依赖检查」是批处理干；「真正 clone 仓库、跑 recipe、拷文件」由 bash 版干——批处理退化为一个「外壳 + 路由器」。

> 综合实践为源码阅读型，无需运行；若在 Windows + git-bash 上实际执行，可对照输出验证每个阶段（**待本地验证**）。

## 6. 本讲小结

- `rime-install.bat` 是 plum 为「没有 bash 的 Windows」准备的**纯批处理**安装器，能力是 bash 版的子集：能下 GitHub ZIP、用 7z 解压、拷固定几类文件（`.yaml`/`.txt`/`opencc/*`），**不**支持 recipe/patch。
- `:install_package` 是核心路由器，靠 .bat 的「变量替换 + 比较」技巧识别 `.zip` 后缀、`https://github.com/` 前缀、`:` 前缀、`-packages.bat` 后缀等形态，分流到 `download_package`/`install_zip_package`/`install_package_group` 等子程序。
- `.zip` 类 target **永远走批处理自带逻辑**（`goto :after_install_package` 跳过转调），因为 bash 版不处理离线 zip；这是路由里最重要的一条例外。
- 7z 与 git 采用「用到才装」策略，同一个安装子程序靠 `%1 == "/needed"` 区分「内部依赖检查（静默）」与「用户显式安装（打印状态）」，并有「已装 → 本地安装包 → 缓存 → 联网下载」四级就近降级。
- `use_plum` 是「升级到 bash 版」的开关：探测到 git-bash 且用户未显式禁用时自动开启；开启后绝大多数 target 经 `:prefer_plum_installer` → `:install_with_plum` 转调 bash，并通过 `WSLENV=plum_dir:rime_dir` 把路径配置透传给 bash 子进程。
- `rime-install-bootstrap.bat` 负责「补齐主脚本 + 建快捷方式」且对 config 幂等不覆盖；`rime-install-config.bat` 是用户配置入口，真正运行的逻辑是从注册表读小狼毫 `RimeUserDir`。

## 7. 下一步学习建议

- 若你想看「被转调的那个 bash 版」内部如何处理配方、增量更新，回到 **u2-l3（install-packages 主循环）** 与 **u2-l6（recipe 执行引擎）** 对照阅读，体会批处理为何只做「子集」。
- 若你对 `:preset` 展开成包名清单的机制（`package_list` 数组/字符串、`.conf` 与 `.bat` 双实现）感兴趣，继续读 **u2-l7（配置包清单与预设集合）**。
- 若你想了解交互式 `--select` 菜单（它在批处理侧被整体转调给 bash），看 **u3-l1（交互式包选择 selector.sh）**。
- 进阶练习：尝试在 `rime-install-config.bat` 里 `set use_plum=0`，然后追踪一个普通包名 `luna-pinyin` 会走 `:fallback_to_builtin_installer` 的哪条分支、最终下载哪个 URL——这是理解「纯批处理兜底路径」的好办法。
