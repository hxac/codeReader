# commit 可达性与 commit-graph

## 1. 本讲目标

上一讲（u7-l1）我们拆解了 `revision.c` 的历史遍历引擎，看清了 `git log`/`git rev-list` 如何在提交 DAG 上「弹一个、压回其父亲」地滚动。但那里留下了一个关键悬念——遍历每弹出一个 commit，都要拿到它的 **parents** 和 **date**；传统做法必须把每个 commit 对象从对象库里解压、解析一遍，这在拥有几十万提交的大型仓库里是巨大的 I/O 与 CPU 开销。

本讲回答两个紧密相关的问题：

1. **算法层**：git 如何判定「X 是不是 Y 的祖先」、如何计算两个分支的最近公共祖先（merge-base）？为什么这些操作能比「把整段历史都遍历一遍」快得多？
2. **数据结构层**：`commit-graph` 这个缓存文件到底是什么？它如何让「取一个 commit 的 parent」从「解压一个对象」变成「读一个数组下标」？

学完本讲你应当能够：

- 说清 **generation number（世代号）** 的定义，以及它如何作为「祖先关系的下界」提前剪枝；
- 读懂 `paint_down_to_common` 这套「涂色」算法，理解 PARENT1/PARENT2/STALE/RESULT 四个标志位的分工；
- 描述 `commit-graph` 文件的分块（chunk）布局，尤其是 `CDAT`（commit data）块里每个提交那 `rawsz + 16` 字节的字段排列；
- 解释 `commit-slab` 机制如何在不修改 `struct commit` 的前提下，给每个提交挂上「图内位置 / 世代号」等附加数据。

## 2. 前置知识

本讲承接 u7-l1，默认你已经掌握：

- **提交 DAG**：每个 commit 指向零个或多个 parent，形成有向无环图；「祖先」就是沿 parent 边可达。
- **prio_queue**：u7-l1 讲过的优先队列，`revision.c` 用它按 commit 时间排序；本讲 `commit-reach.c` 复用同一设施，但改用 generation 排序。
- **对象标志位**：u3-l1 讲过 `struct object` 里有 29 位 flags，是按需分配、用完归还的临时资源。本讲会用到 `object.h` 旗表里靠后的一段（`PARENT1`~`ENQUEUED`，第 16~20 位）。
- **generation number 的直觉**：粗略地说，一个提交的世代号 = `1 + max(各 parent 的世代号)`，根提交为 0。它刻画了「距离根有多远」，越靠近现在的提交世代号越大。本讲会把这团直觉严格化。

还有一个新概念先点透：**涂色（paint）**。git 在做祖先关系判定时，喜欢给提交「刷上颜色」——从 A 出发能走到的提交刷上 `PARENT1` 色，从 B 出发能走到的刷上 `PARENT2` 色；同时被刷上两色的提交，就是 A 和 B 的公共祖先。这是本讲反复出现的心智模型。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `commit-reach.c` | 可达性算法库：merge-base、is-ancestor、ahead/behind 等，全部建立在「涂色 + 世代号剪枝」之上。 |
| `commit-reach.h` | 上述算法的公共声明，定义 `enum merge_base_flags`、`struct ahead_behind_count` 等。 |
| `commit-graph.c` | 读取/写入 `commit-graph` 缓存文件；把图内提交的 tree/date/parents 直接填进 `struct commit`。 |
| `commit-graph.h` | `struct commit_graph` 与 `struct commit_graph_data` 定义、写图选项枚举。 |
| `commit-slab.h` / `commit-slab-decl.h` / `commit-slab-impl.h` | 一套宏，用来给每个提交关联一份附加数据（slab），是本讲算法与缓存共用的底层设施。 |
| `commit.c` | `repo_parse_commit_internal`：解析一个提交时的「先查图、后查对象库」回退链。 |
| `commit.h` | 世代号相关的几个常量（`GENERATION_NUMBER_INFINITY` 等）。 |

---

## 4. 核心概念与源码讲解

### 4.1 可达性与 merge-base 算法

#### 4.1.1 概念说明

「可达性」是 git 历史操作的基石。两个最常见的形态：

- **祖先判定**：commit X 是不是 commit Y 的祖先？（`git merge-base --is-ancestor`、推送时的「fast-forward 判定」都靠它。）
- **merge-base（合并基）**：给定两个提交 A、B，找出它们最近的公共祖先。`git merge`、`git rebase`、三方合并都先要算它。

