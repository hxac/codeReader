# diff 核心引擎与 diffcore

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `git diff` 一次比较在源码里走过的完整管线：从「喂入文件对」到「产出 diff 结果」中间经历了哪些阶段。
- 理解 diffcore 的核心数据模型：`diff_filespec`（单个文件）、`diff_filepair`（新旧两个文件组成的对）、`diff_queue_struct`（文件对队列）。
- 掌握重命名检测（`-M`/`-C`）的两条路径：精确匹配（按对象哈希）与模糊匹配（按内容相似度），以及它如何决定一个文件对的状态是 `R`/`C`/`M`。
- 理解「彻底重写」检测（`-B`）如何先把一个大改动拆成「删除 + 新增」、再在重命名检测后合并回来。
- 看懂 diff 的输出格式：状态码（A/D/M/R/C/T）、相似度分数（如 `R100`）与 raw/patch 两类输出的生成位置。
- 明确本讲（diffcore，文件级）与下一讲（xdiff，行级）的边界。

## 2. 前置知识

本讲默认你已掌握：

- **git 对象模型**（u3-l1）：blob 存文件内容、tree 存目录清单。diff 比较的对象，本质上是两个 blob 或两棵 tree。
- **内容寻址**：每个 blob 有一个对象哈希（OID）。两个内容完全相同的 blob 必然有相同 OID——这是精确重命名检测的基础。
- **工作树 / 索引 / HEAD 三层模型**（u4-l1、u9-l3）：`git diff` 比较的是其中两层（如索引 vs 工作树，或 HEAD vs 索引）。

两个需要先建立的直觉：

**直觉一：diff 分两层。** git 把「找差异」拆成两层：
- **文件级（diffcore，本讲）**：决定「哪些文件和哪些文件配成对、它们是新增/删除/修改/重命名/复制」。这一层不关心文件内部的具体行。
- **行级（xdiff，下一讲 u8-l2）**：给定一对已经配好的文件，算出它们逐行的增删（即真正的 `@@ ... @@` 补丁）。

**直觉二：diff 是一条流水线（pipeline）。** 上游命令（如 `git diff-files`、`git diff-tree`）把「粗粒度的文件对」倒进一个全局队列 `diff_queued_diff`，然后调用 `diffcore_std()` 让流水线依次处理（拆分重写、检测重命名、过滤、排序），最后 `diff_flush()` 把结果按指定格式打印出来。本讲就是在拆解这条流水线。

> 术语：**filepair（文件对）** = 一个旧文件（`one`，preimage）+ 一个新文件（`two`，postimage）。**filespec（文件规格）** = 单个文件的描述（OID + 路径 + 模式）。**porcelain**（高级命令，如 `git diff`）和 **plumbing**（底层命令，如 `git diff-tree`、`git diff-files`）共用同一套 diffcore 引擎。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [diffcore.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore.h) | diffcore 内部头文件，定义 `diff_filespec`、`diff_filepair`、`diff_queue_struct` 三大数据结构与各 `diffcore_*` 阶段函数声明。**仅 diff.c 与各 diffcore-*.c 之间使用。** |
| [diff.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.h) | diff 对外公共 API：`struct diff_options`（所有 diff 选项）、`diff_addremove`/`diff_change`（喂入接口）、`diffcore_std`/`diff_flush`（管线入口/输出）、`DIFF_STATUS_*` 状态码。 |
| [diff.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c) | diff 主框架：选项解析、队列管理、`diffcore_std` 编排、`diff_flush` 输出、行级 patch 生成（调用 xdiff）。 |
| [diffcore-rename.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-rename.c) | 重命名/复制检测：精确匹配 + 基名匹配 + 模糊（inexact）匹配。 |
| [diffcore-break.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-break.c) | 彻底重写检测（`-B`）：拆分 `diffcore_break` 与合并 `diffcore_merge_broken`。 |
| [diffcore-delta.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-delta.c) | 用「分块哈希」近似估算两个文件的内容重叠量，供相似度打分使用（`diffcore_count_changes`）。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- 4.1 数据模型与 diff 管线阶段
- 4.2 break/merge：彻底重写的拆分与合并（`-B`）
- 4.3 diffcore rename 检测：精确 + 模糊匹配（`-M`/`-C`）
- 4.4 diff 输出格式：状态码、分数与 raw/patch

---

### 4.1 数据模型与 diff 管线阶段

#### 4.1.1 概念说明

diffcore 处理的最小单元不是「行」，而是「文件对（filepair）」。整条管线的运转建立在一个全局队列之上：

