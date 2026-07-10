# index_state 与 cache_entry

## 1. 本讲目标

本讲是「索引 index」单元的第一讲。前面几个单元我们学完了对象数据库（blob/tree/commit/tag、松散对象、pack），知道了 git 怎么把内容存成不可变的对象。但你日常用 git 时，写一个文件、`git add`、再 `git commit`，中间一定有某个东西在「记着：这个文件已经准备好要进下一次提交了」。这个东西就是**索引（index）**，也就是暂存区（staging area）。

学完本讲，你应该能够：

- 说清索引在 git 三层数据模型（工作树 / 索引 / 对象数据库）中扮演的「桥梁」角色。
- 读懂 `struct cache_entry` 每个字段的含义，理解一条索引记录到底存了什么。
- 读懂 `struct index_state` 这个运行时结构，知道索引在内存里如何组织。
- 描述索引文件 `.git/index` 在磁盘上的序列化格式：文件头、条目区、扩展区、校验尾。
- 用 `git ls-files --stage`、`git update-index` 等命令对照源码，亲眼看到 `cache_entry` 的字段。

本讲只讲索引的**表示与读写**。`cache-tree`、`split-index`、`sparse-index` 等更复杂的索引机制留到本单元后续讲义（u4-l2、u4-l3）。

## 2. 前置知识

在进入源码前，先用直觉建立两个认知。

### 2.1 索引是「下一次提交的快照草稿」

可以把 git 的三层结构想象成三份清单：

| 层 | 是什么 | 你怎么看到它 |
|---|---|---|
| 工作树（working tree） | 你硬盘上真实的文件 | 直接 `ls`、用编辑器打开 |
| 索引（index / staging area） | 「下次提交要包含哪些文件、各自是哪个对象」的清单 | `git ls-files --stage` |
| 对象数据库（object database） | 不可变的 blob/tree/commit 对象 | `git cat-file -p <oid>` |

索引位于中间。`git add` 把工作树里的文件内容写成一个 blob 对象存进对象数据库，然后在索引里记一条：「路径 `a.txt` → 指向那个 blob 的哈希」。`git commit` 再把整张索引清单打包成一个 tree 对象，进而做成一个 commit 对象。换句话说：

- 索引 = 一张「路径 → 对象哈希」的表，外加每个文件的元信息。
- 提交 = 把这张表冻结成一个 tree。

> 与第三单元的衔接：上一讲我们知道 tree 对象也是「路径 → 对象」的清单。索引和 tree 的关键区别是：**索引还额外缓存了工作树文件的 stat 信息**（修改时间、inode 等），让 git 不必每次都读文件内容就能判断文件是否变过。这点是本讲的重点之一。

### 2.2 关键术语

- **cache_entry（CE）**：索引里的一条记录，对应一个路径。中文可叫「索引条目」。
- **stage（阶段）**：合并冲突时，同一路径在不同阶段会有多条 cache_entry，stage 取 0/1/2/3。stage 0 表示无冲突的正常条目。
- **stat 信息**：操作系统 `lstat()` 返回的文件元数据（mtime/ctime/inode/size 等），用来快速判断文件是否改动。
- **racy clean（竞争性干净）**：一个边角问题——如果一个文件的内容刚好和索引里记录的 size 一样、且 mtime 与索引文件本身相近，git 无法仅凭 stat 判断它是否真的没改，于是会把它的 size「抹脏」存成 0，强迫下次重新比对内容。

## 3. 本讲源码地图

本讲主要围绕下面四个文件：

| 文件 | 作用 |
|---|---|
| [read-cache-ll.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache-ll.h) | 索引的「低层」数据结构定义：`struct cache_entry`、`struct index_state`、磁盘头 `struct cache_header`、各种 CE 标志位、读写函数声明。「ll」= low-level。 |
| [read-cache.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.h) | 索引的「上层」内联小工具（如 `ce_mode_from_stat`、`ce_path_match`），包含 `read-cache-ll.h`。 |
| [read-cache.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c) | 索引读写的全部实现：磁盘 `struct ondisk_cache_entry`、`do_read_index`、`do_write_index`、条目增删查、stat 比对。本讲最核心的文件。 |
| [statinfo.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/statinfo.c) / [statinfo.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/statinfo.h) | `struct stat_data` 的定义，以及「把操作系统 stat 转成索引里存的 stat」「比对两份 stat」的实现。 |

> 小提示：git 的「索引」在源码里常常被叫做 **cache**（目录缓存 directory cache），所以函数名前缀是 `cache_`、结构体叫 `cache_entry`。名字虽老，指的就是同一个东西。

## 4. 核心概念与源码讲解

### 4.1 struct index_state 与 cache_entry

#### 4.1.1 概念说明

`struct index_state` 是「一整个索引文件在内存里的样子」，`struct cache_entry` 是「索引里的一条记录」。一个 `index_state` 持有一个 `cache_entry *` 指针数组，数组里每个元素就是一条路径记录。

