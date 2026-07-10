# 源码目录结构地图

## 1. 本讲目标

上一讲我们把 `git` 从源码编译了出来，并理解了版本号是如何被注入二进制的。本讲不再讨论编译细节，而是带你在动手编译过的那棵源码树里「建立地图」。

学完本讲，你应当能够：

1. 说出 git 源码树的顶层目录各自承担什么职责，并能区分**核心库源码**、**builtin 子命令实现**与**辅助脚本**三类文件。
2. 解释 `builtin/*.c` 中的 `cmd_*` 函数是如何被 `Makefile` 转换成 `git-add`、`git-status` 这样的可执行命令的，并理解「命令名不一定等于源码文件名」这一常见陷阱。
3. 读懂 `command-list.txt`，区分 **porcelain（瓷器/高级命令）** 与 **plumbing（管道/底层命令）**，并知道这个分类在哪里被使用。
4. 识别 `refs/`（引用后端）和 `reftable/`（独立子库）这两类与核心库并列、却组织方式不同的代码。
5. 说出 `t/` 测试套件与 `Documentation/` 文档体系的入口位置与组织约定。

---

## 2. 前置知识

在进入源码地图之前，先用通俗语言对齐几个概念。

### 2.1 单二进制 + 命令分发

很多工具会为每个子命令编译一个独立的可执行文件，git 不是。git 编译出来**只有一个主可执行文件 `git`**，外加一堆指向它的**硬链接**（如 `git-add`、`git-status`）。当你敲 `git add` 时，`git` 程序内部查一张命令表，找到对应的 C 函数去执行。

> 这张命令表和分发逻辑在 `git.c` 里，是下一讲（u1-l4）的主题。本讲你只需要知道：**顶层那些 `git-*` 文件绝大多数是同一个二进制的硬链接**，而不是各自独立的程序。

### 2.2 核心库与子命令的分工

git 的 C 源码可以粗略地分成两层：

- **核心库（libgit）**：放在仓库顶层的 `*.c/*.h` 文件，提供对象存储、索引、引用、配置、diff、revision 遍历等底层能力。它们不直接面向用户，而是被链接进 `git` 二进制。
- **子命令（builtin）**：放在 `builtin/` 目录，每个文件实现一个面向用户的命令（`cmd_add`、`cmd_cat_file` ……），调用核心库完成工作。

这种分层意味着：改一个命令的行为通常只动 `builtin/xxx.c`；改底层数据结构才动顶层库文件。

### 2.3 porcelain 与 plumbing

git 社区用了一个水管（plumbing）和瓷器（porcelain）的比喻：

- **porcelain（瓷器）**：用户日常使用的高级命令，如 `add`、`commit`、`checkout`、`log`。
- **plumbing（管道/底层）**：更接近内部机制、供脚本或高级用户使用的命令，如 `cat-file`、`hash-object`、`update-index`、`rev-parse`。

这个分类不是装饰，它写在 `command-list.txt` 里，被 `git help` 的分组显示和 shell 补全脚本读取。

---

## 3. 本讲源码地图

本讲涉及的关键文件与目录如下：

| 路径 | 作用 |
| --- | --- |
| `README.md` | 项目自述，指向安装、教程、邮件列表入口。 |
| `Makefile` | 构建中枢；定义 `BUILTIN_OBJS`/`BUILT_INS`/`LIB_OBJS` 等变量，决定哪些源码被编译、如何拼成命令。 |
| `command-list.txt` | 命令分类登记表；所有命令（含 builtin 与外部脚本）必须在此登记并标注类型。 |
| `Documentation/CodingGuidelines` | 编码规范；说明 C 与 shell 的风格约定，也隐含了项目对「核心库 C 代码 + 脚本命令」双形态的认同。 |
| 顶层 `*.c` / `*.h` | 核心库源码（如 `blob.c`、`commit.c`、`object-file.c`、`refs.c`）。 |
| `builtin/` | 子命令实现目录（130 个 `.c` 文件）。 |
| `refs/` | 引用后端（files / packed / reftable 三种实现 + 缓存与迭代器）。 |
| `reftable/` | 可独立的 reftable 二进制格式子库（带自己的 LICENSE）。 |
| `t/` | 端到端测试套件（约 1098 个 `.sh` 脚本）。 |
| `Documentation/` | 文档源（`.adoc` 命令手册 + 流程文档）。 |

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- 4.1 顶层核心库源码与整体分层
- 4.2 `builtin/` 子命令目录与 `command-list.txt` 分类
- 4.3 `refs/` 与 `reftable/`：引用后端子库
- 4.4 `t/` 测试套件与 `Documentation/` 文档体系