如果朴素地做：要算 A、B 的 merge-base，就得把 A 的所有祖先收集成一个集合，再把 B 的所有祖先收集成另一个集合，求交集。这要把整段历史走两遍，O(历史总量)。

git 的优化有两层：

1. **涂色 + 优先队列**：从 A、B 同时开始、按 generation 从大到小同时向下走，谁先「碰头」（被两边都涂到）谁就是公共祖先；它的 parent 不必再算（标 STALE）。
2. **世代号剪枝**：利用 generation 的单调性，一旦队列里剩下的提交 generation 都低于某个阈值，就可以断定「再往下走不可能再有公共祖先了」，直接停。

这两层合起来，让 merge-base 通常只需要走「两个分支分叉以来」的那一小段历史，而不是整个仓库历史。

#### 4.1.2 核心流程

先严格化世代号。对提交 \(c\)，其生成代数满足：

\[
\text{gen}(c)=1+\max_{p\in\text{parents}(c)}\text{gen}(p),\qquad \text{gen}(\text{根})=0
\]

由此得到一条**单调性引理**（这是本讲全部剪枝的数学依据）：

> 沿任何从根到 \(c\) 的路径，世代号严格递增。因此若 \(a\) 是 \(c\) 的祖先，则 \(\text{gen}(a)<\text{gen}(c)\)；逆否命题——若 \(\text{gen}(a)\ge\text{gen}(c)\)，则 \(a\) 不可能是 \(c\) 的祖先。

merge-base 主流程 `paint_down_to_common` 的伪代码：

```
输入: one, twos[]（已解析）, min_generation（剪枝下界）
把 one 涂 PARENT1，每个 twos[i] 涂 PARENT2，全部入队（按 gen 降序的优先队列）
while 队列里还有 non-stale 提交:
    c = 弹出一个 gen 最大的提交
    if gen(c) < min_generation: break          # 世代号剪枝
    flags = c 的 (PARENT1|PARENT2|STALE) 位
    if flags == PARENT1|PARENT2:                # 两边都能走到 → 公共祖先
        标记 c 为 RESULT；若只求一个 merge-base 就可以提前 return
        flags |= STALE                          # c 的 parent 不必再当候选
    for c 的每个 parent p:
        if (p 已含 flags) 跳过                   # 已经涂过同色，不必重走
        解析 p；给 p 涂上 flags；p 入队
收集所有「RESULT 且非 STALE」的提交，按日期排序返回 → 即 merge-base
```

关键直觉：**只有「刚被两边同时涂到、且其 parent 尚未被染色」的提交才是真正的 merge-base**。一旦某个公共祖先被找到，它的所有祖先也必然是公共祖先，但它们「更老」，不是「最近」的，于是用 STALE 标记排除掉。

#### 4.1.3 源码精读

先看标志位分配。`commit-reach.c` 占用了 `object.h` 旗表的 16~20 位，并显式注明「记得更新 object.h 的分配表」：

