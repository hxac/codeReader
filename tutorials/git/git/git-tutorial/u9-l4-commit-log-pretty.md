# commit 创建与 log/pretty 展示

## 1. 本讲目标

本讲把前几讲建立的「索引」和「遍历引擎」两块拼图接起来，回答两个看似平常、实则涉及半个 git 源码的问题：

1. **`git commit` 是怎么从索引造出一个 commit 对象的？** 一次提交到底在磁盘上留下了什么字节？
2. **`git log` 是怎么遍历历史、又怎么把每个 commit 格式化成屏幕上那几行字的？** `--pretty=format:'%h %s'` 里的 `%h`、`%s` 到底在源码的哪一行被替换？

学完后你应该能：

- 说清从「索引 → tree → commit」的完整创建链路，并能逐字段解释 `git cat-file -p` 看到的 commit 内容；
- 掌握 pretty 格式系统的两层设计：**结构化格式（raw/medium/short/full/...）** 与 **占位符格式（`%H %s ...`）**；
- 理解 `log-tree.c` 如何把「提交头 + 正文 + diff」拼成一次输出，以及 `--graph` 的 ASCII 图是怎么一行行画出来的。

## 2. 前置知识

本讲承接 **u9-l1（git add 与索引）**、**u7-l1（revision.c 遍历机制）**，并用到 u4-l1/u4-l2 的索引知识。开始前请确认以下概念：

- **三层模型**：工作树（worktree）／索引（index）／对象数据库（object store）。`git add` 把工作树变更写进索引，`git commit` 再把索引冻结成一个**对象**。
- **对象与内容寻址**（u3-l1、u3-l2）：git 有四种对象 `blob/tree/commit/tag`，每个对象以「类型 + 长度 + `\0` + 内容」整段的哈希命名。本讲的主角是 **commit 对象**。
- **cache-tree**（u4-l2）：索引上挂着的目录哈希缓存，能直接给出「整棵索引对应的 tree 哈希」，省去重新构造 tree 的开销。`git commit` 正是直接从这里取 tree 哈希。
- **revision 遍历**（u7-l1）：`git log` 不自己遍历历史，它复用 `get_revision()` 引擎，每弹出一个 commit 就交给本讲的 `log_tree_commit()` 去展示。

一个核心直觉：**commit 对象本身只是几行文本**。它不存文件差异，也不存快照内容，它只存「指向某棵 tree 的哈希 + 指向若干父 commit 的哈希 + 作者/提交者 + 提交说明」。真正的文件内容在 blob 里，目录结构在 tree 里，commit 只是一个把它们串起来的指针节点。理解这一点，本讲的源码就很好读了。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [builtin/commit.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/commit.c) | `git commit` 子命令实现。`cmd_commit` 编排整个提交流程：准备索引、跑钩子、收集消息、调用底层函数造 commit。 |
| [commit.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit.c) | commit 对象的核心库。`commit_tree` / `commit_tree_extended` / `write_commit_tree` 真正把「tree + parent + 消息」拼成字节流并哈希落盘。 |
| [builtin/log.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/log.c) | `git log` 子命令实现。`cmd_log` 装配 `rev_info`，`cmd_log_walk` 循环 `get_revision()` 取出每个 commit 交给 `log_tree_commit()`。 |
| [pretty.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pretty.c) | 格式化引擎。登记格式表、解析 `--pretty`、把一个 commit 渲染成字符串。 |
| [log-tree.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/log-tree.c) | 「一棵提交树」的输出粘合层。`log_tree_commit` / `show_log` 把 graph、header、pretty 正文、diff 串成一次完整输出。 |
| [graph.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/graph.c) | `--graph` 的 ASCII 提交图渲染器，一台一次画一行的状态机。 |

记忆要点：**写入侧**集中在 `builtin/commit.c` + `commit.c`；**展示侧**集中在 `builtin/log.c` + `log-tree.c` + `pretty.c` + `graph.c`。本讲最小模块即对应「写入主流程」「pretty 格式化」「log-tree 与 graph 输出」三段。