- **`diff_filespec`**：描述单个文件（旧侧或新侧），记录它的对象哈希 `oid`、路径 `path`、模式 `mode`，以及内容是否有效（`oid_valid`）。
- **`diff_filepair`**：把一个旧 `one` 和一个新 `two` 组成一对。创建时 `one` 和 `two` 通常来自同一路径；但流水线后续可以「拆开重组」，把不同来源的 filespec 重新配对——这就是重命名检测。
- **`diff_queue_struct`**：一个文件对指针的动态数组。全局变量 `diff_queued_diff` 就是这条流水线的工作台。

调用方先「喂料」：对每个新增文件调 `diff_addremove('+', ...)`、删除调 `diff_addremove('-', ...)`、修改调 `diff_change(...)`，这些函数把构造好的 filepair 塞进 `diff_queued_diff`。喂料完毕后调一次 `diffcore_std()`，让各阶段依次对这个队列做变换。最后调 `diff_flush()` 输出。

这条「喂料 → diffcore_std → diff_flush」的三段式正是 diff.h 头部注释所描述的官方调用序列。

#### 4.1.2 核心流程

```
上游命令（diff-files / diff-tree / status / commit …）
   │  逐文件发现差异
   ▼
diff_addremove() / diff_change()        ← 喂料：构造 diff_filepair 入队
   │  填满全局队列 diff_queued_diff
   ▼
diffcore_std(options)                    ← 流水线入口，按固定顺序变换队列
   ├─ diffcore_skip_stat_unmatch()   （仅 stat 脏、内容其实未变 → 丢弃）
   ├─ diffcore_break()               （-B：把彻底重写拆成 删除+新增）
   ├─ diffcore_rename()              （-M/-C：重命名/复制检测，重组 filepair）
   ├─ diffcore_merge_broken()        （-B：把没匹配上的拆分对合并回来）
   ├─ diffcore_pickaxe()             （-S/-G：按字符串过滤）
   ├─ diffcore_order()               （-O：按 orderfile 重排）
   ├─ diffcore_rotate()              （--rotate-to/--skip-to）
   ├─ diff_resolve_rename_copy()     （为每个 filepair 算出状态码 A/D/M/R/C/T）
   └─ diffcore_apply_filter()        （--diff-filter：按状态码过滤）
   ▼
diff_flush(options)                      ← 输出：raw / name-status / stat / patch
```

关键点：**每个 `diffcore_*` 阶段都是「读入 `diff_queued_diff` → 变换 → 用新队列整体替换旧队列」**。这是典型的「流式变换器」模式：阶段之间不直接传递数据，而是都作用于同一个全局队列，用 `free(q->queue); *q = outq;` 完成替换。

#### 4.1.3 源码精读

