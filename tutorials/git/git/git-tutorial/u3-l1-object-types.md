# 四种对象类型与 struct object

## 1. 本讲目标

本讲是「对象模型与对象存储」单元的第一讲。学完后你应该能够：

- 说出 git 的四种一等对象类型（blob / tree / commit / tag）各自记录什么、彼此如何引用。
- 看懂 `object.h` 里的 `enum object_type`，并理解类型值在磁盘、pack 文件与内存三者之间如何流动。
- 理解 `struct object` 作为「统一基类」的设计：四种具体结构体如何通过把它放在第一个成员位置来实现 C 风格的继承。
- 看懂 `lookup_blob / lookup_tree / lookup_commit / lookup_tag` 的统一模式，以及 `object_as_type` 如何做类型强转（含 commit 的特例）。
- 读懂「对象标志位分配表」这份注释，理解 29 个标志位为什么是一份需要协调的全局资源。

本讲只讲「对象在内存里长什么样、如何被分类」，不讲对象的哈希计算与磁盘压缩——那是下一讲（u3-l2）的内容。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念（它们在前置讲义里已出现过，这里只做最小回顾）：

- **内容寻址（content addressing）**：git 不用「文件名」找数据，而是用「内容的哈希」找数据。一个对象的哈希（git 内部叫 `oid`，object id）就是它的唯一身份证。SHA-1 下哈希是 40 个十六进制字符，SHA-256 下是 64 个。
- **对象（object）**：git 仓库里所有持久化的内容都是「对象」。本讲关心四种「真正的」对象类型。
- **快照（snapshot）**：git 记录的是某个时刻整个目录树的快照，而不是「相对于上一次的逐行改动」（这是它与 CVS/SVN 的根本区别，见 u1-l1）。

四种对象用一句话概括：

| 类型 | 记录什么 | 通俗类比 |
|------|----------|----------|
| **blob** | 一个文件的内容（不含文件名） | 一份纯文件内容 |
| **tree** | 一个目录：里面有哪些文件/子目录，各自的名字、权限和对象哈希 | 一份目录清单 |
| **commit** | 一次快照：指向一个顶层 tree、零或多个父 commit、作者/提交者/留言 | 一张「存档点」卡片 |
| **tag** | 给某个对象（通常是 commit）起的带说明的名字指针 | 一张带注释的书签 |

它们之间的引用关系如下图（箭头表示「指向」）：

```
        tag ──┐
              ▼
   commit ──► commit (parent)
      │
      ▼
     tree ──► tree (子目录)
      │
      ▼
     blob (文件内容)
```

也就是说：**commit 引用 tree 和 parent commit；tree 引用 blob 和子 tree（以及子模块的 commit）；tag 可以引用任意一种对象**。这张引用网就是 git 历史。

> 名词提醒：下面源码里你会频繁看到 `oid`（`struct object_id`，即对象哈希）、`parsed`（是否已解析）、`flags`（标志位）。先记住名字，后文逐一解释。

## 3. 本讲源码地图

本讲涉及的文件不多，但都是对象子系统的核心：

| 文件 | 作用 |
|------|------|
| [object.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h) | 定义 `enum object_type`、`struct object` 基类、标志位分配表注释，以及对象哈希表的查找/创建接口 |
| [object.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.c) | 实现 `type_name`（类型↔字符串）、`lookup_object`、`create_object`、`object_as_type`（类型强转）、按类型分发的 `parse_object_buffer` |
| [blob.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/blob.h) / [blob.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/blob.c) | `struct blob`（最简单的对象）与 `lookup_blob` |
| [tree.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/tree.h) / [tree.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/tree.c) | `struct tree` 与 `lookup_tree` |
| [commit.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit.h) | `struct commit`、`struct commit_list`（commit 如何引用 tree 与 parent） |
| [tag.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/tag.h) / [tag.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/tag.c) | `struct tag` 与 `lookup_tag`，以及 tag 内容的解析 |
| [alloc.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/alloc.c) | 对象的「slab 分配器」，含 `union any_object` 与各类 `alloc_*_node` |
| [revision.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.h) | 领用标志位的子系统之一，定义 `SEEN`/`UNINTERESTING` 等位名 |

