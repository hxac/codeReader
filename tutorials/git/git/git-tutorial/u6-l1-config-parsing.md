# 配置系统与 config 解析

## 1. 本讲目标

本讲是「配置 config」单元的第一讲，承接 u2-l2 的 `struct repository`（仓库运行时上下文）。在 u2-l2 中我们看到，`struct repository` 持有 `config` 指针，它指向一个 `struct config_set`。本讲就要回答：**这个 `config_set` 是怎么被填满的？配置文件从哪里来、按什么顺序读取？我以后怎么查一个配置项？**

读完本讲，你应当能够：

- 说清 git 配置的「回调驱动解析模型」——配置文件被逐字符线性扫描，每解析出一个键值对就回调一次用户函数。
- 列出 git 读取配置文件的多级来源（system / global / local / worktree / command-line）及它们的优先级顺序。
- 理解 `struct config_set` 如何用 hashmap + 有序列表把整仓库配置缓存进内存，以及「最后一份获胜（last one wins）」语义。
- 区分两套不同的「解析」：解析**配置文件**（`config.c`）与解析**子命令选项**（`parse-options.c`），并看懂 `parse_options` 主循环。

## 2. 前置知识

本讲默认你已经读过 u1-l4（命令分发）与 u2-l2（`struct repository`）。需要先理解几个概念：

- **配置项（config variable）**：形如 `core.ignorecase`、`user.name`、`remote.origin.url` 的「键值对」。它由「节（section）」「子节（subsection）」「变量名（key）」三段组成，用点分隔。例如 `[remote "origin"] url = ...` 在文件里写作节 `remote`、子节 `origin`、变量 `url`，规范化键名为 `remote.origin.url`。
- **作用域（scope）**：一个配置项的「出身」。`system`（全机器）、`global`（某用户）、`local`（某仓库）、`worktree`（某工作树）、`command`（命令行 `-c` 注入）。作用域决定了优先级。
- **回调函数（callback）**：一个由调用方提供、由框架在合适时机反复调用的函数。git 的配置解析就是「每解析到一个键值对，就调一次你的回调」。
- **hashmap**：一种按「键的哈希值」快速定位数据的结构，查询接近常数时间。git 用它来按配置键名快速查找。

一句话直觉：**git 的配置不是「一次读取整张表」，而是「一边逐字符解析文件、一边把每个键值对喂给一个回调函数」**。回调可以选择「当场处理」（流式）或「存进缓存」（`config_set`）供日后查询。这两种用法共用同一套解析器。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [config.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.h) | 配置系统的全部公开 API 与数据结构声明：`enum config_scope`、`struct key_value_info`、`struct config_set`、回调类型 `config_fn_t`、各类 `repo_config_get_*` 查询函数。 |
| [config.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c) | 配置系统的实现：字符级解析器、多级文件加载序列、`config_set` 缓存的填充与查询、命令行 `-c` 注入、`include`/`includeIf`。 |
| [parse-options.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/parse-options.c) | 子命令**选项**的解析器（如 `--git-dir`、`--verbose`），与配置文件解析是两套独立机制，本讲对比讲解。 |
| [parse-options.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/parse-options.h) | `struct option`、选项类型枚举、`OPT_*` 初始化宏。 |
| [builtin/config.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/config.c) | `git config` 命令实现，演示回调驱动的「流式」用法（`collect_config`）与 `--show-origin` 如何读取每个键的出身。 |

## 4. 核心概念与源码讲解

本讲按「先讲底层解析机制，再讲它喂进哪些文件，再讲这些文件如何被缓存成可查询结构，最后对比另一套命令行选项解析」的顺序，拆成四个最小模块。

### 4.1 回调驱动的配置解析（基础机制）

#### 4.1.1 概念说明

这是整个配置系统的地基。git 的配置文件（`.git/config`、`~/.gitconfig` 等）都是同一种简单的文本格式：

```
[user]
    name = Linus Torvalds
    email = torvalds@osdl.org
[core]
    ignorecase = true
```

git **不是**把文件一次性读成一张大表，而是逐字符线性扫描：遇到 `[` 就读一个节头，遇到字母就读一个变量名和值，每凑齐一个键值对，就立刻调用一次「回调函数」把 `(key, value, ...)` 交给调用方。

