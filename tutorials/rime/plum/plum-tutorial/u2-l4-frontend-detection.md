# 前端识别与用户目录猜测 frontend.sh

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 plum 是如何**仅凭一个 `OSTYPE` 环境变量**推断出「当前应该装给哪个 Rime 前端」的；
- 默写出五个常见前端（ibus-rime / squirrel / weasel / fcitx-rime / fcitx5-rime）各自对应的默认 Rime 用户目录；
- 解释 `rime_dir` 与 `rime_frontend` 这两个环境变量的**优先级与覆盖关系**，知道在什么情况下猜测逻辑会被完全跳过；
- 自己构造不同的 `OSTYPE` / `rime_frontend` 取值，预测 `guess_rime_user_dir` 会导出什么样的 `rime_dir`。

本讲聚焦单一文件 `scripts/frontend.sh`，它只有 53 行，却是「`curl | bash` 一行命令为什么不需要你指定目录」这一体验的关键。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自 u1-l3 与 u2-l1）：

- **target 与 rime_dir**（u1-l3）：`target` 是命令行上每个空格分隔的参数（告诉 plum **装什么**）；`rime_dir` 是 Rime 用户目录，是文件最终落地的位置（告诉 plum **装到哪**）。当用户不显式给 `rime_dir` 时，就需要 plum 自己「猜」一个。
- **环境变量的两种判空写法**（u1-l2）：`[[ -n "${var}" ]]` 表示「已设置且非空」，`[[ -z "${var}" ]]` 表示「未设置或为空」。本讲几乎每一步都建立在这两个判断上。
- **require/provide 模块机制**（u2-l1）：`scripts/` 下每个模块文件都以 `provide '名字'` 收尾登记自己，使用者用 `require '名字'` 触发去重加载。`frontend.sh` 同样遵循这个约定。

补充两个本讲要用、但属于 Rime 生态背景的概念：

- **Rime 前端（frontend）**：Rime 只是一个输入法**引擎**，它本身不直接接管键盘。真正接收按键、显示候选词的是各个平台的「前端」——Linux 上的 `ibus-rime` / `fcitx-rime` / `fcitx5-rime`、macOS 上的 `squirrel`、Windows 上的 `weasel`（小狼毫）。**不同前端把用户配置放在不同的目录**，这正是 `frontend.sh` 要解决的问题。
- **`OSTYPE`**：Bash 在启动时自动设置的内建变量，用来标识当前操作系统/运行环境，例如 `linux-gnu`、`darwin23`（macOS）、`msys`（Git Bash）等。注意：原生 Windows 的 `cmd.exe` **不会**设置 `OSTYPE`——Windows 用户走的是另一条 `rime-install.bat` 路径（见 u3-l2），本讲讨论的是 bash 脚本路径。

## 3. 本讲源码地图

本讲只涉及一个核心源码文件，外加它的调用方与样式依赖：

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `scripts/frontend.sh` | 定义 `guess_rime_user_dir`：按 `OSTYPE` 猜 `rime_frontend`，再按 `rime_frontend` 映射出 `rime_dir` | **主角**，逐行精读 |
| `rime-install` | 主入口；在 `rime_dir` 为空时加载 frontend 模块并调用 `guess_rime_user_dir` | 调用方，说明何时触发猜测 |
| `scripts/styles.sh` | 提供 `warning` / `print_option` 等终端输出函数 | 依赖；解释 `frontend.sh` 顶部的 `require 'styles'` |

整个 `frontend.sh` 的结构可以用一句话概括：**一个函数 `guess_rime_user_dir`，两个串联的 `case`，外加标准的模块头尾**。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **OSTYPE 推断 frontend**：从「我在哪个系统」推出「装给哪个前端」。
2. **frontend 映射 rime_dir**：从「装给哪个前端」推出「文件放哪个目录」。
3. **环境变量覆盖**：用户如何用 `rime_dir` / `rime_frontend` 直接跳过猜测。

### 4.1 模块骨架与调用时机

#### 4.1.1 概念说明

`guess_rime_user_dir` 不是随便被调用的——它只在「用户没有显式指定安装目录」时才被触发。理解这一点，才能理解为什么有时候你根本看不到任何「猜测」的输出。

整个猜测发生在**主入口 `rime-install` 的进程内**（不是子 shell），因此函数内部用 `export` 导出的 `rime_dir` 会在后续的 `install-packages.sh` 调用中持续可见。这是一个关键设计：**导出的副作用是对外可见的**。