---

## 4. 核心概念与源码讲解

### 4.1 commit 创建主流程

#### 4.1.1 概念说明

一次 `git commit` 表面上「保存改动」，底层做的其实只有一件事：**用索引造出一个新的 commit 对象，并把当前分支指针移过去。**

这里要建立两个关键直觉：

1. **commit 不存改动，只存指针。** 它指向一棵 tree（这次提交的完整目录快照），并指向若干个父 commit（前驱）。所以 commit 对象本身非常小——通常不到一屏文本。
2. **tree 来自索引的 cache-tree。** `git add` 已经把工作树内容落成 blob 并登记进索引；提交时，cache-tree 早就把「整棵索引折叠成的那棵 tree」的哈希算好了（见 u4-l2）。于是 commit 流程**不必重新遍历目录构造 tree**，直接取 `index->cache_tree->oid` 即可。

承接 u3-l2 的内容寻址：commit 对象最终也是一个对象，它的 OID 是对

\[ \texttt{OID} = \mathrm{hash}\big(\texttt{"commit "}\, \|\, \texttt{len}\, \|\, \texttt{"\textbackslash 0"}\, \|\, \text{buffer}\big) \]

的哈希，其中 `buffer` 就是下面 4.1.3 要逐行拼出来的文本。

#### 4.1.2 核心流程

`git commit` 的执行主干（伪代码）：

```
cmd_commit(argv):
  解析选项 (-m/--amend/-a/-S ...)
  current_head = lookup_commit("HEAD")        # 首次提交时为 NULL

  index_file = prepare_index(...)             # ① 准备索引(可能是临时索引)
  prepare_to_commit(...)                      # ② 跑 prepare-commit/commit-msg 钩子、
                                              #    收集/编辑提交说明、把 cache_tree 落成 tree

  # ③ 决定父提交列表 parents
  if   current_head == NULL:  parents = []                       # 初始提交
  elif amend:                 parents = copy(current_head.parents)
  elif 处于 merge 状态:        parents = [HEAD] + 读 MERGE_HEAD 列表
  else:                       parents = [current_head]

  读提交说明文件 → cleanup_message(去注释、裁剪)

  # ④ 关键:造 commit 对象
  commit_tree_extended(msg, tree_oid = index.cache_tree->oid,
                       parents, &oid, author, NULL, sign, extra)

  # ⑤ 公开:更新分支指针(带 reflog)
  update_head_with_reflog(current_head, &oid, reflog_msg, ...)
  清理 MERGE_HEAD 等临时文件
  跑 post-commit 钩子;打印提交摘要
```

其中 `commit_tree_extended`（真正的对象创建）内部再做两步：

```
commit_tree_extended(msg, tree, parents, ...):
  write_commit_tree(&buffer, ...)     # 把 tree/parent/author/committer/... 拼成文本 buffer
  (可选)GPG 签名、兼容哈希算法双写
  odb_write_object_ext(buffer, OBJ_COMMIT)   # 哈希 + 落盘(松散对象或 pack)
```

#### 4.1.3 源码精读

**① 命令入口与流程编排。** `cmd_commit` 在解析完选项、准备好索引、收集完消息后，进入「确定父提交」这一段，注意它如何区分初始提交 / amend / merge / 普通四种情况：