---

### 4.1 顶层核心库源码与整体分层

#### 4.1.1 概念说明

git 源码树的顶层散落着几百个 `.c` 和 `.h` 文件，初看会让人觉得「没有目录组织」。但这正是 git 的有意设计：**核心库以「扁平文件 + 文件名前缀」的方式组织**，文件名本身就承担了模块边界的职责。例如：

- `blob.c` / `commit.c` / `tree.c` / `tag.c`：四种对象类型的实现。
- `object-file.c`：对象的内容哈希与存储。
- `read-cache.c`：索引（index）的读写。
- `refs.c`：引用的统一抽象层。
- `config.c`：配置解析。
- `revision.c`：提交遍历。
- `diff.c` / `diffcore-*.c`：差异引擎。

围绕这些扁平文件，git 还把若干「可复用子系统」单独放进子目录，它们与顶层库是**并列**而非嵌套关系：

| 子目录 | 职责 |
| --- | --- |
| `xdiff/` | 行级差异算法（Myers / patience / histogram），本可独立编译。 |
| `ewah/` | 压缩位图（用于 pack-bitmap）。 |
| `block-sha1/` | git 自带的 SHA-1 实现。 |
| `odb/` | 对象数据库的来源抽象（loose / packed / inmemory）。 |
| `negotiator/` | fetch 协商策略（default / skipping / noop）。 |
| `compat/` | 平台可移植性垫片（mingw / msvc / 终端 / mmap 等）。 |

#### 4.1.2 核心流程

编译时，这些顶层与子目录的源码会被汇总成两个关键列表：

1. `LIB_OBJS`：所有核心库 `.o` 文件，最终链接成静态库 `libgit.a`。
2. `LIB_H`：所有公开头文件，用于依赖追踪（改了 `.h` 就重新编译引用它的 `.c`）。

主可执行文件 `git` 由 `git.o` + `BUILTIN_OBJS` + `libgit.a` 链接而成。换句话说：**核心库提供能力，builtin 提供命令，二者拼装出单一二进制**。

#### 4.1.3 源码精读

`Makefile` 在一块「守卫环境变量」的区域里集中声明了所有构件清单变量：

[Makefile:685-715](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L685-L715) —— 这段定义了 `BUILTIN_OBJS`、`BUILT_INS`、`LIB_OBJS`、`XDIFF_OBJS`、`SCRIPT_SH`、`SCRIPT_PERL` 等构件变量，是把源码树「拼装成可执行文件」的总清单。注释 `# Guard against environment variables` 说明这些变量被显式清空，是为了防止用户 shell 里残留的同名变量污染构建。

随后 `LIB_OBJS` 逐行列出核心库源码，例如：

[Makefile:1090-1104](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L1090-L1104) —— 这里把 `abspath.o`、`alloc.o`、`blob.o` 等顶层 `.o` 逐行加入 `LIB_OBJS`；你能在文件名与顶层目录的 `.c` 之间建立一一对应（例如 `blob.o` 对应顶层 `blob.c`，`commit.o`、`object-file.o` 出现在更靠下的 1119 行与 1213 行）。继续往下还能看到 `refs/files-backend.o`、`reftable/stack.o`、`xdiff/`（通过 `XDIFF_OBJS`）等子目录构件。

头文件清单则用一个发现机制自动收集：

[Makefile:1088](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L1088) —— `LIB_H = $(FOUND_H_SOURCES)`，表示公开头文件不是手写清单，而是由构建系统扫描得来，避免「新增了 `.h` 却忘了登记」的疏漏。

#### 4.1.4 代码实践

**实践目标**：确认「顶层 `.c` 文件 ≈ 核心库源码」这一对应关系。

**操作步骤**：