#### 4.1.2 核心流程

```
rime-install 启动
  │
  ├─ [[ -z "${rime_dir}" ]] ?     ← 用户是否显式指定了目录？
  │     │
  │     ├─ 否（已有 rime_dir） → 跳过整个猜测，直接进入安装循环
  │     │
  │     └─ 是（rime_dir 为空）→ require 'frontend'
  │                              └─ guess_rime_user_dir()
  │                                    │  (在主进程内 export rime_dir)
  │                                    ▼
  └─ for target: install-packages.sh  "${target}"  "${rime_dir:-.}"
                                              ↑ 若仍未设，兜底为当前目录 "."
```

#### 4.1.3 源码精读

触发时机在入口脚本里：

[rime-install:L30-L34](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L30-L34) —— `rime_dir` 为空时才 `require 'frontend'` 并调用 `guess_rime_user_dir`，注释明确写了它会 `exports rime_dir`。

[rime-install:L60](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L60) —— 真正把目录传给安装器时用的是 `${rime_dir:-.}`。这里的 `:-.` 是一道**安全兜底**：即便猜测函数因未知前端而没能导出 `rime_dir`，安装也不会失败，而是退而装到当前目录 `.`，避免出现空参数。

模块文件的头尾遵循 u2-l1 讲过的标准约定：

