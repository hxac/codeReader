# refs 抽象与 ref_store API

## 1. 本讲目标

本讲是「引用 refs」单元的第一讲。学完后你应当能够：

- 说清「引用（ref）」在 git 数据模型里扮演的角色：它是把人类可读的名字（如 `refs/heads/main`、`HEAD`）映射到对象哈希（oid）的命名指针。
- 看懂 `struct ref_store` 这一抽象接口：它如何用一张函数指针表（虚表）把「读写引用」与底层存储后端（files / reftable）解耦。
- 读懂 `refs_resolve_ref_unsafe`：理解符号引用（symref）是如何被递归展开、最终解析到一个 oid 的。
- 读懂 `refs_for_each_ref`：理解 git 用「迭代器 + 回调」两种风格遍历全部引用，并知道结果为何按名字排序。
- 把本讲与上一单元 `struct repository`（u2-l2）里的 `refs_private`、`ref_storage_format` 字段对接起来。

本讲只讲**后端无关的公共 API 与抽象**，具体到 files 松散文件 / packed-refs / reftable 二进制格式的落地实现，留待 u5-l2、u5-l3。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**引用是什么。** 在 u3 单元我们学到：git 的一切对象都按内容哈希寻址，blob/tree/commit/tag 各有一个 40 位（SHA-1）或 64 位（SHA-256）的 oid。但人记不住哈希。引用（reference / ref）就是给某个 oid 起一个稳定的名字：`refs/heads/main` 指向当前 main 分支的最新 commit，`refs/tags/v1.0` 指向某个 tag，`HEAD` 指向「当前所在分支」。于是「移动分支」本质就是改一个引用指向的新 oid。

**两种引用。** 一种是**直接引用（direct ref）**：引用本身就存一个 oid，例如 `refs/heads/main` 的内容就是某个 commit 的哈希。另一种是**符号引用（symbolic ref，symref）**：引用存的是「另一个引用的名字」，例如 `HEAD` 的文件内容是字面量 `ref: refs/heads/main`，表示「HEAD 指向 main」。解析 `HEAD` 就要顺着这条指针再走一步，才能拿到真正的 oid。git 允许 symref 嵌套，但层数有限。

**为什么需要 `ref_store` 抽象。** 引用可以存成松散文件（files 后端）、打包进 `packed-refs`（files 后端的一部分）、或存进 reftable 二进制文件（reftable 后端）。但上层命令（`git rev-parse`、`git log`、`git for-each-ref`……）不应该关心这些差别。于是 git 定义了一个抽象接口 `struct ref_store`：上层只调 `refs_resolve_ref_unsafe(...)`、`refs_for_each_ref(...)`，由 `ref_store` 内部把调用分发给具体后端。这正是面向对象里「接口 + 多个实现 + 虚表分派」的 C 语言写法。

> 术语速查：oid（对象哈希）、symref（符号引用）、direct ref（直接引用）、pseudo-ref（伪引用，如 `FETCH_HEAD`/`MERGE_HEAD`，语义特殊、不走后端）、ref_store（引用存储抽象）、backend（后端）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [refs.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.h) | 引用模块的**公共头文件**。定义上层可调用的全部 API：`refs_resolve_ref_unsafe`、`refs_for_each_ref`、`struct reference`、各类 flag 与回调签名。代码 outside refs 模块只该 include 此文件。 |
| [refs.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c) | 引用模块**后端无关的实现主体**。包含 `refs_resolve_ref_unsafe`、`refs_for_each_ref`、`get_main_ref_store`、后端注册表 `refs_backends[]` 等。 |
| [refs/refs-internal.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/refs-internal.h) | 引用模块**内部头文件**。定义 `struct ref_store`、虚表 `struct ref_storage_be`、`struct ref_iterator`、内部 flag（`REF_HAVE_OLD` 等）。只有 refs 模块自身及各后端实现 include 它。 |
| [refs/iterator.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/iterator.c) | 引用迭代器的通用适配层。`do_for_each_ref_iterator` 把「迭代器风格」适配成「回调风格」。 |
| [reflog.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/reflog.c) | reflog（引用日志）的**过期策略**实现：判定哪些 reflog 条目因不可达而可裁剪、`reflog_delete` 等。reflog 的读写本身也经 `ref_store` 虚表分派（见 4.1）。 |

## 4. 核心概念与源码讲解

### 4.1 ref_store 抽象接口

#### 4.1.1 概念说明

`struct ref_store` 是 git 对「一个仓库的引用存储」的运行时抽象。它回答两个问题：

1. **这些引用存在哪里？** —— 由 `gitdir` 字段记录（多工作树下未必等于 `repo->gitdir`）。
2. **怎么读写？** —— 由 `be` 字段指向一张函数指针表（虚表），每个操作（读、写、遍历、删、fsck……）都对应表里一个函数指针，具体后端填入自己的实现。

