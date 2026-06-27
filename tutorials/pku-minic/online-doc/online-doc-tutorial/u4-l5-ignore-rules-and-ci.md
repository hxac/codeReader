# u4-l5 忽略规则、主流程编排与 CI 集成

## 1. 本讲目标

本讲是链接检查器四连讲的**收官篇**，也是整个第四单元的收口。前三讲分别解决了三个子问题：[u4-l2](u4-l2-link-extraction.md) 把链接提取出来、[u4-l3](u4-l3-docsify-routing-and-local-check.md) 校验站内链接、[u4-l4](u4-l4-async-remote-check.md) 异步校验远程链接。但真实文档里总会有些「明知有问题、但就是想放过」的链接——比如示例仓库故意 404、或某个第三方站点对脚本返回 403。本讲要回答最后两个问题：

1. **怎么「有选择地」放过某些链接？**——忽略规则系统。
2. **这套检查器是怎么被自动跑起来的？**——主流程编排 + GitHub Actions CI。

学完后你应该能够：

1. 读懂 `IgnoreRule` 的**合取匹配**：一条规则上的多个字段必须**同时**满足才算命中；并能区分「链接级字段」和「问题级字段」。
2. 解释为什么过滤要分**两层**——链接层（校验前）和问题层（校验后）——以及它们各自解决什么问题。
3. 把 `check_links` 主流程的完整编排（扫描 → 提取 → 过滤 → 本地/远程校验 → 过滤 → 统计）和退出码（`0`/`1`/`2`）串成一张大图。
4. 看懂 `.github/workflows/check-links.yml` 如何在 PR / push 时自动触发检查，并能准确说出 **CI 在什么条件下会失败**。

---

## 2. 前置知识

进入源码前，先通俗地过一遍本讲依赖的几个概念。

### 2.1 合取（AND）vs 析取（OR）

- **合取（conjunction，逻辑与）**：多个条件**全部**成立，整体才成立。相当于「条件 A **且** 条件 B **且** 条件 C」。
- **析取（disjunction，逻辑或）**：多个条件**任一**成立，整体就成立。相当于「条件 A **或** 条件 B」。

本讲的忽略规则是**合取**的：一条规则上填了几个字段，这几个字段必须**同时**命中才生效。而「多条规则之间」是**析取**的：只要**任意一条**规则命中，这个链接/问题就被忽略。记住这个「字段内合取、规则间析取」的总原则，后面源码就很好读了。

### 2.2 「链接」与「问题」是两个阶段的东西

回顾 [u4-l1](u4-l1-linkchecker-config-and-entry.md) 引入的两个核心数据类：

- **`Link`**：文档里**提取出来的一条链接**，带来源文件、行号、列号、URL。它代表「这里有一个链接」，**还不知道它是不是坏的**。
- **`Issue`**：把一条 `Link` 校验之后，**发现它有问题**时产生的记录，带 `category`（如 `local-missing-target`、`remote-http`）和可选的 `status`（HTTP 状态码）。

关键区别：`Link` 在「校验之前」就存在；`Issue` 在「校验之后」才产生。本讲的忽略规则可以作用在**这两个阶段**，分别叫「链接层过滤」和「问题层过滤」——这是本讲最核心的设计。

### 2.3 退出码（exit code）

命令行程序结束时返回一个整数给操作系统，叫退出码。约定俗成：

- `0` 表示**成功**（一切正常）。
- **非零**表示**失败**，具体数值可用来区分失败种类。

本检查器用 `0`/`1`/`2` 三个值，CI 正是靠这个退出码判断「这次检查通过没有」。

### 2.4 GitHub Actions 的最小心智模型

GitHub Actions 是 GitHub 内置的自动化平台。它的核心概念：

- **workflow（工作流）**：一个 `.github/workflows/*.yml` 文件就是一个工作流。
- **触发条件（`on`）**：什么时候跑——比如「有 PR 时」「push 到 master 时」「手动点按钮时」。
- **job / step**：工作流里包含若干 job，每个 job 在一台虚拟机（`runs-on`）上按顺序跑若干 step；**任何一个 step 的命令返回非零退出码，这个 job 就判为失败**，PR 上会亮红叉。

本讲的 CI 就是一个极简单的工作流：拉代码 → 装依赖 → 跑检查器。检查器返回非零，CI 就红。

---

## 3. 本讲源码地图

本讲围绕三个文件：

| 文件 | 作用 |
|------|------|
| [scripts/check_links.py](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py) | 检查器主体；本讲关注 `IgnoreRule`、两个 `filter_*` 函数、主流程 `check_links` 与入口 `main` |
| [check-links.toml](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/check-links.toml) | 配置文件；本讲关注其中的 `[[ignore]]` 规则段 |
| [.github/workflows/check-links.yml](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml) | CI 工作流；定义何时跑、怎么跑、何时算失败 |

本讲涉及的函数 / 类清单（均在 `scripts/check_links.py` 内）：

| 名称 | 行号 | 职责 |
|------|------|------|
| `IgnoreRule` | L150–L183 | 忽略规则数据类 + 合取匹配方法 `matches` |
| `load_ignore_rules` | L224–L243 | 把 TOML 里的 `[[ignore]]` 解析成 `IgnoreRule` 列表 |
| `filter_ignored_links` | L649–L650 | **链接层**过滤（校验前） |
| `filter_ignored_issues` | L653–L654 | **问题层**过滤（校验后） |
| `check_links` | L657–L697 | 主流程编排：扫描→提取→过滤→本地/远程校验→过滤→统计 |
| `main` | L711–L737 | 命令行入口，决定退出码 `0`/`1`/`2` |

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块，按「自顶向下」的顺序讲：先用 **4.1 主流程编排** 把全局管线画出来，让你看到忽略规则和双层过滤在整条流水线里的位置；再用 **4.2 IgnoreRule 合取匹配**钻进规则本身；接着用 **4.3 双层过滤**解释规则被应用两次的设计意图；最后用 **4.4 CI 触发与判定**把整套东西接到 GitHub Actions 上自动运行。

