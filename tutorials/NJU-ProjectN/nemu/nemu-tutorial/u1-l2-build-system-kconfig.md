# 构建系统——Makefile 与 Kconfig 配置机制

## 1. 本讲目标

本讲带你读懂 NEMU 是如何「从一份配置」变成「一个可执行二进制」的。读完本讲你应当能够：

- 说清 `make menuconfig` 这条命令背后发生了什么，以及它生成了哪三份产物（`.config`、`auto.conf`、`autoconf.h`）。
- 理解 NEMU 借鉴 Linux 内核的 Kconfig 描述语言如何用 `choice`/`config`/`depends on`/`source` 表达「四组开关」。
- 解释 `src/` 下各 `filelist.mk` 如何用 `DIRS-y`/`SRCS-y`/`BLACKLIST` 收集源文件，并随 `CONFIG_*` 改变编译进来的代码集合。
- 描述 `scripts/build.mk` 与 `scripts/native.mk` 的模式规则如何把 `.c` 编译为 `.o` 并链接出名为 `riscv32-nemu-interpreter` 的二进制。

本讲是 u1-l1 的延续：u1-l1 给了 NEMU 的「能力地图」，本讲回答「这些能力是怎样通过开关被选进编译产物里的」。

## 2. 前置知识

阅读本讲前，建议你已具备：

- **Make 基础**：知道变量赋值（`=`/`:=`/`?=`/`+=`）、模式规则（`%.o: %.c`）、`$(call func,arg)`、`$(shell ...)`、`-include`。
- **C 预处理**：知道 `#define` 宏如何在编译期影响代码分支。
- **u1-l1 的结论**：NEMU 顶层 `Kconfig` 用四组 `choice` 定义 ISA、引擎、运行模式、构建目标；换 ISA 或模式本质是改 `CONFIG_xxx` 宏。

如果你对 Linux 内核的 `make menuconfig` 有印象，那会非常有帮助——NEMU 几乎原样搬来了这套机制。下面两个概念是本讲的核心词汇：

- **配置项（config symbol）**：Kconfig 里的一个开关，例如 `CONFIG_MODE_SYSTEM`，它可以是 `y`（开）、`n`（关），或一个字符串/整数/十六进制值。
- **配置产物**：Kconfig 工具把配置项翻译成两种形式——给 Make 用的 `auto.conf`（Make 语法）和给 C 用的 `autoconf.h`（`#define`），让同一份配置同时驱动构建脚本和源码。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Kconfig` | 顶层配置描述文件，定义 ISA/引擎/模式/目标四组 `choice` 及构建、调试选项。 |
| `Makefile` | 入口脚本：引入配置、提取变量、收集源文件、按目标包含不同规则。 |
| `scripts/config.mk` | menuconfig / defconfig / distclean 等配置类规则，调用 kconfig 工具。 |
| `scripts/build.mk` | 真正的编译链接规则：模式规则、目录布局、`app` 目标。 |
| `scripts/native.mk` | Native ELF 模式的包装：包含 `build.mk`、加 difftest、提供 `run`/`gdb`。 |
| `src/filelist.mk` | 顶层源文件清单，声明哪些目录/文件参与编译。 |
| `src/isa/filelist.mk` | 按 `GUEST_ISA` 选择对应 ISA 目录。 |
| `src/device/filelist.mk` | 按设备开关条件加入各外设源文件。 |

另外会顺带提到 `include/common.h`（消费 `autoconf.h`）和 `tools/kconfig/`（kconfig 工具实现）。

## 4. 核心概念与源码讲解

本讲按构建的真实执行顺序拆成四个最小模块：先描述配置（Kconfig）→ 再生成配置（menuconfig 规则）→ 再从配置提取变量并收集源文件（Makefile + filelist）→ 最后编译链接（build.mk）。

### 4.1 Kconfig 配置描述语言

#### 4.1.1 概念说明

Kconfig 是一种「描述配置项之间关系」的领域语言，Linux 内核用它来管理成千上万的编译开关。NEMU 把它原样搬来，用一份顶层 `Kconfig` 加若干子 `Kconfig` 描述自己的开关。

它的核心思想是：**配置项不是散落在代码里的 `#define`，而是集中声明、彼此约束的**。这样工具可以生成一个交互式菜单（`menuconfig`），让你勾选，工具再保证勾选的合法性（比如「开启分页」依赖「系统模式」），最后把结果翻译成 `#define` 和 Make 变量。

