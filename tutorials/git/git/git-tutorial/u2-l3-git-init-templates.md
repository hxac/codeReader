# git init 与仓库模板

## 1. 本讲目标

上一讲（u2-l1）我们讲了 git 在执行命令前如何「找到」一个已存在的仓库（`setup_git_directory` 向上搜索 `.git`）。本讲反过来看：**仓库是怎么被凭空创建出来的**。`git init` 是唯一一个能在磁盘上「无中生有」地造出 `.git` 目录的命令。

学完本讲，你应当能够：

1. 说清楚 `git init` 在磁盘上创建了哪些目录和文件（目录骨架）。
2. 说清楚 `.git/HEAD` 指向哪里、初始分支名从哪里来，以及 `config` 文件里那些默认键（`core.bare`、`core.filemode`、`core.logallrefupdates` 等）是哪段代码写进去的。
3. 说清楚 `templates/` 目录里的 sample hooks、`description`、`info/exclude` 是如何被复制进新仓库的，以及模板来源的四层优先级。

本讲只解决「把一个空仓库的 `.git` 摆出来」这件事，不涉及往仓库里写对象、写索引（那是第 3、4 单元的内容）。

## 2. 前置知识

- **gitdir / 工作树 / bare 仓库**：普通仓库里，`.git` 是「版本库目录」，它的父目录是「工作树」；bare 仓库（`--bare`）没有工作树，当前目录本身就是版本库。这些概念在 u2-l1 已建立。
- **struct repository**：git 运行时用一个 `struct repository`（见 u2-l2）表示「一个仓库」的上下文，持有 `gitdir`、对象库指针、引用存储等字段。本讲我们把这些字段**落盘**。
- **符号引用（symref）**：`HEAD` 是一个「符号引用」，它的内容是另一条引用的名字（例如 `ref: refs/heads/main`），而不是直接的对象哈希。
- **strbuf**：git 源码里到处可见的可变长字符串缓冲区，配合 `strbuf_addstr` / `strbuf_setlen` 做路径拼接与复用。本讲很多路径操作都靠它。
- **parse-options**：git 统一的命令行解析框架（`OPT_STRING`、`OPT_BIT` 等），把 `--bare`、`--template`、`-b` 等参数填进 C 变量。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [builtin/init-db.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/init-db.c) | `git init` 子命令入口 `cmd_init_db`，负责解析参数、决定 gitdir/工作树/bare，最后调用真正的建库函数 `init_db`。 |
| [setup.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c) | 建库的「主力」函数都在这里：`init_db`（总编排）、`create_default_files`（复制模板 + 写 config）、`create_reference_database`（建引用库 + 写 HEAD）、`create_object_directory`（建 objects 目录）、`copy_templates` / `copy_templates_1`（模板复制）、`get_template_dir`（模板来源解析）。 |
| [refs.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c) | `repo_default_branch_name`：解析初始分支名（`init.defaultBranch`，默认 `master`）。 |
| [templates/](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/templates) | 默认模板目录：`description`、`info/exclude`、`hooks/*.sample`。 |
| [templates/Makefile](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/templates/Makefile) | 构建期把 `templates/` 编译安装到 `share/git-core/templates`，并把 `#!` 行里的 shell 路径替换成实际路径。 |
| [environment.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/environment.h) | 定义环境变量名常量，如 `GIT_TEMPLATE_DIR`。 |
| [Documentation/git-init.adoc](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/git-init.adoc) | `git init` 官方手册，给出了模板来源的优先级顺序。 |

> 提示：`cmd_init_db` 只是「外壳」，真正干活的是 `setup.c` 里的 `init_db`。这体现了 git 源码的常见分工——`builtin/*.c` 负责参数解析与命令编排，核心逻辑下沉到非 builtin 的库文件，方便被 `git clone`、`git submodule` 等其他路径复用。

## 4. 核心概念与源码讲解

### 4.1 init_db 目录骨架创建

#### 4.1.1 概念说明

「初始化一个仓库」最底层的动作，就是在磁盘上摆出 `.git` 这棵目录树。一个全新的非 bare 仓库，其 `.git` 目录骨架大致是：