**三大数据结构**（[diffcore.h:L52-L76](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore.h#L52-L76)）：`diff_filespec` 记录单个文件的 `oid`/`path`/`mode`，并用 `oid_valid` 区分「对象已知（信任 oid）」与「需从工作树读取」。

文件对与队列（[diffcore.h:L117-L170](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore.h#L117-L170)）：`diff_filepair` 持有 `one`/`two` 两个 filespec 指针，外加一个 `score`（相似度分数）和 `status`（状态码，初始为 0）。`diff_queue_struct` 仅是 `diff_filepair **queue` + 容量/数量。

**入队原语**（[diff.c:L6382-L6398](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c#L6382-L6398)）：`diff_q` 把一个 filepair 追加进队列（`ALLOC_GROW` 自动扩容）；`diff_queue` 分配并初始化一个新 filepair 再入队。这是所有阶段的共用积木。

**喂料接口** `diff_queue_change`（[diff.c:L7650-L7699](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c#L7650-L7699)）：为旧/新各 `alloc_filespec` 一个 spec，`fill_filespec` 填入 oid 与 mode，再 `diff_queue` 入队。它还顺手做了 `quick` 模式下的 stat 提前判断（`diff_filespec_check_stat_unmatch`），用以快速判定「其实没变」。`diff_addremove`（[diff.c:L7599-L7648](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c#L7599-L7648)）结构相同，只是只填 `one` 或只填 `two`。

**流水线编排** `diffcore_std`（[diff.c:L7483-L7534](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c#L7483-L7534)）正是上面流程图的源码化身。注意三个关键开关：`options->break_opt != -1` 才跑 break/merge，`options->detect_rename` 非零才跑 rename。这两者默认都关闭——也就是说**默认 `git diff` 是不做 `-B`/`-M` 的**（`-M` 对 `git diff` 这个 porcelain 命令另有 UI 默认开启，但核心管线本身默认不跑）。函数末尾根据队列是否非空设置 `has_changes` 标志，供 `--quiet` 等提前退出使用。

**「内容其实没变」过滤** `diffcore_skip_stat_unmatch`（[diff.c:L7396-L7421](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c#L7396-L7421)）：处理「stat 信息显示文件改了（如 mtime 变了），但内容其实相同」的情况，先比 size 再比内容，把假阳性剔除。

> 边界提示：`diffcore_std` 只决定「哪些文件配对、状态是什么」。真正的逐行 `@@ ... @@` 补丁发生在 `diff_flush` → `diff_flush_patch` → `run_diff` → `builtin_diff` → `xdi_diff_outf`（[diff.c:L4135-L4137](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c#L4135-L4137)），那里把一对 filespec 的内容交给 xdiff 库。**行级算法是下一讲 u8-l2 的主题，本讲到此止步。**

#### 4.1.4 代码实践

**目标**：用一个最简单的 diff，看清「喂料 → 管线 → 输出」的最小闭环，并验证默认管线不做重命名检测。

1. 准备一个临时仓库并制造一处修改：
   ```bash
   git init diff-demo && cd diff-demo
   printf 'hello\n' > a.txt && git add a.txt && git commit -m init
   printf 'hello\nworld\n' > a.txt
   ```
2. 用 plumbing 命令查看「raw」格式的输出（直接反映 filepair 的状态码与 oid）：
   ```bash
   git diff-files
   ```
3. 预期看到一行类似 `:100644 100644 <oid> <oid> M\ta.txt`。开头的 `:` 表示 raw 格式，`M` 是状态码（修改）。
4. **观察现象**：现在把 `a.txt` 改名为 `b.txt`（内容不变）：
   ```bash
   git mv a.txt b.txt
   git diff-files
   ```
   **预期结果**：默认管线不检测重命名，会输出两条——`a.txt` 被删除（`D`）和 `b.txt` 被新增（`A`），而不是一条 `R` 重命名。
5. 对比开启重命名检测后（porcelain 默认开 `-M`）：
   ```bash
   git diff-files -M        # 或直接 git status
   ```
   **预期结果**：出现一条形如 `R100\ta.txt\tb.txt` 的记录，`R` 表示重命名、`100` 表示 100% 相似。

> 待本地验证：不同 git 版本的 porcelain 默认行为略有差异；以你本机 `git --version` 的实际输出为准。核心结论是「核心管线默认不做 rename，需 `-M` 触发」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `diffcore_std` 里 `diffcore_break` 和 `diffcore_merge_broken` 总是成对出现、且中间夹着 `diffcore_rename`？

**参考答案**：break 的目的是把「一个文件的彻底重写」拆成「删除旧 + 新增新」两半，让随后的 `diffcore_rename` 有机会把这些碎片和别的文件重新配对（例如把内容从一个文件搬到另一个文件）。rename 跑完后，那些没找到合适配对、其实只是普通重写的碎片，再由 `merge_broken` 合并回一条修改记录。所以三者顺序必须是 break → rename → merge。

**练习 2**：`diff_filepair` 的 `status` 字段在入队时是什么值？由谁最终赋值？

**参考答案**：入队时为 0（`xcalloc` 零初始化，表示「未决」）。最终由 `diff_resolve_rename_copy()`（4.4 节）在管线末尾根据 filepair 的形态赋值为 `A`/`D`/`M`/`R`/`C`/`T` 等状态码。

---

### 4.2 break/merge：彻底重写的拆分与合并（`-B`）

#### 4.2.1 概念说明

考虑这种场景：你把 `a.txt` 的 100 行全删了，重写了 100 行全新内容。这其实是一次「彻底重写」。默认 diff 会把它当成一条普通修改（`M`），但修改量巨大，补丁几乎不可读。

`-B`（break）选项改变这个行为：当一个文件的改动大到「与其说是修改，不如说是删除+新增」时，先把这条修改**拆**成「删除旧文件 + 新增新文件」两个 filepair。拆分的好处是：随后的重命名检测可以把这些碎片和别的文件重新匹配（比如你其实是把 `a.txt` 的内容搬到 `b.txt`，又全新写了 `a.txt`）。

拆完之后，那些没能在重命名检测中找到「伴侣」的碎片，再被 **merge**（合并）回一条修改记录——因为既然没人要它，说明它确实就是原地重写。

#### 4.2.2 核心流程

```
对每条「两侧都是 blob、同名」的修改对 p：
   should_break(src=one, dst=two) 判定改动是否过大
      ├─ 否 → 原样保留
      └─ 是 → 拆成两个 filepair：
              ① (one → 空)  标记 broken_pair，记 score=合并用分数
              ② (空 → two)  标记 broken_pair
   ……中间经过 diffcore_rename 重组……
diffcore_merge_broken：扫描 broken_pair，
   若同一文件的「删除半」与「新增半」都存活且没被重命名匹配 → 合并回一条修改
```

关键在于 `should_break` 用了**两套不同的判定标准**（diffcore-break.c 注释明确说明）：

- **拆分标准**：同时考虑「删除量 + 新增量」（delta），用于决定「要不要拆」。
- **合并标准**：只考虑「删除量」（从源文件中移除了多少），用于决定「拆了之后还要不要合回去」。

理由：从 100 行删到只剩 3 行，无论你又新增了 3 行还是 903 行，本质都是「重写了 97%」。

#### 4.2.3 源码精读

**分数常量**（[diffcore.h:L39-L44](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore.h#L39-L44)）：`MAX_SCORE=60000` 是分数上限（对应 100%）；`DEFAULT_BREAK_SCORE=30000`（改动达 50% 才考虑拆）；`DEFAULT_MERGE_SCORE=36000`（删除量达 60% 才不合回）；`MINIMUM_BREAK_SIZE=400`（太小的文件不拆，避免无谓开销）。

**拆分判定** `should_break`（[diffcore-break.c:L13-L129](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-break.c#L13-L129)）：核心计算两步——

```c
src_removed  = src->size - src_copied;                    /* 从源删了多少 */
*merge_score_p = (int)(src_removed * MAX_SCORE / src->size);  /* 合并用分数 */
if (*merge_score_p > break_score) return 1;               /* 删除太多 → 拆 */

delta_size = src_removed + literal_added;                 /* 删+增 的总改动 */
if (delta_size * MAX_SCORE / max_size < break_score) return 0; /* 改动不够大 → 不拆 */
```

其中 `src_copied`/`literal_added` 由 `diffcore_count_changes`（见 4.3 节的 delta 算法）给出，表示两文件的内容重叠量与纯新增量。

**拆分动作** `diffcore_break`（[diffcore-break.c:L131-L237](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-break.c#L131-L237)）：对命中的修改对，造一个「空 one」和「空 two」，分别组成删除半和新增半，标记 `broken_pair = 1`，并把合并用分数存进 `dp->score`（若该分数低于 merge 阈值则置 0，表示「拆了也随时可以合回」）。

**合并动作** `diffcore_merge_broken`（[diffcore-break.c:L274-L314](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-break.c#L274-L314)）：双重循环，找「同路径的删除半 + 新增半」配对，调 `merge_broken`（[diffcore-break.c:L239-L272](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-break.c#L239-L272)）合成一条 `(d->one, c->two)` 的修改对，并对 `d->one->rename_used++` 标记「这条源还在树里」。

#### 4.2.4 代码实践

**目标**：构造一次「彻底重写」，观察 `-B` 如何改变输出。

1. 在 4.1 的仓库里继续：
   ```bash
   git add -A && git commit -m 'rename a->b'   # 让工作区干净
   printf 'line%d\n' {1..200} > b.txt          # 写入 200 行
   git add b.txt && git commit -m 'fill 200 lines'
   ```
2. 用 sed 把内容几乎全部替换为完全不同的内容（彻底重写）：
   ```bash
   printf 'COMPLETELY DIFFERENT CONTENT %d\n' {1..200} > b.txt
   ```
3. 不加 `-B`：
   ```bash
   git diff --raw
   ```
   **预期**：一条 `M\tb.txt`。
4. 加 `-B`：
   ```bash
   git diff --raw -B
   ```
   **预期**：仍可能是一条 `M`，也可能显示为拆分/合并。可用 `git diff -B50%` 强制把拆分阈值降到 50%，更易触发。观察是否出现 `D`+`A` 的拆分形态。

> 待本地验证：触发 break 与否取决于改动量是否越过阈值（`MINIMUM_BREAK_SIZE=400` 字节也是门槛）。请确保 `b.txt` 足够大且改动足够彻底。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `should_break` 里对很小的文件（`max_size < 400`）直接返回 0？

**参考答案**：小文件的拆分几乎没有收益——重命名检测对小文件本就便宜，且拆分会产生两条记录增加后续负担。400 字节是一个经验门槛，避免对微小改动做无谓拆分。

**练习 2**：`broken_pair` 标志在 break 和 merge 两个阶段分别起什么作用？

**参考答案**：break 阶段用它标记「这条 filepair 是从某条修改拆出来的半边」；merge 阶段则只对带 `broken_pair` 标志的半边去寻找它的「另一半」尝试合并。没带该标志的普通 filepair 永远不参与合并。

---

### 4.3 diffcore rename 检测：精确 + 模糊匹配（`-M`/`-C`）

#### 4.3.1 概念说明

重命名检测是 diffcore 最精巧的部分。它的输入是队列里两类 filepair：

- **删除/修改侧（rename src）**：旧侧 `one` 有效（文件原本存在）。
- **新增侧（rename dst）**：旧侧 `one` 无效但新侧 `two` 有效（文件是新出现的）。

目标是把「一个被删（或被改）的旧文件」与「一个新增的文件」配成对，判定为重命名（`R`）或复制（`C`）。两者的区别：重命名后旧文件消失，复制后旧文件仍在。源码用 `one->rename_used` 计数来区分——一个源被用了一次是重命名，被用了多次就成了复制（详见 4.4 节）。

检测分三条递进的路径，**从便宜到昂贵**：

1. **精确匹配（exact）**：内容完全相同的文件（OID 相等）。因为内容寻址，OID 相等即内容相等，这是 O(1) 哈希查找。
2. **基名匹配（basename）**：文件名（不含目录）相同的候选优先用相似度判定，大幅缩小比较范围。
3. **模糊匹配（inexact）**：对剩余候选两两计算内容相似度，超过阈值才算重命名。这是最贵的 O(N×M) 步骤。

`-M` 开重命名检测，`-C` 开复制检测（复制会把未修改的文件也纳入候选源），可带百分比参数如 `-M50%`。

#### 4.3.2 核心流程

```
diffcore_rename_extended:
  1. 分类：扫队列 → 填 rename_dst[]（新增侧）与 rename_src[]（删除/修改侧）
  2. find_exact_renames()     ← 按 OID 哈希，找内容完全相同的对（最便宜）
  3. find_basename_matches()  ← 按「文件名相同」找候选，相似度判定（中等）
  4. 剩余候选 → 双重循环 estimate_similarity()，每个 dst 只留最好的 4 个 src
     → STABLE_QSORT 按分数降序 → find_renames() 贪心配对（最贵）
  5. 把配对结果写回队列：dst 的 one 改指向 src 的 one，原删除记录被丢弃
```

**相似度分数**（0 到 `MAX_SCORE=60000`）的定义是「目标文件中有多少比例的内容来自源」：

\[
\text{score} = \frac{\text{src\_copied}}{\max(\text{src.size},\ \text{dst.size})} \times \text{MAX\_SCORE}
\]

只有 `score >= minimum_score`（默认 `DEFAULT_RENAME_SCORE=30000`，即 50%）才算匹配成功。注意它衡量的是「重叠内容的比例」，不是简单的「相同行数百分比」。

**性能护栏**：为了避免在超大改动上跑 O(N×M)，有 `rename_limit`（默认 1000）。若候选数 `num_destinations × num_sources > rename_limit²`，要么放弃模糊匹配（`-M`），要么降级（`-C -C` 退化成 `-C`），并设置 `needed_rename_limit` 提示用户「这次没跑全」。

#### 4.3.3 源码精读

**主流程** `diffcore_rename_extended`（[diffcore-rename.c:L1380-L1719](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-rename.c#L1380-L1719)）：注释里的 `trace2_region_enter/leave` 把各阶段切成了可测量的 region（setup / exact renames / basename matches / inexact renames / write back to queue），正好对应上面三条路径。分类循环（L1418-L1459）按 `one`/`two` 是否有效，把 filepair 分进 `rename_dst`（新增）或 `rename_src`（删除/修改）。

**精确匹配** `find_exact_renames`（[diffcore-rename.c:L347-L370](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-rename.c#L347-L370)）：把所有源按 OID 哈希进一个 hashmap，再对每个目标查相同哈希桶（`find_identical_files`，[diffcore-rename.c:L276-L324](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-rename.c#L276-L324)）。命中即 `record_rename_pair(... MAX_SCORE)`（满分）。`hash_filespec`（[diffcore-rename.c:L264-L274](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-rename.c#L264-L274)）对工作树文件（无 OID）临时算一个 blob 哈希，让工作树新增也能参与精确匹配。

**模糊相似度** `estimate_similarity`（[diffcore-rename.c:L132-L214](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-rename.c#L132-L214)）：先用 size 做廉价预筛——若尺寸差太大，连 delta 都不用算直接返回 0（L191 的不等式）；通过预筛才调 `diffcore_count_changes`。最终分数即上面公式。`diffcore_count_changes` 的实现在 `diffcore-delta.c`：把文件切成「以 LF 或 64 字节为界的块」，对每块哈希并计数，再比对两侧的计数直方图近似得出 `src_copied`（被复制的字节数）与 `literal_added`（纯新增字节数）。这是一种**近似**算法，故意牺牲精度换速度。

**候选裁剪**：双重循环（L1583-L1622）对每个目标遍历所有源算相似度，但用 `record_if_better`（[diffcore-rename.c:L1065-L1078](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-rename.c#L1065-L1078)）只保留每个目标最好的 `NUM_CANDIDATE_PER_DST=4`（[diffcore-rename.c:L1064](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-rename.c#L1064)）个源，避免成本矩阵爆炸。

**贪心配对**：`STABLE_QSORT` 按 `score_compare`（[diffcore-rename.c:L242-L256](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-rename.c#L242-L256)，分数高优先、同分看文件名是否一致）排序，再由 `find_renames`（[diffcore-rename.c:L1130-L1157](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-rename.c#L1130-L1157)）从高到低贪心取——分数低于阈值就 `break`，源已被占用（`rename_used` 非零且非复制模式）就跳过。这样保证把最相似的对优先配出。

**护栏** `too_many_rename_candidates`（[diffcore-rename.c:L1086-L1128](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-rename.c#L1086-L1128)）：判定 `num_destinations × num_sources` 是否超过 `rename_limit²`，返回 0（正常）、1（太多，放弃模糊检测）、2（可降级为 `-C`）。

#### 4.3.4 代码实践

**目标**：亲手验证「精确重命名」与「模糊重命名」的判定，并观察 `-M` 阈值的影响。

1. 延续 4.2 的仓库，先提交使工作区干净：
   ```bash
   git add -A && git commit -m 'rewrite'
   ```
2. **精确重命名**（内容完全不变）：
   ```bash
   git mv b.txt c.txt
   git diff --raw -M
   ```
   **预期**：`R100\tb.txt\tc.txt`——100% 相似（精确匹配命中，分数满分）。
3. **模糊重命名**（改一点点内容）：
   ```bash
   git mv c.txt d.txt
   printf '\nextra line\n' >> d.txt      # 内容有少量变化
   git diff --raw -M
   ```
   **预期**：形如 `R093\td.txt` 之类——相似度略低于 100，但仍超过 50% 阈值，判定为重命名。
4. **改阈值**：把 `d.txt` 改动加大到相似度跌破 50%，再用 `-M50%` 与 `-M90%` 对比：
   ```bash
   git diff --raw -M90%   # 阈值 90%，改动较大时不再判为重命名 → 变成 D + A
   ```
   **预期**：高阈值下原本的重命名退化为「删除 + 新增」两条记录。

> 对照阅读：跑第 2 步时，对照 [diffcore-rename.c:L276-L324](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diffcore-rename.c#L276-L324) 理解——因为 `b.txt` 和 `c.txt` 内容相同、OID 相等，`find_identical_files` 在 hashmap 里直接命中，记录满分 `MAX_SCORE` 的重命名。

#### 4.3.5 小练习与答案

**练习 1**：为什么精确匹配要在模糊匹配之前先跑？

**参考答案**：精确匹配按 OID 哈希，是 O(N) 的廉价操作；它能把大量「内容完全相同只是改了名」的对先消除掉，显著缩小模糊匹配阶段（O(N×M)、最贵）的候选规模。这是典型的「用廉价测试尽早剪枝」策略。

**练习 2**：`estimate_similarity` 在调用昂贵的 `diffcore_count_changes` 之前，先做了一次什么廉价判断？为什么？

**参考答案**：先比 size——若 `max_size × (MAX_SCORE − minimum_score) < delta_size × MAX_SCORE`（即尺寸差距大到不可能达到相似度阈值），直接返回 0。因为 size 是 O(1) 信息，能在不读文件内容的情况下排除绝大多数明显不匹配的候选。

**练习 3**：`-M` 与 `-C` 在源选择上的关键差别是什么？

**参考答案**：`-M`（重命名）只把「被删除/被修改」的文件当源；`-C`（复制）会把「未修改的文件」也当源（源码中 `want_copies` 分支 `register_rename_src`），这样即使原文件还在，也能检测出「复制了一份」。代价是候选源变多、更慢。

---

### 4.4 diff 输出格式：状态码、分数与 raw/patch

#### 4.4.1 概念说明

管线跑完后，`diff_flush()` 负责把队列里的 filepair 按 `output_format` 指定的格式打印出来。常见的格式位（[diff.h:L100-L118](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.h#L100-L118)）有 `DIFF_FORMAT_RAW`（plumbing 原始格式）、`DIFF_FORMAT_PATCH`（`-p` 补丁）、`DIFF_FORMAT_NAME_STATUS`（`--name-status`）、`DIFF_FORMAT_DIFFSTAT`（`--stat`）等。

每个 filepair 都有一个**状态码**（[diff.h:L674-L681](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.h#L674-L681)）：

| 码 | 含义 |
|----|------|
| `A` | Added（新增） |
| `D` | Deleted（删除） |
| `M` | Modified（修改） |
| `R` | Renamed（重命名，带相似度） |
| `C` | Copied（复制，带相似度） |
| `T` | Type changed（类型变化，如普通文件↔符号链接） |
| `U` | Unmerged（合并冲突未解决） |

状态码由 `diff_resolve_rename_copy()` 在管线末尾统一赋值——这是「filepair 形态 → 状态码」的翻译层。

#### 4.4.2 核心流程

`diff_resolve_rename_copy` 对每个 filepair 按优先级判定状态：

```
if 未合并             → U
else if 无 one        → A        （新增）
else if 无 two        → D        （删除）
else if 类型变化      → T
else if 是重命名对:
    if 两侧路径相同   → M        （重连后又同名，实为修改）
    else if 源被多次用 → C        （复制：源还在被别处使用）
    else              → R        （重命名）
else if oid/mode 变了 → M        （普通修改）
else                  → 报错（不该有未变对）
```

`R`/`C` 还会带上**相似度分数**：内部存为 0–60000（`score`），显示时由 `similarity_index()` 换算成百分比（0–100），所以你看到 `R100`（完全相同）、`R075`（75% 相似）。

#### 4.4.3 源码精读

**状态码赋值** `diff_resolve_rename_copy`（[diff.c:L6662-L6720](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c#L6662-L6720)）：注释里的判定顺序与上面流程一致。注意 `R` 与 `C` 的区分——`--p->one->rename_used > 0`（[diff.c:L6699-L6702](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c#L6699-L6702)）：把源的 `rename_used` 减 1，若仍大于 0 说明这个源还被别的对引用着，于是当前对是「复制」`C`，否则是「重命名」`R`。

**分数显示** `similarity_index`（[diff.c:L4837-L4840](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c#L4837-L4840)）：`return p->score * 100 / MAX_SCORE;`——把内部 0–60000 映射成 0–100 的整数百分比。

**raw 输出** `diff_flush_raw`（[diff.c:L6469-L6503](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c#L6469-L6503)）：先打 `:mode mode oid oid`，再打状态码；若 `p->score` 非零，状态码后跟三位分数（`%c%03d`），如 `R100`；若是 `R`/`C`，打两个路径（旧→新），否则打一个。

**总输出** `diff_flush`（[diff.c:L7186-L7290](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c#L7186-L7290)）：按固定优先级 raw → stat → summary → patch 依次输出，各格式用 `output_format` 位掩码开关。patch 分支调 `diff_flush_patch_all_file_pairs`（[diff.c:L7099](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c#L7099)），后者对每个 filepair 调 `diff_flush_patch` → `run_diff` → `builtin_diff`，最终由 xdiff 生成 `@@` 补丁块（行级，下一讲）。函数末尾 `diff_queue_clear` 释放整个队列。

#### 4.4.4 代码实践

**目标**：把四种常见格式都跑一遍，看清状态码与分数在输出中的位置。

1. 制造一组覆盖多种状态的改动：
   ```bash
   git add -A && git commit -m clean
   echo hi > added.txt && git add added.txt          # 新增
   git rm d.txt 2>/dev/null || rm -f d.txt            # 删除
   echo change >> e.txt 2>/dev/null || printf 'x\n' > e.txt && git add e.txt  # 修改
   git mv e.txt f.txt 2>/dev/null || true             # 改名（可能失败，忽略）
   ```
2. **name-status**（最简洁，只看状态码）：
   ```bash
   git diff --cached --name-status -M
   ```
   **预期**：每行一个状态码 + 路径，重命名会显示 `R100\told\tnew`。
3. **raw**（plumbing 完整格式，含 oid 与 mode）：
   ```bash
   git diff --cached --raw -M
   ```
   **预期**：`:100644 100644 <oid> <oid> M\tpath`、`A`、`D`、`R100\told\tnew` 等混合。
4. **stat 与 patch**：
   ```bash
   git diff --cached --stat -M
   git diff --cached -M           # 默认 patch 格式
   ```
   **预期**：`--stat` 给出每个文件的增删行数汇总；patch 给出带 `@@ ... @@` 的逐行补丁（这部分由 xdiff 生成）。

> 对照阅读：第 2、3 步看到的 `R100`，正是 [diff.c:L6482](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c#L6482) 的 `"%c%03d%c"` 把状态码 `R` 与 `similarity_index()` 的 100 拼出来的。

#### 4.4.5 小练习与答案

**练习 1**：一个 filepair 被判为 `C`（复制）而非 `R`（重命名）的充要条件是什么？

**参考答案**：它必须是重命名检测配出的对（`DIFF_PAIR_RENAME` 为真），且其源 `one` 的 `rename_used` 计数在被本次配对递减后仍大于 0——即这个源文件还被至少一个别的对引用着，说明它没有「消失」，所以是复制而非重命名。

**练习 2**：为什么 `diff_resolve_rename_copy` 里会出现「两侧路径相同的重命名对被判为 `M`」？

**参考答案**：break/merge 机制可能把一个被拆开的对重新连回原路径；此时虽然带 `renamed_pair` 标志，但新旧路径相同，语义上只是「原地修改」，所以翻译成 `M` 而非 `R`。

---

## 5. 综合实践

**任务**：用一次精心构造的提交，把本讲的四个模块全部串起来，并用 plumbing 命令从输出反推 diffcore 内部发生了什么。

**准备**：
```bash
git init diff-final && cd diff-final
printf 'original content line %d\n' {1..50} > keep.txt
git add keep.txt && git commit -m init
```

**构造四类改动**：
```bash
# (a) 精确重命名：内容不变只改名
cp keep.txt renamed.txt && git add renamed.txt && git rm keep.txt

# (b) 模糊重命名：复制后稍作修改
cp renamed.txt copied.txt && printf '\n# appended\n' >> copied.txt && git add copied.txt

# (c) 彻底重写：内容几乎全换
printf 'totally new %d\n' {1..50} > rewritten.txt && git add rewritten.txt
# （用一个原本存在的大文件去重写它，便于触发 -B）
```

**观察与反推**：
1. 跑 `git diff --cached --raw -M -B`，记录每个文件的状态码与分数。
2. 对每个状态码，回答：它是经管线的哪个阶段产生的？
   - `R100` → 精确匹配（`find_exact_renames`）；
   - `R0xx` → 模糊匹配（`estimate_similarity` + `find_renames`）；
   - `C0xx` → 复制（`-C` 或源被多次引用）；
   - 因 `-B` 拆分/合并产生的 `D`/`A`/`M` → `diffcore_break`/`diffcore_merge_broken`。
3. 把 `renamed.txt` 与 `copied.txt` 视为同一源 `keep.txt` 的两个目标：验证源 `rename_used` 计数如何让其中一个变成 `R`、另一个变成 `C`（可加 `-C` 选项再跑一次对比）。
4. 最后用 `git diff --cached -M -B --stat` 与 patch 格式分别查看，体会「文件级状态由 diffcore 决定、行级补丁由 xdiff 生成」的分层关系。

> 待本地验证：实际能触发哪些状态取决于文件大小是否越过 `MINIMUM_BREAK_SIZE`、相似度是否越过阈值。若现象不符，请增大文件或调整 `-M`/`-B` 的百分比参数。

## 6. 本讲小结

- diff 分两层：**diffcore（文件级，本讲）** 决定「哪些文件配对、状态是什么」；**xdiff（行级，下一讲）** 在已配好的对上生成 `@@` 补丁。
- 核心数据模型是三件套：`diff_filespec`（单文件）→ `diff_filepair`（新旧一对）→ `diff_queue_struct`（队列）。所有阶段都读写同一个全局队列 `diff_queued_diff`。
- `diffcore_std` 是一条固定顺序的流水线：skip-stat-unmatch → break → rename → merge-broken → pickaxe → order → rotate → resolve-rename-copy → apply-filter。
- `-B`（break/merge）用「拆分标准（删+增）」与「合并标准（仅删）」两套阈值，把彻底重写先拆后合，给重命名检测腾出配对空间。
- `-M`/`-C`（rename）按从便宜到贵的顺序走三步：精确匹配（OID 哈希）→ 基名匹配 → 模糊匹配（delta 相似度），并用 `rename_limit`、`NUM_CANDIDATE_PER_DST=4` 等护栏控制成本。
- 状态码（A/D/M/R/C/T/U）与相似度分数由 `diff_resolve_rename_copy` 统一赋值，再由 `diff_flush` 按 raw/name-status/stat/patch 等格式输出；`R100` 这类分数来自 `similarity_index()` 把内部 0–60000 映射成百分比。

## 7. 下一步学习建议

- **下一讲 u8-l2「xdiff 底层行级差异」**：本讲在 `builtin_diff → xdi_diff_outf` 处止步，下一讲进入 `xdiff/` 目录，讲 Myers 算法如何生成 `@@ ... @@` 补丁块，以及 `--histogram`、`--patience`、`--myers` 等算法选项的差异。
- **横向联系 u9-l1/u9-l3**：`git add`、`git status` 是 diffcore 的「喂料方」——它们比较工作树/索引/HEAD，把发现的差异通过 `diff_addremove`/`diff_change` 倒进队列。学完本讲，回头读 u9 会更顺。
- **进阶阅读**：若对重命名检测的目录级推断（`-X` / 目录重命名）感兴趣，可继续读 `diffcore-rename.c` 中 `dir_rename_info` 相关函数（`find_basename_matches` 之后的目录重命名计数逻辑），那是 merge-ort（u10-l1）也会复用的能力。