### 4.1 主流程编排与统计

#### 4.1.1 概念说明

前三讲我们分别看了「提取」「本地校验」「远程校验」三个零件。本模块把这些零件**串成一条流水线**，回答：检查器从头到尾到底按什么顺序做事？忽略规则和过滤插在哪个环节？最后怎么统计、怎么退出？

把主流程想成一个数据处理管线：**输入是一堆文档文件，输出是一份坏链清单 + 一张统计表**。中间每一步要么在「筛掉不该看的」，要么在「真的去检查」。

#### 4.1.2 核心流程

`check_links` 的执行顺序可以用下面这张流程图式的伪代码概括：

```
输入: root（仓库根）, config（已合并的配置）, no_http, verbose
─────────────────────────────────────────────
1. 建 FileTypes              # 知道哪些扩展名算 md / html
2. discover_files            # 扫描 scan.paths，排除 exclude，得到文件列表
3. load_ignore_rules         # 把 [[ignore]] 解析成 IgnoreRule 列表
4. extract_links             # 从每个文件提取链接 → Link 列表
   → filter_ignored_links    # 【链接层过滤】校验之前先剔除被忽略的链接
5. for link in 剩余链接:
      - should_skip(mailto/javascript/...) → 跳过
      - 远程链接 → 攒进 remote_links 字典（按 normalized_url 去重），稍后批量异步校验
      - 本地链接 → 立即 check_local_link → 产出 Issue
6. 若有远程链接且未 --no-http:
      asyncio.run(check_remote_links(...))  # 并发校验，产出 Issue
7. filter_ignored_issues     # 【问题层过滤】校验之后再剔除被忽略的问题
8. 统计 stats（文件数 / 链接数 / 远程数 / 问题数）
9. 返回 (排序后的 issues, stats)
─────────────────────────────────────────────
输出: issues 列表 + stats 字典
```

注意两个过滤步骤（步骤 4 的链接层、步骤 7 的问题层）夹着「校验」这件事——这正是本讲反复强调的「双层过滤」结构。

#### 4.1.3 源码精读

主流程全部在 [check_links](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L657-L697) 这一个函数里。逐段看：

**准备阶段（L658–L666）**：构建文件类型、发现文件、加载忽略规则、定好 `docs_root`。

```python
file_types = configured_file_types(config)
files = discover_files(root, config, file_types)
ignore_rules = load_ignore_rules(config)
```

- [check_links.py:658-660](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L658-L660)：依次建立 `FileTypes`、扫描出所有 `.md`/`.html` 文件（排除 `docs/assets/**`，见 [u4-l1](u4-l1-linkchecker-config-and-entry.md)）、把 `[[ignore]]` 解析成规则列表。

**提取 + 链接层过滤（L661–L662）**：这是**第一层过滤**发生的地方——先提取，再立刻把被忽略的链接剔掉。

- [check_links.py:661-662](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L661-L662)：`extract_links` 的结果直接喂给 `filter_ignored_links`。被忽略的链接在这一步就**消失**了，后面的校验循环根本看不见它们——既不会发本地校验，也不会进 `remote_links`，**省掉了无用功**。

**校验循环（L670–L683）**：逐条处理（过滤后剩下的）链接。

- [check_links.py:670-683](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L670-L683)：三类分支——`should_skip`（`mailto:`/`javascript:` 等非 HTTP 方案，见 [Link.should_skip](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L107-L109)）直接跳过；远程链接用 `setdefault` 按 `normalized_url` 攒进字典（**同一 URL 只校验一次**，但记住所有出处，见 [u4-l4](u4-l4-async-remote-check.md)）；本地链接立即调 `check_local_link`，有问题就追加 `Issue`。

**远程批量校验（L685–L687）**：本地循环跑完，再统一异步校验远程链接。

- [check_links.py:685-687](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L685-L687)：只有「确有远程链接且未指定 `--no-http`」时才发起网络请求，用 `asyncio.run` 驱动 [check_remote_links](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L613-L646)。产出的远程 `Issue` 与本地 `Issue` 合并。

**问题层过滤 + 统计（L689–L697）**：这是**第二层过滤**和收尾。

