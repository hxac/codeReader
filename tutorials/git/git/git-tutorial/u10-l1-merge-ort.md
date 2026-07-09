# merge-ort 与合并策略

## 1. 本讲目标

本讲是专家层「合并 merge」单元的第一讲，承接 u9（工作树操作与提交历史）与 u8（diff 引擎），把前面学过的「索引、对象、tree 遍历、行级 diff」拼成 git 里最复杂的一件事：**把两条历史合并成一条**。

读完本讲，你应当能够：

1. 画出一次 `git merge` 在源码里从 `builtin/merge.c` 到行级 diff 的完整调用栈，并说清每一层各管什么。
2. 说清 **ort 策略的三阶段管线**（收集合并信息 → 检测重命名 → 逐条目处理），以及为什么 ort 在「交叉合并（criss-cross merge）」时要**递归地先把多个合并基合并成一个**。
3. 解释 **三方文件合并的真值表**（match_mask），知道哪些情况自动干净合并、哪些会触发真正的 `ll_merge`。
4. 掌握 **低层合并驱动 merge-ll.c** 如何调 `xdl_merge` 生成 `<<<<<<<` 冲突标记，以及冲突如何以 stage 1/2/3 写进索引。
5. 理解 `builtin/merge.c` 如何按策略名调度，以及为什么 `recursive` 策略在内部其实就是 `ort`。

---

## 2. 前置知识

本讲默认你已经掌握以下概念（在前置讲义中已建立）：

- **三层快照模型**：工作树 / 索引 / 对象数据库（u4-l1）。合并主要发生在「对象数据库的 tree」与「索引」两个层面。
- **tree 对象与 tree 遍历**：一个 commit 指向一棵 tree，tree 是「路径 → (mode, oid)」的清单（u3-l1）。合并本质上是「把三棵 tree 合并成一棵新 tree」。
- **合并基（merge base）与世代号**：两条历史最近的公共祖先，靠 `paint_down_to_common` 的「涂色」算法求出（u7-l2）。
- **行级 diff（xdiff）**：Myers 算法在两段文本间找最长公共子序列（u8-l2）。三方合并的「行级」部分就是连续调用 xdiff。
- **索引条目的 stage**：`git ls-files -u` 看到的 stage 0 = 已合并，stage 1/2/3 = base/ours/theirs 三方（u4-l1）。本讲会看到这些 stage 是怎么被写入的。

几个本讲要新引入的术语：

- **三方合并（3-way merge）**：给定「共同祖先 O、我方 A、对方 B」三个版本，自动算出合并结果。当 A、B 对同一处有不同改动时产生冲突。
- **合并策略（merge strategy）**：实现「如何把多条历史合一条」的一套算法，git 内置 `ort`、`recursive`、`octopus`、`resolve`、`ours`、`subtree` 等。`ort` 是默认策略。
- **冲突标记（conflict marker）**：`<<<<<<<`、`=======`、`>>>>>>>`（diff3 风格还多一个 `|||||||` 祖先段），由行级合并器写进文件内容里。
- **交叉合并（criss-cross merge）**：两条历史有**不止一个**合并基的情形，需要递归地先把合并基自己合并起来。

---

## 3. 本讲源码地图

本讲涉及的关键文件，按「从上到下」的调用顺序排列：

| 文件 | 行数 | 作用 |
|------|------|------|
| `builtin/merge.c` | 1887 | `git merge` 子命令实现：解析参数、选策略、调用后端、提交结果。**策略调度**就在这里。 |
| `merge-ort-wrappers.c` | 135 | 一层薄胶水：把 `builtin/merge.c` 期望的「返回 clean 标志 + 改索引」接口，适配成 `merge-ort.c` 的「返回 `merge_result` + 不碰工作树」接口。 |
| `merge-ort.c` | 5608 | **ort 合并策略主体**。三棵 tree 怎么合成一棵，重命名怎么处理，冲突怎么分类——全在这里。 |
| `merge-ort.h` | 181 | ort 对外公共 API：`struct merge_options`、`struct merge_result`、`merge_incore_recursive()` 等。 |
| `merge-ll.c` | 470 | **单文件三方合并**（low-level merge）。它接三个 blob，吐出合并后的文本（可能含冲突标记）。 |
| `merge-ll.h` | 114 | 单文件合并的公共 API：`struct ll_merge_options`、`ll_merge()`、`enum ll_merge_result`。 |
| `commit-reach.c` | 1432 | 提供合并基计算（`repo_get_merge_bases`），ort 用它找出两条历史的公共祖先。 |

一句话的调用栈（自顶向下）：

```
git merge <branch>
  └─ cmd_merge()                     [builtin/merge.c]
       └─ try_merge_strategy("ort")  [builtin/merge.c]   ← 策略调度
            └─ merge_ort_recursive()  [merge-ort-wrappers.c]
                 └─ merge_incore_recursive()  [merge-ort.c]
                      └─ merge_ort_internal()           ← 递归合并合并基
                           └─ merge_ort_nonrecursive_internal()  ← 三阶段管线
                                ├─ collect_merge_info()        ← 阶段1
                                ├─ detect_and_process_renames()← 阶段2
                                └─ process_entries()           ← 阶段3
                                     └─ handle_content_merge()
                                          └─ merge_3way()
                                               └─ ll_merge()  [merge-ll.c]
                                                    └─ xdl_merge()  ← 行级 diff
```