为什么要分两个结构？因为索引是一个「集合 + 一堆辅助缓存」的复合体：核心是路径清单，但它还顺手维护了路径名哈希表（快速按名查找）、目录树缓存、未跟踪文件缓存、fsmonitor 状态等。把这些都挂在 `index_state` 上，避免全局变量满天飞。

#### 4.1.2 核心流程

索引在内存中的生命周期可以概括为：

```
index_state_init()        -- 构造一个空的 index_state（通常 the_repository->index）
        |
read_index_from()         -- 读 .git/index，解析成 cache_entry[] 填进去
        |
[各种命令增删改 cache_entry] -- add/checkout/merge 等修改 istate->cache[]
        |
write_locked_index()      -- 把内存里的 cache_entry[] 序列化回 .git/index
        |
discard_index()/release_index() -- 释放内存
```

关键点：`index_state` 是**唯一**的索引内存表示，所有子命令都共享 `the_repository->index` 这一个实例。

#### 4.1.3 源码精读

先看一条索引记录 `struct cache_entry`：

[read-cache-ll.h:22-32](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache-ll.h#L22-L32) —— 定义 `cache_entry`，这是索引里的一条记录：

```c
struct cache_entry {
	struct hashmap_entry ent;        /* 挂到 name_hash 用的哈希节点 */
	struct stat_data ce_stat_data;   /* 缓存的工作树 stat 信息 */
	unsigned int ce_mode;            /* 文件类型与权限（见 4.2） */
	unsigned int ce_flags;           /* stage、扩展标志、内存态标志 */
	unsigned int mem_pool_allocated; /* 是否从内存池分配 */
	unsigned int ce_namelen;         /* 路径名长度 */
	unsigned int index;	            /* for link extension */
	struct object_id oid;            /* 指向的 blob/tree/commit 对象哈希 */
	char name[FLEX_ARRAY];           /* 路径名，柔性数组，变长 */
};
```

几点要解释：

- `name[FLEX_ARRAY]` 是 C 的**柔性数组**技巧：`cache_entry` 末尾的 `name` 不占固定大小，分配时按实际路径长度多分配。所以两条不同长度的 cache_entry 大小不同，大小由宏 `cache_entry_size(len)` 计算（见 4.1.3 末尾）。
- `ent` 让 cache_entry 能挂进 `index_state` 的 `name_hash` 哈希表，实现按路径名 O(1) 查找，而不必每次线性扫描数组。
- `oid` 是这条记录指向的对象——对普通文件就是 blob 的哈希，对子模块（gitlink）是 commit 哈希。
- `mem_pool_allocated` 标记这条记录是从内存池分配的，释放时由内存池统一回收（见 4.3）。

再看整个索引 `struct index_state`：

[read-cache-ll.h:166-191](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache-ll.h#L166-L191) —— 定义 `index_state`，整个索引文件的内存表示：

```c
struct index_state {
	struct cache_entry **cache;          /* 条目指针数组，核心数据 */
	unsigned int version;                /* 索引文件格式版本 2/3/4 */
	unsigned int cache_nr, cache_alloc, cache_changed; /* 条目数/容量/脏标记 */
	struct string_list *resolve_undo;    /* 冲突解决撤销记录（REUC 扩展） */
	struct cache_tree *cache_tree;       /* 目录树哈希缓存（TREE 扩展） */
	struct split_index *split_index;     /* 拆分索引（link 扩展） */
	struct cache_time timestamp;         /* 索引文件本身的 mtime */
	unsigned name_hash_initialized : 1,  /* 一组 1 位状态标志 */
	         initialized : 1, ...;
	enum sparse_index_mode sparse_index; /* 是否处于稀疏索引模式 */
	struct hashmap name_hash;            /* 按路径名查找的哈希表 */
	struct hashmap dir_hash;             /* 按目录名查找的哈希表 */
	struct object_id oid;                /* 索引文件内容的校验哈希 */
	struct untracked_cache *untracked;   /* 未跟踪文件缓存（UNTR 扩展） */
	char *fsmonitor_last_update;         /* fsmonitor 状态（FSMN 扩展） */
	struct ewah_bitmap *fsmonitor_dirty;
	struct mem_pool *ce_mem_pool;        /* cache_entry 的内存池 */
	struct progress *progress;
	struct repository *repo;             /* 所属仓库 */
	struct pattern_list *sparse_checkout_patterns;
};
```

字段虽多，但可以分成四组理解：

1. **核心数据**：`cache`（条目数组）、`cache_nr`（条目数）、`cache_alloc`（数组容量）。这就是「索引清单」本身。
2. **脏标记**：`cache_changed` 是一个位图，记录索引相对磁盘被改了什么（条目增删、cache-tree 失效等），对应 [read-cache-ll.h:128-136](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache-ll.h#L128-L136) 里定义的 `SOMETHING_CHANGED`/`CE_ENTRY_ADDED`/`CACHE_TREE_CHANGED` 等位。
3. **扩展数据**：`cache_tree`、`split_index`、`resolve_undo`、`untracked`、`fsmonitor_*`——它们都对应索引文件**扩展区**里的一段（见 4.3）。也就是说，扩展区里有什么，`index_state` 上就挂什么。
4. **查找加速**：`name_hash`、`dir_hash`、`ce_mem_pool`。

初始化与释放一对函数：

[read-cache.c:2421-2425](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2421-L2425) —— `index_state_init` 把 `index_state` 清成空白（用 `INDEX_STATE_INIT` 宏的静态实例 memcpy 覆盖，只保留 `repo` 指针）。

[read-cache.c:2427-2461](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2427-L2461) —— `release_index` 释放索引全部资源（先 `validate_cache_entries` 自检所有条目确实来自内存池，再释放哈希、cache-tree、split-index、untracked、内存池等）；`discard_index` 则是「释放后再 init」，便于复用同一个变量。

最后看条目大小怎么算、怎么放进数组：

[read-cache-ll.h:126](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache-ll.h#L126) —— `cache_entry_size(len)` 计算一条路径长 `len` 的 cache_entry 需要多少字节（`offsetof(name) + len + 1`，那个 +1 是结尾的 `\0`）。

[read-cache.c:134-141](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L134-L141) —— `set_index_entry`：把一条 cache_entry 放进数组的第 `nr` 槽，并把它登记进 name_hash；若它是稀疏目录条目（`S_ISSPARSEDIR`），则把索引标记为 `INDEX_COLLAPSED`。

#### 4.1.4 代码实践

**实践目标**：亲手看到「一个 index_state 里挂着一个 cache_entry 数组」，并验证 cache_entry 的大小随路径长度变化。

**操作步骤**：

1. 在一个 git 仓库里随便建一个文件并 add：
   ```bash
   git init idx-demo && cd idx-demo
   echo hello > a.txt
   git add a.txt
   ```
2. 用 `git ls-files --stage` 查看索引（后面 4.3 会详解这个命令读的就是 `cache[]`）：
   ```bash
   git ls-files --stage
   ```
3. 阅读上面引用的 `struct cache_entry` 定义，结合 `cache_entry_size` 宏，**手算**一条路径为 `a.txt`（长度 5）的 cache_entry 字节数。

**需要观察的现象**：`git ls-files --stage` 会输出类似
```
<oid> 0	a.txt
```
其中第一列是 blob 的对象哈希（`oid`），中间的 `0` 是 stage。

**预期结果**：你能把输出的三列一一对应到 `cache_entry` 的 `oid`、`ce_flags` 里的 stage 位、`name` 字段。手算的 `cache_entry_size(5)` 应该等于 `offsetof(struct cache_entry, name) + 6`（5 字节路径名 + 1 字节 `\0`）。具体数值依赖平台指针大小，「待本地验证」确切字节数。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cache_entry` 用柔性数组 `name[FLEX_ARRAY]` 存路径名，而不是固定长度数组或 `char *` 指针？

> **答案**：固定长度数组会浪费空间（绝大多数路径远短于上限），`char *` 指针则要多一次内存分配和一次间接跳转。柔性数组让路径名紧贴结构体尾部，一次分配搞定，既省内存又对缓存友好。代价是同结构体不能简单 `= ` 赋值，要用 `copy_cache_entry`（[read-cache-ll.h:95-111](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache-ll.h#L95-L111)，注意它特意不拷 name 和哈希链）。

**练习 2**：`index_state` 里的 `cache_nr`、`cache_alloc`、`cache_changed` 各是什么含义？

> **答案**：`cache_nr` 是 `cache[]` 数组当前实际条目数；`cache_alloc` 是数组已分配的容量（按 `alloc_nr` 预留增长，避免每次插入都 realloc）；`cache_changed` 是脏位图，为 0 表示内存索引与磁盘一致、写回时可直接跳过。

---

### 4.2 索引条目字段：mode / oid / stat

#### 4.2.1 概念说明

一条 cache_entry 真正「有信息量」的字段是三个：`ce_mode`（这个文件是什么类型、什么权限）、`oid`（内容对应哪个对象）、`ce_stat_data`（工作树文件的 stat 快照）。理解这三个字段，就理解了索引为什么能当「工作树 ↔ 对象数据库」的桥梁。

- `ce_mode`：回答「这是一般文件、符号链接、还是子模块（gitlink）？可执行吗？」
- `oid`：回答「这个路径当前指向对象库里哪个不可变对象？」
- `ce_stat_data`：回答「我上次看到工作树里这个文件时，它的 mtime/inode/size 是什么？下次只要这些没变，我就不必重新读文件内容、重算哈希。」

#### 4.2.2 核心流程

git 判断「工作树里的文件是否与索引一致」走的是一条**先 stat、后内容**的快速路径：

```
对每条 cache_entry：
  lstat(工作树同名文件)  --> 得到当前 struct stat
  match_stat_data(ce_stat_data, 当前 stat)
        |-- 只比 mtime/ctime/inode/uid/gid/size 等元数据
        |-- 全相等 --> 判定「没改」（无需读内容），ce_uptodate
        |-- 有差异 --> 可能改了，再读文件内容、重算哈希与 ce->oid 比对
```

stat 比对是 O(1) 的系统调用，远比读全部内容+算哈希便宜。这正是索引缓存 stat 的意义。

#### 4.2.3 源码精读

**mode 的规范化**——`create_ce_mode`：操作系统给出的 `st_mode` 五花八门，索引里只存一个规范化的 `ce_mode`。

[object.h:133-143](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h#L133-L143) —— `create_ce_mode` 把任意 `st_mode` 规范成三类：符号链接 → `S_IFLNK`；目录或子模块 → `S_IFGITLINK`；普通文件 → `S_IFREG` 并只保留「属主可执行位」（有 `0100` 给 `0755`，否则 `0644`）。`ce_permissions` 就是那个 `0755`/`0644` 的二选一。注意：它定义在 `object.h` 而非 `read-cache-ll.h`，因为 tree 对象也复用了这套 mode 规范。

> 子模块在索引里用 `S_IFGITLINK` 表示，它的 `oid` 指向的是一个 **commit** 对象，而不是 blob。这点承接第三单元：`type_from_mode_gently`（[object.h:128-131](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h#L128-L131)）正是按 mode 反推对象类型。

**stat 信息结构**——`struct stat_data`：

[statinfo.h:16-24](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/statinfo.h#L16-L24) —— 索引里存的 stat 快照，是操作系统 `struct stat` 的精简版（全 `unsigned int`，固定 62 字节）：

```c
struct stat_data {
	struct cache_time sd_ctime;   /* 文件状态改变时间 */
	struct cache_time sd_mtime;   /* 内容修改时间 */
	unsigned int sd_dev;          /* 所在设备号 */
	unsigned int sd_ino;          /* inode 号 */
	unsigned int sd_uid;          /* 属主 */
	unsigned int sd_gid;          /* 属组 */
	unsigned int sd_size;         /* 文件大小（低 32 位） */
};
```

[statinfo.h:11-14](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/statinfo.h#L11-L14) —— `cache_time` 是「秒 + 纳秒」一对，注释明确说只存低 32 位、靠相等性判断就够。

**把系统 stat 填进 stat_data**：

[statinfo.c:24-35](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/statinfo.c#L24-L35) —— `fill_stat_data` 把 `struct stat` 的相关字段拷进 `stat_data`。注意 `sd_size` 经 `munge_st_size` 处理：

[statinfo.c:11-22](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/statinfo.c#L11-L22) —— `munge_st_size` 把 size 截成低 32 位；特别地，当文件大小恰是 4 GiB 的整数倍（截断后为 0）时，改存 `0x80000000`，避免被当成「size=0」的 racy-clean 信号（见 4.2.3 末尾）。

**stat 比对**——判断文件是否改动：

[statinfo.c:64-105](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/statinfo.c#L64-L105) —— `match_stat_data` 逐项比较 mtime/ctime/uid/gid/ino/dev/size，任一不同就置对应的 `*_CHANGED` 位（位定义在 [statinfo.h:35-41](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/statinfo.h#L35-L41)：`MTIME_CHANGED`/`DATA_CHANGED` 等）。是否比较 ctime/uid/gid/inode/dev 受 `core.trustctime`、`core.checkStat` 配置控制——在网络文件系统上这些字段不稳定，故可关闭。

**结合 mode 与 stat 的完整判定**：

[read-cache.c:311-353](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L311-L353) —— `ce_match_stat_basic`：先用 `ce_mode` 判断类型有没有变（普通文件/符号链接/子模块分别处理），再调用 `match_stat_data` 比对 stat，最后处理「racy smudged entry」——若 `sd_size` 被抹成 0 且 oid 不是空 blob，就置 `DATA_CHANGED`，强迫重新比内容。

> **racy clean 是什么**：当索引文件的 mtime 和某条目的 mtime 太接近（同一秒内），文件系统的秒级时间戳无法区分「先写文件后写索引」还是「先写索引后改文件」，git 保险起见会把这种条目的 size 存成 0（「抹脏」，见 4.3 的 `ce_smudge_racily_clean_entry`），下次 `status` 时强制重读内容确认。这是用空间（存 0）换正确性的典型取舍。

#### 4.2.4 代码实践

**实践目标**：观察 `ce_mode` 在磁盘上的体现，以及 stat 字段如何随文件改动而变化。

**操作步骤**：

1. 延续 4.1 的仓库，先看索引：
   ```bash
   git ls-files --stage
   # 记下 a.txt 的 oid 和 mode（100644）
   ```
2. 给 a.txt 加可执行位再 `add`，再看索引：
   ```bash
   chmod +x a.txt && git add a.txt && git ls-files --stage
   ```
3. 对照 `create_ce_mode`：mode 从 `100644` 变成 `100755`，对应 `ce_permissions` 里的 `0100` 被置位。
4. 用 `git cat-file -p <oid>` 查看新旧两个 blob 内容（应该都是 `hello\n`，但因为是不同对象、各自有独立 oid——实际上内容相同 git 会复用同一个 blob，可验证一下）。

**需要观察的现象**：`chmod +x` 后索引里 a.txt 的 mode 列变化，但 oid 可能不变（内容没变）。这说明 mode 和 oid 是**独立**的两个字段：改权限只动 mode，不改内容就不动 oid。

**预期结果**：你能解释「为什么改了文件权限 `git status` 也能发现」——因为 stat 里的 mode 与 `ce_mode` 不一致了。若平台不支持可执行位（如某些 Windows/挂载），「待本地验证」该现象是否出现。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `stat_data` 里所有字段都是 `unsigned int`（32 位），而不是直接存操作系统的 `struct stat`（其中 `ino_t`、`off_t` 常常是 64 位）？

> **答案**：索引要跨平台、跨机器一致（注释提到「为在 NFS 上透明使用而用大端序存储」）。固定 32 位让磁盘格式稳定紧凑。inode/设备号只用作「是否还是同一个 inode」的弱启发式相等判断，32 位在实践中已足够，碰撞概率极低且最多导致一次多余的内容比对，不会出错。

**练习 2**：`ce_mode` 为 `S_IFGITLINK` 的条目，它的 `oid` 指向哪种对象？为什么？

> **答案**：指向 commit 对象。`S_IFGITLINK` 表示这是一个子模块（gitlink），子模块在主仓库里记录的是「它检出到哪个 commit」，而不是某个文件内容。`type_from_mode_gently`（[object.h:128-131](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h#L128-L131)）正是把 `S_IFGITLINK` 映射成 `OBJ_COMMIT`。

---

### 4.3 索引读写与扩展区

#### 4.3.1 概念说明

索引文件 `.git/index`（又称 DIRC，directory cache）是一个二进制文件，结构非常规整：

```
+----------------------+
| cache_header (12B)    |  签名 "DIRC" + 版本 + 条目数
+----------------------+
| cache_entry 1         |  每条：stat + mode + flags + oid + name
| cache_entry 2         |
| ...                   |
+----------------------+
| 扩展区 extensions     |  若干个 {4字节签名, 4字节长度, 数据}
|  (TREE/REUC/link/...) |
+----------------------+
| 校验哈希 (20B/32B)     |  前面所有内容的哈希，防损坏
+----------------------+
```

理解这个布局后，「读索引」和「写索引」就是它的逆/正过程。**扩展区**是索引格式的精髓：git 不为每个新功能新开一个文件，而是在索引末尾追加一段「扩展」，用 4 字母签名标识。读的时候按签名分派。这种「TLV（type-length-value）扩展」设计让索引格式可向前扩展、向后兼容。

#### 4.3.2 核心流程

**读索引**（`do_read_index`）大致步骤：

```
1. open(.git/index) + mmap 整个文件进内存
2. verify_hdr()      -- 校验 "DIRC" 签名、版本范围、末尾校验哈希
3. 读 cache_header：拿 version 和 entries 数
4. 按 entries 数，逐条 create_from_disk() 解析 cache_entry，填进 cache[]
   （条目多时可多线程，靠 IEOT/EOIE 扩展切分块）
5. 解析扩展区：read_index_extension() 按 4 字母签名分派
6. 记录索引文件本身的 mtime 到 istate->timestamp（用于后续判断索引是否被外部改动）
```

**写索引**（`do_write_index`）是镜像过程：先写头，再逐条 `ce_write_entry`，再依次写各扩展，最后 `finalize_hashfile` 算并追加校验哈希。整个过程写到**临时文件**，校验通过后再原子改名替换 `.git/index`（锁文件机制见 u14-l2）。

#### 4.3.3 源码精读

**文件头**：

[read-cache-ll.h:12-17](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache-ll.h#L12-L17) —— 磁盘头：签名 `CACHE_SIGNATURE = 0x44495243`（ASCII 即 `"DIRC"`）、版本号、条目数。版本范围 [read-cache-ll.h:19-20](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache-ll.h#L19-L20) 限定为 2~4（`INDEX_FORMAT_LB`=2，`INDEX_FORMAT_UB`=4）。

**磁盘上的条目格式** `ondisk_cache_entry`：

[read-cache.c:1668-1685](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L1668-L1685) —— 磁盘条目布局：ctime/mtime/dev/ino/mode/uid/gid/size 各 4 字节（全部大端序），后接哈希、2 字节 flags（若 `CE_EXTENDED` 再加 2 字节 flags2）、变长文件名。注释说明这正是为 NFS 跨机一致而用大端序。注意它和内存 `cache_entry` 不是同一个结构——内存版多了 `ent`、`mem_pool_allocated`、`ce_namelen` 等运行时字段，二者靠 `copy_cache_entry_to_ondisk` / `create_from_disk` 互转。

**读：校验头**：

[read-cache.c:1702-1731](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L1702-L1731) —— `verify_hdr`：检查签名是不是 `DIRC`、版本是否在 2~4；若开了 `verify_index_checksum`，则重算前文哈希与文件末尾存的哈希比对。

**读：读索引主流程**：

[read-cache.c:2199-2335](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2199-L2335) —— `do_read_index`：open→fstat→mmap→`verify_hdr`→读 header→`CALLOC_ARRAY(cache)`→（可选多线程）`load_all_cache_entries`→读扩展→记录 timestamp→按需 `ensure_full_index`/`ensure_correct_sparsity`。其中 [read-cache.c:2053-2070](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2053-L2070) 的 `load_all_cache_entries` 会先建一个 `mem_pool`，所有 cache_entry 从这个池里分配——这正是 `mem_pool_allocated` 字段和 `validate_cache_entries` 的由来（释放时整池回收，不必逐条 free）。

[read-cache.c:1782-1882](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L1782-L1882) —— `create_from_disk`：把磁盘字节解析成内存 cache_entry。关键细节：
- 磁盘 flags 只存低 16 位；高 16 位（如 `CE_INTENT_TO_ADD`、`CE_SKIP_WORKTREE`）只有设了 `CE_EXTENDED` 位时才额外存 2 字节（[read-cache.c:1808-1818](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L1808-L1818)）。
- **v4 的路径名压缩**：相邻条目常共享路径前缀，v4 只存「从上一条名末尾去掉多少字节 + 追加的新字节」（[read-cache.c:1795-1834](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L1795-L1834)），用 varint 编码 strip 长度，显著缩小大仓库的索引体积。

**读：扩展区分派**：

[read-cache.c:69-76](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L69-L76) —— 各扩展的 4 字母签名常量（`TREE`/`REUC`/`link`/`UNTR`/`FSMN`/`EOIE`/`IEOT`/`sdir`）。

[read-cache.c:1733-1769](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L1733-L1769) —— `read_index_extension`：经典 `switch(CACHE_EXT(ext))` 分派——`TREE`→读 cache-tree、`REUC`→读 resolve-undo、`link`→读 split-index、`UNTR`→读 untracked 缓存、`FSMN`→读 fsmonitor、`sdir`→把索引标记为 `INDEX_COLLAPSED`（稀疏索引标志）。**未知的扩展**：若首字母是大写字母就忽略并警告，小写字母才报错（[read-cache.c:1761-1766](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L1761-L1766)）——这是 git 为未来版本预留的兼容机制：大写=实验性可忽略，小写=核心不可忽略。

> 因此扩展区是一个**双向兼容**的设计：旧版 git 遇到新扩展会安全跳过；新版 git 遇到旧索引也不会缺关键字段。

**读：上层入口与 split-index**：

[read-cache.c:2349-2414](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2349-L2414) —— `read_index_from`：先 `do_read_index` 读 `.git/index`；若其中有 `link` 扩展（split-index），再读共享的 `sharedindex.<oid>` 并 `merge_base_index` 合并；最后 `post_read_index_from` 做 `check_ce_order`（校验条目按名排序）、按配置微调 untracked/split/fsmonitor。`is_index_unborn`（[read-cache.c:2416-2419](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2416-L2419)）判断「索引是否还不存在（刚 init 的空仓库）」。

**写：条目落盘**：

[read-cache.c:2612-2637](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2612-L2637) —— `copy_cache_entry_to_ondisk`：把内存 cache_entry 的 stat/mode/oid/flags 拷成大端序的磁盘布局，是 `create_from_disk` 的逆操作。flags 处理见 [read-cache.c:2631-2636](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2631-L2636)：低 12 位存路径名长度（`CE_NAMEMASK`，超出则存满 `0xfff` 并靠结尾 `\0` 定界），`CE_EXTENDED` 位决定是否多写 2 字节扩展 flags。

[read-cache.c:2639-2690](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2639-L2690) —— `ce_write_entry`：把一条条目写进 hashfile（边写边算哈希）。v4 时用 `previous_name` 做前缀压缩（[read-cache.c:2661-2683](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2661-L2683)），否则直接写全名并对齐填充到 8 字节边界（v2/v3 的 `align_padding_size`）。

**写：写索引主流程**：

[read-cache.c:2807-2945](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2807-L2945) —— `do_write_index` 头部与条目循环：先扫描决定是否需要 `extended`（v3）、决定版本（v2/v3 自动降级，[read-cache.c:2846-2853](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2846-L2853)），写 header，再循环 `ce_write_entry` 逐条写，跳过标了 `CE_REMOVE` 的条目，并对 racy 条目调 `ce_smudge_racily_clean_entry`（[read-cache.c:2903-2904](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2903-L2904)）。

[read-cache.c:2982-3071](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2982-L3071) —— 扩展区写入：按固定顺序写各扩展（`IEOT`→`link`→`TREE`→`REUC`→`UNTR`→`FSMN`→`sdir`→`EOIE`），每个扩展都经 `write_index_ext_header`（[read-cache.c:2544-2559](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L2544-L2559)，写「签名 + 长度」8 字节）再写数据。`EOIE`（end-of-index-entries）必须最后写，便于多线程读时先定位扩展区起点。

[read-cache.c:3073-3078](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L3073-L3078) —— `finalize_hashfile`：把累计算好的哈希追加到文件末尾作为校验尾，正是 `verify_hdr` 比对的那段。

[read-cache.c:3310-3337](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L3310-L3337) —— `write_locked_index`：对外写入口。先处理 `SKIP_IF_UNCHANGED`（脏位图为 0 就跳过），再决定走 split-index 还是普通写入，最终都落到 `do_write_locked_index`→`do_write_index`→`commit_locked_index`（原子改名）。

**版本号怎么定**：

[read-cache.c:1629-1658](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L1629-L1658) —— 默认版本 `INDEX_FORMAT_DEFAULT = 3`；`get_index_format_default` 会查环境变量 `GIT_INDEX_VERSION`、配置 `index.version`，范围不合法时回退默认。

#### 4.3.4 代码实践

**实践目标**：用 `git ls-files --stage` 与 `git update-index` 实操索引，再对照源码确认每列对应 `cache_entry` 的哪个字段；最后用原始十六进制看到「DIRC」头与扩展区签名。

**操作步骤**：

1. 在 4.1 的仓库里再建一个文件并 add，制造两条索引记录：
   ```bash
   echo world > b.txt && git add b.txt
   git ls-files --stage
   ```
2. 用 `git update-index` 直接操作索引——把 a.txt 的索引条目换成「空 blob」对应的 `refresh`，或更直观地用 `--cacheinfo` 手动塞一条记录：
   ```bash
   # 手工向索引塞一条：mode 100644、oid 用 b.txt 的 oid、路径 c.txt
   B_OID=$(git rev-parse :b.txt)
   git update-index --add --cacheinfo 100644,"$B_OID",c.txt
   git ls-files --stage
   ```
   这里 `--cacheinfo` 直接绕过工作树、改的就是内存 `index_state->cache[]`，写回 `.git/index`。
3. 对照 [read-cache.c:844-872](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L844-L872) 的 `make_cache_entry`：它正是用 `mode`、`oid`、`path`、`stage` 这几个参数构造 cache_entry——和 `--cacheinfo` 的四个参数一一对应！
4. 用 `od`/`xxd` 看索引文件原始字节，验证格式：
   ```bash
   xxd .git/index | head -2     # 头 4 字节应为 44 49 52 43 ("DIRC")
   xxd .git/index | grep -i 'TREE\|REUC'   # 在尾部扩展区找签名
   ```
5. （可选）开启校验自检，观察读索引时的校验：
   ```bash
   GIT_TEST_VERIFY_CACHE_TREE=1 git status
   ```

**需要观察的现象**：
- `git ls-files --stage` 三列：`<oid> <mode> <stage>\t<path>`，正好对应 `cache_entry` 的 `oid`、`ce_mode`、`ce_flags>>12`、`name`。
- `xxd` 头部能看到 `DIRC`，版本号（通常 `00 00 00 03` 或 `04`），条目数；文件尾部能看到 `TREE` 等扩展签名和最后的 20 字节哈希。

**预期结果**：你能把 `ls-files --stage` 的每一列和 `--cacheinfo` 的每个参数，分别映射到 `cache_entry` 的字段和 `make_cache_entry` 的形参。`xxd` 看到的 `DIRC` 对应 `CACHE_SIGNATURE`，扩展签名对应 `CACHE_EXT_*` 常量。若 `grep` 不到 `TREE`，可能是因为尚未提交导致无 cache-tree，「待本地验证」。

> 如果不方便编译运行，这也提供一个**源码阅读型实践**：沿 `cmd_ls_files`（[builtin/ls-files.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/ls-files.c)）→ `read_index_from` → `do_read_index` → `create_from_disk` 跟一遍读索引链路，再沿 `cmd_update_index` → `add_index_entry` → `write_locked_index` → `do_write_index` → `ce_write_entry` 跟一遍写索引链路，画出时序图。

#### 4.3.5 小练习与答案

**练习 1**：索引 v4 比 v2/v3 小，主要省在哪里？代价是什么？

> **答案**：主要省在**路径名压缩**：v4 相邻条目只存与前一条名不同的后缀（`create_from_disk` 的 `expand_name_field` 分支，[read-cache.c:1820-1834](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L1820-L1834)），对 `src/a/...`、`src/b/...` 这种同前缀大量文件的大仓库能省非常多字节。代价是读写都要维护「上一条名字」状态、解码 varint，CPU 开销略高；且 v4 不再做 8 字节对齐填充。此外 v4 字段可能不对齐，所以用 `get_be16/get_be32/oidread` 而非直接 struct 强转（见 [read-cache.c:1771-1781](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L1771-L1781) 注释）。

**练习 2**：如果未来 git 要在索引里新增一种「文件来源标注」信息，按现有设计应该怎么做？为什么不会破坏旧版 git？

> **答案**：新增一个 4 字母大写签名的扩展（例如 `SRC `），在 `read_index_extension` 的 switch 里加一个 `case`，在 `do_write_index` 里加一段写入。旧版 git 读到该扩展时，因首字母大写会走 `default` 分支「忽略并警告」（[read-cache.c:1761-1766](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L1761-L1766)），不会报错也不会丢失其它数据——这正是扩展区 TLV 设计的兼容性保障。

**练习 3**：为什么写索引要写到临时文件再改名，而不是直接覆盖 `.git/index`？

> **答案**：防止写到一半崩溃导致索引损坏。`.git/index` 是核心数据，直接覆盖若中途断电会留下半截文件。写到临时文件、算完校验哈希、再原子 `rename` 替换，要么是完整的新索引、要么还是旧的完整索引，不会出现中间态。这配合 `lockfile` 机制（详见 u14-l2）实现崩溃一致性。读侧 `verify_hdr` 的校验哈希则是第二道防线。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面的端到端观察任务。

**背景**：我们要从「一次 `git add`」出发，跟踪它对索引的全部影响，并在磁盘字节层面验证。

**任务**：

1. 准备：
   ```bash
   git init idx-final && cd idx-final
   printf 'v1\n' > f.txt
   ```
2. **add 前**：索引尚不存在（`is_index_unborn` 为真），确认 `.git/index` 不存在。
3. **执行 `git add f.txt`**，然后：
   - 用 `git ls-files --stage` 读出索引，记录 f.txt 的 `mode`、`oid`、`stage`。
   - 用 `git cat-file -t <oid>` 确认 oid 是 blob（对应 `ce_mode` 是 `S_IFREG`）。
   - 用 `git cat-file -p <oid>` 看内容是不是 `v1\n`。
4. **修改文件并再次 add**：`printf 'v2\n' > f.txt && git add f.txt`，再次 `ls-files --stage`：
   - 观察 oid 变化（内容变 → 新 blob → 新 oid）。
   - 用 `xxd .git/index` 看头部仍是 `DIRC`，条目数仍为 1，但尾部哈希变了（内容改了）。
5. **对照源码解释**：
   - `git add` 内部走 `add_to_index`（[read-cache.c:712](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L712)）→ 读文件算哈希写 blob → `make_cache_entry`（[read-cache.c:844-872](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L844-L872)）构造/更新 cache_entry → `write_locked_index`（[read-cache.c:3310](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L3310)）落盘。
   - 解释你看到的 `mode`、`oid`、`stage` 分别来自 cache_entry 的哪个字段；解释为什么文件改了索引里条目数却不变（同路径替换，走 `replace_index_entry` 而非新增，[read-cache.c:143-155](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c#L143-L155)）。
6. **提交**：`git commit -m init` 后再用 `xxd .git/index | grep TREE` ——你应该能看到 `TREE` 扩展出现了（cache-tree 在提交生成 tree 后被写入），印证 4.3 讲的扩展区随操作动态增减。

**验收标准**：你能用一段话，把「工作树文件 → blob 对象 → cache_entry → .git/index 文件头/条目/扩展/校验尾」这条链完整讲清，并指出每个环节对应的源码函数。涉及耗时或现象不确定处，标注「待本地验证」。

## 6. 本讲小结

- 索引（index / staging area / directory cache）是工作树与对象数据库之间的桥梁，本质是一张「路径 → 对象哈希 + stat 快照」的表；内存里由 `struct index_state` 表示，每条记录是 `struct cache_entry`。
- `cache_entry` 三个核心字段：`ce_mode`（规范化的文件类型/权限，`create_ce_mode` 限定为 reg/link/gitlink 三类）、`oid`（指向 blob 或子模块 commit）、`ce_stat_data`（缓存的工作树 stat，用于免读内容快速判变）。
- stat 比对走「先 stat 后内容」的快速路径：`match_stat_data` 比 mtime/ctime/inode/size 等元数据，全等即判未改；racy-clean 边角情形靠把 size 抹成 0 强迫重比。
- `.git/index` 磁盘格式 = `DIRC` 文件头 + 条目区 + 扩展区 + 校验哈希尾；条目用大端序、固定 32 位字段以保证跨平台/NFS 一致。
- 扩展区是 TLV 设计，按 4 字母签名分派（`TREE`/`REUC`/`link`/`UNTR`/`FSMN`/`sdir` 等），大写未知扩展被安全忽略，使索引格式前后向兼容。
- 读索引 = `do_read_index`（mmap→校验→`create_from_disk`→读扩展），写索引 = `do_write_index`（写头→`ce_write_entry`→写扩展→`finalize_hashfile`→原子改名）；条目都从内存池分配、整池回收。

## 7. 下一步学习建议

本讲只讲了索引的「表示与读写」这一最基础模块。接下来建议：

- **u4-l2 缓存树 cache-tree 与 split-index**：本讲多次出现的 `TREE` 扩展和 `link` 扩展到底存了什么、如何加速 `git write-tree` 与多工作树场景，在那里深入。
- **u4-l3 稀疏索引 sparse-index**：本讲的 `sparse_index` 字段、`INDEX_COLLAPSED` 模式、`sdir` 扩展、`ensure_full_index` 都只是点到，稀疏索引如何压缩巨型单仓库索引是它的主题。
- **u9-l1 git add 与 update-index**：本讲侧重数据结构，那一讲侧重「命令层」——`add`/`update-index` 如何用 pathspec 选择路径、如何调用本讲的 `add_to_index`/`add_index_entry` 改索引。
- **u14-l2 临时文件与文件锁**：本讲提到写索引走临时文件 + 原子改名，锁文件机制（`struct lock_file`）的完整细节在那里。

继续阅读建议：直接打开 [read-cache.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/read-cache.c)，从 `do_read_index` 和 `do_write_index` 两个大函数读起，对照本讲的字段表，是巩固本讲内容最快的方式。