[scripts/frontend.sh:L1-L5](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh#L1-L5) —— 文件开头先 `require 'styles'`（因为后面要用到 `warning`、`print_option`），再定义 `guess_rime_user_dir`。

[scripts/frontend.sh:L52-L53](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh#L52-L53) —— 文件末尾 `provide 'frontend'`，把模块名登记进 `loaded_modules`，供 `require` 去重。

#### 4.1.4 代码实践

1. **实践目标**：确认「猜测只在 `rime_dir` 为空时触发」这一时序。
2. **操作步骤**：
   ```bash
   cd /path/to/plum
   source scripts/bootstrap.sh
   require 'frontend'
   # 情况一：先清空，再调用
   unset rime_dir rime_frontend
   guess_rime_user_dir
   echo "A: rime_frontend=$rime_frontend  rime_dir=$rime_dir"
   # 情况二：预设一个 rime_dir，再调用
   unset rime_frontend; rime_dir=/tmp/my-rime
   guess_rime_user_dir
   echo "B: rime_frontend=$rime_frontend  rime_dir=$rime_dir"
   ```
3. **观察现象**：情况 A 会打印一行 `Installing for Rime frontend: ...`；情况 B **不会**打印任何东西。
4. **预期结果**：B 中 `rime_frontend` 仍为空、`rime_dir` 仍是你预设的 `/tmp/my-rime`，证明函数第一步就返回了、根本没进入猜测。
5. **待本地验证**：情况 A 的具体 `rime_dir` 取决于你机器的 `OSTYPE` 与 `$HOME`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `guess_rime_user_dir` 用 `export rime_dir=...` 而不是普通的 `rime_dir=...`？

> **答案**：因为它在主入口 `rime-install` 的进程里被直接调用（不是在 `$(...)` 子 shell 里）。`export` 才能保证导出的 `rime_dir` 被**随后启动的子进程** `install-packages.sh` 看到；普通赋值在 bash 中虽对当前 shell 可见，但 `export` 是更明确、更安全的写法，确保环境变量跨进程传递。

---

### 4.2 OSTYPE 推断 frontend

#### 4.2.1 概念说明

这是猜测的**第一段**：从「我跑在什么系统上」推出「这个系统默认用哪个 Rime 前端」。

直觉很朴素：Linux 上最常见的前端是 `ibus-rime`，macOS 上是 `squirrel`，Windows 上是 `weasel`。plum 就按这个「最常见」假设做默认。

但要注意两个**隐藏假设**，它们也是 plum 最容易「猜错」的地方：

1. **Linux 默认猜 ibus-rime**：如果你在 Linux 上用的是 fcitx / fcitx5，这个默认就是错的，需要你显式覆盖（见 README 的推荐用法）。
2. **Windows 原生 cmd 没有 `OSTYPE`**：`OSTYPE` 是 bash 内建变量，所以这一段只对 bash 环境（含 Git Bash / MSYS / Cygwin / WSL）有意义；原生 Windows 用户实际走的是 `rime-install.bat`。

#### 4.2.2 核心流程

第一个 `case` 按 `OSTYPE` 的**通配模式**匹配，给 `rime_frontend` 赋一个带 `rime/` 前缀的「包路径」形式：

```
OSTYPE 模式            →  rime_frontend
─────────────────────────────────────
linux*                 →  rime/ibus-rime
darwin*                →  rime/squirrel
cygwin* | msys* | win* →  rime/weasel   (Weasel)
(其它)                 →  不赋值，打印 "Unknown OSTYPE" 警告
```

注意这里的 `*` 是 `case` 语句的 glob 通配符，不是正则。`linux*` 能匹配 `linux-gnu`、`linux-musl` 等所有以 `linux` 开头的值。

#### 4.2.3 源码精读

[scripts/frontend.sh:L9-L26](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh#L9-L26) —— 第一段 `case`，把 `OSTYPE` 映射到 `rime_frontend`。三个关键细节：

- 外层套着 `if [[ -z "${rime_frontend}" ]]; then`（[L9](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh#L9)）：**只有用户没给 `rime_frontend` 时才猜**。这就是「环境变量覆盖」的一个入口——显式设了就不猜。
- 落到 `*` 分支（[L22-L24](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh#L22-L24)）时**不 export `rime_frontend`**，只打印警告。这会让 `rime_frontend` 继续为空，进而影响第二段 `case`（见 4.3）。
- 警告用了 `$(warning 'WARNING:')` 和 `$(print_option "$OSTYPE")` 两个样式函数（来自 `styles.sh`），分别输出**粗体黄**和**粗体品红**的文字（[styles.sh:L52-L54](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/styles.sh#L52-L54) 与 [styles.sh:L68-L70](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/styles.sh#L68-L70)），让警告在终端里醒目。

#### 4.2.4 代码实践

1. **实践目标**：验证不同 `OSTYPE` 取值如何被映射成 `rime_frontend`。
2. **操作步骤**：
   ```bash
   cd /path/to/plum
   source scripts/bootstrap.sh
   require 'frontend'
   for os in linux-gnu darwin23 msys cygwinwin solaris; do
       unset rime_dir rime_frontend
       OSTYPE="$os" guess_rime_user_dir
       printf 'OSTYPE=%-12s -> rime_frontend=%s\n' "$os" "${rime_frontend:-(空)}"
   done
   ```
   > 注：上面循环里有一个故意写错的值 `cygwinwin`，用来观察「都不匹配」的情况。
3. **观察现象**：前几个值分别映射到 `rime/ibus-rime`、`rime/squirrel`、`rime/weasel`；`cygwinwin` 与 `solaris` 会触发 `Unknown OSTYPE` 警告，且 `rime_frontend` 留空。
4. **预期结果**：与上面的映射表一致。
5. **待本地验证**：`OSTYPE=msys` 的映射（`rime/weasel`）在非 Windows 的 Git Bash 上也能复现，因为它只读 `OSTYPE` 字符串、不真正探测系统。

> **为什么 `OSTYPE=msys` 会猜 `weasel`？** 因为 Git Bash / MSYS 通常跑在 Windows 上，而 Windows 的 Rime 前端就是 weasel。plum 用环境名代替了真正的系统探测——简单但偶尔会失准（例如在装了 Git Bash 的 Linux 上，虽然罕见）。

#### 4.2.5 小练习与答案

**练习 1**：在 WSL（Windows Subsystem for Linux）里运行 `bash rime-install`，`OSTYPE` 会被猜成什么前端？这对吗？

> **答案**：WSL 里的 bash 把 `OSTYPE` 设为 `linux-gnu`（以 `linux` 开头），所以会被猜成 `rime/ibus-rime`，`rime_dir` 指向 `~/.config/ibus/rime`——这是 **WSL 内部**的路径，**不是** Windows 上 weasel 真正读取的 `%APPDATA%\Rime`。所以如果目的是给 Windows 上的 weasel 装方案，在 WSL 里裸跑会装错地方；正确做法是显式 `rime_frontend=weasel` 或直接指定 `rime_dir`。

**练习 2**：如果想让 plum 支持一个新系统（比如 `freebsd*`），默认装 `rime/ibus-rime`，最小改动是什么？

> **答案**：在 [frontend.sh:L11-L25](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh#L11-L25) 的第一个 `case` 里，把 `linux*` 那一支改成 `linux* | freebsd*`（合并到同一个模式分支），就能让 FreeBSD 也默认走 ibus-rime。注意这是「示例性修改」，本讲不要求你真的改源码。

---

### 4.3 frontend 映射 rime_dir

#### 4.3.1 概念说明

这是猜测的**第二段**：既然知道了前端，就查表得到该前端**默认的用户配置目录**。这一段是一张纯查表，没有副作用逻辑——同一个 `rime_frontend` 永远映射到同一个 `rime_dir`。

一个值得注意的细节：第二段的模式里**同时写了带前缀和不带前缀两种写法**，例如 `rime/ibus-rime | ibus-rime`。这是因为第一段猜出来的是带 `rime/` 前缀的（如 `rime/ibus-rime`），而用户在命令行里常写不带前缀的短名（如 `rime_frontend=ibus-rime`）。两种写法都要能匹配，才不会让用户困惑。

#### 4.3.2 核心流程

第二个 `case` 按 `rime_frontend` 查表，赋值并 `export rime_dir`：

| rime_frontend 模式 | 默认 rime_dir |
| --- | --- |
| `fcitx/fcitx-rime` \| `fcitx-rime` | `$HOME/.config/fcitx/rime` |
| `fcitx5/fcitx5-rime` \| `fcitx5-rime` | `$HOME/.local/share/fcitx5/rime` |
| `rime/ibus-rime` \| `ibus-rime` | `$HOME/.config/ibus/rime` |
| `rime/squirrel` \| `squirrel` | `$HOME/Library/Rime` |
| `rime/weasel` \| `weasel` | `$APPDATA\\Rime` |
| (其它) | 不赋值，打印 `Unknown Rime frontend` 警告并 `return` |

两个需要解释的点：

- **为什么 weasel 的目录里有双反斜杠 `$APPDATA\\Rime`？** 因为这段 bash 脚本最终可能在 Windows 的 Git Bash / MSYS 下运行，路径需要用反斜杠风格；`\\` 在 bash 双引号里转义成一个字面的 `\`，最终得到类似 `C:\Users\you\AppData\Roaming\Rime` 的路径。`$APPDATA` 是 Windows 环境变量（在 MSYS 里通常已设置），非 Windows 上它为空，此时这个分支实际无意义。
- **`return` 而非继续**：落到 `*` 分支时（[L44-L47](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh#L44-L47)），函数直接 `return`，**不会**执行 [L49](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh#L49) 的「Installing for Rime frontend」提示，也不会设 `rime_dir`。于是回到 `rime-install` 时，`${rime_dir:-.}` 兜底为 `.`，文件会装到当前目录——一个明显的「出错信号」，提示用户重新指定前端。

#### 4.3.3 源码精读

[scripts/frontend.sh:L28-L48](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh#L28-L48) —— 第二段 `case`，五个已知前端 + 一个 `*` 兜底。注意每个分支都是 `export rime_dir=...`，把结果**写回环境**。

[scripts/frontend.sh:L49](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh#L49) —— 成功赋值后，打印一行 `Installing for Rime frontend: <frontend>`。这行是给用户看的「确认信号」：只要你在终端看到它，就知道 plum 猜了一个前端、并且 `rime_dir` 已被设好。`${rime_frontend:-(unknown)}` 的 `:-` 是参数展开默认值——万一是空串，至少打印 `(unknown)` 而不是空白。

把这五条路径和 4.2 的 OSTYPE 表合起来，就能推出「裸跑（什么都不设）时每个系统装到哪」：

| 系统（OSTYPE） | 猜出的 frontend | 最终 rime_dir |
| --- | --- | --- |
| Linux (`linux*`) | `rime/ibus-rime` | `~/.config/ibus/rime` |
| macOS (`darwin*`) | `rime/squirrel` | `~/Library/Rime` |
| Git Bash/MSYS (`msys*`) | `rime/weasel` | `%APPDATA%\Rime` |

#### 4.3.4 代码实践

1. **实践目标**：亲手验证五个前端的目录映射，特别是 `fcitx5-rime`。
2. **操作步骤**：
   ```bash
   cd /path/to/plum
   source scripts/bootstrap.sh
   require 'frontend'
   for fe in ibus-rime squirrel weasel fcitx-rime fcitx5-rime bogus; do
       unset rime_dir
       rime_frontend="$fe"
       guess_rime_user_dir
       printf '%-12s -> rime_dir=%s\n' "$fe" "${rime_dir:-(未设置)}"
   done
   ```
3. **观察现象**：前五个分别打印对应目录与一行 `Installing for Rime frontend: ...`；`bogus` 会打印 `Unknown Rime frontend: bogus` 且 `rime_dir` 为空。
4. **预期结果**：与上面的映射表一致；注意 `fcitx5-rime` 走的是 `$HOME/.local/share/fcitx5/rime`，**与** `fcitx-rime` 的 `$HOME/.config/fcitx/rime` **不同**——这是 fcitx4 与 fcitx5 的真实目录差异。
5. **待本地验证**：`weasel` 分支依赖 `$APPDATA`，在 Linux 上该变量为空，`rime_dir` 会变成 `\Rime`（路径无意义）——这正好说明该分支只该在 Windows 环境命中。

#### 4.3.5 小练习与答案

**练习 1**：为什么第二段 `case` 每个分支都写成 `rime/ibus-rime | ibus-rime` 两种形式？只写 `ibus-rime` 行不行？

> **答案**：因为第一段 OSTYPE 猜出来的是带 `rime/` 前缀的 `rime/ibus-rime`，而用户手动指定时常写短名 `ibus-rime`。两种来源都要能匹配同一张表，所以并列写出两种模式。如果只写 `ibus-rime`，那么 OSTYPE 自动猜出的 `rime/ibus-rime` 就会落到 `*` 兜底分支，导致「明明是 Linux 却提示 Unknown frontend」的悖论。

**练习 2**：当 `rime_frontend` 为空（比如第一段 OSTYPE 未命中）时，第二段 `case` 会怎样？

> **答案**：`case "" in` 不匹配任何已知模式，落到 `*` 分支，打印 `Unknown Rime frontend: (unknown)`（因为 `${rime_frontend:-(unknown)}` 展开为 `(unknown)`），然后 `return`，`rime_dir` 保持为空。最终 `rime-install` 里 `${rime_dir:-.}` 把目录兜底为当前目录 `.`。

---

### 4.4 环境变量覆盖

#### 4.4.1 概念说明

前面两段都是「猜」。但 plum 的设计哲学是：**用户显式说的，永远比猜的优先**。这一模块讲的就是两道「短路」关卡，它们让用户可以随时跳过猜测。

两道关卡对应两个环境变量，优先级从高到低：

1. **`rime_dir`（最高优先级）**：一旦设了，`guess_rime_user_dir` 在**第一行**就 `return`，整个猜测（含 OSTYPE 段、frontend 段、提示输出）全部跳过。
2. **`rime_frontend`（次优先级）**：只有当 `rime_dir` 未设时才进入函数；进入后，若 `rime_frontend` 已设，则跳过 OSTYPE 猜测段、直接用它做第二段的查表。

换句话说，三者的求值顺序是：

\[
\text{rime\_dir 显式?} \;\succ\; (\text{rime\_frontend 显式?} \;\succ\; \text{OSTYPE 猜测}) \;\succ\; \text{frontend}\to\text{rime\_dir 查表}
\]

#### 4.4.2 核心流程

```
guess_rime_user_dir:
  ├─ rime_dir 非空?         → return（彻底跳过）          [关卡 1]
  ├─ rime_frontend 为空?    → 用 OSTYPE 猜 rime_frontend  [4.2]
  │     （非空则保留用户值，跳过本步）
  ├─ 按 rime_frontend 查表 → export rime_dir             [4.3]
  └─ 打印 "Installing for Rime frontend: ..."
```

两种「前缀赋值」覆盖方式（来自 README 实战示例）：

- `rime_frontend=fcitx-rime bash rime-install` —— 跳过 OSTYPE 段，直接查 fcitx-rime 的表，得到 `~/.config/fcitx/rime`。
- `rime_dir="$HOME/.config/fcitx/rime" bash rime-install` —— 完全跳过猜测，目录由你说了算。

#### 4.4.3 源码精读

[scripts/frontend.sh:L6-L8](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh#L6-L8) —— **关卡 1**：`rime_dir` 非空立即 `return`。这是最高优先级的短路。

[scripts/frontend.sh:L9](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh#L9) —— **关卡 2**：只有 `rime_frontend` 为空时，才进入下面的 OSTYPE `case`。换言之，用户给的 `rime_frontend` 永远胜过 OSTYPE 猜测。

README 给出的两个覆盖示例（与本模块直接对应）：

[README.md:L149-L159](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/README.md#L149-L159) —— 分别示范用 `rime_frontend` 覆盖前端、用 `rime_dir` 覆盖目录。README 特意在「第三方 Rime 发行版」一节给出，正是因为 Linux 上 fcitx 用户无法依赖 OSTYPE 的默认猜测（默认是 ibus-rime），必须显式覆盖。

#### 4.4.4 代码实践

1. **实践目标**：验证「显式覆盖 > 猜测」的三层优先级。
2. **操作步骤**：
   ```bash
   cd /path/to/plum
   source scripts/bootstrap.sh
   require 'frontend'

   echo "--- 情况 A：什么都不设（全靠猜）---"
   unset rime_dir rime_frontend
   guess_rime_user_dir; echo "rime_frontend=$rime_frontend rime_dir=$rime_dir"

   echo "--- 情况 B：只设 rime_frontend=fcitx5-rime ---"
   unset rime_dir; rime_frontend=fcitx5-rime
   guess_rime_user_dir; echo "rime_frontend=$rime_frontend rime_dir=$rime_dir"

   echo "--- 情况 C：同时设 rime_dir（最高优先级）---"
   rime_dir=/tmp/override rime_frontend=fcitx5-rime
   guess_rime_user_dir; echo "rime_frontend=$rime_frontend rime_dir=$rime_dir"
   ```
3. **观察现象**：A 打印一行 `Installing for Rime frontend: ...`（内容取决于你的 `OSTYPE`）；B 打印 `Installing for Rime frontend: fcitx5-rime` 且 `rime_dir` 是 fcitx5 目录；C **什么都不打印**，`rime_dir` 仍是你设的 `/tmp/override`。
4. **预期结果**：与上述一致——C 证明了 `rime_dir` 的最高优先级会完全压制 `rime_frontend`。
5. **待本地验证**：情况 A 在你的机器上的具体取值需实跑确认。

#### 4.4.5 小练习与答案

**练习 1**：README 为什么推荐 `rime_dir="$HOME/.config/fcitx/rime" bash rime-install` 这种「命令前缀赋值」，而不是先 `export rime_dir=...` 再跑？

> **答案**：前缀赋值（`VAR=val cmd`）只对这一条命令的子进程生效，**不会污染当前 shell 的环境**。用完之后 `rime_dir` 在你当前的终端里依然是空的，下次想装到别处时不会被上次的值干扰。`export` 则会一直留在 shell 里，容易「忘记清掉」导致后续命令都装到同一个地方。

**练习 2**：如果你同时设了 `rime_dir` 和一个**错误**的 `rime_frontend`（比如 `rime_frontend=bogus`），`guess_rime_user_dir` 会报 `Unknown Rime frontend` 吗？

> **答案**：不会。因为 [L6-L8](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh#L6-L8) 检查到 `rime_dir` 非空就立刻 `return`，根本走不到读 `rime_frontend` 的那一步。错误的前端值被静默忽略——这也是「`rime_dir` 最高优先级」的一个体现。

---

## 5. 综合实践

**任务**：写一个最小的「前端探测脚本」，把本讲三个模块串起来，模拟 plum 在不同机器上的行为。

1. **实践目标**：用一段脚本枚举若干 `(OSTYPE, rime_frontend, rime_dir)` 组合，预测并验证 `guess_rime_user_dir` 的输出，画出完整的优先级判定表。
2. **操作步骤**：
   ```bash
   cd /path/to/plum
   source scripts/bootstrap.sh
   require 'frontend'

   probe() {
       # $1=OSTYPE  $2=预设的 rime_frontend(可空)  $3=预设的 rime_dir(可空)
       unset rime_dir rime_frontend
       OSTYPE="$1"; [[ -n "$2" ]] && rime_frontend="$2"; [[ -n "$3" ]] && rime_dir="$3"
       echo "### OSTYPE=$1  rime_frontend=${2:-(空)}  rime_dir=${3:-(空)}"
       guess_rime_user_dir
       echo "    => rime_frontend=${rime_frontend:-(空)}  rime_dir=${rime_dir:-(空/将兜底为 .)}"
       echo
   }

   probe linux-gnu   ""           ""
   probe linux-gnu   fcitx5-rime  ""
   probe darwin23    ""           ""
   probe msys        ""           ""
   probe solaris     ""           ""          # 未知 OSTYPE
   probe linux-gnu   ""           /tmp/my     # rime_dir 最高优先级
   probe linux-gnu   bogus        ""          # 未知 frontend
   ```
3. **观察现象**：逐条对比「输入三元组」与「输出」，注意哪几条**没有**打印 `Installing for Rime frontend`（分别是 `rime_dir` 已设、以及未知 frontend 这两种）。
4. **预期结果**：
   - 第 1 条：`rime_frontend=rime/ibus-rime`，`rime_dir=~/.config/ibus/rime`；
   - 第 2 条：`rime_frontend=fcitx5-rime`（保留用户值），`rime_dir=~/.local/share/fcitx5/rime`；
   - 第 3 条：`rime_frontend=rime/squirrel`，`rime_dir=~/Library/Rime`；
   - 第 4 条：`rime_frontend=rime/weasel`，`rime_dir=$APPDATA\Rime`（Linux 上 `$APPDATA` 为空）；
   - 第 5 条：打印 `Unknown OSTYPE`，且接着 `Unknown Rime frontend: (unknown)`，`rime_dir` 为空；
   - 第 6 条：**无任何输出**，`rime_dir=/tmp/my`（最高优先级短路）；
   - 第 7 条：跳过 OSTYPE 段，打印 `Unknown Rime frontend: bogus`，`rime_dir` 为空（将兜底为 `.`）。
5. **待本地验证**：涉及 `$HOME` / `$APPDATA` 的具体展开值取决于你的机器；`~` 在 `echo` 里不会自动展开，需用 `echo "$HOME..."` 或 `eval` 才能看到完整路径。

## 6. 本讲小结

- `guess_rime_user_dir` 由**两个串联的 `case`** 组成：先用 `OSTYPE` 猜 `rime_frontend`（Linux→ibus-rime、macOS→squirrel、MSYS/Cygwin/Win→weasel），再用 `rime_frontend` 查表得到 `rime_dir`。
- 五个前端对应五条默认目录：`~/.config/ibus/rime`、`~/Library/Rime`、`$APPDATA\Rime`、`~/.config/fcitx/rime`、`~/.local/share/fcitx5/rime`；其中 fcitx 与 fcitx5 路径不同（`.config` vs `.local/share`）。
- 三层优先级：**`rime_dir`（显式）> `rime_frontend`（显式）> OSTYPE 猜测**；设了 `rime_dir` 函数第一行就 `return`，设了 `rime_frontend` 就跳过 OSTYPE 段。
- 所有赋值都用 `export`，因为函数在主入口 `rime-install` 进程里被直接调用，导出的 `rime_dir` 要跨进程传给后续的 `install-packages.sh`。
- 两类「猜不到」都会落到 `*` 兜底分支、打印黄色 `WARNING`：未知 `OSTYPE` 不设 frontend、未知 frontend 不设 `rime_dir`（后者还会让 `rime-install` 的 `${rime_dir:-.}` 兜底为当前目录 `.`）。
- 成功时会打印一行 `Installing for Rime frontend: ...`，它是判断「plum 这次到底猜没猜、猜成了什么」的最直接信号。

## 7. 下一步学习建议

- **回到主流程**：本讲只解决了「`rime_dir` 从哪来」。接下来建议读 [u2-l3 安装主循环 install-packages.sh](u2-l3-install-main-loop.md)，看 `rime_dir`（即 `install-packages.sh` 的 `output_dir`）是如何作为第二参数、被 `install_files` 用来决定每个文件落地位置的。
- **横向对比 Windows 实现**：本讲的 bash 路径依赖 `OSTYPE`；原生 Windows 用户走的是 `rime-install.bat`，那里用 `%APPDATA%\Rime` 直接定目录（[rime-install.bat:L13](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install.bat#L13)）。等学到 [u3-l2 Windows 批处理安装器](u3-l2-windows-batch-installer.md) 时，可以对比两套实现的「目录确定」策略差异。
- **延伸阅读源码**：只需重读 [scripts/frontend.sh](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/scripts/frontend.sh) 全文（仅 53 行）与 [rime-install:L30-L34](https://github.com/rime/plum/blob/b1be1969f914cc005add4090631b855db00c2591/rime-install#L30-L34) 的调用点，即可完整掌握本讲内容。