下面三个最小模块分别讲这条栈的三个层次：**策略调度**（builtin/merge.c）、**ort 引擎**（merge-ort.c）、**低层合并驱动**（merge-ll.c）。

---

## 4. 核心概念与源码讲解

### 4.1 合并策略调度（builtin/merge.c）

#### 4.1.1 概念说明

`git merge` 命令本身不实现任何合并算法，它只是一个**调度器（dispatcher）**。真正的「怎么合并」被抽象成一个个**策略（strategy）**，每个策略有名字（`ort`、`resolve`、`octopus`……）和一组属性。`git merge` 的工作是：

1. 确定要用哪些策略（默认 `ort`，可用 `-s` 指定，可用 `-s a -s b` 让多个策略竞争）。
2. 把共同祖先（merge base）、HEAD、对方提交交给策略后端。
3. 后端把结果写进索引和工作树，返回一个 clean 标志（0=干净、1=有冲突、2=本策略根本处理不了）。
4. 根据返回值决定：直接提交、停下来让用户解决冲突、还是换一个策略再试。

之所以要做成「多策略可竞争」，是历史遗留：早年 `recursive` 策略处理不好某些场景，git 会用多策略轮询，挑「留下冲突最少」的那个。今天默认的 `ort` 已经足够强，单策略就够了，但调度框架保留了下来。

#### 4.1.2 核心流程

策略调度的核心是两张表 + 一个分发函数。

**策略表 `all_strategy[]`** 是一个静态数组，每项是「策略名 + 属性位」。属性位用四个宏：

```
DEFAULT_TWOHEAD (1<<0)  — 适合「两个头」(普通两路合并) 的默认策略
DEFAULT_OCTOPUS (1<<1)  — 适合「多个头」(章鱼合并) 的默认策略
NO_FAST_FORWARD (1<<2)  — 该策略永远不产生快进
NO_TRIVIAL      (1<<3)  — 该策略不能做「平凡合并」(trivial, 即纯快进) 的捷径
```

**分发函数 `try_merge_strategy()`** 按策略名分流：

- 若是 `recursive` / `subtree` / `ort`：走内置 ort 后端，调 `merge_ort_recursive()`。
- 否则（`octopus` / `resolve` / `ours` 等）：走 `try_merge_command()`，它会去 PATH 找 `git-merge-<strategy>` 外部脚本（`git-merge-octopus`、`git-merge-resolve` 其实是 shell 脚本）。

**别名映射**：`get_strategy()` 里有一个小技巧——当用户写 `-s recursive` 而默认策略已是 `ort` 时，悄悄把 `recursive` 改名成 `ort`。也就是说，在当代 git 里 `recursive` 和 `ort` 走的是同一套代码，`recursive` 只是一个向后兼容的名字。

**cmd_merge 的主循环**：如果只指定一个策略，直接调；如果指定多个，则逐个尝试，每次失败先回退工作树到干净状态再试下一个，最后用 `evaluate_result()` 数索引里的冲突条目数，挑最少的那个留下的结果。

#### 4.1.3 源码精读

先看属性宏和策略结构体（属性位用一个 `unsigned` 承载）：

- [builtin/merge.c:L55-L63](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L55-L63)：定义 `DEFAULT_TWOHEAD`/`DEFAULT_OCTOPUS`/`NO_FAST_FORWARD`/`NO_TRIVIAL` 四个属性位，以及 `struct strategy { name; attr; }`。

再看内置策略表——注意 `ort` 带着 `DEFAULT_TWOHEAD | NO_TRIVIAL`（两路合并的默认策略），`octopus` 带 `DEFAULT_OCTOPUS`：

- [builtin/merge.c:L101-L108](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L101-L108)：`all_strategy[]`，六项 `recursive`/`octopus`/`ort`/`resolve`/`ours`/`subtree`，每项带各自的属性位。

分发函数把策略名翻译成实际的合并调用：

- [builtin/merge.c:L800-L801](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L800-L801)：`if (!strcmp(strategy, "recursive") || !strcmp(strategy, "subtree") || !strcmp(strategy, "ort"))`——这三种都走内置 ort 后端。
- [builtin/merge.c:L833-L834](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L833-L834)：`clean = merge_ort_recursive(&o, head, remoteheads->item, reversed, &result);`——把 HEAD、对方提交、倒序的合并基列表交给 ort，拿回 clean 标志。
- [builtin/merge.c:L846-L850](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L846-L850)：`else` 分支调 `try_merge_command()`，即把 `octopus`/`resolve`/`ours` 等交给外部 `git-merge-*` 脚本。

别名映射的小技巧（当代 git 把 `recursive` 等同于 `ort`）：

- [builtin/merge.c:L182-L184](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L182-L184)：当默认策略是 `ort` 而用户要 `recursive` 时，`name = "ort";` 直接改名。

最后是 cmd_merge 的多策略竞争主循环：

- [builtin/merge.c:L1781-L1820](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L1781-L1820)：逐个策略尝试，`ret < 2` 表示该策略「处理了」（0 干净、1 有冲突），其中 `ret == 0` 立即成功跳出；多个策略时用 `evaluate_result()`（数索引冲突条目）挑最优。

ort 之上的薄胶水层把「返回 clean 标志 + 自动写索引」对接到 ort「返回 `merge_result` + 不碰磁盘」的设计：