> 阅读建议：先看 `object.h` 的三段（枚举、struct、标志位表），建立全局观；再带着它去对照四个具体结构体和 `object.c` 的强转逻辑。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **enum object_type 四种类型** —— 类型如何在磁盘 / pack / 内存三处表示。
2. **struct object 统一基类** —— 四种结构体如何共用同一组「身份证字段」。
3. **对象标志位分配表** —— 29 个标志位如何成为一份需要协调的全局资源。

### 4.1 enum object_type 四种类型

#### 4.1.1 概念说明

每一个 git 对象都有一个**类型**。这个类型决定了：

- 对象**内容怎么解释**（tree 的内容是一串「权限 名字 哈希」条目；commit 的内容是带表头的文本；blob 的内容就是原始字节）。
- 对象**如何被解析**（见 `object.c` 里按类型分发的 `parse_object_buffer`）。

git 在 C 层用一个枚举 `enum object_type` 给类型编号。这个编号会在三个地方出现：

1. **磁盘上（松散对象）**：对象内容前面有一行明文头，例如 `blob 1234\0`，其中 `blob` 就是类型名。
2. **pack 文件里**：类型用一个 **3 位的二进制字段**编码（这正是 `TYPE_BITS = 3` 的来源）。
3. **内存里（`struct object`）**：类型存进 `struct object.type` 这个 3 位位域。

因为 pack 格式只用 3 位存类型，所以类型总数被限制在 \(2^3 = 8\) 个以内。git 用掉了其中 4 个「真类型」、2 个「delta 类型」、1 个留空、1 个待扩展。

#### 4.1.2 核心流程

类型值在不同表示之间的流转：

```text
明文类型名 "blob"                  ← 磁盘头 / cat-file 输出
      │  type_from_string_gently() │  type_name()
      ▼                            ▼
   enum object_type (OBJ_BLOB = 3) ← 内存 / pack 中的 3 位编码
      │
      │  object_as_type() / parse_object_buffer()
      ▼
   分派到 lookup_blob() 等具体处理
```

要点：

- `OBJ_NONE = 0` 不是一种「真的类型」，而是「**还不知道这是什么类型**」。一个对象刚被分配、尚未读盘确认类型时就是 `OBJ_NONE`。
- `OBJ_BAD = -1` 表示出错（类型未知或损坏）。
- `OBJ_OFS_DELTA = 6` 与 `OBJ_REF_DELTA = 7` 是 **pack 专用的 delta 类型**，表示「这个对象是相对某个 base 对象的差量」，它们不会作为内存中的「一等对象」存在。
- `OBJ_ANY` 是一个通配值，用于「任意类型都行」的查找场景。

#### 4.1.3 源码精读

