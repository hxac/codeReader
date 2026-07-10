# 命令分发主入口 git.c

## 1. 本讲目标

在前一讲里我们已经知道：git 是「一个二进制加一堆硬链接」，所有 `git-xxx` 命令其实是同一个可执行文件的硬链接，靠 `argv[0]`（即程序被调用时的名字）来决定执行哪段子命令逻辑。那么问题来了：当你在终端敲下 `git status` 时，字符串 `"status"` 到底是怎么被翻译成函数 `cmd_status()` 的？

本讲就读 `git.c` 这一个文件，把「命令行字符串 → C 函数」这条分发链路彻底讲清。学完后你应该能：

- 说清楚 `main()` → `cmd_main()` → `handle_options()` → `run_argv()` 这条主路径上每一步做了什么。
- 看懂 `commands[]` 命令表的结构，并能解释 `RUN_SETUP`、`NEED_WORK_TREE` 等标志位如何影响一个命令的运行环境。
- 复述出「内置命令（builtin）」「别名（alias）」「dashed external 外部命令」三条分发路径的优先级与回退关系。
- 自己照着表项的格式，画出一次 `git log` 从 `argv` 到 `cmd_log()` 的完整调用流程。

---

## 2. 前置知识

在进入源码前，先建立几个直觉概念。如果这些你已经清楚，可以快速跳过。

- **argc / argv**：C 程序的 `main(int argc, const char **argv)` 里，`argv[0]` 是程序自己的名字（如 `/usr/bin/git`），`argv[1]`、`argv[2]` …… 才是用户传的参数。当用户敲 `git status -s`，`argv` 大致是 `["git", "status", "-s"]`。
- **函数指针表（command table）**：把「命令名字符串」和「处理它的函数地址」配对存进一个数组，运行时用字符串查表得到函数指针再调用。这是 C 里实现「字符串分派到函数」最常见的手法，git.c 的 `commands[]` 就是这种表。
- **硬链接分发**：上一讲讲过，`git status` 这个名字本身可能是个硬链接。git 会看 `argv[0]` 的文件名部分：如果是 `git-status`，就等价于「`git status`」。本讲会看到这发生在哪里。
- **builtin 与 dashed external**：
  - **builtin（内置命令）**：编译进同一个二进制、在 `commands[]` 表里登记的命令，如 `status`、`log`、`commit`。
  - **dashed external（带横线的外部命令）**：不在表里、而是去 `PATH` 里找一个叫 `git-xxx` 的独立可执行脚本/程序来运行。git 自带的 `git-*` shell/perl 脚本，以及用户自己装的扩展命令，都走这条路。
- **别名（alias）**：用户在配置里写 `alias.co = checkout`，之后 `git co` 就等于 `git checkout`。别名本质是在 `argv` 被分发前做一次字符串替换。

一句话总结本讲的分发模型：**git 先剥掉全局选项，再把第一个非选项参数拿去查表；查到 builtin 就直接调用，查不到就依次尝试别名、外部命令，最后还找不到就报「未知命令」并给出拼写建议。**

---

## 3. 本讲源码地图

本讲几乎全部内容都集中在 `git.c` 这一个文件里，其余三个文件起辅助作用。

| 文件 | 作用 |
| --- | --- |
| [git.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c) | 命令分发的总入口与全部核心逻辑：`cmd_main`、`commands[]` 命令表、`handle_builtin`、`execv_dashed_external`、`handle_alias`。 |
| [builtin.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin.h) | 声明所有 `cmd_*` 函数的原型，并用注释文档说明了「如何新增一个 builtin」以及各标志位含义。 |
| [alias.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/alias.c) | 别名查询与展开的底层实现：`alias_lookup` 从配置读别名、`split_cmdline` 把别名串拆成参数数组。 |
| [help.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/help.c) | 命令列表加载与「未知命令」处理，包括 `load_command_list`、`help_unknown_cmd`（拼写纠错）。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **cmd_main 与命令表 commands[]** —— 从 `main()` 进入、剥离全局选项、进入主分发循环。
2. **命令选项 RUN_SETUP / NEED_WORK_TREE 等** —— `commands[]` 每一项的第三个字段如何控制命令的运行环境。
3. **handle_builtin 与 execv_dashed_external** —— 查表调用内置命令、别名展开、以及外部命令的回退执行。

### 4.1 cmd_main 与命令表 commands[]

#### 4.1.1 概念说明