- [merge-ort-wrappers.c:L54-L74](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort-wrappers.c#L54-L74)：`merge_ort_recursive()` 先用 `unclean()` 检查索引是否干净，再调 `merge_incore_recursive()` 算出内存里的 `merge_result`，最后 `merge_switch_to_result()` 把结果真正写进工作树与索引。
- [merge-ort-wrappers.c:L15-L28](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort-wrappers.c#L15-L28)：`unclean()`——合并前确认索引与 HEAD 一致，避免覆盖用户未提交的改动（这道闸和 u9-l2 讲的 checkout 保护机制同一思想）。

#### 4.1.4 代码实践

**实践目标**：从命令行观察策略调度，并用 `-s` 强制切换策略，验证 `recursive` 实际走 ort。

**操作步骤**：

```sh
# 1. 建一个玩具仓库
git init merge-dispatch && cd merge-dispatch
printf 'line1\n' > f.txt && git add f.txt && git commit -m base

# 2. 在 main 上改一行
git checkout -b topic 2>/dev/null
printf 'line1-topic\n' > f.txt && git commit -am topic

# 3. 切回 main，对同一行做不同改动，制造冲突
git checkout master 2>/dev/null || git checkout main
printf 'line1-main\n' > f.txt && git commit -am main

# 4a. 默认策略（ort）合并
git merge topic            # 预期冲突
git merge --abort

# 4b. 显式用 ort
git merge -s ort topic
git merge --abort

# 4c. 用 resolve（外部脚本策略）
git merge -s resolve topic
git merge --abort
```

**需要观察的现象**：

- 4a、4b 都报冲突，且 `git config merge.conflictstyle` 决定标记样式。
- 用 `GIT_TRACE2_PERF=1 git merge -s ort topic`（合并前先 abort 干净）能在 trace 输出里看到 `merge` 的各个 region（`collect_merge_info`、`renames`、`process_entries`），印证三阶段管线。

**预期结果**：默认 `git merge` 与 `-s ort` 行为一致；`-s recursive` 也一致（因为是别名）。**待本地验证** `resolve` 策略在该冲突下给出的冲突区段是否与 ort 完全相同。

#### 4.1.5 小练习与答案

**练习 1**：`all_strategy[]` 里 `ours` 策略带 `NO_FAST_FORWARD | NO_TRIVIAL`，这两个属性分别意味着什么？

**参考答案**：`NO_FAST_FORWARD` 表示该策略永远不会产生快进合并（`ours` 本就是「丢弃对方、只保留我方」，与快进语义无关）；`NO_TRIVIAL` 表示它不能走「平凡合并」捷径（即对方是我方祖先时直接移动指针那种快路径），因为 `ours` 即使在能快进时也要丢弃对方内容。

**练习 2**：为什么 `try_merge_strategy()` 只把 `recursive`/`subtree`/`ort` 走内置后端，而 `octopus`/`resolve` 走 `try_merge_command()`？

**参考答案**：因为 `octopus`（多路合并）和 `resolve`（古老的两路策略）在 git 源码里实现为 `git-merge-octopus`、`git-merge-resolve` 这些 shell 脚本，不在 C 主二进制里；`try_merge_command()` 去 PATH 找并执行它们。而 ort/recursive 是性能关键的 C 实现，直接在进程内调用。

---

### 4.2 ort 递归合并核心（merge-ort.c）

#### 4.2.1 概念说明

ort（**O**stensibly **R**ecursive's **T**win，意为「recursive 的孪生」）是 git 2.34 起的默认合并策略，用来取代更老的 `recursive`。两者算法思想一致，但 ort 重写了实现，主要改进是：

- **在内存里合并（in-core）**：recursive 边合并边反复读写索引和工作树，ort 全程只在内存的 tree 上操作，最后一次性 `merge_switch_to_result()` 落盘，故快得多。
- **数据结构更好**：用 `strmap` 按「路径 → 合并信息」组织，避免反复遍历。
- **可重用重命名结果**：连续合并（如 rebase 一串提交）时，前一次的重命名检测可以缓存给后一次用。

**为什么要「递归」？** 关键在交叉合并（criss-cross）。看这个历史：

```
       C
      / \
     D   E
      \ /
       F      ← 要合并 D 和 E
```

D 和 E 有**两个**共同祖先 C 和 F'（假设 F 是更早的合并提交）。于是合并基不唯一。如果直接拿其中一个当祖先，结果可能偏向某一方（`recursive` 早期因此有 bug）。解决办法是**递归地先把多个合并基自己合并成一个「虚拟祖先」**，再用它当唯一祖先去做最终合并。这就是 `merge_ort_internal()` 里那段递归循环的来历。

#### 4.2.2 核心流程

ort 的对外入口有两个，区别只在「要不要递归合并合并基」：

- `merge_incore_nonrecursive()`：调用方**自己**已经算好唯一的合并基（一棵 tree），直接三路合并。用于「我知道只有一个祖先」的场景。
- `merge_incore_recursive()`：调用方给的是**提交列表**，ort 自己去算合并基、必要时递归合并它们。`git merge` 走的是这个。

`merge_incore_recursive()` → `merge_ort_internal()` 的核心逻辑分两段：

**第一段：递归塌缩合并基**。如果有多个合并基，两两取出来，用 `merge_ort_internal()` 自己（递归！）合并成一个虚拟提交，循环直到只剩一个。最后把这个虚拟祖先当成唯一合并基。

**第二段：三阶段管线**（`merge_ort_nonrecursive_internal()`）：

1. **`collect_merge_info`**：用 `traverse_trees()` 同时并行遍历「合并基、side1、side2」三棵 tree，对每个路径收集三方的 mode/oid，存进 `opt->priv->paths` 这张 map。回调里用一张「真值表」判定该路径能否平凡解决。
2. **`detect_and_process_renames`**：处理重命名（`-X` 选项、目录重命名等），把「对方把 a.txt 改名成 b.txt」这种跨路径变更对齐回同一逻辑文件。本讲只点到为止，细节属重命名检测。
3. **`process_entries`**：逐条目做最终决策——能干净合并的写进结果 tree，有冲突的记进 `opt->priv->conflicted` 并生成带冲突标记的文件内容。遍历顺序是「先子后父」，这样能先写出子树、再组装父树（u4-l2 的 cache-tree 思想）。

最后 `process_entries` 产出结果 tree 的 oid，存进 `result->tree`；冲突则由后续 `merge_switch_to_result()` 写进索引的 stage 1/2/3（见 4.3）。

#### 4.2.3 源码精读

先看内存里的核心数据结构——「每个路径的合并信息」分两种：能干净合并的用小的 `merged_info`，有冲突的用大的 `conflict_info`（把 `merged_info` 放在首成员，实现 C 风格继承，u3-l1 讲过这套手法）：

- [merge-ort.c:L441-L470](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L441-L470)：`struct merged_info`——结果版本 `result`、`is_null`、`clean` 标志、以及目录名等性能字段。注释明确「检查 `clean` 之后才能安全读 `conflict_info` 的其余字段」。
- [merge-ort.c:L472-L486](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L472-L486)：`struct conflict_info`——`merged` 基类之外，还存三方的 `stages[3]`（mode+oid）、`pathnames[3]`（重命名后可能不同）、`df_conflict`（目录/文件冲突）等。

合并的三方用枚举编号，后面写索引 stage 时会用到（stage = 编号+1）：

- [merge-ort.c:L76-L78](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L76-L78)：`MERGE_BASE = 0, MERGE_SIDE1 = 1, MERGE_SIDE2 = 2`。

再看两个对外入口的内部实现。递归版会先算合并基：

- [merge-ort.c:L5429-L5451](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L5429-L5451)：`merge_incore_recursive()` 先 `merge_start()` 初始化内部状态，再调 `merge_ort_internal()`。

`merge_ort_internal()` 是「递归塌缩合并基 + 调三阶段管线」的枢纽：

- [merge-ort.c:L5319-L5333](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L5319-L5333)：若调用方没给合并基，用 `repo_get_merge_bases()` 自己算（commit-reach.c 提供，u7-l2 讲过它的涂色算法），并按「从旧到新」倒序。
- [merge-ort.c:L5355-L5387](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L5355-L5387)：**递归核心**——对剩余的每一个合并基 `next`，把它和已合并出的 `prev` 用 `merge_ort_internal(opt, NULL, prev, next, result)` 自己合并（`call_depth++` 标记「现在是虚拟祖先合并」，会让 `ll_merge` 走 `virtual_ancestor` 模式），结果做成一个虚拟提交。循环结束就只剩一个合并基。
- [merge-ort.c:L5390-L5395](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L5390-L5395)：拿塌缩后的唯一合并基、两棵 tree，调 `merge_ort_nonrecursive_internal()` 进入三阶段管线。

三阶段管线本身（注意每阶段都包在 trace2 region 里，方便性能分析）：

- [merge-ort.c:L5260-L5290](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L5260-L5290)：`collect_merge_info` → `detect_and_process_renames` → `process_entries` 三段顺序，每段一对 `trace2_region_enter/leave`；`result->clean` 初值由 `detect_and_process_renames` 给，`process_entries` 可能把它改成 -1（出错）。

阶段一——并行遍历三棵 tree：

- [merge-ort.c:L1738-L1768](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L1738-L1768)：`collect_merge_info()` 把三棵 tree 装进 `tree_desc t[3]`，调 `traverse_trees(NULL, 3, t, &info)`，回调设成 `collect_merge_info_callback`。

回调里那张著名的「三方合并真值表」（match_mask）。三个布尔分别表示 side1/侧2 是否等于合并基、两侧是否相等：

- [merge-ort.c:L1327-L1333](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L1327-L1333)：计算 `match_mask`。位 0=合并基、位 1=side1、位 2=side2；置位的那些「彼此相等」。
- [merge-ort.c:L1349-L1358](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L1349-L1358)：`match_mask == 7`（三方全等）直接判定为已解决，无需进一步合并。

这张真值表整理成表格（A=side1/我方，B=side2/对方，O=合并基）：

| match_mask | 含义 | 处理 | 是否冲突 |
|------------|------|------|----------|
| 7 (111) | O == A == B | 直接用任一方 | 干净 |
| 6 (110) | A == B，O 不同 | 两边做了相同改动，用 A/B | 干净 |
| 5 (101) | O == B，A 不同 | 只有一方（A）改了，用 A | 干净 |
| 3 (011) | O == A，B 不同 | 只有一方（B）改了，用 B | 干净 |
| 0 | 三方都不同 | 真正的内容冲突，交给 `ll_merge` | **冲突** |

> 数学上，这就是经典的三方合并规则：若只有一方相对 O 发生了变化，采用变化方；若两方变化相同，采用任一方；若两方变化不同，冲突。形式化地，设 \( \Delta_X = (X \ne O) \) 表示方 X 改了，则干净合并的条件是 \( \neg(\Delta_A \land \Delta_B \land A \ne B) \)。

阶段三——逐条目决策。`process_entry()` 是「文件级冲突判定」的总控，开头就断言 match_mask 只可能是 {0,3,5,6}（7 已在阶段一平凡解决）：

- [merge-ort.c:L4079-L4098](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L4079-L4098)：`process_entry()` 入口，处理目录/文件冲突（dirmask）等。`assert(ci->match_mask == 0 || ... == 6)` 印证了上面的真值表。
- [merge-ort.c:L4498-L4577](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L4498-L4577)：`process_entries()` 把所有路径排成「子在前、父在后」的顺序，逆序遍历；`mi->clean` 为真就 `record_entry_for_tree` 写进结果 tree，否则调 `process_entry` 处理冲突。

冲突的类型用一个大枚举区分（merge-ort.c:L530-L586），每类有固定文案，外部工具依赖这些字符串，故不可改动：

- [merge-ort.c:L530-L586](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L530-L586)：`enum conflict_and_info_types`，如 `CONFLICT_CONTENTS`（文本冲突）、`CONFLICT_MODIFY_DELETE`、`CONFLICT_RENAME_RENAME` 等。
- [merge-ort.c:L594-L636](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L594-L636)：`type_short_descriptions[]`，把枚举映射成 `"CONFLICT (contents)"` 等固定字符串。

#### 4.2.4 代码实践

**实践目标**：阅读源码理解递归合并基，并在真实仓库里复现交叉合并。

**操作步骤**：

```sh
# 1. 制造交叉历史：两条分支各有自己的合并，形成多个合并基
git init crisscross && cd crisscross
printf '1\n' > f && git add f && git commit -m base       # C0
git branch b1
git branch b2

# 在 b1 上：base -> m1
printf '2\n' > f && git commit -am b1-m1                   # C1

# 在 b2 上：base -> m2
git checkout -q b2
printf '3\n' > f && git commit -am b2-m2                   # C2

# 互相合并产生交叉：b1 合 b2，b2 合 b1（用 ours 避免冲突）
git checkout -q b1 && git merge -s ours b2 -m cross1       # M1
git checkout -q b2 && git merge -s ours b1 -m cross2       # M2

# 2. 现在合并 M1 与 M2，二者有两个共同祖先 C1、C2 —— 触发递归合并基
git checkout -q b1
git merge b2
```

**需要观察的现象**：

- 最后一次 `git merge b2` 时，M1 和 M2 的共同祖先是 C1 和 C2 两个，于是 ort 会先递归把 C1、C2 合并成一个虚拟祖先，再做最终合并。
- 用 `GIT_TRACE2_PERF=1 git merge b2` 可以看到嵌套的 `merge` region（内层那次就是合并基之间的虚拟合并）。

**阅读源码对应**：把上面的现象对照 [merge-ort.c:L5355-L5387](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L5355-L5387) 的递归循环，确认「两个合并基 → 一次内层 `merge_ort_internal` → 一个虚拟祖先」。

**预期结果**：合并正常完成（因为两边内容经 ours 已确定，最终多为干净或可自动解决）。**待本地验证** trace 输出中是否真的出现内层 `incore_recursive` region。

#### 4.2.5 小练习与答案

**练习 1**：`merge_ort_internal()` 在递归合并合并基时，把分支名临时改成了 `"Temporary merge branch 1/2"`（见 L5371-L5372）。为什么？

**参考答案**：因为内层那次合并产生的冲突标记里会带分支名，而这些分支名是给「最终合并」用的。如果直接复用真实分支名，虚拟祖先合并产生的冲突标记会误导用户（看起来像最终冲突）。改成临时占位名，说明这些只是内部中间产物；而且内层合并的 clean 标志会被忽略（注释 L5361-L5368 说明），冲突标记只是被「当成已解决」存进虚拟提交。

**练习 2**：`process_entries` 为什么要按「子目录在前、父目录在后」的顺序遍历（L4548 的逆序遍历）？

**参考答案**：因为要边遍历边**组装结果 tree**：必须先把一个目录下所有文件/子树写好、算出该目录的 tree oid，才能把它登记进父目录。逆序（子在前）保证了处理到父目录时，它所有子节点的 tree 都已就绪。同时这也是处理「目录/文件冲突」所必需的——得先知道某个目录最终是否还挡着同名文件。

---

### 4.3 merge-ll 低层合并驱动（merge-ll.c）

#### 4.3.1 概念说明

当 ort 的真值表判定某文件是「真正的内容冲突」（match_mask == 0），就要对**单个文件的三方内容**做行级合并。这件事交给 `merge-ll.c`（ll = low-level）。

`ll_merge()` 的契约很直接：传入共同祖先 blob（O）、我方 blob（A）、对方 blob（B）三个内存缓冲，以及三方的标签（用于填冲突标记），它返回合并后的文本（存在冲突就把 `<<<<<<<`/`=======`/`>>>>>>>` 直接写进文本里），并用返回码告诉调用方是否干净。

关键设计是「**合并驱动（merge driver）**」可插拔。git 内置三个：

| 驱动 | 行为 |
|------|------|
| `text` | 正常的行级三方合并（调 xdiff），冲突时写标记——**默认**。 |
| `binary` | 二进制文件没法行级合并，直接按 `-Xours/-Xtheirs` 选一边，或报二进制冲突。 |
| `union` | 并集合并，两侧内容都保留、不加冲突标记（如 `git merge-file --union`）。 |

用户还能在 `.gitattributes` 里给特定路径指定**自定义合并驱动**（`*.doc merge=word` 之类），甚至写 `[merge "word"] driver = ...` 调外部程序。`ll_merge()` 第一步就是查 `.gitattributes` 决定用哪个驱动。

冲突标记的长度（`<<<<<<<` 是 7 个字符）由 `conflict-marker-size` 属性控制，默认 7（`DEFAULT_CONFLICT_MARKER_SIZE`，定义在 xdiff/xdiff.h:144）。嵌套冲突时 ort 会通过 `extra_marker_size` 加长标记以区分层次。

#### 4.3.2 核心流程

`ll_merge()` 内部分三步：

1. **查属性**：`load_merge_attributes()` + `git_check_attr()` 读 `.gitattributes`，拿到该路径的 `merge` 属性（驱动名）和 `conflict-marker-size`。
2. **选驱动**：`find_ll_merge_driver()` 把属性值映射到一个 `struct ll_merge_driver *`——`true`→`text`、`false`→`binary`、具体名字→查用户驱动表再查内置三驱动、找不到→默认 `text`。
3. **执行驱动**：调 `driver->fn(...)`。对 `text` 驱动就是 `ll_xdl_merge()`，它把参数装进 `xmparam_t`，最终调 `xdl_merge()`（u8-l2 的行级 diff）。返回码 `>0` 表示有冲突。

ort 侧的入口是 `merge_3way()`：它读三个 blob 进内存、组装 `ll_merge_options`、设好三方标签、调 `ll_merge()`，把返回的缓冲当作「合并后内容」（即便含冲突标记）写进工作树。

冲突一旦产生，ort 在内存里把它登记进 `opt->priv->conflicted`；最后 `merge_switch_to_result()` → `record_conflicted_index_entries()` 把这些路径以 **stage 1/2/3** 写进索引（替换掉 stage 0），并写出带冲突标记的工作树文件。

#### 4.3.3 源码精读

先看公共选项与结果类型：

- [merge-ll.h:L49-L86](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.h#L49-L86)：`struct ll_merge_options`——`virtual_ancestor`（虚拟祖先合并时为 1，影响二进制处理）、`variant`（`XDL_MERGE_FAVOR_OURS/THEIRS/UNION`，对应 `-Xours` 等）、`renormalize`、`extra_marker_size`、`conflict_style`、`xdl_opts`。
- [merge-ll.h:L90-L95](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.h#L90-L95)：`enum ll_merge_result`——`LL_MERGE_OK`（干净）、`LL_MERGE_CONFLICT`（文本冲突）、`LL_MERGE_BINARY_CONFLICT`（二进制冲突）、`LL_MERGE_ERROR`。

驱动本身是一个带函数指针的结构体（典型的 C 虚表，和 u5-l1 的 `ref_storage_be` 同一思路）：

- [merge-ll.c:L32-L39](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L32-L39)：`struct ll_merge_driver { name; description; fn; recursive; next; cmdline; }`——`fn` 是合并函数指针。
- [merge-ll.c:L171-L175](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L171-L175)：内置三驱动表 `ll_merge_drv[]`：`binary`/`text`/`union`。

三个驱动的实现。`text` 驱动先做二进制检测，正常则转给 xdiff：

- [merge-ll.c:L103-L147](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L103-L147)：`ll_xdl_merge()`——任一输入过大或含 NUL（`buffer_is_binary`）就退化成 `ll_binary_merge`；否则装 `xmparam_t`（`level = XDL_MERGE_ZEALOUS`、`favor`、`style`、`marker_size`、ancestor/file1/file2 标签），调 `xdl_merge(orig, src1, src2, &xmp, result)`。`xdl_merge` 返回 `>0` 表示冲突。
- [merge-ll.c:L58-L101](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L58-L101)：`ll_binary_merge()`——二进制不合并，按 `virtual_ancestor`/`variant` 决定取祖先、我方或对方，否则报 `LL_MERGE_BINARY_CONFLICT`。
- [merge-ll.c:L149-L166](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L149-L166)：`ll_union_merge()`——把 `variant` 改成 `XDL_MERGE_FAVOR_UNION` 后转调 `ll_xdl_merge`，两侧内容都保留、不插冲突标记。

公共入口 `ll_merge()`：查属性、选驱动、派发：

- [merge-ll.c:L406-L451](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L406-L451)：`ll_merge()` 主体。L429 `git_check_attr` 查 `merge` 与 `conflict-marker-size` 两个属性；L439 `find_ll_merge_driver` 选驱动；L441-L444 若 `virtual_ancestor` 且该驱动配了 `recursive` 替代驱动则换用；L445-L447 叠加 `extra_marker_size`；L448 调 `driver->fn(...)` 执行。
- [merge-ll.c:L363-L394](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L363-L394)：`find_ll_merge_driver()`——`ATTR_TRUE`→`text`、`ATTR_FALSE`→`binary`、未设但有 `default_ll_merge` 配置则用它、具体名字先查用户驱动链再查内置三驱动、找不到默认 `text`。

ort 侧的调用方 `merge_3way()`：组装选项与标签，调 `ll_merge`：

- [merge-ort.c:L2107-L2163](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L2107-L2163)：`merge_3way()`。L2124-L2127 把 `opt` 的 renormalize/xdl_opts/conflict_style 装进 `ll_opts`；L2129-L2144 若 `call_depth`（虚拟祖先合并）则 `virtual_ancestor=1` 且 `variant=0`，否则按 `recursive_variant` 设 `-Xours/-Xtheirs`；L2147-L2155 生成三方标签（同名时只写分支名，重命名时写 `分支:路径`）；L2157-L2159 把三个 blob 读进 `mmfile_t`；L2161 调 `ll_merge(...)` 拿回含（可能）冲突标记的结果缓冲。

最后是冲突如何落进索引——这是「冲突被记录」的真正含义：

- [merge-ort.c:L4648-L4680](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L4648-L4680)：`record_conflicted_index_entries()` 遍历 `opt->priv->conflicted`，准备把冲突路径的索引条目改成多 stage。
- [merge-ort.c:L4741-L4749](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L4741-L4749)：**关键**——对 base/side1/side2 每个存在的方，`make_cache_entry(index, vi->mode, &vi->oid, path, i+1, 0)` 生成 stage 为 `i+1`（即 1/2/3）的索引条目并 `add_index_entry(..., ADD_CACHE_JUST_APPEND)`。这正好对应 `git ls-files -u` 看到的三行。
- [merge-ort.c:L4757-L4759](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L4757-L4759)：追加完后 `remove_marked_cache_entries` 清掉旧的 stage-0 条目，再排序索引，让 stage 1/2/3 各就各位。

> 冲突的「双重表示」：同一份冲突既在**工作树文件**里（用 `<<<<<<<` 标记表示，方便人读），又在**索引**里（用 stage 1/2/3 三条目表示，方便机器解析）。`git diff` 显示前者，`git ls-files -u` 显示后者，二者描述的是同一个冲突。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手制造一个三方内容冲突，运行合并，然后对照源码说明「冲突区段是如何标记与记录的」。

**操作步骤**：

```sh
git init conflict-demo && cd conflict-demo

# 共同祖先 O：f.txt 内容是 "base"
printf 'base\n' > f.txt && git add f.txt && git commit -m base

# side1（我方 main）：改成 "ours"
git checkout -q -b side2 2>/dev/null; git checkout -q master 2>/dev/null || git checkout -q main
printf 'ours\n' > f.txt && git commit -am ours

# side2（对方）：同一行改成 "theirs"
git checkout -q -b side2 HEAD~1
printf 'theirs\n' > f.txt && git commit -am theirs

# 合并 → 冲突
git checkout -q master 2>/dev/null || git checkout -q main
git merge side2          # 预期：Auto-merging f.txt / CONFLICT (contents)
```

**第一步：观察工作树里的冲突标记**：

```sh
cat f.txt
# <<<<<<< HEAD
# ours
# =======
# theirs
# >>>>>>> side2
```

对照 [merge-ll.c:L103-L147](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L103-L147)：这 7 字符的标记是 `xdl_merge()` 在发现 A、B 对同一行有不同改动时写出的，标记里的 `HEAD`/`side2` 来自 [merge-ort.c:L2147-L2155](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L2147-L2155) 生成的 `name1`/`name2`。

**第二步：换 diff3 风格，看到祖先段**：

```sh
git merge --abort
git -c merge.conflictstyle=diff3 merge side2
cat f.txt
# <<<<<<< HEAD
# ours
# ||||||| merged common ancestors
# base
# =======
# theirs
# >>>>>>> side2
```

对照 [merge-ll.c:L135-L136](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ll.c#L135-L136)：`opts->conflict_style` 来自 `merge.conflictstyle` 配置，透传给 `xmp.style`，决定 xdiff 是否多输出 `|||||||` 祖先段。

**第三步：观察索引里的 stage 1/2/3**：

```sh
git ls-files -u
# 100644 <oid-base>  1  f.txt
# 100644 <oid-ours>  2  f.txt
# 100644 <oid-theirs> 3  f.txt
```

对照 [merge-ort.c:L4741-L4749](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L4741-L4749)：这三行正是 `i+1`（1/2/3）三个 stage 的 `cache_entry`，oid 分别是 base、ours、theirs 三个 blob。

**第四步：验证 oid 来源**：

```sh
git ls-files -u | awk '{print $2}' | while read o; do git cat-file -p "$o" | head -1; done
# base
# ours
# theirs
```

**预期结果**：工作树 `f.txt` 含 7 字符冲突标记；索引 `ls-files -u` 显示同一路径三个 stage（1=base、2=ours、3=theirs），每个 stage 的 oid 反解出来正是三方原始内容。这就完整对应了「冲突在工作树用标记表示、在索引用 stage 表示」的双重记录。

#### 4.3.5 小练习与答案

**练习 1**：把一个**二进制**文件（如 PNG）在两侧都修改后合并，会发生什么？对应 `merge-ll.c` 哪段代码？

**参考答案**：`ll_xdl_merge` 在 L117-L129 检测到输入含 NUL 字节（`buffer_is_binary`）或过大，立即退化成 `ll_binary_merge`。后者（L58-L101）对二进制不做行级合并：默认 `variant` 下返回 `LL_MERGE_BINARY_CONFLICT` 并取我方内容，工作树文件不会被插入冲突标记（二进制插不了文本标记），ort 侧 `merge_3way` 据此发 `CONFLICT (binary)` 警告（见 L2164-L2168）。用户只能手动选边。

**练习 2**：为什么 `ll_merge()` 在 `opts->virtual_ancestor` 为真时，会查找驱动是否配了 `recursive` 替代驱动（L441-L444）？

**参考答案**：`virtual_ancestor` 表示当前这次 `ll_merge` 是在「合并基之间的虚拟合并」里调的（4.2 的递归塌缩），其结果只会当祖先用、不会进最终工作树。某些合并驱动在「产生真冲突」和「产生虚拟祖先」时希望表现不同（例如对二进制，真合并要报冲突让用户看，而虚拟祖先合并应尽量取一方避免污染祖先），故允许在 `[merge "<driver>"] recursive = <另一个驱动>` 里指定虚拟祖先场景下的替代驱动。

---

## 5. 综合实践

把三个最小模块串起来：**追踪一次冲突合并从命令行到索引的完整数据流，并在每个环节对照源码**。

**任务**：在一个仓库里制造「文本冲突 + 重命名」的复合场景，然后回答下面四个问题，每个问题都给出**源码行号依据**。

**准备**：

```sh
git init integration && cd integration
printf 'v1\n' > old.txt && git add old.txt && git commit -m base

# side1：修改 old.txt 内容（不动文件名）
git branch side2
printf 'ours\n' > old.txt && git commit -am edit-on-main

# side2：把 old.txt 改名成 new.txt，并修改内容
git checkout -q side2
git mv old.txt new.txt
printf 'theirs\n' > new.txt && git commit -am rename-and-edit

# 合并
git checkout -q master 2>/dev/null || git checkout -q main
git merge side2
```

**回答（带着源码验证）**：

1. **调度层**：这次合并走了哪个策略？依据 [builtin/merge.c:L800-L801](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L800-L801) 说明为什么 `git merge`（不带 `-s`）最终进了 `merge_ort_recursive`。
2. **管线层**：用 `GIT_TRACE2_PERF=1 git merge side2`（合并前先 reset 干净）找到三个 region（`collect_merge_info`/`renames`/`process_entries`）。重命名是在哪个阶段被处理的？依据 [merge-ort.c:L5277-L5290](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L5277-L5290)。
3. **冲突判定**：本次是否产生冲突？若产生，是哪一类（依据 [merge-ort.c:L594-L606](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L594-L606) 的文案）？为什么重命名+修改通常能自动解决而纯文本冲突不能（回到 4.2 的真值表）？
4. **记录层**：若有冲突，`git ls-files -u` 的输出里 stage 1/2/3 分别对应哪三方？依据 [merge-ort.c:L4741-L4749](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L4741-L4749) 说明 `i+1` 如何变成 1/2/3。

**预期结果**：因为 side2 重命名 + 改内容、side1 改的是旧名 old.txt 的内容，ort 在阶段 2 检测到重命名后，会把两边的修改对齐到同一逻辑文件并尝试三方合并；若两边的行改动不重叠则干净合并、若重叠则报 `CONFLICT (content)`。**待本地验证**你这次具体落到了哪种结果，并用源码行号解释。

---

## 6. 本讲小结

- **合并是一条分层调用栈**：`builtin/merge.c`（调度）→ `merge-ort-wrappers.c`（胶水）→ `merge-ort.c`（策略引擎）→ `merge-ll.c`（单文件三方合并）→ `xdl_merge`（行级 diff）。每层职责单一。
- **策略调度在 `all_strategy[]` 表 + `try_merge_strategy()` 分发**；`recursive` 在当代 git 里是 `ort` 的别名（[builtin/merge.c:L182-L184](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/merge.c#L182-L184)）；`octopus`/`resolve` 走外部 shell 脚本。
- **ort 用递归解决交叉合并的多合并基问题**：`merge_ort_internal()` 把多个合并基两两合并成虚拟祖先（[merge-ort.c:L5355-L5387](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L5355-L5387)），再进入三阶段管线。
- **三阶段管线**：`collect_merge_info`（并行遍历三 tree，用 match_mask 真值表判定平凡解决）→ `detect_and_process_renames` → `process_entries`（写结果 tree、处理冲突）。
- **三方合并真值表**：只有一方改、或两方改得相同，都干净；三方都不同才算冲突（match_mask == 0），交给 `ll_merge`。
- **冲突有双重表示**：工作树文件里用 7 字符的 `<<<<<<<` 标记（由 `xdl_merge` 写，受 `merge.conflictstyle` 控制），索引里用 stage 1/2/3 三条目（[merge-ort.c:L4741-L4749](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/merge-ort.c#L4741-L4749)）。

---

## 7. 下一步学习建议

- **u10-l2（unpack-trees 三方合并与冲突）**：本讲的 ort 在内存 tree 上合并；而 `git merge` 把结果落到工作树时，以及更早的 `recursive` 策略，依赖 `unpack-trees.c` 在 index/worktree 层的三方合并与 `resolve-undo` 撤销记录。两者对照阅读能看清「内存合并」与「磁盘合并」的分工。
- **重命名检测深入**：本讲对阶段 2（`detect_and_process_renames`）只点到为止。若想理解「同名改不同名、目录整体改名」如何影响合并，建议直接读 `merge-ort.c` 的 `detect_and_process_renames()`（L3553 起）及 diffcore 的 rename 检测（u8-l1）。
- **行级 diff 算法**：`ll_xdl_merge` 最终调的 `xdl_merge` 是 Myers/直方图算法的实现，详见 u8-l2 的 `xdiff/xdiffi.c`。
- **协议与协商**：合并产生的对象在 `git fetch`/`git pull` 时如何通过网络协商传输，见 u11 单元。