这套设计的好处写在 [config.h:9-22](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.h#L9-L22) 的注释里：

> 配置文件被线性解析，每发现一个变量就传给一个调用方提供的回调函数。回调负责决定要做什么，且可以自由忽略某些选项。在一次 git 程序运行中，配置被解析多次、用不同回调挑出各自关心的变量，是很常见的。

回调的类型是 [`config_fn_t`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.h#L163-L164)，签名为「键名、值、上下文、用户数据」，返回 0 成功、-1 表示解析失败：

```c
typedef int (*config_fn_t)(const char *, const char *,
                           const struct config_context *, void *);
```

其中键名是「规范化的扁平形式」——节、子节、变量名用点连起来，且**节名和变量名全小写**（子节保留原大小写），如 `core.ignorecase`、`diff.SomeType.textconv`。值若没有写（裸布尔）则为 `NULL`，通常解释为「真」。

「上下文」`struct config_context` 里最重要的是 `kvi`（key-value info），它记录这个键值对**来自哪个文件、第几行、什么作用域**——这正是 `git config --show-origin` 能显示来源的依据：

```c
struct key_value_info {
    const char *filename;
    int linenr;
    enum config_origin_type origin_type;   /* 文件 / stdin / blob / 命令行 */
    enum config_scope scope;               /* system / global / local / ... */
};
```

定义见 [config.h:120-125](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.h#L120-L125)。

#### 4.1.2 核心流程

解析一个配置源的完整链路：

```
git_config_from_file_with_options(fn, filename, ...)   // 打开文件
        │
        ▼
do_config_from_file(...)                                // 装填 config_source
        │  绑定 fgetc/ungetc/ftell 三个函数指针
        ▼
do_config_from(top, fn, ...)                            // 初始化 strbuf 与 kvi
        │
        ▼
git_parse_source(top, fn, kvi, ...)   ★ 主循环：逐字符扫描 ★
        │  遇到 '['  → get_base_var 读节头，组装 var = "section."
        │  遇到字母  → get_value 读变量名 + parse_value 读值
        │                  → 调用 fn(name, value, ctx, data)
        │  遇到 '#'/';' → 注释，跳过
        │  遇到 '\n' 且 eof → 结束
```

关键在于解析器与「数据从哪来」解耦了：数据源由 `struct config_source` 抽象，它内部用三个函数指针 `do_fgetc`/`do_ungetc`/`do_ftell` 取字符。文件源绑定到真正的 `getc`/`ungetc`；内存源（如 blob、stdin 缓冲）绑定到在内存缓冲上前进/后退的实现。这样解析器只认「给我下一个字符」，不关心背后是磁盘文件还是内存块。

#### 4.1.3 源码精读

**`struct config_source` —— 解析器的统一输入抽象**，[config.c:40-63](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L40-L63)。注意三个函数指针 `do_fgetc`/`do_ungetc`/`do_ftell`，以及 `var`（正在拼装的变量名）、`value`（拼装中的值）、`linenr`（当前行号）：

```c
struct config_source {
    struct config_source *prev;
    union { FILE *file; struct config_buf { const char *buf; size_t len, pos; } buf; } u;
    enum config_origin_type origin_type;
    const char *name;
    enum config_error_action default_error_action;
    int linenr;
    int eof;
    ...
    struct strbuf value, var;
    unsigned subsection_case_sensitive : 1;
    int (*do_fgetc)(struct config_source *c);
    int (*do_ungetc)(int c, struct config_source *conf);
    long (*do_ftell)(struct config_source *c);
};
```

**`git_parse_source` —— 解析主循环**，[config.c:1048-1185](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1048-L1185)。下面是它的骨架（删去 BOM、事件回调等细节）：

```c
for (;;) {
    int c = get_next_char(cs);
    if (c == '\n') { /* 换行：遇 eof 则结束 */ }
    if (c == '#' || c == ';') { comment = 1; continue; }   // 注释
    if (c == '[') {                                          // 节头
        strbuf_reset(var);
        if (get_base_var(cs, var) < 0 || var->len < 1) break;
        strbuf_addch(var, '.');                              // var = "section."
        baselen = var->len;
        continue;
    }
    if (!isalpha(c)) break;                                  // 非法字符
    strbuf_setlen(var, baselen);                             // 回退到节头前缀
    strbuf_addch(var, tolower(c));                           // 追加变量名首字母
    if (get_value(cs, kvi, fn, data, var) < 0) break;        // 读完整变量名+值，回调
}
```

注意 `var` 这个 `strbuf` 的复用：解析器把当前节名（如 `core.`）留在 `var` 的前 `baselen` 字节里，每次读到新变量时先用 `strbuf_setlen(var, baselen)` 截断到节名前缀，再追加变量名。这是「逐字符、状态机式」解析的典型写法。

**`get_value` —— 读完变量名与值并触发回调**，[config.c:901-943](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L901-L943)。它继续吃字符拼完变量名，跳过空格，遇到 `=` 就调 `parse_value` 取值，然后**正是这里调用了回调 `fn`**：

```c
value = NULL;
if (c != '\n') {
    if (c != '=') return -1;
    value = parse_value(cs);              // 取出值字符串（或 NULL=裸布尔）
    if (!value) return -1;
}
cs->linenr--;
kvi->linenr = cs->linenr;                 // 记下行号，供错误信息/--show-origin
ret = fn(name->buf, value, &ctx, data);   // ★ 回调在这里发生 ★
```

**`parse_value` —— 取出右值**，[config.c:835-899](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L835-L899)。它处理引号、转义（`\t`/`\n`/`\b`）、行内注释（`#`/`;`）和尾部空格裁剪（`trim_len`）。

**`do_config_from` —— 把 config_source 与解析器对接**，[config.c:1355-1376](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1355-L1376)。它初始化两个 `strbuf`（`value`/`var`）、用 `kvi_from_source` 填好出身信息，然后调用 `git_parse_source`。`kvi_from_source` 见 [config.c:1038-1046](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1038-L1046)，把文件名、行号、作用域写进 `kvi`。

#### 4.1.4 代码实践

**实践目标**：用真实配置文件验证「节头 + 变量名」如何拼成规范化键名。

**操作步骤**：

1. 在一个测试仓库里写入一个含子节、含引号值、含注释的配置片段（示例代码，用于观察解析行为）：

```bash
git init cfg-demo
cd cfg-demo
cat >> .git/config <<'EOF'
[user]
    ; 这是一行注释
    name = Demo User
    email = demo@example.com
[remote "origin"]
    url = https://github.com/example/repo.git
    fetch = +refs/heads/*:refs/remotes/origin/*
EOF
```

2. 用 `git config --get` 验证规范化键名（节小写、子节保留大小写）：

```bash
git config --get user.name
git config --get remote.origin.url
git config --get remote.origin.fetch
```

3. 用 `--get-regexp` 列出某节下所有键，观察规范化后的键名形态：

```bash
git config --get-regexp '^remote\.'
```

**需要观察的现象**：`remote.origin.url` 中的 `origin`（子节）保留了你写的大小写；而 `remote`（节）和 `url`（变量名）即使你写成大写 `[Remote]`、`URL =`，查询时也必须用小写 `remote.origin.url`。

**预期结果**：三条 `--get` 都能返回对应值，说明解析器把节/变量名归一为小写、子节保留大小写，与 [config.c:901-943](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L901-L943) 中 `strbuf_addch(name, tolower(c))` 的行为一致。

> 注：命令的实际运行结果取决于你的本地环境。若无法运行，请对照源码理解：`git config --get remote.origin.url` 等价于在解析后用规范化键 `remote.origin.url` 查询。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `git config` 的回调签名里，键名是「扁平的点分形式」而磁盘上却写成节头 `[remote "origin"]`？解析器在哪一步完成这个转换？

**答案**：磁盘格式便于人读写（节头省去重复前缀），但程序查询需要唯一、规范化的键名。解析器在 `git_parse_source` 读到 `[` 时调 `get_base_var` 拼出 `remote.origin.` 前缀存进 `var`（见 [config.c:1109-1119](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1109-L1119)），随后 `get_value` 追加变量名，最终把扁平键 `remote.origin.url` 喂给回调。

**练习 2**：如果一个配置值写成 `name = Demo` 后面跟一个行内注释 `# 老名字`，解析后回调收到的 value 是什么？

**答案**：是 `Demo`（尾部空格被裁剪）。`parse_value` 在遇到非引号状态下的 `#`/`;` 时置 `comment=1` 跳过剩余字符，并用 `trim_len` 去掉值尾部的空白（见 [config.c:861-868](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L861-L868)）。

---

### 4.2 配置文件加载层级

#### 4.2.1 概念说明

上一模块讲的是「怎么解析一个文件」。本模块讲「git 到底读哪些文件、按什么顺序」。git 的配置来自多个层级，按优先级从低到高为：

| 层级 | 作用域 | 典型路径 | 谁覆盖谁 |
|------|--------|----------|----------|
| 系统级 | `CONFIG_SCOPE_SYSTEM` | `/etc/gitconfig`（或 `$GIT_CONFIG_SYSTEM`） | 优先级最低 |
| 全局级 | `CONFIG_SCOPE_GLOBAL` | `~/.gitconfig` 与 `$XDG_CONFIG_HOME/git/config` | 覆盖系统级 |
| 仓库级 | `CONFIG_SCOPE_LOCAL` | `.git/config`（即 `<commondir>/config`） | 覆盖全局级 |
| 工作树级 | `CONFIG_SCOPE_WORKTREE` | `.git/config.worktree`（需开启 `extensions.worktreeConfig`） | 覆盖仓库级 |
| 命令行级 | `CONFIG_SCOPE_COMMAND` | `git -c key=val`、`GIT_CONFIG_COUNT` 等环境变量 | 优先级最高 |

作用域由 [`enum config_scope`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.h#L39-L47) 枚举。**核心规则是「最后读到的获胜（last one wins）」**：git 按上表从低到高依次把每个文件喂给同一个回调，回调每收到一个值就用新值覆盖旧值，于是最后被喂入的（优先级最高的）最终生效。这一点 [config.h:204-216](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.h#L204-L216) 的注释说得非常清楚。

此外，配置文件里可以写 `include.path` 与 `includeIf.<条件>.path` 来**包含**其他配置文件，这是一种把大配置拆分、或按仓库条件加载不同配置的机制。

#### 4.2.2 核心流程

```
repo_config(repo, fn, data)              // 仓库级总入口（也走缓存，见 4.3）
config_with_options(fn, data, src, repo, opts)        // 可定制入口
        │  若 respect_includes：把 fn 包装成 git_config_include（拦截 include 指令）
        │  若指定了具体 src（文件/stdin/blob）：只解析这一个
        │  否则走标准序列 ↓
        ▼
do_git_config_sequence(opts, repo, fn, data)          // ★ 多级加载主序列 ★
        │  1. git_config_system() 为真 → 读 system_config      (scope=SYSTEM)
        │  2. xdg_config 存在 → 读                              (scope=GLOBAL)
        │  3. user_config 存在 → 读 ~/.gitconfig                (scope=GLOBAL)
        │  4. 非 ignore_repo 且 repo_config 存在 → 读 .git/config (scope=LOCAL)
        │  5. 非 ignore_worktree 且开启 worktree_config → 读 config.worktree (scope=WORKTREE)
        │  6. 非 ignore_cmdline → git_config_from_parameters      (scope=COMMAND)
```

注意第 2、3 步都是 global 作用域，但 xdg 先于 user 读取，所以 `~/.gitconfig` 覆盖 `$XDG_CONFIG_HOME/git/config`。

#### 4.2.3 源码精读

**`do_git_config_sequence` —— 多级加载主序列**，[config.c:1544-1610](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1544-L1610)。这是本模块最核心的代码，它严格按 system → xdg → user → local → worktree → command 顺序逐个 `git_config_from_file_with_options`：

```c
if (git_config_system() && system_config &&
    !access_or_die(system_config, R_OK, ...))
    ret += git_config_from_file_with_options(fn, system_config, data,
                                             CONFIG_SCOPE_SYSTEM, NULL);

git_global_config_paths(&user_config, &xdg_config);
if (xdg_config   && !access_or_die(xdg_config,   R_OK, ACCESS_EACCES_OK))
    ret += git_config_from_file_with_options(fn, xdg_config, data, CONFIG_SCOPE_GLOBAL, NULL);
if (user_config   && !access_or_die(user_config,   R_OK, ACCESS_EACCES_OK))
    ret += git_config_from_file_with_options(fn, user_config, data, CONFIG_SCOPE_GLOBAL, NULL);

if (!opts->ignore_repo && repo_config && !access_or_die(repo_config, R_OK, 0))
    ret += git_config_from_file_with_options(fn, repo_config, data, CONFIG_SCOPE_LOCAL, NULL);

if (!opts->ignore_worktree && worktree_config &&
    repo && repo->repository_format_worktree_config &&
    !access_or_die(worktree_config, R_OK, 0))
    ret += git_config_from_file_with_options(fn, worktree_config, data, CONFIG_SCOPE_WORKTREE, NULL);

if (!opts->ignore_cmdline && git_config_from_parameters(fn, data) < 0)
    die(_("unable to parse command-line config"));
```

逐行对照即可看出优先级顺序。注意每个文件只在「存在且可读」时才被读取，缺失的层级被静默跳过。

**各级路径如何确定**：

- 系统级：[`git_system_config`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1496-L1503)，优先 `$GIT_CONFIG_SYSTEM`，否则编译期常量 `ETC_GITCONFIG`（`system_path` 处理）。[`git_config_system`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1539-L1542) 还会看 `$GIT_CONFIG_NOSYSTEM` 来彻底关闭系统配置。
- 全局级：[`git_global_config_paths`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1525-L1537)，优先 `$GIT_CONFIG_GLOBAL`，否则 `~/.gitconfig` 加 `$XDG_CONFIG_HOME/git/config`。
- 仓库级：`mkpathdup("%s/config", opts->commondir)`（[config.c:1564](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1564)）。这里的 `commondir` 与 u2-l2 讲的「多工作树共享的公共目录」呼应。

**`config_with_options` —— 可定制入口**，[config.c:1612-1652](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1612-L1652)。当 `opts->respect_includes` 为真时，它把用户的回调 `fn` 包了一层 [`git_config_include`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L416-L448)。这层包装在把每个普通键值对**透传**给原回调之外，额外拦截 `include.path` 与 `includeIf.<cond>.path`：

```c
ret = inc->fn(var, value, ctx, inc->data);     // 先把值透传给真正的回调
if (!strcmp(var, "include.path"))
    ret = handle_path_include(ctx->kvi, value, inc);   // 无条件包含
if (!parse_config_key(var, "includeif", &cond, &cond_len, &key) &&
    cond && include_condition_is_true(...) && !strcmp(key, "path"))
    ret = handle_path_include(ctx->kvi, value, inc);   // 条件包含
```

`handle_path_include`（[config.c:142-191](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L142-L191)）展开路径（相对路径以「包含它的配置文件所在目录」为基准）、递归地再次调用 `git_config_from_file_with_options`，并用 `MAX_INCLUDE_DEPTH=10` 防止循环包含（[config.c:178-179](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L178-L179)）。条件包含支持 `gitdir:`、`gitdir/i:`、`onbranch:`、`hasconfig:remote.*.url:`，由 [`include_condition_is_true`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L396-L414) 分派。

**命令行级配置**：[`git_config_from_parameters`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L731-L797) 读取 `$GIT_CONFIG_COUNT` + `$GIT_CONFIG_KEY_*`/`$GIT_CONFIG_VALUE_*` 与 `$GIT_CONFIG_PARAMETERS`（即 `CONFIG_DATA_ENVIRONMENT`，由 u6-l2 会讲的 `git_config_push_parameter` 填充）。它同样最终回调 `fn`，只是 `kvi` 标成命令行出身。

#### 4.2.4 代码实践

**实践目标**：用 `git config --list --show-origin --show-scope` 直观看到每一级来源与优先级。

**操作步骤**：

1. 在不同层级写同名配置 `user.name`，制造覆盖关系：

```bash
git config --system   user.name "System User"   2>/dev/null || echo "（系统级不可写，跳过）"
git config --global   user.name "Global User"
git config --local    user.name "Local User"
git -c user.name="Cmdline User" config --get user.name
```

2. 列出所有配置并显示来源与作用域：

```bash
git config --list --show-origin --show-scope | grep '^.\{0,40\}user\.name'
```

3. 对照阅读 `git config` 自己如何用回调展示来源：[`show_config_origin`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/config.c#L234-L247) 读取 `kvi->origin_type` 与 `kvi->filename` 拼出形如 `file:.git/config` 的前缀，[`show_config_scope`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/config.c#L249-L258) 读取 `kvi->scope`。

**需要观察的现象**：

- `--show-origin` 会显示形如 `file:/home/you/.gitconfig`、`file:.git/config`、`command line:` 的来源前缀，正好对应 `enum config_origin_type` 的取值。
- 三个层级都写了 `user.name`，但 `git config --get user.name` 只返回优先级最高的 `Local User`；再加 `-c` 则返回 `Cmdline User`。

**预期结果**：你亲眼看到「同一键名出现多份，但 `--get` 只返回优先级最高的那份」，这正是 `do_git_config_sequence` 从低到高喂入、回调「后者覆盖前者」的结果。

> 注：系统级配置写入通常需要 root 权限，若不可写则跳过该行，不影响对优先级的观察。运行结果依本地环境而定，对照 [config.c:1544-1610](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1544-L1610) 理解即可。

#### 4.2.5 小练习与答案

**练习 1**：如果同时设置了 `$XDG_CONFIG_HOME/git/config` 里的 `user.name` 和 `~/.gitconfig` 里的 `user.name`，哪个生效？为什么？

**答案**：`~/.gitconfig` 生效。因为 `do_git_config_sequence` 先读 xdg、后读 user（[config.c:1580-1586](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1580-L1586)），「最后读到的获胜」，所以 `~/.gitconfig` 覆盖 xdg。两者同属 `GLOBAL` 作用域。

**练习 2**：`include.path` 指向的文件，其作用域如何确定？相对路径以哪里为基准？

**答案**：被包含文件继承「包含它的那个文件」的作用域（`handle_path_include` 把 `kvi->scope` 透传给递归调用，见 [config.c:183-184](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L183-L184)）。相对路径以「发起包含的配置文件所在目录」为基准（[config.c:162-175](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L162-L175)），所以 `~/.gitconfig` 里的 `include.path = git/work.conf` 指的是 `~/git/work.conf`。

---

### 4.3 config_set 缓存与查询

#### 4.3.1 概念说明

前两模块的回调是「流式」的——每解析出一个键值对就当场处理一次，不存留。但很多 builtin 想在运行期间**反复查询**配置（比如先查 `core.bare`，再查 `core.repositoryformatversion`……），若每次都重新扫一遍所有文件就太慢了。

于是 git 提供了第二套用法：**把整个仓库的配置一次性解析进一个内存缓存 `struct config_set`，之后所有查询都查这个缓存**。这正是 `struct repository.config` 指向的东西（u2-l2）。它的查询速度接近常数时间，因为内部用 hashmap 按规范化键名建了索引。

`struct config_set` 同时维护两套结构（[config.h:493-497](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.h#L493-L497)）：

```c
struct config_set {
    struct hashmap config_hash;     // 按键名快速定位
    int hash_initialized;
    struct configset_list list;     // 按解析顺序排列的扁平列表
};
```

- `config_hash`：把每个键映射到一个 `config_set_element`，后者持有该键的**值列表** `value_list`（一个键可能被多个层级写入，故值有多个）。
- `list`：按「文件内顺序 + 文件加载顺序」记录所有键值对的扁平列表，用于保留「最后获胜」所需的顺序信息。

[config.h:471-491](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.h#L471-L491) 给出了两个元素结构：

```c
struct config_set_element {
    struct hashmap_entry ent;
    char *key;
    struct string_list value_list;     // 该键的所有值（按优先级升序）
};
struct configset_list_item {
    struct config_set_element *e;
    int value_index;                  // 指向 value_list 中的第几项
};
```

**两种查询语义**：

- 单值查询（`repo_config_get_value` 等）：返回**值列表的最后一项**，即「最后获胜」的最高优先级值。
- 多值查询（`repo_config_get_value_multi`）：返回整个值列表（按优先级升序），适用于 `credential.helper` 这类允许多值的键。

#### 4.3.2 核心流程

**填充（一次）**：

```
首次访问 repo->config
        │  git_config_check_init(repo)   （config.h 无，见 config.c:2320）
        ▼
repo_read_config(repo)                            // config.c:2287
        │  新建/清空 repo->config（一个 config_set）
        │  config_with_options(repo_config_callback, ...)  // 走 4.2 的多级序列
        ▼
repo_config_callback(key, value, ctx, data)       // config.c:2277
        │  （顺带处理 comment_char 等内置配置）
        ▼
config_set_callback(key, value, ctx, set)         // config.c:1822
        ▼
configset_add_value(kvi, set, key, value)         // config.c:1743
        │  configset_find_element → hashmap 查/建元素
        │  string_list_append 到 value_list
        │  追加一项到 set->list，并保存 kvi（出身信息）
```

**查询（多次）**：

```
repo_config_get_value(repo, key, &value)          // config.c:2350
        │  git_config_check_init(repo)            // 懒加载：首次访问才填缓存
        ▼
git_configset_get_value(repo->config, key, &value, NULL)
        ▼
git_configset_get_value_multi → configset_find_element（hashmap 查找）
        │  返回 value_list 最后一项 = 最高优先级值
```

注意「懒加载」：缓存在**第一次有人查询配置时**才被填充（`git_config_check_init` → `repo_read_config`），之后整个进程复用同一份；`repo_config_clear` 可以使其失效、下次查询时重建。

#### 4.3.3 源码精读

**`configset_add_value` —— 往缓存里塞一个键值对**，[config.c:1743-1778](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1743-L1778)。这是理解缓存结构的关键：

```c
ret = configset_find_element(set, key, &e);          // hashmap 查这个键
if (!e) {                                            // 首次见到这个键
    e = xmalloc(sizeof(*e));
    hashmap_entry_init(&e->ent, strhash(key));
    e->key = xstrdup(key);
    string_list_init_dup(&e->value_list);
    hashmap_add(&set->config_hash, &e->ent);
}
si = string_list_append_nodup(&e->value_list, xstrdup_or_null(value));  // 值进 value_list

ALLOC_GROW(set->list.items, set->list.nr + 1, set->list.alloc);
l_item = &set->list.items[set->list.nr++];
l_item->e = e;                                       // 同时记进扁平 list
l_item->value_index = e->value_list.nr - 1;

*kv_info = *kvi_p;                                   // 复制出身信息
si->util = kv_info;                                  // 挂到 string_list_item 上
```

要点：同一个键多次出现时，`value_list` 会越来越长（每层一个值），而 `list` 记录每一次追加的「元素指针 + 在其 value_list 中的下标」。出身信息 `kvi` 被复制后挂在 `si->util` 上，所以**每个值都记得自己来自哪个文件第几行**。

**`configset_find_element` —— 用 hashmap 查/建元素**，[config.c:1719-1741](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1719-L1741)。注意它在查询前先用 [`git_config_parse_key`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L610) 把用户给的键规范化（节/变量名小写、子节保留大小写），保证 `User.Name`、`user.name`、`USER.NAME` 查到同一个元素。规范化逻辑在 [`do_parse_config_key`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L545-L608)：`strrchr` 找到最后一个点，点之前（节）和点之后（变量名）一律 `tolower`，中间的子节保留原样。

**`git_configset_get_value_multi` —— 多值查询**，[config.c:1858-1871](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1858-L1871)，直接返回元素的 `value_list`。单值查询 `git_configset_get_value`（[config.c:1836-1856](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1836-L1856)）在其注释里点明了「最后获胜」：取 value_list 的最后一项。

**`repo_read_config` —— 填充仓库缓存**，[config.c:2287-2318](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L2287-L2318)。它把 `repo->config` 初始化为空 `config_set`，然后用 `config_with_options(repo_config_callback, ...)` 跑一遍 4.2 的多级序列——每解析出一个键值对，`repo_config_callback` 最终经 `config_set_callback` 调到 `configset_add_value`，于是缓存被填满。

**`repo_config` 与 `repo_config_get_value` —— 两种消费方式**，[config.c:2334-2355](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L2334-L2355)。`repo_config` 是「按缓存遍历」：它先确保缓存已建，再用 [`configset_iter`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1654-L1673) 把缓存扁平 list 里的每一项回调给用户 `fn`；`repo_config_get_value` 则是「按键查缓存」。

对比一下「流式」与「缓存」：`git config --list`（[builtin/config.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/config.c)）用的是**流式**——它直接 `config_with_options(collect_config, ...)`，回调 [`collect_config`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/config.c#L503-L536) 把每条结果格式化进一个 `strbuf` 数组，**不**走 `repo->config` 缓存；而绝大多数 builtin 用 `repo_config_get_*` 走缓存。

#### 4.3.4 代码实践

**实践目标**：体会「同一键多值」与「最后获胜」，并理解缓存只建一次。

**操作步骤**：

1. 制造一个允许多值的键 `credential.helper`（在不同层级各写一个）：

```bash
git config --global credential.helper "store"
git config --local  credential.helper "cache --timeout=3600"
```

2. 单值查询只返回最高优先级，多值查询返回全部：

```bash
git config --get             credential.helper          # 期望：cache ... （local 最后获胜）
git config --get-all         credential.helper          # 期望：两行都列出
git config --get-all         credential.helper --show-origin   # 看各自来源
```

3. 验证查询是「懒加载 + 缓存」：在 `config.c:2287` 的 `repo_read_config` 入口处理解——缓存只在首次查询时建一次。你可以用 `GIT_TRACE2_PERF=1 git rev-parse HEAD` 观察 trace 里 config 读取只发生一次（高级观察，可选）。

**需要观察的现象**：`--get` 只给一个值（local 的），`--get-all` 给出全部两个值，且 `--show-origin` 显示它们分别来自 `~/.gitconfig` 与 `.git/config`。

**预期结果**：单值「最后获胜」（local 覆盖 global），多值按优先级升序列出，与 [config.c:1836-1871](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1836-L1871) 的语义一致。

> 注：实际输出依你的全局配置而定；若你的全局已有别的 `credential.helper`，结果会更多。对照源码理解「value_list 升序、单值取末项」即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `configset_add_value` 要把每个值都追加进 `value_list`，而不是直接用新值覆盖旧值？

**答案**：因为 git 需要同时支持两种语义——单值查询取「最后获胜」（取末项即可），而多值查询（如 `credential.helper`、`--get-all`）需要保留**所有层级**的值。若直接覆盖，多值信息就丢了。此外每个值还挂着自己的 `kvi` 出身信息，`--show-origin` 才能逐条显示来源。

**练习 2**：用户调用 `repo_config_get_bool(repo, "core.bare", &val)` 时，配置文件是在这一刻才被读取的吗？

**答案**：不完全是。「读取并解析所有配置文件」只发生在**首次**访问缓存的 `repo_read_config`（懒加载，`git_config_check_init`）；`repo_config_get_bool` 本身只做 hashmap 查询，不碰磁盘。后续所有 `repo_config_get_*` 都复用同一份内存缓存，除非有人调 `repo_config_clear` 使其失效。

---

### 4.4 parse-options 命令行解析

#### 4.4.1 概念说明

到目前为止讲的「解析」都是**配置文件解析**。但 git 子命令还有另一种解析需求：**命令行选项**解析，比如 `git log --oneline -n 5`、`git config --global user.name "X"` 里的 `--oneline`/`-n`/`--global`。这套机制由 `parse-options.c` 提供，与 `config.c` 完全独立，但思想上有平行之处（都是「逐项扫描 + 分派」），且本单元经常被并列，故在此对比讲解。

两者的核心区别：

| 维度 | 配置解析（config.c） | 命令行选项解析（parse-options.c） |
|------|----------------------|-----------------------------------|
| 输入来源 | 多个配置文件 + 环境变量 + `-c` | `argv[]` 命令行参数 |
| 数据结构 | 回调 + `config_set` hashmap 缓存 | `struct option[]` 选项表 + 直接写变量 |
| 「键名」 | `section.subsection.key` 点分 | `--long-name` / `-s` 短名 |
| 处理结果 | 喂给回调或进缓存 | 直接把值写进调用方的 C 变量 |

调用方事先声明一张 `struct option options[]` 表，每项描述一个选项（长名、短名、类型、要填充的变量指针、帮助文本），然后调用 `parse_options(argc, argv, ...)`，框架会自动把命令行里的 `-h`/`--help`、未知选项报错、缩写匹配、`--` 分隔等都处理好，并把识别出的值写进表里指定的变量。

#### 4.4.2 核心流程

```
parse_options(argc, argv, prefix, options, usagestr, flags)   // parse-options.c:1181
        │  preprocess_options：处理别名、子命令模式等
        │  parse_options_start_1：初始化上下文 ctx
        ▼
parse_options_step(ctx, options, usagestr)   ★ 主循环：逐个处理 argv ★
        │  对每个 arg：
        │    非 '-' 开头 → 位置参数或子命令（parse_subcommand）
        │    "--"        → 之后全是位置参数
        │    "--xxx"     → parse_long_opt 匹配长选项
        │    "-x"        → parse_short_opt 匹配短选项（支持聚合 -abc）
        │  内置：-h/--help 打印用法，--git-completion-helper 给补全脚本用
        ▼
get_value → 按 option.type 分派（OPTION_STRING/INTEGER/CALLBACK/BIT...）
        │  把值写进 opt->value 指向的变量
parse_options_end → 返回剩余非选项参数个数
```

#### 4.4.3 源码精读

**`struct option` —— 描述一个选项**，[parse-options.h:154-169](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/parse-options.h#L154-L169)：

```c
struct option {
    enum parse_opt_type type;       // 选项类型（见下）
    int short_name;                 // 短名，如 'v'
    const char *long_name;          // 长名，如 "verbose"
    void *value;                    // 要填充的变量指针
    size_t precision;
    const char *argh, *help;
    enum parse_opt_option_flags flags;
    parse_opt_cb *callback;
    intptr_t defval;
    parse_opt_ll_cb *ll_callback;
    ...
};
```

`type` 来自 [`enum parse_opt_type`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/parse-options.h#L12-L32)，常见有 `OPTION_BOOL`（布尔）、`OPTION_STRING`（字符串）、`OPTION_INTEGER`（整数）、`OPTION_CALLBACK`（自定义回调）、`OPTION_BIT`（按位或）、`OPTION_SET_INT`（置定值）。头文件提供大量 `OPT_*` 宏（如 `OPT_BOOL_F`、`OPT_STRING_F`，[parse-options.h:171-220](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/parse-options.h#L171-L220)）来便捷地初始化表项。

**`parse_options` —— 顶层入口**，[parse-options.c:1181-1235](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/parse-options.c#L1181-L1235)。它预处理选项表、初始化上下文，然后调 `parse_options_step`，并根据返回码处理「请求帮助 / 报错退出 / 补全退出」等：

```c
real_options = preprocess_options(&ctx, options);
parse_options_start_1(&ctx, argc, argv, prefix, options, flags);
switch (parse_options_step(&ctx, options, usagestr)) {
case PARSE_OPT_HELP:
case PARSE_OPT_ERROR:  exit(129);
case PARSE_OPT_COMPLETE: exit(0);
...
}
return parse_options_end(&ctx);   // 返回剩余（非选项）参数个数
```

**`parse_options_step` —— 逐参数主循环**，[parse-options.c:995-1169](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/parse-options.c#L995-L1169)。骨架如下：

```c
for (; ctx->argc; ctx->argc--, ctx->argv++) {
    const char *arg = ctx->argv[0];
    if (*arg != '-' || !arg[1]) {            // 非选项：位置参数 / 子命令
        ...
        ctx->out[ctx->cpidx++] = ctx->argv[0];
        continue;
    }
    if (arg[1] != '-') {                      // 短选项 -x（可聚合 -abc）
        ctx->opt = arg + 1;
        parse_short_opt(ctx, options);
        while (ctx->opt) parse_short_opt(ctx, options);
        continue;
    }
    if (!arg[2]) break;                       // "--" 之后全是位置参数
    ...
    parse_long_opt(ctx, arg + 2, options);    // 长选项 --xxx
}
return PARSE_OPT_DONE;
```

注意它内置了 `-h`/`--help`（打印用法）和 `--git-completion-helper`（供 `git-completion.bash` 列出该命令支持哪些选项）等「魔法选项」，所有 builtin 共享。

**与配置解析的呼应**：`parse_long_opt`/`parse_short_opt` 匹配到选项后，最终经 `get_value` 按 `option.type` 分派，把参数值写进 `opt->value` 指向的 C 变量——这与配置解析里「回调收到 key/value 后由调用方自行处理」类似，但选项解析**直接改写调用方的变量**，无需调用方再写回调（除非用 `OPTION_CALLBACK`）。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：看清「选项表 → 命令行参数 → C 变量」的对应，并理解 `--git-completion-helper` 这个内置魔法。

**操作步骤**：

1. 让任意 builtin 吐出它的选项表（这是 `parse_options_step` 内置支持的特殊参数）：

```bash
git config --git-completion-helper
git rev-parse --git-completion-helper
```

2. 阅读 `git config` 自己的选项表：在 [builtin/config.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/config.c) 里搜索 `OPT_BOOL` / `OPT_STRING`，你会看到形如 `OPT_BOOL(0, "show-origin", &opts.show_origin, N_("show origin of config ..."))` 的表项（[builtin/config.c:115](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/config.c#L115)）。对照 [parse-options.h:201](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/parse-options.h#L201) 的 `OPT_BOOL_F` 宏展开，理解这一行声明了「长名 `--show-origin`、无短名、无参数、解析后把 `1` 写进 `opts.show_origin`」。

3. 验证「未知选项报错 + 缩写匹配」：分别试 `git config --bogus-xxx`（报错）和 `git config --lis`（缩写匹配 `--list`，若未禁用缩写）。

**需要观察的现象**：`--git-completion-helper` 输出一串空格分隔的长选项名，正是该 builtin 选项表里所有选项的 `long_name`；这正是 shell 补全脚本能补全 `git config --<Tab>` 的数据来源。

**预期结果**：你能把 `--git-completion-helper` 的输出逐项对应回 builtin 选项表里的 `OPT_*` 宏，从而确认「命令行选项」与「配置项」是两套各自独立的解析体系。

> 注：是否允许缩写匹配受 `GIT_TEST_DISALLOW_ABBREVIATED_OPTIONS` 影响（见 [parse-options.c:1190-1191](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/parse-options.c#L1190-L1191)），生产环境默认允许。

#### 4.4.5 小练习与答案

**练习 1**：`git config --list --show-origin` 中，`--list` 和 `--show-origin` 分别由哪套机制解析？`user.name` 之类又由哪套机制处理？

**答案**：`--list`、`--show-origin` 是**命令行选项**，由 `parse-options.c` 的 `parse_options` 解析（匹配 `git config` 选项表后置 `opts.list=1`、`opts.show_origin=1`）。而 `user.name` 这种「配置键名」只有在它作为 `git config user.name` 的位置参数出现时，才由 `git config` 内部去调用 `config.c` 的配置机制查询/写入。两者职责不同：前者解析「怎么运行这个命令」，后者处理「配置内容」。

**练习 2**：为什么 `parse_options` 的返回值是「剩余非选项参数的个数」？

**答案**：因为 `parse_options` 把识别出的选项「吃掉」了，剩下的（`--` 之后或无法识别为选项的位置参数，如 `git config user.name` 里的 `user.name`）需要原样交还给 builtin 继续处理。它把剩余参数放进 `ctx->out` 并返回个数，builtin 据此遍历 `ctx->out`（见 [parse-options.c:1171](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/parse-options.c#L1171) `parse_options_end`）。

---

## 5. 综合实践

把本讲四条主线串起来：**配置项从「磁盘文件」经过「回调解析」进入「多级序列」，最终落进「`config_set` 缓存」供查询；而命令行「选项」走另一套 `parse-options`。**

请完成下面这个端到端的小任务：

1. **制造多层级 + 多值的配置**：

```bash
git init cs-demo && cd cs-demo
git config --global user.name "Global Me"
git config --local  user.name "Local Me"
# 命令行级用 -c 注入，不必落盘
git -c user.name="Inline Me" config --list --show-origin --show-scope | grep user.name
```

2. **观察加载顺序与出身**：运行 `git config --list --show-origin --show-scope`，在输出里找到 `user.name` 的记录，确认每条的 `origin_type`（`file`/`command line`）和 `scope`（`global`/`local`/`command`）。对照 [config.c:1544-1610](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1544-L1610) 解释这些条目是按什么顺序被喂入回调的。

3. **验证「最后获胜」**：`git config --get user.name` 应返回优先级最高的那一个；再加 `-c user.name=X` 又会如何？把这与 `configset_add_value` 的 value_list 追加顺序（[config.c:1767](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1767)）和单值查询取末项（[config.c:1842-1856](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1842-L1856)）对应起来。

4. **画出数据流图**：在一张纸上画出从 `repo_config_get_value(repo, "user.name", &v)` 出发，经过 `git_config_check_init`（懒加载）→ `repo_read_config` → `config_with_options` → `do_git_config_sequence`（读各级文件）→ `git_parse_source`（逐字符解析）→ `configset_add_value`（进 hashmap 缓存），再回到 `configset_find_element`（查询）的完整闭环。

5. **区分两套解析**：运行 `git config --get user.name --show-origin`，指出命令里哪些 token 由 `parse-options.c` 处理（`--get`、`--show-origin`），哪些由 `config.c` 处理（`user.name`）。

如果你能把第 4 步的闭环图讲清楚，并答出第 5 步的分工，本讲就达标了。

## 6. 本讲小结

- git 配置采用**回调驱动的线性解析**：`git_parse_source` 逐字符扫描，每凑齐一个键值对就回调一次 `config_fn_t`，键名被规范化为「节.子节.变量名」（节/变量名小写、子节保留大小写）。
- 配置来源分多级，由 [`do_git_config_sequence`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1544-L1610) 按 **system → xdg → user(global) → local → worktree → command** 顺序加载，遵循**「最后读到的获胜」**；`include`/`includeIf` 由回调包装层 `git_config_include` 拦截处理。
- `struct config_set` 把整仓库配置缓存进内存（hashmap + 有序列表），**懒加载**于首次查询，单值查询取「最后获胜」、多值查询返回全部；每个值都记得自己的出身 `kvi`。
- 存在「流式」（如 `git config --list` 的 `collect_config`）与「缓存」（`repo_config_get_*`）两种用法，共用同一套解析器。
- 子命令**选项**由独立的 `parse-options.c` 解析，靠 `struct option[]` 表 + `parse_options` 主循环把命令行参数直接写进 C 变量，与配置文件解析是两套体系。
- 缓存与出身信息（`kvi`）让 `git config --show-origin`/`--show-scope` 能逐条标注每个配置项的来源。

## 7. 下一步学习建议

- 下一讲 **u6-l2 环境变量与配置层级** 会专门讲 `environment.c`：`GIT_DIR`/`GIT_WORK_TREE` 等环境变量如何覆盖仓库上下文，以及 `git -c`/`--config-env` 注入的优先级与生效时机——它会用到本讲的 `git_config_push_parameter`/`git_config_push_env`，是本讲的自然延伸。
- 若想深入「查询」，可读 `config.c` 中 [`repo_config_get_*`](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L2344-L2435) 系列与值类型解析辅助函数 `git_config_int`/`git_config_bool`（[config.c:1226-1300](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/config.c#L1226-L1300)）。
- 若想理解「写配置」的原子性（`.git/config` 加锁、改名），可预读 `lockfile.c`，为后续讲义中 `repo_config_set_multivar_in_file_gently` 的实现做准备。
- 对 `parse-options` 感兴趣的读者，官方 API 文档在 `Documentation/technical/api-parse-options.adoc`，可对照 `parse-options.c` 的 `get_value` 分派逻辑精读。