NEMU 的 `Kconfig` 用四组 `choice` 表达「四选一」的互斥开关：

1. **Base ISA**：x86 / mips32 / riscv / loongarch32r
2. **Execution engine**：目前只有 Interpreter
3. **Running mode**：System mode（全系统）
4. **Build target**：Native ELF / Shared object / AM

此外还有两组菜单：**Build Options**（编译器、优化级别、LTO/DEBUG/ASAN）与 **Testing and Debugging**（TRACE/ITRACE/DIFFTEST 等）。

#### 4.1.2 核心流程

Kconfig 描述语言的关键构件：

```
mainmenu "标题"        # 顶层菜单名
choice ... endchoice   # 一组互斥选项，只有一个能选中
config NAME            # 声明一个配置项
  bool/string/int/hex  # 类型
  prompt "显示文字"     # 菜单里出现的文字
  default y / "x"      # 默认值
  depends on COND      # 仅当 COND 为真时该项才可见/可选
  select OTHER         # 选中此项时强制开启 OTHER
menu ... endmenu       # 分组
source "path/Kconfig"  # 把另一个 Kconfig 文件包含进来
```

一个 `choice` 里多个 `bool` 配置项互斥；但为了在 Make/C 里方便使用，NEMU 还额外声明一个**字符串配置项**（如 `ISA`），它的 `default` 根据「哪个 bool 被选中」给出对应字符串。这是 Kconfig 的常见写法：用 `bool` 表达选择，用 `string` 聚合成一个可直接用的值。

`depends on` 是约束的核心，它让某些选项只在特定前提下出现。例如差分测试 `DIFFTEST` 依赖 `TARGET_NATIVE_ELF`（共享库和 AM 模式下不可用）。

#### 4.1.3 源码精读

顶层 `Kconfig` 先用 `choice` 列出四种 ISA：

[Kconfig:L3-L14](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L3-L14) —— `choice` 里四个 `bool` 互斥，默认 `ISA_riscv`。

随后用一个 `string` 配置项 `ISA` 把选择聚合成字符串，注意 riscv 还要看 `RV64` 决定是 32 还是 64：

[Kconfig:L16-L23](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L16-L23) —— 例如 `default "riscv32" if ISA_riscv && !RV64`。

运行模式与构建目标也是 `choice`，构建目标决定了 NEMU 编译成可执行文件、共享库还是 AM 上的应用：

[Kconfig:L50-L69](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L50-L69) —— System mode 与三种 Build target。

`source` 把子配置拉进来。当选择系统模式时，才引入内存和设备的 Kconfig：

[Kconfig:L196-L199](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L196-L199) —— `if MODE_SYSTEM` 下 `source` 内存与设备配置。

而 riscv 的子配置（定义 `RV64`/`RVE`）只在选了 riscv 时引入：

[Kconfig:L31-L33](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L31-L33) —— `if ISA_riscv` 下 `source "src/isa/riscv32/Kconfig"`。

`depends on` 的典型例子在 Testing 菜单里，差分测试强依赖 Native ELF 目标：

[Kconfig:L155-L161](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L155-L161) —— `config DIFFTEST` 依赖 `TARGET_NATIVE_ELF`。

> 说明：`tools/kconfig/` 目录下是 kconfig 工具的实现（`confdata.c`、`lexer.l`、`parser.y`、`mconf.c`、`lxdialog/`），它本身就是 Linux 内核 kconfig 的精简移植，用 flex/bison 生成词法/语法分析器。你不需要读它的源码，只需知道它读 `Kconfig`、写 `.config`/`auto.conf`/`autoconf.h`。

#### 4.1.4 代码实践

