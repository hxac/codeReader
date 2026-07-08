# 仓库发现：setup_git_directory

## 1. 本讲目标

上一讲我们看完了 `git.c` 的命令分发：用户敲下的命令字符串如何被翻译成 `cmd_*` 函数（见 [u1-l4 命令分发](u1-l4-command-dispatch.md)）。但分发只是第一步——大多数命令（`git status`、`git log`、`git add`……）在真正干活之前还必须先回答一个问题：

> **我现在在哪个仓库里？它的 `.git` 目录到底在哪？**

这个问题由本讲的 `setup_git_directory` 家族函数负责。本讲结束时应掌握：

- 理解 git 如何从当前工作目录**向上逐层搜索** `.git`，直到找到仓库或撞到边界。
- 掌握 **gitfile**（`.git` 是一个文件而非目录时）的解析格式，以及 bare（裸）仓库如何被识别。
- 理解 **所有权校验**（dubious ownership）与 `safe.directory`、`safe.bareRepository` 等安全机制。
- 了解发现的 gitdir 最终如何被写入进程**环境变量**，供后续命令使用。

本讲只讲「找到仓库」这一步，不涉及仓库内部的 `struct repository` 字段细节（那是 [u2-l2](u2-l2-repository-struct.md) 的内容）。

## 2. 前置知识

在进入源码前，先用三个生活化的类比建立直觉。

**类比一：爬楼梯找门牌。** 你站在大楼的某一层，要找到「这栋楼的总机房」。你不知道机房在哪层，于是策略是：先看当前层有没有机房，没有就上一层再看，直到顶层。git 的「目录上溯」就是这个过程——从当前工作目录（cwd）开始，每往上一层就看一眼有没有 `.git`。

**类比二：转交信封（gitfile）。** 机房有两种形态：要么就摆在 `房间号/.git` 这间屋里（`.git` 是目录）；要么门口只贴了一张小纸条写着「真正的机房在 `/data/repos/foo`」（`.git` 是文件）。这张小纸条就是 **gitfile**，它的内容固定是 `gitdir: <真实路径>`。

**类比三：门禁卡（所有权校验）。** 哪怕你找到了机房，git 还会查「这张机房是不是你自己的」。如果你在一个别人创建的仓库目录里运行 git（典型场景：容器、挂载点、CI），git 会拒绝继续，并提示 `detected dubious ownership`，要求你用 `safe.directory` 显式登记一次「我知道这个仓库，放行」。

几个必须先建立的术语：

| 术语 | 含义 |
|------|------|
| **cwd** | current working directory，进程当前工作目录 |
| **gitdir** | 仓库的元数据目录，通常是 `.git` |
| **bare 仓库（裸仓库）** | 没有工作树、整个目录本身就是 gitdir 的仓库，常见于服务器 |
| **worktree（工作树）** | 你实际编辑文件的那层目录 |
| **prefix** | cwd 相对于工作树根的相对路径，子目录里运行 git 时它非空 |

