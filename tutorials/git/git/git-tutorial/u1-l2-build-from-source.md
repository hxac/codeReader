# 从源码构建 git：Makefile 体系

## 1. 本讲目标

本讲承接上一讲「Git 是什么」。上一讲我们建立了「git 是内容寻址的对象数据库」这一认知地基，并用 `version.h` 与 `version.c` 串起了「文档版本号 ↔ 源码符号」的链路。本讲要把这条链路一直延伸到**构建系统**：源码里的 `git_version_string` 这个符号，到底是怎么被编译进可执行文件里的。

学完后你应当能够：

1. 不看文档，仅凭 Makefile 说出 `make` 默认会构建出哪些产物、装到哪里。
2. 看懂 git Makefile 顶部那一大片 `NO_*` / `HAVE_*` 可移植性开关的用途，知道在缺库的机器上怎么关掉某个可选依赖。
3. 完整复述版本号 `git --version` 背后的生成链路：`GIT-VERSION-GEN` → `version-def.h` → `version.c` → `git_version_string`。
4. 在自己机器上从源码编译出一个能跑的 `./git`，并验证它确实来自你刚编译的代码。

## 2. 前置知识

- **Make 与 Makefile**：`make` 是一个根据「依赖关系」决定要执行哪些命令的构建工具。Makefile 里的一条规则形如 `目标: 依赖` 换行 `\t命令`。`make` 不带参数时，默认执行**第一条不带点（`.`）前缀的规则**。
- **变量与 `include`**：Makefile 里 `prefix = $(HOME)` 是变量赋值，`$(prefix)` 是取值；`include foo` 会把 `foo` 的内容原样插入当前位置，`-include foo` 则在 `foo` 不存在时不报错。
- **C 编译基本流程**：`.c` → 编译 → `.o`（目标文件）→ 链接 → 可执行文件。git 用 `cc` 编译、`ar` 归档静态库。
- **内容寻址复习**：上一讲提到 git 以内容哈希命名一切对象。本讲与哈希无关，但要记住 `git --version` 输出的版本字符串也是一个「被编译进二进制」的常量，它和源码符号一一对应。

> 本讲不要求会写 C，但需要能读懂 shell 脚本和 Makefile 语法。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Makefile` | 总构建脚本。定义默认目标、路径变量、可移植性开关、对象列表、链接规则。 |
| `shared.mak` | 跨子 Makefile 共享的行为：去掉 GNU make 隐式规则、`.DELETE_ON_ERROR`、静默输出（`QUIET_*`）、`version_gen` 宏。 |
| `GIT-VERSION-GEN` | shell 脚本。负责**解析出当前版本号**，并把模板里的 `@GIT_VERSION@` 等占位符替换成真实值。 |
| `GIT-VERSION-FILE.in` | 旧式版本文件模板，仅一行 `GIT_VERSION=@GIT_VERSION@`，用于把版本号注入到 make 变量。 |
| `version-def.h.in` | 新式 C 头文件模板，把版本号定义成 C 宏 `#define GIT_VERSION "@GIT_VERSION@"`。 |
| `version.c` | 用 `version-def.h` 里的宏定义 `git_version_string[]` 常量，最终被 `git --version` 打印。 |
| `INSTALL` | 给最终用户看的安装说明，讲了 `make` / `make install`、可选依赖、profile 构建。 |

## 4. 核心概念与源码讲解

### 4.1 Make 默认目标与构建产物

#### 4.1.1 概念说明

git 是一个有上千个 `.c` 文件的大型 C 项目。直接手敲 `cc` 命令逐个编译是不现实的，所以项目用一套 Makefile 来描述「先编译什么、再链接什么、装到哪里」。

理解 git 构建的第一步，是回答一个最朴素的问题：**在一个干净 checkout 上敲 `make`，到底会发生什么？** 答案藏在 Makefile 的第一条规则里，以及它依赖的那几行 `all::` 累加目标里。

#### 4.1.2 核心流程

`make` 不带参数时的执行流程：