```
.git/
├── HEAD                 # 符号引用，指向初始分支
├── config               # 仓库级配置
├── description          # 给 gitweb 用的描述（模板复制来）
├── hooks/               # 示例钩子（*.sample，模板复制来）
├── info/
│   └── exclude          # 仓库级忽略规则（模板复制来）
├── objects/             # 对象数据库
│   ├── info/
│   └── pack/
└── refs/                # 引用存储（由引用后端创建）
```

本模块只关注「目录骨架」：谁负责建 `.git` 本身、谁建 `objects/` 及其子目录。`HEAD`、`config`、`hooks/` 的具体内容分别留到 4.3 和 4.2。

#### 4.1.2 核心流程

建库的总体编排由 [setup.c:2802](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2802-L2902) 的 `init_db` 完成，它的关键步骤是：

```
init_db(repo, git_dir, ...):
  1. set_git_dir(repo, git_dir, 1)            # 把 gitdir 路径登记进 struct repository
  2. check_repository_format_gently(...)       # 新仓库无 config，这里通常空过
  3. safe_create_dir(repo, git_dir, 0)         # ★ 创建 .git 目录本身
  4. reinit = create_default_files(...)        # 复制模板 + 写 config（见 4.2 / 4.3）
  5. create_reference_database(...)            # 建引用库 + 写 HEAD（见 4.3）
  6. create_object_directory(repo)            # ★ 创建 objects/、objects/pack、objects/info
  7. 打印 "Initialized empty Git repository in ..."
```

注意顺序上的一个细节：**目录与文件是交错创建的**——先建 `.git`，再在 `create_default_files` 里复制模板（模板里就带 `hooks/`、`info/` 子目录）和写 config，再写 HEAD，最后才建 `objects/`。所以你在 `init_db` 里看不到对 `hooks/`、`info/` 的显式 `mkdir`——它们是随模板复制一并出现的。

#### 4.1.3 源码精读

`git init` 命令在源码里叫 `cmd_init_db`（注意名字和真正的 `init_db` 函数很像，但它是 builtin 外壳）。它先用 `parse_options` 把 `--bare`、`--template`、`-b`、`--object-format`、`--shared` 等参数读进 C 变量，处理掉「目标目录不存在就先 mkdir」的逻辑，然后把所有参数打包交给 `init_db`：

