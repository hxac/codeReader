# xdiff 底层行级差异

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「行级 diff」要解决的数学问题：在两个行序列之间，找一个**最短的编辑脚本（SES）**，它等价于找一条**最长公共子序列（LCS）**。
- 读懂 `xdiff/xdiffi.c` 里 Myers O(ND) 差分算法的核心实现：对角线（diagonal）扫描、`snake`、分治（divide & conquer），以及为性能而加的启发式剪枝。
- 理解 `xdiff/xprepare.c` 如何把一整块字节「切碎成行记录」并归类（`minimal_perfect_hash`），这是后续比较的统一货币。
- 掌握 `xdiff-interface.c` 这座「桥」：git 主框架（`diff.c`）如何把两个 blob 喂给 xdiff、又如何把 xdiff 吐出的字节流重新拼成一行一行的输出。
- 了解 `userdiff.c` 的「文件类型规则」：不同语言的函数名正则（hunk 头）、word regex（`--word-diff`）与二进制检测如何驱动。
- 看懂三种 diff 算法选项的差异：默认 Myers、`--minimal`、`--patience`、`--histogram`，以及 `--indent-heuristic` 如何滑动 hunk 边界让结果更顺眼。

## 2. 前置知识

本讲默认你已掌握：

- **diff 的两层架构（u8-l1）**：diffcore 是「文件级」——决定哪些文件配对、状态是新增/删除/修改/重命名；xdiff（本讲）是「行级」——给定**一对已经配好的文件**，算出逐行增删的 `@@ ... @@` 补丁。本讲只在「一对文件」内部工作，不再关心文件如何配对。
- **对象模型（u3-l1/u3-l2）**：要比较的内容来自 blob 对象；一个 blob 就是一段字节。xdiff 比较的输入 `mmfile_t` 就是「一段字节 + 长度」。
- **C 语言的函数指针与回调**：xdiff 与 git 之间的数据交换完全靠回调函数，不靠共享全局变量。

两个需要先建立的直觉：

**直觉一：行级 diff 是一个图搜索问题。** 把旧文件的 N 行和新文件的 M 行画成一张网格：从左上角 `(0,0)` 走到右下角 `(N,M)`，向右走 =「删除旧行」，向下走 =「新增新行」，斜着走 =「两行相同」。找最少改动，就是在网格里找一条**拐弯最少**的路径。Myers 算法就是高效地搜这条路径。

**直觉二：xdiff 是一个「被嵌进来的第三方库」。** `xdiff/` 目录源自 Davide Libenzi 的 LibXDiff（文件头版权年份 2003、LGPL）。它自成一格、几乎不依赖 git 的其他设施（连内存分配都被宏重定向到 git 的 `xmalloc`）。因此 git 用一个薄薄的 `xdiff-interface.c` 把「git 的世界（blob、struct diff_options）」翻译成「xdiff 的世界（`mmfile_t`、回调）」。理解这条边界，是本讲的关键。

> 术语：**mmfile（memory-mapped file，这里只是「内存里的文件」）** = 一段字节 `{char *ptr; long size;}`。**record（记录）** = 一行（从上一个 `\n` 之后到本 `\n`，含本 `\n`）。**hunk（补丁块）** = 一段连续的增删，对应输出里的 `@@ -a,b +c,d @@`。**diagonal（对角线）** = Myers 网格里 `k = x - y` 相同的点集。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [xdiff/xdiffi.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c) | xdiff 的算法核心：Myers 差分（`xdl_split`/`xdl_recs_cmp`/`xdl_do_diff`）、hunk 压缩与缩进启发式（`xdl_change_compact`）、编辑脚本生成（`xdl_build_script`）、顶层编排（`xdl_diff`）。 |
| [xdiff/xdiffi.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.h) | 算法环境 `xdalgoenv_t`、变更记录 `xdchange_t`，及对内函数声明。 |
| [xdiff/xprepare.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xprepare.c) | 预处理：把字节流切成行记录、归类（赋 `minimal_perfect_hash`）、剔除无匹配行、裁掉公共首尾（`xdl_prepare_env`/`xdl_prepare_ctx`/`xdl_cleanup_records`）。 |
| [xdiff/xtypes.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xtypes.h) | xdiff 核心数据结构：`xrecord_t`（一行）、`xdfile_t`（一个文件的记录集）、`xdfenv_t`（两个文件的环境）。 |
| [xdiff/xdiff.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiff.h) | xdiff 对外公共头：`mmfile_t`、`xpparam_t`（参数+flags）、`xdemitconf_t`（输出配置）、`xdemitcb_t`（输出回调）、所有 `XDF_*` 标志位定义。 |
| [xdiff-interface.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.c) | **桥接层**：`xdi_diff`（入口+大小保护+公共尾部裁剪）、`xdi_diff_outf`（装配行回调）、`xdiff_outf`（把字节缓冲拼成完整行）、funcname 正则注入。 |
| [xdiff-interface.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.h) | 桥接层公共 API：`MAX_XDIFF_SIZE` 上限、`xdiff_emit_line_fn`/`xdiff_emit_hunk_fn` 回调类型。 |
| [userdiff.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c) | 文件类型规则：内置各语言 funcname/word-regex 驱动表、按路径或名字查找驱动、`diff.*` 配置解析。 |
| [xdiff/xhistogram.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xhistogram.c) | `--histogram` 算法实现：基于出现次数的 LCS 查找，递归分治。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- 4.1 xdiff 差异算法核心：从字节到「行记录」，再到 Myers LCS
- 4.2 xdiff-interface 桥接：git 与 xdiff 的双向翻译
- 4.3 userdiff 文件类型规则：funcname、word regex 与二进制检测

---

### 4.1 xdiff 差异算法核心

#### 4.1.1 概念说明

「找两个文件的差异」这件事，要解决的数学问题可以严格表述。

给定两个**行序列** \(A = a_1 a_2 \dots a_N\)（旧文件，N 行）与 \(B = b_1 b_2 \dots b_M\)（新文件，M 行）。我们想要：

- 一条**最长公共子序列 LCS**：它是 A、B 共同拥有、且保持相对顺序的最长行序列。
- 等价地，一条**最短编辑脚本 SES**：用最少的「删除 A 的某行 / 插入 B 的某行」把 A 变成 B。

两者是同一枚硬币的两面：LCS 里的行被保留（斜走），不在 LCS 里的旧行被删（右走）、新行被插（下走）。把网格画出来，就是把网格图 \( (N+1)\times(M+1) \) 上从 `(0,0)` 到 `(N,M)` 的最短路径问题。设编辑距离为 \(D\)，则