1. 在仓库根目录运行 `ls *.c | wc -l`，统计顶层 C 源码数量。
2. 在 `Makefile` 中检索 `LIB_OBJS += blob.o`、`LIB_OBJS += commit.o`、`LIB_OBJS += object-file.o`，确认它们都被收录。
3. 任选一个顶层 `.c`（如 `blob.c`），用编辑器打开它的同名 `.h`（`blob.h`），观察「类型 + 操作」是如何成对组织的。

**需要观察的现象**：顶层 `.c` 文件数量较多（两百多个），且绝大多数都能在 `LIB_OBJS` 列表里找到同名条目；少数找不到的，往往是只被某个 builtin 直接包含、或通过别的 `.o` 间接编译的文件。

**预期结果**：你能对着 `Makefile` 的 `LIB_OBJS` 列表，把顶层目录里任意一个核心库 `.c` 「点名」。若某个 `.c` 在 `LIB_OBJS` 里查不到，请记录下来——下一讲我们会看到有些命令实现也藏在非同名文件里。

#### 4.1.5 小练习与答案

**练习 1**：`xdiff/` 目录里的源码（如 `xdiffi.c`）为什么不直接放在顶层？

> **参考答案**：`xdiff` 是一个相对独立、可单独维护的行级差异库（最早源自 xemacs 的 xdiff），有自己的接口边界。放进子目录便于把它当作「内嵌的第三方库」对待，通过 `XDIFF_OBJS` 单独汇总，而不是与 git 核心库的扁平文件混在一起。

**练习 2**：为什么 `LIB_H` 用 `$(FOUND_H_SOURCES)` 自动扫描，而不是手写一份头文件清单？

> **参考答案**：手写清单容易在新增/删除头文件时遗漏，导致依赖关系失效（改了 `.h` 却不重编引用者）。自动扫描保证头文件清单与磁盘实际状态一致，是「DRY」原则在构建系统里的体现。

---

### 4.2 `builtin/` 子命令目录与 `command-list.txt` 分类

#### 4.2.1 概念说明

`builtin/` 目录里每一个 `.c` 文件对应一个（或多个）面向用户的命令，文件里导出一个形如 `cmd_<命令名>` 的入口函数。例如：

- `builtin/add.c` → `int cmd_add(...)`
- `builtin/cat-file.c` → `int cmd_cat_file(...)`
- `builtin/checkout.c` → `int cmd_checkout(...)`

`git.c` 里的命令表（下一讲精讲）会把这些函数与命令名一一对应。而 `command-list.txt` 则是另一份「登记簿」：它记录**所有命令的名字 + 分类**，用于 `git help` 分组、man 手册生成和 shell 补全。新增一个命令时，必须同时改 `builtin/`、`git.c` 命令表和 `command-list.txt`。

#### 4.2.2 核心流程

从「一个 builtin 源码文件」到「一个 `git-xxx` 命令」，链路是这样的：

```
builtin/add.c  ──编译──▶  builtin/add.o  ──加入──▶  BUILTIN_OBJS
                                                        │
                                         Makefile 用 patsubst 把
                                         builtin/add.o 转成 git-add
                                                        ▼
                                                  git-add（硬链接到 git）
```

关键转换在 `Makefile` 的一行模式替换里完成。但有一类**例外**：有些命令名并没有同名的 `builtin/xxx.c`——它的 `cmd_*` 函数住在另一个 builtin 文件里。最典型的就是 `git status`：它的实现 `cmd_status` 并不在 `builtin/status.c`（该文件不存在），而在 `builtin/commit.c`，因为 status 与 commit 共享大量暂存区逻辑。

#### 4.2.3 源码精读

先看 `Makefile` 如何把 `builtin/*.o` 批量转成命令名：

[Makefile:888-890](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L888-L890) —— 注释解释了「例外」机制：`BUILT_INS += $(patsubst builtin/%.o,git-%$X,$(BUILTIN_OBJS))` 这行把每个 `builtin/xxx.o` 替换成 `git-xxx`。`$X` 是平台可执行后缀（Windows 上是 `.exe`，类 Unix 上为空）。

紧接着的若干行就登记了那些「没有同名源码文件」的命令，例如 `git-status`：

