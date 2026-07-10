# submodule 机制：把外部仓库作为 gitlink 嵌入主仓库

## 1. 本讲目标

本讲是「子模块与多工作树」单元（u12）的第一讲，专门讲清一个看似简单、实则绕人的问题：**git 如何把「另一个完整仓库」嵌进当前仓库，又如何记住它应该停在哪个提交上**。

读完本讲，你应当能够回答：

1. **数据模型层**——子模块在主仓库（superproject，超项目）里到底以什么形式存在？什么是 **gitlink**（`0160000` 模式的 tree 条目），它和普通的 blob、tree 有什么区别？为什么光有一个 gitlink 还不够，必须再配一个 `.gitmodules` 文件？
2. **磁盘布局层**——子模块的 Git 目录为什么会「搬家」到 `$GIT_DIR/modules/<name>`，工作区里那个 `.git`「文件」又是什么？`connect_work_tree_and_git_dir` 是怎么把它们连起来的？
3. **配置加载层**——`submodule-config.c` 用什么数据结构缓存 `.gitmodules`？为什么缓存的 key 里要带 `.gitmodules` 这个 blob 的 OID？`submodule_from_path` 如何做到「按任意历史版本查询某条路径当时绑定的是哪个子模块」？
4. **操作流程层**——`git submodule add / init / update / sync / absorbgitdirs` 这几条命令在源码里分别走了哪条链路？为什么 `init` 要把 `.gitmodules` 的内容「抄写」一遍到 `.git/config`？

> 说明：本讲只覆盖子模块自身的嵌入模型、配置缓存与基本操作命令。子模块的 **fetch/push 递归协商**建立在 u11-l1 讲过的传输层之上，本讲只点到为止；多工作树（worktree）的共享对象库模型留待 **u12-l2**。

## 2. 前置知识

- **四种对象类型与 tree 对象（来自 u3-l1）**：仓库里一切内容都是对象，目录被记成 **tree** 对象，tree 的每一条目是 `(mode, name, oid)` 三元组。子模块在 tree 里也是一条目，但它的 `mode` 与 `oid` 都很特殊——这是本讲的起点。
- **索引 cache_entry（来自 u4-l1）**：工作区与对象库之间的桥梁是索引，每条 `cache_entry` 有 `ce_mode`。子模块在索引里也是一条 `cache_entry`，模式同样是 `0160000`。
- **引用 refs 与 commit OID（来自 u5-l1、u7-l1）**：子模块嵌入主仓库时，主仓库只保存子模块的**某个 commit 的 OID**，并不保存子模块的历史对象。理解「OID 指向一个 commit」至关重要。
- **gitfile（来自 u2-l1）**：仓库发现阶段讲过，`.git` 可以是一个普通文件，内容形如 `gitdir: <真实 git 目录路径>`。子模块的工作区正是用这种 gitfile 指向被「吸收」进 `modules/` 的真实 Git 目录。
- **配置回调模型（来自 u6-l1）**：`git_config_from_mem` 逐键值对回调 `config_fn_t`。`.gitmodules` 本质上就是一份 git 配置文件，它的解析完全复用 u6-l1 讲过的配置解析器。
- **传输层（来自 u11-l1）**：子模块的克隆、抓取都是 spawn 一个 `git` 子进程去跑 `clone`/`fetch`，经 `prepare_submodule_repo_env` 准备干净环境后执行。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的关键符号 |
| --- | --- | --- |
| `object.h` | 定义 gitlink 的特殊模式常量与模式规范化 | `S_IFGITLINK`、`S_ISGITLINK`、`create_ce_mode`、`canon_mode` |
| `environment.h` | `.gitmodules` 在源码中的三种「寻址」常量 | `GITMODULES_FILE`、`GITMODULES_INDEX`、`GITMODULES_HEAD` |
| `submodule-config.h` | 子模块配置缓存的公共 API 与 `struct submodule` | `struct submodule`、`submodule_from_path`、`submodule_from_name`、`repo_read_gitmodules` |
| `submodule-config.c` | `.gitmodules` 的缓存、解析、按版本查询 | `struct submodule_cache`、`config_from`、`parse_config`、`check_submodule_name`、`check_submodule_url` |
| `submodule.h` / `submodule.c` | 子模块运行时辅助：gitdir 定位、环境准备、吸收、校验 | `submodule_to_gitdir`、`submodule_name_to_gitdir`、`is_submodule_populated_gently`、`absorb_git_dir_into_superproject`、`prepare_submodule_repo_env`、`is_writing_gitmodules_ok` |
| `dir.c` | 把子模块工作区与 `modules/` 里的 Git 目录用 gitfile 连起来 | `connect_work_tree_and_git_dir`、`relocate_gitdir` |
| `builtin/submodule--helper.c` | `git submodule <cmd>` 的全部 C 实现 | `cmd_submodule__helper`、`module_add`、`add_submodule`、`clone_submodule`、`configure_added_submodule`、`init_submodule`、`sync_submodule` |
| `Documentation/gitsubmodules.adoc` | 子模块模型的权威说明 | 「A submodule is a repository embedded inside another repository」 |

## 4. 核心概念与源码讲解

本讲按**自底向上**的顺序拆三个最小模块：先讲子模块在数据模型上长什么样（gitlink + `.gitmodules` 两条腿），再讲这份配置如何被读取和缓存，最后讲 `git submodule` 系列命令在源码里的完整操作链路。

### 4.1 gitlink 与 .gitmodules：子模块的两条腿

#### 4.1.1 概念说明

先建立一个最关键、也最容易被误解的认知：

> **主仓库并不存储子模块的内容对象，它只存储「子模块应该停在哪个 commit 上」这一个指针。**

官方文档 `Documentation/gitsubmodules.adoc` 把这一点说得非常清楚：