枚举本身定义在 [object.h:98-110](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h#L98-L110)，注意每个值上方/旁边的注释，尤其是那句「3 位范围之外的值属于 pack 文件格式」：

```c
enum object_type {
	OBJ_BAD = -1,
	OBJ_NONE = 0,
	OBJ_COMMIT = 1,
	OBJ_TREE = 2,
	OBJ_BLOB = 3,
	OBJ_TAG = 4,
	/* 5 for future expansion */
	OBJ_OFS_DELTA = 6,
	OBJ_REF_DELTA = 7,
	OBJ_ANY,
	OBJ_MAX
};
```

位宽常量在 [object.h:90-92](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h#L90-L92)：`FLAG_BITS = 29`、`TYPE_BITS = 3`。

枚举值与字符串的映射在 [object.c:29-42](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.c#L29-L42)。数组下标恰好等于枚举值，所以 `type_name(OBJ_BLOB)` 直接返回 `"blob"`：

```c
static const char *object_type_strings[] = {
	NULL,		/* OBJ_NONE = 0 */
	"commit",	/* OBJ_COMMIT = 1 */
	"tree",		/* OBJ_TREE = 2 */
	"blob",		/* OBJ_BLOB = 3 */
	"tag",		/* OBJ_TAG = 4 */
};
const char *type_name(unsigned int type) { ... return object_type_strings[type]; }
```

反向映射（字符串→枚举）在 [object.c:44-59](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.c#L44-L59) 的 `type_from_string_gently`，靠线性扫描 + `xstrncmpz` 比对。

此外，tree 条目里记录的是**文件权限 mode**（不是类型枚举），需要把 mode 翻译成对象类型。这个翻译就是 [object.h:126-131](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h#L126-L131) 的内联函数 `object_type()`：

```c
static inline enum object_type object_type(unsigned int mode)
{
	return S_ISDIR(mode) ? OBJ_TREE :
		S_ISGITLINK(mode) ? OBJ_COMMIT :
		OBJ_BLOB;
}
```

它说明：目录 → tree；子模块链接（gitlink）→ commit；其余（普通文件、符号链接）→ blob。这一条把「目录树里的 mode」和「对象类型枚举」对接起来。

#### 4.1.4 代码实践

**实践目标**：用 `git cat-file -t` 观察四种对象类型，亲眼看到枚举对应的字符串。

**操作步骤**：

1. 找一个已有提交的 git 仓库（可以用 git 自身源码仓库，或随便一个有历史的项目）。
2. 找到一个 commit 的哈希，查看它的类型与内容：
   ```bash
   git cat-file -t HEAD          # 期望输出: commit
   git cat-file -p HEAD          # 打印 commit 内容，里面有 tree / parent 行
   ```
3. 从上一步输出的 `tree <hash>` 行取出 tree 哈希，查看它：
   ```bash
   git cat-file -t <tree-hash>   # 期望输出: tree
   git cat-file -p <tree-hash>   # 打印目录条目，每行带 mode + 类型 + 哈希 + 名字
   ```
4. 从 tree 条目里挑一个普通文件的哈希（mode 为 `100644` 的那行），查看 blob：
   ```bash
   git cat-file -t <blob-hash>   # 期望输出: blob
   ```
5. （tag 可能没有）若有 annotated tag，例如 `git tag -a v0.1 -m "first"` 后：
   ```bash
   git cat-file -t v0.1          # 期望输出: tag
   git cat-file -p v0.1          # 内容里有 object / type / tag / tagger 行
   ```

**需要观察的现象**：四种命令分别打印出 `blob` / `tree` / `commit` / `tag`，正好对应 [object.c:29-35](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.c#L29-L35) 里那张字符串表。

**预期结果**：你会看到 `cat-file -p HEAD` 输出形如：

```
tree <40位哈希>
parent <40位哈希>      # 首个提交没有 parent 行
author ...
committer ...

<提交说明>
```

这正好对应 commit 对象的内容格式。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `OBJ_NONE` 的值是 `0` 而不是 `OBJ_COMMIT`？

**参考答案**：因为对象刚分配、尚未读盘时，整块内存被 `memset` 清零（见 `alloc.c` 的 `alloc_node`），清零后 `type` 自然是 0。让 `0` 表示「未知类型」就能用「全零 = 未初始化」这一默认状态，无需额外赋值。这也是 `type_name(OBJ_NONE)` 返回 `NULL` 的原因。

**练习 2**：`OBJ_OFS_DELTA` 和 `OBJ_REF_DELTA` 为什么不算「一等对象类型」？

**参考答案**：它们只出现在 pack 文件里，表示「本对象是相对某个 base 的差量」。读 pack 时 git 会先解析 delta、还原出真正的 blob/tree/commit/tag，再交给上层。也就是说，内存中的 `struct object` 永远只会是那四种真类型之一，delta 只是 pack 的存储压缩手段。所以枚举注释特别提醒「3 位范围之外的值属于 pack 格式」。

### 4.2 struct object 统一基类

#### 4.2.1 概念说明

四种对象类型虽然内容千差万别，但它们都共享一组**公共字段**：

- `parsed`：这个对象是否已经被「解析」过（即内容是否已被读进来并填好了结构体）。
- `type`：对象类型（3 位）。
- `flags`：标志位（29 位），用于遍历/协商等过程中的临时记账。
- `oid`：对象的哈希身份证。

git 把这组公共字段抽成一个 `struct object`，然后让四种具体结构体都**把 `struct object` 放在第一个成员的位置**：

```c
struct blob  { struct object object; /* 无额外字段 */ };
struct tree  { struct object object; void *buffer; unsigned long size; };
struct commit{ struct object object; timestamp_t date; struct commit_list *parents; ... };
struct tag   { struct object object; struct object *tagged; char *tag; timestamp_t date; };
```

这是 C 语言里实现「继承」的经典手法：因为第一个成员的地址等于整个结构体的地址，所以**任何一种具体对象的指针都可以安全地被当成 `struct object *` 来用**。这样上层代码（比如遍历历史、收集对象）就可以只操作 `struct object *`，不关心到底是 blob 还是 commit；需要具体信息时再向下转型。

`struct object` 因此扮演了「**统一基类**」的角色，`parsed / type / flags / oid` 就是所有对象共有的「身份证 + 状态」。

#### 4.2.2 核心流程

对象的「查找或创建」走的是统一模式，四种类型一模一样。以 blob 为例：

```text
lookup_blob(r, oid):
  obj = lookup_object(r, oid)        # 先去全局对象哈希表里找
  if (没找到)
      return create_object(r, oid, alloc_blob_node(r))   # 新建一个带类型的节点
  else
      return object_as_type(obj, OBJ_BLOB, 0)            # 已存在 → 强转确认类型
```

其中：

- `lookup_object`（查哈希表）和 `create_object`（建表项）对所有类型通用，定义在 `object.c`。
- `alloc_blob_node / alloc_tree_node / alloc_tag_node` 负责分配并**立即把 `type` 设成具体类型**；而 `alloc_object_node` 把 `type` 设成 `OBJ_NONE`（用于「还不知道类型」的场景）。
- `object_as_type` 是关键的「类型强转」函数：它确认一个已存在的对象确实是指定类型，必要时把 `OBJ_NONE` 升级为真类型。**commit 有特例**：从 `OBJ_NONE` 升级成 commit 时，除了设类型，还要给它分配一个全局序号 `index`（用于 commit-slab 关联数据，见 u7-l2）。

#### 4.2.3 源码精读

先看基类本身 [object.h:159-164](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h#L159-L164)：

```c
struct object {
	unsigned parsed : 1;
	unsigned type : TYPE_BITS;
	unsigned flags : FLAG_BITS;
	struct object_id oid;
};
```

位宽常量 `TYPE_BITS = 3`、`FLAG_BITS = 29` 见 [object.h:90-92](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h#L90-L92)。

四个具体结构体都把基类放在第一位：
- blob：[blob.h:8-10](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/blob.h#L8-L10)（最干净，除了基类什么都没有）。
- tree：[tree.h:10-14](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/tree.h#L10-L14)（多出 `buffer` 和 `size`，即目录条目的原始字节）。
- commit：[commit.h:27-39](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit.h#L27-L39)。
- tag：[tag.h:8-13](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/tag.h#L8-L13)。

四种 `lookup_*` 函数是同一模式的四个副本，最具代表性的是最短的 [blob.c:7-13](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/blob.c#L7-L13)：

```c
struct blob *lookup_blob(struct repository *r, const struct object_id *oid)
{
	struct object *obj = lookup_object(r, oid);
	if (!obj)
		return create_object(r, oid, alloc_blob_node(r));
	return object_as_type(obj, OBJ_BLOB, 0);
}
```

tree 与 tag 完全对应：[tree.c:167-173](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/tree.c#L167-L173)、[tag.c:97-103](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/tag.c#L97-L103)。

类型强转函数 [object.c:165-183](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.c#L165-L183) 是本模块的核心，注意三条分支：

```c
void *object_as_type(struct object *obj, enum object_type type, int quiet)
{
	if (obj->type == type)              /* 类型已一致，直接用 */
		return obj;
	else if (obj->type == OBJ_NONE) {   /* 还不知道类型 → 升级 */
		if (type == OBJ_COMMIT)
			init_commit_node((struct commit *) obj);  /* commit 特例 */
		else
			obj->type = type;
		return obj;
	}
	else {                              /* 类型对不上 → 报错 */
		if (!quiet) error(...);
		return NULL;
	}
}
```

commit 的特例 `init_commit_node` 在 [alloc.c:118-122](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/alloc.c#L118-L122)，它多做一件事——分配全局序号 `index`：

```c
void init_commit_node(struct commit *c)
{
	c->object.type = OBJ_COMMIT;
	c->index = alloc_commit_index();
}
```

分配器层面，[alloc.c:79-105](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/alloc.c#L79-L105) 的 `alloc_blob_node / alloc_tree_node / alloc_tag_node` 各自把 `type` 设成具体类型，唯独 `alloc_object_node` 设成 `OBJ_NONE`。而 `alloc_object_node` 分配的大小是 `sizeof(union any_object)`，这个联合体（[alloc.c:22-28](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/alloc.c#L22-L28)）把四种结构体并在一起，取其中最大的尺寸：

```c
union any_object {
	struct object object;
	struct blob blob;
	struct tree tree;
	struct commit commit;
	struct tag tag;
};
```

这样「类型未知」的对象可以先按最大尺寸分配，日后确认类型时无需搬家就能原地重解释成任意具体类型。

最后，按类型分派解析的入口是 [object.c:261-309](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.c#L261-L309) 的 `parse_object_buffer`，它用一串 `if (type == OBJ_BLOB) ... else if (type == OBJ_TREE) ...` 调用各类型的 `parse_*_buffer`——这正是「统一基类 + 按类型分派」的总成。

#### 4.2.4 代码实践

**实践目标**：阅读 `commit.h` 里 `struct commit` 的定义，说清楚「commit 如何引用 tree 与 parent」。

**操作步骤**：

1. 打开 [commit.h:27-39](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit.h#L27-L39)，逐字段阅读 `struct commit`。
2. 配合 [commit.h:17-20](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit.h#L17-L20) 的 `struct commit_list`，理解 parent 是如何组织的。
3. 用 `git cat-file -p HEAD` 把一个真实 commit 的原始内容打印出来，把其中的 `tree` 行、`parent` 行和结构体字段一一对应。

**需要观察的现象 / 预期结果**：你应该能用自己的话写出如下对应关系（这也是本实践的参考答案）：

- commit 通过 **`struct tree *maybe_tree`** 字段引用它所代表的目录树快照；它指向一个 tree 对象。注意字段名带 `maybe_` 前缀和注释：当 commit 是从 commit-graph 文件加载时，这个指针可能为 `NULL`，必须通过 `repo_get_commit_tree()` / `get_commit_tree_oid()` 访问，不能直接解引用。
- commit 通过 **`struct commit_list *parents`** 引用父提交；`commit_list` 是一个单链表（`{ struct commit *item; struct commit_list *next; }`），所以 merge commit 可以有多个 parent（链表多个节点），普通提交只有一个，首个提交没有（链表为空）。
- `date` 字段缓存提交时间（用于遍历排序）；`index` 是分配的全局序号（供 commit-slab 用）；第一个成员 `struct object object` 提供身份证 `oid`、类型、标志位。
- 磁盘上 `tree <hash>` 行里的哈希，解析后被填进 `maybe_tree` 指向的 tree 对象的 `oid`；`parent <hash>` 行们则被解析成 `parents` 链表里的各个 commit。

> 「待本地验证」：`maybe_tree` 为 `NULL` 的情形需要 commit-graph 文件参与，本讲不强求复现，知道访问约定即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `lookup_unknown_object` 要按 `sizeof(union any_object)` 分配，而不是按 `sizeof(struct object)`？

**参考答案**：`lookup_unknown_object` 用于「还不知道对象类型」的场景，分配出来的对象之后可能被确认成 blob/tree/commit/tag 中的任意一种并被重解释。如果只按 `sizeof(struct object)` 分配，空间就放不下后续的具体结构体（比如 `struct commit` 比 `struct object` 大很多）。按 `union any_object`（取四种结构体的最大尺寸）分配，就能原地安全地重解释，无需重新分配和搬家。

**练习 2**：`blob` 的 `parse_blob_buffer` 函数体只有一行 `item->object.parsed = 1;`（见 [blob.c:15-18](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/blob.c#L15-L18)），为什么这么简单？

**参考答案**：因为 blob 的内容就是原始字节，没有任何需要解析的结构化字段，`struct blob` 也没有额外成员要填。`parse_blob_buffer` 唯一要做的就是设置 `parsed = 1`，表示「这个对象的内容已确认能成功从库里读出来」。对比 tree/commit/tag，它们的 `parse_*_buffer` 都要拆解文本头、查找并挂接被引用的对象，复杂得多。

### 4.3 对象标志位分配表

#### 4.3.1 概念说明

`struct object.flags` 是一个 **29 位**（`FLAG_BITS = 29`）的字段。这些位不是对象的持久属性，而是**一次操作过程中给对象打的临时记号**——比如「这个 commit 已经在历史遍历里访问过了（`SEEN`）」「这个对象是用户明确不要的（`UNINTERESTING`）」。

关键约束：**所有在内存里的对象共用同一组 29 个位**。这意味着不同子系统（历史遍历、fetch 协商、pack 上传、blame……）都想用这些位来记账。如果两个子系统同时把同一位用于不同含义，就会互相踩踏。

git 的解决办法是：在 `object.h` 顶部维护一份**注释表格**，登记「哪一位归哪个文件/子系统使用」。这只是一个**靠约定维护的注册表**（不是编译器强制的），但它让各子系统知道哪些位是空闲的、哪些已被占用。

#### 4.3.2 核心流程

标志位的典型生命周期：

```text
1. 某子系统开始一次操作（如 git log 的历史遍历）
2. 在涉及的 object 上设置自己的标志位（如 SEEN = 1u<<0）
3. 操作过程中根据标志位做判断（已 SEEN 的 commit 跳过）
4. 操作结束，调用 clear_object_flags() 把这些位清掉，归还给「公共池」
```

要点：

- 标志位是**操作级的临时数据**，操作完成后必须清零复用，所以它们不需要持久化到磁盘。
- 清除有两种粒度：`clear_object_flags` 清所有对象的指定位；`repo_clear_commit_marks` 只清 commit 对象的指定位。
- 因为是「先到先得、用完归还」，位段的归属由那张注释表协调——新增一个使用标志位的子系统时，必须先查表挑空闲位，再去 `revision.h`（或对应文件）里 `#define` 出符号名。

#### 4.3.3 源码精读

标志位总数 [object.h:90](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h#L90)：`#define FLAG_BITS 29`。

那份「分配表」注释在 [object.h:66-89](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h#L66-L89)。它用一种紧凑的「网格」记法——每个字符代表一位（位 0 到位 28），数字表示该位被某个子系统占用：

```c
 * object flag allocation:
 * revision.h:               0---------10         15               23--------28
 * fetch-pack.c:             01    67
 * negotiator/default.c:       2--5
 * ...
 * builtin/show-branch.c:    0-----------------------------------------------28
```

怎么读这张表？以 `revision.h` 那行为例：`0---------10         15               23--------28` 表示 `revision.h` 占用了第 0~10 位、第 15 位、第 23~28 位（`-` 表示该位被这一行占用，空格表示不用）。`fetch-pack.c` 那行 `01    67` 表示它用第 0、1、6、7 位。

这些符号名定义在各文件里。以 `revision.h` 为例，[revision.h:32-40](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.h#L32-L40) 给出了真实的位名：

```c
#define SEEN          (1u<<0)
#define UNINTERESTING (1u<<1)
#define SHOWN         (1u<<3)
#define TMP_MARK      (1u<<4) /* for isolated cases; clean after use */
#define BOUNDARY      (1u<<5)
#define SYMMETRIC_LEFT (1u<<8)
```

清除函数 `clear_object_flags` 在 [object.c:532-541](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.c#L532-L541)，它遍历整个对象哈希表，把指定位从**所有**对象上抹掉；`repo_clear_commit_marks`（[object.c:543-552](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.c#L543-L552)）则只处理 `type == OBJ_COMMIT` 的对象：

```c
void clear_object_flags(struct repository *repo, unsigned flags)
{
	for (i = 0; i < repo->parsed_objects->obj_hash_size; i++) {
		struct object *obj = repo->parsed_objects->obj_hash[i];
		if (obj)
			obj->flags &= ~flags;
	}
}
```

> 注意 `object_as_type`、`create_object`（[object.c:148-163](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.c#L148-L163)）都会把 `flags` 初始化为 0，确保新对象不会带着脏的标志位进入「公共池」。

#### 4.3.4 代码实践

**实践目标**：在源码里追踪一个真实标志位的「定义 → 设置 → 清除」全过程。

**操作步骤**：

1. 在 [revision.h:32-40](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.h#L32-L40) 找到 `SEEN (1u<<0)`。
2. 在仓库里搜索 `SEEN` 被设置和清除的位置（这是源码阅读型实践）：
   ```bash
   grep -n "SEEN" revision.c commit-reach.c
   ```
3. 对照 [object.c:532-541](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.c#L532-L541) 看 `clear_object_flags(repo, SEEN | ...)` 如何在遍历结束后把这些位归还。

**需要观察的现象**：你会看到 `SEEN` 在遍历到一个 commit 时被设上（防止重复访问），整个遍历结束后被一次性清除。这正是标志位「操作级临时数据」的典型用法。

**预期结果**：能用自己的话描述「`SEEN` 这一位在 `revision.h` 里登记为第 0 位，归历史遍历使用；它不是对象的持久属性，操作完成后由 `clear_object_flags` 清零」。

> 若本地没有现成的 grep 环境，可改为在 GitHub 上阅读上述文件并人工追踪，结论一致即可。

#### 4.3.5 小练习与答案

**练习 1**：`FLAG_BITS` 为什么是 29，而不是更大？

**参考答案**：因为 `struct object` 把 `parsed`(1 位)、`type`(3 位)、`flags` 三者放成连续位域，再加上后面的 `struct object_id oid`。在保证 `oid` 对齐和整体布局合理的前提下，`flags` 被定为 29 位，足以容纳当前所有子系统的并发占用（见分配表，最多用到第 28 位）。这是一个「够用且紧凑」的工程取舍。

**练习 2**：如果某个新功能需要给对象打一个跨操作持久化的标记（比如「这个对象已被审计」），能不能直接用 `flags` 里的一位？

**参考答案**：不能。`flags` 是**操作级临时数据**，会在操作结束后被 `clear_object_flags` 清零，且不写入磁盘。要持久化的对象级数据应当存进对象数据库（例如写成一个 commit 的 trailer）、或用 commit-slab / decoration 这类「内存关联结构」、或专门的缓存文件（如 commit-graph）。`flags` 不适合承载任何需要跨操作保留的语义。

## 5. 综合实践

把三个模块串起来，完成下面这个「把磁盘对象映射到内存结构」的小任务。

**任务**：在一个测试仓库里制造出四种对象，逐一观察，然后画出它们的「内存结构假想图」。

**步骤**：

1. 建一个临时仓库并产生四种对象：
   ```bash
   mkdir obj-lab && cd obj-lab && git init
   echo "hello" > greeting.txt
   git add greeting.txt
   git commit -m "first"            # 产生 blob + tree + commit
   git tag -a v1 -m "tag v1"        # 产生 tag
   ```
2. 用 `git cat-file -p HEAD^{tree}` 看顶层 tree，记下 `greeting.txt` 对应的 blob 哈希。
3. 对四种对象各运行一次 `git cat-file -t`，确认类型字符串与 [object.c:29-35](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.c#L29-L35) 的映射一致。
4. 在纸上（或文本里）画出每个对象在内存中的 `struct` 布局，要求：
   - 每个 `struct` 顶部都画一个公共的 `struct object` 块（标出 `parsed / type(3位) / flags(29位) / oid`）。
   - 在 commit 的图上标出 `maybe_tree` 指向 tree、`parents` 链表指向父 commit（本例为空）。
   - 在 tag 的图上标出 `tagged` 指针指向那个 commit。
5. 对照 [object.h:159-164](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h#L159-L164) 与四个具体结构体定义，检查你画的「公共块」位置是否都正确地位于每个结构体的**第一个成员**。

**验收标准**：你能指着图说明——「因为 `struct object` 在第一个成员，所以 `struct commit *` 能被当成 `struct object *` 传给通用代码，`object_as_type` 再负责把它认回 commit 类型」。这就是本讲最核心的一句话。

## 6. 本讲小结

- git 有四种一等对象类型 **blob / tree / commit / tag**，外加表示「未知」的 `OBJ_NONE`、表示出错的 `OBJ_BAD`，以及 pack 专用的两个 delta 类型；类型在 pack 里用 **3 位**编码，故枚举值受限。
- 枚举 `enum object_type`（[object.h:98-110](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h#L98-L110)）与字符串通过 `object_type_strings` / `type_name` / `type_from_string_gently` 互转。
- `struct object` 是**统一基类**，承载 `parsed / type / flags / oid`；四种具体结构体都把它放在第一个成员，实现 C 风格继承。
- 四个 `lookup_*` 函数同构：`lookup_object` 查表 → 未命中则 `create_object(alloc_*_node)` → 命中则 `object_as_type` 强转；commit 在强转时有特例（`init_commit_node` 分配全局 `index`）。
- `object_as_type`（[object.c:165-183](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.c#L165-L183)）负责「类型一致直接用 / `OBJ_NONE` 升级 / 类型不符报错」三条分支。
- `struct object.flags` 是 **29 位**的**操作级临时**资源，由 [object.h:66-89](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object.h#L66-L89) 的注释表协调分配，用完由 `clear_object_flags` 归还，不持久化。

## 7. 下一步学习建议

本讲只讲了「对象在内存里长什么样、怎么分类」，**还没讲对象是怎么从字节流算出哈希、怎么压缩写进 `.git/objects` 的**。下一讲 **u3-l2 对象哈希与松散对象存储** 会补上这一环：阅读 `object-file.c` 的 `write_object_file`、`loose.c` 的 zlib 压缩，以及 `hash.h` 的哈希算法抽象，你会看清「内容 → 哈希 → 磁盘文件」的完整链路，并把本讲的 `oid` 字段和磁盘上的真实文件对应起来。

之后 **u3-l3 pack 文件格式与打包存储** 会讲 delta 类型（`OBJ_OFS_DELTA` / `OBJ_REF_DELTA`）如何节省空间——正好解释本讲练习 2 留下的疑问。