> 关于环境变量名：源码里大量出现的 `GIT_DIR_ENVIRONMENT`、`DEFAULT_GIT_DIR_ENVIRONMENT` 等都是宏，分别展开为字符串 `"GIT_DIR"`、`".git"`，定义在 [environment.h:8-23](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/environment.h#L8-L23)。下文为了简洁会混用宏名与字符串值。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [setup.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c) | **本讲主战场**：目录上溯、gitfile 解析、格式校验、所有权校验、入口分发全部住在这里 |
| [setup.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.h) | 对外声明 `setup_git_directory`、`is_git_directory`、`enum discovery_result` 与 `READ_GITFILE_ERR_*` 错误码 |
| [environment.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/environment.c) | 维护 `local_repo_env[]` 列表，以及发现结果写入环境后的相关上下文 |

调用层次（自顶向下）：

```
setup_git_directory()                      对外入口，返回 prefix
  └─ setup_git_directory_gently()          处理发现结果、决定 die 还是继续
       └─ setup_git_directory_gently_1()   ★ 目录上溯主循环（私有）
            ├─ read_gitfile_gently()        解析 .git 文件
            ├─ is_git_directory()           判定是否仓库目录
            └─ ensure_valid_ownership()     所有权校验
```

## 4. 核心概念与源码讲解

### 4.1 仓库发现的入口与返回值语义

#### 4.1.1 概念说明

仓库发现是一个「可能成功、也可能因多种原因失败」的过程：可能找到了普通仓库，可能找到 bare 仓库，可能根本不在仓库里，可能跨越了文件系统边界，也可能所有权可疑。git 用一个枚举把所有这些结局统一表达出来。本模块先把这个「结果语义表」和两个最薄的对外入口搞清楚，它们是后两个模块的骨架。

#### 4.1.2 核心流程

对外暴露两条入口：

- `setup_git_directory(repo)`：大多数命令用这个——找不到仓库就直接 `die`（报错退出）。
- `setup_git_directory_gently(repo, nongit_ok)`：温和版，找不到仓库时把 `*nongit_ok` 置 1 返回而不是 `die`，供那些「在仓库外也能跑」的命令（如 `git --version`、部分 plumbing）使用。

真正的发现工作委托给私有的 `setup_git_directory_gently_1()`，它返回下面这个枚举：

```c
enum discovery_result {
    GIT_DIR_EXPLICIT = 1,      // GIT_DIR 环境变量显式指定
    GIT_DIR_DISCOVERED = 2,    // 上溯找到了普通 .git
    GIT_DIR_BARE = 3,          // 当前目录本身是 bare 仓库
    /* 以下是错误情况 */
    GIT_DIR_HIT_CEILING = -1,      // 搜到 ceiling 边界仍没找到
    GIT_DIR_HIT_MOUNT_POINT = -2,  // 跨越文件系统边界
    GIT_DIR_INVALID_GITFILE = -3,  // .git 文件格式非法
    GIT_DIR_INVALID_OWNERSHIP = -4,// 所有权可疑
    GIT_DIR_DISALLOWED_BARE = -5,  // bare 仓库被策略禁止
    GIT_DIR_INVALID_FORMAT = -6,
    GIT_DIR_CWD_FAILURE = -7,
};
```

正数表示「找到了」，负数表示「出了问题」。注意 `0` 不在合法取值里——这是个有意设计，`discover_git_directory_reason` 的注释里专门提醒：零值是 `GIT_DIR_NONE`，与旧接口语义不同。

#### 4.1.3 源码精读

枚举定义在 [setup.h:67-79](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.h#L67-L79)。两个入口的声明紧随其后：[setup.h:139-140](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.h#L139-L140)。

最薄的包装 `setup_git_directory` 只是把 `nongit_ok` 传 `NULL`，这样一旦找不到仓库就会 `die`：

```c
const char *setup_git_directory(struct repository *repo)
{
    return setup_git_directory_gently(repo, NULL);
}
```

见 [setup.c:2155-2158](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2155-L2158)。

`setup_git_directory_gently` 先记录当前 cwd，再调用 `setup_git_directory_gently_1`，然后用一个大 `switch` 处理每一种返回值——要么 `chdir` 到仓库根并设置 gitdir，要么根据是否温和决定 `die` 还是置 `nongit_ok`：

```c
switch (setup_git_directory_gently_1(&dir, &gitdir, &report, 1)) {
case GIT_DIR_EXPLICIT:
    prefix = setup_explicit_git_dir(repo, gitdir.buf, &cwd, &repo_fmt, nongit_ok);
    break;
case GIT_DIR_DISCOVERED:
    if (dir.len < cwd.len && chdir(dir.buf))
        die(_("cannot change to '%s'"), dir.buf);
    prefix = setup_discovered_git_dir(repo, gitdir.buf, &cwd, dir.len,
                                      &repo_fmt, nongit_ok);
    break;
case GIT_DIR_BARE:
    ...
}
```

见 [setup.c:1942-1956](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1942-L1956)。注意 `dir.len < cwd.len && chdir(dir.buf)`：如果在子目录里发现的仓库，会把进程 cwd 切回仓库根，于是后续命令拿到的就是「仓库根视角」。

每个负数分支都遵循「温和模式让步、严格模式 die」的模式，例如搜到天花板（ceiling）：

```c
case GIT_DIR_HIT_CEILING:
    if (!nongit_ok)
        die(_("not a git repository (or any of the parent directories): %s"),
            DEFAULT_GIT_DIR_ENVIRONMENT);
    *nongit_ok = 1;
    break;
```

见 [setup.c:1957-1962](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1957-L1962)。这就是你在非仓库目录里敲 `git status` 看到 `fatal: not a git repository` 的源头。

> 设计亮点：把「找到的仓库分几类、找不到有几种原因」用一个枚举说尽，调用方用 `switch` 各取所需。这种「结果即枚举」的写法在 C 项目里是比裸返回码 + errno 更清晰的做法。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到 `setup_git_directory` 在不同场景下的返回语义。
2. **操作步骤**：
   - 在仓库内任意子目录运行 `git status`（成功）。
   - 切到一个非仓库目录（如 `/tmp/discover-demo-$$`，先 `mkdir` 再 `cd` 进去）运行 `git status`（失败）。
3. **需要观察的现象**：仓库内正常输出状态；非仓库目录报 `fatal: not a git repository (or any of the parent directories): .git`。
4. **预期结果**：那句错误信息正是 `setup.c:1959` 的 `die` 文本，证明走了 `GIT_DIR_HIT_CEILING` 分支。
5. 待本地验证（取决于你的环境是否方便切目录）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `setup_git_directory` 把 `nongit_ok` 传成 `NULL` 而不是传一个整型变量的地址？
**答案**：传 `NULL` 表示「调用方不希望容忍找不到仓库的情况」，于是 `setup_git_directory_gently` 在所有错误分支里走 `if (!nongit_ok) die(...)` 直接报错退出。若想容忍，就得用 `_gently` 版本并传入非空指针。

**练习 2**：`enum discovery_result` 的正数值和负数值分别代表什么？
**答案**：正数（`EXPLICIT/DISCOVERED/BARE`）表示成功找到了某种仓库；负数（`HIT_CEILING/HIT_MOUNT_POINT/INVALID_*` 等）表示发现了失败。这种符号化分类让上层能用一个 `switch` 精确处理每种结局。

---

### 4.2 gitfile 解析：从 `.git` 文件到真实 gitdir

#### 4.2.1 概念说明

绝大多数仓库的 `.git` 是个**目录**，里面装着 `HEAD`、`objects/`、`refs/` 等。但有三种情况 `.git` 会是一个**文件**：

1. **worktree（链接工作树）**：`git worktree add` 创建的工作树，其 `.git` 是一个指向主仓库 `.git/worktrees/<name>` 的指针文件。
2. **子模块**：子模块在主仓库索引里是 gitlink，其工作目录的 `.git` 也是指针文件。
3. **手动分离**：用 `git init --separate-git-dir=<path>` 把 gitdir 挪到别处。

这个文件就叫 **gitfile**，内容是固定的一行：

```
gitdir: /真实/绝对/或/相对/路径
```

`read_gitfile_gently` 负责读取并校验它。

#### 4.2.2 核心流程

`read_gitfile_gently(path)` 的判定序列：

1. `stat(path)`：文件不存在 → `MISSING`；是目录 → `IS_A_DIR`；非普通文件 → `NOT_A_FILE`。
2. 文件大小超 1 MiB → `TOO_LARGE`（防止读巨型文件）。
3. `open` + 读全文，读取长度不符 → `READ_FAILED`。
4. 内容不以 `"gitdir: "` 开头 → `INVALID_FORMAT`。
5. 去掉末尾换行后路径为空（`len < 9`）→ `NO_PATH`。
6. 相对路径则拼上 gitfile 所在目录，转成可定位路径。
7. 校验该路径 `is_git_directory()` 为真，否则 → `NOT_A_REPO`。
8. 全部通过 → 用 `strbuf_realpath` 解析成绝对路径返回。

每个错误都对应一个 `READ_GITFILE_ERR_*` 码，调用方据此决定「继续往上搜」还是「直接报错」。

#### 4.2.3 源码精读

错误码宏定义在 [setup.h:31-40](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.h#L31-L40)，公开声明 `read_gitfile_gently` 与一个「严格版」宏 `read_gitfile` 在 [setup.h:42-43](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.h#L42-L43)：

```c
const char *read_gitfile_gently(const char *path, int *return_error_code);
#define read_gitfile(path) read_gitfile_gently((path), NULL)
```

传 `NULL` 给 `return_error_code` 时，出错会调用 `read_gitfile_error_die` 直接 `die`（见 [setup.c:928-953](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L928-L953)）。

核心实现关键片段（[setup.c:965-1044](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L965-L1044)）：

```c
const int max_file_size = 1 << 20;  /* 1MB */
...
if (!starts_with(buf, "gitdir: ")) {
    error_code = READ_GITFILE_ERR_INVALID_FORMAT;
    goto cleanup_return;
}
while (buf[len - 1] == '\n' || buf[len - 1] == '\r')
    len--;
if (len < 9) { error_code = READ_GITFILE_ERR_NO_PATH; goto cleanup_return; }
buf[len] = '\0';
dir = buf + 8;                       /* 跳过 "gitdir: " 这 8 个字符 */
...
if (!is_git_directory(dir)) {
    error_code = READ_GITFILE_ERR_NOT_A_REPO;
    goto cleanup_return;
}
strbuf_realpath(&realpath, dir, 1);  /* 解析成绝对路径 */
```

`buf + 8` 这个魔法数字对应 `"gitdir: "` 正好 8 个字符（`g i t d i r : 空格`）。注意它返回的是一个 **静态缓冲区** `realpath.buf`——函数注释明确写了「返回值来自共享缓冲区」，所以调用方不能跨多次调用持有该指针。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到一个真实的 gitfile 内容。
2. **操作步骤**：在一个已有仓库 `foo` 里执行 `git worktree add ../foo-wt`，然后查看 `../foo-wt/.git` 这个**文件**（不是目录）的内容：`cat ../foo-wt/.git`。
3. **需要观察的现象**：文件内容形如 `gitdir: /abs/path/foo/.git/worktrees/foo-wt`。
4. **预期结果**：内容恰好以 `gitdir: ` 开头，与 `read_gitfile_gently` 解析的格式一致；它指向主仓库 `.git/worktrees/` 下的一个子目录。
5. 待本地验证（需要可写环境创建 worktree）。

#### 4.2.5 小练习与答案

**练习 1**：如果有人把 `.git` 文件内容写成 `gitdir:/x/y`（冒号后少一个空格），会发生什么？
**答案**：`starts_with(buf, "gitdir: ")`（注意末尾空格）失败，返回 `READ_GITFILE_ERR_INVALID_FORMAT`。gitfile 格式要求 `gitdir:` 后必须有一个空格。

**练习 2**：为什么 `read_gitfile_gently` 最后要再调一次 `is_git_directory(dir)`？
**答案**：防止 gitfile 指向一个不存在的或残缺的目录。gitfile 只是「一张指路条」，目的地必须真的是个合法仓库目录（有合法 HEAD、objects、refs），否则记为 `NOT_A_REPO`。

---

### 4.3 目录上溯主循环 `setup_git_directory_gently_1`

#### 4.3.1 概念说明

这是本讲的「心脏」函数。它接收当前目录 `dir`，通过一个 `for (;;)` 无限循环，**一层层往上**寻找 `.git`，找到就返回对应的 `discovery_result`。理解了这个函数，git「如何向上找到 `.git`」的机制就完全清楚了。

#### 4.3.2 核心流程

主循环在每一层做如下探测，顺序见源码注释（[setup.c:1595-1605](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1595-L1605)）：

```
对当前层 dir，依次尝试：
  1. dir/.git 是文件吗？        → read_gitfile_gently   （gitfile 模式）
  2. dir/.git 是目录且是仓库吗？ → is_git_directory       （普通 .git 目录）
  3. dir 自身是仓库吗？          → is_git_directory(dir) （bare 模式）
  都不是 → 上溯一层（dir = dir/..），重复
```

主循环开始前还有三个预处理：

- **短路 GIT_DIR**：若环境变量 `GIT_DIR` 已设置，直接返回 `GIT_DIR_EXPLICIT`，不做任何上溯。
- **天花板 ceiling**：解析 `GIT_CEILING_DIRECTORIES` 环境变量，算出 `ceil_offset`，作为上溯的「禁线」——搜到这里还没找到就放弃（返回 `HIT_CEILING`），避免一直爬到根目录浪费时间或误入不该进的仓库。
- **文件系统边界**：默认 `one_filesystem=1`，记录起始目录的设备号；上溯中若换了一个设备（跨挂载点）且没设 `GIT_DISCOVERY_ACROSS_FILESYSTEM`，立即返回 `HIT_MOUNT_POINT`。

一个很巧妙的实现技巧：循环里复用同一个 `dir` 这个 `strbuf`，每次先 `strbuf_addstr(dir, ".git")` 探测，探测完再 `strbuf_setlen(dir, offset)` 把 `.git` 后缀「砍掉」，然后砍掉最后一段路径名上溯一层。这样既不反复分配内存，路径也始终连贯。

#### 4.3.3 源码精读

整个函数在 [setup.c:1552-1707](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1552-L1707)。函数头注释说得很清楚——它**故意不改全局状态**（不 chdir），以便早期调用者使用；`dir` 出参存「搜索结束时的目录」，`gitdir` 出参存「找到的 .git 路径」（[setup.c:1540-1551](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1540-L1551)）。

**预处理三连**：

```c
gitdirenv = getenv(GIT_DIR_ENVIRONMENT);
if (gitdirenv) {
    strbuf_addstr(gitdir, gitdirenv);
    return GIT_DIR_EXPLICIT;
}
...
if (env_ceiling_dirs) {
    string_list_split(&ceiling_dirs, env_ceiling_dirs, path_sep, -1);
    ...
    ceil_offset = longest_ancestor_length(dir->buf, &ceiling_dirs);
}
...
one_filesystem = !git_env_bool("GIT_DISCOVERY_ACROSS_FILESYSTEM", 0);
if (one_filesystem)
    current_device = get_device_or_die(dir->buf, NULL, 0);
```

见 [setup.c:1569-1608](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1569-L1608)。

**主循环核心**——先拼 `.git` 后缀并尝试当 gitfile 读：

```c
for (;;) {
    int offset = dir->len, error_code = 0;
    ...
    strbuf_addstr(dir, DEFAULT_GIT_DIR_ENVIRONMENT);     /* 拼上 ".git" */
    gitdirenv = read_gitfile_gently(dir->buf, &error_code);
    if (!gitdirenv) {
        switch (error_code) {
        case READ_GITFILE_ERR_MISSING:
            break;                      /* 这层没 .git，继续 */
        case READ_GITFILE_ERR_IS_A_DIR:
            if (is_git_directory(dir->buf)) {            /* .git 是合法目录 */
                gitdirenv = DEFAULT_GIT_DIR_ENVIRONMENT;
                gitdir_path = xstrdup(dir->buf);
            }
            break;
        ...
        }
    } else {
        gitfile = xstrdup(dir->buf);    /* .git 是文件，命中 gitfile */
    }
    strbuf_setlen(dir, offset);         /* 砍掉 ".git" 后缀 */
    if (gitdirenv) {
        /* 命中：校验所有权后返回 DISCOVERED */
        if (ensure_valid_ownership(gitfile, dir->buf, gitdir_candidate, report)) {
            strbuf_addstr(gitdir, gitdirenv);
            ret = GIT_DIR_DISCOVERED;
        } else
            ret = GIT_DIR_INVALID_OWNERSHIP;
        ...
        return ret;
    }
    /* 没命中 .git：检查当前目录本身是否 bare 仓库 */
    if (is_git_directory(dir->buf)) {
        ...
        return GIT_DIR_BARE;            /* 见下一模块 */
    }
    if (offset <= min_offset) return GIT_DIR_HIT_CEILING;
    /* 上溯一层 */
    while (--offset > ceil_offset && !is_dir_sep(dir->buf[offset])) ;
    if (offset <= ceil_offset) return GIT_DIR_HIT_CEILING;
    strbuf_setlen(dir, offset > min_offset ? offset : min_offset);
    if (one_filesystem &&
        current_device != get_device_or_die(dir->buf, NULL, offset))
        return GIT_DIR_HIT_MOUNT_POINT;
}
```

见 [setup.c:1609-1706](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1609-L1706)。

`is_git_directory` 本身判定的「仓库三件套」是：合法 HEAD、可访问的 `objects` 目录、可访问的 `refs` 目录（[setup.c:414-452](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L414-L452)）：

```c
strbuf_addstr(&path, "HEAD");
if (validate_headref(path.buf)) goto done;   /* HEAD 必须是合法 ref 或对象名 */
...
strbuf_addstr(&path, "/objects");
if (access(path.buf, X_OK)) goto done;        /* objects 目录可进入 */
strbuf_addstr(&path, "/refs");
if (access(path.buf, X_OK)) goto done;        /* refs 目录可进入 */
ret = 1;
```

这正是 git 判断「某个目录算不算 gitdir」的最低标准——光有个 `.git` 空目录是不够的。

#### 4.3.4 代码实践（对应规格里的核心实践任务）

1. **实践目标**：在子目录里运行 git，对照源码确认「向上找到 `.git`」的过程。
2. **操作步骤**：
   - 在仓库里建一个子目录：`mkdir -p deep/nested/dir && cd deep/nested/dir`。
   - 运行 `git rev-parse --show-toplevel`（显示工作树根）和 `git rev-parse --git-dir`（显示 gitdir）。
   - 再运行 `GIT_CEILING_DIRECTORIES="$PWD/../.." git rev-parse --show-toplevel`，故意把天花板设在中间。
3. **需要观察的现象**：
   - `--show-toplevel` 输出仓库根的绝对路径，证明 git 从 `deep/nested/dir` 一路向上找到了仓库根的 `.git`。
   - `--git-dir` 通常输出 `.git`（相对路径，因为 git 把 cwd 切回了仓库根，对应 `dir.len < cwd.len && chdir`）。
   - 设置 ceiling 后，若天花板切断了仓库根，会报 `not a git repository`，对应 `GIT_DIR_HIT_CEILING`。
4. **预期结果**：与源码 [setup.c:1609-1706](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1609-L1706) 的上溯逻辑一一吻合。
5. 待本地验证（路径分隔符与挂载点行为随系统而异）。

#### 4.3.5 小练习与答案

**练习 1**：`GIT_DISCOVERY_ACROSS_FILESYSTEM` 这个环境变量解决什么问题？
**答案**：默认情况下，上溯一旦跨过挂载点（设备号变化）就停止并返回 `HIT_MOUNT_POINT`，避免从 `/home/user/repo` 误爬到 `/` 这种不同文件系统上。但有时仓库的工作树本身就跨挂载点（如 NFS），此时设 `GIT_DISCOVERY_ACROSS_FILESYSTEM=1` 关闭该限制。

**练习 2**：循环里为什么先 `strbuf_addstr(dir, ".git")` 探测、再 `strbuf_setlen(dir, offset)` 砍掉？
**答案**：为了在同一个 `strbuf` 上反复拼装路径而不重复分配。`offset` 记录的是「加 `.git` 之前」的长度，砍回 `offset` 就恢复了当前层目录的纯净路径，随后再砍掉最后一段路径名完成「上溯一层」。

---

### 4.4 bare 仓库识别与所有权校验

#### 4.4.1 概念说明

主循环里有两道「安全关」放到了本模块讲：**bare 仓库策略** 和 **所有权校验**。它们都是近年 git 为了防御攻击（如容器/挂载场景下被恶意仓库利用）而加的关卡。

- **bare 仓库**：当 git 发现「当前目录本身就是个 gitdir」（即 `is_git_directory(dir)` 为真），它不会无脑接受，而是先看 `safe.bareRepository` 策略——默认 `explicit` 时，只有用户**显式**进入的 bare 仓库才被允许，避免你在 `cd` 进某个目录时被里面的 bare 仓库「钓」到。
- **所有权校验（dubious ownership）**：git 检查 gitdir、gitfile、worktree 的属主是否就是当前用户；不是则报 `detected dubious ownership`，要求用 `safe.directory` 登记放行。

#### 4.4.2 核心流程

主循环里的 bare 分支（[setup.c:1683-1692](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1683-L1692)）：

```
若 is_git_directory(当前目录) 为真：
    若策略 == EXPLICIT 且不是显式 bare 仓库 → 返回 GIT_DIR_DISALLOWED_BARE
    若所有权校验失败                         → 返回 GIT_DIR_INVALID_OWNERSHIP
    否则 gitdir = "."，返回 GIT_DIR_BARE
```

所有权校验 `ensure_valid_ownership(gitfile, worktree, gitdir, report)` 的判定（[setup.c:1414-1444](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1414-L1444)）：

```
若三要素（gitfile/worktree/gitdir 中非空者）都「属当前用户」 → 安全(返回1)
否则：规范化路径，查 safe.directory 配置是否放行 → 放行则安全，否则不安全(返回0)
```

#### 4.4.3 源码精读

bare 分支：

```c
if (is_git_directory(dir->buf)) {
    trace2_data_string("setup", NULL, "implicit-bare-repository", dir->buf);
    if (get_allowed_bare_repo() == ALLOWED_BARE_REPO_EXPLICIT &&
        !is_implicit_bare_repo(dir->buf))
        return GIT_DIR_DISALLOWED_BARE;
    if (!ensure_valid_ownership(NULL, NULL, dir->buf, report))
        return GIT_DIR_INVALID_OWNERSHIP;
    strbuf_addstr(gitdir, ".");
    return GIT_DIR_BARE;
}
```

见 [setup.c:1683-1692](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1683-L1692)。注意 `gitdir` 被设成 `"."`——bare 仓库的 gitdir 就是当前目录本身。

`get_allowed_bare_repo` 的默认值由编译期宏 `WITH_BREAKING_CHANGES` 决定：默认 `ALLOWED_BARE_REPO_ALL`，开启 breaking changes 时为 `EXPLICIT`，并可用 `safe.bareRepository` 配置覆盖（[setup.c:1487-1496](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1487-L1496)）。

所有权校验：

```c
static int ensure_valid_ownership(const char *gitfile,
                                  const char *worktree, const char *gitdir,
                                  struct strbuf *report)
{
    struct safe_directory_data data = { 0 };
    if (!git_env_bool("GIT_TEST_ASSUME_DIFFERENT_OWNER", 0) &&
        (!gitfile || is_path_owned_by_current_user(gitfile, report)) &&
        (!worktree || is_path_owned_by_current_user(worktree, report)) &&
        (!gitdir || is_path_owned_by_current_user(gitdir, report)))
        return 1;                       /* 三要素都属当前用户，安全 */
    data.path = real_pathdup(worktree ? worktree : gitdir, 0);
    if (!data.path) return 0;
    git_protected_config(safe_directory_cb, &data);  /* 查 safe.directory 是否放行 */
    return data.is_safe;
}
```

见 [setup.c:1414-1444](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1414-L1444)。`is_path_owned_by_current_user` 负责实际的属主比对（跨平台实现，POSIX 上比对 `st_uid` 与 `getuid()`）。失败时上层 `die_upon_dubious_ownership` 会打印那段著名的提示并建议 `git config --global --add safe.directory <path>`（[setup.c:1446-1465](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1446-L1465)）。

#### 4.4.4 代码实践

1. **实践目标**：触发并理解「所有权可疑」提示。
2. **操作步骤**：阅读 [setup.c:1459-1464](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1459-L1464) 的 `die` 文本，再阅读 `ensure_valid_ownership`。在你的环境里如果方便，可以 `sudo chown -R nobody: $(pwd)` 改一个测试仓库的属主，然后以自己身份运行 `git status`，观察报错。
3. **需要观察的现象**：报 `detected dubious ownership in repository at '...'`，并提示 `git config --global --add safe.directory <path>`。
4. **预期结果**：执行该配置后再次运行 `git status` 能正常工作，因为 `safe.directory_cb` 会把该路径标记为 `is_safe`。
5. 待本地验证（改属主需 root 权限；若无权限，纯阅读源码理解流程亦可）。

> 安全提示：不要为了绕过提示而 `safe.directory = *`（放行全部），这会关闭对其它目录的保护。实践中应只登记确信安全的路径。

#### 4.4.5 小练习与答案

**练习 1**：`safe.bareRepository` 配成 `explicit` 时，什么样的 bare 仓库仍可使用？
**答案**：用户**显式**指定的 bare 仓库（如通过 `GIT_DIR` 环境变量、`--git-dir` 选项，或当前工作目录就是该 bare 目录且 `is_implicit_bare_repo` 判定为真）。隐式发现的（在普通 `cd` 过程中撞上的）会被 `GIT_DIR_DISALLOWED_BARE` 拒绝，防止误操作。

**练习 2**：为什么所有权校验要同时检查 `gitfile`、`worktree`、`gitdir` 三个？
**答案**：因为这三者都可能被攻击者构造——一个看起来无害的 worktree 可能用 gitfile 指向一个别人拥有的、含恶意 hook 的 gitdir。只要其中任一要素属主不是当前用户且未被 `safe.directory` 放行，就视为可疑，拒绝继续。

---

### 4.5 发现结果如何写入环境（衔接下一讲）

#### 4.5.1 概念说明

本讲主题里有一句「把仓库路径写入环境」——本模块补上这最后一环。`setup_git_directory_gently_1` 只负责「找到」gitdir，把它真正**登记到**进程上下文的工作在 `setup_*_git_dir` 这几个分支函数里，最终落到 `set_git_dir_1`。

#### 4.5.2 核心流程

```
setup_git_directory_gently
  └─ (DISCOVERED 分支) setup_discovered_git_dir
        └─ set_git_dir(repo, gitdir, ...)
              └─ set_git_dir_1(repo, path)
                    ├─ xsetenv(GIT_DIR_ENVIRONMENT, path, 1)   ← 写入环境变量 GIT_DIR
                    └─ setup_git_env_internal(repo, path)      ← 初始化仓库内部路径
```

#### 4.5.3 源码精读

普通仓库命中后，`setup_discovered_git_dir` 会把工作树设为 `"."` 并登记 gitdir（[setup.c:1247-1250](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1247-L1250)）：

```c
set_git_work_tree(repo, ".");
if (strcmp(gitdir, DEFAULT_GIT_DIR_ENVIRONMENT))
    set_git_dir(repo, gitdir, 0);          /* gitdir 不是 ".git" 时显式登记 */
```

最终写入点是 `set_git_dir_1`（[setup.c:1079-1083](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1079-L1083)）：

```c
static void set_git_dir_1(struct repository *repo, const char *path)
{
    xsetenv(GIT_DIR_ENVIRONMENT, path, 1);
    setup_git_env_internal(repo, path);
}
```

`xsetenv` 把字符串 `"GIT_DIR"` 设进进程环境，于是后续所有 git 子进程（通过 [run-command](u14-l1-run-command.md) 派生的）都会继承这个变量；`setup_git_env_internal` 则把 gitdir 记进 `struct repository` 的内部字段。注意：若 gitdir 是相对路径，还会注册一个 `chdir_notify` 回调，保证后续 `chdir` 时同步修正相对 gitdir（[setup.c:1100-1114](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1100-L1114)）。

`environment.c` 里维护的 `local_repo_env[]` 列出了所有「仓库级」的 `GIT_*` 环境变量（含 `GIT_DIR`、`GIT_WORK_TREE`、`GIT_OBJECT_DIRECTORY` 等，[environment.c:95-112](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/environment.c#L95-L112)），它们正是发现阶段写入、后续命令读取的「仓库上下文信道」。

#### 4.5.4 代码实践

1. **实践目标**：验证 `GIT_DIR` 真的被写入了进程环境。
2. **操作步骤**：在仓库子目录运行 `git rev-parse --git-dir`，再运行 `GIT_DIR=.git git rev-parse --git-dir`（用 `GIT_DIR_EXPLICIT` 短路），对比两者输出。
3. **需要观察的现象**：显式设 `GIT_DIR` 时 git 不做上溯（对应 `setup.c:1569-1573` 的提前返回），直接用你给的值。
4. **预期结果**：两种方式得到的 gitdir 一致，但走的代码路径不同（一个 `DISCOVERED`，一个 `EXPLICIT`）。
5. 待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么发现普通仓库时要 `set_git_work_tree(repo, ".")`？
**答案**：因为此刻进程 cwd 已经被切到仓库根（或就在仓库根），工作树就是当前目录。设成 `"."` 让工作树跟随 cwd，与 gitdir 的相对关系保持一致。

**练习 2**：`local_repo_env[]` 数组末尾为什么是 `NULL`？
**答案**：它是一个以 `NULL` 结尾的字符串指针数组，方便用「遍历到 NULL 停止」的方式统一处理（例如在克隆/切换仓库上下文时整体清除或传递这些变量）。

## 5. 综合实践

把本讲的知识串起来，模拟「一次 `git status` 在 setup 阶段走过的路」：

1. 准备一个非 bare 仓库 `demo`，并在其中建嵌套子目录 `demo/a/b/c`，`cd demo/a/b/c`。
2. 运行 `git status`，确认它能正常工作（说明上溯成功）。
3. 用 `GIT_TRACE2_PERF=1 git status 2>&1 | grep -i setup` 观察 setup 相关的 trace2 事件（若你的 git 编译带 trace2）。
4. 把仓库根的 `.git` **临时**改名（如 `mv .git .git-bak`），再到子目录运行 `git status`，确认报 `not a git repository`（对应 `GIT_DIR_HIT_CEILING`）。然后 `mv .git-bak .git` 恢复。
5. 写一段 200 字以内的说明，按以下顺序描述源码路径：
   - 入口 `setup_git_directory`（[setup.c:2155](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2155)）
   - 主循环 `setup_git_directory_gently_1`（[setup.c:1609](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1609)）如何逐层拼 `.git`、调 `read_gitfile_gently` 与 `is_git_directory`
   - 命中后 `ensure_valid_ownership`（[setup.c:1414](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1414)）放行
   - 最后 `set_git_dir_1`（[setup.c:1079](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1079)）把 gitdir 写入 `GIT_DIR` 环境变量。

预期：你能用自己的话复现「cwd → 上溯 → 命中 → 校验 → 写环境」五步，并把每一步对应到一个具体的源码位置。

## 6. 本讲小结

- git 通过 `setup_git_directory` 家族从 cwd **向上逐层**搜索 `.git`，结果用 `enum discovery_result`（[setup.h:67-79](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.h#L67-L79)）精确分类。
- 主循环 `setup_git_directory_gently_1`（[setup.c:1609-1706](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1609-L1706)）复用同一个 `strbuf`，每层依次试「`.git` 文件 → `.git` 目录 → 当前目录是否 bare」，靠 `GIT_CEILING_DIRECTORIES` 与文件系统设备号设两道边界。
- `.git` 是文件时由 `read_gitfile_gently`（[setup.c:965-1044](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L965-L1044)）解析 `gitdir: <path>` 格式，并校验目标确实是仓库。
- `is_git_directory`（[setup.c:414-452](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L414-L452)）以「合法 HEAD + 可访问 objects + 可访问 refs」作为仓库的最低判据。
- bare 仓库受 `safe.bareRepository` 策略约束（[setup.c:1683-1692](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1683-L1692)），所有权校验 `ensure_valid_ownership`（[setup.c:1414-1444](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1414-L1444)）配合 `safe.directory` 防御可疑仓库。
- 发现结果最终经 `set_git_dir_1`（[setup.c:1079-1083](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L1079-L1083)）写入 `GIT_DIR` 环境变量，成为后续命令的仓库上下文。

## 7. 下一步学习建议

本讲只解决了「gitdir 在哪」，但 gitdir 里装的东西（对象库、引用、索引、配置）还没有被加载进内存。建议接着读：

- **[u2-l2 核心结构 struct repository](u2-l2-repository-struct.md)**：看 `setup_git_env_internal` 之后，`struct repository` 如何把 gitdir、对象数据库、引用存储、索引聚合为一个运行时上下文。这是承接本讲的自然下一步。
- **[u2-l3 git init 与仓库模板](u2-l3-git-init-templates.md)**：从「创建」视角看一个新仓库的 `.git` 骨架是怎么长出来的，能反向加深对「发现」阶段校验项（HEAD/objects/refs）的理解。
- 想深入工作树与多工作树，可先跳到 [u12-l2 多工作树 worktree](u12-l2-worktree.md)，结合本讲的 gitfile 机制理解链接工作树。