1. **实践目标**：直观感受 Kconfig 的「依赖关系」如何影响菜单。
2. **操作步骤**：
   - 打开 `Kconfig`，定位到 `config CC_CLANG`（约第 79 行），注意它 `depends on !TARGET_AM`。
   - 再定位到 `config CC_ASAN`（约第 121 行），注意它 `depends on MODE_SYSTEM`。
3. **需要观察的现象**：在脑中模拟——如果你把 Build target 选成 AM，clang 选项应消失；如果你不选 System mode，ASAN 选项应消失。
4. **预期结果**：能口述出「`depends on` 让选项在菜单中动态出现/隐藏」这一机制。
5. **待本地验证**：运行 `make menuconfig` 实际切换 Build target 与 Running mode，确认 clang / ASAN 选项的显隐与你推断一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么需要单独的 `ISA` 字符串配置项，而不是直接在 Makefile 里判断 `CONFIG_ISA_riscv`？

**答案**：`choice` 里的 `bool` 只表达「选中了哪个」，但 Make 和 C 代码更希望直接拿到一个字符串值（如 `"riscv32"`）去拼路径和宏。`ISA` 这个 `string` 配置项把选择结果聚合成一个可直接 `$(CONFIG_ISA)` 使用的值，避免在多处重复写 `if ISA_riscv && !RV64` 这样的判断。

**练习 2**：`config ISA64` 没有 `prompt`，且 `default y`，它的作用是什么？

**答案**：它是一个「派生配置项」——没有 `prompt` 意味着用户不能在菜单里手动改它，它的值完全由 `depends on ISA_riscv && RV64` + `default y` 决定：当选了 riscv 且开了 RV64 时自动为 `y`。源码和 Makefile 用 `CONFIG_ISA64` 来区分 32/64 位字长（见 `include/common.h` 中 `word_t` 的定义）。

### 4.2 menuconfig 规则与配置产物

#### 4.2.1 概念说明

光有 `Kconfig` 描述文件还不够，还需要一个工具把它变成交互式菜单、并把用户选择写成文件。这就是 `scripts/config.mk` 提供的 `menuconfig` 规则。

这一步会产出**三份关键文件**，理解它们的关系是本讲的重中之重：

| 文件 | 形式 | 消费者 | 内容示例 |
| --- | --- | --- | --- |
| `.config` | 文本 | 人 / 版本控制 | `CONFIG_ISA_riscv=y`、`CONFIG_ISA="riscv32"` |
| `include/config/auto.conf` | Make 片段 | Makefile | `CONFIG_ISA="riscv32"` |
| `include/generated/autoconf.h` | C 头文件 | 源码 | `#define CONFIG_ISA_riscv 1` |

也就是说：**同一份配置，被翻译成两种语言**——Make 能 `include` 的 `auto.conf`，和 C 能 `#include` 的 `autoconf.h`。这样一份配置同时驱动构建脚本和源码。

#### 4.2.2 核心流程

`make menuconfig` 的执行链：

```
make menuconfig
   │
   ├─ 先按需编译三个工具：
   │    tools/kconfig/build/mconf   （ncurses 交互式 TUI）
   │    tools/kconfig/build/conf     （非交互，负责写出产物）
   │    tools/fixdep/build/fixdep    （修正依赖，让改 CONFIG_ 能触发重编译）
   │
   ├─ mconf Kconfig        → 弹出菜单，用户勾选，写入 .config
   └─ conf --syncconfig    → 读 .config，生成 auto.conf 与 autoconf.h
```

`mconf` 负责「人机交互」，`conf --syncconfig` 负责「同步产物」。两者分工明确。

此外 `config.mk` 还提供 `defconfig` / `savedefconfig` 规则：从 `configs/` 下的一份最小配置文件恢复整套配置（kconfig 会自动补齐默认值），便于快速切换 ISA/模式。

#### 4.2.3 源码精读

`scripts/config.mk` 在没有 `.config` 时给出醒目警告，提示先跑 menuconfig：