1. 读到第一条规则 `all::`（Makefile 第 2 行），这是默认目标。
2. `all::` 是**双冒号规则**，可以被多条 `all:: ...` 反复追加依赖与命令。make 会把它们全部收集起来。
3. `all::` 依次依赖并执行：
   - `shell_compatibility_test`：检查 `SHELL_PATH` 指向的 shell 是否够现代。
   - `$(ALL_COMMANDS_TO_INSTALL) $(SCRIPT_LIB) $(OTHER_PROGRAMS) GIT-BUILD-OPTIONS`：编译主程序 `git`、`scalar`、各 builtin 命令、shell/perl 脚本，并生成 `GIT-BUILD-OPTIONS` 记录。
   - 子目录 `git-gui` / `gitk-git` / `templates`：递归 `make` 进子目录。
   - 若 `LINK_FUZZ_PROGRAMS` 定义，则构建 fuzz 程序。
4. 最终产物：可执行文件 `git`（默认无扩展名，Windows 下为 `git.exe`，即 `git$X`）、`scalar`、一系列 `git-<cmd>` 硬链接/拷贝、shell 与 perl 脚本、模板目录 `templates/blt`。

伪代码：

```
all::  shell_compatibility_test
all::  $(ALL_COMMANDS_TO_INSTALL) $(SCRIPT_LIB) $(OTHER_PROGRAMS) GIT-BUILD-OPTIONS
all::  子目录 git-gui / gitk-git / templates
```

#### 4.1.3 源码精读

默认目标就在文件最顶部：

[Makefile:L2-L2](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L2-L2) —— `all::` 是 `make` 无参数时执行的默认目标，双冒号表示可被后续多条 `all::` 累加。

紧接着引入共享行为：

[Makefile:L5-L5](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L5-L5) —— `include shared.mak` 把跨子目录共享的 make 行为（去隐式规则、`.DELETE_ON_ERROR`、静默前缀）拉进来，本讲 4.3 节会用到它里面的 `version_gen` 宏。

安装路径的默认值集中在一处，理解它们就理解了 `make install` 会把文件放到哪：

[Makefile:L629-L634](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L629-L634) —— 默认 `prefix=$(HOME)`，所以 `bindir` 默认是 `~/bin`、`gitexecdir` 是 `~/libexec/git-core`。这就是为什么 INSTALL 说「默认装到你自己的 `~/bin/`」。要全局安装就 `make prefix=/usr`。

默认编译器与工具：

[Makefile:L660-L661](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L660-L661) —— 默认 `CC = cc`、`AR = ar`，可被 `config.mak` 或命令行覆盖。

哪些东西要被构建出来，由三个对象列表决定：

[Makefile:L686-L700](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L686-L700) —— `BUILTIN_OBJS`（各子命令的 `.o`）、`LIB_OBJS`（核心库 `.o`）、`OTHER_PROGRAMS`（非 builtin 的顶层程序）三个列表是构建清单的核心。

[Makefile:L911-L912](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L911-L912) —— `git` 与 `scalar` 两个主可执行文件被加入 `OTHER_PROGRAMS`。

把上面这些汇总成「默认目标真正要构建的东西」：

[Makefile:L2581-L2581](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L2581-L2581) —— 这是 `all::` 最关键的一条依赖：构建所有要安装的命令、脚本库、`git`/`scalar`，以及记录编译选项的 `GIT-BUILD-OPTIONS`。

[Makefile:L2586-L2591](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L2586-L2591) —— `all::` 还会递归进入 `git-gui`、`gitk-git`、`templates` 三个子目录构建（后者产出 `templates/blt`，仓库初始化时要复制它，详见 u2-l3）。

主可执行文件的链接规则：

[Makefile:L2666-L2668](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L2666-L2668) —— `git` 可执行文件由 `git.o`、所有 `$(BUILTIN_OBJS)`、`$(GITLIBS)`（核心静态库）链接而成。这就是为什么你改一个 builtin 的 `.c`，只需要重编译那一个 `.o` 再重新链接。

而那些 `git-status`、`git-show` 等 dashed 命令并不是独立二进制，而是 `git` 的硬链接/拷贝：

[Makefile:L2691-L2693](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L2691-L2693) —— `$(BUILT_INS)` 通过 `ln` 硬链接到 `git`，失败则退回拷贝。运行时 `git` 根据 `argv[0]`（即被调用时的名字）判断要执行哪个子命令。