这种设计的好处是：上层代码只面对 `struct ref_store *` 这一个类型，调用统一的 `refs_*` 函数；换后端（files ↔ reftable）只需换 `be` 指向的虚表，业务代码一行不改。这正是 u2-l2 提到的 `enum ref_storage_format`（`FILES`/`REFTABLE`）落地的机制。

承接 u2-l2：`struct repository` 的 `refs_private` 字段就是「主仓库的 `ref_store` 指针」，懒加载——直到第一次有人需要引用时才创建。

#### 4.1.2 核心流程

一个 `ref_store` 从「被选中」到「可用」的流程：

```text
仓库对象 repository
   │  ref_storage_format = FILES 或 REFTABLE（u2-l2）
   │  refs_private = NULL（初始未加载）
   ▼
首次访问引用（如 git rev-parse HEAD）
   │  调 get_main_ref_store(r)
   ▼
ref_store_init(r, r->ref_storage_format, r->gitdir, ALL_CAPS)
   │  1) find_ref_storage_backend(format)  ──查 refs_backends[] 表──▶ be
   │  2) be->init(repo, payload, gitdir, opts)  ──后端构造自己的 ref_store
   │     └─ 内部调 base_ref_store_init() 填 be/repo/gitdir 三个公共字段
   ▼
r->refs_private = 新 ref_store  （后续直接复用，不再重建）
```

虚表分派的统一模式：公共 API 函数体几乎只有一行 `return refs->be->某操作(refs, ...)`，把调用转给后端。

#### 4.1.3 源码精读

**`struct ref_store` 基类**——只有三个公共字段，极其精简：