[Makefile:892-907](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L892-L907) —— 这里手动列出 `git-init`、`git-status`、`git-show`、`git-switch` 等，注释里那句「whose implementation `cmd_$C()` is not in `builtin/$C.o` but is linked in as part of some other command」正是解释它们的原因。

我们用源码验证 `git status` 的真实落点：

[builtin/commit.c:1537](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/commit.c#L1537) —— `int cmd_status(int argc, ...)`，这就是 `git status` 的真正入口，住在 `commit.c` 里。

对比一下「同名」的常规命令：

[builtin/add.c:382](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/add.c#L382) —— `int cmd_add(int argc, ...)`，`git add` 的入口，文件名与命令名一致，属于 patsubst 自动覆盖的常规情况。

再看 `command-list.txt` 如何登记这两类命令。文件顶部是一段说明分类属性的注释：

[command-list.txt:9-18](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/command-list.txt#L9-L18) —— 列出所有命令类型：`mainporcelain`（主瓷器）、`plumbingmanipulators` / `plumbinginterrogators`（底层操作/查询）、`ancillary*`（辅助）、`foreignscminterface`（外来 SCM 接口）、`synchingrepositories` / `synchelpers`（同步）、`purehelpers`（纯辅助脚本）。

登记表正文从一行标记之后开始：

[command-list.txt:55-57](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/command-list.txt#L55-L57) —— `### command list (do not change this line)` 是机器识别的分界，下一行是列说明，随后 `git-add  mainporcelain  worktree` 就是第一条登记：命令名 + 主类型 + 可选子分组（`worktree` 表示它属于工作树相关常用命令）。

一个 plumbing 命令的例子：

[command-list.txt:69](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/command-list.txt#L69) —— `git-cat-file  plumbinginterrogators`，`cat-file` 被归类为底层查询命令，它直接读取对象数据库，是典型的 plumbing。

#### 4.2.4 代码实践

**实践目标**：亲手验证「命令名 ≠ 源码文件名」这一陷阱。

**操作步骤**：

1. 在 `command-list.txt` 里找出一个 porcelain 命令（如第 57 行的 `git-add`）和一个 plumbing 命令（如第 69 行的 `git-cat-file`）。
2. 对每个命令，到 `builtin/` 目录下查找同名的 `.c`：`builtin/add.c` 与 `builtin/cat-file.c` 是否都存在？
3. 现在挑 `git-status`（`command-list.txt` 第 192 行 `git-status  mainporcelain  info`），到 `builtin/` 下找 `status.c`——你会发现**没有这个文件**。
4. 用文本搜索在 `builtin/` 中定位 `cmd_status`，确认它住在 `builtin/commit.c`。
5. 回到 `Makefile` 第 903 行附近，确认 `BUILT_INS += git-status$X` 正是手动登记它的那一行。

**需要观察的现象**：大多数命令满足「`builtin/<name>.c` 存在 → 自动生成 `git-<name>`」；但 `status`、`show`、`switch`、`init` 等少数命令的实现藏在别的 builtin 里，需要手动登记。

**预期结果**：你能复述这条规则——**若 `builtin/<name>.c` 存在则命令自动产生；否则必须在 `Makefile` 的 `BUILT_INS` 手动追加，并在 `command-list.txt` 登记**。这是一个新命令接入 git 时容易踩的坑。

#### 4.2.5 小练习与答案

**练习 1**：`git-show` 与 `git-log` 共享代码很多。请猜测 `cmd_show` 住在哪个 builtin 文件里，并说明验证方法。

> **参考答案**：`cmd_show` 住在 `builtin/log.c`（`git log` 的实现文件），因为 `show` 本质上是「展示单个对象的 log」。验证：在 `builtin/` 下不存在 `show.c`，且 `Makefile` 在第 901 行手动登记了 `git-show`。可在 `builtin/log.c` 中搜索 `int cmd_show` 确认。

**练习 2**：`command-list.txt` 里某条命令的第二个字段除了主类型，还能跟一个词（如 `git-add` 后的 `worktree`）。这个额外字段的作用是什么？

> **参考答案**：它是 `mainporcelain` 命令的可选「常见分组」标签（`init` / `worktree` / `info` / `history` / `remote`），用于把最常用的瓷器命令再分到 `git help` 输出的小组里，方便用户浏览。非常用瓷器命令不得带这些标签。

**练习 3**：为什么 git 要把 `status` 和 `commit` 放在同一个 `builtin/commit.c` 里？

> **参考答案**：`git status` 与 `git commit` 都需要把「HEAD / 索引 / 工作树」三层对比一遍（status 用来展示、commit 用来决定提交什么），共享大量暂存区收集与差异比对代码。放在同一文件可以共享这些内部辅助函数，避免重复实现与逻辑漂移。

---

### 4.3 `refs/` 与 `reftable/`：引用后端子库

#### 4.3.1 概念说明

**引用（ref）** 是 git 给对象起的「名字指针」，例如 `refs/heads/master` 指向某个 commit。引用的存储格式不止一种，git 把「读写引用」抽象成统一的 `ref_store` 接口，再为不同存储格式提供**后端（backend）**实现。这些后端代码集中在 `refs/` 目录：

| 文件 | 后端 / 职责 |
| --- | --- |
| `refs/files-backend.c` | 传统 files 后端：松散文件 `.git/refs/...` + `.git/packed-refs`。 |
| `refs/packed-backend.c` | 单一 `packed-refs` 文件后端。 |
| `refs/reftable-backend.c` | reftable 后端：把 `reftable/` 子库接到 `ref_store` 接口。 |
| `refs/ref-cache.c` | 引用的内存缓存。 |
| `refs/iterator.c` | 统一的 ref 遍历迭代器。 |
| `refs/debug.c` | 调试埋点。 |
| `refs/refs-internal.h` | 后端内部约定。 |

而 `reftable/` 是另一回事：它是一个**可独立使用的二进制格式库**，定义了「把引用存成可追加的块状二进制文件」这一存储格式，甚至自带 `LICENSE`，风格上像一个内嵌的第三方项目。

#### 4.3.2 核心流程

一次引用读写的概念路径：

```
用户命令 git update-ref / git for-each-ref
            │
            ▼
       refs.c  ──公共 API（与后端无关）──▶  ref_store 抽象接口
            │
   ┌────────┼─────────────┐
   ▼        ▼             ▼
files-backend  packed-backend  reftable-backend
                                   │
                                   ▼
                            reftable/  （二进制格式实现：stack/table/record/writer …）
```

也就是说，`refs/` 负责「适配」不同后端到统一接口；`reftable/` 只负责「reftable 这一种磁盘格式」的读写。换后端不影响上层命令，这就是抽象层存在的价值。

#### 4.3.3 源码精读

`refs/` 目录的文件构成（按 ls 结果）：

`debug.c`、`files-backend.c`、`iterator.c`、`packed-backend.c`、`packed-backend.h`、`ref-cache.c`、`ref-cache.h`、`refs-internal.h`、`reftable-backend.c` —— 共 9 个文件，覆盖三种后端 + 缓存 + 迭代器 + 调试 + 内部头。它们在 `Makefile` 中以 `refs/xxx.o` 的形式加入 `LIB_OBJS`：

[Makefile:1269-1274](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L1269-L1274) —— 这里把 `refs/debug.o`、`refs/files-backend.o`、`refs/reftable-backend.o`、`refs/iterator.o`、`refs/packed-backend.o`、`refs/ref-cache.o` 全部纳入核心库。

`reftable/` 则自成一体，包含 `LICENSE`、`stack.c`、`table.c`、`record.c`、`writer.c`、`block.c`、`merged.c`、`iter.c`、`pq.c`、`tree.c`、`basics.c`、`system.c`、`error.c`、`fsck.c` 等文件，以及大量 `reftable-*.h` 公开头。它同样在 `Makefile` 里集中登记：

[Makefile:1276-1289](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L1276-L1289) —— 这里列出 `reftable/basics.o`、`reftable/stack.o`、`reftable/table.o`、`reftable/record.o`、`reftable/writer.o` 等，可见 reftable 是作为一个完整子库整体接入的。

> 提示：reftable 的源码精读留到第 5 单元（u5 引用）讲；本讲你只需要记住**「`refs/` 是后端适配层，`reftable/` 是可独立格式库」**这一定位差异。

#### 4.3.4 代码实践

**实践目标**：在磁盘上看到「files 后端」与「reftable 后端」的不同布局。

**操作步骤**：

1. 在一个普通仓库里查看 `ls .git/refs` 与 `cat .git/packed-refs`（若存在），这是 **files 后端**的磁盘形态：松散目录文件 + 一个打包文件。
2. 阅读 `refs/files-backend.c` 的开头注释与 `refs/packed-backend.c` 的开头注释，对照你看到的磁盘文件。
3. 若你的 git 已支持 reftable，可用 `git init --ref-format=reftable rtable` 创建一个 reftable 仓库，再查看 `.git/refs/` 下出现的二进制表文件（如 `refs/heads/...` 不再是文本文件，而是 reftable 块）。
4. 对比两种仓库的 `.git/refs` 目录，体会「换后端 = 换存储格式，但上层命令不变」。

**需要观察的现象**：files 后端的 ref 是人眼可读的「40/64 位哈希」文本文件；reftable 后端的文件则是二进制块，无法用 `cat` 直接阅读。

**预期结果**：你能指出 `.git/refs` 下哪些文件属于 files 后端，并说出 reftable 后端「为什么需要 `reftable/` 这样一个独立库」——因为它要实现一套完整的二进制索引格式，远比写文本文件复杂。若环境不支持 reftable，请明确写「待本地验证」并只完成第 1、2 步。

#### 4.3.5 小练习与答案

**练习 1**：`refs/reftable-backend.c` 与 `reftable/stack.c` 的分工是什么？

> **参考答案**：`refs/reftable-backend.c` 是「适配器」，把 reftable 格式的读写翻译成 `ref_store` 统一接口所要求的操作；`reftable/stack.c` 是 reftable 库内部负责「管理一摞可追加的二进制表文件（stack）」的实现，不知道 git 的 `ref_store` 存在。前者依赖后者，反之不成立。

**练习 2**：为什么 `reftable/` 目录里会有一个独立的 `LICENSE` 文件？

> **参考答案**：reftable 起初是作为「可被 git 之外的项目复用的引用存储格式」来设计的，带有自己的许可声明，便于社区把它当作半独立的子项目对待。这与 `xdiff/` 作为内嵌差异库的处理方式类似——都体现了 git 对「可复用子系统」的隔离意识。

---

### 4.4 `t/` 测试套件与 `Documentation/` 文档体系

#### 4.4.1 概念说明

git 的质量保证高度依赖两套「源码化」的资产：

- **`t/` 测试套件**：约 1098 个 `.sh` 脚本，每个脚本是一组端到端测试——真正跑起编译好的 `git`，在临时沙箱里建仓库、执行命令、断言结果。git 的测试不是 C 单元测试为主，而是 shell 端到端为主。
- **`Documentation/` 文档源**：约 288 个条目，绝大多数是 `.adoc`（AsciiDoc）源文件，每个命令一份 `git-<command>.adoc`，编译后就是 `man git-<command>` 看到的手册。还有 `SubmittingPatches`、`CodingGuidelines`、`MyFirstContribution.adoc` 等流程与入门文档。

理解这两套资产的位置，是后续「改了代码 → 跑测试 → 改文档」工作流的前提。

#### 4.4.2 核心流程

测试的组织遵循「编号 + 主题」：

```
t/t0001-init.sh        ← 编号 t00xx，主题 init
t/t0002-readtree.sh    ← 主题 read-tree
t/lib-pack.sh          ← 共享夹具（被多个测试 source）
t/helper/test-tool     ← C 写的测试辅助程序
t/unit-tests/          ← 较新的 C 单元测试
```

测试号是**稳定契约**：一个测试文件一旦合入，它的编号不再变更，这样失败日志里出现 `t0001` 就能精确定位。

文档的组织则遵循「命令名 + 文件名」：`Documentation/git-add.adoc` 对应 `git add`。`Documentation/cmd-list.sh` 会读取 `command-list.txt` 把命令分到不同 man 章节（`git(1)` 主页、各分类页）。

#### 4.4.3 源码精读

一个真实测试文件的骨架：

[t/t0001-init.sh](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/t/t0001-init.sh) —— 这是 `git init` 的端到端测试。打开它你会看到标准的 `test_expect_success '描述' '命令序列'` 断言块，以及顶部的 `#!/bin/sh` 与 `test_description`。第 15 单元（u15）会专门讲这个框架，本讲只需建立「测试就长这样」的印象。

文档侧的入门指引：

[Documentation/MyFirstContribution.adoc](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/MyFirstContribution.adoc) —— 官方的「首次贡献」教程，手把手带你新增一个 builtin 命令。它会让你同时改 `builtin/`、`Makefile`、`git.c` 命令表与 `command-list.txt`，正好串起本讲 4.2 节的接入流程。

而 `CodingGuidelines` 既管 C 也管 shell，是「核心库 + 脚本命令」双形态的规范源头：

[Documentation/CodingGuidelines:249-282](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/CodingGuidelines#L249-L282) —— C 代码规范：用 tab 缩进（按 8 空格解释）、尽量不超过 80 列、推荐开 `DEVELOPER=1` 编译、且 git 自 v2.35.0 起要求 C99。这些规则解释了为什么顶层 `.c` 与 `builtin/*.c` 看起来风格高度一致。

[Documentation/CodingGuidelines:61-90](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/CodingGuidelines#L61-L90) —— shell 脚本规范：用 tab、`case` 分支与 `case`/`esac` 同级、重定向写成 `>file` 而非 `> file`、用 `$( ... )` 而非反引号。这些规则同时约束着 `t/*.sh` 测试和顶层 shell 脚本（如 `git-sh-setup.sh`）。

#### 4.4.4 代码实践

**实践目标**：跑通一个真实测试，并从文档里找到「新增命令」的官方清单。

**操作步骤**：

1. 确保已完成上一讲的 `make` 编译（包括 `make -C t` 可能需要的测试辅助程序）。
2. 运行 `sh t/t0001-init.sh`（或 `make -C t t0001-init`），观察输出里每个断言的 `ok` / `not ok`。
3. 运行结束后查看该测试生成的临时目录（`t/trash directory.t0001-init`），体会「沙箱仓库」长什么样。
4. 打开 `Documentation/MyFirstContribution.adoc`，搜索其中要求你修改 `Makefile` 与 `command-list.txt` 的段落，把它们抄成一份「新命令接入清单」。

**需要观察的现象**：测试会输出一串 `ok N - description`；若全部通过，结尾给出汇总。`trash` 目录里是一个完整的临时 `.git`，可以看到 `refs/`、`objects/` 等本讲提到的真实磁盘结构。

**预期结果**：你能在不读 C 源码的前提下，仅靠运行 `t0001-init.sh` 就观察到 `git init` 创建的目录结构；并能从 `MyFirstContribution.adoc` 复述新增一个 builtin 命令需要改动的 4 处位置。若编译环境缺少依赖导致测试跑不起来，请写「待本地验证」并改为纯阅读 `t0001-init.sh` 的断言来推断 `git init` 的行为。

#### 4.4.5 小练习与答案

**练习 1**：为什么 git 的测试以 shell 端到端为主，而不是 C 单元测试为主？

> **参考答案**：git 的用户契约是「命令行行为」，端到端测试最贴近真实使用场景，能捕获「核心库正确但命令行拼装出错」这类问题。此外 git 历史上就是一个跨平台 shell + C 项目，shell 测试可移植且易写。近年来 `t/unit-tests/` 也在补充 C 单元测试，用于覆盖性能敏感或难以从外部触发的内部逻辑。

**练习 2**：`Documentation/git-add.adoc` 与 `command-list.txt` 是什么关系？

> **参考答案**：前者是该命令的用户手册正文；后者是该命令的分类登记。`command-list.txt` 决定 `git-add` 属于 `mainporcelain` / `worktree`，进而决定它在 `git help` 与 man 分组里的归属；而手册内容本身写在 `git-add.adoc` 里。两者都是「新增命令时必须同步更新」的资产。

**练习 3**：`CodingGuidelines` 里「用 tab 缩进、按 8 空格解释」这条规则对源码地图有什么影响？

> **参考答案**：它让顶层 `.c`、`builtin/*.c`、`refs/*.c`、`reftable/*.c` 以及 `t/*.sh` 在视觉风格上高度统一，读者在目录之间切换时不会有「换了一个项目」的突兀感。这正是「扁平核心库 + 多个子系统」能保持可读性的软约束基础。

---

## 5. 综合实践

把本讲四个模块串起来，完成一份「**源码地图档案**」。

1. **顶层核心库**：在仓库根目录运行 `ls *.c | head -20`，挑出 3 个核心库源码（如 `blob.c`、`commit.c`、`object-file.c`），并在 `Makefile` 的 `LIB_OBJS` 里找到它们的 `.o` 条目，确认行号。
2. **builtin 子命令**：在 `builtin/` 里挑 3 个命令（如 `add.c`、`cat-file.c`、`checkout.c`），记录每个文件里 `cmd_*` 函数所在的行号；再找出一个「命令名 ≠ 文件名」的例子（如 `git status` → `builtin/commit.c`）并给出 `Makefile` 中手动登记它的行号。
3. **辅助脚本**：找出 3 个 shell 脚本（如 `git-sh-setup.sh`、`git-sh-i18n.sh`、`git-merge-one-file.sh`），在 `command-list.txt` 里确认它们的分类（应为 `purehelpers`）。
4. **命令分类**：在 `command-list.txt` 里各找一个 porcelain（如 `git-add`）和一个 plumbing（如 `git-cat-file`），抄下它们的完整登记行，并说明第二列、第三列的含义。
5. **后端与文档定位**：记录 `refs/reftable-backend.c` 与 `reftable/stack.c` 各自的职责差异，并写下 `Documentation/MyFirstContribution.adoc` 里新增一个命令需要改动的 4 处位置。

把以上 5 项整理成一张表格（列：路径 / 类型 / 关键行号 / 一句话作用）。这张表就是你后续阅读 git 源码的「索引页」。

> 提示：这一步是纯阅读 + 记录，不需要改任何源码。若某个行号在你看的版本里略有出入，以你本地 HEAD 为准并在档案里注明 commit。

---

## 6. 本讲小结

- git 的顶层是**扁平的核心库**（`blob.c`、`commit.c`、`object-file.c` …），靠文件名前缀划分模块，并由 `Makefile` 的 `LIB_OBJS` 统一汇总。
- `builtin/` 是**子命令实现目录**，每个 `.c` 导出一个 `cmd_*`；`Makefile` 用 `patsubst` 把 `builtin/xxx.o` 自动转成 `git-xxx`，但「命令名 ≠ 文件名」的情况（如 `git status` 住在 `commit.c`）需手动登记。
- `command-list.txt` 是所有命令的**分类登记表**，区分 `mainporcelain`（瓷器）与 `plumbing*`（底层），驱动 `git help` 分组与 man 手册生成。
- `refs/` 是引用的**后端适配层**（files / packed / reftable），`reftable/` 则是一个**可独立的二进制格式子库**，二者定位不同。
- `t/` 是约 1098 个 shell 端到端测试，`Documentation/` 是 `.adoc` 文档源加流程规范（`CodingGuidelines`、`MyFirstContribution.adoc`）；它们与 `Makefile`、`command-list.txt` 一起，构成新增命令时的「四件套」改动面。
- 风格上，`CodingGuidelines` 用「tab 缩进、C99、shell 规范」把核心库、builtin、子库与测试统一成一个可读整体。

---

## 7. 下一步学习建议

本讲建立的是「静态地图」。下一讲 **u1-l4 命令分发主入口 git.c** 会把地图「点亮」：你将进入 `git.c`，看 `cmd_main` 如何解析 argv、查 `commands[]` 表、调用 `handle_builtin`，以及命令名不匹配时如何用 `execv_dashed_external` 回退执行外部命令。

在进入下一讲之前，建议你：

1. 重读本讲的「源码地图档案」（综合实践产物），确保能对着行号说出每个目录的职责。
2. 跳读 `Documentation/MyFirstContribution.adoc` 的前几节，建立「新增一个 builtin 命令要动哪些文件」的预期——这会让 u1-l4 的命令表讨论更具体。
3. 如果好奇底层存储，可以先扫一眼 `object.h` 与 `refs.h` 的注释（不必读懂），为第 3、5 单元埋下印象。
