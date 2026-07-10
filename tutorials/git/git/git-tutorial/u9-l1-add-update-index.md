# git add 与 update-index：暂存

## 1. 本讲目标

本讲承接 [u4-l1（index_state 与 cache_entry）](u4-l1-index-state.md)，回答一个具体问题：**当你在命令行敲下 `git add` 时，工作树里的变更究竟是怎么进入索引的？** 学完本讲你应当能够：

- 说清 `git add` 从解析参数到写回索引的完整主流程，以及它如何区分「已跟踪文件的修改」与「新增文件」两条路径。
- 理解 `git update-index` 作为更底层 plumbing 命令的逐路径处理模型，以及它和 `git add` 的关系。
- 掌握 pathspec（路径规范）的数据结构、magic 语法与匹配机制，明白 `:(glob)`、`:(icase)`、`:(exclude)` 等写法在源码里如何被解析。
- 看懂交互式暂存（`git add -i` 与 `git add -p`）的内部结构，尤其是「逐 hunk 选择」的呈现与裁决循环。

## 2. 前置知识

在进入源码前，先建立两组直觉。

**第一，git 的三层数据模型。** git 把世界分成三层：工作树（你眼睛看到的文件）、索引/暂存区（一张「路径 → 对象哈希 + stat 快照」的表，见 u4-l1）、对象数据库（内容寻址的 blob/tree/commit）。`git add` 做的事，就是把工作树的当前状态「同步」进索引——它**不直接产生提交**，提交是 `git commit` 的事。索引是工作树与对象数据库之间的桥梁，`add` 就是把桥的工作树一端拉过来。

**第二，「暂存」在底层等价于两步原子操作。** 把一个文件加进索引，本质是：①把文件内容写成 blob 对象存入对象数据库（算哈希、zlib 压缩，见 u3-l2）；②在索引里新增/替换一条 `cache_entry`，记录该路径、模式、对象哈希与 stat 快照。所以 `git add` 既动对象数据库，也动索引文件。索引写回磁盘走的是「持锁 → 写临时文件 → 原子改名」的标准套路（见 u4-l1 的 `do_write_index` 与 u14-l2 的 lockfile）。

**术语速查：**

| 术语 | 含义 |
|---|---|
| pathspec（路径规范） | 命令行里 `git add` 后面那一串路径/模式，如 `*.c`、`:(icase)README` |
| magic | pathspec 的「魔法前缀」，如 `:(glob)`、`:(top)`、`:(exclude)` |
| porcelain / plumbing | porcelain 是面向用户的高级命令（`add`），plumbing 是底层命令（`update-index`） |
| hunk | diff 里的一个变更块，由 `@@ ... @@` 头加若干 `+`/`-`/` ` 行组成 |
| cache_entry / index_state | 索引条目 / 索引整体，详见 u4-l1 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `builtin/add.c` | `git add` 的实现，命令分发、参数解析、新增文件与已跟踪文件两条暂存路径 |
| `builtin/update-index.c` | `git update-index` 的实现，plumbing 级逐路径索引操作 |
| `read-cache.c` | 索引核心库；提供 `add_file_to_index`、`add_to_index`、`add_files_to_cache`、`update_callback` 等暂存原语 |
| `pathspec.c` / `pathspec.h` | pathspec 的解析（`parse_pathspec`）与匹配（`match_pathspec`、`find_pathspecs_matching_against_index`） |
| `add-interactive.c` | `git add -i` 全交互式界面（status/update/revert/patch/diff 菜单） |
| `add-patch.c` | `git add -p` 逐 hunk 选择的核心（`run_add_p`、`patch_update_file`） |

> 提示：`git add -p`（patch 模式）的真正实现不在 `add-interactive.c`，而在 `add-patch.c`。`add-interactive.c` 里的 `run_patch` 菜单项也会委托给 `add-patch.c` 的 `run_add_p`。本讲第 4.3 节会同时讲清两者的分工。

## 4. 核心概念与源码讲解

### 4.1 add 主流程与索引更新

#### 4.1.1 概念说明

`git add` 看似只是「把文件加进去」，但它其实要处理三类不同的工作树状态，分别走不同的代码路径：

1. **已跟踪文件被修改/删除**（工作树与索引已有条目不一致）——由 diff 引擎发现差异，再逐个更新索引。
2. **全新文件**（索引里没有的路径）——由目录遍历（`fill_directory`）发现，逐个加入索引。
3. **特殊情况**：`-u`（只更新已跟踪）、`-A`（含新增与删除）、`-N`（intent-to-add，只记一个占位条目）、`--renormalize`（按当前 EOL 规则重写已跟踪文件）、`--refresh`（只刷新 stat 不暂存）。

`git add` 与 `git update-index` 的关键区别：`add` 是面向用户的 porcelain，默认会**自动发现新增文件**、自动处理删除；`update-index` 是 plumbing，默认**只更新已跟踪文件**，新增文件必须显式 `--add`，删除必须显式 `--remove`。换句话说，`add` 帮你把「要不要加新文件、要不要删」的决策默认成「要」，而 `update-index` 把这些决策权交给你。

#### 4.1.2 核心流程

`cmd_add` 的主流程可以概括为：

```text
cmd_add(argv)
├─ repo_config / parse_options          # 读配置、解析 -i/-p/-u/-A/-N 等选项
├─ prepare_repo_settings + 关闭 command_requires_full_index  # 允许稀疏索引
├─ 若 -i/-p → interactive_add() → 转交 add-interactive.c / add-patch.c（见 4.3）
├─ parse_pathspec()                      # 把命令行路径串解析成 struct pathspec（见 4.2）
├─ repo_hold_locked_index()              # 拿索引锁
├─ repo_read_index_preload()             # 读入当前索引
├─ 若 add_new_files:
│    ├─ fill_directory()                 # 遍历工作树找未跟踪文件
│    └─ prune_directory()                # 用 pathspec 过滤、标记匹配
├─ odb_transaction_begin()               # 开对象数据库批量事务
├─ add_files_to_cache()                  # 路径①：暂存已跟踪文件的修改/删除（diff 驱动）
├─ add_files()                           # 路径②：把发现的新文件逐个加入索引
├─ chmod_pathspec()（若 --chmod）
├─ odb_transaction_commit()
└─ write_locked_index(COMMIT_LOCK)       # 原子写回索引
```