[refs/refs-internal.h:612-623](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/refs-internal.h#L612-L623) 定义了 `ref_store`：`be`（后端虚表指针）、`repo`（所属仓库）、`gitdir`（引用存储所在目录）。所有具体后端（如 files 的 `files_ref_store`、reftable 的 `migrated_ref_store`）都把这个结构体放在首成员，实现 C 风格继承。

**虚表 `struct ref_storage_be`**——一张函数指针表，是整个抽象的契约：

[refs/refs-internal.h:567-601](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/refs-internal.h#L567-L601) 列出后端要实现的全部操作：`init`/`release`/`create_on_disk`/`remove_on_disk`、事务三件套 `transaction_prepare`/`transaction_finish`/`transaction_abort`、`optimize`、`rename_ref`/`copy_ref`、`iterator_begin`（遍历）、`read_raw_ref`（单条读取）、`read_symbolic_ref`（读 symref 目标），以及一组 reflog 操作（`reflog_iterator_begin`/`for_each_reflog_ent`/`reflog_exists`/`create_reflog`/`delete_reflog`/`reflog_expire`）和 `fsck`。注意 reflog 的读写也在这张表里——这说明 reflog 同样是「经 ref_store 抽象、由后端落地」的资源，reflog.c 只负责过期策略，不直接碰磁盘格式。

**后端注册表**——按格式枚举索引的数组：

[refs.c:38-41](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L38-L41) 的 `refs_backends[]` 把 `REF_STORAGE_FORMAT_FILES` 槽位填 `&refs_be_files`、`REF_STORAGE_FORMAT_REFTABLE` 槽位填 `&refs_be_reftable`。注意数组里只有这两个后端——尽管 refs-internal.h 还 `extern` 声明了 `refs_be_packed`，但它**不在注册表里**：packed 只是 files 后端内部用来读 `packed-refs` 的辅助实现，不是可独立选择的存储格式。[refs.c:43-49](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L43-L49) 的 `find_ref_storage_backend` 即按枚举值到这个数组里查指针。

**构造与公共字段填充**：

[refs.c:2330-2352](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2330-L2352) 的 `ref_store_init` 先 `find_ref_storage_backend` 拿到虚表，再调 `be->init` 让后端构造实例；[refs.c:2481-2487](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2481-L2487) 的 `base_ref_store_init` 是后端构造函数里必调的「基类构造」，负责填 `be`/`repo`/`gitdir` 三字段。

**懒加载入口**——连接 `struct repository` 与 `ref_store`：

[refs.c:2360-2379](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2360-L2379) 的 `get_main_ref_store`：若 `r->refs_private` 已存在直接返回；否则用 `REF_STORE_ALL_CAPS`（读/写/odb/主库全权限，见 [refs/refs-internal.h:393-400](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/refs-internal.h#L393-L400)）新建并缓存。注意那个 `static bool initializing` 的递归守卫——若初始化过程中又回调到自己就 `BUG()`，防止无限递归。

**虚表分派的典型写法**——一行转交：

[refs.c:2095-2106](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2095-L2106) 的 `refs_read_raw_ref`：先对伪引用（`FETCH_HEAD`/`MERGE_HEAD`，见 [refs.c:888-901](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L888-L901) 的 `is_pseudo_ref`）走文件系统特例 `refs_read_special_head`，否则 `return ref_store->be->read_raw_ref(...)` 把活儿交给后端。这就是「公共 API → 虚表 → 后端」的标准链路。

> **承接 u2-l2 的一句话**：git 正在推进「显式传 `struct repository *`」的迁移。本仓库里已经**不再有**无前缀的 `resolve_ref_unsafe()` / `for_each_ref()` 全局包装，调用方现在显式写 `refs_for_each_ref(get_main_ref_store(the_repository), ...)`（实例见 [builtin/rev-parse.c:926](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/rev-parse.c#L926)）。`ref_store` 抽象正是这一迁移能成立的前提。

#### 4.1.4 代码实践

**实践目标**：在磁盘上对比两种后端的布局，并在源码里确认后端是如何被选中的。

**操作步骤**：

1. 建一个默认（files）仓库并观察引用目录：
   ```sh
   git init files-repo && cd files-repo
   git commit --allow-empty -m first
   ls -la .git/refs/heads          # 看到 main 文件（松散引用）
   cat .git/HEAD                   # 内容是 "ref: refs/heads/main"
   cat .git/refs/heads/main        # 内容是某个 commit 的 oid
   ```
2. 另建一个 reftable 仓库对比：
   ```sh
   cd ..
   git init --ref-format=reftable reftable-repo && cd reftable-repo
   git commit --allow-empty -m first
   ls -la .git/refs/               # 没有 heads/ 目录，而是 reftable 文件
   ls .git/reftable 2>/dev/null || find .git -name '*.ref'   # reftable 二进制文件
   ```
3. 阅读源码链路：从 `get_main_ref_store`（[refs.c:2360](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2360)）→ `ref_store_init`（[refs.c:2330](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2330)）→ `find_ref_storage_backend`（[refs.c:43](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L43)）→ `refs_backends[]`（[refs.c:38](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L38)），确认 `r->ref_storage_format` 决定了选哪个 `be`。

**需要观察的现象**：files 仓库的引用是「一个引用一个松散文件」；reftable 仓库没有 `refs/heads/main` 这种文件，所有引用打包进二进制 reftable。**上层 `git rev-parse HEAD` / `git log` 在两种仓库里行为完全一致**——这正是 `ref_store` 抽象的意义。

**预期结果**：两条命令在两个仓库里都正确输出第一个 commit 的 oid 与日志。

#### 4.1.5 小练习与答案

**练习 1**：`struct ref_store` 只有 `be`/`repo`/`gitdir` 三个字段，那「当前所有引用的值」存在哪里？
**答案**：不在 `ref_store` 里。`ref_store` 只持有「去哪里读、怎么读」的能力（虚表 + 路径），引用的值每次都由后端按需从磁盘或自己的缓存里读出。这是一种「薄抽象 + 按需读」的设计，避免在内存里维护一份可能与磁盘不一致的副本。

**练习 2**：`refs_be_packed` 在 refs-internal.h 里被 `extern` 声明，却不在 `refs_backends[]` 数组里，为什么？
**答案**：因为 packed 不是供用户独立选择的存储格式，而是 files 后端内部用来读取 `packed-refs` 文件的辅助实现。用户能选的格式只有 files 与 reftable 两种（对应 `enum ref_storage_format` 的两个值），所以注册表只列这两个。

---

### 4.2 ref 解析 refs_resolve_ref_unsafe

#### 4.2.1 概念说明

「解析一个引用」=给出一个名字（如 `HEAD` 或 `refs/heads/main`），拿到它最终指向的 oid。难点在于 symref：`HEAD` 不是直接指向 oid，而是指向 `refs/heads/main`，后者才指向 oid。所以解析必须**递归跟随 symref 链**，直到遇到直接引用或超出深度限制。

`refs_resolve_ref_unsafe` 是整个引用模块最核心、被调用最频繁的函数之一——`git rev-parse`、`git log`、`git status`…… 几乎所有命令都要先解析 `HEAD` 才能干活。名字里的 `_unsafe` 不是「有安全漏洞」，而是指**返回的字符串指针指向静态缓冲区或输入参数**，下一次调用可能覆盖它，调用方若要跨调用保留必须自己 `xstrdup`（这正是 `refs_resolve_refdup` 的用途）。

#### 4.2.2 核心流程

`refs_resolve_ref_unsafe` 的主循环（最多 `SYMREF_MAXDEPTH = 5` 轮）：

```text
输入: refname, resolve_flags, &oid, &flags
─────────────────────────────────────────
若 refname 格式非法:
   若不允许坏名 → 返回 NULL
   否则置 REF_BAD_NAME，继续（后续可能补 REF_ISBROKEN）

for symref_count in 0..SYMREF_MAXDEPTH-1:
    refs_read_raw_ref(refs, refname, &oid, &sb_refname, &read_flags, &errno)
       └─ pseudo-ref?  → refs_read_special_head（走文件系统）
          否则         → be->read_raw_ref（走后端）

    若读取失败:
       若 RESOLVE_REF_READING 模式 → 返回 NULL（必须解析到对象）
       若 errno 不是 ENOENT/EISDIR/ENOTDIR → 返回 NULL（真错误）
       否则: oid 清零，返回 refname（未定义引用，作为「将写入的目标」）

    若不是 symref（read_flags 无 REF_ISSYMREF）:
       返回 refname（直接引用，oid 已填好）

    是 symref: refname = sb_refname.buf（换成目标名）
       若 RESOLVE_REF_NO_RECURSE → oid 清零，返回新 refname（只解一层）
       若新 refname 格式非法 → 视坏名规则处理
       ──继续下一轮循环──

循环跑满仍末解完 → 返回 NULL（symref 链过深）
```

三个 `resolve_flags`（[refs.h:87-89](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.h#L87-L89)）控制行为：

- `RESOLVE_REF_READING`（0x01）：读取模式，要求最终解析到对象；解析不到就返回 NULL。最常见的模式。
- `RESOLVE_REF_NO_RECURSE`（0x02）：只解一层。对 symref，返回它**直接指向的引用名**、oid 置空。这正是 `git symbolic-ref HEAD` 的语义。
- `RESOLVE_REF_ALLOW_BAD_NAME`（0x04）：允许名字不合规（参见 `git-check-ref-format`），但仍会标记 `REF_BAD_NAME`/`REF_ISBROKEN`。

#### 4.2.3 源码精读

**函数签名与完整文档**——先读注释再读码：

[refs.h:39-95](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.h#L39-L95) 详细说明了返回值语义：返回「最终那个非符号引用的名字」（指针进静态缓冲区或输入）；`oid` 非空则存解析到的对象；`flags` 反映 `REF_ISPACKED`/`REF_ISSYMREF`/`REF_BAD_NAME`/`REF_ISBROKEN`。声明在 [refs.h:91-95](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.h#L91-L95)。

**主循环实现**：

[refs.c:2114-2201](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2114-L2201) 是函数本体。关键几段：

- [refs.c:2132-2146](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2132-L2146)：入口先 `check_refname_format` 校验名字；非法且不允许坏名则直接 NULL，否则只标 `REF_BAD_NAME` 暂不判 broken（因为还不知道引用是否存在）。
- [refs.c:2148-2198](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2148-L2198)：`SYMREF_MAXDEPTH`（[refs/refs-internal.h:260](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/refs-internal.h#L260)，值为 5）次循环。每轮调 `refs_read_raw_ref` 读一层：
  - 读取失败时（[refs.c:2156-2173](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2156-L2173)）：`READING` 模式必返回 NULL；否则只有 `ENOENT`（不存在）/`EISDIR`（名字是目录）/`ENOTDIR`（前缀非目录）这几种「可接受的缺失」才把 oid 清零、返回当前 refname（供后续写入使用），其它 errno 视为真错误返回 NULL。
  - 读取成功且非 symref（[refs.c:2178-2184](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2178-L2184)）：直接返回 refname，oid 已由 `read_raw_ref` 填好。
  - 是 symref（[refs.c:2186-2197](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2186-L2197)）：把 `refname` 换成 `sb_refname.buf`（即 symref 的目标），`NO_RECURSE` 则立即返回这一层，否则校验新名字后进入下一轮。
- 循环跑满仍未解完 → [refs.c:2200](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2200) 返回 NULL（symref 链过深，视作坏引用）。

注意那个 `static struct strbuf sb_refname`（[refs.c:2120](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2120)）——这就是「`_unsafe`」的由来：返回的 symref 目标名可能指向这块静态缓冲，下次调用会被覆盖。

**三个便捷包装**——都建在 `refs_resolve_ref_unsafe` 之上：

[refs.c:436-473](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L436-L473) 集中了一组薄包装：
- `refs_resolve_refdup`（436）：调完再 `xstrdup_or_null`，返回可安全持有的副本。
- `refs_read_ref_full`（455）：把「返回非 NULL」转成「返回 0 表示成功」，更符合 C 习惯的 0/负数返回。
- `refs_read_ref`（464）：固定 `RESOLVE_REF_READING`、不关心 flags 的极简版。
- `refs_ref_exists`（469）：`!!refs_resolve_ref_unsafe(...)`，引用存在即真。

#### 4.2.4 代码实践

**实践目标**：用 `git symbolic-ref` 与 `git rev-parse` 对比，直观体会「只解一层」与「递归解析到 oid」的差别，再回源码印证。

**操作步骤**：

1. 在任意 git 仓库里：
   ```sh
   git symbolic-ref HEAD              # 输出 HEAD 直接指向的引用名
   git rev-parse HEAD                 # 输出 HEAD 最终解析到的 oid
   git rev-parse --symbolic-full-name HEAD   # 输出与 symbolic-ref 一致
   ```
2. 构造一条两层的 symref 链（仅用于观察，正常不会这么用）：
   ```sh
   git symbolic-ref refs/heads/test refs/heads/main   # test → main（symref）
   git symbolic-ref HEAD refs/heads/test              # HEAD → test（symref）
   git symbolic-ref HEAD          # 只解一层：refs/heads/test
   git rev-parse HEAD             # 递归到底：main 的 commit oid
   ```
3. 对照源码：`git symbolic-ref HEAD` 走的等价于 `RESOLVE_REF_NO_RECURSE` 语义——[refs.c:2187-2190](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2187-L2190) 在遇到 symref 时立即返回目标名、oid 清零；而 `git rev-parse HEAD` 走 `RESOLVE_REF_READING`，循环会继续把 `refs/heads/test` 再解一层到 `refs/heads/main`、最终拿到 oid。
4. 实验完还原：`git symbolic-ref HEAD refs/heads/main && git update-ref -d refs/heads/test`。

**需要观察的现象**：`symbolic-ref` 永远只给你「下一跳」的名字；`rev-parse` 给你链尾的 oid。当 symref 链有两层时，二者输出明显不同。

**预期结果**：第 2 步里 `symbolic-ref HEAD` 输出 `refs/heads/test`，`rev-parse HEAD` 输出 main 分支那个 commit 的 40 位哈希。若误把 `symbolic-ref` 当成「拿到 oid」，就会在这里踩坑。

> 说明：第 2 步构造两层 symref 仅用于演示解析深度差异，真实仓库里分支几乎都是直接引用，`HEAD` 是唯一的常规 symref。

#### 4.2.5 小练习与答案

**练习 1**：为什么函数叫 `refs_resolve_ref_unsafe`？怎样用才安全？
**答案**：因为返回的字符串指针可能指向函数内的 `static` 缓冲区或输入参数，下一次调用会覆盖它。安全用法是立即使用、或用 `refs_resolve_refdup` 拿一份 `xstrdup` 副本长期持有。不要把返回指针存进结构体跨调用使用。

**练习 2**：`SYMREF_MAXDEPTH` 为什么是 5 这样的小值？
**答案**：symref 嵌套在实际仓库里极少超过 1 层（基本只有 `HEAD` 一层），5 层已远超正常需求；设小上界既能防止「symref 环」（A→B→A）导致无限递归，也能在引用被人为构造得很深时及时判 `NULL`（视为坏引用）而非耗尽栈。

---

### 4.3 ref 遍历 refs_for_each_ref

#### 4.3.1 概念说明

「遍历引用」=枚举仓库里的全部（或某一前缀下的）引用，对每条拿到 `{名字, oid, flags}`。`git for-each-ref`、`git branch -a`、`git tag`、`git log` 的引用枚举都依赖它。

git 给遍历设计了**两套对外风格**，理解这点能省去很多困惑：

1. **迭代器风格**（`struct ref_iterator`）：面向需要精细控制的调用方。`ref_iterator_advance()` 每次推进一条，返回 `ITER_OK`/`ITER_DONE`/`ITER_ERROR`。
2. **回调风格**（`refs_for_each_ref` + `refs_for_each_cb`）：面向「逐条处理」的简单场景。传入一个回调，遍历器对每条引用调一次回调，回调返回非 0 即停止。

二者由适配函数 `do_for_each_ref_iterator` 桥接：回调风格内部其实是「创建迭代器 → 循环 advance → 喂给回调」。

#### 4.3.2 核心流程

从 `refs_for_each_ref` 到「每条引用」的调用链：

```text
refs_for_each_ref(refs, cb, data)                 # 最简入口：遍历全部
   └─ refs_for_each_ref_ext(refs, cb, data, &零选项)
         │  处理 opts: prefix / pattern(glob) / namespace / exclude / trim
         │  pattern 无通配符则补 "/"+"*"；有通配符则用 for_each_filter_refs 包一层回调
         ▼
      refs_ref_iterator_begin(refs, prefix, exclude, trim, flags)
         │  规范化 exclude（保证以 "/" 结尾）
         │  应用 ref_paranoia（GIT_REF_PARANOIA 默认开 → 含 broken、省 dangling symref）
         │  iter = refs->be->iterator_begin(refs, prefix, exclude, flags)   # 后端造迭代器
         │  若 trim: iter = prefix_ref_iterator_begin(iter, "", trim)       # 再包一层裁剪
         ▼
      do_for_each_ref_iterator(iter, cb, data)    # 迭代器→回调 适配
         └─ while (advance == ITER_OK) cb(&iter->ref, data); cb 非0 即停
```

几个要点：

- **结果按 refname 排序**：后端的 `iterator_begin` 保证输出按引用名字典序，这样 `git for-each-ref` 的输出才是确定、可 diff 的。
- **`ref_paranoia`**：默认开启（`GIT_REF_PARANOIA=1`），遍历会包含坏引用（指向缺失对象、损坏文件等），但**省略 dangling symref**（指向不存在引用的 symref）。这是为了 `git gc` 等操作能发现损坏，又不被无害的悬空 symref 噪声干扰。
- **`struct reference`** 是喂给回调的统一记录类型，包含 `name`/`target`（symref 目标）/`oid`/`peeled_oid`（tag 剥离值）/`flags`。

#### 4.3.3 源码精读

**回调签名与记录类型**：

[refs.h:413](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.h#L413) 定义回调 `typedef int refs_for_each_cb(const struct reference *ref, void *cb_data)`；[refs.h:370-393](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.h#L370-L393) 定义 `struct reference`，注释强调回调收到的内存**只在单次回调内有效**，跨回调要保留必须拷贝。`flags` 取值见 [refs.h:346-367](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.h#L346-L367) 的 `enum reference_status`（`REF_ISSYMREF`/`REF_ISPACKED`/`REF_ISBROKEN`/`REF_BAD_NAME`）。

**最简入口**：

[refs.c:1968-1972](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L1968-L1972) 的 `refs_for_each_ref` 只有三行：构造一个全零的 `refs_for_each_ref_options`（无 prefix 即遍历全部），转给 `refs_for_each_ref_ext`。声明在 [refs.h:509-510](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.h#L509-L510)。

**带选项的富版本**：

[refs.c:1884-1966](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L1884-L1966) 的 `refs_for_each_ref_ext` 处理选项：
- `pattern`（glob）：无通配符时自动补 `/`+`*`（[refs.c:1919-1924](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L1919-L1924)），匹配用 `wildmatch`，通过 `for_each_filter_refs`（[refs.c:475-490](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L475-L490)）把原回调包一层。
- `namespace`：把 prefix 与 exclude 都改写到命名空间下。
- 最后 `refs_ref_iterator_begin` 建迭代器，`do_for_each_ref_iterator` 跑回调。

**迭代器创建**：

[refs.c:1833-1882](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L1833-L1882) 的 `refs_ref_iterator_begin`：
- 规范化 exclude 模式（[refs.c:1843-1858](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L1843-L1858)）：保证每条以 `/` 结尾，避免 `foo` 误匹到 `foobar`。
- 应用 `ref_paranoia`（[refs.c:1860-1869](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L1860-L1869)）：默认 `GIT_REF_PARANOIA=1`，于是加上 `INCLUDE_BROKEN` 与 `OMIT_DANGLING_SYMREFS`。
- `iter = refs->be->iterator_begin(...)`（[refs.c:1871](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L1871)）——又一行虚表分派，后端负责产出按名字排序的迭代器。
- 若要 `trim`，再套一个 `prefix_ref_iterator_begin`（[refs.c:1876-1877](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L1876-L1877)）裁掉前缀字符。

**迭代器→回调适配**：

[refs/iterator.c:425-441](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/iterator.c#L425-L441) 的 `do_for_each_ref_iterator`：`while ((ok = ref_iterator_advance(iter)) == ITER_OK)` 每推进一条就调 `fn(&iter->ref, cb_data)`，回调返回非 0 即跳出；`ITER_ERROR` 则整体返回 -1；无论如何最后 `ref_iterator_free(iter)`。这正是两套风格的桥接点。迭代器自身的协议（advance 返回 `ITER_OK/DONE/ERROR`）见 [refs.h:1342-1383](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.h#L1342-L1383)。

**一个不遍历的特例**：

[refs.c:1814-1831](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L1814-L1831) 的 `refs_head_ref`：它不遍历全部引用，而是只解析 `HEAD` 一次（用 `refs_resolve_ref_unsafe`，4.2 的同款），构造一条 `struct reference` 喂给回调。这是「把 HEAD 当作单条引用纳入遍历结果」的常见入口，把 4.2 的解析与 4.3 的回调两种机制缝合在一起。

#### 4.3.4 代码实践

**实践目标**：用 `git for-each-ref` 观察遍历输出，配合 `GIT_REF_PARANOIA` 理解「含 broken、省 dangling symref」，再回源码印证排序与过滤。

**操作步骤**：

1. 观察默认输出（按名字排序）：
   ```sh
   git for-each-ref --format='%(refname) %(objectname:short) %(symref)' | head
   # 注意 refs/heads/*、refs/remotes/*、refs/tags/* 是字典序排列的
   ```
2. 用前缀与 glob 过滤，对照源码里的 `prefix`/`pattern`：
   ```sh
   git for-each-ref refs/heads/                  # prefix，等价 opts.prefix
   git for-each-ref 'refs/tags/v*'               # glob pattern，无通配符会补 /*
   ```
3. 制造一个 dangling symref，观察 paranoia 行为：
   ```sh
   git update-ref refs/heads/dangling refs/heads/no-such-branch --no-deref 2>/dev/null \
     || git symbolic-ref refs/heads/dangling refs/heads/no-such-branch
   GIT_REF_PARANOIA=0 git for-each-ref --format='%(refname)' | grep dangling || echo "默认省略 dangling"
   GIT_REF_PARANOIA=1 git for-each-ref --format='%(refname)' | grep dangling || echo "paranoia 也省略 dangling symref"
   ```
4. 对照 [refs.c:1860-1869](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L1860-L1869)：默认 `ref_paranoia=1` 会同时置 `INCLUDE_BROKEN` 与 `OMIT_DANGLING_SYMREFS`，所以即便开了 paranoia，dangling symref 仍被省略——这与「坏引用（指向缺失对象）会被包含」是两回事。
5. 清理：`git update-ref -d refs/heads/dangling`（或对应删除方式）。

**需要观察的现象**：输出严格按 refname 字典序；prefix 与 glob 都能正确缩范围；dangling symref 在默认与 paranoia 下都不出现。

**预期结果**：第 1 步看到排序良好的引用列表；第 3 步两次 grep 都走 `echo` 分支（dangling symref 被省略）。**若行为与预期不符，标注「待本地验证」并记录实际输出**——不同 git 版本对 dangling symref 的处理细节可能略有差异。

#### 4.3.5 小练习与答案

**练习 1**：`refs_for_each_ref` 与直接用 `struct ref_iterator` 有何区别？何时该用哪个？
**答案**：`refs_for_each_ref` 是回调风格，内部已把「建迭代器→循环 advance→喂回调→释放」封装好，适合「逐条处理、可中途返回非 0 停止」的简单场景。`struct ref_iterator` 是迭代器风格，调用方自己控制 advance 时机，适合需要在两条迭代之间穿插其它逻辑、或按需 seek（`ref_iterator_seek`）的复杂场景。两者由 `do_for_each_ref_iterator` 桥接。

**练习 2**：为什么默认要开 `ref_paranoia`（含 broken 引用）？
**答案**：`git gc`、`git fsck` 等维护操作需要「看到」损坏的引用（指向缺失对象、文件损坏）才能报告与修复；若遍历默认隐藏 broken，这些损坏就会被悄无声息地忽略。同时为避免噪声，dangling symref（指向不存在引用，属正常现象而非损坏）被单独省略。这是一个「宁可多报损坏、不误报悬空」的谨慎默认。

---

## 5. 综合实践

**任务**：跟踪一次 `git rev-parse HEAD` 从「字符串 `HEAD`」到「输出一个 oid」的完整解析路径，把本讲三个最小模块串起来。

**操作步骤**：

1. 在任意仓库执行，记下输出：
   ```sh
   git rev-parse HEAD
   git rev-parse --symbolic-full-name HEAD     # 应为 refs/heads/<分支>
   ```
2. 用一张图画出调用链，标注每步落在哪个函数、哪条虚表分派：
   ```text
   git rev-parse HEAD
     └─ (builtin/rev-parse.c) 需要解析 HEAD
        └─ get_main_ref_store(the_repository)          [4.1, refs.c:2360]
              └─ ref_store_init → be = refs_be_files   [4.1, refs.c:2330/38]
        └─ refs_resolve_ref_unsafe(refs, "HEAD", READING, &oid, &flags)   [4.2, refs.c:2114]
              ├─ 第1轮: refs_read_raw_ref("HEAD") → be->read_raw_ref     [4.1, refs.c:2095]
              │           读到 "ref: refs/heads/main" → REF_ISSYMREF
              ├─ refname 改为 refs/heads/main
              └─ 第2轮: refs_read_raw_ref("refs/heads/main") → 拿到 oid，非 symref → 返回
   ```
3. 验证 `HEAD` 是 symref：`cat .git/HEAD` 应为 `ref: refs/heads/<分支>`；对照源码确认第 1 轮读到 `REF_ISSYMREF` 后会进入 [refs.c:2186-2190](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2186-L2190) 换名续解。
4. 把仓库切到 detached HEAD（`git checkout --detach`）再重复：此时 `.git/HEAD` 直接存 oid 而非 `ref: ...`。预期 `rev-parse --symbolic-full-name HEAD` 失败（HEAD 不再是 symref），而 `rev-parse HEAD` 仍输出 oid——因为解析第 1 轮就拿到非 symref 的 oid 直接返回（[refs.c:2178-2184](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2178-L2184)）。实验后 `git checkout <原分支>` 还原。
5. （可选）用 `GIT_TRACE_REFS=1 git rev-parse HEAD` 观察引用后端的追踪输出，对照你画的调用链核对每一步是否真的发生。

**预期结果**：你能清楚指出——`ref_store` 抽象（4.1）提供了 `be->read_raw_ref` 这一虚表槽；`refs_resolve_ref_unsafe`（4.2）用它驱动 symref 递归；而遍历（4.3）虽在本任务未直接用到，但用的是同一套 `be->iterator_begin` 虚表槽与同样的 `struct reference` 记录类型。三个模块共用一张虚表、一套 flag，这就是「ref_store 抽象」的统一性。

## 6. 本讲小结

- **引用是命名指针**：把 `refs/heads/main`、`HEAD` 这类名字映射到对象 oid；分直接引用（存 oid）与符号引用（存 `ref: <另一个引用>`）两种，另有伪引用 `FETCH_HEAD`/`MERGE_HEAD` 走文件系统特例。
- **`struct ref_store` 是抽象接口**：基类只含 `be`/`repo`/`gitdir` 三字段；`be` 指向虚表 `struct ref_storage_be`，公共 API 几乎只做 `return refs->be->操作(...)` 的分派，从而把上层与 files/reftable 后端解耦。后端按 `enum ref_storage_format` 注册在 `refs_backends[]`，由 `get_main_ref_store` 懒加载进 `repository.refs_private`（承接 u2-l2）。
- **`refs_resolve_ref_unsafe` 递归解析 symref**：最多 `SYMREF_MAXDEPTH=5` 轮，每轮 `refs_read_raw_ref` 读一层；遇 symref 换名续解，遇直接引用返回 oid。`READING`/`NO_RECURSE`/`ALLOW_BAD_NAME` 三个 flag 控制「必须解析到对象」「只解一层」「允许坏名」三种语义。`_unsafe` 指返回串可能指向静态缓冲，需 `refs_resolve_refdup` 复制。
- **遍历有迭代器与回调两套风格**：`refs_for_each_ref` → `refs_for_each_ref_ext`（处理 prefix/glob/namespace/exclude/trim）→ `refs_ref_iterator_begin`（规范化、应用 ref_paranoia、虚表分派 `be->iterator_begin`）→ `do_for_each_ref_iterator`（advance 循环喂回调）。结果按 refname 排序，默认含 broken、省 dangling symref。
- **迁移趋势**：本仓库已无无前缀的 `resolve_ref_unsafe()`/`for_each_ref()` 全局包装，调用方显式传 `ref_store`/`repository`（如 `refs_for_each_ref(get_main_ref_store(the_repository), ...)`），`ref_store` 抽象正是这一迁移的根基。

## 7. 下一步学习建议

本讲只讲了「后端无关的公共 API」。要真正理解引用在磁盘上长什么样，建议继续：

- **u5-l2 refs 后端：files、packed 与 reftable**：进入 `refs/files-backend.c`、`refs/packed-backend.c`、`refs/reftable-backend.c`，看 `be->read_raw_ref`、`be->iterator_begin` 在每种后端里到底怎么落地——本讲里那句 `refs->be->read_raw_ref(...)` 到底读了哪个文件、解析了什么格式，答案在那里。
- **u5-l3 ref 事务与 ref-cache**：本讲的 `refs_resolve_ref_unsafe` 是「读」，而 `git update-ref`、`git commit` 改引用走的是**事务**（`ref_transaction_begin/update/commit`，已在 refs.h 声明、虚表的 `transaction_prepare/finish/abort`）。事务如何保证原子性与回滚、ref-lock 如何排他、`refs/ref-cache.c` 如何缓存，是下一讲的焦点。
- 读完 u5-l2、u5-l3 后，可以回头重做本讲综合实践，尝试在源码里把 `be->read_raw_ref` 这一行「展开」成 files 后端的具体读文件逻辑，验证你对整个调用链的理解。