[builtin/commit.c:1849-1894](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/commit.c#L1849-L1894) —— 这段按 `current_head` 是否存在、`amend`、`whence == FROM_MERGE` 三个判据组装 `parents` 列表；普通提交就是把当前 HEAD 插入 `parents`。

**② 真正造 commit 的那一行。** 紧接着，函数把消息、父列表交给底层，**tree 的哈希直接取自索引的 cache-tree**：

[builtin/commit.c:1938-1943](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/commit.c#L1938-L1943) —— `&the_repository->index->cache_tree->oid` 就是「这棵索引折叠成的 tree」的哈希（见 u4-l2 的 cache-tree），把它连同 `sb`（提交说明）、`parents`、`author_ident.buf`（作者身份）一起传给 `commit_tree_extended`。失败则回滚索引锁。这就是「commit = tree + parent + message」的源头。

**③ 拼装 commit 文本。** `commit_tree_extended` 把工作委托给 `write_commit_tree`，后者逐行拼出 buffer：

[commit.c:1693-1734](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit.c#L1693-L1734) —— 这就是 commit 对象的「图纸」：

```
strbuf_addf(buffer, "tree %s\n", oid_to_hex(tree));      // tree 行
for (...) strbuf_addf(buffer, "parent %s\n", ...);       // 0 或多行 parent
strbuf_addf(buffer, "author %s\n", author);              // author 行
strbuf_addf(buffer, "committer %s\n", committer);        // committer 行
(若非 utf8) strbuf_addf(buffer, "encoding %s\n", ...);
while (extra) add_extra_header(buffer, extra);           // 额外头(如 GPG 签名、merge tag)
strbuf_addch(buffer, '\n');                              // 头与正文的空行分隔
strbuf_add(buffer, msg, msg_len);                        // 提交说明正文
```

注意注释里那句「**This ordering means that the same exact tree merged with a different order of parents will be a different changeset**」：父提交的**顺序**也参与哈希，所以 merge 提交即使内容相同，只要父顺序不同就是不同对象。

**④ 哈希落盘。** 回到 `commit_tree_extended`，buffer 拼好（含可选的 GPG 签名嵌入、兼容哈希算法双写）后：

[commit.c:1855-1856](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit.c#L1855-L1856) —— `odb_write_object_ext(..., OBJ_COMMIT, ...)` 把 buffer 按对象规则哈希、写入对象数据库（承接 u3-l2 的写入路径）。写入成功后，新 commit 的 OID 由 `ret` 带回 `cmd_commit`。

> 小知识：`commit.c` 还提供一个更简洁的包装 `commit_tree()`（[commit.c:1565](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit.c#L1565)），它内部先追加 merge tag 头再调用 `commit_tree_extended`，供不需要那么多控制权的调用方使用。`git commit` 走的是功能更全的 `commit_tree_extended`。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 commit 对象的原始文本，并把它与 `write_commit_tree` 的拼装顺序一一对应。

**操作步骤**：

1. 在任意目录初始化一个临时仓库（用刚才编译出的 git）：

   ```bash
   /path/to/compiled/git init demo && cd demo
   echo hello > file.txt
   /path/to/compiled/git add file.txt
   /path/to/compiled/git -c user.email=a@b.c -c user.name=A commit -m "first commit"
   ```

2. 打印刚创建的 commit 对象的原始内容：

   ```bash
   /path/to/compiled/git cat-file -p HEAD
   ```

3. 对照 [commit.c:1706-1733](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit.c#L1706-L1733) 的拼装顺序，逐行核对输出。

**需要观察的现象**：

- `git cat-file -p HEAD` 的输出应当形如：

  ```
  tree <40 位哈希>
  author A <a@b.c> <时间戳> <时区>
  committer A <a@b.c> <时间戳> <时区>

  first commit
  ```

  首次提交**没有 `parent` 行**（因为 `parents` 为空，循环零次）；非首次提交会多出 `parent` 行；merge 提交会有多个 `parent` 行。

- 头部与正文之间正好一个空行（对应 `write_commit_tree` 末尾的 `strbuf_addch(buffer, '\n')`）。

**预期结果**：你能在输出的每一行与源码的 `strbuf_addf` 调用之间建立一一对应。若进一步想验证「tree 行指向的真是这次快照」，可对 `tree` 哈希再跑一次 `git cat-file -p <tree>`，会看到里面的 `file.txt` 条目。

> 说明：本实践依赖已编译好的 git（见 u1-l2）。若尚未编译，用系统自带的 `git` 也可观察同样输出，只是无法在源码层面逐行比对。

#### 4.1.5 小练习与答案

**练习 1**：为什么首次提交的 `git cat-file -p HEAD` 没有 `parent` 行？请从源码角度回答。

**参考答案**：`cmd_commit` 在 `current_head == NULL` 时（[builtin/commit.c:1851-1853](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/commit.c#L1851-L1853)）不向 `parents` 列表插入任何项，于是 `commit_tree_extended` 收到的 `parents` 为空，`write_commit_tree` 里 `for (i...)` 的 `parent` 循环执行零次，buffer 里自然没有 `parent` 行。

**练习 2**：`git commit --amend` 时，新 commit 的父提交是谁？

**参考答案**：是**原 commit 的父提交列表**（[builtin/commit.c:1857](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/commit.c#L1857) 的 `commit_list_copy(current_head->parents)`），而不是原 commit 自己。这样旧 commit 被新 commit「替换」，历史看起来像在原地修改。

---

### 4.2 pretty 格式化

#### 4.2.1 概念说明

`git log` 默认输出一段人眼友好的多行信息（medium 格式），但你也可以用 `--pretty=oneline`、`--pretty=format:'%h %s'` 改成一行或自定义字段。源码用**两套机制**实现这两类需求：

1. **结构化格式（`enum cmit_fmt`）**：`raw / medium / short / full / fuller / oneline / email / mboxrd`。它们是「按固定结构把 commit 的 header + 正文重新排版」，比如 `medium` 显示 author+日期，`fuller` 额外显示 committer+日期。代码里是一个 enum + 一张名字表。
2. **占位符格式（`USERFORMAT`）**：`%H`、`%h`、`%s`、`%an`、`%ad` …… 用户给一个含 `%` 的模板，引擎逐个替换占位符。`oneline` 其实等价于 `%H %s`。

一个 `--pretty` 参数会被 `get_commit_format()` 分类：含 `format:`/`tformat:`/`%` 的归到占位符格式；其余按名字查结构化格式表。

#### 4.2.2 核心流程

```
get_commit_format(arg, rev):                  # 在 setup_revisions 里被 --pretty 调用
  if "format:" 前缀        → save_user_format(is_tformat=0)
  elif 空/"tformat:"/含'%' → save_user_format(is_tformat=1)
  else                     → 查 builtin 格式表 → rev->commit_format = 该 enum

pretty_print_commit(pp, commit, sb):          # 渲染入口
  if pp->fmt == CMIT_FMT_USERFORMAT:
      repo_format_commit_message(...)         # 走占位符循环
      return
  pp_header(...)                              # 结构化:重排 header
  pp_remainder(...)                           # 处理正文

repo_format_commit_message(format):           # 占位符循环
  while strbuf_expand_step(sb, &format):      # 逐段扫描模板
      if "%%"  → 输出字面 '%'
      elif len = format_commit_item(...)      → 解析并替换一个 %占位符
      else     → 当作字面 '%'
```

结构化格式的差异主要体现在 `pp_header`：`MEDIUM` 只显示 author，`FULL` 显示 author + committer 但不带日期，`FULLER` 两者都带日期（见 [pretty.c:2092-2105](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pretty.c#L2092-L2105) 的注释）。

#### 4.2.3 源码精读

**① 格式表登记。** 启动时 `setup_commit_formats` 把内置格式填进一张 `cmt_fmt_map` 表：

[pretty.c:120-137](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pretty.c#L120-L137) —— 注意 `reference` 这一格式的 `user_format` 字段就是 `"%C(auto)%h (%s, %ad)"`，说明「结构化格式」与「占位符模板」在这里是统一的：`reference` 本质上是一个被预设好的 userformat，`format` 取 `CMIT_FMT_USERFORMAT`。

**② `--pretty` 参数分类。**

[pretty.c:190-222](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pretty.c#L190-L222) —— `get_commit_format` 三分支：`format:` 前缀、含 `%`（或 `tformat:`）走 `save_user_format`；其余用 `find_commit_format` 按名字查表并拷贝其 enum（`rev->commit_format`）。`is_tformat` 决定每条记录末尾是否补一个分隔符（见下文 `use_terminator`）。

**③ 渲染入口的分发。**

[pretty.c:2298-2345](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pretty.c#L2298-L2345) —— `pretty_print_commit` 一上来判断：若是 `USERFORMAT` 就转交 `repo_format_commit_message` 后直接返回；否则进入 `pp_header`（重排头部）+ `pp_remainder`（正文）的结构化路径。`oneline` 与 mail 格式会对 subject（标题行）特殊处理。

**④ 占位符替换循环。**

[pretty.c:2000-2023](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pretty.c#L2000-L2023) —— `repo_format_commit_message` 用 `strbuf_expand_step` 把模板切成一段段：遇到 `%` 就调 `format_commit_item` 替换，遇到 `%%` 输出字面 `%`。

**⑤ 单个占位符的解析。** `format_commit_item` 处理魔法前缀（`%+`、`%-`、`% `，用于「非空才加换行/空格」），然后调 `format_commit_one` 真正解释占位符：

[pretty.c:1906-1948](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pretty.c#L1906-L1948) —— `format_commit_item` 的分发骨架；真正的巨大 `switch` 在 `format_commit_one`，例如：

[pretty.c:1567-1600](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pretty.c#L1567-L1600) —— 这就是 `%H`（完整哈希）、`%h`（缩写哈希）、`%T`/`%t`（tree 哈希）、`%P`/`%p`（父哈希）的来源。注意 `%H`、`%h` 还会自动包裹提交色（`DIFF_COMMIT`），这就是 `--pretty` 输出里哈希有颜色的原因。

#### 4.2.4 代码实践

**实践目标**：用同一个 commit，对比 `--pretty=medium`（结构化）与 `--pretty=format:'%H%n作者:%an%n标题:%s'`（占位符），理解两条渲染路径。

**操作步骤**：

```bash
git log -1 --pretty=medium
git log -1 --pretty=format:'%H%n作者:%an%n标题:%s'
git log -1 --pretty=format:'%h %s'   # 你会发现末尾没有换行
git log -1 --pretty=tformat:'%h %s'  # tformat 在末尾补一个换行
```

**需要观察的现象**：

- `medium` 输出多行，含 `Author:`、`Date:` 与正文；
- 自定义 `format:` 输出完全由你给的模板决定，`%H` 换成完整哈希、`%an` 换成作者名、`%s` 换成标题；
- `format:` 与 `tformat:` 的唯一差别是末尾换行（对应 [pretty.c:57-58](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pretty.c#L57-L58) 的 `rev->use_terminator = 1`）。

**预期结果**：你能预测任意 `%` 模板的输出。若想确认某个占位符的源码出处，可在 [pretty.c:1437](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pretty.c#L1437) 起的 `format_commit_one` switch 里搜索对应的 `case`。

#### 4.2.5 小练习与答案

**练习 1**：`--pretty=oneline` 和 `--pretty=format:'%H %s'` 效果几乎一样，源码上它们是同一条路径吗？

**参考答案**：`oneline` 是结构化格式（`CMIT_FMT_ONELINE`，见 [pretty.c:130](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pretty.c#L130)），走 `pp_header` 的特化分支（只取 subject、`indent=0`）；而 `format:'%H %s'` 走 `USERFORMAT` 占位符循环。两者实现路径不同、输出恰好相近。

**练习 2**：为什么 `--pretty=format:'%h %s'` 输出的哈希是**带颜色**的？

**参考答案**：`%h` 对应分支里调了 `diff_get_color(... DIFF_COMMIT)`（[pretty.c:1572-1577](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pretty.c#L1572-L1577)），只有当 `--no-color` 或输出不是终端时颜色码才会被吞掉。

---

### 4.3 log-tree 与 graph 输出

#### 4.3.1 概念说明

pretty.c 只负责「把一个 commit 渲染成字符串」，但 `git log` 的屏幕输出还要解决：**记录之间的分隔、可选的 diff、可选的 ASCII 图、可选的 reflog/装饰/note**。这些「粘合」工作都在 `log-tree.c`，而 ASCII 图由 `graph.c` 单独承担。

关键关系链：

```
cmd_log → cmd_log_walk → get_revision()(u7-l1) → log_tree_commit()
                                                      ├─ log_tree_diff()   # 可选 diff
                                                      └─ show_log()        # 头 + pretty 正文 + graph
```

`--graph` 不是 pretty 的一部分，而是一个**独立的渲染器** `struct git_graph`，挂在 `rev_info->graph` 上。它是一台状态机，每次 `graph_next_line()` 画一行 ASCII（如 `*`、`|`、`|\`），循环到「画完本 commit 的 `*` 行」为止；之后 `show_log` 把 pretty 正文逐行前缀上 graph 的列宽。

#### 4.3.2 核心流程

```
cmd_log_walk(rev):                          # git log 主循环
  prepare_revision_walk(rev)
  while commit = get_revision(rev):         # u7-l1 的遍历引擎
      log_tree_commit(rev, commit)

log_tree_commit(opt, commit):
  shown = log_tree_diff(opt, commit, ...)   # 若需要 diff 先算
  if !shown && always_show_header:
      show_log(opt)                         # 打印 header + pretty 正文

show_log(opt):                              # 一次完整输出
  graph_show_commit(opt->graph)             # 先画 graph 到本 commit 行(*)
  (按格式)输出 "commit <hash>" / 装饰 / reflog
  填充 pretty_print_context ctx
  pretty_print_commit(&ctx, commit, &msgbuf)   # 调 4.2 的引擎,得到正文
  graph_show_commit_msg(opt->graph, file, &msgbuf)  # 给正文每行加 graph 前缀
```

graph 状态机的状态枚举：

```
enum graph_state { GRAPH_PADDING, GRAPH_SKIP, GRAPH_PRE_COMMIT,
                   GRAPH_COMMIT, GRAPH_POST_MERGE, GRAPH_COLLAPSING }
```

`graph_next_line` 根据 `state` 调不同的画线函数，每画一行把 graph 推进一个状态，直到打出 `GRAPH_COMMIT`（带 `*` 的那行）才认为「本 commit 画完」。

#### 4.3.3 源码精读

**① `git log` 的遍历循环。**

[builtin/log.c:406-439](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/log.c#L406-L439) —— `cmd_log_walk_no_free` 的核心就是 `while ((commit = get_revision(rev)) != NULL) log_tree_commit(rev, commit)`。这里**直接复用 u7-l1 的遍历引擎**，本讲只关心 `log_tree_commit` 怎么展示。注意循环里还会 `free_commit_buffer` 释放已用完的 commit 文本以省内存。

**② log_tree_commit 的编排。**

[log-tree.c:1267-1296](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/log-tree.c#L1267-L1296) —— 先 `log_tree_diff` 尝试算 diff；若没东西可显示但 `always_show_header` 成立，就调 `show_log` 单独打印提交头（这正是 `git log` 不带 `-p` 时仍打印每个 commit 的原因）。

**③ show_log：粘合 graph + header + pretty。**

[log-tree.c:742-769](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/log-tree.c#L742-L769) —— 开头先 `graph_show_commit(opt->graph)` 把图画到本 commit 行；非 verbose 模式（`oneline` 等）打印缩写哈希后即返回。这是「graph 在前，文字在后」的起点。

[log-tree.c:873-889](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/log-tree.c#L873-L889) —— 把 `opt` 里的日期模式、缩写长度、颜色、`commit_format` 等塞进 `pretty_print_context ctx`，并从 graph 取列宽 `ctx.graph_width = graph_width(opt->graph)`，然后调 `pretty_print_commit(&ctx, commit, &msgbuf)` 得到正文 buffer。

[log-tree.c:915-920](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/log-tree.c#L915-L920) —— `graph_show_commit_msg(opt->graph, file, &msgbuf)` 把刚才渲染好的正文**逐行**前缀上 graph 的列，于是正文和 `*`、`|` 自然对齐；最后若 `use_terminator`（tformat 场景）补一条分隔。

**④ `--graph` 的启用与状态机。**

[revision.c:2622-2624](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/revision.c#L2622-L2624) —— `--graph` 在 `setup_revisions` 里被解析为 `revs->graph = graph_init(revs)`，graph 对象从此挂在 rev_info 上；`--no-graph` 则清空它。

[graph.c:65-69](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/graph.c#L65-L69) —— graph 的状态枚举，刻画「一行 ASCII 画到哪一步了」。

[graph.c:154-184](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/graph.c#L154-L184) —— `struct git_graph` 持有当前 commit、列布局、宽度、`state` 等；它的注释清楚说明 `width` 是「为保证后续正文对齐而统一填充到的列宽」。

[graph.c:1528-1557](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/graph.c#L1528-L1557) —— `graph_show_commit` 反复调 `graph_next_line` 画线、换行，直到 `shown_commit_line` 为真（即打出含 `*` 的 commit 行）才停。`graph_next_line` 的状态分派见 [graph.c:1454-1474](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/graph.c#L1454-L1474)。

#### 4.3.4 代码实践

**实践目标**：观察 graph 与 pretty 正文的对齐关系，并对照状态机理解 `*` 行的来源。

**操作步骤**：

```bash
# 造一条分叉历史
git init graph-demo && cd graph-demo
git commit --allow-empty -m root
git checkout -b side && git commit --allow-empty -m side-1
git checkout master && git commit --allow-empty -m master-1
git merge --no-ff side -m merge

# 开 graph 看效果
git log --graph --oneline
git log --graph --pretty=format:'%h %s'    # 正文与 * 对齐
```

**需要观察的现象**：

- `--graph` 在每个 commit 左侧画出 `*`、`|\`、`|/` 等连线，merge 处会出现分叉再合拢；
- 正文（哈希+标题）始终从同一列开始，这就是 `ctx.graph_width` + `graph_show_commit_msg` 的对齐效果；
- 对比无 `--graph` 的版本，少了左侧连线，正文顶格。

**预期结果**：你能指出 `*` 行对应 `graph_next_line` 在 `GRAPH_COMMIT` 状态下画的线（[graph.c:1464-1467](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/graph.c#L1464-L1467)），其余 `|`/`|\` 行来自 `GRAPH_PADDING`/`GRAPH_POST_MERGE`/`GRAPH_COLLAPSING` 等状态。

> 待本地验证：具体 ASCII 形状取决于你的真实分支拓扑；上述命令仅用于触发 graph 渲染。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `git log` 默认不显示 diff，但仍然打印每个 commit 的头？

**参考答案**：`log_tree_commit` 先调 `log_tree_diff`；不带 `-p` 时 diff 无输出，于是 `shown == 0`，但 `opt->always_show_header` 在 `cmd_log` 里被设为 1（[builtin/log.c:842](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/log.c#L842)），因此进入 `show_log` 分支单独打印提交头（[log-tree.c:1292-1296](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/log-tree.c#L1292-L1296)）。

**练习 2**：`--graph` 和 `--pretty` 是同一个子系统吗？

**参考答案**：不是。`--pretty` 控制 `commit_format`（pretty.c），`--graph` 创建独立的 `struct git_graph`（graph.c）。两者在 `show_log` 里被串起来：先 `graph_show_commit` 画图，再 `pretty_print_commit` 出正文，最后 `graph_show_commit_msg` 把正文前缀上列宽。

---

## 5. 综合实践

把本讲三个模块串起来，做一个端到端的「造提交 + 看内部 + 美化展示」小任务。

**任务**：在一个新仓库里制造一次普通提交和一次 merge 提交，然后：

1. 用 `git cat-file -p` 查看普通 commit 与 merge commit 的原始内容，对照 [commit.c:1706-1733](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit.c#L1706-L1733) 解释普通 commit 有 0/1 个 parent、merge commit 有 ≥2 个 parent 的来源（[builtin/commit.c:1858-1885](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/commit.c#L1858-L1885) 的 `FROM_MERGE` 分支）。
2. 用一条 `--pretty=tformat` 自定义模板，把 merge commit 的「完整哈希、作者、提交者、所有父哈希、标题」一次性输出，并逐个占位符回指到 `format_commit_one` 的对应 `case`（提示：`%H`、`%an`、`%cn`、`%P`、`%s`）。
3. 开 `--graph`，确认正文与 `*`/`|\` 对齐，并用一句话说明这是 `show_log` 中 `graph_show_commit_msg` 起的作用（[log-tree.c:915](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/log-tree.c#L915)）。

**验收标准**：你能不查文档地说出「merge commit 为什么有多行 parent」「`%P` 在源码哪一行被替换」「graph 的 `*` 是哪个状态画的」这三件事的答案。

## 6. 本讲小结

- **commit 对象是文本指针，不是改动**：`git commit` 经 `cmd_commit` 编排，由 `commit_tree_extended` → `write_commit_tree` 拼出 `tree/parent/author/committer/...` + 空行 + 正文，哈希后落盘（[commit.c:1693](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit.c#L1693)）。
- **tree 哈希取自索引的 cache-tree**：`&index->cache_tree->oid`（[builtin/commit.c:1938](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/commit.c#L1938)），所以提交无需重新构造目录树（承接 u4-l2）。
- **父列表分四种情况**：初始 / amend / merge / 普通，由 [builtin/commit.c:1849-1894](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/commit.c#L1849-L1894) 决定，父的顺序也参与哈希。
- **pretty 是两套机制**：结构化格式（`pp_header`/`pp_remainder`）与占位符格式（`repo_format_commit_message` 的 `%` 循环，`format_commit_item`→`format_commit_one`）。
- **`--pretty` 分类在 `get_commit_format`**：含 `%` 走 userformat，否则查 builtin 表（[pretty.c:190](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pretty.c#L190)）。
- **log-tree 是粘合层**：`git log` 复用 u7-l1 的 `get_revision`，每个 commit 交 `log_tree_commit` → `show_log`，把 graph、header、pretty 正文、diff 串成输出；`--graph` 是 `graph.c` 里一台一次画一行的状态机。

## 7. 下一步学习建议

本讲覆盖了「写入 commit」与「展示历史」的主干。建议接着深入：

- **u8-l1（diff 核心引擎）**：本讲的 `log_tree_diff` 会调 diffcore 生成 `-p` 的补丁，理解 diff 管线后，`git log -p` 的每一行 hunk 都能找到出处。
- **u7-l2（commit 可达性与 commit-graph）**：`git log` 的大仓库性能依赖 commit-graph 缓存 parent 查找，承接本讲对遍历的理解。
- **`builtin/log.c` 的其余命令**：`git show`（`cmd_show`，[builtin/log.c:661](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/log.c#L661)）、`git format-patch`（email 格式，`pp_email_subject`）都复用本讲的 pretty/log-tree 基础设施，可作为阅读练习。
- **若想动手改 git**：尝试在 [pretty.c:1437](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pretty.c#L1437) 的 switch 里加一个自定义占位符（不提交），观察 `--pretty=format:'%<你的占位符>'` 的输出变化——这是熟悉格式引擎最快的方式。