> 给最终用户看的简明说明在 [INSTALL:L4-L9](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/INSTALL#L4-L9)：`make` 后 `make install`，默认装到 `~/bin`，全局装用 `prefix=/usr`。

#### 4.1.4 代码实践

**实践目标**：不安装，直接在源码树里跑起来刚编译的 git，验证「产物」确实来自本地源码。

**操作步骤**：

1. 在项目根目录执行 `make`（若机器缺库，见 4.2 节关掉可选依赖；zlib 是必需的，见 [INSTALL:L115-L115](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/INSTALL#L115-L115)）。
2. 编译完成后，**不要** `make install`，改为使用构建目录里的包装脚本：`./bin-wrappers/git --version`。INSTALL 推荐这种方式，见 [INSTALL:L73-L77](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/INSTALL#L73-L77)。
3. 想看每条编译命令的完整内容（而非静默的 `CC xxx`），用 `make V=1`。静默前缀定义在 [shared.mak:L46-L97](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/shared.mak#L46-L97)。

**需要观察的现象**：

- `make` 输出里出现 `CC`、`LINK`、`GEN` 等前缀行，最后生成无扩展名的 `git` 可执行文件。
- `./bin-wrappers/git --version` 能打印版本号（具体字符串见 4.3 节）。

**预期结果**：`./bin-wrappers/git --version` 输出形如 `git version <某版本串>`。版本串的来源是 4.3 节的主题。

**待本地验证**：不同发行版缺库情况不同，首次 `make` 可能因缺 zlib/openssl 等失败。若失败，先阅读报错，再按 4.2 节用 `NO_*` 关掉对应可选依赖重试。我没有在你的机器上运行过该命令，无法保证一次通过。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `make all; make prefix=/usr install` 在 INSTALL 里被明确警告「不会工作」？

> **答案**：因为 git 的可执行文件会把 `prefix` 派生出的若干路径（`gitexecdir`、模板目录、locale 目录等）**编译进二进制**（见 [Makefile:L2662-L2664](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L2662-L2664) 里的 `-DGIT_HTML_PATH` 等）。`make all` 时用默认 `prefix=$(HOME)` 编译，`install` 时换 `prefix=/usr` 只改了安装路径，二进制里编码的路径仍是 `$(HOME)`，运行时会找不到辅助脚本。

**练习 2**：`git status` 这个命令对应的可执行文件在磁盘上是怎样的存在？

> **答案**：它不是独立二进制，而是 `git` 的硬链接（[Makefile:L2691-L2693](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L2691-L2693)）。运行时 `git` 程序靠 `argv[0]`（即 `git-status`）判断该分发到 `cmd_status`。所以删掉所有 `git-*` 硬链接，只要 `git` 本体在，`git status` 仍可用。

### 4.2 可移植性编译开关

#### 4.2.1 概念说明

git 要在 Linux、macOS、Windows、各种 BSD、Solaris、z/OS 等几十种平台上用同一份源码编译。不同平台的 C 库函数、头文件、系统调用语义都不一样。git 的做法不是在代码里到处写 `#ifdef`，而是**在 Makefile 里用一整套 `NO_*` / `HAVE_*` 开关**把「这台机器有/没有什么」声明出来，源码再根据这些宏条件编译。

这套开关有两类读者：

- **自动检测**：`config.mak.uname` 根据操作系统名自动设好一批开关。
- **手动覆盖**：你在 `config.mak`（本地不发布）或命令行里写 `NO_OPENSSL=YesPlease` 来关掉某个库。

#### 4.2.2 核心流程

```
Makefile 顶部 ~600 行注释，逐条说明每个 NO_*/HAVE_* 开关
        │
        ├─ include config.mak.uname   （按 uname_S 自动设置平台开关）
        ├─ -include config.mak.autogen（./configure 生成，可选）
        ├─ -include config.mak         （你自己的本地设置，不发布）
        └─ ifdef DEVELOPER → include config.mak.dev （开发者严格模式）
```

这些开关最终汇入 `BASIC_CFLAGS`、`BASIC_LDFLAGS`，在编译每个 `.o` 时以 `-DNO_OPENSSL` 这样的形式传给 C 编译器，源码里的 `#ifndef NO_OPENSSL` 据此选择代码路径。同时它们也会增删 `LIB_OBJS`（比如没 OpenSSL 就换成内置的 SHA1 实现）。

可选依赖遵循统一约定：加 `NO_<LIBRARY>=YesPlease` 即可去掉。例如 `NO_CURL`、`NO_EXPAT`、`NO_TCLTK`、`NO_PERL`、`NO_GETTEXT`。

#### 4.2.3 源码精读

Makefile 顶部一大段注释就是开关目录，摘几条典型的：

[Makefile:L43-L44](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L43-L44) —— `NO_OPENSSL`：没有 OpenSSL 时定义，源码会改用其他 SHA1/HTTPS 后端。

[Makefile:L134-L134](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L134-L134) —— `NO_MMAP`：想避免 `mmap` 时定义，体现「连标准系统调用都可被关掉」。

[Makefile:L150-L150](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L150-L150) —— `NO_PTHREADS`：不用 POSIX 线程时定义，会关闭多线程索引预加载等特性。

[Makefile:L166-L172](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L166-L172) —— `CSPRNG_METHOD`：选择安全随机数来源（`arc4random`/`getrandom`/`openssl`/`/dev/urandom`），是跨平台差异的典型例子。

三个 `config.mak*` 文件的引入顺序很关键：

[Makefile:L1042-L1044](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L1042-L1044) —— `config.mak.uname`（强制 include，按平台自动设开关）、`config.mak.autogen`（`./configure` 生成，可选）、`config.mak`（你的本地覆盖，可选）。后者覆盖前者，所以你的 `config.mak` 优先级最高。

[Makefile:L1046-L1048](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L1046-L1048) —— 定义 `DEVELOPER` 时额外引入 `config.mak.dev`，开启更严格的编译警告与开发期检查。这是贡献代码时常用的开关。

最终所有 CFLAGS 汇总成一条：

[Makefile:L1594-L1594](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L1594-L1594) —— `ALL_CFLAGS = $(DEVELOPER_CFLAGS) $(CPPFLAGS) $(CFLAGS) $(CFLAGS_APPEND)`，再在 [Makefile:L2549-L2549](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L2549-L2549) 追加 `$(BASIC_CFLAGS)`（`NO_*` 产生的 `-D` 就在这里）。

可选依赖清单（最终用户视角）在 INSTALL：

[INSTALL:L110-L113](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/INSTALL#L110-L113) —— git 自给自足但依赖少量外部库，缺哪个就用对应的 `NO_<LIBRARY>=YesPlease` 关掉。

> 本地设置放 `config.mak`，注意它**不随源码发布**（`.gitignore` 忽略），名字被保留给本地使用，见 [INSTALL:L169-L171](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/INSTALL#L169-L171)。

#### 4.2.4 代码实践

**实践目标**：用 `config.mak` 关掉一个可选依赖，观察构建行为变化。

**操作步骤**：

1. 在项目根目录创建文件 `config.mak`，写入一行：`NO_TCLTK = YesPlease`（不构建 gitk/git-gui）。
2. 重新 `make`。如果你想确认开关被读到了，可以 `make V=1` 并在输出里查找是否还出现 `git-gui`/`gitk` 子目录构建。
3. 对照 [Makefile:L2586-L2590](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L2586-L2590)：`ifndef NO_TCLTK` 包住了那两条 `$(QUIET_SUBDIR0)git-gui ...`，所以定义 `NO_TCLTK` 后这两条子目录构建会被跳过。
4. 实验完用 `rm config.mak` 清理（该文件本就不入版本库）。

**需要观察的现象**：

- 定义 `NO_TCLTK` 后，`make` 不再进入 `git-gui` / `gitk-git` 子目录。
- `make` 不再要求系统有 `wish`（Tcl/Tk）。

**预期结果**：构建产物里不再有 gitk/git-gui，构建时间略缩短。

**待本地验证**：若你的机器本来就没装 Tcl/Tk，未定义 `NO_TCLTK` 时 `make` 可能在这步报错；定义后即可绕过。

#### 4.2.5 小练习与答案

**练习 1**：`config.mak.uname`、`config.mak.autogen`、`config.mak` 三者引入方式不同（`include` vs `-include`），为什么？

> **答案**：`config.mak.uname` 是项目自带的、必须存在的平台检测文件，用强制 `include`，缺失即报错；后两者是**可选**的本地/自动生成文件，用 `-include`，不存在时静默跳过。这样默认 checkout 不需要你先创建 `config.mak` 也能构建。

**练习 2**：为什么 git 选择「在 Makefile 里用 `NO_*` 开关」而不是「在 C 代码里直接 `#ifdef __linux__`」？

> **答案**：因为同一操作系统上也可能装/没装某个库（比如 OpenSSL），平台宏无法区分库的有无；而 `NO_*` 开关既可由 `config.mak.uname` 按平台设，也可由用户/`./configure` 按实际探测结果覆盖，把「平台特征」和「库可用性」统一成同一套机制，更灵活也更易维护。

### 4.3 版本号生成

#### 4.3.1 概念说明

这是本讲最值得精读的一条链路。上一讲我们说 `version.c` 里有 `git_version_string[]` 常量，它等于一个 `GIT_VERSION` 宏。但 `GIT_VERSION` 这个宏的值从哪来？它不是源码里写死的，而是**每次构建时由 `GIT-VERSION-GEN` 脚本动态算出来**，再写进一个生成的头文件 `version-def.h`，最后被 `version.c` 包含。

这条链路同时服务两个目的：

1. 让开发版能自动带上「自上次发版以来第几个提交」的信息（通过 `git describe`）。
2. 让发布 tarball（不含 `.git` 目录）也能有正确的版本号（通过打包时预置的 `version` 文件）。

理解了它，你就能回答：为什么同一份源码，在 git checkout 里编译和在解压的 tarball 里编译，`git --version` 的输出可能不同。

#### 4.3.2 核心流程

完整链路（从敲 `make` 到 `git --version` 打印）：

```
make 触发 version-def.h 目标
        │
        ▼
Makefile 调用 version_gen 宏（定义在 shared.mak）
        │  实质执行：GIT-VERSION-GEN <源码目录> <version-def.h.in> <version-def.h>
        ▼
GIT-VERSION-GEN 解析版本号（三选一）：
   ① 优先读源码目录里的 version 文件（发布 tarball 预置）
   ② 否则用 git describe --match="v[0-9]*"（开发 checkout）
   ③ 都没有则用 DEF_VER=v2.55.GIT 兜底
        │  并算出 GIT_BUILT_FROM_COMMIT / GIT_DATE / GIT_USER_AGENT
        ▼
GIT-VERSION-GEN 用 sed 把 version-def.h.in 里的占位符替换成真实值
   @GIT_VERSION@ @GIT_BUILT_FROM_COMMIT@ @GIT_USER_AGENT@ @GIT_DATE@ …
        ▼
生成 version-def.h：
   #define GIT_VERSION "v2.55.0.N.gXXXXXXX"   （举例）
        ▼
version.c 包含 version-def.h，定义：
   const char git_version_string[] = GIT_VERSION;
        ▼
git --version  →  help.c 里 strbuf_addf(buf, "git version %s\n", git_version_string)
```

注意：版本号的「真实值」是构建期决定的，源码里只有占位符模板。这也是为什么修改源码不会改变版本号，而打新 tag 会。

> 另有一条**并行的旧链路**：`GIT-VERSION-FILE`（生成自 `GIT-VERSION-FILE.in`）被 `-include` 进 Makefile，把 `GIT_VERSION` 注入成 **make 变量**，供 Makefile 自身使用（如 `GIT_VERSION_OVERRIDE`）。它和 `version-def.h` 共用同一个 `GIT-VERSION-GEN` 脚本，只是输出模板不同。

#### 4.3.3 源码精读

先看兜底默认值，这是版本号的最终回退：

[GIT-VERSION-GEN:L3-L3](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/GIT-VERSION-GEN#L3-L3) —— `DEF_VER=v2.55.GIT`。当既没有 `version` 文件也无法 `git describe` 时，版本号就是它。

三选一的解析逻辑：

[GIT-VERSION-GEN:L38-L61](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/GIT-VERSION-GEN#L38-L61) —— 整段版本解析。`GIT_VERSION` 环境变量优先；否则依次尝试 `version` 文件、`git describe`、`DEF_VER`。

[GIT-VERSION-GEN:L42-L44](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/GIT-VERSION-GEN#L42-L44) —— ① 发布 tarball 里预置的 `version` 文件，直接读取。

[GIT-VERSION-GEN:L45-L55](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/GIT-VERSION-GEN#L45-L55) —— ② 开发 checkout 用 `git describe --dirty --match="v[0-9]*"`，把 tag 之后多了几个提交算出来，再用 `sed 's/-/./g'` 把连字符换成点（因为版本号里不允许出现 `-`）。

[GIT-VERSION-GEN:L56-L58](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/GIT-VERSION-GEN#L56-L58) —— ③ 兜底用 `DEF_VER`。

[GIT-VERSION-GEN:L60-L60](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/GIT-VERSION-GEN#L60-L60) —— 用 `expr` 去掉开头的 `v`，得到纯版本号（`GIT_VERSION` 宏值本身不含前导 `v` 之外的符号，但 `v` 保留）。

除版本号外还顺带算出几个伴生信息：

[GIT-VERSION-GEN:L63-L71](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/GIT-VERSION-GEN#L63-L71) —— `GIT_BUILT_FROM_COMMIT`（当前 HEAD 的完整哈希）与 `GIT_DATE`（提交日期），都来自 git。这就是 `git version --build-options` 能显示「built from commit …」的来源。

[GIT-VERSION-GEN:L73-L76](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/GIT-VERSION-GEN#L73-L76) —— `GIT_USER_AGENT` 默认为 `git/$GIT_VERSION`，用于 HTTP 协议握手时自我标识（见 u11 传输协议）。

把版本号拆成主/次/修订/补丁级，供需要数值比较的地方用：

[GIT-VERSION-GEN:L81-L83](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/GIT-VERSION-GEN#L81-L83) —— 把 `GIT_VERSION` 按非数字/非字母切分，读出 `GIT_MAJOR_VERSION` 等四个数值。

核心的占位符替换：

[GIT-VERSION-GEN:L85-L93](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/GIT-VERSION-GEN#L85-L93) —— 用 `sed` 把模板里的 `@GIT_VERSION@`、`@GIT_BUILT_FROM_COMMIT@`、`@GIT_USER_AGENT@`、`@GIT_DATE@` 等全部替换成真实值。这正是「模板 + 脚本生成」模式的关键一步。

原子写文件，避免并发构建损坏：

[GIT-VERSION-GEN:L95-L106](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/GIT-VERSION-GEN#L95-L106) —— 先写到 `输出.$$+` 临时文件，仅在内容变化时才 `mv` 覆盖目标，否则删掉临时文件。这样即使重复 `make`，`version-def.h` 内容不变就不会触发下游 `version.o` 重编译。

模板长什么样：

[version-def.h.in:L4-L6](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/version-def.h.in#L4-L6) —— 三个 `#define` 占位符，替换后变成真正的 C 宏定义。

Makefile 里生成 `version-def.h` 的规则：

[Makefile:L2686-L2687](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L2686-L2687) —— 目标 `version-def.h` 依赖模板 `version-def.h.in`、脚本 `GIT-VERSION-GEN`、以及 `GIT-VERSION-FILE`、`GIT-USER-AGENT`；命令调用 `version_gen` 宏。

`version_gen` 宏定义在共享文件里：

[shared.mak:L123-L132](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/shared.mak#L123-L132) —— 它把 `GIT_BUILT_FROM_COMMIT`、`GIT_DATE`、`GIT_USER_AGENT`、`GIT_VERSION_OVERRIDE` 作为环境变量传给 `GIT-VERSION-GEN`，再调用脚本。其中 `GIT_VERSION_OVERRIDE` 来自 make 变量 `GIT_VERSION`，用来允许外部强制指定版本。

`version.o` 依赖生成的头文件：

[Makefile:L2689-L2689](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L2689-L2689) —— `version.sp version.s version.o: version-def.h`，即 `version-def.h` 变化会触发 `version.o` 重编译。

C 侧如何使用：

[version.c:L6-L10](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/version.c#L6-L10) —— `version.c` 包含 `version-def.h`（当未定义 `GIT_VERSION_H` 时）。

[version.c:L12-L13](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/version.c#L12-L13) —— `const char git_version_string[] = GIT_VERSION;` 与 `git_built_from_commit_string[]`。这两个常量被链接进 `git` 二进制。

最后是输出端：

[help.c:L786-L786](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/help.c#L786-L786) —— `git --version` 最终走 `strbuf_addf(buf, "git version %s\n", git_version_string)`。至此闭环：`GIT-VERSION-GEN` 算出的字符串 → `version-def.h` 宏 → `version.c` 常量 → `git --version` 输出。

> 并行的旧链路：[Makefile:L1568-L1578](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L1568-L1578) 生成 `GIT-VERSION-FILE`（模板 [GIT-VERSION-FILE.in:L1-L1](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/GIT-VERSION-FILE.in#L1-L1) 只有 `GIT_VERSION=@GIT_VERSION@`），再 `-include` 进 Makefile，把 `GIT_VERSION` 注入成 make 变量，供 [Makefile:L1577-L1577](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L1577-L1577) 的 `GIT_VERSION_OVERRIDE` 使用。

#### 4.3.4 代码实践

**实践目标**：亲手验证版本号是从 `GIT-VERSION-GEN` 算出来的，并观察修改默认前缀的效果。

**操作步骤**：

1. 在项目根目录直接运行脚本，看它会算出什么（不写文件）：
   ```sh
   ./GIT-VERSION-GEN . --format='@GIT_VERSION@'
   ```
   这会走 ② `git describe` 分支，打印当前 checkout 的版本串。
2. 运行 `./bin-wrappers/git --version`（前提是已 `make`），对比两者里的版本号是否一致。
3. **观察默认前缀的效果**（本步骤会临时改源码，请最后还原）：把 [GIT-VERSION-GEN:L3](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/GIT-VERSION-GEN#L3) 的 `DEF_VER=v2.55.GIT` 临时改成 `DEF_VER=v9.99.GIT`。然后**强制走兜底分支**来验证它生效：
   ```sh
   # 临时让 git describe 失败：把 .git 目录名改掉片刻
   mv .git .git_bak
   ./GIT-VERSION-GEN . --format='@GIT_VERSION@'   # 此时应输出 v9.99.GIT
   mv .git_bak .git
   ```
4. 还原脚本：`git checkout -- GIT-VERSION-GEN`。

**需要观察的现象**：

- 步骤 1 打印一个形如 `v2.55.0.N.gXXXXXXX` 的串（开发 checkout，由 `git describe` 产生），或带 `-dirty` 后缀（若有未提交改动）。
- 步骤 2 的 `git --version` 版本号与步骤 1 一致。
- 步骤 3 在 `.git` 不可见时打印 `v9.99.GIT`，证明兜底分支确实用了 `DEF_VER`。

**预期结果**：能清晰看到「版本号 = git describe 优先，DEF_VER 兜底」的三选一逻辑。

**待本地验证**：步骤 1 的具体串取决于你 checkout 的 tag 与提交数，我无法预知确切值；若当前 HEAD 恰好正打 tag，则 `git describe` 只输出 tag 本身（如 `v2.55.0`）而无后缀。步骤 3 改名 `.git` 时请确保没有别的进程在用该仓库，操作完立即还原。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `GIT-VERSION-GEN` 在写 `version-def.h` 时要先写临时文件再 `mv`，并且只在内容变化时才覆盖？

> **答案**：两点好处。一是**原子性**：`mv` 是原子的，并发 `make` 或中途崩溃不会留下半个文件。二是**避免无谓重编译**：若内容没变就不覆盖目标，`version-def.h` 的 mtime 不变，make 就不会因此重编译 `version.o` 再重新链接 `git`，节省构建时间。见 [GIT-VERSION-GEN:L95-L106](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/GIT-VERSION-GEN#L95-L106)。

**练习 2**：发布 tarball（解压后没有 `.git` 目录）和 git checkout 编译出的 `git --version` 会一样吗？

> **答案**：通常不一样。tarball 里打包时预置了 `version` 文件（如内容为 `v2.55.0`），脚本走 ① 分支读到固定串；而 checkout 走 ② `git describe`，会带上「自 tag 以来第 N 个提交」的后缀。所以同一份源码两种分发方式版本号不同，这是设计预期。见 [GIT-VERSION-GEN:L42-L58](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/GIT-VERSION-GEN#L42-L58)。

**练习 3**：`GIT_VERSION_OVERRIDE` 这个变量存在的意义是什么？

> **答案**：它让外部能在不修改 `GIT-VERSION-GEN`、不影响 `git describe` 结果的前提下，强制指定一个版本号注入构建。`version_gen` 宏把它作为 `GIT_VERSION` 环境变量传给脚本（[shared.mak:L130-L130](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/shared.mak#L130-L130)），而脚本里 `if test -z "$GIT_VERSION"` 只在它为空时才去解析（[GIT-VERSION-GEN:L38-L39](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/GIT-VERSION-GEN#L38-L39)）。发行版打包时常用它固定版本号。

## 5. 综合实践

把本讲三个模块串起来，完成一次「带自定义版本号的本地构建」：

1. **看默认行为**：在干净源码树执行 `make V=1`，观察输出里的 `GEN version-def.h`、`CC version.o`、`LINK git` 三类命令，确认它们对应 4.1 / 4.3 描述的链路。
2. **改版本号**：用 `GIT_VERSION_OVERRIDE` 在命令行注入一个自定义版本号重新构建：
   ```sh
   make GIT_VERSION_OVERRIDE=v9.99-mybuild
   ```
   预期 `version-def.h` 被改写、`version.o` 重编译、`git` 重新链接。
3. **验证注入**：运行 `./bin-wrappers/git --version`，应输出 `git version v9.99-mybuild`。这说明版本号确实经 `GIT-VERSION-GEN` → `version-def.h` → `version.c` 流到了二进制。
4. **加一个可移植性开关**：在同一命令里追加 `NO_TCLTK=YesPlease`，确认 `make` 不再构建 gitk/git-gui（4.2 节）。
5. **还原**：`git checkout -- version-def.h`（若它被纳入忽略则直接 `rm`）并重新 `make` 回到正常版本号。

> 完成后，你应当能向别人解释：敲下 `make` 后，从 Makefile 默认目标、到 `GIT-VERSION-GEN` 算版本号、到链接出 `git`、再到 `git --version` 打印，中间每一环的源码分别在哪个文件的第几行。这一整条「构建期」链路，正是后续所有「运行期」源码阅读的地基。

## 6. 本讲小结

- `make` 默认目标 `all::` 是双冒号累加规则，最终产出 `git`、`scalar`、各 `git-*` 硬链接、脚本与模板；默认装到 `~/bin`，全局装需 `prefix=/usr` 且构建与安装必须用同一 prefix。
- 主可执行文件 `git` 由 `git.o` + `BUILTIN_OBJS` + `GITLIBS` 链接而成（[Makefile:L2666-L2668](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L2666-L2668)）；`git-status` 等只是 `git` 的硬链接，靠 `argv[0]` 分发。
- 跨平台可移植性靠一套 `NO_*` / `HAVE_*` 开关，由 `config.mak.uname` 自动设、`config.mak` 手动覆盖，最终汇入 `BASIC_CFLAGS` 以 `-D` 形式传给编译器。
- 版本号是构建期动态生成的：`GIT-VERSION-GEN` 三选一（`version` 文件 → `git describe` → `DEF_VER`）解析版本，用 `sed` 替换 `version-def.h.in` 占位符生成 `version-def.h`，再被 `version.c` 包含成 `git_version_string` 常量。
- `shared.mak` 提供跨子 Makefile 的共享行为：去隐式规则、`.DELETE_ON_ERROR`、`V=1` 静默前缀，以及封装 `GIT-VERSION-GEN` 调用的 `version_gen` 宏。
- 不安装也能用：`./bin-wrappers/git` 直接跑构建产物，适合源码学习阶段（[INSTALL:L73-L77](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/INSTALL#L73-L77)）。

## 7. 下一步学习建议

你已经能把 git 编译出来并理解了版本号链路。下一步建议：

- 进入 **u1-l3 源码目录结构地图**：构建产物（`builtin/*.o`、`templates/`、脚本）对应源码树的哪些目录，为后续阅读 `git.c` 命令分发做准备。
- 之后学 **u1-l4 命令分发主入口 git.c**：本讲提到的「`git-status` 靠 `argv[0]` 分发到 `cmd_status`」正是 `git.c` 里 `commands[]` 表的工作，那里会讲清楚硬链接如何变成子命令。
- 若你对构建细节感兴趣，可顺带阅读 `config.mak.uname` 看你的平台被自动设了哪些开关，以及 `Documentation/Makefile`（文档单独构建，`make doc` 默认不跑，见 [INSTALL:L173-L176](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/INSTALL#L173-L176)）。
- 本讲涉及的 `trace2` 在 u13-l3 会深入；`GIT_USER_AGENT` 在 u11 传输协议会再次出现——记住版本号链路同时为网络握手供给了自我标识字符串。