其中两条暂存路径的关键差异：

- **路径①（已跟踪文件）**：`add_files_to_cache` 不直接遍历文件，而是跑一次 `run_diff_files`（对比索引与工作树），用 diff 回调 `update_callback` 对每个有差异的文件决定「修改→`add_file_to_index`」还是「删除→`remove_file_from_index`」。
- **路径②（新文件）**：`add_files` 直接对 `fill_directory` 找到的每个目录项调用 `add_file_to_index`。

两条路径最终都落到同一个底层原语 `add_file_to_index → add_to_index`：把文件内容写成 blob 对象、构造 `cache_entry`、插入索引。

#### 4.1.3 源码精读

**`cmd_add` 入口与选项解析**——先读配置、解析选项，然后做一件对稀疏仓库很重要的事：关闭 `command_requires_full_index`，让 `git add` 可以在稀疏索引上直接工作（见 u4-l3）。

[builtin/add.c:382-404](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/add.c#L382-L404) — `cmd_add` 开头：`repo_config` 读配置、`parse_options` 解析选项、`prepare_repo_settings` 后把 `command_requires_full_index` 置 0。

接着是选项的语义关键。`git add` 用一组布尔开关决定行为，下面这张选项表把 `-i/-p/-e/-u/-A/-N/--renormalize` 等都登记进来：

[builtin/add.c:254-285](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/add.c#L254-L285) — `builtin_add_options[]` 选项表，每个 `OPT_BOOL` 把一个命令行开关绑定到一个静态变量（如 `-u` 绑到 `take_worktree_changes`，`-N` 绑到 `intent_to_add`）。

**「`git add <pathspec>` 隐式等价于 `git add -A <pathspec>`」**——这是理解 `git add` 行为的一个关键点：当你给路径但没给 `-u`/`-A` 时，源码会自动把 `addremove` 置 1，意味着「既加修改也加删除也加新增」：

[builtin/add.c:484-486](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/add.c#L484-L486) — 注释直说 "Turn `git add pathspec...` to `git add -A pathspec...`"。

**flags 的拼装**——把多个布尔开关压成一组 `ADD_CACHE_*` 位标志，传给底层 `add_file_to_index`：

[builtin/add.c:488-493](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/add.c#L488-L493) — `flags` 由 `ADD_CACHE_VERBOSE`、`ADD_CACHE_PRETEND`（dry-run）、`ADD_CACHE_INTENT`（-N）、`ADD_CACHE_IGNORE_ERRORS`、`ADD_CACHE_IGNORE_REMOVAL` 等按位或而成。

**拿锁、读索引、发现新文件**——`repo_hold_locked_index` 取索引锁（见 u14-l2），`repo_read_index_preload` 预加载索引，`fill_directory` 遍历工作树：

[builtin/add.c:454-514](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/add.c#L454-L514) — 持锁 → 解析 pathspec → 读索引 → `fill_directory` 找未跟踪文件 → `prune_directory` 用 pathspec 过滤。

**两条暂存路径的调用点**——`add_files_to_cache` 处理已跟踪文件，`add_files` 处理新文件，二者都被包在 `odb_transaction` 批量事务里以优化对象写入：

[builtin/add.c:584-603](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/add.c#L584-L603) — `odb_transaction_begin` → `add_files_to_cache`（或 `renormalize_tracked_files`）→ `add_files` → `chmod_pathspec` → `odb_transaction_commit`。

**路径①：`add_files_to_cache` 用 diff 驱动暂存**——它不遍历文件，而是构造一个 `rev_info`，跑 `run_diff_files`，把每个差异通过回调 `update_callback` 处理：

[read-cache.c:4012-4052](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L4012-L4052) — 设置 `DIFF_FORMAT_CALLBACK` + `format_callback = update_callback`，把 pathspec 挂到 `rev.prune_data`，然后 `run_diff_files`。注意 `rev.diffopt.detect_rename = 0`：暂存工作树变更不需要重命名检测。

**`update_callback`：差异→动作的分派**——对每个 diff 文件对，按状态分派：修改→`add_file_to_index`，删除→`remove_file_from_index`：

[read-cache.c:3970-4010](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L3970-L4010) — `switch (fix_unmerged_status(p, data))`：`DIFF_STATUS_MODIFIED`/`TYPE_CHANGED` 走 `add_file_to_index`；`DIFF_STATUS_DELETED` 且未设 `ADD_CACHE_IGNORE_REMOVAL` 时走 `remove_file_from_index`。

**路径②与新文件共用底层原语：`add_file_to_index`**——先 `lstat` 拿 stat，再交给 `add_to_index`：

[read-cache.c:810-816](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L810-L816) — `add_file_to_index`：`lstat` 后调 `add_to_index`。

**`add_to_index`：真正「写对象 + 建条目」的核心**——构造 `cache_entry`、填 stat、定模式、`index_path`（把内容哈希并写成 blob）、`add_index_entry` 插入索引。其中有一处关键短路优化：若已有同名条目且 `ie_match_stat` 判定「没真变」，就直接复用、不重写对象：

[read-cache.c:712-808](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L712-L808) — `add_to_index` 全流程。注意 766-779 行的 alias 短路：`ie_match_stat` 认为没变就 `return 0`，避免无谓的对象写入；`index_path`（782 行）负责把文件内容落盘成 blob 并填入 `ce->oid`。

**`git update-index` 的逐路径模型**——它是 plumbing，默认逐个处理命令行上给的路径，每个路径走 `update_one`：

[builtin/update-index.c:1128-1178](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/update-index.c#L1128-L1178) — 自定义的 parse 循环：每遇到一个非选项参数就 `prefix_path` 规范化后调 `update_one(p)`。注意它用 `parse_options_start`/`parse_options_step` 而非一次性 `parse_options`，因为要把路径参数「边来边处理」。

**`update_one`：单路径分派**——按当前模式（`--assume-unchanged`/`--skip-worktree`/`--force-remove` 或普通更新）分派，普通情况落到 `process_path`：

[builtin/update-index.c:463-505](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/update-index.c#L463-L505) — `update_one`：先 `lstat`，再按 `mark_valid_only`/`mark_skip_worktree_only`/`mark_fsmonitor_only`/`force_remove` 分派，否则 `process_path`。

**`process_path` → `add_one_path`**——`process_path` 处理目录/缺失等情形，普通文件落到 `add_one_path`，它和 `add_to_index` 干的事几乎一样（建条目、`index_path` 写对象、`add_index_entry`），但更「裸」：

[builtin/update-index.c:381-415](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/update-index.c#L381-L415) — `process_path`：查索引位置、处理 skip-worktree、目录、lstat 错误，最终 `add_one_path`。

[builtin/update-index.c:283-311](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/update-index.c#L283-L311) — `add_one_path`：`ie_match_stat` 短路 → 建 `cache_entry` → `index_path` 写对象 → `add_index_entry`。`info_only`（`--info-only`）时不写对象、只更索引。

**写回索引**——`cmd_add` 末尾用 `write_locked_index` 原子提交索引锁；`cmd_update_index` 则在确有改动（或 `--force-write-index`）时才写，否则回滚锁：

[builtin/add.c:605-613](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/add.c#L605-L613) — `write_locked_index(..., COMMIT_LOCK | SKIP_IF_UNCHANGED)`。

[builtin/update-index.c:1307-1319](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/update-index.c#L1307-L1319) — 仅当 `cache_changed || force_write` 才 `write_locked_index`，否则 `rollback_lock_file`。

#### 4.1.4 代码实践

**实践目标**：用 `git ls-files --stage` 观察 `git add` 前后索引条目的变化，验证「暂存 = 写 blob + 换 cache_entry」。

**操作步骤**（在一个临时仓库里）：

1. `git init add-lab && cd add-lab`
2. `printf 'hello\n' > a.txt && git add a.txt` —— 新增文件，走路径②。
3. `git ls-files --stage` —— 看到 `a.txt` 的模式 `100644`、一个 blob 哈希、stage 0。
4. `git cat-file -p <那个哈希>` —— 输出 `hello`，证实 add 已经把内容写成了 blob 对象。
5. `printf 'hello world\n' > a.txt && git add a.txt` —— 修改已跟踪文件，走路径①（`add_files_to_cache` + `update_callback`）。
6. 再次 `git ls-files --stage` —— 哈希变了，说明索引条目被替换成了新 blob。
7. 对比 plumbing：`printf 'bye\n' > b.txt && git update-index b.txt`（不带 `--add`）会报错 `cannot add ... - missing --add option?`；而 `git update-index --add b.txt` 才会成功。这正体现了 plumbing 默认不自动加新文件。

**需要观察的现象**：第 3 步能看到 blob 哈希；第 4 步能读回原始内容；第 6 步哈希确实更新；第 7 步 `update-index` 不带 `--add` 时拒绝新增。

**预期结果**：索引里 `a.txt` 的 oid 与 `git hash-object a.txt` 的输出一致；`update-index` 不带 `--add` 对新文件报错。

> 若你用的是仓库自带的、刚刚 `make` 出来的 `./git`，可直接 `./git add` 替代系统 `git`，以验证你读到的源码版本与二进制一致。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `add_files_to_cache` 里要设 `rev.diffopt.detect_rename = 0`？

> **答案**：暂存工作树变更只是把「当前文件内容」同步进索引，不需要像 `git diff`/`git log` 那样猜测「这个文件是不是从那个文件改名来的」。关掉重命名检测可以省掉昂贵的相似度比对，且语义上 `add` 本就按路径操作，不关心重命名。

**练习 2**：`git add -N <file>`（intent-to-add）在索引里留下的是什么？`add_to_index` 里哪一行对应它？

> **答案**：留下一条指向「空 blob」的占位 `cache_entry`，并标 `CE_INTENT_TO_ADD`，让该文件在 `git status` 里显示为「待添加」但内容尚未暂存。对应 [read-cache.c:720](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L720) 的 `intent_only = flags & ADD_CACHE_INTENT`，以及 739-742 行的 `else ce->ce_flags |= CE_INTENT_TO_ADD;`（不调 `index_path` 写真实内容，改由 `set_object_name_for_intent_to_add_entry` 填空树）。

---

### 4.2 pathspec 路径匹配

#### 4.2.1 概念说明

命令行里 `git add` 后面那一串参数，git 称为 **pathspec（路径规范）**。它不只是「文件名」，而是一种带语法的小语言。你写的 `*.c`、`src/`、`:(icase)readme`、`:(exclude)vendor/` 都是 pathspec。

pathspec 有两层能力：

1. **前缀补全**：当你在子目录 `src/foo/` 里运行 `git add bar.c`，git 要把它补成相对仓库根的 `src/foo/bar.c`。这靠 `prefix`（u1-l4 讲过）和 `parse_pathspec` 协作完成。
2. **magic（魔法）**：用 `:(...)` 或简写前缀修饰匹配方式，比如 `:(glob)` 强制按通配符、`:(icase)` 忽略大小写、`:(top)` 从仓库根算起、`:(exclude)` 排除、`:(attr)` 按 gitattributes 过滤。

内存里 pathspec 是 `struct pathspec`，里面是一组 `pathspec_item`，每个 item 记录规范化后的 `match` 串、原始写法 `original`、magic 位图、前缀长度、以及「不含通配符的前缀长度 `nowildcard_len`」（用于快速预筛）。

#### 4.2.2 核心流程

```text
parse_pathspec(pathspec, magic_mask, flags, prefix, argv)
├─ 无参数 + PREFER_FULL → 空 pathspec（匹配全部）
├─ 无参数 + PREFER_CWD  → 用 prefix 本身作唯一 item
├─ 对每个 argv 元素 init_pathspec_item():
│    ├─ parse_element_magic()        # 识别 :(long) 或 :短写 magic
│    │    ├─ parse_long_magic()      # :(glob,icase,prefix:N,attr:...)
│    │    └─ parse_short_magic()     # :/ :! 等
│    ├─ get_global_magic()           # 合并 GIT_*_PATHSPECS 环境变量
│    ├─ prefix_path_gently()         # 把 prefix 拼到路径前（除非 :(top)）
│    ├─ 计 nowildcard_len = simple_length(match)
│    └─ PATHSPEC_EXCLUDE-only → 末尾补一条 "." 作正向匹配
└─ （若 MAXDEPTH）排序 items

匹配阶段（供 add 等命令用）:
├─ find_pathspecs_matching_against_index(ps, istate)  # 遍历索引标记 seen[]
├─ add_pathspec_matches_against_index()               # 对每条 ce_path_match
└─ ce_path_match / match_pathspec                     # 调用 wildmatch 等
```

magic 位图（见 [pathspec.h:7-21](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pathspec.h#L7-L21)）：

| 位 | 简写 | 全称 | 含义 |
|---|---|---|---|
| `PATHSPEC_FROMTOP` | `:/` | `top` | 从仓库根匹配，不加 prefix |
| `PATHSPEC_LITERAL` | — | `literal` | 当作字面量，不解释通配符 |
| `PATHSPEC_GLOB` | — | `glob` | 按 shell glob 解释 `*` `?` `[]` |
| `PATHSPEC_ICASE` | — | `icase` | 大小写不敏感 |
| `PATHSPEC_EXCLUDE` | `:!` | `exclude` | 排除匹配的路径 |
| `PATHSPEC_ATTR` | — | `attr` | 按 gitattributes 匹配 |

「唯一前缀」匹配时，`list_and_choose` 用的是排序后的 `string_list` + `string_list_find_insert_index`，定位接近 \(O(\log n)\)（排序数组上的二分插入点查找），再校验前缀唯一性。

#### 4.2.3 源码精读

**`struct pathspec` 与 `pathspec_item`**——理解 pathspec 的内存表示是理解一切匹配的基础：

[pathspec.h:30-56](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pathspec.h#L30-L56) — `struct pathspec`：`nr`（条目数）、`has_wildcard`、`magic`，以及 `items[]` 数组；每个 `pathspec_item` 含 `match`（规范化串）、`original`（原始写法）、`magic`、`len`、`prefix`、`nowildcard_len`、`flags`、以及 `attr_match`/`attr_check`（用于 `:(attr)`）。

**magic 登记表**——每个 magic 对应一个助记符（短写）和一个长名：

[pathspec.c:101-112](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pathspec.c#L101-L112) — `pathspec_magic[]`：`/`→`top`、`!`→`exclude` 等；`literal`/`glob`/`icase`/`attr` 没有助记符，只能用 `:(long)` 形式。

**`parse_pathspec` 主入口**——处理「无参数」的两种语义（PREFER_FULL 返回空=匹配全部；PREFER_CWD 用 prefix），并对每个参数调 `init_pathspec_item`。还有一个易被忽略的细节：**若所有 item 都是 `exclude`，会自动补一条正向 `"."`**，否则「全是排除」会匹配不到任何东西：

[pathspec.c:595-685](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pathspec.c#L595-L685) — `parse_pathspec`。注意 674-678 行：`if (nr_exclude == n) { init_pathspec_item(item + n, ..., "."); ... }`。

**`init_pathspec_item`**——单条 pathspec 的初始化：解析 magic、合并全局 magic、拼 prefix、算 `nowildcard_len`：

[pathspec.c:447-552](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pathspec.c#L447-L552) — 关键点：461-462 行 `PATHSPEC_LITERAL_PATH` 直接强制 literal；485-487 行 `PATHSPEC_FROMTOP` 时不拼 prefix；526-532 行算 `nowildcard_len`（用 `simple_length` 找第一个通配符位置），它让匹配器能先做廉价的字节前缀比较再决定要不要调昂贵的 `wildmatch`。

**长/短 magic 解析**——`:(glob,icase)` 与 `:/`/`:!` 两种写法的解析器：

[pathspec.c:333-387](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pathspec.c#L333-L387) — `parse_long_magic`：逐段切分逗号分隔的 magic，识别 `prefix:N` 与 `attr:...`。

[pathspec.c:395-428](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pathspec.c#L395-L428) — `parse_short_magic`：逐字符消费 `:` 后的助记符（如 `/`、`!`，`^` 也作为 `!` 的别名）。

**全局 magic（环境变量）**——`GIT_GLOB_PATHSPECS`、`GIT_ICASE_PATHSPECS` 等环境变量会全局改变默认 magic：

[pathspec.c:297-324](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pathspec.c#L297-L324) — `get_global_magic`：把环境变量折算进 `global_magic`，并校验 `literal` 与其他 magic 互斥。

**匹配：把 pathspec 对着索引「盖章」**——`add_files_to_cache` 之外，`git add` 还用 `find_pathspecs_matching_against_index` 检查「有没有 pathspec 完全没匹配到任何索引条目」（没匹配就报 `pathspec '%s' did not match any files`）：

[pathspec.c:32-57](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pathspec.c#L32-L57) — `add_pathspec_matches_against_index`：遍历索引每个 `cache_entry`，对每条 pathspec 调 `ce_path_match`，在 `seen[]` 里标记「该 pathspec 是否被某条目命中」。

[pathspec.c:67-74](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pathspec.c#L67-L74) — `find_pathspecs_matching_against_index`：一次性分配 `seen[]` 并填充的包装。

**`git add` 里的「未匹配」报错**——把上面这套用在 `cmd_add` 里：

[builtin/add.c:521-571](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/add.c#L521-L571) — 先 `find_pathspecs_matching_against_index` 得到 `seen`，再对每个 `!seen[i]` 的 pathspec 判断：是 glob/不存在则按 `--ignore-missing` 决定报错还是记为 ignored，否则 `die("pathspec '%s' did not match any files")`。`GUARD_PATHSPEC`（533-539 行）是一道安全闸：声明本代码路径只支持这些 magic，超出就 `BUG()`。

#### 4.2.4 代码实践

**实践目标**：用不同 magic 的 pathspec 观察匹配范围，并对照源码确认行为来源。

**操作步骤**：

1. 建一个含大小写文件的仓库：
   ```sh
   git init ps-lab && cd ps-lab
   printf a > README.md; printf b > readme.md; printf c > a.c; printf d > b.c
   git add . && git commit -m init
   ```
2. `git add :(icase)readme.md && git diff --cached --name-only` —— 应同时命中 `README.md` 与 `readme.md`（大小写不敏感）。用 `git reset` 撤销。
3. `git add ':(exclude)*.c' -- . && git diff --cached --name-only` 体会 exclude；注意源码里「全是 exclude 会补 `"."`」的细节——单独一个 `:(exclude)*.c` 仍会匹配非 `.c` 文件。
4. 在子目录 `mkdir -p src/foo && cd src/foo && touch x.c && git add x.c`，然后 `git ls-files` 看到 `src/foo/x.c`——验证 prefix 被拼上。
5. 设环境变量重跑：`GIT_ICASE_PATHSPECS=1 git add readme.md`（在干净的索引上）也会大小写不敏感，对应 `get_global_magic` 读 `GIT_ICASE_PATHSPECS`。

**需要观察的现象**：第 2 步两个文件都被暂存；第 3 步只有非 `.c` 文件被暂存；第 4 步路径带上了前缀。

**预期结果**：与上述一致。运行结果待本地验证（不同文件系统的大小写敏感性可能影响第 4 步以外行为，以 `core.ignorecase` 配置为准）。

#### 4.2.5 小练习与答案

**练习 1**：`nowildcard_len` 这个字段解决了什么问题？

> **答案**：它记录 pathspec 串中「第一个通配符之前」的字面前缀长度。匹配时可以先做廉价的字节前缀比较（`strncmp`），只有前缀对得上的路径才需要调用昂贵的 `wildmatch` 做完整通配匹配。这是把「字面部分」与「通配部分」分开以加速过滤的经典手法。见 [pathspec.c:526-532](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pathspec.c#L526-L532)。

**练习 2**：为什么 `git add` 要在解析后用 `GUARD_PATHSPEC` 显式声明它支持的 magic 集合？

> **答案**：pathspec 是跨命令共享的通用设施，但并非每个命令的后续代码都能正确处理所有 magic（比如某些 magic 只在「对着索引匹配」时有意义）。`parse_pathspec` 用 `magic_mask` 在入口礼貌拒绝不支持的 magic；但若调用方忘了设 mask，`GUARD_PATHSPEC` 是第二道防线——一旦出现未预期的 magic 就直接 `BUG()` 崩溃，暴露调用方的编程错误，而不是让不支持的 magic 静默产生错误结果。见 [pathspec.h:58-62](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pathspec.h#L58-L62) 与 [builtin/add.c:533-539](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/add.c#L533-L539)。

---

### 4.3 交互式 add 分块

#### 4.3.1 概念说明

`git add` 有两种交互模式：

- **`git add -i`（interactive）**：一个菜单式界面，列出 `status`/`update`/`revert`/`add untracked`/`patch`/`diff` 等命令，让你选文件级操作。实现在 `add-interactive.c` 的 `run_add_i`。
- **`git add -p`（patch）**：逐 hunk 展示工作树差异，对每个 hunk 单独问「要不要暂存这块？」。实现在 `add-patch.c` 的 `run_add_p`。

两者都从 `cmd_add` 的同一个入口分流：`-i` 或 `-p` 会让 `cmd_add` 调 `interactive_add`，后者按是否 patch 分别转给 `run_add_i` 或 `run_add_p`。

> 注意：`add-interactive.c` 的 `run_patch` 菜单项内部也是调 `run_add_p`。所以「hunk 如何呈现与选择」的真正答案在 `add-patch.c`，`add-interactive.c` 主要负责文件级菜单与「staged/unstaged」两栏状态展示。

两种模式都**不直接改索引**，而是先收集用户选择，最后一次性持锁写回索引（`-i` 的 `update`/`patch` 命令）或通过 `git apply --cached` 应用补丁（`-p`）。

#### 4.3.2 核心流程

**`git add -i`（`run_add_i`）流程**：

```text
run_add_i(repo, ps)
├─ init_add_i_state()            # 读颜色、diff 上下文等配置
├─ run_status()                  # 先展示一遍 modified 文件清单（staged/unstaged 两栏）
└─ 主循环 list_and_choose(commands):
     ├─ status    → run_status     # 重列文件
     ├─ update    → run_update     # 选 worktree 改动 → add_file_to_index
     ├─ revert    → run_revert     # 选 staged 改动 → 回退到 HEAD
     ├─ add untracked → run_add_untracked
     ├─ patch     → run_patch → run_add_p   # 进入 hunk 模式（见下）
     ├─ diff      → run_diff       # 看 HEAD vs index
     └─ quit
```

其中 `get_modified_files` 是「发现变更」的核心：它跑**两次** diff——一次 `run_diff_files`（工作树 vs 索引，得 unstaged）、一次 `run_diff_index`（索引 vs HEAD，得 staged），把结果合并进同一个 `file_item` 的 `worktree`/`index` 两个 `adddel` 字段，于是每个文件能同时显示「staged 增删」与「unstaged 增删」两栏。

**`git add -p`（`run_add_p`）流程**：

```text
run_add_p(repo, ADD_P_ADD, ...)
├─ 选 patch_mode_add（diff_cmd=diff-files, apply_args=--cached, 提示 "Stage this hunk ..."）
├─ run_add_p_common():
│    ├─ parse_diff(ps)            # 跑 `git diff-files -p -- <paths>`，解析成 file_diff[]/hunk[]
│    └─ 对每个 file_diff: patch_update_file()   # 逐文件、逐 hunk 循环
└─ （非 auto_advance 时）对每个文件 apply_patch()

patch_update_file 的 hunk 循环:
  for (;;) {
    render_hunk(hunk)                       # 打印 @@ 头 + +/- 行
    提示 "Stage this hunk [y,n,q,a,d,j,k,g,/,s,e,p,?]?"
    读一个字符 ch
    y → hunk.use = USE_HUNK; 跳到下一个 undecided
    n → hunk.use = SKIP_HUNK
    a → 本文件剩余 undecided 全置 USE
    d → 本文件剩余 undecided 全置 SKIP
    q → 退出
    s → split_hunk() 把当前 hunk 切成更小 hunk
    e → edit_hunk_loop() 手工编辑
    ... j/k/J/K/g/ 导航, / 正则搜索
  }
  apply_patch(): reassemble_patch() 把所有 USE_HUNK 拼成补丁 → `git apply --cached`
```

每个 hunk 有一个三态 `use`：`UNDECIDED_HUNK`（未决定）、`USE_HUNK`（要暂存）、`SKIP_HUNK`（跳过）。循环只关心未决定的，全部决定后退出。

#### 4.3.3 源码精读

**入口分流：`cmd_add` → `interactive_add`**——`-p` 会让 `patch_interactive` 置 1，进而 `add_interactive=1`，进入 `interactive_add`，按 `patch` 标志选 `run_add_p` 或 `run_add_i`：

[builtin/add.c:411-418](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/add.c#L411-L418) — `if (patch_interactive) add_interactive = 1; if (add_interactive) { ... exit(interactive_add(..., patch_interactive, ...)); }`。

[builtin/add.c:161-182](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/add.c#L161-L182) — `interactive_add`：先 `parse_pathspec`（注意它用 `PATHSPEC_PREFIX_ORIGIN`，把 prefix 编进 pathspec 以便传给子命令），再按 `patch` 调 `run_add_p` 或 `run_add_i`。

**`run_add_i` 的命令表与主循环**——一组 `{名字, 函数}` 对，构成 `git add -i` 的主菜单：

[add-interactive.c:1064-1085](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-interactive.c#L1064-L1085) — `run_add_i` 里的 `command_list[]`：`status`/`update`/`revert`/`add untracked`/`patch`/`diff`/`quit`/`help`。`patch` 项的函数是 `run_patch`。

[add-interactive.c:1132-1151](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-interactive.c#L1132-L1151) — 先 `run_status` 展示一遍，然后 `for (;;)` 调 `list_and_choose` 选命令并执行。

**发现变更：`get_modified_files` 跑两次 diff**——一次 worktree、一次 index，合并到同一 `file_item`：

[add-interactive.c:472-536](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-interactive.c#L472-L536) — 关键在 494-525 行的两轮循环：`FROM_WORKTREE` 用 `run_diff_files`，`FROM_INDEX` 用 `run_diff_index`；都把输出导向 `collect_changes_cb` 回调。`opt.def` 是 HEAD（或初始提交的空树）。

[add-interactive.c:409-464](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-interactive.c#L409-L464) — `collect_changes_cb`：用 `compute_diffstat` 算出每个文件的 `added`/`deleted` 行数，填进 `file_item` 的 `index` 或 `worktree` 两个 `adddel`。

**`run_update`：文件级暂存**——`git add -i` 里选 `update` 后，对选中文件逐个 `add_file_to_index`（缺失则 `remove_file_from_index`），最后一次性 `write_locked_index`：

[add-interactive.c:622-677](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-interactive.c#L622-L677) — `run_update`：`list_and_choose` 选文件 → 持锁 → 逐个 `lstat`，缺失则 `remove_file_from_index`，否则 `add_file_to_index` → `write_locked_index`。

**`run_patch` 委托给 `run_add_p`**——`git add -i` 的 patch 菜单项先把选中文件收集成 pathspec，再调 `run_add_p`：

[add-interactive.c:872-936](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-interactive.c#L872-L936) — `run_patch`：`get_modified_files(WORKTREE_ONLY)` → 过滤掉二进制/未合并 → `list_and_choose` 选文件 → 把选中路径 `parse_pathspec` 后 `run_add_p(..., ADD_P_ADD, ...)`。

**`run_add_p` 选模式**——`git add -p` 用 `patch_mode_add`，它定义了「用 diff-files 取差异、用 git apply --cached 应用、提示语是 Stage this hunk」：

[add-patch.c:44-64](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L44-L64) — `patch_mode_add`：`diff_cmd={"diff-files"}`、`apply_args={"--cached"}`、四类提示语（mode change/deletion/addition/hunk）。

[add-patch.c:2040-2101](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L2040-L2101) — `run_add_p`：按 `mode` 选 `patch_mode_*`，刷新索引后调 `run_add_p_common`。

**`run_add_p_common`：解析 diff → 逐文件循环**——先 `parse_diff` 把 diff 文本切成 `file_diff[]`，再对每个文件 `patch_update_file`：

[add-patch.c:2008-2038](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L2008-L2038) — `run_add_p_common`：`parse_diff` → 循环 `patch_update_file`；非 `auto_advance` 时循环结束后再对每个文件 `apply_patch`。

**`parse_diff`：把 diff 文本结构化**——它实际跑一个子进程 `git diff-files ... -p -- <paths>` 捕获输出，再逐行解析成 `file_diff` 与 `hunk`：

[add-patch.c:538-575](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L538-L575) — `parse_diff` 开头：用 `capture_command` 跑 `diff_cmd`，按需再跑一次带 `--color` 的版本以着色展示。

[add-patch.c:620-762](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L620-L762) — 解析循环：遇到 `diff ` 开新 `file_diff`，遇到 `@@ ` 开新 `hunk`，并维护 `splittable_into`（这个 hunk 能切成几段）。

**hunk 的内存表示与三态**——

[add-patch.c:259-264](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L259-L264) — `struct hunk`：`start/end`（在 plain 缓冲里的字节范围）、`colored_start/end`、`splittable_into`、`delta`（编辑后行数偏移）、`use`（`UNDECIDED_HUNK`/`SKIP_HUNK`/`USE_HUNK`）。

**`patch_update_file`：hunk 呈现与裁决的主循环**——这是「hunk 如何呈现与选择」的核心。它渲染当前 hunk、打印提示、读一个字符、按字符分支：

[add-patch.c:1607-1677](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L1607-L1677) — 循环开头：算出前/后未决定 hunk 位置，渲染 hunk，构造提示串（动态拼出当前可用的 `j,k,g,/,s,e` 等），打印 `(i/N) Stage this hunk [...]?`。

[add-patch.c:1775-1809](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L1775-L1809) — `y`/`n`/`a`/`d`/`q` 分支：`y` 置 `USE_HUNK` 后跳到下一个 undecided；`a` 把本文件剩余 undecided 全置 `USE`；`d` 全置 `SKIP`；`q` 退出。

**`render_hunk`：动态重算 `@@` 头**——因为前面的 hunk 可能被跳过，后面 hunk 的行号偏移会变，所以 `@@ -a,b +c,d @@` 里的 `c` 要按累积 `delta` 重算：

[add-patch.c:791-852](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L791-L852) — `render_hunk`：`new_offset += delta`（非 reverse 时）后重新格式化 `@@ -%lu +%lu @@`。

**`split_hunk`：把一个 hunk 切细**——在「连续 +/- 行之间的上下文行」处切开：

[add-patch.c:1051-1095](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L1051-L1095) — `split_hunk` 开头：只有 `splittable_into >= 2` 才可切；切分依据是「上一行是 `-`/`+`、当前行是 ` `（上下文）」的边界。

**`apply_patch`：把选中的 hunk 落到索引**——重新拼出只含 `USE_HUNK` 的补丁，喂给 `git apply --cached`：

[add-patch.c:1550-1584](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L1550-L1584) — `apply_patch`：若有任何 `USE_HUNK`，`reassemble_patch` 拼补丁 → `setup_child_process("apply", ...)` + `apply_args`（`--cached`）→ `pipe_command` 喂入补丁。

#### 4.3.4 代码实践

**实践目标**：亲手走一遍 `git add -p` 的 hunk 选择，并对照 `add-patch.c` 说明每个 hunk 是如何被渲染、裁决、最终应用的。

**操作步骤**：

1. 建一个有多处独立改动的文件：
   ```sh
   git init p-lab && cd p-lab
   printf 'line1\nline2\nline3\nline4\nline5\nline6\n' > f.txt
   git add f.txt && git commit -m init
   ```
2. 在 `f.txt` 里做两处不相邻的修改（中间留几行上下文），例如把 `line2` 改成 `line2-changed`，把 `line5` 改成 `line5-changed`。
3. 运行 `git add -p f.txt`。你会看到第一个 hunk 被渲染（`@@ ... @@` 头 + 上下文 + `-`/`+` 行），并提示 `Stage this hunk [y,n,q,a,d,j,J,g,/,s,e,?]?`。
4. 对第一个 hunk 按 `n`（跳过），对第二个 hunk 按 `y`（暂存）。
5. 退出后 `git diff --cached f.txt` 应只显示第二处改动；`git diff f.txt` 仍显示第一处改动（因为没暂存）。
6. 重置后 `git add -p` 再试 `s`（split）：若一个 hunk 内有两段改动被上下文隔开，`s` 会把它切成两个更小的 hunk，对应 `split_hunk` 里 `splittable_into` 的逻辑。
7. 对照源码：在 [add-patch.c:1745-1759](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L1745-L1759) 处看到提示语 `(i/N)` 与 `Stage this hunk [...]` 的打印；在 [add-patch.c:1775-1779](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L1775-L1779) 处看到 `y` 如何置 `USE_HUNK` 并跳到下一个 undecided。

**需要观察的现象**：第 5 步 `--cached` 只含第二处改动，说明 hunk 是**单独**裁决的；第 6 步 `s` 后一个 hunk 变成两个，说明 `split_hunk` 在上下文边界切开。

**预期结果**：选择性暂存成功，`git diff --cached` 与 `git diff` 互补地分别显示已暂存与未暂存的改动。运行结果待本地验证。

> 想看更底层的交互？开 `GIT_TRACE=1` 再跑 `git add -p`，能看到它实际 spawn 了 `git diff-files` 与 `git apply --cached` 子进程，对应 `parse_diff` 的 `capture_command` 与 `apply_patch` 的 `pipe_command`。

#### 4.3.5 小练习与答案

**练习 1**：`git add -p` 里按 `a` 与按 `y` 有什么区别？源码里如何体现？

> **答案**：`y` 只把**当前** hunk 标为 `USE_HUNK` 然后跳到下一个未决定 hunk；`a` 把当前 hunk **及本文件剩余所有未决定 hunk** 全部标为 `USE_HUNK`。见 [add-patch.c:1783-1794](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L1783-L1794)：`a` 分支里 `for (; hunk_index < file_diff->hunk_nr; hunk_index++)` 把每个 `UNDECIDED_HUNK` 都置 `USE_HUNK`。

**练习 2**：为什么 `render_hunk` 要动态重算 `@@` 头里的行号，而不是直接用 diff 原文里的数字？

> **答案**：因为前面的 hunk 可能被跳过（`SKIP_HUNK`），导致后续 hunk 在「最终应用到索引」的补丁里的起始行号与原始 diff 不同。`reassemble_patch` 用累积的 `delta` 调整偏移，`render_hunk` 则把 `delta` 反映进展示给用户的 `@@ -old +new @@` 头，保证展示与应用一致。见 [add-patch.c:825-836](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L825-L836)（`new_offset += delta`）与 [add-patch.c:1015-1049](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L1015-L1049)（`reassemble_patch` 累积 `delta`）。

**练习 3**：`git add -i` 的 `run_update` 与 `git add -p` 的 `apply_patch` 最终分别用什么原语改索引？

> **答案**：`run_update` 直接调库函数 `add_file_to_index`/`remove_file_from_index` 然后写索引（文件级整存整取，见 [add-interactive.c:651-668](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-interactive.c#L651-L668)）；`apply_patch` 则把选中的 hunk 拼成补丁，spawn 子进程 `git apply --cached` 来应用（hunk 级部分暂存，见 [add-patch.c:1570-1576](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/add-patch.c#L1570-L1576)）。前者粒度是整文件，后者粒度是 hunk——这正是 `-p` 能「半个文件地暂存」的根本原因。

## 5. 综合实践

把本讲三个模块串起来，完成一次「选择性暂存 + pathspec 过滤」的完整流程：

1. 建仓库，制造混合变更：
   ```sh
   git init final-lab && cd final-lab
   printf 'a\nb\nc\n' > keep.c
   printf 'x\ny\nz\n' > drop.c
   git add . && git commit -m init
   ```
2. 修改两个文件各加一行：`echo extra >> keep.c; echo extra >> drop.c`，再新建一个未跟踪文件 `printf 'new\n' > new.c`。
3. 用 pathspec 只对 `.c` 文件操作，并验证 magic：`git add ':(glob)*.c'` 应同时命中三个 `.c` 文件（含新的 `new.c`，因为 `git add <pathspec>` 隐式等价 `-A`）。用 `git reset` 撤销。
4. 现在用 `git add -p` 只暂存 `keep.c` 里的改动，跳过 `drop.c`。操作时对照 4.3 节确认你看到的提示来自 `patch_update_file`。
5. 用 `git update-index --add new.c`（plumbing 方式）暂存新文件，对比第 3 步的 porcelain 方式。
6. 运行 `git ls-files --stage` 与 `git diff --cached --stat`，确认：索引里有 `keep.c`（hunk 级暂存）和 `new.c`（plumbing 暂存），但 `drop.c` 仍未暂存。
7. 画出这次操作的数据流：哪些变更走了 `add_files_to_cache`（diff 驱动），哪些走了 `add_files`（新文件），`-p` 那次又是怎么经 `run_add_p` → `git apply --cached` 落地的。

**验收标准**：能说清第 7 步的数据流，且 `git diff --cached` 与 `git diff` 的输出互补（已暂存/未暂存不重叠）。

## 6. 本讲小结

- `git add` 是 porcelain，默认自动处理新增与删除（`git add <pathspec>` 隐式等价 `-A`）；它分两条路径暂存：已跟踪文件走 `add_files_to_cache`（diff 驱动的 `update_callback`），新文件走 `add_files`（目录遍历）。两者最终都落到 `add_file_to_index → add_to_index`：写 blob 对象 + 换 cache_entry。
- `git update-index` 是 plumbing，逐路径处理，默认不自动加新文件（需 `--add`）、不自动删（需 `--remove`），更裸也更可控；其单路径处理链是 `update_one → process_path → add_one_path`。
- pathspec 是带语法的小语言，内存表示为 `struct pathspec`/`pathspec_item`；`parse_pathspec` 解析 magic（`glob`/`icase`/`exclude`/`top`/`attr`）与 prefix，`nowildcard_len` 用于快速预筛，`find_pathspecs_matching_against_index` 把 pathspec 对着索引盖章以检测未匹配。
- `git add -i`（`run_add_i`）是文件级菜单，靠跑两次 diff（worktree 与 index）展示 staged/unstaged 两栏；`git add -p`（`run_add_p`）是 hunk 级选择，靠 `patch_update_file` 的三态（UNDECIDED/USE/SKIP）循环与 `y/n/a/d/s/e` 等命令裁决。
- `-p` 的「半个文件地暂存」之所以可能，是因为它最终用 `git apply --cached` 应用「只含选中 hunk」的补丁，而非整文件替换；`render_hunk` 用累积 `delta` 动态重算 `@@` 头以保持展示与应用一致。
- 所有暂存操作都包在索引锁（`repo_hold_locked_index` + `write_locked_index`）与对象数据库事务（`odb_transaction_begin/commit`）里，保证原子性与批量效率。

## 7. 下一步学习建议

- **进入 checkout/switch**：本讲只讲了「工作树 → 索引」的方向。反方向「索引/树 → 工作树」由 `git checkout`/`switch` 的 `unpack-trees` 完成，建议接着读 [u9-l2（checkout 与 unpack-trees）](u9-l2-checkout-unpack-trees.md)。
- **看 status 如何复用本讲的 diff**：`git add -i` 的 `get_modified_files` 跑两次 diff 的套路，与 `git status` 的三层对比模型同源，可对照 [u9-l3（status 与 wt-status）](u9-l3-status-wt-status.md)。
- **深入 diff 引擎**：本讲里 `run_diff_files`/`run_diff_index` 与 `:(attr)` 匹配都依赖 diff 与 pathspec 子系统，想理解 hunk 是怎么从差异算法产生的，读 [u8-l1（diff 核心引擎）](u8-l1-diff-core.md) 与 [u8-l2（xdiff）](u8-l2-xdiff-library.md)。
- **提交形成**：暂存之后如何从索引生成 commit 对象，见 [u9-l4（commit 创建与 log/pretty）](u9-l4-commit-log-pretty.md)。