- [check_links.py:689](https://github.com/pku-minic/online-doc/blob/d172f8994fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L689)：所有校验都跑完后，对 `Issue` 再过一遍忽略规则。这一层能用上 `category`/`status` 等「问题级字段」（因为此时 `Issue` 已经存在）。
- [check_links.py:690-696](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L690-L696)：统计 `files`/`links`/`remote_links`/`unique_remote_links`/`issues`。**注意两个口径**：`links` 是**链接层过滤之后**的链接数（被忽略的链接不计入）；`issues` 是**问题层过滤之后**的坏链数（被忽略的问题不计入）。
- [check_links.py:697](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L697)：按 `(来源文件, 行号, URL)` 排序后返回，让输出稳定可读。

**退出码（main 函数）**：主流程只返回数据和统计，**真正决定退出码的是 `main`**。

- [main:725-727](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L725-L727)：`load_config`（可能抛 `ValueError`）与 `check_links` 在同一个 `try` 里。
- [main:728-730](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L728-L730)：配置错误（如配置文件不存在）→ 打到 `stderr`，**返回 `2`**。
- [main:737](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L737)：`return 1 if issues else 0`——**有坏链返回 `1`，没有返回 `0`**。

于是退出码三态非常清晰：

| 退出码 | 含义 | 触发条件 |
|--------|------|----------|
| `0` | 通过 | 没有任何 Issue（坏链数为 0） |
| `1` | 有坏链 | 至少一条 Issue 没被忽略规则消除 |
| `2` | 配置错误 | `load_config` 抛 `ValueError`（如配置文件找不到） |

> 提示：`2` 和 `1` 对 CI 来说都是「非零」，都会让 CI 失败。区分它们主要是给人看——配置错误是「你把检查器本身配错了」，有坏链是「文档里的链接坏了」。

#### 4.1.4 代码实践

**目标**：跑一次完整的主流程，亲眼看到「双层过滤」和统计数字，并验证退出码。

**操作步骤**：

1. 先确认依赖已装（见 [u4-l2](u4-l2-run-locally.md)）：`python -m pip install -r requirements.txt`。
2. 跑离线版（跳过远程，省时间、不联网）：
   ```
   python scripts/check_links.py --config check-links.toml --no-http --verbose
   ```
3. 末尾会打印一行统计，类似 `Checked N file(s), M link(s), 0 remote occurrence(s) (0 unique).`。
4. 看一下这条命令的退出码（紧跟在命令后执行）：
   ```
   echo $?
   ```

**需要观察的现象**：

- `--verbose` 会逐条打印 `Checking local link: ... PASS`，最后给统计。
- 因为仓库当前没有坏链，`No broken links found.`，退出码应为 `0`。

**预期结果**：退出码 `0`，统计里的 `issues` 为 0。

> 待本地验证：具体文件数、链接数随仓库当前内容变化，以你本地的输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果 `check_links` 把「链接层过滤」（L661–L662）删掉，只保留「问题层过滤」（L689），功能上还等价吗？为什么作者要写两层？

> **答案**：**结果上**基本等价（最终 Issue 清单差不多），但**性能和行为有差异**。链接层过滤在校验**之前**剔除链接，意味着被忽略的远程链接**根本不会发起 HTTP 请求**——这对像 `kira-cpp` 这种「明知 404」的链接很重要：既省一次网络往返，又避免给目标服务器添麻烦。若只剩问题层过滤，被忽略的远程链接仍会被真实请求一次，只是请求完再丢弃。所以两层各有侧重：链接层「省事」，问题层「能按结果忽略」。

**练习 2**：`stats['links']` 统计的是「提取出来的全部链接」还是「过滤之后的链接」？

> **答案**：**过滤之后**的。因为 [L661–L662](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L661-L662) 把 `filter_ignored_links` 的结果赋给了 `links`，而 [L692](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L692) 统计的是这个变量。所以被忽略规则命中的链接不会出现在 `links` 计数里。

---

### 4.2 IgnoreRule 合取匹配

#### 4.2.1 概念说明

上一模块我们看到「过滤」会调用每条 `IgnoreRule` 的 `matches` 方法。本模块钻进这条规则本身：**一条规则长什么样、用什么字段描述「我想忽略谁」、多个字段之间是什么逻辑关系**。

一句话总结本模块的核心：**一条规则上填的所有字段，必须同时命中，这条规则才算匹配（合取）；但只要任意一条规则匹配，目标就被忽略（规则间析取）**。

`IgnoreRule` 一共支持 8 个匹配字段，分成两组：

| 组别 | 字段 | 含义 | 匹配方式 |
|------|------|------|----------|
| 链接级（有 `Link` 就能判） | `url` | 完整 URL 精确相等 | `==`，额外比对 `normalized_url` |
| | `url_regex` | URL 正则 | `re.search` |
| | `path` | 来源文件路径精确相等 | `==`（仓库相对 posix 路径） |
| | `path_glob` | 来源文件 shell 通配 | `fnmatch`（`*`/`?`/`[...]`） |
| | `line` | 来源行号精确相等 | `==` |
| 问题级（需要 `Issue`） | `category` | 问题类别（`local-missing-target` 等） | `==` |
| | `status` | 单个 HTTP 状态码 | `==` |
| | `statuses` | 一组状态码 | `in` |

「链接级」字段只要有 `Link` 就能判断；「问题级」字段必须等到校验产生 `Issue` 之后才有意义（因为 `category` 和 `status` 是 `Issue` 的属性）。

#### 4.2.2 核心流程

`matches(link, issue=None)` 的判定逻辑（合取）：

```
对每个「已填写（非 None）」的字段，逐一检查：
    如果该字段的条件 不满足 → 立即 return False（这条规则不匹配）
    （未填写的字段 = 不施加约束，跳过）

如果跑到这里都没 return False：
    若 issue 为 None（即只给了 Link，在链接层过滤）：
        return True   # ← 注意：问题级字段在此根本不会被检查！
    否则继续检查问题级字段（category/status/statuses），任一不满足 return False

全部通过 → return True（规则匹配）
```

这里有一个**极其关键、也最容易踩坑**的细节：当 `issue is None`（也就是在链接层过滤、只有 `Link` 没有结果时），方法在 [L175–L176](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L175-L176) **直接 `return True`**，**完全跳过了 `category`/`status`/`statuses` 这三个问题级字段**。这意味着：在链接层，一条「只填了问题级字段」的规则会命中**所有**链接。我们会在 4.3 和练习里专门讨论它的后果。

#### 4.2.3 源码精读

规则数据类与匹配方法全在 [IgnoreRule](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L150-L183)：

**字段定义（L151–L160）**：除了 `reason`（纯说明文本，不参与匹配），其余 8 个都是可选匹配字段，默认 `None`（`statuses` 默认空元组）。

- [check_links.py:151-160](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L151-L160)：`reason: str = ''` 是给人看的注释；`url`/`url_regex`/`path`/`path_glob`/`line`/`category`/`status` 默认 `None`，`statuses` 默认 `()`。

**匹配方法 `matches`（L162–L183）**——逐字段看合取：

- [check_links.py:163-165](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L163-L165)（`url`）：精确比较，且**同时比对** `link.url` 与 `link.normalized_url`。后者把协议相对的 `//host/...` 补成 `https://host/...`（见 [Link.normalized_url](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L99-L101)），所以规则里写 `https://...` 或 `//...` 都能命中。不满足任一形式才返回 `False`。
- [check_links.py:166-168](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L166-L168)（`url_regex`）：用 `re.search` 在 `link.url` **和** `normalized_url` 上各试一次，任一命中即通过——比 `url` 灵活，能匹配一类 URL（如某域名下所有链接）。
- [check_links.py:169-170](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L169-L170)（`path`）：`rel_path = link.source.as_posix()`，即链接来源文件的**仓库相对 posix 路径**（如 `docs/misc-app-ref/examples.md`），与规则里的 `path` 精确相等。
- [check_links.py:171-172](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L171-L172)（`path_glob`）：用标准库 `fnmatch.fnmatch` 做 shell 风格通配，如 `docs/lv1-main/**` 匹配某章节所有文件。
- [check_links.py:173-174](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L173-L174)（`line`）：行号精确相等，配合 `path` 可精确定位「某文件某行的某个链接」。
- [check_links.py:175-176](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L175-L176)（**关键早返回**）：`if issue is None: return True`。链接层过滤走到这里说明所有链接级字段都通过了，直接判定匹配——**问题级字段在此被完全跳过**。
- [check_links.py:177-182](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L177-L182)（问题级字段）：只有在 `issue` 非 `None`（问题层过滤）时才检查 `category`/`status`/`statuses`，任一不满足返回 `False`。

**规则如何从 TOML 加载（load_ignore_rules，L224–L243）**：

- [check_links.py:224-243](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L224-L243)：遍历 `config['ignore']`（一个列表，对应 TOML 里多条 `[[ignore]]`）；校验每条必须是字典；把 `statuses`（TOML 数组）转成元组；其余字段用 `item.get(...)` 取，缺省即 `None`。每条 `[[ignore]]` → 一个 `IgnoreRule`。

**现成的真实样例（kira-cpp）**：仓库里已经有一条忽略规则，正好是合取匹配的最小例子——只填了一个链接级字段 `url`。

- [check-links.toml:21-23](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/check-links.toml#L21-L23)：`url = "https://github.com/pku-minic/kira-cpp"`，配一句 `reason` 解释为什么忽略。
- 它的来源是 [docs/misc-app-ref/examples.md:6](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/misc-app-ref/examples.md#L6)，那行文档**自己就写明**「(暂未完成, 404 是正常现象)」——这正是需要忽略规则的典型场景：链接「客观上是坏的」，但「主观上我们就是要保留它」。
- [check-links.toml:18-20](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/check-links.toml#L18-L20)：配置文件里这段注释明确写了「Ignore rules are conjunctive」（忽略规则是合取的），并列出全部支持字段——和我们源码读到的一致。

#### 4.2.4 代码实践

**目标**：亲手加一条 `url` 型忽略规则（照搬 `kira-cpp` 的写法），验证它能消除一条「坏链」报告，并观察到它是在**链接层**就被剔除的。

**操作步骤**：

1. 先「制造」一条可复现的本地坏链（用本地路由，无需联网）。在 `docs/` 下新建临时文件 `docs/_ignore_practice.md`：
   ```markdown
   # 忽略规则练习

   这是一个故意写错的本地链接：[不存在的页面](/lv99-totally-fake/)
   ```
2. 先**不加**任何忽略规则，跑一次：
   ```
   python scripts/check_links.py --config check-links.toml --no-http --verbose
   ```
   记下输出里 `/lv99-totally-fake/` 的 `local-missing-target` 报错，以及末尾统计的 `link(s)` 数字和退出码（应为 `1`）。
3. 在 [check-links.toml](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/check-links.toml) 末尾追加一条规则（模仿 `kira-cpp`）：
   ```toml
   [[ignore]]
   url = "/lv99-totally-fake/"
   reason = "练习：忽略本地练习文件里故意写错的链接"
   ```
4. 再跑一次同样的命令。

**需要观察的现象**：

- 第 4 步：`/lv99-totally-fake/` 的报错**消失**，退出码变回 `0`。
- **关键观察**：对比第 2 步和第 4 步统计行里的 `Checked ... link(s)` 数字——第 4 步的链接数应该比第 2 步**少 1**（或更多，取决于你练习文件里有几个链接）。这说明该链接是在**链接层过滤（校验之前）**就被剔掉的，根本没有进入校验循环。如果你给它换成远程 URL，它同样**不会发起 HTTP 请求**。

**预期结果**：加上 `url` 规则后，该坏链不再出现，统计的 `links` 计数下降，退出码归 `0`。

**清理（重要）**：练习结束后删除 `docs/_ignore_practice.md`，并把 `check-links.toml` 里你加的规则删掉，最后 `git diff` 确认没有残留改动（这些只是练习，**不要提交**）。

> 待本地验证：链接计数的具体数值取决于你仓库当前内容，以本地输出为准。

#### 4.2.5 小练习与答案

**练习 1**：假设你写了一条只有 `category = "remote-http"`、其余字段都不填的规则，它在「链接层过滤」时会发生什么？

> **答案**：**会命中所有链接**。因为所有链接级字段（`url`/`url_regex`/`path`/`path_glob`/`line`）都是 `None`（不施加约束），走到 [L175](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L175) 时 `issue is None` 成立，直接 `return True`。`category` 这个问题级字段根本没机会被检查。后果是：所有链接在链接层就被全部忽略，检查器相当于什么都没查。**结论：问题级字段（`category`/`status`/`statuses`）一定要搭配至少一个链接级字段（通常是 `url` 或 `path`）来「收窄」范围，否则很危险。**

**练习 2**：`url` 和 `url_regex` 都能匹配 URL，为什么源码里 `url` 要同时比对 `link.url` 和 `link.normalized_url` 两个值？

> **答案**：因为本仓库里同一目标可能写成两种形式——协议相对的 `//github.com/...` 或完整的 `https://github.com/...`。`normalized_url`（[L99–L101](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L99-L101)）把 `//` 开头补成 `https:`，于是规则里写 `https://...` 就能同时命中这两种写法。`url_regex` 同理也在两个值上各 `re.search` 一次。

---

### 4.3 链接/问题双层过滤

#### 4.3.1 概念说明

4.1 我们看到主流程里调了两次过滤，4.2 我们读懂了单条规则。本模块把两者接起来：**为什么要把同一套规则、在两个时机各应用一次？**

答案在于「链接级字段」和「问题级字段」的分工：

- **链接层过滤（校验前）**：手里只有 `Link`，没有校验结果。它只能用链接级字段（`url`/`path`/`line` 等）。它的价值是**「提前剔除，省掉无用校验」**——比如 `kira-cpp`「明知 404」，与其真去请求一次再丢掉，不如压根不查。
- **问题层过滤（校验后）**：手里有了 `Issue`，能用上问题级字段（`category`/`status`/`statuses`）。它的价值是**「按结果忽略」**——比如「所有返回 403 的远程链接都忽略」，这在链接层做不到，因为校验前你根本不知道状态码。

两个 `filter_*` 函数都只有一两行，但它们的**调用时机**不同，决定了它们能用哪些字段、解决哪类问题。

#### 4.3.2 核心流程

两个过滤函数的结构一模一样，差别只在传给 `matches` 的参数：

```
filter_ignored_links(links, rules):
    保留那些「没有任何一条规则 matches(link)        命中」的链接   # 只传 Link

filter_ignored_issues(issues, rules):
    保留那些「没有任何一条规则 matches(issue.link, issue) 命中」的问题 # 传 Link + Issue
```

规则间是**析取**（`any(... for rule in rules)`）：只要**任意一条**规则命中，就忽略；要保留，必须**所有规则都不命中**。

#### 4.3.3 源码精读

两个函数贴在一起对比：

- [filter_ignored_links（L649–L650）](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L649-L650)：`rule.matches(link)`——**只传 `Link`，不传 `Issue`**。所以在这一层，`matches` 内部命中 `if issue is None: return True`，问题级字段全部失效。被命中的链接在校验前就被剔除。
- [filter_ignored_issues（L653–L654）](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L653-L654)：`rule.matches(issue.link, issue)`——**同时传 `Link` 和 `Issue`**。此时 `matches` 会继续检查 `category`/`status`/`statuses`。被命中的 `Issue` 在统计前被剔除。

**两层各自的调用点**（回到 4.1 的主流程）：

- 链接层：[check_links.py:661-662](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L661-L662)，紧跟 `extract_links`，**在所有校验之前**。
- 问题层：[check_links.py:689](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L689)，在本地校验和远程校验**都跑完之后**，统计之前。

**一条规则在两层的表现**（以 `kira-cpp` 的 `url` 规则为例）：

| 时机 | `matches` 收到的参数 | 命中情况 |
|------|----------------------|----------|
| 链接层 | `(link,)`，issue=None | `url` 相等 → 链路级通过 → `issue is None` → `return True` → **链接被剔除，根本不查** |
| 问题层 | 不会到达（链接层已剔除） | — |

可见 `kira-cpp` 这条**链接级**规则实际上只在链接层起作用（而且更高效）。反过来，一条**问题级**规则（如 `url=..., status=403`）则主要在问题层发挥作用——但注意 [4.2.5 练习 1](#425-小练习与答案) 的提醒：只要带了链接级字段 `url`，它在链接层也会提前剔除整个链接，`status` 反而没机会被检查。要让 `status`/`category` 真正生效，需要理解这层交互——这也是为什么现网配置通常用最简单的 `url` 规则。

> 小结：双层过滤 = **链接层用「字段提前剔除」省事 + 问题层用「结果字段」精细忽略**。两套规则共用同一个 `IgnoreRule`，靠 `matches` 里 `issue is None` 的分支自动适配两层。

#### 4.3.4 代码实践

**目标**：亲手验证「问题级字段只在问题层生效」，并对比链接级字段与问题级字段的不同表现。

**操作步骤**：

1. 沿用 4.2.4 创建的 `docs/_ignore_practice.md`（含坏链 `/lv99-totally-fake/`）。先确认没有为它配任何忽略规则，跑一次 `--no-http --verbose`，确认它报 `local-missing-target`。
2. 把忽略规则改成**只用问题级字段**：
   ```toml
   [[ignore]]
   category = "local-missing-target"
   reason = "练习：只填问题级字段"
   ```
3. 跑 `--no-http --verbose`。

**需要观察的现象**：

- 第 3 步：你会看到**整个仓库的所有链接都不再被检查**——统计里 `link(s)` 变成 0（或非常小），`Checking local link:` 行几乎消失。原因正是 [4.2.5 练习 1](#425-小练习与答案)：这条规则没有任何链接级字段约束，在链接层对**每一条**链接都 `return True`，于是所有链接在校验前就被剔光了。这直观展示了「问题级字段不能裸用」。
4. 把规则改成「链接级 + 问题级」合取，收窄到这个文件：
   ```toml
   [[ignore]]
   path = "docs/_ignore_practice.md"
   category = "local-missing-target"
   reason = "练习：用 path 收窄到练习文件"
   ```
5. 再跑一次。

**需要观察的现象（第 5 步）**：

- 这回**只有练习文件里的链接**在链接层被全部剔除（因为 `path` 命中、`issue is None` → `return True`，整个文件的链接都不查了），其它文件的链接照常检查。练习坏链不再报错。注意：因为 `path` 是链接级字段，它在链接层就把该文件**所有**链接剔掉了（`category` 实际没起作用）——这正是「链接级字段会在链接层提前生效」的体现。

**预期结果**：第 3 步全站链接被误删（演示问题级字段裸用的危险）；第 5 步只有目标文件被忽略（演示用链接级字段收窄范围）。两个对比能让你深刻记住「问题级字段必须搭配链接级字段」。

**清理**：删除 `docs/_ignore_practice.md`，还原 `check-links.toml`，`git diff` 确认干净。

> 待本地验证：第 3 步「全站链接被删」是确定会发生的逻辑结果，但具体剩余链接数取决于仓库内容。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `filter_ignored_issues` 传的是 `(issue.link, issue)` 两个参数，而 `filter_ignored_links` 只传 `(link,)` 一个？

> **答案**：因为问题层过滤时 `Issue` 已经存在，把它一并传进去，`matches` 才能检查 `category`/`status`/`statuses` 这些问题级字段（见 [L177–L182](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L177-L182)）。链接层过滤时还没有 `Issue`，只能传 `Link`，于是 `matches` 走 `if issue is None: return True` 早返回，只看链接级字段。

**练习 2**：假设你想「忽略所有返回 429（限流）的远程链接」，应该怎么写规则？它会在哪一层真正生效？

> **答案**：写成 `status = 429`（或 `statuses = [429]`）。但它**必须**搭配一个链接级字段（如 `url_regex = "https://"`）来收窄，否则会在链接层把所有链接误删（见 4.2.5 练习 1）。即便收窄了，只要带了链接级字段，命中链接仍会在**链接层**就被提前剔除（`status` 没机会检查）；要让 `status` 真正在问题层生效，目前的实现下很难单独触发——这也解释了为什么现网 `kira-cpp` 用的是最朴素的 `url` 规则。更稳妥的工程做法是：对 429 这类「按结果忽略」的需求，靠 [check-links.toml](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/check-links.toml#L13) 的 `accepted_statuses = [401, 403, 429]` 直接放行（见 [u4-l4](u4-l4-async-remote-check.md)），而不是靠忽略规则。

---

### 4.4 CI 触发与判定

#### 4.4.1 概念说明

前面三模块讲的都是「检查器这个程序本身」。但检查器如果只能手动跑，就形同虚设——大家改文档时很容易忘了跑。本模块讲怎么把它**接到 GitHub Actions**，让每次 PR、每次 push 都**自动**跑一遍，并用退出码决定 CI 是绿是红。

这是把「检查器能力」变成「团队流程约束」的最后一公里：**退出码 0 → CI 绿 → PR 可合并；退出码非 0 → CI 红 → PR 被拦下**。

#### 4.4.2 核心流程

CI 工作流 [.github/workflows/check-links.yml](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml) 做三件事：

```
触发（on）：
    - pull_request：改了文档相关文件时
    - push 到 master：改了文档相关文件时
    - workflow_dispatch：手动点按钮

job（ubuntu 虚拟机）：
    step 1: actions/checkout      # 拉代码
    step 2: pip install -r ...     # 装依赖
    step 3: python scripts/check_links.py --config check-links.toml --verbose
                                   # 跑检查器（注意：不带 --no-http，远程链接会被真查）
```

**关键点**：第 3 步检查器的退出码就是这一步的退出码。GitHub Actions 规定「step 命令非零退出 → job 失败」。所以：**有坏链（退出码 1）或配置错误（退出码 2）→ CI 红；无坏链（退出码 0）→ CI 绿**。

#### 4.4.3 源码精读

**触发条件（L3–L22）**：

- [check-links.yml:4-11](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml#L4-L11)（`pull_request`）：带 `paths` 过滤器，只有当 PR **改动**了这些文件才触发：`README.md`、`docs/**`、`scripts/check_links.py`、`requirements.txt`、`check-links.toml`、`.github/workflows/check-links.yml`。这意味着：一个只改了 `.gitignore` 的 PR **不会**触发链接检查——因为这些文件与文档链接无关，没必要重跑。
- [check-links.yml:12-21](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml#L12-L21)（`push`）：限定 `branches: master`，且同样的 `paths` 过滤。即合并/直推到 master、且触及文档相关文件时才跑。
- [check-links.yml:22](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml#L22)（`workflow_dispatch`）：支持在 GitHub 页面**手动**触发，方便排查或定期复检。

> 注意：`online-doc-tutorial/`（本套讲义）**不在** `paths` 里——它是生成物/教学材料，不参与文档站渲染，所以改它不会触发链接检查。

**job 与 step（L24–L32）**：

- [check-links.yml:25-26](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml#L25-L26)：job `check-links` 跑在 `ubuntu-latest`。
- [check-links.yml:28](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml#L28)：`actions/checkout@v6` 拉取仓库代码到虚拟机。
- [check-links.yml:29-30](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml#L29-L30)：`python -m pip install -r requirements.txt` 装好 `httpx`/`markdown-it-py`/`selectolax`（见 [u4-l2](u4-l2-run-locally.md)）。
- [check-links.yml:31-32](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml#L31-L32)：**核心一步**——`python scripts/check_links.py --config check-links.toml --verbose`。

**把退出码接到 CI 判定**：

这里命令**没有** `--no-http`，所以 CI 里**远程链接会被真实请求**（这就是 [u4-l4](u4-l4-async-remote-check.md) 那套 HEAD/GET、`accepted_statuses` 真正发力的地方）。结合 [main](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L711-L737) 的退出码：

| 检查器退出码 | 含义 | CI 这一 step / job |
|--------------|------|--------------------|
| `0` | 无坏链 | ✅ 通过（绿） |
| `1` | 有坏链（未被忽略规则消除） | ❌ 失败（红） |
| `2` | 配置错误（如 `check-links.toml` 写错/找不到） | ❌ 失败（红） |

所以「CI 因链接检查失败」的**充要条件**就是：检查器返回了 `1` 或 `2`。对 `1` 而言，就是「存在某条 Issue，既没被链接层过滤、也没被问题层过滤消除」。本模块把 [4.1](#41-主流程编排与统计) 的退出码、[4.2](#42-ignorerule-合取匹配)/[4.3](#43-链接问题双层过滤) 的忽略规则、CI 判定闭合成环：**忽略规则是让本该红的 CI 变绿的唯一「合法」手段**——除此之外，任何坏链都会让 PR 亮红。

#### 4.4.4 代码实践

**目标**：在不真的改坏仓库的前提下，理解「CI 在什么条件下会因链接检查失败」，并验证 paths 过滤器的范围。

**操作步骤**：

1. 打开 [.github/workflows/check-links.yml](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml)，对照 [L4–L11](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml#L4-L11) 的 `paths` 列表，回答：如果你只改了 `docs/lv1-main/README.md`，会触发吗？只改了 `.gitignore` 呢？只改了 `online-doc-tutorial/` 里某个 `.md` 呢？
2. 用只读命令统计一下正文里有多少处链接指向附录（这是 CI 每次都会校验的对象之一）：
   ```
   git grep -cE '\(/misc-app-ref/' -- 'docs/*.md' | head
   ```
3. 本地**模拟 CI 那一步**（联网，远程链接会被真查）：
   ```
   python scripts/check_links.py --config check-links.toml --verbose
   echo $?
   ```

**需要观察的现象**：

- 第 1 步：改 `docs/lv1-main/README.md` → 触发（命中 `docs/**`）；改 `.gitignore` → 不触发；改 `online-doc-tutorial/*.md` → 不触发。
- 第 3 步：因为没有 `--no-http`，你会看到大量 `Checking remote link: ...`，远程链接被逐个真查。最终退出码 `0` 表示当前仓库链接都健康；若你看到某些 `FAIL` 但最终仍 `0`，多半是它们被 `accepted_statuses`（如 403）或忽略规则（如 `kira-cpp`）合法放行了。

**预期结果**：你能准确说出「CI 失败 ⟺ 检查器退出码非 0 ⟺ 存在未被忽略的 Issue 或配置错误」。

> 待本地验证：第 3 步需要联网，且远程站点的状态随时间变化，以本地实际结果为准。若网络受限，可加 `--no-http` 只验本地部分（但这与 CI 行为不同，CI 始终带网络）。

#### 4.4.5 小练习与答案

**练习 1**：某天 CI 突然红了，日志显示某条 GitHub 链接 `remote-http: HTTP 404`。但你不觉得最近改过这个链接。可能的原因有哪些？该怎么修？

> **答案**：远程链接是否「健康」取决于**目标站点本身**，不只是你的改动——可能目标仓库被改名/删除/设为私有，导致原本好的链接变成 404；也可能目标站点临时对脚本限流。修法有三种（按优先级）：① 如果是己方链接写错，改成正确 URL；② 如果链接客观坏了但需要保留（像 `kira-cpp`），加一条 `[[ignore]]` 规则并写清 `reason`；③ 如果是误报（如对方对脚本返回 403/429），可考虑加入 `accepted_statuses`（见 [u4-l4](u4-l4-async-remote-check.md)）。这正是忽略规则系统存在的意义。

**练习 2**：为什么 CI 命令里没有 `--no-http`，而我们在本地练习时常用 `--no-http`？

> **答案**：CI 的职责是**把关真实质量**，必须真去请求远程链接才能发现「目标页面已失效」这类问题；`--no-http` 会跳过远程校验，让 CI 失去意义。本地练习用 `--no-http` 是为了**快、不联网、不干扰**——专注验证本地/Docsify 路由逻辑和忽略规则机制。[main](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L718-L719) 的 `--no-http` 开关就是把这种「本地快速跑 vs CI 完整跑」的选择权交还给调用方。

---

## 5. 综合实践

把本讲 4 个模块串起来，做一个端到端的「坏链生命周期」追踪任务：从一条坏链被产生，到被忽略规则消除，再到它对 CI 的影响。

**任务**：制造一条坏链，分别体验「不忽略 → CI 会红」和「用忽略规则消除 → CI 变绿」两种结局，并解释每一步对应 4.1–4.4 的哪个机制。

操作步骤：

1. **制造坏链**（4.1 输入侧）：新建 `docs/_ci_practice.md`，写入一个本地坏链和一个远程坏链：
   ```markdown
   # CI 综合练习

   - 本地坏链：[没了](/lv99-gone/)
   - 远程坏链：[空仓库](https://github.com/pku-minic/this-repo-does-not-exist-ci)
   ```
2. **模拟「不忽略」**：
   - 离线先看本地：`python scripts/check_links.py --config check-links.toml --no-http --verbose`，确认 `/lv99-gone/` 报 `local-missing-target`，退出码 `1`（对应 [4.1](#41-主流程编排与统计) 的退出码 → [4.4](#44-ci-触发与判定) 的 CI 红）。
   - （联网）去掉 `--no-http` 再跑，确认远程坏链报 `remote-http`。
3. **用忽略规则消除（4.2 + 4.3）**：在 [check-links.toml](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/check-links.toml) 加两条规则，分别用链接级字段精准命中：
   ```toml
   [[ignore]]
   url = "/lv99-gone/"
   reason = "练习：本地坏链"

   [[ignore]]
   url = "https://github.com/pku-minic/this-repo-does-not-exist-ci"
   reason = "练习：远程坏链"
   ```
4. **验证消除**：再跑（先 `--no-http`，再联网各一次）。确认两条坏链都消失，退出码归 `0`（CI 绿）。**重点观察**统计行 `Checked ... link(s)` 比第 2 步少了——说明它们在**链接层**（[4.3](#43-链接问题双层过滤)）就被剔掉，远程那条**根本没发请求**。
5. **回答收口问题**：结合 [4.4](#44-ci-触发与判定) 说明——如果这条 `docs/_ci_practice.md` 被提交进一个 PR，CI 会在什么条件下跑、什么条件下红、加了忽略规则后又为什么变绿。
6. **清理**：删除 `docs/_ci_practice.md`，还原 `check-links.toml`，`git diff` 确认干净（这些都是练习，**不要提交**）。

**预期结果**：你能写出一段完整因果链——「坏链被提取 → 链接层过滤未命中 → 进入校验 → 产生 Issue → 问题层过滤未命中 → 退出码 1 → CI 红」；加规则后变成「→ 链接层过滤命中 → 链接被剔除 → 不产生 Issue → 退出码 0 → CI 绿」。具体走链接层还是问题层，取决于你的规则用了链接级还是问题级字段，**以本地实际观察为准**。

> 这个练习一次性覆盖了 4 个最小模块：主流程编排与统计（4.1）、IgnoreRule 合取匹配（4.2）、双层过滤（4.3）、CI 触发与判定（4.4）。

---

## 6. 本讲小结

- **主流程编排**：[check_links](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L657-L697) 按「建类型 → 扫文件 → 加载规则 → 提取 → **链接层过滤** → 本地即时校验 / 远程攒批异步校验 → **问题层过滤** → 统计」串成管线；`stats['links']` 与 `stats['issues']` 都是**过滤之后**的口径。
- **退出码三态**：[main](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L711-L737) 用 `0`（无坏链）/ `1`（有坏链）/ `2`（配置错误），这是 CI 判定红绿的唯一依据。
- **IgnoreRule 合取匹配**：一条规则上所有「已填写」字段必须**同时**命中（合取），规则之间是**任一命中即忽略**（析取）；8 个字段分**链接级**（`url`/`url_regex`/`path`/`path_glob`/`line`）和**问题级**（`category`/`status`/`statuses`）两组。
- **关键早返回**：[matches](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L162-L183) 在 `issue is None` 时直接 `return True`——链接层只看链接级字段，**问题级字段必须搭配链接级字段收窄，否则会误删全部链接**。
- **双层过滤**：[filter_ignored_links](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L649-L650)（校验前，只传 `Link`，省无用功）与 [filter_ignored_issues](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L653-L654)（校验后，传 `Link+Issue`，能按结果忽略）；`kira-cpp` 是链接级 `url` 规则的现成样例，源于文档自述「404 是正常现象」。
- **CI 集成**：[check-links.yml](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml) 在 PR / push（带 `paths` 过滤）/ 手动三种时机触发，CI 命令**不带** `--no-http`（远程真查）；**CI 失败 ⟺ 检查器退出码非 0 ⟺ 存在未被忽略的 Issue 或配置错误**。

---

## 7. 下一步学习建议

至此，第四单元「链接检查器架构」四连讲 + 本讲收官，整套学习手册（u1–u4）也画上句号。本讲把前三讲的提取、本地校验、远程校验三个零件，用**主流程编排**串成管线，用**忽略规则 + 双层过滤**提供「合法豁免」出口，最后用 **CI** 把能力固化成团队流程——形成了一个完整闭环。

接下来你可以：

1. **横向打通第四单元**：回头把 [u4-l1 总览](u4-l1-linkchecker-config-and-entry.md)→[u4-l2 提取](u4-l2-link-extraction.md)→[u4-l3 本地校验](u4-l3-docsify-routing-and-local-check.md)→[u4-l4 远程校验](u4-l4-async-remote-check.md)→本讲连起来读，画出一张从「文档文件」到「CI 红绿」的全链路数据流图，自检是否每个环节都讲得清。
2. **二次开发练习**：给检查器加一个小特性练手，例如：① 新增一个 `--format json` 选项，把 `issues` 输出成机器可读的 JSON（方便别的工具消费）；② 在 [print_issues](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L700-L708) 里按 `category` 分组统计；③ 给 `IgnoreRule` 增加一个 `expires` 字段，让忽略规则在指定日期后自动失效（避免「永久豁免」被遗忘）。每个都涉及 `argparse`/`main`/`IgnoreRule`，正好用上本讲的内容。
3. **回到文档主线**：如果你是课程学生，链接检查器只是「工具」，真正的目标是 [u3](u3-l1-lab-layering-and-pipeline.md) 讲的编译器。可以带着「文档是怎么被组织、被自动校验」的工程视角，重新进入 Lv1–Lv9 的编译实践；如果你是文档维护者，本讲给出的忽略规则与 CI 机制就是你日常保证文档链接健康的得力工具。