[commit-reach.c:16-22](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-reach.c#L16-L22) —— 定义 `PARENT1/PARENT2/STALE/RESULT/ENQUEUED` 五个本模块专用标志位。

排序比较函数把「世代号」作为主键、commit 日期作为次键，这正是优先队列「从大到小」处理的依据：

[commit-reach.c:24-41](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-reach.c#L24-L41) —— `compare_commits_by_gen`：世代号小的排前面。

核心算法 `paint_down_to_common`（只摘关键几行）。注意它用了一个 `nonstale_queue` 包装，提供 O(1) 的「队列里是否还有非 STALE 提交」判定：

[commit-reach.c:100-191](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-reach.c#L100-L191) —— `paint_down_to_common` 主体。其中三处最值得对照阅读：

- 第 129 行 `while (queue.max_nonstale)`：只要还有非 STALE 提交就继续；这是 O(1) 终止判定。
- 第 141~142 行 `if (generation < min_generation) break;`：**世代号剪枝**——队列按 gen 降序弹出，弹到 gen 低于下界时，剩下的不可能再是公共祖先。
- 第 144~160 行：判定 `flags == (PARENT1 | PARENT2)` 即两边都涂到了，标记 RESULT；并给其 parent 的 flags 或上 STALE。

公共入口 `repo_get_merge_bases` 经多层包装进入上面的算法；多于一个候选时还要调用 `remove_redundant` 去掉「能从其它候选走到」的冗余祖先：

[commit-reach.c:552-559](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-reach.c#L552-L559) —— `repo_get_merge_bases`：两个提交的 merge-base 公共入口。

祖先判定 `repo_in_merge_bases_many` 是另一个典型用例：把 `commit` 自己的世代号作为 `min_generation` 传入，于是「走到 gen 低于该提交时就停」——因为这些提交不可能是该提交的祖先：

[commit-reach.c:596-633](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-reach.c#L596-L633) —— `repo_in_merge_bases_many`：注意第 619~621 行先取 `commit` 的世代号，若它比所有 reference 都大，直接返回 0（无需任何遍历）。

> 阅读提示：`remove_redundant` 内部按「是否已启用世代号」分两条实现（`_with_gen` 与 `_no_gen`），见 [commit-reach.c:458-480](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-reach.c#L458-L480)。有世代号时走更高效的版本，没有时退化。这正说明世代号是「能用就用」的加速器，而非正确性前提。

#### 4.1.4 代码实践

**实践目标**：用 `git merge-base` 观察算法，并理解世代号剪枝在何时生效。

**操作步骤**：

1. 找一个有多条分支的仓库（git 自身仓库即可）。
2. 选两个分叉较远的提交，例如 `master` 与某个旧 tag：
   ```sh
   git merge-base master v2.0.0
   git log --oneline $(git merge-base master v2.0.0) | head -1
   ```
3. 用 `--is-ancestor` 做祖先判定，验证单调性直觉：
   ```sh
   git merge-base --is-ancestor v2.0.0 master && echo "v2.0.0 是 master 的祖先"
   ```
4. （可选）打开 trace 观察一次 merge-base 实际触发了多少对象解析：
   ```sh
   GIT_TRACE2_PERF=1 git merge-base --all master v2.0.0 2>&1 | tail -20
   ```

**预期现象**：步骤 2 会打印一个 commit 的 OID，即两个分支的最近公共祖先。步骤 3 退出码为 0 时打印「是祖先」。

**待本地验证**：步骤 4 的 trace 行数与耗时取决于仓库规模与是否已生成 commit-graph，具体数值需在本机观察。

#### 4.1.5 小练习与答案

**练习 1**：`paint_down_to_common` 里，为什么找到一个 RESULT（公共祖先）后，要立刻把它的 parent 标记为 STALE？

> **答案**：因为 RESULT 的所有祖先必然也是两边的公共祖先，但它们 generation 更小（更老），不可能是「最近」公共祖先。标 STALE 让它们不再被当作 RESULT 候选，从而保证返回的是**最小**公共祖先集合。

**练习 2**：若仓库**没有** commit-graph、所有提交的 generation 都等于 `GENERATION_NUMBER_INFINITY`，`paint_down_to_common` 还能给出正确结果吗？

> **答案**：能。世代号只影响「剪枝早停」的性能，不影响正确性。注意第 114~115 行：当 `min_generation == 0` 且未启用 corrected commit date 时，比较函数退化为按 commit 日期排序（`compare_commits_by_commit_date`），算法照常运行，只是剪枝能力变弱。

**练习 3**：`repo_in_merge_bases_many` 第 619~621 行，当 `commit` 的世代号大于所有 reference 时直接 `return ret`（0），连一次遍历都不做。这依据的是哪条数学性质？

> **答案**：单调性引理的逆否命题——`gen(commit) >= gen(reference)` 意味着 `commit` 不可能是 `reference` 的祖先，于是无需遍历即可判定为「不是祖先」。

---

### 4.2 commit-graph 文件结构

#### 4.2.1 概念说明

`commit-graph` 是一个**只读缓存文件**（通常在 `.git/objects/info/commit-graph`），把仓库里**所有 commit** 的元数据预先提取、紧凑排列，并预先算好每个 commit 的世代号。它解决两个痛点：

1. **取 parent 太贵**：没有它，要拿一个 commit 的 parent，得把这个 commit 对象从松散文件或 pack 里解压、扫描它的文本头部。遍历十万个提交就要解压十万次。
2. **没有世代号**：纯 commit 对象里根本没有世代号字段，每次 merge-base 都得临时算。

有了 commit-graph，取 parent 变成「按数组下标读 4 字节」，世代号也是现成的常数时间读取。代价是要维护这个缓存（`git commit-graph write`），并接受它可能**过期**（新增 commit 后未重写时，新提交不在图里，此时回退到传统解析）。

> 重要心法：commit-graph 是**加速器**，不是**真相之源**。对象库里的 commit 对象才是真相。任何「图里能查到」的提交，git 仍可校验它确实存在于对象库（受 `GIT_COMMIT_GRAPH_PARANOIA` 控制）。

#### 4.2.2 核心流程

commit-graph 文件采用 git 通用的 **chunk（分块）格式**：一个文件头 + 一张「分块目录（TOC）」+ 若干命名分块。

```
偏移 0:   文件头 (8 字节)
            [0..4)  签名 "CGPH"（0x43475048）
            [4]     版本号 (=1)
            [5]     哈希版本（SHA-1=1, SHA-256=2）
            [6]     分块个数 num_chunks
            [7]     保留
偏移 8:   分块目录 TOC：每个分块占 12 字节（4 字节 chunk id + 8 字节偏移），末尾还有一个全 0 的哨兵项
之后:     各分块数据（OIDF / OIDL / CDAT / GDA2 / EDGE / BIDX / BDAT / BASE ...）
末尾:     整个文件的哈希校验尾（rawsz 字节）
```

每个 commit 在 `CDAT`（commit data）块里占 `graph_data_width = rawsz + 16` 字节，字段排列如下（关键！）：

| 相对偏移 | 长度 | 字段 | 含义 |
| --- | --- | --- | --- |
| `0` | `rawsz` | tree OID | 该提交指向的 tree（原始字节，非十六进制） |
| `rawsz+0` | 4 | 第一个 parent 的 `edge_value` | **图内位置下标**，不是 OID！`0x70000000` 表示无 parent |
| `rawsz+4` | 4 | 第二个 parent 的 `edge_value` | 同上；若超过两个 parent，最高位置 1 并指向 `EDGE` 块 |
| `rawsz+8` | 4 | 打包字 | 高 30 位 = 世代号(v1/topo level)；低 2 位 = 提交日期的高位 |
| `rawsz+12` | 4 | 提交日期的低位 | 与上 2 位拼成完整提交时间戳 |

这就是「加速 parent 查找」的全部秘密：**parent 不是按哈希存，而是按「它在图里的第几行」存**。读 parent = 读 4 字节下标 → 直接定位到另一行 CDAT，全程是纯内存数组访问，没有解压、没有哈希查找。

世代号 v2（corrected commit date，更紧的剪枝下界）单独存在 `GDA2` 块，每个 commit 一个 32 位「相对提交日期的偏移」；偏移过大时存进 `GDO2` 溢出块。

读取流程：

```
prepare_commit_graph(r)              # 首次访问时懒加载，mmap 整个文件
  → read_commit_graph_one
    → load_commit_graph_one_fd_st → parse_commit_graph
       校验签名/版本 → 读 TOC → 逐块回调，把每块起始指针存进 struct commit_graph 字段
之后任何 commit 解析:
  parse_commit_in_graph(r, item)
    → find_commit_pos_in_graph: 用 bsearch 在 OIDL 块里二分定位该 commit 的行号 pos
    → fill_commit_in_graph(item, g, pos): 从 CDAT 第 pos 行读出 tree/date/parents
```

#### 4.2.3 源码精读

文件头与各分块的 chunk id 都定义在文件顶部：

[commit-graph.c:44-65](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-graph.c#L44-L65) —— `GRAPH_SIGNATURE`（"CGPH"）、各 `GRAPH_CHUNKID_*`、以及 `GRAPH_PARENT_NONE = 0x70000000`、`GRAPH_DATA` 相关掩码。

每条 CDAT 记录的宽度，正好是 `rawsz + 16`：

[commit-graph.c:78-81](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-graph.c#L78-L81) —— `graph_data_width`：`rawsz + 16`。

`parse_commit_graph` 负责校验文件头、读 TOC、逐块派发回调。注意哪些块是「必需」、哪些是「可选」：

[commit-graph.c:373-491](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-graph.c#L373-L491) —— `parse_commit_graph`。第 433~444 行强制要求 `OIDF`(fanout)、`OIDL`(lookup)、`CDAT`(data) 三块；第 453~462 行按配置决定是否读 `GDA2`（世代号 v2）；第 464~469 行按配置决定是否读 bloom 块。

fanout 块的作用与 pack 的 `.idx` 完全同构（见 u3-l3）：256 项累积计数，把二分范围缩到同首字节桶，并把提交总数写在 `fanout[255]`：

[commit-graph.c:282-304](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-graph.c#L282-L304) —— `graph_read_oid_fanout`：第 291 行 `g->num_commits = ntohl(g->chunk_oid_fanout[255])` 直接拿到提交总数。

**最核心的一段**——从 CDAT 第 `pos` 行还原一个 commit 的全部信息。对照上面的字段表逐行读：

[commit-graph.c:879-921](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-graph.c#L879-L921) —— `fill_commit_graph_info`。其中：
- 第 893 行用 `graph_data_width * lex_index` 定位到该 commit 的行；
- 第 898~900 行拼出提交日期（高 2 位 + 低 32 位）；
- 第 902~917 行读世代号：v2 从 `GDA2` 块读「偏移」并加上提交日期，v1 从打包字的高 30 位读。

parent 的还原在 `fill_commit_in_graph`，关键是那两个 `edge_value` 是**下标**而非哈希：

[commit-graph.c:928-982](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-graph.c#L928-L982) —— `fill_commit_in_graph`。第 951、956 行分别读第一、第二个 parent 的 `edge_value`；等于 `GRAPH_PARENT_NONE` 即无该 parent；超过两个 parent 时（第 959 行 `GRAPH_EXTRA_EDGES_NEEDED`）去 `EDGE` 块继续读。`edge_value` 经 `insert_parent_or_die` 转回 OID：

[commit-graph.c:861-877](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-graph.c#L861-L877) —— `insert_parent_or_die`：把「图内位置 pos」翻译成「OID → `struct commit *`」，并顺手把该 parent 的 `graph_pos` 记进 slab，避免下次重复二分。

定位一个 commit 在图里的行号靠 `bsearch_graph`——和 pack `.idx` 一样用 fanout + 二分：

[commit-graph.c:834-838](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-graph.c#L834-L838) —— `bsearch_graph`：在 `OIDL` 块里按哈希二分。

懒加载入口 `prepare_commit_graph` 体现了「按需、且只尝试一次」的策略：

[commit-graph.c:738-779](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-graph.c#L738-L779) —— `prepare_commit_graph`：第 752~754 行用 `commit_graph_attempted` 保证只尝试一次（即便失败也不重试）；第 758~759 行受 `core.commitGraph` 配置与 `GIT_TEST_COMMIT_GRAPH` 环境变量控制。

> 全局视图：`struct commit_graph` 本身就是个「分块指针盒」——每个 `chunk_*` 字段指向 mmap 出来的对应分块起始地址，见 [commit-graph.h:84-115](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-graph.h#L84-L115)。

#### 4.2.4 代码实践

**实践目标**：亲手生成 commit-graph，在磁盘上找到它，并对照源码看清 `CGPH` 文件头与 CDAT 行宽。

**操作步骤**：

1. 在一个有历史的仓库（git 自身仓库很合适）生成提交图：
   ```sh
   git commit-graph write --reachable
   ls -la .git/objects/info/commit-graph
   ```
2. 用 `xxd` 查看文件头，确认签名 `CGPH`：
   ```sh
   xxd .git/objects/info/commit-graph | head -2
   # 应看到第一行以 4347 5048 (= "CGPH") 开头
   ```
3. 校验文件完整性：
   ```sh
   git commit-graph verify
   ```
4. 对比「开/关 commit-graph」下 `git log` 的耗时（`git log` 遍历每个 commit 都要读 parent）：
   ```sh
   # 关闭提交图
   git -c core.commitGraph=false log --oneline > /dev/null
   # 你可以分别用 /usr/bin/time -v 或 GIT_TRACE2_PERF 测两次
   GIT_TRACE2_PERF=1 git -c core.commitGraph=false log --oneline > /dev/null 2>&1 | tail -15
   GIT_TRACE2_PERF=1 git                       log --oneline > /dev/null 2>&1 | tail -15
   ```

**预期现象**：步骤 1 生成文件；步骤 2 头四个字节是 `43 47 50 48`（"CGPH"），第 5 字节（版本）为 `01`；步骤 3 校验通过；步骤 4 中开启 commit-graph 的运行通常 region 事件里的「解析 commit」耗时更小。

**待本地验证**：步骤 4 的具体加速比取决于仓库规模、磁盘缓存与 CPU，本机数值需自行观察；在小型仓库上差异可能不明显。

#### 4.2.5 小练习与答案

**练习 1**：为什么 commit-graph 里 parent 用「图内行号下标」而不是直接存 parent 的 OID？

> **答案**：存下标让「读 parent」变成 O(1) 数组访问——拿到 `edge_value` 后直接跳到 CDAT 的那一行即可还原 parent 的全部信息；若存 OID，则还要再对 parent 做一次哈希二分（`bsearch_graph`）才能定位，多一次查找。下标方案把整条 parent 链的遍历压成连续的内存跳转。

**练习 2**：commit-graph 文件末尾的 `rawsz` 字节校验和，保护的是什么？为什么 `git commit-graph verify` 有存在的必要——光有校验和不够吗？

> **答案**：校验和只保证「文件本身没被比特翻转 / 没截断」。`verify` 要做的是**语义校验**：图里记录的 parent/世代号是否与对象库里真实的 commit 对象一致（例如图过期、或被错误重写时，校验和仍可能通过但内容与对象库矛盾）。

**练习 3**：`fill_commit_graph_info` 第 902 行有个判断 `if (g->read_generation_data)`，它决定了从哪里读世代号。请说明这两条分支分别对应「世代号 v1」和「世代号 v2（corrected commit date）」。

> **答案**：`read_generation_data` 为真（存在 `GDA2` 块）时走 v2：从 `GDA2` 读「相对提交日期的偏移」并加上 `item->date` 得到 corrected commit date；否则走 v1：直接从打包字的高 30 位（`>> 2`）读传统世代号。v2 是更紧的剪枝下界。

---

### 4.3 commit-slab 关联数据

#### 4.3.1 概念说明

你大概注意到了一个矛盾：算法需要给每个 commit 挂上「涂色标志」「世代号」「图内位置」等临时数据，但 `struct commit`（见 u3-l1）的字段是固定、全局共享的，不可能为每种算法都往里加字段。

git 的解决方案是 **commit-slab**：一套宏，**在不修改 `struct commit` 的前提下**，给每个 commit 关联一份任意类型的附加数据。它的妙处在于：

- **解耦**：每种算法/缓存自己定义自己的 slab，互不干扰（merge-base 用 `bit_arrays`、commit-graph 用 `commit_graph_data_slab`、`contains` 用 `contains_cache`……）。
- **稀疏**：只为你真正访问过的 commit 分配，没访问过的不占内存。
- **零初始化**：新分配的槽位是 `xcalloc` 出来的，天然为 0，很多算法借此用「0 表示未访问/默认值」。

commit-slab 复用了 `struct commit` 里唯一一个「为辅助数据结构服务」的字段——`index`：每个 commit 在被创建时会拿到一个**单调递增的整数编号**（见 u3-l1 的 `init_commit_node`）。slab 就拿这个 `index` 当主键。

#### 4.3.2 核心流程

一个 slab 在内存里是一个**两级稀疏数组**（`elemtype **slab`）：

```
struct commit_pos {
    unsigned slab_size;   // 每块能装多少个元素
    unsigned stride;      // 每个 commit 关联几个元素（默认 1）
    unsigned slab_count;  // 已分配多少块
    int **slab;           // 块指针数组
}
```

按 commit 的 `index` 寻址：

\[
\text{nth\_slab} = \lfloor c\to\text{index} / \text{slab\_size}\rfloor,\qquad
\text{nth\_slot} = c\to\text{index} \bmod \text{slab\_size}
\]

即「第几块」+「块内第几格」。第一次访问某块时才 `xcalloc` 分配它（约 512KB 一块），所以是按需、批量地生长。

使用三步曲：

```c
define_commit_slab(indegree, int);          // 1. 声明+实现一个「int 型」slab，名为 indegree
static struct indegree indegrees;           // 2. 定义一个实例
init_indegree(&indegrees);                  // 3. 初始化（或用 COMMIT_SLAB_INIT 静态初始化）
int *p = indegree_at(&indegrees, commit);   // 取该 commit 关联的 int 指针（按需分配）
*p = 5;
clear_indegree(&indegrees);                 // 用完释放（离开作用域前必做，防泄漏）
```

宏 `define_commit_slab` 会展开成：一个 `struct indegree` 结构体定义，以及一组 `init_/clear/_at/_peek` 函数。其中 `_at` 在缺失时分配，`_peek` 在缺失时返回 NULL（只读探测）。

#### 4.3.3 源码精读

`define_commit_slab` 本身只是把「声明」和「实现」两个宏拼起来：

[commit-slab.h:62-64](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-slab.h#L62-L64) —— `define_commit_slab` 宏。

slab 的内存布局（两级数组）与默认块大小：

[commit-slab-decl.h:4-16](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-slab-decl.h#L4-L16) —— `COMMIT_SLAB_SIZE`（约 512KB）与 `struct slabname` 布局：`slab_size / stride / slab_count / elemtype **slab`。

寻址与按需分配的核心逻辑——`_at_peek`，`_at`/`_peek` 都委托给它：

[commit-slab-impl.h:52-77](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-slab-impl.h#L52-L77) —— `slabname_at_peek`：第 58~59 行用 `c->index` 算块号与槽号；第 61~68 行按需 `REALLOC_ARRAY` 扩展块指针数组；第 70~75 行按需 `xcalloc` 分配新块（这就是零初始化的来源）；第 76 行返回对应槽位。

来看 commit-graph 如何用它给每个 commit 挂上「图内位置 + 世代号」：

[commit-graph.c:107-109](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-graph.c#L107-L109) —— 定义 `commit_graph_data_slab`，元素类型是 `struct commit_graph_data`。

[commit-graph.h:197-200](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-graph.h#L197-L200) —— `struct commit_graph_data { graph_pos; generation; }`：每个 commit 关联的正是这两项。

读取世代号的公共函数就是一次 slab `_peek`：

[commit-graph.c:126-135](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-graph.c#L126-L135) —— `commit_graph_generation`：从 slab 取数据；若没有或为 0，返回 `GENERATION_NUMBER_INFINITY`。注意它**不分配**（用 `_peek`），所以只读查询是安全的、无副作用的。

`commit_graph_data_at` 则展示了一个细节：slab 默认零初始化，但 `graph_pos` 需要用「未在图中」的哨兵值 `COMMIT_NOT_FROM_GRAPH (0xFFFFFFFF)` 而非 0，于是分配新块时要手动改写：

[commit-graph.c:147-172](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-graph.c#L147-L172) —— `commit_graph_data_at`：第 166~169 行把新分配块的每个 `graph_pos` 改写为 `COMMIT_NOT_FROM_GRAPH`。

> 在 `commit-reach.c` 里也能看到同样的套路，例如 ahead/behind 计算用的 `bit_arrays` slab：
> [commit-reach.c:1089-1090](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-reach.c#L1089-L1090) —— `define_commit_slab(bit_arrays, struct bitmap *)`，给每个 commit 关联一个位图。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：在源码里找出 5 处不同的 `define_commit_slab` 用法，体会「同一机制、多种用途」。

**操作步骤**：

1. 全仓搜索 slab 定义：
   ```sh
   git grep -n "define_commit_slab(" -- '*.c' '*.h'
   ```
2. 对每一条结果，回答三个问题：
   - 它给 commit 关联了什么类型的数据？
   - 这个数据是「算法临时状态」（如 indegree、涂色位图）还是「缓存持久状态」（如 graph_pos、generation）？
   - 调用的是 `_at`（会分配）还是 `_peek`（只读）？为什么？

**预期现象**：你会看到至少这些：`commit_graph_data_slab`（graph_pos/generation）、`topo_level_slab`（拓扑层）、`contains_cache`（contains 结果缓存）、`bit_arrays`（ahead/behind 位图）、`commit_pos`（写入时的临时序号）。

**结论**：凡是「需要按 commit 记账」的场景，git 的统一答案都是 commit-slab，而不是给 `struct commit` 加字段。

#### 4.3.5 小练习与答案

**练习 1**：slab 用 `c->index` 作主键。如果两个不同的 commit 拿到了相同的 `index`，会发生什么？这个情况会发生吗？

> **答案**：会发生数据错乱（两个 commit 读写同一个槽位）。但 `index` 是 commit 对象**创建时**由全局计数器分配的单调递增值（见 u3-l1 `init_commit_node`），每个 `struct commit` 实例对应唯一 `index`，所以只要「同一个 OID 只 lookup 一次、复用同一个 `struct commit`」（这正是 git 的对象池约定），就不会冲突。

**练习 2**：`commit_graph_generation` 用 `_peek`，而 `commit_graph_data_at` 用 `_at`。为什么读世代号不能也用 `_at`？

> **答案**：`_at` 会在缺失时**分配**槽位（有副作用、可能扩容）。读世代号是极高频的只读操作，且对未在图中的 commit 应返回 `INFINITY` 而非分配一个全零槽位，所以必须用无副作用的 `_peek`。`_at` 仅用于「确实要写入该 commit 数据」的场合。

**练习 3**：commit-slab 的块大小约 512KB（`COMMIT_SLAB_SIZE`）。为什么选这么大、而不是每个 commit 一块？

> **答案**：大块摊薄了「块指针数组」的扩容次数与 `malloc` 调用次数——512KB 一块意味着每块能装下成千上万个 commit 的数据，访问新 commit 时大多命中已分配的块，只有跨块时才触发一次分配。这与 git 别处「批量分配、池化回收」的思路一致（对照 u4-l1 索引条目的内存池）。

---

## 5. 综合实践

把本讲三个模块串起来：**亲手制造一次「无 commit-graph → 有 commit-graph」的对比，并用源码解释你观察到的差异从何而来。**

1. **准备**：clone 一个中型仓库（例如 git 自身）。
2. **基线测量**：先确保没有 commit-graph（`rm -f .git/objects/info/commit-graph`），跑一次带 trace 的 `git log`：
   ```sh
   GIT_TRACE2_PERF=1 git -c core.commitGraph=false log --oneline > /dev/null 2>trace_off.txt
   grep -E "region|parse" trace_off.txt | tail -20
   ```
3. **生成提交图**：
   ```sh
   git commit-graph write --reachable
   git commit-graph verify
   ```
4. **对照测量**：
   ```sh
   GIT_TRACE2_PERF=1 git log --oneline > /dev/null 2>trace_on.txt
   grep -E "region|parse" trace_on.txt | tail -20
   ```
5. **源码解释**：对照 [commit.c:600-660](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit.c#L600-L660) 的 `repo_parse_commit_internal`——注意第 625 行 `if (use_commit_graph && parse_commit_in_graph(r, item))` 是**首选路径**，只有图里查不到（返回 0）时才回退到第 641 行的传统对象解析。说明你测得的差异主要来自：遍历中每个 commit 的 parent/date 从「解压对象」变成「读 CDAT 数组行」。
6. **加深一层**：用 `git merge-base master <某tag>` 配合 trace，观察「世代号剪枝」让 merge-base 只走了分叉以来的那一小段历史（结合 4.1 的 `min_generation` 机制解释）。

**交付物**：一段 300 字以内的说明，包含 (a) 你测得的两组耗时/事件数对比，(b) 用本讲源码链路（`prepare_commit_graph → parse_commit_in_graph → fill_commit_in_graph` 的 edge_value 下标定位）解释差异来源。耗时部分若环境波动大，标注「待本地验证」。

## 6. 本讲小结

- **可达性算法**建立在「涂色 + 世代号剪枝」之上：`paint_down_to_common` 从两边同时向下涂 PARENT1/PARENT2，碰头处即公共祖先（RESULT），其 parent 标 STALE 排除；世代号的单调性（祖先世代号严格更小）提供了提前停的数学依据。
- **世代号** \(\text{gen}(c)=1+\max_{p}\text{gen}(p)\) 是 merge-base、祖先判定、ahead/behind 共用的剪枝下界；没有它算法仍正确，只是更慢。
- **commit-graph** 是只读缓存，把所有 commit 的 tree/date/parents/世代号紧凑排进分块文件；parent 存成**图内行号下标**（不是 OID），让取 parent 变成 O(1) 数组访问。
- **CDAT 行宽** `rawsz + 16` 字节：tree OID + 两个 parent 下标 + 打包的世代号/日期；多 parent 经 `EDGE` 块续接。
- 它是**加速器而非真相**：`repo_parse_commit_internal` 先查图、查不到再回退对象库；过期/缺失时自动降级，`GIT_COMMIT_GRAPH_PARANOIA` 还可强校验图内提交确实存在于对象库。
- **commit-slab** 用 `c->index` 作主键的两级稀疏数组，在不改 `struct commit` 的前提下给每个 commit 挂临时/缓存数据；`_at` 分配、`_peek` 只读，零初始化特性被各算法广泛利用。

## 7. 下一步学习建议

- **继续向性能纵深**：本讲的 commit-graph 与 u3-l3 的 pack 是两大数据加速设施；下一单元 u13 会把它们和 **multi-pack-index、pack-bitmap、fsmonitor** 放在一起讲大规模仓库的可扩展性，建议接着读 `midx.c` 与 `pack-bitmap.c`。
- **阅读相邻源码**：想看世代号如何在**写入时**被批量计算，读 `commit-graph.c` 的 `compute_generation_numbers`（[commit-graph.c:1746-1780](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-graph.c#L1746-L1780)）与 `compute_reachable_generation_numbers`；想看「corrected commit date」偏移如何落盘，读同文件的 `compute_generation_offset`。
- **拓扑排序串联**：u7-l1 提到拓扑遍历用「入度法」，其入度计数正是用一个 `define_commit_slab(indegree, int)` 扛的——可以回到 `revision.c` 找到它，作为本讲 slab 知识的直接应用。
- **协议层预告**：fetch/push 的对象协商（u11）会大量复用本讲的 `can_all_from_reach` 与世代号剪枝来「少通告、少传对象」，届时可回看 [commit-reach.c:856-1014](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/commit-reach.c#L856-L1014)。