git 的真正 C 语言入口是 [common-main.c:4](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/common-main.c#L4) 的 `main()`，它非常薄，几乎只做平台相关的启动初始化，随后把控制权交给 `cmd_main()`：

- [common-main.c:9](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/common-main.c#L9) 调用 `result = cmd_main(argc, argv);`

之所以把业务逻辑放在 `cmd_main` 而不是直接在 `main` 里写，是为了让 git 的「分发逻辑」和「跨平台启动胶水代码」解耦：`common-main.c` 负责 signal、locale、`argv` 编码等环境准备，`git.c` 的 `cmd_main` 负责纯逻辑分发。这种「瘦 main + cmd_main」的分层在 git 源码里很常见（每个 builtin 实际上也有自己的入口约定）。

#### 4.1.2 核心流程

`cmd_main`（[git.c:918-1012](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L918-L1012)）的整体流程可以概括为这几步（注意下面的 `git-xxxx` 处理排在最前）：

```text
cmd_main(argc, argv)
  │
  ├─ 1. 从 argv[0] 取出程序名，去掉目录前缀（保留 "git" 或 "git-status"）
  │
  ├─ 2. 若名字形如 "git-xxxx"  ──► 当作 "git xxxx" 直接 handle_builtin
  │                                  （不能再走 external，否则会无限递归）
  │
  ├─ 3. 跳过 argv[0]，调用 handle_options() 剥离 -C / -c / --git-dir 等全局选项
  │
  ├─ 4. 若剥完后没有命令  ──► 打印 usage + 常用命令帮助，exit(1)
  │
  ├─ 5. 把 --version/-v 改写成 "version"，--help/-h 改写成 "help"
  │
  ├─ 6. setup_path()：把 exec-path 插到 PATH 最前，方便找 git-* 外部命令
  │
  └─ 7. 进入 run_argv() 主循环 ──► 解析 / 查表 / 回退，直至命令执行或彻底失败
```

需要特别理解的是第 2 步的「`git-xxxx` 特例」。源码注释解释得很清楚：

- [git.c:935-944](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L935-L944) —— 说明 `git-xxxx` 等价于 `git xxxx`，但有两个限制：中间不能插选项，而且不能当外部命令去 exec（否则会再次启动一个 `git-xxxx`，无限循环）。

所以这条路径**只**调用 `handle_builtin`，查不到就直接 `die`：

- [git.c:945-951](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L945-L951) —— `if (skip_prefix(cmd, "git-", &cmd)) { ... handle_builtin(&args); ... die(...) }`。这正是硬链接分发到 builtin 的入口：当你用名字 `git-status` 调用程序时走这里。

第 3 步的 `handle_options` 把所有「`git` 自己的全局选项」吃掉，剩下的第一个参数才是真正的子命令。它还负责把 `--git-dir`、`-c key=val` 这类设置写进环境变量或配置栈，供后续 builtin 读取（这部分细节留到配置讲义）：

- [git.c:953-956](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L953-L956) —— `argv++; argc--; handle_options(&argv, &argc, NULL);`

第 5 步把两个历史遗留的「看起来像选项的命令」改写为正常子命令名：

- [git.c:967-970](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L967-L970) —— `--version`/`-v` → `"version"`，`--help`/`-h` → `"help"`。这就是为什么 `git --version` 实际上运行的是 builtin `version`。

最后进入第 7 步的 `run_argv` 循环（见 4.3 节）。

#### 4.1.3 源码精读：commands[] 命令表

`cmd_main` 之所以能查表，是因为 `git.c` 里定义了一张全局命令表 `commands[]`：

- [git.c:33-37](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L33-L37) 定义了表项结构 `struct cmd_struct`，三个字段分别是：命令名字符串 `cmd`、函数指针 `fn`、选项位图 `option`。

```c
struct cmd_struct {
    const char *cmd;
    int (*fn)(int, const char **, const char *, struct repository *);
    unsigned int option;
};
```

- [git.c:529-685](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L529-L685) 是整张 `commands[]` 表，每一行就是一条 `{ "名字", cmd_函数, 选项 }`。我们关心的两个例子：

  - `git status` 的表项在 [git.c:660](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L660)：`{ "status", cmd_status, RUN_SETUP | NEED_WORK_TREE }`。
  - `git log` 的表项在 [git.c:597](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L597)：`{ "log", cmd_log, RUN_SETUP }`。

  注意两个「反直觉」细节（承接 u1-l3 的「命令名 ≠ 函数名」结论）：
  - `cmd_status` 的**声明**在 [builtin.h:260](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin.h#L260)，但它的**实现**并不在 `builtin/status.c`，而在 [builtin/commit.c:1537](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/commit.c#L1537)（`int cmd_status(...)`）。历史原因，status 和 commit 共用一个文件。
  - `cmd_log` 声明在 [builtin.h:207](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin.h#L207)，实现在 [builtin/log.c:825](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/log.c#L825)。
  - 同一个函数可以挂多个名字：例如 [git.c:658](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L658) `{ "stage", cmd_add, ... }` 让 `git stage` 等价于 `git add`；[git.c:626](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L626) `{ "pickaxe", cmd_blame, ... }` 让 `git pickaxe` 等价于 `git blame`。

查表函数是 `get_builtin`，逻辑极简——线性扫描、字符串比较：

- [git.c:687-695](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L687-L695) —— 遍历 `commands[]`，`strcmp` 命中就返回表项指针，否则返回 `NULL`。git 命令表是按字母序手工排好的，但查找仍是 O(n) 线性扫，对几百条命令完全够用。
- [git.c:697-700](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L697-L700) —— `is_builtin(s)` 只是 `get_builtin` 的布尔包装，别处（如 `execv_dashed_external`）用来判断某名字是不是内置命令。

#### 4.1.4 代码实践：追踪 git status 的分发路径

**实践目标**：把「`git status`」从命令行一路跟到 `cmd_status()`，验证表项确实在工作。

**操作步骤**：

1. 确认你的 git 是从本仓库源码编译出来的（u1-l2 已讲）。在仓库任意子目录运行：

   ```bash
   ./git status -s
   ```

2. 打开 `git.c`，在 `commands[]` 里定位 `status` 表项（[git.c:660](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L660)），记下它的函数名 `cmd_status` 和选项 `RUN_SETUP | NEED_WORK_TREE`。
3. 打开 `builtin.h` 找到 `cmd_status` 的声明（[builtin.h:260](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin.h#L260)）。
4. 用 grep 找到它的实现位置（已知在 `builtin/commit.c:1537`）。
5. 在 `cmd_status` 入口处临时加一行 `fprintf(stderr, "dispatch: reached cmd_status\n");`（**仅用于本地观察，不要提交**），重新 `make`，再跑一次 `./git status`。

**需要观察的现象**：步骤 5 应在标准错误输出里看到 `dispatch: reached cmd_status`，证明控制流确实走到了表项指向的函数。

**预期结果**：你能完整说出 `status` 字符串 → `get_builtin` 命中 → `run_builtin` → `cmd_status` 的链路。**待本地验证**：步骤 5 的加日志重编译结果取决于你的本地编译环境，若编译报错请检查是否在正确的源码副本上操作。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `git --git-dir=/tmp/x.git status` 能把 `--git-dir` 传给 git 而不是被 status 当成参数？
**参考答案**：因为 `handle_options`（[git.c:956](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L956)）在分发到 `status` **之前**就已经把 `--git-dir` 吃掉、写进 `GIT_DIR` 环境变量（见 [git.c:214-223](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L214-L223)），剩下的 `argv` 里第一个参数才是 `status`。

**练习 2**：`git stage` 为什么能等价于 `git add`？请用源码位置说明。
**参考答案**：[git.c:658](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L658) 表项 `{ "stage", cmd_add, RUN_SETUP | NEED_WORK_TREE }`，把名字 `"stage"` 直接映射到了 `cmd_add`。

---

### 4.2 命令选项 RUN_SETUP / NEED_WORK_TREE 等

#### 4.2.1 概念说明

`commands[]` 每一项第三个字段 `option` 是一个**位图（bitmask）**：用按位或 `|` 把若干标志位组合起来，记录这个命令「需要什么样的运行环境」。这些标志位的定义在 [git.c:21-31](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L21-L31)：

```c
#define RUN_SETUP        (1<<0)
#define RUN_SETUP_GENTLY (1<<1)
#define USE_PAGER        (1<<2)
#define NEED_WORK_TREE   (1<<3)
#define DELAY_PAGER_CONFIG (1<<4)
#define NO_PARSEOPT      (1<<5) /* parse-options is not used */
#define DEPRECATED       (1<<6)
```

每个标志位的官方含义，最权威的说明其实写在 `builtin.h` 的注释里（[builtin.h:30-68](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin.h#L30-L68)），下表做了中文转述：

| 标志位 | 含义 |
| --- | --- |
| `RUN_SETUP` | 必须在仓库内运行；若找不到 `.git` 直接报错；若在子目录里则 `chdir` 到工作树顶部。 |
| `RUN_SETUP_GENTLY` | 「温和版」：有仓库就 `chdir`，没有也不报错（命令自己决定怎么处理）。 |
| `USE_PAGER` | 默认启用分页器（pager）。 |
| `NEED_WORK_TREE` | 必须有工作树，不能在 bare 仓库（裸仓库）里跑；只有配合 `RUN_SETUP` 才有意义。 |
| `DELAY_PAGER_CONFIG` | 让 builtin 自己决定何时读 `pager.<cmd>` 配置，而不是由 git.c 提前处理。 |
| `NO_PARSEOPT` | 该命令不用 parse-options 库解析选项（自定义解析）。 |
| `DEPRECATED` | 已废弃命令（如 `whatchanged`），运行时会提示。 |

为什么要把「是否需要仓库」「是否需要工作树」做成标志位？因为 git 的命令分两类：

- 像 `status`、`log`、`commit` 这种**必须在仓库里**才能工作（要读索引、对象库、引用）。
- 像 `clone`、`init`、`version` 这种**仓库外**也能跑（克隆时还没有仓库呢）。

把这件事集中到一个位图、由 `run_builtin` 统一处理，就不用每个 `cmd_*` 自己重复写一遍「找仓库」的逻辑。这是典型的**把横切关注点提取到调度层**的设计。

#### 4.2.2 核心流程：run_builtin 如何使用这些标志位

真正「消费」这些标志位的是 `run_builtin`（[git.c:466-527](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L466-L527)）。它在调用 `cmd_*` 之前，先按标志位把环境准备好：

```text
run_builtin(p, argc, argv, repo)
  │
  ├─ 取 run_setup = p->option & (RUN_SETUP | RUN_SETUP_GENTLY)
  │
  ├─ 若命令带 -h 求助：把强 RUN_SETUP 降级为 GENTLY
  │     （这样 "git <cmd> -h" 在仓库外也能打印帮助）
  │
  ├─ 按 run_setup 调用仓库发现：
  │     RUN_SETUP        → setup_git_directory()      （找不到仓库就死）
  │     RUN_SETUP_GENTLY → setup_git_directory_gently()（找不到也返回）
  │     都没有           → prefix = NULL
  │
  ├─ 处理 pager 选择（USE_PAGER / pager.<cmd> 配置）
  │
  ├─ 若 NEED_WORK_TREE：setup_work_tree() 确保工作树存在
  │
  ├─ validate_cache_entries() 校验索引
  ├─ status = p->fn(argc, argv, prefix, repo)   ◄── 真正调用 cmd_*
  ├─ validate_cache_entries() 再次校验
  │
  └─ 检查 stdout 写入是否成功（管道/磁盘满等），有问题就 die
```

这里的关键判断在 [git.c:472-486](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L472-L486)：

- `run_setup = (p->option & (RUN_SETUP | RUN_SETUP_GENTLY))` 先把两个 setup 位取出来。
- [git.c:474-477](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L474-L477) 的「降级」技巧：当用户带 `-h` 求助时，把强 `RUN_SETUP` 降成 `RUN_SETUP_GENTLY`，这样 `git foo -h` 在仓库外也能正常打印帮助，而不是因为「找不到仓库」而失败。这是用户体验上的一个细节。
- [git.c:479-486](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L479-L486) 是真正的三路仓库发现分支。
- [git.c:499-500](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L499-L500)：`NEED_WORK_TREE` 在这里被消费——调用 `setup_work_tree()`，对 bare 仓库会失败。
- [git.c:506](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L506)：`status = p->fn(argc, argv, prefix, no_repo ? NULL : repo);` 是分发的最终一击——通过函数指针 `p->fn` 调用具体的 `cmd_*`。`prefix` 是「命令启动时所在子目录相对工作树顶部的路径」，给 builtin 用来把用户给的相对路径换算成工作树根的相对路径（详见 [builtin.h:104-109](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin.h#L104-L109) 的说明）。

对照我们关心的两个命令：

- `status`（`RUN_SETUP | NEED_WORK_TREE`）：必须在仓库内、且必须有工作树 → 这就是为什么 `git status` 在 bare 仓库里会报错。
- `log`（仅 `RUN_SETUP`）：必须在仓库内，但**不**要求工作树 → 所以你在 bare 仓库里 `git log` 是合法的（裸仓库也有提交历史可看）。

#### 4.2.3 源码精读：如何新增一个 builtin

`builtin.h` 顶部有一段非常宝贵的「操作手册」（[builtin.h:7-113](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin.h#L7-L113)），讲清了新增一个 builtin 要做哪些事。核心 4 步（[builtin.h:14-69](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin.h#L14-L69)）：

1. 写实现函数，签名固定为 `int cmd_foo(int argc, const char **argv, const char *prefix, struct repository *repo);`（[builtin.h:20-21](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin.h#L20-L21)）。
2. 在 `builtin.h` 里加 `extern` 声明（例如 [builtin.h:260](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin.h#L260) 的 `cmd_status`）。
3. 在 `git.c` 的 `commands[]` 里加一行 `{ "foo", cmd_foo, <options> }`。
4. 在 `Makefile` 的 `BUILTIN_OBJS` 里加 `builtin/foo.o`。

这恰好印证了 u1-l3 总结的「新增 builtin 要同步改四处」。注意第 3 步的 `<options>` 就是我们本节讲的标志位位图。

#### 4.2.4 代码实践：观察 NEED_WORK_TREE 的效果

**实践目标**：亲手验证 `NEED_WORK_TREE` 标志如何让命令在 bare 仓库里失败。

**操作步骤**：

1. 建一个 bare 仓库（没有工作树）：

   ```bash
   ./git init --bare /tmp/bare.git
   ```

2. 在 bare 仓库里分别运行两个命令，对比结果：

   ```bash
   ./git --git-dir=/tmp/bare.git log        # 仅 RUN_SETUP
   ./git --git-dir=/tmp/bare.git status     # RUN_SETUP | NEED_WORK_TREE
   ```

3. 回到源码：`log` 表项 [git.c:597](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L597) 没有 `NEED_WORK_TREE`，`status` 表项 [git.c:660](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L660) 带了 `NEED_WORK_TREE`。

**需要观察的现象**：`log` 能正常执行（即便仓库为空也只是提示无提交）；`status` 会报错，提示这是一个 bare 仓库、无法执行该命令。

**预期结果**：错误信息来自 `setup_work_tree()`（[git.c:499-500](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L499-L500) 调用）。这证明了标志位是在 `run_builtin` 里、调用 `cmd_status` **之前**就被检查的。**待本地验证**：具体错误措辞可能随版本略有差异。

#### 4.2.5 小练习与答案

**练习 1**：`git clone` 和 `git init` 的表项为什么**没有** `RUN_SETUP`？
**参考答案**：克隆和初始化时本地还没有仓库，若要求 `RUN_SETUP` 会因为「找不到 `.git`」立刻失败。看 [git.c:554](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L554) `{ "clone", cmd_clone }` 和 [git.c:593](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L593) `{ "init", cmd_init_db }`，选项字段都是空的。

**练习 2**：`git diff` 的表项是 `{ "diff", cmd_diff, NO_PARSEOPT }`（[git.c:567](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L567)），它既没有 `RUN_SETUP` 也没有 `NEED_WORK_TREE`。这意味着什么？
**参考答案**：`git diff` 既可以在仓库外被调用（用于比较两个任意文件，如 `git diff file1 file2`），也可以在仓库内比较版本。不强制 setup 给了它这种灵活性。

---

### 4.3 handle_builtin 与 execv_dashed_external

#### 4.3.1 概念说明

到目前为止，我们只讲了「查表命中 builtin」这一条路。但真实的 `git.c` 分发远不止查表——它要在「内置命令、别名、外部命令」三条路径之间循环试探和回退。这个总调度发生在 `run_argv`（[git.c:838-916](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L838-L916)）里，被 `cmd_main` 的主循环反复调用。

先认识两个关键函数：

- **`handle_builtin`**：拿当前 `argv[0]` 去 `get_builtin` 查表，查到就调 `run_builtin` 执行并 `exit`；查不到就**直接返回**（不报错），把机会让给后续路径。
- **`execv_dashed_external`**：当内置命令和别名都搞不定时，去 `PATH` 里找一个叫 `git-<cmd>` 的外部可执行文件来跑。

它们之所以需要「循环 + 回退」，是因为别名的存在让事情变复杂：用户可能用别名 `alias.log = show` 把 `git log` 重定向到 `git show`，于是一次命令可能要先展开别名、再查表、再可能展开下一层别名……所以 `run_argv` 是一个 `while(1)` 循环。

#### 4.3.2 核心流程：run_argv 的三分支循环

```text
run_argv(args)              ◄── cmd_main 的主循环里反复调用
  └─ while (1):
       ① 若是 deprecated 命令且能被别名覆盖  → handle_alias 展开别名，continue
       ② 若还没展开过别名      → handle_builtin(args)   （查表，命中就 exit）
            否则若展开过别名且结果仍是 builtin → fork 一个 "git <结果>" 子进程跑
       ③ execv_dashed_external(args.v)        （去 PATH 找 git-<cmd>）
       ④ 若连外部命令也 ENOENT → 再试一次 handle_alias
            展开成功 → done_alias=1，continue 回到 ②
            展开失败 → break，返回让 cmd_main 处理「未知命令」
```

关键在于：**`handle_builtin`、`execv_dashed_external`、`handle_alias` 三者都会在「命中」时让进程 `exit`，只有都落空时循环才会 `break` 回到 `cmd_main`**，由那里的 `help_unknown_cmd` 给出「未知命令 + 拼写建议」。

#### 4.3.3 源码精读

**handle_builtin：查表命中即执行**

- [git.c:750-787](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L750-L787) 是 `handle_builtin` 的全部逻辑：
  - [git.c:758-767](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L758-L767) 一个贴心改写：把 `git <cmd> --help` 改写成 `git help --exclude-guides <cmd>`，这样求助统一走 `cmd_help`。
  - [git.c:769-786](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L769-L786) 是核心：`get_builtin(cmd)` 查表，命中就**浅拷贝 argv**（因为 `run_builtin` 会改动它），调 `run_builtin`，然后 `exit(ret)`。注意：**查不到就什么都不做、直接返回**，这是「回退」语义的关键。

**execv_dashed_external：去 PATH 找外部命令**

- [git.c:789-830](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L789-L830)：
  - [git.c:798-799](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L798-L799) 拼出子进程命令名 `git-<cmd>`（注意它把 `argv[0]` 前面加了 `git-` 前缀），其余参数原样跟上。
  - [git.c:802](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L802) `cmd.silent_exec_failure = 1` 很重要：如果 `git-<cmd>` 根本不存在（`ENOENT`），不要当成致命错误大张旗鼓地报，而是静默返回，让上层 `run_argv` 继续尝试别名或报「未知命令」。
  - [git.c:819-829](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L819-L829) 根据子进程返回状态决定：成功/正常退出码就 `exit(status)`；若是 `ENOENT`（命令不存在）则**返回**继续回退；其它错误 `exit(128)`。

**handle_alias：别名展开（底层在 alias.c）**

别名查询的真正实现在 `alias.c`：

- [alias.c:84-91](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/alias.c#L84-L91) `alias_lookup(alias)` 通过 `read_early_config` 读取配置，回调 `config_alias_cb` 找 `alias.<name>` 配置项。
- [alias.c:16-82](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/alias.c#L16-L82) `config_alias_cb` 兼容两种写法：`alias.name = value`（无子节，大小写不敏感）和 `[alias "name"] command = value`（有子节，大小写敏感）。

回到 git.c 的 `handle_alias`（[git.c:368-464](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L368-L464)），别名分两种：

- **以 `!` 开头的「shell 别名」**（[git.c:384-409](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L384-L409)）：整条交给 shell 执行，`exit(ret)` 结束。例如 `alias.l = "!git log --oneline"`。
- **普通别名**（[git.c:410-458](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L410-L458)）：用 `split_cmdline`（[alias.c:126-180](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/alias.c#L126-L180)）把别名串拆成参数数组，替换掉 `argv[0]`，然后返回 `1` 让 `run_argv` 再走一轮查表。这里有两道安全闸：
  - [git.c:415-418](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L415-L418)：别名不能改变环境变量（否则后续直接调 builtin 不安全）。
  - [git.c:425-445](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L425-L445)：检测别名递归/环路（如 `alias.a = b`、`alias.b = a`），用 `expanded_aliases` 链表记录展开历史，发现环路就 `die`。

**run_argv 的总循环**

- [git.c:838-916](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L838-L916) 把上面三者串起来：
  - [git.c:864-865](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L864-L865)：未展开过别名时调 `handle_builtin`。
  - [git.c:866-898](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L866-L898)：展开过别名后，为安全起见**不再直接调 builtin**，而是 fork 一个新的 `git` 子进程去跑（注释在 [git.c:856-863](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L856-L863) 解释了原因：别名可能改动了环境，直接调 builtin 不再安全）。
  - [git.c:901](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L901) `execv_dashed_external(args->v)` 是外部命令的兜底。
  - [git.c:908-910](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L908-L910) 兜底之后再试一次别名（处理 `alias.log = show` 这种用别名覆盖内置命令的写法），失败才 `break`。

**cmd_main 的外层循环与未知命令处理**

回到 `cmd_main`，[git.c:985-1005](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L985-L1005) 是外层 `while(1)`：

- [git.c:986-988](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L986-L988)：每次 `run_argv` 后看 `errno`，只要不是 `ENOENT` 就认为「该试的都试完了」，`break`。
- [git.c:996-1001](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L996-L1001)：只有一次机会调用 `help_unknown_cmd(cmd)` 做拼写纠错——这就是你打错命令时看到「Did you mean this?」建议的来源。该函数在 [help.c:644](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/help.c#L644) 用 Levenshtein 编辑距离（[help.c:701-702](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/help.c#L701-L702)）在所有命令里找最接近的候选。

#### 4.3.4 代码实践：观察 dashed external 与拼写建议

**实践目标**：看到「外部命令」分发路径和「未知命令」的拼写建议真实发生。

**操作步骤**：

1. **触发 dashed external**：git 自带很多 `git-*` 脚本（如 `git-request-pull`、`git-submodule` 的包装等）。运行一个并非内置命令的脚本，并打开执行追踪：

   ```bash
   GIT_TRACE=1 ./git request-pull HEAD https://github.com/git/git master 2>&1 | head -20
   ```

   > 注意：这只是用来触发分发路径演示，不必真的有有效参数；重点看 trace 里的 `exec:` 行。

2. **触发未知命令建议**：故意打错一个命令名：

   ```bash
   ./git satuts
   ```

3. 对照源码阅读：
   - 外部命令路径 [git.c:789-830](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L789-L830)。
   - 拼写建议 [help.c:644-706](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/help.c#L644-L706)。

**需要观察的现象**：
- 步骤 1 的 trace 输出里应出现形如 `trace: exec: git-request-pull ...` 的行，说明 git 在内置表里没找到、转而去 `PATH` 执行了外部脚本。
- 步骤 2 会输出 `git: 'satuts' is not a git command.` 并给出 `Did you mean this? status` 之类的建议（受 `help.autocorrect` 配置影响，可能直接执行或仅提示）。

**预期结果**：你能解释 `satuts` → `run_argv` 全部落空 → `cmd_main` 调 `help_unknown_cmd` → Levenshtein 匹配出 `status` 的链路。**待本地验证**：步骤 1 是否真的有对应外部脚本取决于 git 的编译配置；若无 `git-request-pull`，可改用 `./git submodule--helper` 之外的纯脚本命令，或直接观察步骤 2。

#### 4.3.5 小练习与答案

**练习 1**：当 `handle_builtin` 在表里查不到命令时，它做了什么？为什么这样设计？
**参考答案**：它**什么都不做、直接返回**（[git.c:769-786](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L769-L786) 只有 `if (builtin) {...}` 分支，没有 else）。这样设计的目的是把机会留给后续的别名展开和外部命令查找，实现「内置 → 别名 → 外部」的优雅回退链。

**练习 2**：为什么展开过别名之后，`run_argv` 不再直接调用 builtin，而是 fork 一个新 `git` 进程？
**参考答案**：见 [git.c:856-863](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L856-L863) 的注释：别名展开可能已经影响了进程环境（虽然 `handle_options` 检查了环境变量改变并 die，但仍有不安全的情形），为稳妥起见，用一个干净的子进程重新走一遍完整分发更安全。

**练习 3**：用户配置了 `alias.lg = log --oneline`。敲 `git lg` 时，`"lg"` 最终是怎么变成 `cmd_log` 的？
**参考答案**：`run_argv` 第一轮 `handle_builtin("lg")` 查不到 → `execv_dashed_external("git-lg")` 也 `ENOENT` → [git.c:908](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L908) 调 `handle_alias` 查到 `alias.lg = log --oneline` → `split_cmdline` 拆成 `["log", "--oneline"]` 替换 `argv[0]` → 循环回到第二轮，`handle_builtin("log")` 命中 `cmd_log`（[git.c:597](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L597)）。

---

## 5. 综合实践：画出 `git log` 的完整分发流程图

这个任务把本讲三个模块串起来。请你在**注释或一张图**里（不要改源码逻辑，只做文档性说明）完成下面这件事：

**任务**：仿照 `commands[]` 表项的格式（[git.c:33-37](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L33-L37) 的 `struct cmd_struct`），画出一次 `git log --oneline` 从 `argv` 进入到 `cmd_log()` 被调用的完整流程，并在每一步标注它对应的源码行号。

参考答案骨架（你可以把它誊抄到笔记里）：

```text
用户输入: git log --oneline
argv = ["git", "log", "--oneline"]

1. common-main.c:4  main()
2. common-main.c:9  → cmd_main(argc, argv)          [git.c:918]
3. git.c:928-931    取 argv[0]="git"，剥掉目录前缀
4. git.c:945        skip_prefix("git") 不成立（这里是 "git" 不是 "git-log"），跳过
5. git.c:954-956    argv++ 跳过 "git"，handle_options 剥离全局选项
                    （"--oneline" 不是全局选项，留给 log）
6. git.c:967-970    argv[0]="log" 不是 --version/--help，不改写
7. git.c:980        setup_path() 调整 PATH
8. git.c:982-1005   主循环 → run_argv(&args)        [git.c:838]
9. git.c:864-865    handle_builtin(args)            [git.c:750]
10.git.c:769        get_builtin("log") 命中表项     [git.c:687]
                   （表项: git.c:597 { "log", cmd_log, RUN_SETUP }）
11.git.c:782        run_builtin(builtin, ...)        [git.c:466]
12.git.c:479-480    RUN_SETUP → setup_git_directory() 发现仓库
13.git.c:506        p->fn(...) == cmd_log(argc, argv, prefix, repo)
                   （实现在 builtin/log.c:825）
14.run_builtin 返回 → handle_builtin exit(ret)       [git.c:785]
```

**验收标准**：

- 你能指出 `--oneline` 是在哪一步之后才被解析的吗？（答：它作为 `argv` 一部分被传给 `cmd_log`，由 `cmd_log` 内部用 parse-options 解析，**不在** git.c 层面处理——这就是为什么 `log` 没有 `NO_PARSEOPT` 标志。）
- 你能解释为什么 `git log` 在 bare 仓库里也能跑吗？（答：`log` 表项只有 `RUN_SETUP` 没有 `NEED_WORK_TREE`，见 [git.c:597](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L597)。）

---

## 6. 本讲小结

- git 的 C 入口是 `common-main.c` 的 `main()`，它几乎立刻把控制权交给 [git.c:918](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L918) 的 `cmd_main()`，后者完成「剥程序名 → 处理 `git-xxxx` 特例 → 剥全局选项 → 改写 version/help → 进入 `run_argv` 主循环」。
- 命令分发的核心是一张函数指针表 `commands[]`（[git.c:529-685](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/git.c#L529-L685)），每项是 `{ 名字, cmd_函数, 选项位图 }`；查表靠线性 `strcmp`（`get_builtin`）。
- 选项位图（`RUN_SETUP`/`NEED_WORK_TREE`/`USE_PAGER` 等）把「需要仓库/工作树/分页器」等横切关注点集中到 `run_builtin` 统一处理，避免每个命令重复造轮子。
- 分发是「内置 → 别名 → 外部命令」的三路回退循环（`run_argv`）：`handle_builtin` 查到即执行并退出，查不到就让位；别名能递归展开但带环路检测；都失败时由 `help_unknown_cmd` 用编辑距离给拼写建议。
- 新增一个 builtin 要同步改 `builtin/` 实现、`builtin.h` 声明、`git.c` 的 `commands[]`、`Makefile` 的 `BUILTIN_OBJS` 四处（外加文档、测试、command-list.txt）。

---

## 7. 下一步学习建议

本讲讲清了「命令字符串 → cmd_* 函数」的分发，但**故意没有**深入两件事：

1. **仓库是怎么被「发现」的**——`run_builtin` 里那个 `setup_git_directory()` 到底怎么从当前目录向上找到 `.git`、怎么处理 gitfile 和 bare 仓库。这正是下一单元 u2-l1《仓库发现：setup_git_directory》的主题，建议紧接着读 `setup.c`。
2. **全局选项 `-c` / `--git-dir` 如何影响配置**——`handle_options` 把它们写进了环境变量和配置栈，但真正的配置加载在 `config.c`，留到 u6（配置）单元。

如果你想立刻动手加深对分发的理解，可以挑一个简单的 builtin（比如 `cmd_version`，它的实现极短）作为第一个阅读目标，对照本讲的流程图走一遍：从 `git version` 到 `cmd_version()`，你应该能完全不卡壳地走完。