[scripts/config.mk:L19-L22](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/scripts/config.mk#L19-L22) —— 检测 `.config` 是否存在。

它定位三个工具的路径（都构建在各自 `build/` 子目录下）：

[scripts/config.mk:L24-L33](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/scripts/config.mk#L24-L33) —— `CONF`/`MCONF`/`FIXDEP` 路径，以及 `Kconfig := $(NEMU_HOME)/Kconfig`。

`menuconfig` 规则的精髓——先编译工具，再跑 mconf，最后 syncconfig：

[scripts/config.mk:L44-L46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/scripts/config.mk#L44-L46) —— `menuconfig: $(MCONF) $(CONF) $(FIXDEP)`，依次执行 mconf 与 `conf --syncconfig`。

`defconfig` 规则让你用一份预设配置快速初始化：

[scripts/config.mk:L48-L53](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/scripts/config.mk#L48-L53) —— `savedefconfig` 存最小配置，`%defconfig` 从 `configs/$@` 恢复。

`fixdep` 的作用藏在编译模式里（见 4.4），它把「这个 `.o` 依赖哪些 `CONFIG_` 项」写进依赖文件，使得你改了某个 `CONFIG_` 后，相关 `.o` 能自动重编。

产物被两处消费：Makefile 用 `-include` 拉入 `auto.conf`；源码经 `include/common.h` 拉入 `autoconf.h`：

[include/common.h:L24-L25](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L24-L25) —— `#include <generated/autoconf.h>`，让所有 C 文件都能看到 `CONFIG_*` 宏。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到三份产物的存在与差异。
2. **操作步骤**：
   - 在仓库根目录运行 `make menuconfig`（若提示缺 `ncurses`，安装 `libncurses-dev`）。
   - 选中 Base ISA = riscv、Running mode = System mode、Build target = Linux Native ELF；进入 Devices 菜单开启 Devices。
   - 保存退出。
   - 查看 `.config`（根目录）、`include/config/auto.conf`、`include/generated/autoconf.h`。
3. **需要观察的现象**：同一个开关在三处分别长什么样——`.config` 里 `CONFIG_MODE_SYSTEM=y`，`auto.conf` 里同样一行（Make 语法），`autoconf.h` 里 `#define CONFIG_MODE_SYSTEM 1`。
4. **预期结果**：能指出字符串型配置 `CONFIG_ISA` 在 `auto.conf` 里带引号（`"riscv32"`），在 `autoconf.h` 里是 `#define CONFIG_ISA "riscv32"`。
5. **待本地验证**：上述产物路径与内容随你的具体勾选而定，需本地运行确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `auto.conf` 用 `-include`（前缀减号）而不是 `include`？

**答案**：`-include` 表示「文件不存在也不报错」。首次使用 NEMU 时还没跑过 menuconfig，`auto.conf` 不存在；用 `include` 会让 make 直接报错中断，而 `-include` 让 Makefile 继续往下走，最终由 `config.mk` 里的警告提示用户先跑 menuconfig。这是渐进式引导的常见手法。

**练习 2**：改了一个 `CONFIG_` 后，源码如何「知道」要重新编译？

**答案**：`autoconf.h` 被 `common.h` 包含，而 `common.h` 几乎被所有 `.c` 包含。编译时 `fixdep` 会把 `autoconf.h`（以及其中涉及的 `CONFIG_`）记录到每个 `.o` 的 `.d` 依赖文件里。当你改了 `CONFIG_` 重跑 menuconfig，`autoconf.h` 内容变化，make 据依赖文件发现 `.o` 过期，于是重编相关文件。`-MMD` 标志负责生成这些依赖。

### 4.3 Makefile 变量提取与 filelist 源文件收集

#### 4.3.1 概念说明

有了 `auto.conf`，`Makefile` 接下来要做两件事：

1. **提取变量**：从 `CONFIG_*` 里取出 Make 需要的值，比如 ISA 名、引擎名、编译器、编译选项，并据此拼出二进制名。
2. **收集源文件**：遍历 `src/` 下所有 `filelist.mk`，让每个模块自己声明「我有哪些源文件」，再剔除黑名单，得到最终参与编译的 `SRCS` 列表。

这一步的关键设计是：**源文件集合是配置驱动的**。选 riscv 就只编 riscv 的 ISA 目录；开设备就编设备源文件；AM 模式下 SDB 和 alarm 被排除。这让同一套框架代码适配多种 ISA 和模式。

#### 4.3.2 核心流程

```
-include include/config/auto.conf          # 把 CONFIG_* 引入 Make
   │
   ├─ remove_quote 去引号
   ├─ GUEST_ISA = CONFIG_ISA       (riscv32)
   ├─ ENGINE    = CONFIG_ENGINE    (interpreter)
   ├─ NAME = $(GUEST_ISA)-nemu-$(ENGINE)
   ├─ CC / CFLAGS_BUILD / CFLAGS_TRACE / -D__GUEST_ISA__
   │
   ├─ find ./src -name filelist.mk → 逐个 include
   │     每个 filelist 往 DIRS-y / SRCS-y / *-BLACKLIST 里追加
   │
   └─ SRCS = (DIRS-y 下所有 *.c + 显式 SRCS-y) − (BLACKLIST)
```

`remove_quote` 是个小但关键的函数：Kconfig 的字符串配置在 `auto.conf` 里带引号（`CONFIG_ISA="riscv32"`），而拼路径时不需要引号，所以用 `$(patsubst "%",%,$(1))` 把首尾的 `"` 去掉。

#### 4.3.3 源码精读

`Makefile` 顶部先做合法性检查，确保 `NEMU_HOME` 指向一个真实的 NEMU 仓库：

[Makefile:L16-L19](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile#L16-L19) —— 检查 `src/nemu-main.c` 是否存在。

引入配置产物，并定义去引号函数：

[Makefile:L21-L25](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile#L21-L25) —— `-include auto.conf` 与 `remove_quote`。

从配置提取 ISA、引擎，拼出 `NAME`（这就是最终二进制名的前缀）：

[Makefile:L27-L30](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile#L27-L30) —— `GUEST_ISA`/`ENGINE`/`NAME`。

收集并过滤源文件——这是「filelist 机制」的落点：

[Makefile:L33-L41](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile#L33-L41) —— `find -L ./src -name "filelist.mk"` 全部 include，再用 `find` 展开 `DIRS-y`、减去黑名单得到 `SRCS`。

提取编译器与选项，其中 `-D__GUEST_ISA__=$(GUEST_ISA)` 是把 ISA 名注入 C 源码的关键宏（u1-l4 会详讲它如何驱动 `isa.h`）：

[Makefile:L43-L53](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile#L43-L53) —— `CC`/`CFLAGS_BUILD`/`CFLAGS_TRACE`/`-D__GUEST_ISA__`。

最后按构建目标分支：AM 模式走 AM 的 Makefile，否则走 Native：

[Makefile:L56-L64](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile#L56-L64) —— `ifdef CONFIG_TARGET_AM` 分支。

现在看几个具体的 `filelist.mk`。顶层清单声明基础目录与模式/目标相关的条件目录：

[src/filelist.mk:L16-L22](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/filelist.mk#L16-L22) —— 注意 `DIRS-$(CONFIG_MODE_SYSTEM) += src/memory`（非系统模式不编内存）和 `DIRS-BLACKLIST-$(CONFIG_TARGET_AM) += src/monitor/sdb`（AM 下排除 SDB）。

ISA 切换的核心就在这两行——按 `GUEST_ISA` 选择对应 ISA 目录：

[src/isa/filelist.mk:L16-L17](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/filelist.mk#L16-L17) —— `DIRS-y += src/isa/$(GUEST_ISA)`，并加入该 ISA 的 include 路径。

设备清单展示「每个外设一个 `SRCS-$(CONFIG_HAS_*)`」的细粒度开关，以及 AM 下排除 alarm：

[src/device/filelist.mk:L16-L26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/filelist.mk#L16-L26) —— 例如 `SRCS-$(CONFIG_HAS_SERIAL) += src/device/serial.c`，以及 `SRCS-BLACKLIST-$(CONFIG_TARGET_AM) += src/device/alarm.c`。

> 条件目录的原理：`DIRS-$(CONFIG_MODE_SYSTEM)` 当 `CONFIG_MODE_SYSTEM=y` 时展开为 `DIRS-y`（追加进编译集），否则展开为 `DIRS-n`（被忽略）。这是 Make 里用配置驱动文件集合的惯用法。

#### 4.3.4 代码实践

1. **实践目标**：验证「换 ISA 即换编译目录」。
2. **操作步骤**：
   - 先 `make riscv32-am_defconfig`（或用 menuconfig 选 riscv）。
   - 运行 `make -n`（dry-run，只打印不执行），观察命令里出现的 `src/isa/riscv32/...` 文件。
   - 再 `make x86`（若有对应 defconfig）或 menuconfig 切到 x86，重新 `make -n`。
3. **需要观察的现象**：编译列表里的 ISA 目录从 `src/isa/riscv32/` 变成 `src/isa/x86/`，其余文件不变。
4. **预期结果**：确认 `src/isa/filelist.mk` 的 `$(GUEST_ISA)` 替换是 ISA 切换的唯一入口。
5. **待本地验证**：实际可用的 defconfig 文件以 `configs/` 目录为准（当前仓库内有 `riscv32-am_defconfig` 等）。

#### 4.3.5 小练习与答案

**练习 1**：`SRCS = $(filter-out $(SRCS-BLACKLIST-y),$(SRCS-y))` 这一步为什么必要？

**答案**：因为有些目录是整体加入 `DIRS-y` 的（例如 `src/device`），但其中个别文件在某些模式下不该编译（例如 AM 模式下的 `alarm.c`，因为 AM 自己提供时钟）。先把整个目录的 `.c` 收集进 `SRCS-y`，再用 `filter-out` 减去黑名单，就能「按文件粒度排除」而不必拆目录。这比给每个文件单独写条件更灵活。

**练习 2**：`NAME = $(GUEST_ISA)-nemu-$(ENGINE)`，如果你选了 riscv32 + interpreter，最终二进制叫什么？

**答案**：`riscv32-nemu-interpreter`。它会出现在 `build/` 目录下（见 4.4）。选 x86 + interpreter 则是 `x86-nemu-interpreter`。差分测试的共享库版本会再加 `-so` 后缀。

### 4.4 build.mk 编译模式与 native.mk 包装

#### 4.4.1 概念说明

源文件集合确定后，最后一步是把每个 `.c` 编成 `.o`，再链接成二进制。这部分由 `scripts/build.mk` 用**模式规则**完成，`scripts/native.mk` 则是 Native ELF 模式下对 `build.mk` 的一层包装，额外提供 `run`/`gdb` 等便捷目标。

`build.mk` 的设计有两个亮点：

- **目录布局随 `NAME` 变化**：不同 ISA/引擎的产物隔离在 `build/obj-$(NAME)/` 下，互不污染。
- **共享库与可执行文件共用一套规则**：用 `$(SO)` 变量在路径和链接选项上区分，避免维护两套规则。

#### 4.4.2 核心流程

```
默认目标 app
   │
   ├─ BINARY = build/$(NAME)$(SO)        # SO 为空（ELF）或 -so（共享库）
   ├─ OBJ_DIR = build/obj-$(NAME)$(SO)
   ├─ OBJS = 每个 SRCS 对应的 .o 路径
   │
   ├─ 模式规则:  build/obj-.../%.o : %.c
   │     @echo + CC $<
   │     gcc $(CFLAGS) -c -o $@ $<
   │     fixdep 修正依赖
   │
   └─ 链接:  $(BINARY) : $(OBJS) $(ARCHIVES)
         @echo + LD $@
         gcc -o $@ $(OBJS) $(LDFLAGS) $(ARCHIVES) $(LIBS)
```

`native.mk` 在此基础上：包含 `build.mk`、包含 `tools/difftest.mk`、把 `compile_git` 作为二进制的先决条件（自动 git commit 记录编译），并定义 `run`/`gdb` 目标，给 NEMU 默认带上 `--log=build/nemu-log.txt` 参数。

#### 4.4.3 源码精读

`build.mk` 第一行就定下默认目标，所以直接 `make` 等于 `make app`：

[scripts/build.mk:L1-L1](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/scripts/build.mk#L1-L1) —— `.DEFAULT_GOAL = app`。

共享库模式下的特殊处理——加 `-fPIC`、`-shared`，并在产物名加 `-so`：

[scripts/build.mk:L3-L8](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/scripts/build.mk#L3-L8) —— `SHARE=1` 时 `SO = -so`。

目录布局——`OBJ_DIR` 与 `BINARY` 都带 `$(NAME)$(SO)`，实现多 ISA 产物隔离：

[scripts/build.mk:L10-L15](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/scripts/build.mk#L10-L15) —— `WORK_DIR`/`BUILD_DIR`/`OBJ_DIR`/`BINARY`。

模式规则把 `.c` 编成 `.o`，并调用 `fixdep` 修正依赖：

[scripts/build.mk:L28-L41](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/scripts/build.mk#L28-L41) —— `$(OBJ_DIR)/%.o: %.c` 与 `%.cc` 两条模式规则。

链接规则与便捷目标：

[scripts/build.mk:L48-L57](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/scripts/build.mk#L48-L57) —— `app: $(BINARY)`、`$(BINARY):: $(OBJS) $(ARCHIVES)`、`clean`。

`native.mk` 包装 `build.mk` 并加 difftest 与运行目标：

[scripts/native.mk:L16-L19](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/scripts/native.mk#L16-L19) —— `-include ../Makefile`、`include build.mk`、`include difftest.mk`。

`compile_git` 作为二进制的先决条件，`run`/`gdb` 提供便捷运行：

[scripts/native.mk:L21-L38](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/scripts/native.mk#L21-L38) —— 注意 `override ARGS ?= --log=$(BUILD_DIR)/nemu-log.txt`，所以 `make run` 默认会写日志。

`CFLAGS` 里有一处重要细节：`-Wall -Werror`（见 build.mk 第 25 行）意味着任何警告都会变成错误，这是 NEMU 对代码严谨性的教学要求。

#### 4.4.4 代码实践

1. **实践目标**：完整跑一遍「配置→编译→运行」，记录产物路径与名字。
2. **操作步骤**：
   - `make menuconfig`：riscv + System mode + Native ELF。
   - `make`：编译。
   - 查看产物：`ls build/`，应看到 `riscv32-nemu-interpreter` 与 `obj-riscv32-nemu-interpreter/`。
   - `make run`：运行，观察是否生成 `build/nemu-log.txt`。
3. **需要观察的现象**：编译时终端逐行打印 `+ CC src/...`、最后 `+ LD build/riscv32-nemu-interpreter`。
4. **预期结果**：二进制路径为 `build/riscv32-nemu-interpreter`，对象文件在 `build/obj-riscv32-nemu-interpreter/` 下。
5. **待本地验证**：能否成功编译取决于本地是否装好 `gcc`、`readline`、`flex`/`bison`（kconfig 需要）等依赖；运行还需内置镜像，初次可能直接退出，属正常现象。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `$(BINARY)` 用双冒号 `::` 而不是单冒号 `:`？

**答案**：双冒号规则允许**同一目标有多条规则**且都带 recipe。`build.mk` 里 `$(BINARY)` 既有自身的链接规则（`$(BINARY):: $(OBJS) $(ARCHIVES)`），又在 `native.mk` 里被附加了 `compile_git` 作为先决条件（`$(BINARY):: compile_git`）。用双冒号才能把「链接」和「先 git commit」这两个独立规则合到同一目标上而不冲突。

**练习 2**：`SO` 变量在共享库模式下取什么值？它如何影响产物名？

**答案**：`SHARE=1`（即选了 TARGET_SHARE）时 `SO = -so`，于是 `OBJ_DIR = build/obj-riscv32-nemu-interpreter-so`，`BINARY = build/riscv32-nemu-interpreter-so`。这样差分测试用的共享库与普通可执行文件互不覆盖，且一眼可辨。

## 5. 综合实践

把本讲四个模块串起来，完成一次「换 ISA 并观察整条构建链」的端到端实践：

1. **从一份预设配置出发**：运行 `make riscv32-am_defconfig`（AM 模式），再用 `make savedefconfig` 看 `configs/defconfig` 里只剩寥寥几行——体会 kconfig 的「最小配置 + 默认值补全」。
2. **切回 Native 系统模式**：`make menuconfig` 选 riscv + System mode + Native ELF + 开启 Devices，保存。
3. **对照三份产物**：打开 `.config`、`include/config/auto.conf`、`include/generated/autoconf.h`，找到 `CONFIG_ISA`、`CONFIG_MODE_SYSTEM`、`CONFIG_DEVICE` 在三处的不同写法。
4. **追踪源文件集合**：运行 `make -n` 把编译计划重定向到文件，搜索确认 `src/isa/riscv32/` 被编入、`src/monitor/sdb/` 被编入、`src/device/alarm.c` 被编入；再切到 AM 模式（`make riscv32-am_defconfig`）重跑 `make -n`，确认 `sdb` 与 `alarm.c` 消失（黑名单生效）。
5. **编译并记录产物**：`make` 后记录 `build/` 下的二进制名与对象目录名，验证它们符合 `$(GUEST_ISA)-nemu-$(ENGINE)` 公式。
6. **改一个开关看增量重编**：在 menuconfig 里关掉 `ITRACE`，再 `make`，观察只有受影响的部分 `.o` 重编（fixdep 的功劳），而非全量重编。

完成后，你应当能画出这样一张数据流图：

```
Kconfig --menuconfig--> .config --syncconfig--> auto.conf (Make) + autoconf.h (C)
   |                                                          |
   |  Makefile 提取变量: GUEST_ISA/ENGINE/NAME/CC/CFLAGS       |  源码 #include autoconf.h
   v                                                          v
filelist.mk 收集 SRCS(随 CONFIG_* 变) ──> build.mk 模式规则 ──> build/$(NAME) 二进制
```

## 6. 本讲小结

- NEMU 借鉴 Linux 内核，用 **Kconfig 描述配置** + **Makefile 驱动构建** 的两段式体系，配置与代码解耦。
- `make menuconfig` 经 `mconf`（交互）与 `conf --syncconfig`（同步）产出**三份文件**：`.config`（人读）、`include/config/auto.conf`（Make 用）、`include/generated/autoconf.h`（C 用）。
- `Makefile` 用 `remove_quote` 从 `auto.conf` 提取 `GUEST_ISA`/`ENGINE`，拼出 `NAME`（即二进制名），并把 `-D__GUEST_ISA__` 注入 C 源码。
- **filelist 机制**让每个模块自声明源文件，`DIRS-$(CONFIG_*)`/`SRCS-$(CONFIG_*)` 实现配置驱动的文件集合，`filter-out` 黑名单实现细粒度排除（如 AM 下排除 sdb/alarm）。
- `build.mk` 用模式规则把 `.c`→`.o`→二进制，产物按 `$(NAME)$(SO)` 隔离；`fixdep` 保证改 `CONFIG_` 后增量重编。
- `native.mk` 包装 `build.mk`，提供 `run`/`gdb`，并默认带 `--log=build/nemu-log.txt`。

## 7. 下一步学习建议

本讲解决了「NEMU 怎么编译出来」，下一讲 **u1-l3 启动流程** 将进入运行时：追踪 `main()` → `init_monitor()` → `engine_start()` 的初始化链路，看这份编译产物启动后究竟做了什么。建议在继续前：

- 亲自跑通一次 `make menuconfig` + `make`，对本讲的产物路径有手感。
- 阅读 `include/common.h` 全文，看 `autoconf.h` 里的 `CONFIG_*` 如何通过 `MUXDEF` 等宏影响 `word_t`/`vaddr_t` 等基础类型定义（这会自然过渡到 u1-l4 的 ISA 抽象层）。
- 浏览 `src/` 下任意一个 `filelist.mk`，确认你能看懂它的每一行在控制什么。