[Documentation/gitsubmodules.adoc:20-37](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/gitsubmodules.adoc#L20-L37) —— 这段话定义了子模块的完整模型：一个子模块由三部分组成——(i) 位于超项目 `$GIT_DIR/modules/` 下的 Git 目录；(ii) 超项目工作区内的工作目录；(iii) 工作目录根部那个指向 (i) 的 `.git` 文件。

关键句是：

> The superproject tracks the submodule via a `gitlink` entry in the tree … The `gitlink` entry contains the object name of the commit that the superproject expects the submodule's working directory to be at.

也就是说，子模块在主仓库的 tree 对象里，就是一条普通 tree 条目，但它有两个「不普通」之处：

1. **模式是 `0160000`（`S_IFGITLINK`）**，与普通文件、符号链接、目录都不同。
2. **OID 指向的不是 blob 也不是 tree，而是一个 commit**——而且是**子模块仓库里的 commit**，这个对象在主仓库的对象库里根本不存在！

这意味着：光靠主仓库自己的对象库，你**无法**还原出这个 commit 的内容。要真正拿到子模块的代码，你还得知道**去哪里克隆**这个子模块仓库。这个「去哪里克隆」的信息，gitlink 自己存不下（它只是一个 commit OID），于是需要**第二条腿**：`.gitmodules` 文件。

所以子模块的「绑定」是由两样东西共同描述的，缺一不可：

| 载体 | 位置 | 存什么 | 回答的问题 |
| --- | --- | --- | --- |
| **gitlink** | tree 对象的一条目（也在索引里） | 子模块 commit 的 OID（模式 `0160000`） | 「子模块该停在哪个提交？」 |
| **`.gitmodules`** | tree 对象里的一个普通 blob 文件 | `submodule.<name>.{path,url,branch,...}` | 「这个 commit 去哪克隆？名字叫什么？路径在哪？」 |

一个常见的困惑：**name（名字）和 path（路径）有什么区别？** 它们可以不同。`name` 是 `.gitmodules` 里 `[submodule "名字"]` 小节的键，是子模块的**逻辑身份**，一旦设定一般不再变；`path` 是它在工作区里的**检出到哪里**，是会变的。把 name 与 path 解耦，正是为了允许「同一个子模块换路径」而不丢身份。这在本讲的缓存设计里会再次体现——缓存同时支持按 name 和按 path 两种 key 查询。

#### 4.1.2 核心流程

把上述模型落到磁盘上，一个「被吸收（absorbed）」的子模块长这样：

```
superproject/                    # 主仓库工作区
├── .git/                        # 主仓库的 Git 目录
│   └── modules/
│       └── <name>/              # ← 子模块的 Git 目录搬到这里了
│           ├── HEAD
│           ├── config
│           ├── objects/         # 子模块自己的对象库（独立！）
│           └── refs/
├── path/to/sub/                 # 子模块工作区（检出在这里）
│   ├── .git                     # ← 这是个「文件」，不是目录！
│   │                            #   内容: gitdir: ../../../.git/modules/<name>
│   └── (子模块检出的文件)
└── .gitmodules                  # 普通文件，被版本控制
```

要点：

1. **子模块的 Git 目录不在 `<path>/.git`，而在主仓库的 `.git/modules/<name>/` 下。** 这叫「吸收」。好处是：即使你删掉子模块工作区（`path/to/sub/`），子模块的 Git 目录和历史仍然安全地待在主仓库里，随时可以重新检出。
2. **`<path>/.git` 是一个 gitfile**（普通文件），内容是一行 `gitdir: <相对路径>`，指向 `modules/<name>`。这就是 u2-l1 讲过的 gitfile 机制在子模块上的应用。
3. **子模块的对象库与主仓库完全独立**——它们不共享 objects/。这也是为什么子模块能各自独立浅克隆、独立设置访问权限。
4. **`.gitmodules` 是被版本控制的普通文件**，它会和主仓库的其它文件一样进入索引、进入历史。

为什么 gitlink 的模式偏偏是 `0160000`？源码注释解释了这个取值的来由：

> A "directory link" is a link to another git directory. The value 0160000 is not normally a valid mode, and also just happens to be S_IFDIR + S_IFLNK

`0160000` = `S_IFDIR`（`0040000`）+ `S_IFLNK`（`0120000`）——字面意思就是「目录链接」，巧妙地复用了两个本不该相加的位，得到一个在任何正常文件系统里都不会出现的模式值，从而不会和真实文件混淆。

#### 4.1.3 源码精读

**gitlink 模式常量**定义在 `object.h`：

[object.h:115-131](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h#L115-L131) —— `S_IFGITLINK` 取值 `0160000`，`S_ISGITLINK(m)` 用 `S_IFMT` 掩码判断。注意紧随其后的 `object_type(mode)`：对 gitlink 模式直接返回 `OBJ_COMMIT`，这就是「tree 里这条目的 OID 是个 commit」在源码层面的根据。

**模式规范化**`create_ce_mode` 把磁盘/树的原始模式规整为索引能存的 `ce_mode`：

[object.h:134-154](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h#L134-L154) —— 当模式是目录或 gitlink 时，`create_ce_mode` 返回 `S_IFGITLINK`；`canon_mode` 更是「凡不是 reg/lnk/dir 的，统统归为 gitlink」。这解释了为什么索引里子模块条目的 `ce_mode` 永远是干净的 `0160000`。

**`.gitmodules` 的三种寻址常量**在 `environment.h`：

[environment.h:30-32](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/environment.h#L30-L32) —— 同一份 `.gitmodules` 有三个「视角」：工作区里的文件 `".gitmodules"`、索引里的条目 `":.gitmodules"`、HEAD 里的版本 `"HEAD:.gitmodules"`。后续 `config_from_gitmodules` 和 `is_writing_gitmodules_ok` 会用到这三个视角的取舍。

**把工作区与 `modules/` 连起来**的 `connect_work_tree_and_git_dir`，是子模块磁盘布局的核心：

[dir.c:4108-4146](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/dir.c#L4108-L4146) —— 它做两件事：(1) 在 `<工作区>/.git` 写入一行 `gitdir: <到 modules/<name> 的相对路径>`（用 `relative_path` 算相对路径，保证可移植）；(2) 在子模块 Git 目录的 `config` 里写入 `core.worktree = <反向相对路径>`，让子模块知道自己挂在哪个工作区下。这样工作区与 Git 目录就双向绑定好了。

#### 4.1.4 代码实践

**实践目标**：亲手验证「gitlink 是 commit 指针」「`.gitmodules` 是普通被版本控制的文件」「`<path>/.git` 是 gitfile」这三件事。

**操作步骤**（在一个临时目录里做，不会污染你的真实仓库）：

1. 建一个将被当作子模块的「外部仓库」：
   ```sh
   mkdir -p /tmp/sm-demo && cd /tmp/sm-demo
   git init sub-upstream && cd sub-upstream
   echo "hello from sub" > readme.txt
   git add readme.txt && git commit -q -m "sub init"
   cd ..
   ```
2. 建主仓库并把它加为子模块：
   ```sh
   git init super && cd super
   git -c protocol.file.allow=always submodule add ../sub-upstream mylib
   git commit -q -m "add submodule"
   ```
3. 观察数据模型——这是本实践的核心：
   ```sh
   # (a) 看 tree 里的 gitlink 条目：模式 0160000 + 类型 commit
   git ls-tree HEAD mylib
   # (b) 看索引里的同一条目
   git ls-files --stage mylib
   # (c) 看 .gitmodules 内容
   cat .gitmodules
   # (d) 看工作区里的 .git 是「文件」而非目录
   file mylib/.git
   cat mylib/.git
   # (e) 看被吸收进 modules/ 的真实 Git 目录
   ls .git/modules/
   ```

**需要观察的现象 / 预期结果**：

- `git ls-tree HEAD mylib` 输出形如 `160000 commit <40 位哈希> mylib`——注意类型是 **commit** 而非 blob/tree。
- `git ls-files --stage mylib` 的模式字段同样是 `160000`，且 stage 为 0。
- `.gitmodules` 里有 `[submodule "mylib"]` 小节，含 `path = mylib` 和 `url = ../sub-upstream`。
- `mylib/.git` 是一个**普通文件**，内容形如 `gitdir: ../../.git/modules/mylib`。
- `.git/modules/` 下能看到 `mylib/` 目录，里面有完整的 `HEAD`、`config`、`objects/`、`refs/`——子模块的真正 Git 目录。

> 如果你没有联网或环境受限，第 2 步可能因为 `protocol.file.allow` 默认拒绝本地路径而失败。加 `-c protocol.file.allow=always` 即可。若仍无法运行，可改用「源码阅读型实践」：阅读 `git ls-tree` 的输出格式约定（`Documentation/git-ls-tree.adoc`），对照本节源码确认 `160000` 这个模式号来自 `S_IFGITLINK`。**待本地验证**实际输出。

#### 4.1.5 小练习与答案

**练习 1**：假如把 `mylib` 子模块当前指向的 commit 记成 `C1`。现在你在 `mylib/` 里 `git checkout` 切到另一个分支、提交了新 commit `C2`，但**没有**回到主仓库重新 `git add mylib`。此时 `git ls-tree HEAD mylib`（在主仓库）会显示哪个 OID？`git status`（在主仓库）又会有什么提示？

**答案**：`git ls-tree HEAD mylib` 仍显示 `C1`（HEAD 没变，因为主仓库还没把 `C2` 记进 gitlink）。但 `git status` 会提示 `mylib` 有「新提交」，因为索引里记录的 gitlink 仍是 `C1`，而子模块工作区的 HEAD 已经移到 `C2`，二者不一致。只有当你在主仓库 `git add mylib` 后，索引与 HEAD 的 gitlink 才会更新为 `C2`。

**练习 2**：为什么子模块的模式值 `0160000` 不会和「一个真实存在的普通目录」或「普通可执行文件」冲突？

**答案**：因为 `0160000 = S_IFDIR | S_IFLNK`，在正常文件系统里这不是任何合法文件类型位（普通文件是 `100000`/`100755`，目录是 `040000`，符号链接是 `120000`）。git 选这个值正是为了让它在任何真实 inode 上都不会出现，从而 `S_ISGITLINK` 的判定既安全又无歧义。

**练习 3**：`name` 和 `path` 可以不同。请设想一个场景说明这种解耦的好处。

**答案**：例如子模块原来检出到 `vendor/libfoo`，后来想挪到 `third_party/libfoo`。只要 `.gitmodules` 里 `name` 仍是 `libfoo`（逻辑身份不变），只改 `path` 字段并 `git mv`，子模块的 Git 目录 `.git/modules/libfoo`、它的远程配置、历史都无需重建。若 name 与 path 强绑定，换路径就等于换了一个全新子模块。

---

### 4.2 子模块配置加载：submodule-config.c 的缓存

#### 4.2.1 概念说明

上一节讲过，子模块的元信息存在 `.gitmodules` 里。但 `.gitmodules` 有一个微妙之处：**它是一个被版本控制的文件，不同历史提交里它的内容可能不一样。**

想象一个需求：你想知道「三天前那个提交里，`vendor/libfoo` 这个路径绑定的是哪个子模块、URL 是什么」。你不能只读工作区里当前的 `.gitmodules`，而必须去读**那个提交里的** `.gitmodules` blob。`git log --submodule`、`git diff` 展示子模块变化时，正是这么做的。

`submodule-config.c` 提供的缓存 API，就是为这个需求设计的。它官方文档（`submodule-config.h` 顶部注释）这样描述：

> The submodule config cache API allows to read submodule configurations/information from **specified revisions**. Internally information is lazily read into a cache … Lookups can be done by submodule path or name.

三个关键词：**按版本查询（specified revisions）**、**懒加载（lazily）**、**按 path 或 name 双 key 查询**。

实现思路是把「每一份不同的 `.gitmodules`」都解析结果缓存起来，**用这份 `.gitmodules` blob 的 OID 当作缓存 key 的一部分**。这样：

- 工作区当前的 `.gitmodules`（一份 blob）→ 一个 OID → 一组解析结果。
- 某个历史提交的 `.gitmodules`（另一份 blob）→ 另一个 OID → 另一组解析结果。
- 如果两个提交的 `.gitmodules` 内容相同（OID 相同），就共享同一份缓存，不必重复解析。

这就是为什么缓存 key 是 `(gitmodules_oid, name)` 或 `(gitmodules_oid, path)` 的二元组。

#### 4.2.2 核心流程

查询一条子模块信息的完整链路（以「按路径查」为例）：

```
submodule_from_path(repo, treeish, path)
        │
        ▼
repo_read_gitmodules(repo, skip_if_read=1)   # 保证工作区 .gitmodules 已读入缓存(null_oid 为 key)
        │
        ▼
config_from(cache, treeish, path, lookup_path)
        │
        ├─ gitmodule_oid_from_commit(treeish)  # 算 "<treeish>:.gitmodules" 的 blob OID
        │
        ├─ cache_lookup_path(cache, oid, path) # 先查缓存：命中就直接返回
        │     └─ 命中 → 返回 struct submodule*
        │
        └─ 未命中 ↓
           odb_read_object(oid)                # 把这份 .gitmodules blob 读出来
           git_config_from_mem(parse_config,…)  # 复用 u6-l1 的配置解析器，逐行回调 parse_config
                └─ parse_config 把每个 submodule.<name>.<key> 填进 struct submodule，
                   并插入 for_path / for_name 两个 hashmap
           cache_lookup_path(cache, oid, path) # 再查一次，这次必命中
```

有两个值得注意的设计点：

1. **「工作区视角」用 null OID 当 key**。`repo_read_gitmodules` 读的是磁盘上的 `.gitmodules`（或回退到索引/HEAD 版本），它不属于任何具体历史提交，于是用 `null_oid()` 作为 key，官方把它标注为 `"WORKTREE"`（见 `warn_multiple_config` 里 `commit_string = "WORKTREE"`）。
2. **解析器与 u6-l1 的配置解析器是同一套**。`.gitmodules` 本质就是一份 git 配置文件，键名规范为 `submodule.<name>.<key>`（节小写、name 保留大小写、key 小写），完全复用 `git_config_from_mem` + `config_fn_t` 回调。这一点是 u6-l1 与本讲的天然衔接。

缓存里存的每条记录是 `struct submodule`，字段就是 `.gitmodules` 里能配的所有项：

[submodule-config.h:34-45](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule-config.h#L34-L45) —— `path`、`name`、`url`、`fetch_recurse`、`ignore`、`branch`、`update_strategy`、`gitmodules_oid`（来源标记）、`recommend_shallow`。其中 `gitmodules_oid` 字段就是上面说的「这份信息来自哪份 .gitmodules」的来源印记，也是缓存 key 的一部分。

#### 4.2.3 源码精读

**缓存结构**——两个 hashmap 分别以 path 和 name 为辅 key：

[submodule-config.c:32-46](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule-config.c#L32-L46) —— `struct submodule_cache` 持有 `for_path` 与 `for_name` 两个 hashmap，外加 `initialized`、`gitmodules_read` 两个状态位。每个 `struct submodule` 被「薄封装」进 `struct submodule_entry`（带 `hashmap_entry`）分别挂到这两个表里，于是同一份数据支持两种查询方式。

**比较函数**揭示了 key 的真实结构：

[submodule-config.c:53-79](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule-config.c#L53-L79) —— `config_path_cmp` 在比较两条目是否相等时，要求**既** `strcmp(path)` 相等**又** `oideq(gitmodules_oid)` 相等。这印证了「缓存 key 是 (oid, path) 二元组」——同一个 path 在不同历史版本可能绑定不同子模块，必须用 oid 区分开。

**查询主函数**`config_from`，把「先查缓存、未命中则读 blob 解析、再查缓存」的三段式写得很清楚：

[submodule-config.c:692-763](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule-config.c#L692-L763) —— 先用 `gitmodule_oid_from_commit` 把 `treeish` 翻译成 `.gitmodules` 的 blob OID（行 721）；按 name/path 查缓存（行 724-733）；未命中就 `odb_read_object` 读出 blob 文本（行 735-738）；用 `git_config_from_mem(parse_config, CONFIG_ORIGIN_SUBMODULE_BLOB, ...)` 解析并填充缓存（行 745-746）；最后再查一次缓存返回（行 750-757）。注意行 709-719 的特例：`treeish` 或 `key` 为 NULL 时直接返回缓存里「任意一条」记录，可用于探测「仓库里到底有没有子模块」。

**逐键值回调**`parse_config`——这里能看到 `.gitmodules` 里每一个合法配置项是如何被认领的：

[submodule-config.c:564-668](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule-config.c#L564-L668) —— 先用 `name_and_item_from_var` 把键名 `submodule.<name>.<key>` 拆成 `name` 与 `key`（行 573，内部还会校验 name 合法性）；再按 `key` 分派：`path`/`url`/`branch`/`ignore`/`update`/`shallow`/`fetchrecursesubmodules` 各自填入 `struct submodule` 对应字段。注意行 585-593：当设置 `path` 时要先把旧 path 从 `for_path` 表移除再插新的（`cache_remove_path` + `cache_put_path`），因为 path 是 `for_path` 表的 key。`overwrite` 标志控制是覆盖还是「发现重复就 warn 并跳过」（`warn_multiple_config`）。

**安全校验**——`.gitmodules` 是不可信输入（它可能来自 clone 来的仓库），所以解析时有针对路径穿越和恶意 URL 的防御：

[submodule-config.c:214-237](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule-config.c#L214-L237) —— `check_submodule_name` 禁止 name 里出现 `..` 路径组件（跨平台分隔符判定），防止构造恶意 name 逃出 `modules/` 目录。

[submodule-config.c:311-363](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule-config.c#L311-L363) —— `check_submodule_url` 防御 URL 注入：拒绝像命令行选项的串（`looks_like_command_line_option`）、拒绝含换行的 URL、拒绝用 `../` 逃逸出 host 字段（注释提到 CVE-2020-11008），并对走 `git-remote-curl` 的 URL 做 normalize 后再查换行。这些是 submodule 历史安全补丁在源码里的沉淀。

**工作区加载入口**`repo_read_gitmodules` 与对外查询：

[submodule-config.c:830-844](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule-config.c#L830-L844) —— 读索引、检查 `.gitmodules` 是否处于合并冲突状态（`is_gitmodules_unmerged`，冲突时不解析以免读到错误内容），再经 `config_from_gitmodules(gitmodules_cb, ...)` 把磁盘 `.gitmodules` 灌入缓存，最后置 `gitmodules_read=1`。`skip_if_read` 实现懒加载幂等。

[submodule-config.c:871-877](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule-config.c#L871-L877) —— `submodule_from_path` 就是 `repo_read_gitmodules(r, 1) + config_from(..., lookup_path)` 这两步，是上层最常用的查询入口。

#### 4.2.4 代码实践

**实践目标**：对照 `submodule-config.c` 理解「`.gitmodules` 是如何被解析并缓存」的，并验证 name/path 双 key 查询。

**操作步骤**（接 4.1.4 已建好的 `/tmp/sm-demo/super`）：

1. 直接用 git 的配置查询能力读 `.gitmodules`，验证它「就是一份配置文件」：
   ```sh
   git config -f .gitmodules --list
   git config -f .gitmodules submodule.mylib.url
   ```
2. 给子模块配一个 `branch` 项，观察 `parse_config` 里 `branch` 分支的效果：
   ```sh
   git config -f .gitmodules submodule.mylib.branch main
   git config -f .gitmodules --get-regexp 'submodule\.'
   ```
3. 查询「按历史版本」的子模块绑定（模拟 `config_from` 传一个 treeish 的场景）：
   ```sh
   # 看上一个提交里 mylib 绑定的 commit
   git ls-tree HEAD~ mylib 2>/dev/null || git ls-tree HEAD mylib
   ```

**需要观察的现象 / 预期结果**：

- 第 1 步：`git config -f .gitmodules --list` 能正常列出 `submodule.mylib.path=...`、`submodule.mylib.url=...`，证明 `.gitmodules` 与 `.git/config` 用的是同一套 INI 风格语法、同一套解析器——这正是 `parse_config` 能直接挂到 `git_config_from_mem` 上的原因。
- 第 2 步：写入后 `--get-regexp` 能列出新增的 `submodule.mylib.branch=main`，对应 `parse_config` 里 `else if (!strcmp(item.buf, "branch"))` 分支（[submodule-config.c:652-661](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule-config.c#L652-L661)）。
- 第 3 步：`git ls-tree <treeish> mylib` 给出的 commit OID，与 `.gitmodules` 里记录的 `name=mylib` 共同构成「那个版本里 mylib 的完整绑定」——这正是 `config_from` 按 `(gitmodules_oid, path)` 查询的语义。

> 若 4.1.4 的子模块未能成功创建，第 1、2 步可以用任意手写的 `.gitmodules` 文件配合 `git config -f` 验证解析；第 3 步则纯为源码阅读理解。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么缓存 key 要带 `.gitmodules` 的 blob OID，而不是只用 name（或 path）？

**答案**：因为不同历史提交里的 `.gitmodules` 内容可能不同——同一个 name `libfoo` 在 commit A 里 URL 指向 `https://a/...`，在 commit B 里改成了 `https://b/...`。如果只用 name 当 key，新旧两份解析结果会互相覆盖，导致「按版本查询」失败。带上 OID 后，每份不同的 `.gitmodules` 各自独立缓存，互不干扰，相同内容（同 OID）还能自动复用。

**练习 2**：`submodule_from_path` 第一次调用时会触发解析；第二次用同样的 `treeish` 再调用，还会重新解析 `.gitmodules` 吗？

**答案**：不会。第一次调用时 `config_from` 会把结果插入 `for_path`/`for_name` 两个 hashmap；第二次调用时 `cache_lookup_path` 直接命中（因为 key `(oid, path)` 已存在），跳过 `odb_read_object` 和 `git_config_from_mem`，直接返回缓存的 `struct submodule`。这就是「lazily read into a cache」的含义。

**练习 3**：`check_submodule_name` 为什么要禁止 name 中出现 `..` 组件？请举一个它能阻止的具体攻击。

**答案**：因为 name 被直接拼进 `submodule_name_to_gitdir` 生成 `.git/modules/<name>` 的路径。若 name 可以是 `../../etc/payload`，攻击者就能让子模块的 Git 目录被创建在 `modules/` 之外、甚至覆盖主仓库的关键文件。禁止 `..` 组件（且用跨平台分隔符判定）从源头杜绝了这类路径穿越。

---

### 4.3 子模块操作流程：add / init / update / sync / absorbgitdirs

#### 4.3.1 概念说明

前面两节讲的是「子模块是什么、配置怎么存」。这一节讲「`git submodule <cmd>` 这一系列命令在源码里到底做了什么」。它们全部实现在 `builtin/submodule--helper.c`，由一个统一的分发器 `cmd_submodule__helper` 用 `OPT_SUBCOMMAND` 子命令表（u1-l4 讲过的命令分发在 helper 内部的翻版）路由到各个 `module_<cmd>` 函数。

先建立这几条命令的**分工直觉**，它们对应子模块生命周期的不同阶段：

| 命令 | 作用（一句话） | 本质动作 |
| --- | --- | --- |
| `git submodule add <url> <path>` | 新增一个子模块 | 克隆 + 建 gitfile + 把 gitlink 和 `.gitmodules` 写进索引 |
| `git submodule init` | 初始化（把 `.gitmodules` 的信息「落实」到本地 `.git/config`） | 从 `.gitmodules` 抄写 url/branch 等到 `.git/config` |
| `git submodule update` | 把子模块工作区检出到 gitlink 指向的 commit | （必要时先克隆）checkout 到指定 commit OID |
| `git submodule sync` | 同步 URL 等到子模块自己的 remote 配置 | 把新 URL 写进子模块的 `modules/<name>/config` |
| `git submodule absorbgitdirs` | 把就地 `.git` 目录「吸收」进 `modules/` | 把 `<path>/.git` 搬到 `.git/modules/<name>` 并换成 gitfile |

一个让新手最困惑的点：**`add` 之后为什么还要 `init` + `update`？** 这要从「克隆主仓库」的场景说起。当你 `git clone` 一个带子模块的主仓库时：

1. 你得到了主仓库的历史，包括 tree 里的 gitlink（commit OID）和 `.gitmodules`（URL）。
2. 但你**没有**得到子模块——主仓库对象库里根本不存在子模块的对象，gitlink 指向的 commit 在你这里是「悬空」的。
3. `.gitmodules` 是版本控制的、你有了；但 `.git/config` 里**没有** `submodule.<name>.url`（这是本地配置，不被克隆）。

所以要补两步：`init` 把 URL 从共享的 `.gitmodules` 抄到本地的 `.git/config`（让本地知道去哪抓），`update` 才能据此克隆子模块并检出到 gitlink 指向的 commit。`git submodule update --init` 把这两步合一，这是 clone 后最常用的组合。

#### 4.3.2 核心流程

**`git submodule add <url> <path>` 的完整链路**（最复杂的一条，理解了它其余都好办）：

```
git submodule add …
  └─ git-submodule.sh → git submodule--helper add …
       └─ cmd_submodule__helper 分发 → module_add
            │
            ├─ 解析参数：默认 name=path，默认 path=URL 的 basename
            ├─ is_writing_gitmodules_ok()        # 确保 .gitmodules 可写（见 4.3.3）
            ├─ check_submodule_name(name)         # 名字合法
            │
            ├─ add_submodule()                    # 真正把子模块克隆下来
            │     ├─ 若 <path> 已是仓库 → 直接复用
            │     └─ 否则 clone_submodule():
            │          ├─ sm_gitdir = .git/modules/<name>   # 子模块 Git 目录归处
            │          ├─ git clone --separate-git-dir <sm_gitdir> --no-checkout <url> <path>
            │          │     # ↑ 把 Git 目录放进 modules/，工作区暂不检出
            │          ├─ connect_work_tree_and_git_dir(<path>, sm_gitdir)  # 写 <path>/.git gitfile
            │          └─ git checkout -f -q [-B <branch>]   # 检出默认/指定分支
            │
            └─ configure_added_submodule()        # 把绑定写进主仓库
                  ├─ repo_config_set submodule.<name>.url=<url>  (写 .git/config)
                  ├─ git add <path>            # ← 关键：把 <path> 作为 gitlink 暂存进索引
                  ├─ config_set_in_gitmodules_file submodule.<name>.path / .url  (写 .gitmodules)
                  ├─ git add .gitmodules       # 暂存 .gitmodules
                  └─ 设 submodule.<name>.active=true
```

注意第 `git add <path>` 这一步：它不是普通地把文件加进索引，而是因为 `<path>/.git` 是个 gitfile/嵌入仓库，git 的 `add` 逻辑（见 u9-l1）会把它**作为 gitlink 条目**（`ce_mode = S_IFGITLINK`，OID = 子模块当前 HEAD 的 commit）写进索引。这就是「gitlink 被写入主仓库」的真正发生点。

**`git submodule init`** 要简单得多，核心是 `init_submodule`——把 `.gitmodules` 里的 `url`、`update` 抄写到本地 `.git/config`：

[builtin/submodule--helper.c:573-656](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/submodule--helper.c#L573-L656) —— 它对每个子模块：(1) 若未激活则设 `submodule.<name>.active=true`；(2) 当 `.git/config` 里**还没有** `submodule.<name>.url` 时，把 `.gitmodules` 里的 url（必要时 `resolve_relative_url` 解析相对 URL）写进 `.git/config`；(3) 同样地补写 `update` 策略。注意「只在尚未设置时才抄写」——这是 `init` 幂等、且尊重用户手动覆盖的关键。

**`git submodule sync`** 处理的是「上游搬家了」的场景：当 `.gitmodules` 里的 URL 改变后，需要把新 URL 同步到**子模块自己**的 remote 配置里（因为 `fetch`/`push` 是在子模块仓库内跑的，它读的是子模块的 `modules/<name>/config`）：

[builtin/submodule--helper.c:1429-1520](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/submodule--helper.c#L1429-L1520) —— `sync_submodule` 同时更新两处：超项目的 `.git/config`（`submodule.<name>.url`，行 1471-1472）和子模块的 `modules/<name>/config`（`remote.<default>.url`，行 1487-1490，用 `submodule_to_gitdir` 定位子模块 Git 目录）。行 1494-1511 还支持 `--recursive` 递归同步嵌套子模块。

**`git submodule absorbgitdirs`** 把「就地」的 `<path>/.git` 目录搬进 `.git/modules/<name>` 并替换为 gitfile。它走 `absorb_git_dir_into_superproject`：

[submodule.c:2556-2611](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule.c#L2556-L2611) —— 它先 `resolve_gitdir_gently(<path>/.git)` 探测子模块当前 Git 目录；若发现该 Git 目录还不在超项目 `commondir` 之下（即尚未吸收），就调 `relocate_single_git_dir_into_superproject` 把它 `rename` 进 `modules/<name>`（`relocate_gitdir`），再用 `connect_work_tree_and_git_dir` 重建 gitfile。这正好补全了 4.1 节讲的「吸收」布局——某些场景下子模块可能就地存在一个 `.git` 目录（比如旧式手动 clone），吸收后统一管理。

#### 4.3.3 源码精读

**命令分发器**——`OPT_SUBCOMMAND` 子命令表（与 u1-l4 的 `commands[]` 同构，只是层级更内）：

[builtin/submodule--helper.c:3803-3832](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/submodule--helper.c#L3803-L3832) —— `cmd_submodule__helper` 把 `add/update/init/sync/deinit/...` 等子命令一一映射到 `module_add`/`module_update`/`module_init`/`module_sync`/... 等函数。本讲涉及的几条命令都能在这里找到入口。

**`module_add` 的参数推导**——决定 name 与 path 的默认值：

[builtin/submodule--helper.c:3642-3801](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/submodule--helper.c#L3642-L3801) —— 行 3695-3699：第一个参数是 URL；若只给一个参数，`sm_path` 取 URL 的 basename（`git_url_basename`），否则第二个参数当 path；行 3777 用 `check_submodule_name` 校验 name；行 3679 的 `is_writing_gitmodules_ok()` 保证 `.gitmodules` 处于可安全写入的状态。

`is_writing_gitmodules_ok` 的逻辑很重要——它防止「盲目覆盖」：

[submodule.c:74-79](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule.c#L74-L79) —— 只有当 `.gitmodules` **在工作区里存在**，或者（不存在时）它既不在索引里也不在 HEAD 里（即真的是新建）才允许写。否则写入会盲目覆盖索引/HEAD 里的旧内容，故禁止。

**`add_submodule` 的克隆分支**：

[builtin/submodule--helper.c:3404-3501](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/submodule--helper.c#L3404-L3501) —— 行 3411：若 `<path>` 已是个 git 仓库，直接复用并打印提示；否则进入克隆。行 3428 `submodule_name_to_gitdir` 先算出 `sm_gitdir = .git/modules/<name>`，检查是否已存在（存在且非 `--force` 则报错，提示复用本地已克隆目录）。行 3460-3477 把参数填进 `clone_data` 交给 `clone_submodule`。行 3486-3494 克隆后在子模块工作区里 `git checkout`（可带 `-B <branch>`）。

**`clone_submodule` 的真正克隆**：

[builtin/submodule--helper.c:1899-2031](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/submodule--helper.c#L1899-L2031) —— 行 1903 算 `sm_gitdir`；行 1917-1968 若该目录不存在，则 spawn `git clone`（行 1927-1960 拼参数），关键开关是 `--separate-git-dir <sm_gitdir>`（把 Git 目录放进 `modules/`）和 `--no-checkout`（工作区先空着，后面统一 checkout）；行 1963 `prepare_submodule_repo_env` 准备子进程环境（见下）。克隆完成后行 1999-2006 再做一次 `validate_submodule_git_dir` 防 race（并行克隆时可能被别的进程搞成嵌套），最后行 2008 `connect_work_tree_and_git_dir` 把工作区和 `modules/` 连起来。

**`configure_added_submodule` 的暂存分支**——gitlink 就是在这里进索引的：

[builtin/submodule--helper.c:3518-3590](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/submodule--helper.c#L3518-L3590) —— 行 3526-3527 先把 url 写进 `.git/config`；行 3530-3538 跑 `git add --no-warn-embedded-repo -- <path>`，这一步把 `<path>` 作为 gitlink（模式 `0160000`、OID=子模块 HEAD commit）暂存进索引；行 3540-3542 把 `submodule.<name>.path`/`.url` 写进 `.gitmodules`；行 3550-3555 跑 `git add .gitmodules` 暂存它；行 3567-3589 按需设 `active` 标志（见 4.3.2）。注意全程用 `run_command` spawn 子 git，而不是直接改索引——这是 `submodule--helper` 的普遍风格：重操作复用 porcelain 命令。

**`init_submodule` 的抄写逻辑**——见 4.3.2 已引用的 [builtin/submodule--helper.c:573-656](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/submodule--helper.c#L573-L656)，重点在「只在 `.git/config` 尚未设置时才写」（行 608 的 `repo_config_get_string` 返回非 0 即「未设置」才进入抄写），以及行 616-622 对相对 URL 的 `resolve_relative_url`。

**子模块 Git 目录的定位**——`submodule_to_gitdir`，给定一个 path 找到它的 Git 目录：

[submodule.c:2702-2734](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule.c#L2702-L2734) —— 先看 `<path>/.git` 是否为 gitfile（`read_gitfile`），是则直接拿到目标；否则用 `submodule_from_path` 查出 name，再 `submodule_name_to_gitdir` 算 `.git/modules/<name>`。这是 `sync`、`update` 等命令定位子模块 Git 目录的统一入口。

[submodule.c:2736-2772](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule.c#L2736-L2772) —— `submodule_name_to_gitdir` 把 name 拼成 `modules/<name>` 路径（除非启用了 `extensions.submodulePathConfig` 扩展，则从 `submodule.<name>.gitdir` 配置读取自定义位置），并经 `validate_submodule_git_dir` 防止嵌套到别的子模块 Git 目录里。

**子进程环境准备**——`prepare_submodule_repo_env`，每次在子模块里 spawn git 都要先清掉主仓库的环境变量，避免子进程误以为自己在主仓库：

[submodule.c:495-498](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule.c#L495-L498) —— 它委托 `prepare_other_repo_env(out, DEFAULT_GIT_DIR_ENVIRONMENT)`，清除 `GIT_DIR` 等「仓库局部」环境变量（呼应 u6-l2 讲的 `local_repo_env[]`），这样 `clone_submodule` 里 spawn 出来的 `git clone` 才会正确地把工作区当成独立仓库。

#### 4.3.4 代码实践

**实践目标**：完整跑一遍 `git submodule add`，然后用源码知识解释每一步在磁盘和索引上留下的痕迹，最后演练 `init`/`sync` 的「抄写」语义。

**操作步骤**（接 4.1.4 的 `/tmp/sm-demo`）：

1. 在 4.1.4 已经 `submodule add` 过的基础上，先用 plumbing 命令把 gitlink 看清楚：
   ```sh
   cd /tmp/sm-demo/super
   git ls-files --stage                # 索引里 mylib 是 160000 模式的 gitlink
   git ls-tree HEAD                    # HEAD 的 tree 里同样有这条 gitlink
   ```
2. 模拟「别人 clone 了我的主仓库」——即只复制 `.gitmodules` 与 gitlink、删掉本地配置与子模块工作区，再 `init`+`update` 重建：
   ```sh
   # 先记录子模块当前 commit
   SM_OID=$(git ls-tree HEAD mylib | awk '{print $3}')
   # 删掉本地配置里的子模块 url，模拟「克隆来的主仓库没有这条本地配置」
   git config --unset submodule.mylib.url
   # 删掉子模块工作区，模拟「克隆主仓库时没带子模块」
   rm -rf mylib
   git submodule init                  # ← init: 从 .gitmodules 把 url 抄回 .git/config
   git config --get submodule.mylib.url  # 验证抄写成功
   git submodule update                # ← update: 克隆并检出到 gitlink 指向的 commit
   git -C mylib rev-parse HEAD          # 应等于 $SM_OID
   ```
3. 演练 `sync`——改 `.gitmodules` 的 URL（模拟上游搬家），观察 sync 把新 URL 写进子模块自己的 remote 配置：
   ```sh
   # 在子模块 upstream 做个改动并提交（略，或直接演示 sync 的配置传播）
   git config -f .gitmodules submodule.mylib.url "$(pwd)/../sub-upstream"
   git submodule sync mylib
   grep -A1 '\[remote' .git/modules/mylib/config   # 子模块的 remote.<name>.url 已被同步
   ```

**需要观察的现象 / 预期结果**：

- 第 1 步：`git ls-files --stage` 能看到 `160000 <commit-oid> 0	mylib`——这正是 `configure_added_submodule` 里 `git add <path>`（[builtin/submodule--helper.c:3530-3538](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/submodule--helper.c#L3530-L3538)）写入的 gitlink 条目。
- 第 2 步：`init` 后 `git config --get submodule.mylib.url` 重新有值（对应 `init_submodule` 行 607-631 的抄写）；`update` 后 `mylib` 重新被克隆并检出，`git -C mylib rev-parse HEAD` 等于原先记录的 `$SM_OID`，证明 update 把工作区精确对齐到 gitlink 指向的 commit。
- 第 3 步：`.git/modules/mylib/config` 里 `remote.<default>.url` 被更新为 `.gitmodules` 的新值（对应 `sync_submodule` 行 1487-1490）。

> 若本地无法联网克隆或 `protocol.file.allow` 受限，第 2 步的 `update` 可能失败。可退化为「源码阅读型实践」：阅读 `init_submodule` 与 `module_update`，画出 `init` 把哪些键从 `.gitmodules` 抄到 `.git/config`、`update` 如何用这些键克隆并 checkout。**待本地验证**第 3 步的实际配置变化。

#### 4.3.5 小练习与答案

**练习 1**：`git submodule add` 完成后，主仓库索引里与 `.gitmodules` 各多了什么？它们分别由 `configure_added_submodule` 的哪一步产生？

**答案**：索引里多了一条 `mylib` 的 gitlink 条目（`160000` 模式 + 子模块 commit OID），由 `configure_added_submodule` 里 `git add <path>`（行 3530-3538）产生；`.gitmodules` 多了 `[submodule "mylib"]` 小节（含 path、url），由 `config_submodule_in_gitmodules`（行 3540-3542）写入、`git add .gitmodules`（行 3550-3555）暂存。两者一起构成子模块的完整绑定。

**练习 2**：为什么 `init_submodule` 在抄写 url 时要先检查 `.git/config` 里是否已有 `submodule.<name>.url`（行 608 的 `repo_config_get_string`）？

**答案**：为了让 `init` 幂等，并尊重用户的手动覆盖。`.gitmodules` 是版本控制的「建议默认值」，而 `.git/config` 是本地的「实际生效值」。如果用户已经在本地为子模块配了不同的 url（比如用了内网镜像），`init` 不应擅自覆盖它。所以「只在尚未设置时才抄写」既保证幂等，又让本地配置优先。

**练习 3**：`sync` 为什么要同时更新**两处** URL（超项目的 `.git/config` 与子模块的 `modules/<name>/config`）？

**答案**：因为读取 URL 的「主体」不同。超项目侧记录 `submodule.<name>.url` 是为了让超项目知道「这个子模块去哪克隆/抓取」（供 `submodule update --remote` 等用）；而真正执行 `git fetch`/`git push` 的是**子模块自己**，它读的是自己 `modules/<name>/config` 里的 `remote.<name>.url`。上游搬家后若只改一处，另一处仍指向旧地址，fetch 就会失败。`sync` 同步两处，保证二者一致。

---

## 5. 综合实践

设计一个贯穿本讲三节内容的小任务：**亲手搭建一个两层嵌套的子模块结构，并用本讲授的源码知识解释每一步的底层变化。**

**目标**：在主仓库 `super` 里嵌入一个子模块 `mylib`，再在 `mylib` 里嵌入一个二级子模块 `deep`，然后用 plumbing 命令与源码对照，验证 gitlink、`.gitmodules`、`modules/` 布局与配置缓存。

**操作步骤**：

1. 准备三个独立仓库 `deep-upstream`、`sub-upstream`、`super`（用 `git init` 各建一个并做一次初始提交）。
2. 在 `sub-upstream` 里 `git submodule add <deep-upstream> deep` 并提交——`sub-upstream` 现在自身也是个带子模块的超项目。
3. 在 `super` 里 `git submodule add <sub-upstream> mylib` 并提交——`mylib` 成了 `super` 的子模块，而 `mylib` 内部又含 `deep`。
4. 用以下命令采集「证据」，并逐条对应源码：
   ```sh
   # (a) gitlink 是 commit 指针
   git ls-tree HEAD mylib
   # (b) .gitmodules 是版本控制的普通文件
   cat .gitmodules && git ls-files --stage .gitmodules
   # (c) 子模块 Git 目录被吸收进 modules/
   ls .git/modules/ && cat mylib/.git
   # (d) 二级子模块的 Git 目录在 mylib 自己的 modules/ 下
   ls .git/modules/mylib/modules/ 2>/dev/null || ls mylib/.git 2>/dev/null
   ```
5. 删除 `mylib` 工作区、清掉本地 `submodule.mylib.url`，再 `git submodule update --init --recursive`，验证嵌套子模块能被完整重建，并解释 `--recursive` 在源码里对应 `clone_submodule`/`sync` 的递归分支。
6. 写一段 200 字以内的总结：用「gitlink（指向 commit） + `.gitmodules`（指向 URL） + `modules/`（吸收的 Git 目录）」三件套，说明为什么主仓库能在不持有子模块历史对象的前提下，精确地把子模块固定在某个 commit。

**预期结果**：(a) 显示 `160000 commit <oid> mylib`；(b) `.gitmodules` 是普通 blob、在索引里模式为 `100644`；(c) `.git/modules/mylib/` 存在，`mylib/.git` 是内容为 `gitdir: ...` 的文件；(d) 能定位到二级子模块的 Git 目录。最终总结应点明「主仓库只存 commit 指针，子模块历史独立存在于各自的对象库」这一核心设计。

> 这是一个开放型实践，**待本地验证**每一步输出。若无法联网，可把第 4 步的命令替换为「阅读 `gitsubmodules.adoc` + 本讲源码链接，画出两层嵌套的目录与索引示意图」。

## 6. 本讲小结

- **子模块 = 嵌入的外部仓库**。主仓库不持有子模块的历史对象，只在 tree 里存一条 **gitlink**（模式 `0160000`、OID 指向子模块的某 commit），再用一个版本控制的 `.gitmodules` 文件说明「去哪克隆、叫什么名字、检出到哪条路径」。两者缺一不可。
- **gitlink 是 commit 指针**。`S_IFGITLINK = 0160000`，`object_type` 对它返回 `OBJ_COMMIT`；索引里同一条目的 `ce_mode` 经 `create_ce_mode`/`canon_mode` 规范化为 `0160000`。模式值取 `S_IFDIR + S_IFLNK` 以保证不与真实文件冲突。
- **磁盘布局是「吸收」式的**。子模块的 Git 目录在主仓库 `.git/modules/<name>/`，工作区里的 `<path>/.git` 是一个 `gitdir:` gitfile 指向它，二者由 `connect_work_tree_and_git_dir` 双向绑定（`core.worktree` 反向回填）。
- **`.gitmodules` 由 `submodule-config.c` 缓存**。缓存 key 是 `(blob_oid, name)` 或 `(blob_oid, path)` 二元组，因此支持「按任意历史版本查询」，且内容相同的 `.gitmodules` 自动复用。`submodule_from_path` 经 `config_from` 走「查缓存→未命中读 blob 解析→再查」三段式，复用 u6-l1 的配置解析器，并对 name/URL 做安全校验。
- **`git submodule` 系列命令各有分工**：`add` = 克隆（`clone_submodule`）+ 建 gitfile + `git add` 写 gitlink + 写 `.gitmodules`；`init` = 把 `.gitmodules` 的 url/branch 抄到本地 `.git/config`（幂等、尊重覆盖）；`update` = 克隆并检出到 gitlink commit；`sync` = 把新 URL 同时写进超项目与子模块的配置；`absorbgitdirs` = 把就地 `.git` 搬进 `modules/`。
- **关键衔接**：子模块的克隆/抓取是 spawn 一个带干净环境（`prepare_submodule_repo_env` 清掉主仓库 `GIT_DIR` 等）的 `git` 子进程，建立在 u11-l1 的传输层之上；子模块的 URL 等本地配置遵循 u6-l1/u6-l2 的配置优先级链。

## 7. 下一步学习建议

- **u12-l2 多工作树 worktree**：与本讲互为镜像。worktree 同样用「共享对象库 + 各自 HEAD」的模型，但它是**同一个仓库**的多个工作区，而子模块是**不同仓库**的嵌套。对比学习 `connect_work_tree_and_git_dir` 在两者中的异同，能加深对 gitfile 机制的理解。
- **u11-l1 / u11-l3 传输与协商**：本讲只点到 `git clone`/`fetch` 是子进程。深入 `fetch-pack`/`send-pack` 的对象协商后，可以回到 `submodule.c` 的 `fetch_submodules`（[submodule.c:1826](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule.c#L1826)）和 `--recurse-submodules` 的并行抓取，理解「超项目抓取如何驱动各子模块并行协商」。
- **继续阅读的源码**：
  - `builtin/submodule--helper.c` 的 `module_update`（[builtin/submodule--helper.c:2980](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/submodule--helper.c#L2980)）——`git submodule update` 的完整实现，含合并策略与检出到 gitlink commit 的细节。
  - `submodule.c` 的 `submodule_move_head`（[submodule.c:2126](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule.c#L2126)）——超项目切换分支时如何把子模块 HEAD 一起移动，是 checkout 与 submodule 交互的关键。
  - `submodule-config.c` 的 `submodules_of_tree`（[submodule-config.c:929](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/submodule-config.c#L929)）——遍历一棵树里所有（非嵌套）子模块，是 `git submodule foreach`/`status` 的底层。
- **文档**：`Documentation/gitsubmodules.adoc`（模型与「active submodule」机制）、`Documentation/git-submodule.adoc`（命令手册）、`Documentation/gitmodules.adoc`（`.gitmodules` 全部可配置项）。