\[
D = N + M - 2 \cdot |LCS|
\]

也就是说，**公共子序列越长，编辑距离越短**，diff 的增删行就越少——这正是「最小 diff」的含义。

直接做动态规划是 \(O(NM)\) 的，对大文件不可接受。Myers 在 1986 年提出一个关键观察：**复杂度正比于编辑距离 D，而非文件大小**。当两个文件很相似（D 很小）时，Myers 算法极快；这正是版本控制里最常见的情况（每次只改了几行）。复杂度为：

\[
O\big((N+M)\,D\big)
\]

git 内置三种算法，由 `xpparam_t.flags` 里的算法位选择（见 [xdiff/xdiff.h:44-47](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiff.h#L44-L47)）：

- **Myers（默认）**：上面描述的对角线扫描。标志位为 0（即 `myers`/`default`）。
- **`XDF_NEED_MINIMAL`（`--minimal`）**：仍是 Myers，但关掉启发式剪枝，强行追求最小编辑量（更慢，但极端情况下更准）。
- **`XDF_PATIENCE_DIFF`（`--patience`）**：基于「全局唯一行」做锚点切分，递归。
- **`XDF_HISTOGRAM_DIFF`（`--histogram`）**：基于「行出现次数」的直方图，递归，且能在 hunk 内部继续细分。

命令行字符串如何映射成这些位，见 [diff.c:222-237](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c#L222-L237)：`myers`/`default`→0、`minimal`→`XDF_NEED_MINIMAL`、`patience`→`XDF_PATIENCE_DIFF`、`histogram`→`XDF_HISTOGRAM_DIFF`。

#### 4.1.2 核心流程

整条链路从 git 侧进入 xdiff 的入口是 `xdl_diff`（[xdiff/xdiffi.c:1088](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L1088)），它把一次行级 diff 编排成固定的几步：

```
xdl_diff(mf1, mf2, xpp, xecfg, ecb)        # 顶层编排
  ├── xdl_do_diff(mf1, mf2, xpp, xe)        # 第 1 步：算出哪些行变了
  │     ├── xdl_prepare_env(...)            #   1a. 字节→行记录，归类，裁剪
  │     └── (按算法分派)
  │           ├── xdl_do_patience_diff      #   --patience
  │           ├── xdl_do_histogram_diff     #   --histogram
  │           └── xdl_recs_cmp (Myers)      #   默认：分治 + xdl_split
  ├── xdl_change_compact(xdf1, xdf2)        # 第 2 步：左右各压缩一次，滑 hunk 边界
  ├── xdl_build_script(xe)                  # 第 3 步：收集成 xdchange_t 编辑脚本
  └── ef(xe, xscr, ecb, xecfg)              # 第 4 步：输出（默认 xdl_emit_diff）
```

**第 1 步（do_diff）的核心产出是一个布尔数组 `changed[]`**：对每个文件，标记「这一行是否被改动了」。Myers 的目标就是填好这两个数组。下面分别看预处理和 Myers 主体。

**预处理（xprepare.c）** 要做的事，是把「字节」变成可比较的「行」：

1. **切行**：按 `\n` 把字节流切成一条条 `xrecord_t`，每条记 `{ptr, size}`。
2. **算哈希 + 归类**：对每行算哈希，把「内容相同的行」归到同一个类（`xdlclass_t`），给每个类一个编号。
3. **回填**：把每行的 `minimal_perfect_hash` 字段设为它所属类的编号——这就是后面比较两行是否相等的「身份证」。
4. **裁剪优化**：去掉在另一文件里完全没出现过的行（必然是纯增删），裁掉两文件公共的首尾行，缩小问题规模。

经过预处理，「比较两行是否相等」就退化成「比较两个整数是否相等」（`minimal_perfect_hash` 是否相同），极快。

**Myers 主体（xdiffi.c）** 用分治法（divide & conquer）：

1. `xdl_recs_cmp(off1,lim1, off2,lim2)` 处理一个「盒子」（两段行范围）。
2. 先把盒子四周的公共行削掉（前后各扫一遍 `snake`）。
3. 若某一边变空，另一边整段标为 changed。
4. 否则调用 `xdl_split` 找一个**中间分裂点**，把盒子一分为二，递归处理两半。
5. `xdl_split` 就是 Myers 的对角线扫描：同时从盒子左上角（前向）和右下角（后向）出发，沿对角线推进，直到两条搜索在中间某条对角线上「碰头」，碰头点就是分裂点。

#### 4.1.3 源码精读

**(a) 核心数据结构** —— 先认清 xdiff 手里的几样东西（[xdiff/xtypes.h:41-58](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xtypes.h#L41-L58)）：

```c
typedef struct s_xrecord {
    uint8_t const *ptr;            // 指向行内容的字节
    size_t size;                   // 行长度（含 \n）
    size_t minimal_perfect_hash;   // 所属「行类」的编号（不是真哈希，是类下标）
} xrecord_t;

typedef struct s_xdfile {
    xrecord_t *recs;        // 行记录数组
    size_t nrec;            // 总行数
    ptrdiff_t dstart, dend; // 有效比较范围 [dstart, dend]（裁剪后）
    bool *changed;          // 【核心产出】每行是否改动；首尾各多一格哨兵
    size_t *reference_index;// 把「有效行下标」映射回原始 recs 下标
    size_t nreff;           // 有效（未被预剔除）行数
} xdfile_t;

typedef struct s_xdfenv { xdfile_t xdf1, xdf2; } xdfenv_t;
```

注意 `changed` 数组被刻意「前移一格」：在 `xdl_prepare_ctx` 里分配 `nrec+2` 格后执行 `xdf->changed += 1`（[xprepare.c:171-174](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xprepare.c#L171-L174)），这样 `changed[-1]` 和 `changed[nrec]` 都是合法的零值，让后续循环免去做边界判断（见 `xdl_change_compact` 上方注释 [xdiffi.c:686-688](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L686-L688)）。

`minimal_perfect_hash` 这个名字有点误导——它**不是哈希值**，而是「行类」在 `rcrecs[]` 数组里的下标（见下方 `xdl_classify_record` 的 `rcrec->idx`）。两行相等 ⟺ 两个 `minimal_perfect_hash` 相等。

**(b) 预处理：切行与归类** —— `xdl_prepare_ctx` 逐行扫描字节流（[xprepare.c:142-184](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xprepare.c#L142-L184)）：

```c
if ((cur = blk = xdl_mmfile_first(mf, &bsize))) {
    for (top = blk + bsize; cur < top; ) {
        prev = cur;
        hav = xdl_hash_record(&cur, top, xpp->flags);  // 算行哈希，并推进 cur 到行尾
        ...
        crec = &xdf->recs[xdf->nrec++];
        crec->ptr = prev;          // 行内容起点
        crec->size = cur - prev;   // 行长度
        if (xdl_classify_record(pass, cf, crec, hav) < 0)  // 归类
            goto abort;
    }
}
```

`xdl_hash_record`（由 [xdiff/xutils.c:307](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xutils.c#L307) 的 `xdl_hash_record_verbatim` 或 [:252](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xutils.c#L252) 的 `xdl_hash_record_with_whitespace` 实现，取决于是否设置忽略空白类 flag）一边走一边用 djb2 变体累乘哈希，遇到 `\n` 即停，并把游标推进到下一行开头——一行的工作一次完成。

归类逻辑在 `xdl_classify_record`（[xprepare.c:97-131](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xprepare.c#L97-L131)）：先用哈希在桶链 `rchash` 里找，找到再 `xdl_recmatch` 逐字节复核（防哈希碰撞）；找不到就新建一个 `xdlclass_t`，赋予新编号 `idx = cf->count++`。最后把行记录的身份证设为类编号：

```c
(pass == 1) ? rcrec->len1++ : rcrec->len2++;   // 统计此类在文件1/文件2出现次数
rec->minimal_perfect_hash = (size_t)rcrec->idx; // 回填身份证
```

`len1`/`len2` 记录某行类分别在两个文件出现几次，这正是下一步「剔除无匹配行」的依据。

**(c) 预剔除：缩小问题规模** —— `xdl_cleanup_records`（[xprepare.c:265-382](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xprepare.c#L265-L382)）给每行打三个标签之一：

- `DISCARD`：在另一文件里出现 0 次（`nm == 0`）→ 必然纯增删，直接标 `changed=true`，不进入比较。
- `KEEP`：出现次数低于阈值 → 正常参与比较。
- `INVESTIGATE`：出现次数过多（如空行、`}`、`return;` 这种到处都是的行）→ 暂时存疑。

阈值 `mlim` 用 `xdl_bogosqrt(nrec)`（[xutils.c:26-36](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xutils.c#L26-L36)，一个用移位逼近的整数平方根）算出，并受 `XDL_MAX_EQLIMIT=1024` 封顶；`--minimal` 时阈值设为 `PTRDIFF_MAX`（无穷），等于不做这层剪枝、保留全部行去拼最小编辑量。`INVESTIGATE` 的行再由 `xdl_clean_mmatch`（[:194](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xprepare.c#L194)）按邻域判定是否真的该丢——只在「周围确实有 DISCARD」时才丢，避免把一整片相同行误判。

`xdl_trim_ends`（[xprepare.c:388-411](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xprepare.c#L388-L411)）则把两文件开头/结尾连续相同的行削掉，缩小成 `[dstart, dend]` 区间。注意：patience 和 histogram 算法**跳过** `xdl_optimize_ctxs`（[xprepare.c:460-462](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xprepare.c#L460-L462)），因为它们自带不同的预处理策略。

**(d) Myers 核心：分治与对角线扫描** —— 入口 `xdl_do_diff`（[xdiffi.c:314-366](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L314-L366)）先预处理，再按算法分派，Myers 路径里分配两条「K 向量」`kvdf`（前向）和 `kvdb`（后向）：

```c
ndiags = xe->xdf1.nreff + xe->xdf2.nreff + 3;
kvd = XDL_ALLOC_ARRAY(..., 2 * ndiags + 2);
kvdf = kvd;  kvdb = kvdf + ndiags;
kvdf += xe->xdf2.nreff + 1;   // 居中偏移，让下标可为负（对角线 k 可正可负）
kvdb += xe->xdf2.nreff + 1;
xenv.mxcost = (long)xdl_bogosqrt((uint64_t)ndiags);  // 启发式代价上限
...
res = xdl_recs_cmp(..., (xpp->flags & XDF_NEED_MINIMAL) != 0, &xenv);
```

`kvdf`/`kvdb` 用「居中偏移」的技巧让数组能以负下标访问：Myers 里对角线编号 `k = x - y` 可正可负，把数组中点对准 `k=0` 即可。`mxcost` 是「最多搜多少轮就放弃精确解、改用启发式收尾」的上限，用平方根估算。

`xdl_recs_cmp`（[xdiffi.c:265-311](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L265-L311)）是分治骨架：先前后各扫一遍 `snake`（连续相同行）削盒子，再调 `xdl_split` 找中点 `spl.i1/spl.i2`，然后递归两半。`need_min` 参数即 `XDF_NEED_MINIMAL`，一路透传给 `xdl_split` 控制是否启用剪枝。

真正的对角线扫描在 `xdl_split`（[xdiffi.c:50-257](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L50-L257)）。其顶部注释直接点明出处（[xdiffi.c:41-49](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L41-L49)）：

> See "An O(ND) Difference Algorithm and its Variations", by Eugene Myers.

每一轮 `ec`（编辑距离猜测值）做两件事：

1. **前向推进**（[xdiffi.c:88-105](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L88-L105)）：对每条对角线 `d`，取「上一轮相邻两条对角线里的更远者」为起点，然后尽可能沿对角线吃掉相同行（`snake`）：
   ```c
   for (; i1 < lim1 && i2 < lim2 && get_hash(xdf1, i1) == get_hash(xdf2, i2); i1++, i2++);
   ```
   这里 `get_hash` 取的就是 `minimal_perfect_hash`（[xdiffi.c:25-28](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L25-L28)）。若在某条对角线上前向结果与后向结果交汇（`kvdb[d] <= i1`），就找到了分裂点，立即返回。

2. **后向推进**（[xdiffi.c:125-142](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L125-L142)）：对称地从盒子右下角往左上扫。

`XDL_SNAKE_CNT=20`、`XDL_K_HEUR=4` 等常量（[xdiffi.c:30-34](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L30-L34)）服务两段启发式：「好 snake」采样（[:157-205](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L157-L205)）和「代价超限收尾」（[:212-255](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L212-L255)）。当 `ec >= xenv.mxcost` 时（[:212](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L212)），算法不再死磕精确最优，而是在当前所有对角线里挑「走最远」的点作为近似分裂点返回——这就是「默认 Myers 不保证绝对最小、但绝大多数时候够好」的由来；`--minimal` 通过让 `need_min` 为真并走到 `continue`（[:144-145](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L144-L145)）来抑制这些剪枝。

**(e) 压缩与缩进启发式** —— `changed[]` 填好后，`xdl_change_compact`（[xdiffi.c:793-972](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L793-L972)）对每个文件各跑一次，目的是「把可滑动的改动块滑到一个更合理的位置」。因为 diff 只要求增删集合正确，但同一处可有多种摆法（比如连续 5 行里有 3 行变了，边界可以左移或右移），这步负责挑一个让人类更易读的摆法。它先把改动块上下滑到极限（`group_slide_up/down`，[:751-786](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L751-L786)），再决定落点。

`--indent-heuristic`（位 `XDF_INDENT_HEURISTIC`，[xdiff.h:49](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiff.h#L49)）在这步起作用（[xdiffi.c:876-919](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L876-L919)）：对每个候选切分位置算一个「坏度分数」（`score_add_split`，[:588-664](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L588-L664)），偏好落在空行、缩进减少处（更像「块的结尾」），避免把 hunk 头切在一行代码中间。注意 git 现在默认就开启这个启发式。

**(f) 编辑脚本生成** —— `xdl_build_script`（[xdiffi.c:975-998](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L975-L998)）从两个 `changed[]` 数组倒着扫，把连续的改动聚合成 `xdchange_t` 节点（`{i1,i2,chg1,chg2}`，即旧/新起点与长度），串成链表——这就是最终交给输出阶段的「编辑脚本」。

> 提示：Myers 默认算法不保证绝对最小，所以「换算法后行数可能不同」是正常的，本讲实践环节会亲手验证。

#### 4.1.4 代码实践

**实践目标**：用一段「故意让 Myers 的启发式与最小解产生分歧」的小文本，体会 `--minimal` 与默认算法的差异，并理解 `mxcost` 剪枝的存在。

**操作步骤**（在 git 源码仓库里执行，会生成临时文件，记得事后清理）：

1. 准备旧文件 `a.txt`：
   ```
   printf 'x\nA\nB\nC\nD\nE\nF\nG\nH\nI\nJ\nK\nL\nx\n' > a.txt
   ```
2. 准备新文件 `b.txt`（删掉中间一段，但首尾各保留一个 `x`，制造大量相同行干扰 snake 选择）：
   ```
   printf 'x\nL\nx\n' > b.txt
   ```
3. 分别用默认算法、`--minimal`、`--histogram` 跑 diff：
   ```
   ./git diff --no-index --no-color a.txt b.txt | tail -n +5
   ./git diff --no-index --no-color --minimal a.txt b.txt | tail -n +5
   ./git diff --no-index --no-color --histogram a.txt b.txt | tail -n +5
   ```
4. （可选）阅读 [xdiffi.c:212-255](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L212-L255) 的「超代价收尾」分支，并对照 `mxcost = xdl_bogosqrt(ndiags)`（[:351](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L351)）理解：当文件足够大、`ec` 触顶时，默认算法会提前收尾，因而可能与 `--minimal` 的增删行数不同。

**需要观察的现象**：三者的 `@@ ... @@` 范围与增删行可能在细节上不同（例如某个相同的 `x` 被算作上下文还是被删后重加）。即使行数相同，hunk 的边界位置也可能不同。

**预期结果**：默认与 `--histogram` 通常给出干净的「删一段」；`--minimal` 保证编辑脚本最短。若这个小样本上三者恰好一致，把 `a.txt` 加长到几十行重复字母再试——样本越大，启发式与最小解分化的概率越高。

**如果无法确定运行结果**：以上命令的具体输出「待本地验证」，取决于你构造的文本与 git 的具体版本；重点不是背下输出，而是确认「不同算法确实可能给出不同 diff」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Myers 算法在「两个文件几乎相同」时特别快，而在「两个完全无关的文件」时退化？用复杂度 \(O((N+M)D)\) 解释。

> **答案**：复杂度里的 \(D\) 是编辑距离。两文件几乎相同时 \(D\) 很小（只改了几行），算法只沿对角线走很少几轮即可；两文件完全无关时 \(D \approx N+M\)，复杂度退化为 \(O((N+M)^2)\)，与朴素 DP 相当。这正是 git 默认用 Myers 的底气——版本控制里日常 diff 的 \(D\) 通常极小。

**练习 2**：`minimal_perfect_hash` 字段名叫「哈希」，但它实际存的是什么？为什么这样设计？

> **答案**：它存的是该行所属「行类」在 `rcrecs[]` 数组里的下标（`xdl_class_t.idx`），不是哈希值。设计目的：把「两行是否相等」从「逐字节比较」降为「整数比较」，让 Myers 主循环里的 `get_hash(xdf1,i)==get_hash(xdf2,i)` 极快；同时归类阶段已用 `xdl_recmatch` 做过逐字节复核，保证了相等性判定的正确性（哈希碰撞不会导致误判）。

**练习 3**：`xdl_do_diff` 里 `kvdf += xe->xdf2.nreff + 1` 这一步偏移有什么作用？

> **答案**：Myers 用对角线编号 `k = x - y`，`k` 可正可负，而 C 数组下标不能为负。把数组的中点对齐到 `k=0`（偏移 `nreff+1`），于是 `kvdf[k]` 对负 `k` 也能合法访问，无需在每个 `xdl_split` 循环里做下标平移。

---

### 4.2 xdiff-interface 桥接

#### 4.2.1 概念说明

`xdiff/` 是一个相对独立的库，它只认得自己定义的类型：输入是 `mmfile_t`（一段字节），参数是 `xpparam_t`，输出配置是 `xdemitconf_t`，产出通过 `xdemitcb_t` 里的两个回调函数送出。而 git 这边——`diff.c`、`combine-diff.c`、`merge-recursive` 等——手里是 `struct object_id`（blob 哈希）、`struct diff_options`（一大堆选项）、`strbuf`（动态字符串）。

两边对不上。`xdiff-interface.c` 就是那座翻译桥，它做三件事：

1. **适配输入**：把 git 的 blob（或工作树文件）读成 `mmfile_t`；并做一道安全闸（文件太大就拒绝）和一道优化（裁掉两文件相同的尾部）。
2. **适配输出**：xdiff 是「按缓冲区」吐字节的（一次可能给半行、或多行拼一起），而 git 想要「按行」处理。桥层用一个 `strbuf` 把半行攒起来，凑成整行再回调 git。
3. **适配选项**：把 git 的 funcname 正则、冲突风格等翻译成 xdiff 的 `xdemitconf_t` 字段。

理解这座桥，就能解释一个常见困惑：「为什么 xdiff 的输出不是直接 printf，而要绕一圈回调？」——因为 xdiff 是库，不能假设输出目标是 stdout，它只负责「算出该输出什么」，由调用方决定「往哪儿写」。

#### 4.2.2 核心流程

桥的入口是 `xdi_diff`（[xdiff-interface.c:119](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.c#L119)），它几乎是 `xdl_diff` 的直通，但加了两道处理：

```
xdi_diff(mf1, mf2, xpp, xecfg, xecb)
  ├── if (size > MAX_XDIFF_SIZE) return -1;     # 安全闸：超大文件直接放弃
  ├── if (无上下文行要求) trim_common_tail(&a,&b); # 优化：裁掉相同尾部
  └── xdl_diff(&a, &b, xpp, xecfg, xecb);        # 进入 xdiff 真身
```

更高层、也更常用的入口是 `xdi_diff_outf`（[:133](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.c#L133)）：调用方只需提供两个回调（`hunk_fn` 和 `line_fn`），桥层负责把它们包成 `xdemitcb_t` 再调 `xdi_diff`。`diff.c` 里所有「打印 patch」的地方（如 [diff.c:4135](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c#L4135) 的 `fn_out_consume`）走的都是这条路。

行装配的核心是回调 `xdiff_outf`（[:56](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.c#L56)）：xdiff 每次给它一个 `mmbuffer_t[]` 数组（若干段字节），它逐段检查「这段是否以 `\n` 结尾」：

- 不以 `\n` 结尾 → 是「半行」，先存进 `remainder` 这个 strbuf，等下一段拼上。
- 以 `\n` 结尾 → 若 `remainder` 非空就拼上再整体交给 `consume_one`（按 `\n` 切成行回调 `line_fn`），否则直接处理。

这样无论 xdiff 内部如何分块，git 侧拿到的永远是「完整的行」。

#### 4.2.3 源码精读

**(a) 安全闸与尾部裁剪** —— `xdi_diff`（[xdiff-interface.c:119-131](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.c#L119-L131)）：

```c
if (mf1->size > MAX_XDIFF_SIZE || mf2->size > MAX_XDIFF_SIZE)
    return -1;
if (!xecfg->ctxlen && !(xecfg->flags & XDL_EMIT_FUNCCONTEXT))
    trim_common_tail(&a, &b);
return xdl_diff(&a, &b, xpp, xecfg, xecb);
```

`MAX_XDIFF_SIZE` 定义为 `1024*1024*1023`（约 1GB−1MB，[xdiff-interface.h:14](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.h#L14)），头部注释说明「xdiff 处理不了吉字节级内容」。注意它返回 `-1`（错误），git 上层据此把该文件当作「二进制/无法 diff」处理。

`trim_common_tail`（[xdiff-interface.c:98-117](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.c#L98-L117)）以 1024 字节为块从尾部倒着 `memcmp`，找到第一处不同，再回退到最近的 `\n`（保证不把一行切两半），把公共尾部从两个 `mmfile_t` 的 `size` 里减掉。仅当「不需要上下文行」时才做——因为带上下文（`-U` 或函数上下文）时，尾部公共行可能仍要作为 context 显示，不能裁。

**(b) 行装配回调** —— `xdiff_outf`（[xdiff-interface.c:56-92](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.c#L56-L92)）核心片段：

```c
for (i = 0; i < nbuf; i++) {
    if (mb[i].ptr[mb[i].size-1] != '\n') {
        /* Incomplete line */
        strbuf_add(&priv->remainder, mb[i].ptr, mb[i].size);
        continue;
    }
    /* we have a complete line */
    if (!priv->remainder.len) {
        stop = consume_one(priv, mb[i].ptr, mb[i].size);
        continue;
    }
    strbuf_add(&priv->remainder, mb[i].ptr, mb[i].size);
    stop = consume_one(priv, priv->remainder.buf, priv->remainder.len);
    strbuf_reset(&priv->remainder);
}
```

`consume_one`（[:38-54](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.c#L38-L54)）再按 `\n` 把一段字节切成一行一行，逐行回调 `line_fn`。这里有个设计点：`line_fn` 返回非零表示「提前中止」（如 `git log -S` 找到第一个匹配就停），桥层会把这个信号一路冒泡回 `xdi_diff_outf`（见头文件注释 [xdiff-interface.h:16-36](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.h#L16-L36)）。

**(c) hunk 回调** —— `xdiff_out_hunk`（[:22-36](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.c#L22-L36)）：每当 xdiff 要开始一个新 hunk，先回调它，把 `{old_begin, old_nr, new_begin, new_nr, func, funclen}` 传给 git 侧（`func` 就是 hunk 头里那个函数名，由 `xdemitconf_t.find_func` 算出）。开头那句 `if (priv->remainder.len) BUG(...)` 是个断言：hunk 边界必须落在行边界上，否则就是 xdiff 出 bug。

**(d) 输入侧辅助** —— 把 git 对象读成 `mmfile_t` 的两个助手：`read_mmfile`（从磁盘文件，[:158](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.c#L158)）和 `read_mmblob`（从对象库按 OID 读 blob，[:179](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.c#L179)）。`read_mmblob` 对 null OID（新增/删除侧）返回空串，使「单边变更」也能用同一套 diff 路径。还有 `buffer_is_binary`（[:198](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.c#L198)）：在前 8000 字节里找 NUL 字节，命中即判为二进制——这是 git 决定「该不该走文本 diff 还是显示 Binary files differ」的依据。

**(e) funcname 正则注入** —— `xdiff_set_find_func`（[:250-284](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.c#L250-L284)）把 `diff.<driver>.xfuncname` 配置的正则编译进一个 `ff_regs` 结构，挂到 `xecfg->find_func`。它支持多行（以 `\n` 分隔）和取反（`!` 前缀）——这是 `git diff` 能在 hunk 头显示 `@@ ... @@ static void foo(void)` 这种函数名的底层机制（具体正则来自下一模块的 userdiff）。

#### 4.2.4 代码实践

**实践目标**：追踪一次 `git diff` 从 `diff.c` 到 `xdiff` 真身的完整调用链，确认「桥」的位置与职责。

**操作步骤**（纯源码阅读型实践，不运行命令）：

1. 打开 [diff.c:4135](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/diff.c#L4135)，看到 `xdi_diff_outf(&mf1, &mf2, NULL, fn_out_consume, ...)`——这里 `mf1/mf2` 就是两个 `mmfile_t`，`fn_out_consume` 是「每收到一行就格式化输出」的回调。
2. 跳到 `xdi_diff_outf`（[xdiff-interface.c:133](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.c#L133)），看清它如何把 `fn_out_consume` 包进 `xdiff_emit_state`、再把 `xdiff_outf` 注册为 `ecb.out_line`。
3. 跟到 `xdi_diff`（[:119](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.c#L119)），确认它过 `MAX_XDIFF_SIZE` 闸、做 `trim_common_tail`，然后调 `xdl_diff`（进入 `xdiff/` 库）。
4. 最后在 [xdiffi.c:1088](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L1088) 的 `xdl_diff` 里看到 `ef`（emit function）默认是 `xdl_emit_diff`，它内部会回调 `ecb->out_line`——也就是回到桥层的 `xdiff_outf`，闭环。

**需要观察的现象**：数据流形成一个「git → 桥 → xdiff → 桥 → git」的环。git 侧只提供「内容」和「每行/每 hunk 的回调」，xdiff 侧只提供「算法」，桥负责两边语义对齐。

**预期结果**：你能画出这条调用链，并指出「行装配（拼半行）」发生在桥层的 `xdiff_outf`，而「算法（找差异）」发生在 `xdiff/xdiffi.c`，两者职责清晰分离。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `xdi_diff` 在「带上下文行（`-U` 非零或函数上下文）」时跳过 `trim_common_tail`？

> **答案**：`trim_common_tail` 会把两文件相同的尾部从比较范围里删掉。但带上下文时，那些尾部公共行可能需要作为「上下文」显示在 hunk 里；如果先裁掉，xdiff 就看不到它们、无法输出 context，导致 patch 不完整。所以仅当「完全不要上下文」时才允许裁。

**练习 2**：`xdiff_outf` 里的 `remainder` 这个 `strbuf` 解决了什么问题？如果没有它会怎样？

> **答案**：xdiff 的输出回调可能一次给「半行」（缓冲区不以 `\n` 结尾）。`remainder` 把这半行暂存，等下一次回调拼上剩余部分凑成整行，再交给 `line_fn`。没有它的话，git 侧的 `line_fn` 会收到不完整的行，`@@ ... @@` 头、`+`/`-` 前缀等都会错位。

**练习 3**：`MAX_XDIFF_SIZE` 命中（文件超大）时 `xdi_diff` 返回 `-1`。git 上层会据此做什么？

> **答案**：上层（如 `diff.c` 的 builtin formatter）会把该文件对当作「无法做文本 diff」，转而输出 `Binary files differ`（或按二进制处理）。这是一道安全闸，避免 xdiff 在吉字节级输入上耗时爆炸。

---

### 4.3 userdiff 文件类型规则

#### 4.3.1 概念说明

xdiff 本身是「语言无关」的——它只把输入当字节行。但 git 的 diff 有几处需要「懂」文件是什么语言：

- **函数名（funcname / hunk 头）**：`git diff` 的 hunk 头 `@@ -a,b +c,d @@ <funcname>` 里那串函数名，是怎么来的？不同语言「什么算一个函数/块的起始行」不一样：C 里是 `void foo(...) {`，Python 里是 `def foo(...):`，TeX 里是 `\section{...}`。
- **word regex（`--word-diff` / `--word-diff-regex`）**：按「词」而非按「行」显示差异时，怎么切词？C 里 `foo_bar` 是一个词，TeX 里 `\section` 是一个词。
- **二进制判定与外部 diff/textconv**：某类文件要不要当二进制？要不要调用外部命令做 diff 或文本转换（如对二进制 `*.png` 用 `git textconv` 转 ASCII 再 diff）？

这些「因文件类型而异」的规则，由 `userdiff.c` 集中管理。核心抽象是 **userdiff driver（驱动）**：每个 driver 有一个名字（如 `"cpp"`、`"python"`）、一个 funcname 正则、一个 word regex、以及若干二进制/textconv/algorithm 字段。git 根据**文件路径**（经 `.gitattributes` 的 `diff` 属性）或**驱动名**选出对应的 driver，再把它的正则注入 xdiff。

> 术语：**driver（驱动）** = 一组「如何 diff 这种文件」的规则。**funcname** = hunk 头显示的「当前所在函数/块」名。**textconv** = 把二进制内容转成可 diff 文本的外部命令。

#### 4.3.2 核心流程

userdiff 的运作分四步：

1. **内置驱动表**：`builtin_drivers[]`（[userdiff.c:45-368](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L45-L368)）硬编码了约 30 种语言的规则（ada/bash/cpp/csharp/fortran/golang/java/python/rust/...），每种用 `PATTERNS` 或 `IPATTERN` 宏填好 funcname 与 word regex。
2. **按路径选驱动**：`userdiff_find_by_path`（[:528](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L528)）查 `.gitattributes` 里该路径的 `diff` 属性，得到驱动名（如 `diff=cpp`），再按名查表。
3. **按名查表**：`userdiff_find_by_name`（[:516](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L516)）先查用户自定义驱动（来自 `diff.*` 配置），再查内置表。
4. **配置扩展**：用户可在 `.gitconfig` / `.gitattributes` 里用 `diff.<driver>.xfuncname` / `.wordregex` / `.textconv` / `.algorithm` 等自定义新驱动（`userdiff_config`，[:457](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L457)）。

选出的 driver 的 funcname 正则最终经 4.2 节的 `xdiff_set_find_func` 注入 xdiff，驱动 hunk 头生成。

#### 4.3.3 源码精读

**(a) 内置驱动表与宏** —— 两个宏（[userdiff.c:15-34](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L15-L34)）定义了驱动条目的写法：

```c
#define PATTERNS(lang, rx, wrx) { \
    .name = lang, .binary = -1, \
    .funcname = { .pattern = rx, .cflags = REG_EXTENDED }, \
    .word_regex = wrx "|[^[:space:]]|[\xc0-\xff][\x80-\xbf]+", \
    .word_regex_multi_byte = wrx "|[^[:space:]]", \
}
```

`PATTERNS` 用大小写敏感匹配，`IPATTERN` 额外加 `REG_ICASE`（大小写不敏感，用于 fortran/css/fountain/ada）。注意 word regex 末尾自动拼上 `|[^[:space:]]|[\xc0-\xff][\x80-\xbf]+`——前者兜底「非空白串算一个词」，后者处理 UTF-8 多字节字符（这就是 `word_regex_multi_byte` 的由来，当运行时正则库支持多字节时改用它，见 [:516-526](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L516-L526) 与 [:401-418](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L401-L418) 的 `regexec_supports_multi_byte_chars` 运行时探测）。

表里每条都是「这种语言的函数/块起始行长什么样」。例如 Python（[:323-329](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L323-L329)）：

```c
PATTERNS("python",
     "^[ \t]*((class|(async[ \t]+)?def)[ \t].*)$",     // funcname: class/def 行
     /* -- */
     "[a-zA-Z_][a-zA-Z0-9_]*"                           // word regex: 标识符
     "|[-+0-9.e]+[jJlL]?|0[xX]?[0-9a-fA-F]+[lL]?"
     "|[-+*/<>%&^|=!]=|//=?|<<=?|>>=?|\\*\\*=?"),
```

表末尾是特殊的 `default` 驱动（[:367](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L367)）：无 funcname、无 word regex，什么语言规则都没有——所有未被 `.gitattributes` 指定驱动的文件都用它（即 hunk 头不带函数名）。

**(b) 真/假二进制驱动** —— `driver_true`（`diff=true`，强制按文本 diff）和 `driver_false`（`!diff`，强制按二进制），见 [:372-380](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L372-L380)。它们让 `.gitattributes` 能显式覆盖 git 的自动二进制探测（4.2 节的 `buffer_is_binary`）。

**(c) 按路径选驱动** —— `userdiff_find_by_path`（[:528-546](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L528-L546)）：

```c
git_check_attr(istate, path, check);          // 查 .gitattributes 的 diff 属性
if (ATTR_TRUE(check->items[0].value))  return &driver_true;   // diff=true
if (ATTR_FALSE(check->items[0].value)) return &driver_false;  // !diff
if (ATTR_UNSET(check->items[0].value)) return NULL;           // 未设置 → 用 default
return userdiff_find_by_name(check->items[0].value);          // diff=cpp 等
```

这里 `git_check_attr` 承接 git 的属性系统（u6 的配置体系相关）：`.gitattributes` 里一行 `*.c diff=cpp` 就会让所有 `.c` 文件用 cpp 驱动。注意它把 `istate`（索引）作为参数——属性查找需要索引上下文（u4-l1）。

**(d) 配置自定义驱动** —— `userdiff_config`（[:457-514](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L457-L514)）解析所有 `diff.<name>.<type>` 配置项。它用 `parse_config_key` 把键拆成 `name` 与 `type`，按 `type` 分派：

| 配置键后缀 | 作用 | 对应字段 |
|---|---|---|
| `xfuncname` | funcname 正则（扩展正则） | `funcname.pattern`（`REG_EXTENDED`） |
| `funcname` | funcname 正则（基础正则） | `funcname.pattern` |
| `wordregex` | `--word-diff` 切词正则 | `word_regex` |
| `binary` | 是否二进制（auto/true/false） | `binary` |
| `command` | 外部 diff 命令 | `external.cmd` |
| `textconv` | 二进制→文本转换命令 | `textconv` |
| `algorithm` | 该类型文件专用 diff 算法 | `algorithm` |

若驱动名是新的（内置表里没有），就在动态 `drivers[]` 数组里 `ALLOC_GROW` 新建一个（[:467-473](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L467-L473)）。查找时先查自定义、再查内置（`for_each_userdiff_driver`，[:583-599](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L583-L599)），所以用户配置可覆盖同名内置驱动。

**(e) textconv 缓存** —— `userdiff_get_textconv`（[:548-566](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L548-L566)）：若驱动设了 `cachetextconv`，把转换结果存进一个 notes 缓存（`notes_cache`，名字形如 `textconv/<driver>`），避免对同一二进制对象反复跑外部转换命令。

#### 4.3.4 代码实践

**实践目标**：亲手让 `git diff` 显示函数名 hunk 头，并验证是 userdiff 驱动在起作用。

**操作步骤**（在一个临时 git 仓库里）：

1. 建仓并写一个 Python 文件：
   ```
   cd /tmp && rm -rf ud-test && mkdir ud-test && cd ud-test
   <你的 git>/git init -q
   printf 'def foo():\n    return 1\n\ndef bar():\n    return 2\n' > m.py
   <你的 git>/git add m.py && <你的 git>/git commit -q -m init
   ```
2. 修改 `bar` 函数体，制造一个改动：
   ```
   printf 'def foo():\n    return 1\n\ndef bar():\n    return 999\n' > m.py
   ```
3. 看 diff，注意 hunk 头：
   ```
   <你的 git>/git diff
   ```
4. 对比：对同样改动，强制指定一个不存在的驱动（funcname 失效，hunk 头不再带函数名）：
   ```
   printf 'm.py diff=notarealdriver\n' > .gitattributes
   <你的 git>/git diff
   ```
5. 阅读源码印证：`m.py` 默认匹配 [userdiff.c:323](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L323) 的 python 驱动（funcname 为 `^[ \t]*((class|(async[ \t]+)?def)[ \t].*)$`），所以 `def bar` 会出现在 hunk 头。

**需要观察的现象**：第 3 步的 hunk 头形如 `@@ -4,3 +4,3 @@ def bar()`——末尾带 `def bar()`；第 4 步因驱动不存在（回退到 `default`，无 funcname），hunk 头变成裸的 `@@ -4,3 +4,3 @@`。

**预期结果**：确认 hunk 头的函数名来自 userdiff 驱动的 funcname 正则；驱动由 `.gitattributes` 的 `diff=` 属性选择，查不到则回退 `default`。

**如果无法确定运行结果**：第 4 步中「不存在的驱动名」是否报错取决于 git 是否要求该驱动已定义——若 git 仅警告并回退，hunk 头就丢失 funcname。具体行为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：一个全新语言（假设叫 `zyx`）的源文件，如何让 `git diff` 对它显示正确的函数名 hunk 头，而不改 git 源码？

> **答案**：在仓库根写 `.gitattributes`：`*.zyx diff=zyx`；再在 `.gitconfig` 写 `[diff "zyx"] xfuncname = "^\\s*(function|class)\\s+.*$"`（按 zyx 语法定义正则）。`userdiff_config` 会解析这些配置生成自定义驱动，`userdiff_find_by_path` 经属性系统选中它，funcname 正则经 `xdiff_set_find_func` 注入 xdiff。全程无需改源码。

**练习 2**：`driver_true` 和 `driver_false` 各自的 `binary` 字段是 0 和 1，而内置语言驱动是 -1。这三个值分别意味着什么？

> **答案**：`-1` 表示「未知/自动」——交给 `buffer_is_binary` 在前 8000 字节找 NUL 自动判定；`0`（`diff=true`）强制按文本 diff；`1`（`!diff`）强制按二进制处理。它们让 `.gitattributes` 能显式覆盖自动探测，例如对某个被误判的文本文件强制 `diff=true`。

**练习 3**：为什么 word regex 末尾要自动拼上 `[\xc0-\xff][\x80-\xbf]+`？

> **答案**：这是 UTF-8 多字节字符的字节模式（首字节 0xC0–0xFF、续字节 0x80–0xBF）。拼上它后，`--word-diff` 在切词时能把一个完整的多字节字符（如中文）当一个词，而不是按字节切碎。运行时若正则库支持多字节（`regexec_supports_multi_byte_chars` 探测为真），则改用 `word_regex_multi_byte` 版本。

---

## 5. 综合实践

把三个模块串起来，做一次「端到端」的 diff 行为探究。目标：用同一个改动样本，对比四种算法 + 缩进启发式的输出差异，并能在源码里指认每一步发生在哪个函数。

**准备**（临时仓库）：

```
cd /tmp && rm -rf xdiff-lab && mkdir xdiff-lab && cd xdiff-lab
<你的 git>/git init -q
# 一个有缩进结构、含重复行的 C 风格文件，便于观察 indent heuristic 与算法分歧
cat > f.c <<'EOF'
void f(void) {
    int i;
    for (i = 0; i < 3; i++) {
        printf("x");
    }
}
EOF
<你的 git>/git add f.c && <你的 git>/git commit -q -m init
# 改动：在循环体里加一行
cat > f.c <<'EOF'
void f(void) {
    int i;
    for (i = 0; i < 3; i++) {
        printf("x");
        printf("y");
    }
}
EOF
```

**任务**：

1. 跑五种变体，分别保存输出：
   ```
   <你的 git>/git diff                       > myers.txt
   <你的 git>/git diff --minimal             > minimal.txt
   <你的 git>/git diff --patience            > patience.txt
   <你的 git>/git diff --histogram           > histogram.txt
   <你的 git>/git diff --no-indent-heuristic > noindent.txt
   ```
2. 用 `diff myers.txt histogram.txt` 等对比，找出哪些变体输出不同、差异在 hunk 边界还是增删内容。
3. 回到源码为每个观察结果「定位」：
   - 算法分派点：`xdl_do_diff` 的 `XDF_DIFF_ALG` 分支（[xdiffi.c:324-332](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L324-L332)）。
   - histogram 真身：`xdl_do_histogram_diff` → `histogram_diff` → `find_lcs`（[xhistogram.c:365](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xhistogram.c#L365)）。
   - 缩进启发式：`xdl_change_compact` 的 `XDF_INDENT_HEURISTIC` 分支（[xdiffi.c:876-919](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff/xdiffi.c#L876-L919)）。
   - hunk 头函数名：来自 userdiff 的 cpp 驱动 funcname（[userdiff.c:90](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/userdiff.c#L90)）。
4. 把你的发现整理成一张表：「观察到的输出差异 → 对应的源码位置 → 起作用的标志位」。

**验收**：你能解释清楚「为什么 `--histogram` 与默认在某些样本上 hunk 边界不同」「为什么关掉 indent heuristic 后 hunk 可能落在更别扭的位置」，并能在源码里指出对应的函数与行号。具体输出「待本地验证」。

## 6. 本讲小结

- **行级 diff = 找 LCS/SES**：xdiff 解决的数学问题是「在两个行序列间找最长公共子序列」，等价于「最短编辑脚本」，编辑距离 \(D = N+M-2|LCS|\)；Myers 的复杂度是 \(O((N+M)D)\)，与编辑距离成正比，所以在「文件几乎相同」时极快。
- **预处理是统一的货币**：`xprepare.c` 把字节切成行记录、按内容归类并赋 `minimal_perfect_hash`，使「比较两行」退化为「比较两个整数」；还做无匹配行剔除与首尾裁剪缩小规模。
- **Myers 用分治 + 对角线扫描**：`xdl_recs_cmp` 分治，`xdl_split` 从盒子两端沿对角线推进直到交汇；`XDF_NEED_MINIMAL` 关闭启发式剪枝以追求绝对最小，`mxcost` 是提前收尾的代价上限。
- **xdiff-interface 是翻译桥**：`xdi_diff` 加大小安全闸与尾部裁剪后转交 `xdl_diff`；`xdiff_outf` 用 `remainder` 把 xdiff 的字节缓冲拼成完整行；funcname 正则在此注入。
- **userdiff 管「语言规则」**：内置约 30 种语言的 funcname/word-regex 驱动表，由 `.gitattributes` 的 `diff=` 属性按路径选择，查不到回退 `default`；用户可用 `diff.<driver>.*` 配置自定义。
- **算法与启发式都是可选项**：默认 Myers、`--minimal`、`--patience`、`--histogram` 四种算法，加上默认开启的 `--indent-heuristic`，共同决定 diff 的最终形态——它们在 `xpparam_t.flags` 的位里编码，在 `xdl_do_diff` 处分派。

## 7. 下一步学习建议

- **向上：把 diff 接进版本遍历**。本讲只讲「一对文件」的行级差异。结合 u7-l1（revision walk）与 u8-l1（diffcore），看 `builtin/log.c`、`log-tree.c` 如何在历史遍历中对每个提交调用 diffcore→xdiff，输出 `git log -p`。
- **横向：合并如何复用 xdiff**。`xdiff/xmerge.c` 在 xdiff 算出的差异之上做三方合并，u10-l1（merge-ort）会用到；`xdiff-interface.c` 里的 `git_xmerge_config` / `parse_conflict_style_name`（[:312](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/xdiff-interface.c#L312)）是 `merge.conflictstyle`（merge/diff3/zdiff3）的入口，值得顺藤摸瓜。
- **深入：patience 与 histogram 的细节**。本讲对 `xpatience.c`、`xhistogram.c` 只点到为止，可精读 `find_lcs` 的「出现次数最少优先」策略，理解它为何在「大段重复行」场景比 Myers 更稳。
- **实践：自定义一个 userdiff 驱动**。按 4.3.5 练习 1，为一个新语言配 funcname 正则，用 `git diff` 验证 hunk 头，体会「不改源码也能扩展 git」的属性系统力量（与 u6 配置体系呼应）。