[builtin/init-db.c:265-267](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/init-db.c#L265-L267) —— `cmd_init_db` 把决定好的 `git_dir`、`template_dir`、哈希算法、引用格式、初始分支等一股脑传给 `init_db`：

```c
flags |= INIT_DB_EXIST_OK;
ret = init_db(the_repository, git_dir, real_git_dir, template_dir, hash_algo,
              ref_storage_format, initial_branch,
              init_shared_repository, flags);
```

如果用户没指定 `git_dir`，`cmd_init_db` 会回退到默认的 `.git`：

[builtin/init-db.c:200-201](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/init-db.c#L200-L201) —— 没有 `GIT_DIR` 环境变量时，使用 `DEFAULT_GIT_DIR_ENVIRONMENT`（即 `.git`）。

进入 `init_db` 后，**建 `.git` 目录本身**只有一行：

[setup.c:2853-2860](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2853-L2860) —— 先 `safe_create_dir` 建 gitdir，再依次调用建默认文件、建引用库、建对象目录：

```c
safe_create_dir(repo, git_dir, 0);

reinit = create_default_files(repo, template_dir, original_git_dir,
                              &repo_fmt, init_shared_repository);

if (!(flags & INIT_DB_SKIP_REFDB))
    create_reference_database(repo, initial_branch, flags & INIT_DB_QUIET);
create_object_directory(repo);
```

`safe_create_dir` 是 git 对「创建目录」的统一封装：它先 `mkdir`，失败时根据错误码判断是「已存在」还是「权限/路径问题」，并在 `--shared` 等场景下调用 `adjust_shared_perm` 调整权限。第三个参数 `1` 表示「允许已存在」（用于子目录），`0` 表示对顶层 gitdir 更严格些。

`objects/` 子目录由 `create_object_directory` 建立：

[setup.c:2632-2648](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2632-L2648) —— 建立 `objects/`、`objects/pack/`、`objects/info/` 三个目录：

```c
safe_create_dir(repo, path.buf, 1);                 /* objects/        */
strbuf_setlen(&path, baselen);
strbuf_addstr(&path, "/pack");
safe_create_dir(repo, path.buf, 1);                 /* objects/pack/   */
strbuf_setlen(&path, baselen);
strbuf_addstr(&path, "/info");
safe_create_dir(repo, path.buf, 1);                 /* objects/info/   */
```

注意这里反复用 `strbuf_setlen(&path, baselen)` 把缓冲区「截回」到基长度，再 `strbuf_addstr` 拼接新的子路径——这是 git 源码里拼接兄弟路径的标准手法，避免反复 `malloc`。

> **与 u2-l2 的衔接**：`init_db` 第 1 步 `set_git_dir` 把 gitdir 字符串写进了 `struct repository`；第 6 步建好的 `objects/` 目录路径，对应 `struct repository` 里 `objects`（对象库）指针指向的根目录。至此，u2-l2 介绍的几个关键字段都有了真实的磁盘对应物。

#### 4.1.4 代码实践

**实践目标**：亲眼看到一个新仓库的目录骨架，并把它与源码里的建目录函数一一对应。

**操作步骤**：

1. 找一个空目录做实验：
   ```sh
   cd $(mktemp -d)
   ```
2. 运行（用刚从源码编译出的 git，确保和源码版本一致）：
   ```sh
   /path/to/git init sample
   ```
3. 列出骨架里的所有目录：
   ```sh
   find sample/.git -type d | sort
   ```
4. 列出骨架里的所有普通文件：
   ```sh
   find sample/.git -type f | sort
   ```

**需要观察的现象**：`find -type d` 应当输出 `sample/.git`、`sample/.git/hooks`、`sample/.git/info`、`sample/.git/objects`、`sample/.git/objects/info`、`sample/.git/objects/pack`、`sample/.git/refs`（及引用后端建出的子目录）。

**预期结果**：`objects/`、`objects/pack/`、`objects/info/` 这三个目录正好对应 `create_object_directory` 里的三次 `safe_create_dir`；而 `hooks/`、`info/` 不在 `init_db` 里显式出现——它们是模板复制（4.2）带来的。`refs/` 由 `create_reference_database` 经引用后端创建。

> 待本地验证：如果你用的是系统的 git 而非刚编译的，目录布局基本一致；但默认分支名、引用存储格式可能因版本/配置不同而略有差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `init_db` 里没有显式创建 `.git/hooks` 和 `.git/info` 目录的代码，但它们却出现在新仓库里？

**参考答案**：因为 `create_default_files` 在建目录之后会先调用 `copy_templates`，把默认模板里的 `hooks/`、`info/` 整棵子树复制进来，目录是随模板文件一起出现的。

**练习 2**：`create_object_directory` 里反复出现 `strbuf_setlen(&path, baselen)`，去掉它会怎样？

**参考答案**：`strbuf_addstr` 是追加，不去截回基长度的话，路径会越来越长（变成 `objects/pack`、`objects/pack/info` 这样错误嵌套），`mkdir` 会建出错误的目录树。`setlen` 是为了在同一个缓冲区上反复拼接「兄弟」路径。

---

### 4.2 templates/ 模板复制

#### 4.2.1 概念说明

新仓库里那些「每个仓库都一样、但又允许用户定制」的东西——示例钩子（`hooks/pre-commit.sample` 等）、给 gitweb 的 `description`、仓库级忽略规则 `info/exclude`——git 不在 C 代码里硬编码，而是放在一个**模板目录**里，建库时整棵复制进新 `.git`。

这样做的好处：

- **可定制**：用户可以准备自己的模板（比如团队统一的 hook），让每个新仓库都自动带上。
- **解耦**：这些文件本质上是「数据」而非「逻辑」，用文件复制代替 C 代码生成更清晰。
- **样例默认禁用**：钩子文件名带 `.sample` 后缀就不会被执行，去掉后缀才启用，安全又直观。

#### 4.2.2 核心流程

模板复制分两步：先**解析模板来源目录**，再**递归复制**。

**模板来源的四层优先级**（高 → 低，命中即止），由 `get_template_dir` 实现：

1. `--template=<dir>` 命令行参数；
2. `GIT_TEMPLATE_DIR` 环境变量；
3. `init.templateDir` 配置变量；
4. 编译进二进制的默认目录 `DEFAULT_GIT_TEMPLATE_DIR`（通常是 `/usr/share/git-core/templates`）。

> 这与官方手册 [Documentation/git-init.adoc:150-158](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/git-init.adoc#L150-L158) 给出的顺序完全一致。

**复制规则**（由 `copy_templates_1` 实现，对模板目录做深度优先遍历）：

- 跳过以 `.` 开头的条目（所以模板里的 `.gitignore` 这类不会被复制）。
- 模板条目是**目录**：在目标侧建同名目录，递归。
- 模板条目是**普通文件**：用 `copy_file` 复制（保留权限位）。
- 模板条目是**符号链接**：读出链接目标，在目标侧重建符号链接。
- 目标侧**已存在同名条目**：跳过（不覆盖用户已有的文件）。

复制的目标根是 **common dir**（`repo_get_common_dir(repo)`），对普通仓库就是 `.git` 本身。

#### 4.2.3 源码精读

模板来源解析在 [setup.c:2241-2264](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2241-L2264) 的 `get_template_dir`，四层回退清晰可见：

```c
const char *template_dir = option_template;                 /* 1. --template        */
if (!template_dir)
    template_dir = getenv(TEMPLATE_DIR_ENVIRONMENT);        /* 2. $GIT_TEMPLATE_DIR */
if (!template_dir) {
    /* 通过 git_protected_config 读 init.templatedir */      /* 3. init.templateDir  */
    ...
    template_dir = data.path;
}
if (!template_dir) {
    dir = system_path(DEFAULT_GIT_TEMPLATE_DIR);             /* 4. 编译期默认目录     */
    template_dir = dir;
}
```

其中第 3 层通过回调 [setup.c:2220-2226](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2220-L2226) 读取配置键 `init.templatedir`（注意源码里 key 是小写 `init.templatedir`，文档里写成 `init.templateDir`，git 配置键大小写不敏感）：

```c
static int template_dir_cb(const char *key, const char *value, ...)
{
    ...
    if (strcmp(key, "init.templatedir"))
        return 0;
    ...
}
```

> `git_protected_config` 是一类「受保护」的配置读取：`init.templatedir` 这类可能指向本地路径、有安全含义的配置，只在受信任的配置层级里读取，防止恶意仓库通过 `.git/config` 注入路径。这是 git 安全模型的一个细节。

复制入口在 [setup.c:2343-2394](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2343-L2394) 的 `copy_templates`。它先打开模板目录、校验模板的 `config` 文件「版本兼容」（防止用过老的模板污染新仓库），然后把目标根设为 common dir，交给 `copy_templates_1`：

```c
strbuf_addstr(&path, repo_get_common_dir(repo));
strbuf_complete(&path, '/');
copy_templates_1(repo, &path, &template_path, dir);
```

真正的递归复制在 [setup.c:2274-2341](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2274-L2341) 的 `copy_templates_1`。核心循环对每个目录条目分类处理：

```c
while ((de = readdir(dir)) != NULL) {
    ...
    if (de->d_name[0] == '.')            continue;   /* 跳过隐藏条目 */
    ...
    if (S_ISDIR(st_template.st_mode)) {              /* 目录：递归     */
        ...
        copy_templates_1(repo, path, template_path, subdir);
    }
    else if (exists)                     continue;   /* 目标已存在：跳过 */
    else if (S_ISLNK(st_template.st_mode)) { ... }   /* 符号链接：重建   */
    else if (S_ISREG(st_template.st_mode))           /* 普通文件：复制   */
        copy_file(path->buf, template_path->buf, st_template.st_mode);
}
```

被复制的具体文件清单，由 [templates/Makefile:35-51](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/templates/Makefile#L35-L51) 的 `TEMPLATES` 变量登记，正是我们开头看到的那批文件：

```makefile
TEMPLATES  =
TEMPLATES += description
TEMPLATES += hooks/applypatch-msg.sample
TEMPLATES += hooks/commit-msg.sample
TEMPLATES += hooks/pre-commit.sample
...
TEMPLATES += info/exclude
```

构建期（见 [templates/Makefile:53-65](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/templates/Makefile#L53-L65)）这些模板被 `sed` 把 shebang 行（`#!/bin/sh`）替换成实际的 `SHELL_PATH`，再 `chmod` 出可执行位，产物落在 `blt/`，安装时整棵 tar 到 `share/git-core/templates`。所以你在 `.git/hooks/` 里看到的 `pre-commit.sample` 不是源码原样复制的，而是构建期处理过的。

#### 4.2.4 代码实践

**实践目标**：用一个自定义模板验证模板复制机制，并看清默认模板复制了哪些文件。

**操作步骤**：

1. 准备一个自定义模板目录，放一个自定义 hook 和一个自定义文件：
   ```sh
   mkdir -p mytmpl/hooks
   echo '#!/bin/sh' > mytmpl/hooks/pre-commit.sample
   echo 'echo hello-from-template' >> mytmpl/hooks/pre-commit.sample
   echo 'this is a custom template file' > mytmpl/README-template
   ```
2. 用自定义模板初始化：
   ```sh
   /path/to/git init --template=mytmpl withtmpl
   ```
3. 检查自定义文件是否被复制：
   ```sh
   cat withtmpl/.git/README-template
   ls withtmpl/.git/hooks
   ```
4. 对比默认模板初始化的仓库里有哪些 sample hooks：
   ```sh
   /path/to/git init defaulttmpl
   ls defaulttmpl/.git/hooks
   ```

**需要观察的现象**：`withtmpl/.git/README-template` 内容是你写的那行；`withtmpl/.git/hooks/pre-commit.sample` 来自你的自定义模板（且因为模板里没有别的 sample，所以只有它一个）。`defaulttmpl/.git/hooks` 里则是默认的那十几个 `*.sample`。

**预期结果**：自定义模板会**完全替代**默认模板（不是合并）——`get_template_dir` 是四选一，命中其一就不再回退。所以 `withtmpl` 里**没有** `info/exclude`、`description` 这些默认模板才有的文件。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `copy_templates_1` 要 `if (exists) continue;`（目标已存在就跳过），而不是覆盖？

**参考答案**：为了在 `git init` 重新初始化（reinit）一个已有仓库时不破坏用户已有的定制。比如用户已经把 `pre-commit.sample` 改名成 `pre-commit` 并编辑过，reinit 时绝不能用模板覆盖它。

**练习 2**：模板里放一个名为 `.hidden` 的文件，`git init --template` 后它会被复制吗？

**参考答案**：不会。`copy_templates_1` 里 `if (de->d_name[0] == '.') continue;` 会跳过所有以点开头的条目。这也是模板目录里可以有 `.gitignore`（用于忽略构建产物 `blt/`）而不被当成模板复制的原因。

---

### 4.3 默认 HEAD 与 config 写入

#### 4.3.1 概念说明

骨架目录建好、模板复制完之后，新仓库还缺两样「灵魂」：

1. **`HEAD`**：一个符号引用，指明「当前在哪个分支」。新仓库还没有任何提交，所以 `HEAD` 只能指向一条**尚不存在的分支**（例如 `ref: refs/heads/master`）。这条分支的「名字」就是初始分支名。
2. **`config`**：一组仓库级默认配置，记录这个仓库的格式版本、是否 bare、文件系统是否支持可执行位/符号链接/大小写等能力探测结果。

这两样都是 git 根据运行环境**动态生成**的（不像 hooks 是静态复制的），所以放在 C 代码里写。

#### 4.3.2 核心流程

`init_db` 把这两件事分给两个函数：

```
create_default_files(repo, ...):
  1. copy_templates(...)            # 先复制模板（模板里可能自带 config，4.2 已讲）
  2. repo_config_clear + 重读 config # 模板可能改了 config，清缓存重读
  3. initialize_repository_version  # 写 core.repositoryformatversion
  4. 探测并写 core.filemode         # 文件系统是否可信地保留可执行位
  5. 写 core.bare                   # 是否 bare
  6. 非 bare 时写 core.logallrefupdates = true
  7. 非 bare 且需要时写 core.worktree
  8. 首次初始化时探测并写 core.symlinks / core.ignorecase

create_reference_database(repo, initial_branch, ...):
  1. ref_store_create_on_disk(...)  # 由引用后端建出 refs/ 结构（files 后端建 refs/heads 等）
  2. 非 reinit 时：把 HEAD 指向 refs/heads/<初始分支>
```

**初始分支名的来源**（`repo_default_branch_name`）：

1. `init.defaultBranch` 配置变量；
2. 否则硬编码 `master`（若编译时开了 `WITH_BREAKING_CHANGES` 则是 `main`）。

#### 4.3.3 源码精读

`create_default_files` 的开头就把模板复制放在最前，并特意解释了原因——**模板里可能自带一个 config 文件**，要先装好再读：

[setup.c:2544-2556](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2544-L2556)：

```c
/*
 * First copy the templates -- we might have the default
 * config file there, in which case we would want to read
 * from it after installing.
 * ...
 */
copy_templates(repo, template_path);
repo_config_clear(repo);
repo_settings_reset_shared_repository(repo);
repo_config(repo, git_default_config, NULL);
```

`repo_config_clear` + 重读这一步非常关键：模板可能刚把一个 `config` 写到磁盘上，必须清掉内存里缓存的配置再重新读，否则后续判断会用到过期的（空的）配置。

随后是一组「能力探测 + 写 config」的代码。最有代表性的是 `core.filemode` 的探测——git 需要知道当前文件系统是否**可信地**区分可执行位（有些文件系统会丢失或伪造可执行位）：

[setup.c:2580-2592](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2580-L2592)：

```c
repo_git_path_replace(repo, &path, "config");
filemode = TEST_FILEMODE;
if (TEST_FILEMODE && !lstat(path.buf, &st1)) {
    struct stat st2;
    filemode = (!chmod(path.buf, st1.st_mode ^ S_IXUSR) &&   /* 翻转可执行位 */
            !lstat(path.buf, &st2) &&
            st1.st_mode != st2.st_mode &&                    /* 看是否真的变了 */
            !chmod(path.buf, st1.st_mode));                  /* 再翻回来 */
    ...
}
repo_config_set(repo, "core.filemode", filemode ? "true" : "false");
```

紧接着是 `core.bare` 与 `core.logallrefupdates`：

[setup.c:2594-2607](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2594-L2607)：

```c
if (is_bare_repository(repo))
    repo_config_set(repo, "core.bare", "true");
else {
    repo_config_set(repo, "core.bare", "false");
    /* allow template config file to override the default */
    if (repo_config_get_string_tmp(repo, "core.logallrefupdates", &value))
        repo_config_set(repo, "core.logallrefupdates", "true");
    ...
}
```

注意 `core.logallrefupdates` 的写法：只有当配置里**还没有**这个键时才写默认值 `true`——这正是「允许模板自带的 config 覆盖默认」的体现（注释也这么写）。

首次初始化（非 reinit）时还会探测符号链接和大小写敏感性：

[setup.c:2609-2626](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2609-L2626) —— 通过在 `.git` 里尝试建一个符号链接、再尝试访问大小写变体文件名 `CoNfIg` 来探测文件系统能力，能力不足时写 `core.symlinks=false` / `core.ignorecase=true`。

HEAD 的写入在 `create_reference_database` 里：

[setup.c:2493-2522](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2493-L2522)：

```c
void create_reference_database(struct repository *repo,
                               const char *initial_branch, int quiet)
{
    ...
    int reinit = is_reinit(repo);
    if (ref_store_create_on_disk(get_main_ref_store(repo), 0, &err))  /* 建 refs/ 结构 */
        die("failed to set up refs db: %s", err.buf);

    if (!reinit) {
        char *ref;
        if (!initial_branch)
            initial_branch = to_free =
                repo_default_branch_name(repo, quiet);                /* 解析初始分支名 */
        ref = xstrfmt("refs/heads/%s", initial_branch);
        ...
        if (refs_update_symref(get_main_ref_store(repo), "HEAD", ref, NULL) < 0)
            ...                                                       /* 写 HEAD 符号引用 */
    }
    ...
}
```

`ref_store_create_on_disk` 把建 `refs/` 目录结构的工作**委托给当前引用后端**（默认 files 后端；reftable 后端则建自己的二进制文件）——这是 u5 会展开的引用后端抽象。`refs_update_symref` 则把 `HEAD` 的内容写成 `ref: refs/heads/<初始分支>`。

`is_reinit` 通过「`.git/HEAD` 是否已存在」判断是不是重新初始化：

[setup.c:2481-2491](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2481-L2491) —— 已有 HEAD 就视为 reinit，reinit 时不重写 HEAD（保留用户当前所在分支）。

初始分支名由 [refs.c:691-720](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L691-L720) 的 `repo_default_branch_name` 决定：

```c
const char *config_key = "init.defaultbranch";
...
if (!ret && repo_config_get_string(r, config_key, &ret) < 0)
    die(_("could not retrieve `%s`"), config_display_key);

if (!ret) {
#ifdef WITH_BREAKING_CHANGES
    ret = xstrdup("main");
#else
    ret = xstrdup("master");
#endif
    ...
}
```

所以默认分支名就是 `init.defaultBranch`（默认 `master`），而 `git init -b <name>` 或 `--initial-branch=<name>` 传进来的 `initial_branch` 会**覆盖**它（见上面 `create_reference_database` 里 `if (!initial_branch)` 的判断）。

#### 4.3.4 代码实践

**实践目标**：观察新仓库的 `HEAD` 与默认 `config`，并验证初始分支名与默认配置键的来源。

**操作步骤**：

1. 新建一个仓库并查看 HEAD：
   ```sh
   cd $(mktemp -d)
   /path/to/git init demo
   cat demo/.git/HEAD
   ```
2. 查看仓库级 config（只看 local）：
   ```sh
   /path/to/git -C demo config --list --local
   ```
3. 用 `-b` 指定初始分支再建一个：
   ```sh
   /path/to/git init -b develop demo2
   cat demo2/.git/HEAD
   ```
4. 用 `init.defaultBranch` 配置再建一个（注意：建库前就要设好全局配置）：
   ```sh
   /path/to/git -c init.defaultBranch=trunk init demo3
   cat demo3/.git/HEAD
   ```

**需要观察的现象**：

- `demo/.git/HEAD` 内容形如 `ref: refs/heads/master`（或你编译版本的默认分支）。
- `demo/.git/config` 里有 `core.repositoryformatversion`、`core.filemode`、`core.bare = false`、`core.logallrefupdates = true` 等键。
- `demo2/.git/HEAD` 是 `ref: refs/heads/develop`。
- `demo3/.git/HEAD` 是 `ref: refs/heads/trunk`。

**预期结果**：`-b` 的优先级最高（直接作为 `initial_branch` 传入，跳过 `repo_default_branch_name`）；`init.defaultBranch` 在没有 `-b` 时生效。`core.filemode` 的值取决于文件系统（Linux 原生 ext4 通常是 `true`）。

> 待本地验证：`core.filemode` / `core.symlinks` / `core.ignorecase` 的具体取值依赖你运行 git 的文件系统，不同环境结果可能不同。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `create_default_files` 要在 `copy_templates` 之后立刻 `repo_config_clear` 再重读 config？

**参考答案**：因为模板可能自带了一个 `config` 文件并被复制到 `.git/config`。内存里的配置缓存还是空的（建库前没有 config），必须清掉缓存、重新从磁盘读，才能让后续的 `core.logallrefupdates` 等判断看到模板带进来的配置。

**练习 2**：在一个已有仓库里再次运行 `git init`，`HEAD` 会被重置成默认分支吗？

**参考答案**：不会。`is_reinit` 检测到 `.git/HEAD` 已存在，`create_reference_database` 在 reinit 分支里跳过 `refs_update_symref`，保留你当前所在的分支。这正是「`git init` 是幂等的、安全的」的来源。

**练习 3**：`core.filemode` 的探测为什么用「翻转可执行位再看是否真变」的方式，而不是直接信任 `st_mode`？

**参考答案**：有些文件系统（如某些挂载选项下的 FAT、网络文件系统）不可靠地存储或返回可执行位——可能对所有文件都报可执行，或完全丢失。git 通过实际 `chmod` 后再 `stat` 比对，来判定该文件系统是否「可信地」反映可执行位，从而决定索引里是否记录该位。

## 5. 综合实践

把本讲三个模块串起来：**用自定义模板初始化一个仓库，验证模板复制、HEAD、config 三者都按预期生成。**

1. 准备一个自定义模板，包含一个「自定义默认 config」和一个示例 hook：
   ```sh
   T=$(mktemp -d)
   mkdir -p $T/hooks $T/info
   # 模板自带的 config：会被 create_default_files 读到
   printf '[core]\n\tlogallrefupdates = false\n' > $T/config
   printf 'my-project\n' > $T/description
   printf '#!/bin/sh\necho custom-hook-ran\n' > $T/hooks/pre-commit.sample
   printf '*.log\n' > $T/info/exclude
   ```
2. 用该模板 + 指定初始分支初始化：
   ```sh
   /path/to/git init --template=$T -b main proj
   ```
3. 验证三件事，并分别对应到本讲的源码：
   - **模板复制**：`cat proj/.git/description`（应是 `my-project`）、`ls proj/.git/hooks`（应有 `pre-commit.sample`）。
   - **HEAD**：`cat proj/.git/HEAD`（应是 `ref: refs/heads/main`，来自 `-b main`）。
   - **config 合并**：`git -C proj config --get core.logallrefupdates`。

   重点观察第 3 项：模板 config 写了 `logallrefupdates = false`，而 [setup.c:2602-2603](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c#L2602-L2603) 的逻辑是「只有配置里**还没有**这个键时才写默认 `true`」。模板已经提供了该键，所以 git **不会**覆盖它——最终值应是 `false`。这就验证了「模板 config 可覆盖默认、但探测类键（filemode 等）仍由 git 写入」的分工。
4. 最后列出 `proj/.git` 的完整目录树，逐项标注每个文件/目录是「骨架（safe_create_dir / create_object_directory）」「模板复制（copy_templates）」还是「动态写入（create_default_files / create_reference_database）」三者中的哪一个。

> 这个练习同时检验了 4.1 的目录骨架、4.2 的模板复制（含 config 自带与跳过已存在）、4.3 的 HEAD 与 config 默认键生成，是本讲的收尾。

## 6. 本讲小结

- `git init` 的真正建库逻辑在 [setup.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/setup.c) 的 `init_db`，`builtin/init-db.c` 的 `cmd_init_db` 只做参数解析与目录准备。
- 目录骨架由 `safe_create_dir(git_dir)` 与 `create_object_directory`（建 `objects/`、`objects/pack/`、`objects/info/`）完成；`hooks/`、`info/` 等子目录是随模板复制出现的，不在 `init_db` 里显式 mkdir。
- 模板来源有四层优先级：`--template` → `$GIT_TEMPLATE_DIR` → `init.templateDir` → 编译期默认目录；命中其一即止（不合并）。复制时跳过隐藏文件、不覆盖已存在文件。
- `config` 由 `create_default_files` 动态写入：先复制模板（模板可自带 config），清缓存重读，再写 `core.repositoryformatversion`、探测写入 `core.filemode` / `core.symlinks` / `core.ignorecase`，以及 `core.bare` / `core.logallrefupdates`。
- `HEAD` 由 `create_reference_database` 写成指向 `refs/heads/<初始分支>` 的符号引用；初始分支名取自 `-b`/`--initial-branch`，否则 `init.defaultBranch`，再否则默认 `master`（或编译期 `main`）。reinit 时不重写 HEAD。
- 整个 `git init` 是**幂等**的：`is_reinit` 依据 `.git/HEAD` 是否存在，决定哪些步骤跳过，因此对已有仓库重复 `git init` 是安全的。

## 7. 下一步学习建议

本讲把 `.git` 的「空壳」搭好了，但里面还没有任何对象、索引或真正的引用内容。建议接下来：

- **第 3 单元（对象模型与存储）**：往这个空仓库里写第一个对象。可从 `git hash-object -w` 入手，对照 `object-file.c` 的 `write_object_file`，看本讲建的 `objects/` 目录如何接收松散对象。
- **第 4 单元（索引 index）**：`git add` 之后 `.git/index` 出现，对照 `read-cache.c` 理解索引格式——它正是工作树与对象数据库之间的桥梁。
- **第 5 单元（引用 refs）**：本讲里 `ref_store_create_on_disk` 把建 `refs/` 的细节委托给了引用后端。第 5 单元会展开 files / packed / reftable 三种后端，你会看清 `create_reference_database` 真正在磁盘上建出了什么。
- 若想立刻动手验证本讲内容，建议先完成「综合实践」，把模板、HEAD、config 三条线在同一个仓库里跑通，再进入对象存储会更踏实。
