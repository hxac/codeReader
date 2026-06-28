# 链接检查器总览、配置与入口

> 本讲属于第四单元「链接检查器架构（核心代码）」的第一篇。从本讲起，我们正式进入仓库里**唯一一段有分量的程序代码** [`scripts/check_links.py`](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py)。本讲只打地基：搞清楚这个工具**干什么、由哪些数据结构撑起、怎么读配置、怎么从命令行启动**。链接提取、Docsify 路由校验、远程异步校验、忽略规则与 CI 等细节留给后续四讲（u4-l2 ~ u4-l5）。

---

## 1. 本讲目标

学完本讲，你应当能够：

- 说清链接检查器要解决什么问题，并能画出它从「扫描文件」到「输出问题」的主流程。
- 认识 `Link` / `Issue` / `RemoteResponse` / `RemoteResult` / `FileTypes` / `IgnoreRule` 这六个核心 `dataclass`，知道各自承载什么信息、在哪里被构造。
- 理解 `DEFAULT_CONFIG` 默认配置、`check-links.toml` 配置文件，以及 `deep_merge` 如何把二者**递归合并**（而不是简单覆盖整个段）。
- 掌握 `argparse` 命令行入口的四个参数，以及 `main()` 返回的退出码 `0 / 1 / 2` 各自意味着什么、它们如何决定 CI 是否失败。

---

## 2. 前置知识

本讲默认你已经读过：

- **u1-l3 仓库目录结构一览**：你已经知道 `scripts/check_links.py` 是仓库里唯一有分量的代码，它配合 `.github/workflows/check-links.yml`、`check-links.toml`、`requirements.txt` 构成一个「链接检查闭环」；扫描范围是 `README.md` 与整个 `docs/`，但**排除** `docs/assets/**`。
- **u1-l4 站点入口与导航机制**：你已经知道 Docsify 是**单页应用（SPA）**，站点是「一个 `index.html` 外壳 + 一堆按需抓取的 `.md`」。

此外需要补充三条本讲会用到的 Python 与工程常识：

1. **`dataclass`**：Python 用 `@dataclass` 把一个类变成「主要用来装数据的容器」，自动生成 `__init__` / `__repr__` / `__eq__` 等方法。加上 `frozen=True` 后实例**不可变**（不能改字段），好处是可以放进集合、可以安全地在函数间传递而不用担心被偷偷改掉。
2. **TOML 与 `tomllib`**：TOML 是一种「像 ini 但更严格」的配置文件格式，用 `[section]` 分段、`[[array]]` 表示数组里的一个元素。Python 3.11+ 标准库自带 `tomllib` 模块可以解析它（本仓库 CI 跑在 `ubuntu-latest`，Python 为 3.12，直接可用）。`scripts/check_links.py` 第 24 行就 `import tomllib`。
3. **进程退出码（exit code）**：程序结束时返回给操作系统的一个整数。Shell/CI 用 `$?` 读取它：**0 表示成功，非 0 表示失败**。GitHub Actions 里一条命令返回非 0，整个步骤就判为失败。链接检查器正是靠退出码告诉 CI「有没有坏链接」。

> 一句话直觉：这个检查器像一个**图书校对员**——它把整本「文档站」从头到尾翻一遍，把每一处链接（不管是站内跳转还是外网网址）都点一下，点不通的就记进「问题清单」，最后用退出码汇报「全通 / 有坏链 / 配置出错」。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲用到的部分 |
| --- | --- | --- |
| [scripts/check_links.py](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py) | 链接检查器本体（742 行单文件）。 | 模块文档、`DEFAULT_CONFIG`、六个 `dataclass`、`deep_merge` / `load_config`、`check_links` 主流程、`main` 入口。 |
| [check-links.toml](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/check-links.toml) | 检查器的 TOML 配置：扫描范围、HTTP 行为、忽略规则。 | 三个配置段 `[scan]` / `[http]` / `[[ignore]]`。 |
| [requirements.txt](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/requirements.txt) | 检查器的 Python 依赖。 | 三个包各自对应检查器的哪项能力。 |

> 本讲**不深入**链接如何被提取（u4-l2）、如何按 Docsify 路由校验站内链接（u4-l3）、如何异步校验远程链接（u4-l4）。本讲只看「骨架」。

---

## 4. 核心概念与源码讲解

按四个最小模块拆分：

1. 工具职责与流程概览
2. 核心 `dataclass`
3. TOML 配置与 `deep_merge`
4. `argparse` 入口

---

### 4.1 工具职责与流程概览

#### 4.1.1 概念说明

文档站是 Docsify 单页应用，它的链接有个**麻烦的特性**：站内链接写的是「浏览器路由」，而不是「磁盘上的相对文件路径」。

回顾 u3-l2 讲过的三种站内链接形态：

- `/lv1-main/` —— 指向目录的 `README.md`（路由以 `/` 结尾）
- `/lv1-main/structure` —— 指向 `lv1-main/structure.md`（路由无后缀）
- `/misc-app-ref/koopa?id=符号名称` —— 跳到某页的标题锚点（`?id=`）

这意味着一个**普通**的链接检查器（只看「文件存不存在」）在这里会大面积误报：它看到 `/lv1-main/` 就去找 `docs/lv1-main/` 这个文件，发现「不存在」就报坏链——可实际上 Docsify 会把它解析成 `docs/lv1-main/README.md`，链接是好用的。

所以本仓库**自研**了这个检查器，让它「懂 Docsify 路由」。它的完整职责有四条（见模块开头的文档字符串）：

1. **扫描** `README.md` 与整个 `docs/`（排除 `docs/assets/**`）下的所有 `.md` / `.html` 文件。
2. **提取**其中的链接（Markdown 链接/图片、HTML 里的 `href`/`src`、以及裸 URL）。
3. **校验站内链接**：按 Docsify 路由风格把链接翻译成「一个或多个候选文件」，只要有一个存在就算通；若带锚点，还要确认该锚点在目标文件里存在。
4. **校验远程链接**：用 HTTP 先 `HEAD`、失败再 `GET` 回退；若链接带锚点，还要抓取页面确认锚点存在。

#### 4.1.2 核心流程

整个检查流程由一个函数 `check_links` 编排，它的输入输出与中间产物如下（先看流程，再看源码）：

```
输入: 仓库根目录 root + 配置 config + 开关 no_http/verbose
  │
  ├─ 1. configured_file_types(config)        → FileTypes  （哪些后缀算 md/html）
  ├─ 2. discover_files(root, config, types)  → list[Path] （扫描出待检查文件）
  ├─ 3. load_ignore_rules(config)            → list[IgnoreRule]
  ├─ 4. extract_links(root, files, types)    → list[Link] （u4-l2 详讲）
  │     └─ filter_ignored_links(...)         → 剔除被忽略的链接（第一层过滤）
  │
  ├─ 5. 遍历每个 Link:
  │     ├─ should_skip（mailto/javascript/data 等） → 跳过
  │     ├─ is_remote（http(s)://、//） → 暂存进 remote_links（除非 --no-http）
  │     └─ 否则 → check_local_link(...)  → Issue | None（u4-l3 详讲）
  │
  ├─ 6. 若有远程链接且未 --no-http:
  │     └─ asyncio.run(check_remote_links(...)) → list[Issue]（u4-l4 详讲）
  │
  ├─ 7. filter_ignored_issues(issues, rules) → 第二层过滤（连「问题」也能忽略）
  ├─ 8. 统计 stats（文件数 / 链接数 / 远程链接数 / 问题数）
  └─ 返回 (排序后的 issues, stats)
```

两个关键设计值得现在就记住：

- **两层过滤**：链接在「提取后」过滤一次（第 4 步），「问题」在「校验后」再过滤一次（第 7 步）。后者很巧妙——某些链接你不想完全忽略、只是不想在校验阶段为它报警，这时可以写一条针对「问题类别」的忽略规则。
- **本地链接即时查、远程链接攒一批**：本地校验是纯文件操作，直接在循环里逐个查；远程校验要发网络请求，于是先把远程链接按 URL 去重攒进 `remote_links` 字典，最后一次性交给 `asyncio` 并发处理。

#### 4.1.3 源码精读

**模块文档字符串**——开篇就用三句话交代了工具懂 Docsify 路由、远程用 HEAD 先于 GET、带锚点要抓页面验证，是理解全工具的最佳入口：

[scripts/check_links.py:3-15](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L3-L15) —— 模块说明，列出 Docsify 路由解析规则与远程校验策略。

**主流程 `check_links`**——上面的流程图就是逐行对应这个函数。重点看它如何把「文件类型 → 文件发现 → 忽略规则 → 链接提取 → 分流本地/远程 → 双层过滤 → 统计」串起来：

[scripts/check_links.py:657-697](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L657-L697) —— 主编排函数。注意第 671 行的 `link.should_skip`、第 673 行的 `link.is_remote` 分流，以及第 686 行用 `asyncio.run` 触发远程校验。

其中**统计字典** `stats` 是「`--verbose` 那行总结性输出」的数据来源，记住这五个键名，后面看实践输出时就能对上号：

[scripts/check_links.py:690-696](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L690-L696) —— 统计 `files / links / remote_links / unique_remote_links / issues` 五个计数。

#### 4.1.4 代码实践

> 实践目标：把上面那张流程图和真实输出对应起来，确认「扫描 → 提取 → 校验 → 统计」四步都真的发生了。

操作步骤：

1. 安装依赖（若未装）：`python -m pip install -r requirements.txt`。
2. 在仓库根目录运行：
   ```bash
   python scripts/check_links.py --config check-links.toml --no-http --verbose
   ```
   - `--no-http` 让第 6 步（远程校验）整个跳过，避免发外网请求、跑得又快又稳定，适合先观察本地流程。
   - `--verbose` 打开第 664 行的 `Scanning N link(s)...` 与第 678 行逐条 `Checking local link: ... PASS/FAIL`。

需要观察的现象：

- 终端会先打印一行 `Scanning N link(s)...`（来自第 664 行）。
- 然后逐行打印 `Checking local link: /xxx/ ... PASS`（第 678/683 行）。
- 最后打印一行 `Checked A file(s), B link(s), C remote occurrence(s) (D unique).`（第 733-736 行，其中 remote 数应为 0，因为 `--no-http`）。

预期结果：

- 退出码为 `0`（用 `echo $?` 查看），即当前仓库**没有本地坏链**。
- 具体的 `files` / `links` 数值取决于当前 `docs/` 体量，**待本地验证**——把它记下来，后面改文档时可以对比这个基线。

#### 4.1.5 小练习与答案

**练习 1**：为什么第 6 步远程校验要用 `asyncio.run(check_remote_links(...))` 一次性跑，而不是像本地那样在主循环里逐个发请求？

> **参考答案**：本地校验是磁盘读文件，微秒级且无副作用，逐个同步查即可；远程校验要发 HTTP 请求，单次几十到几百毫秒、还可能失败重试。若串行跑上百个远程链接会非常慢。攒成一批后用 `asyncio` + 信号量并发（u4-l4 详讲），总耗时接近最慢的那一个，而非全部之和。

**练习 2**：流程图里有「两层过滤」，第一层过滤的是 `Link`，第二层过滤的是 `Issue`。请说出一个「只想用第二层、不想用第一层」的真实场景。

> **参考答案**：某个外网链接本身你**希望它出现在文档里、也希望它被提取**（所以不能用第一层 `filter_ignored_links` 直接剔除），但它在检查时偶尔返回 403/429 这类不稳定状态码，你不想为它报警。这时写一条针对 `category='remote-http'` 的忽略规则，让它在校验后的 `filter_ignored_issues` 阶段被过滤掉即可。

---

### 4.2 核心 `dataclass`

#### 4.2.1 概念说明

检查器在文件里读到的每一个链接、发现的每一个问题、每一次远程请求的结果，都被建模成一个**不可变的数据对象**（`@dataclass(frozen=True)`）。这样做有三个好处：

1. **语义清晰**：一个 `Link` 对象就完整描述「这条链接来自哪个文件、第几行第几列、URL 是什么」，不需要在函数间传一堆零散参数。
2. **不可变（frozen）**：不会被某段代码偷偷改掉字段，且能放进 `set` 去重（去重逻辑在 `dedupe_links` 里就用到了 `Link` 的字段）。
3. **自带行为**：把「判断是不是远程链接」「该不该跳过」这类**派生属性**写成 `@property`，挂在数据对象上，调用处读起来像自然语言（`link.is_remote`）。

本工具一共定义了六个核心 `dataclass`，按下表记住它们的「一句话职责」即可：

| dataclass | 一句话职责 | 关键字段 / 行为 |
| --- | --- | --- |
| `Link` | 一条被提取到的链接。 | `source` / `line` / `column` / `url`；属性 `is_remote`、`should_skip`、`normalized_url`。 |
| `Issue` | 一条被发现的「坏链问题」。 | `link` + `category`（如 `local-missing-target`）+ `message` + 可选 `status`。 |
| `RemoteResponse` | 一次远程请求的原始返回。 | `status` / `content_type` / `body` / `final_url`。 |
| `RemoteResult` | 对一次远程请求的**判定结论**。 | `ok: bool` + `message` + 可选 `status`。 |
| `FileTypes` | 「哪些后缀算 md / html」的集合。 | `markdown` / `html` 两个集合；方法 `is_markdown` / `is_html` / `is_candidate`。 |
| `IgnoreRule` | 一条忽略规则。 | 多个可选匹配字段（`url` / `path` / `line` / `category` …）+ `matches()` 方法。 |

> 区分 `RemoteResponse` 与 `RemoteResult`：前者是**客观事实**（服务器回了什么），后者是**主观判断**（这个事实算不算「通过」）。`evaluate_remote_response` 负责把前者翻译成后者（u4-l4 详讲）。

#### 4.2.2 核心流程

以一个**远程链接**的生命周期为例，看这些 dataclass 如何接力：

```
markdown/html 文本
   │  extract_links（u4-l2）
   ▼
Link(source, line, column, url)         ← 描述「链接本身」
   │  check_remote_link 发请求
   ▼
RemoteResponse(status, content_type, body, final_url)   ← 「服务器回了什么」
   │  evaluate_remote_response 判定
   ▼
RemoteResult(ok, message, status)       ← 「算不算通过」
   │  若 !ok，则包装成
   ▼
Issue(link, category='remote-http', message, status)    ← 进入问题清单
```

而 `FileTypes` 与 `IgnoreRule` 是横切全流程的「配置型」对象：`FileTypes` 在扫描阶段决定哪些文件被纳入、在路由校验阶段决定候选后缀；`IgnoreRule` 在两层过滤阶段决定哪些链接/问题被剔除。

#### 4.2.3 源码精读

**`Link`**——最核心的数据对象。注意三个 `@property` 把「派生判断」封装得很干净：`normalized_url` 把协议相对 URL（`//xxx`）补成 `https://xxx`；`is_remote` 判断是否需要走 HTTP；`should_skip` 用一个 `SKIPPED_SCHEMES` 集合过滤掉 `mailto:` / `javascript:` / `data:` 等非网络链接：

[scripts/check_links.py:78-109](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L78-L109) —— `Link` 数据类。`__post_init__`（第 96-97 行）在构造后对 URL 做了 `html.unescape` + `strip`，保证 HTML 实体（如 `&amp;`）被还原。

**`Issue` / `RemoteResponse` / `RemoteResult`**——三个小而精的容器，字段即语义：

[scripts/check_links.py:112-132](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L112-L132) —— `Issue`、`RemoteResponse`、`RemoteResult` 三个数据类。

**`FileTypes`**——把「后缀集合」与「判断方法」打包在一起，扫描和路由校验都靠它：

[scripts/check_links.py:135-147](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L135-L147) —— `FileTypes` 数据类。`is_candidate` 表示「只要 md 或 html 就算候选文件」。

**`IgnoreRule`**——忽略规则的「合取匹配」引擎。`matches()` 方法的关键思想是：**每填一个字段就多一个约束，所有填了的字段必须同时满足才算命中**（u4-l5 详讲）。本讲只需知道它的结构与字段：

[scripts/check_links.py:150-183](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L150-L183) —— `IgnoreRule` 数据类与其 `matches()` 方法。

#### 4.2.4 代码实践（源码阅读型）

> 实践目标：不用运行，只靠阅读源码，搞清楚每个 dataclass 在哪里被**构造**、`Issue.category` 都出现过哪些值。

操作步骤：

1. 用编辑器在 `scripts/check_links.py` 里搜索 `Issue(`，记录每处构造点对应的 `category` 字符串。提示：本地两类错误在第 500、503 行附近（`local-missing-target`、`local-missing-anchor`），远程一类在第 645 行（`remote-http`）。
2. 搜索 `Link(`，确认它只在「链接提取」相关函数（`extract_markdown_links` / `html_links` / `bare_html_links`）里被构造——这印证了 `Link` 只描述「被提取到的链接」。
3. 搜索 `RemoteResult(`，确认它只在 `evaluate_remote_response` 与 `check_remote_*` 里被构造。

需要观察的现象 / 预期结果：

- `Issue` 的 `category` 一共就三类取值：`local-missing-target`、`local-missing-anchor`、`remote-http`。这正好对应「目标文件不存在」「锚点不存在」「远程请求失败」三种坏链。
- 这三个类别后面会出现在**忽略规则**（`IgnoreRule.category`）里——也就是说你能按类别忽略某一类问题。

> 说明：本实践是「源码阅读型」，不修改任何文件，也不需要运行。若想验证搜索结果，可用 `grep -n 'Issue(' scripts/check_links.py`（只读命令）。

#### 4.2.5 小练习与答案

**练习 1**：`Link` 用了 `frozen=True`，但它的 `__post_init__` 又通过 `object.__setattr__(self, 'url', ...)` 改了 `url` 字段。这矛盾吗？

> **参考答案**：不矛盾。`frozen=True` 禁止的是「普通赋值」`self.url = ...`（会抛 `FrozenInstanceError`），但 Python 留了 `object.__setattr__` 这个「后门」允许在 `__post_init__` 里做一次性初始化加工。这里用它把 URL 做了 `html.unescape` + `strip` 归一化，之后该对象依然不可变。

**练习 2**：`RemoteResponse` 和 `RemoteResult` 都有 `status` 字段，为什么 `RemoteResult.ok` 还要单独存一份布尔？

> **参考答案**：因为「状态码」和「是否通过」不是一一对应。比如 403 在默认配置里被 `accepted_statuses` 接受、判为通过（u4-l4 详讲），而 404 则判为失败；还有些情况下连状态码都没有（请求直接抛异常）。所以需要一个独立、明确的 `ok: bool` 作为最终结论，`status` 仅作诊断信息附带。

---

### 4.3 TOML 配置与 `deep_merge`

#### 4.3.1 概念说明

检查器的所有「可调参数」——扫描范围、HTTP 超时、并发数、忽略规则——都来自一份配置。这份配置有**两个来源**，且通过一种叫**深度合并（deep merge）**的策略叠加：

1. **默认配置 `DEFAULT_CONFIG`**：写死在代码里（`scripts/check_links.py` 第 37-56 行），保证「不传任何配置文件也能跑」。
2. **TOML 配置文件 `check-links.toml`**：放在仓库根目录，团队按需覆盖默认值。

为什么不直接「用 TOML 整段替换默认值」？因为那样太粗暴——假设你只想把 `http.workers` 从 8 改成 16，就得在 TOML 里把整个 `[http]` 段（超时、重试、UA、可接受状态码……）全都抄一遍，漏抄一个就丢失了默认值。`deep_merge` 的作用就是**递归地、按键合并**：只有两边都是字典时才继续往下钻，否则用 TOML 的值覆盖默认值。

#### 4.3.2 核心流程

合并的递归规则可以写成（`base` 是默认、`override` 是 TOML）：

\[
\text{merge}(base, override)[k] =
\begin{cases}
\text{merge}(base[k], override[k]) & \text{若 } base[k] \text{ 与 } override[k] \text{ 都是 dict} \\
override[k] & \text{否则}
\end{cases}
\]

配置加载的完整流程在 `load_config` 里：

```
load_config(root, config_path):
  ├─ config_path 为 None？  → 直接返回 DEFAULT_CONFIG（不读任何文件）
  ├─ 把相对路径解析成 root 下的绝对路径
  ├─ 文件不存在？  → raise ValueError（→ 退出码 2）
  └─ deep_merge(DEFAULT_CONFIG, tomllib.load(file))
```

`check-links.toml` 的三个配置段对应三个功能域：

| 段 | 作用 | 典型覆盖点 |
| --- | --- | --- |
| `[scan]` | 扫描哪些路径、哪些后缀、排除什么、`docs_root` 在哪。 | `paths`、`exclude=["docs/assets/**"]`、`docs_root="docs"`。 |
| `[http]` | 远程校验的 HTTP 行为。 | `timeout`、`workers`（本仓库覆盖为 16）、`accepted_statuses`、`check_fragments`。 |
| `[[ignore]]` | 忽略规则数组，每条是一个 TOML 表。 | 见本仓库唯一的 `kira-cpp` 规则。 |

#### 4.3.3 源码精读

**`DEFAULT_CONFIG`**——默认配置的总源头。注意 `ignore` 默认是空列表 `[]`：

[scripts/check_links.py:37-56](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L37-L56) —— 默认配置。`scan` / `http` / `ignore` 三段，与 TOML 三段一一对应。

**`deep_merge`**——递归合并的核心。第 187-188 行先对 `base` 做一次**浅拷贝**（且把其中的 dict 也 `.copy()` 一份），避免污染原始的 `DEFAULT_CONFIG`；第 189-193 行遍历 `override`，遇 dict 就递归、否则直接覆盖：

[scripts/check_links.py:186-194](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L186-L194) —— 深度合并函数。

**`load_config`**——把「解析路径 + 校验存在 + 读 TOML + 合并」串起来。注意它对**相对路径**用 `root / config_path` 拼接，所以 `--config check-links.toml` 是相对于仓库根目录解析的：

[scripts/check_links.py:197-206](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L197-L206) —— 配置加载入口。

**`check-links.toml` 实物**——三个段的真实写法。重点看 `http.workers = 16` 是如何覆盖默认值 8 的，以及 `[[ignore]]` 这条针对 `kira-cpp` 仓库 404 的规则（注释明确说明「文档里说这个示例仓库没写完，404 是预期的」）：

[check-links.toml:1-24](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/check-links.toml#L1-L24) —— 配置文件全文。

**`requirements.txt`**——三个依赖各司其职，正好对应检查器的三大能力：

[requirements.txt:1-3](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/requirements.txt#L1-L3) —— `httpx`（发 HTTP 请求校验远程链接）、`markdown-it-py[linkify]`（解析 Markdown 抽取链接、并把裸 URL 转成链接）、`selectolax`（解析 HTML 片段抽取 `href`/`src`）。

#### 4.3.4 代码实践

> 实践目标：亲眼看到 `deep_merge`「按键合并」而非「整段覆盖」的效果。

操作步骤：

1. 先阅读 `check-links.toml`，逐段说出作用（见上表）。
2. 做一个**对比实验**，体会深度合并：
   - 运行 `python scripts/check_links.py --no-http --verbose`（注意：**不传** `--config`）。根据 `load_config` 第 198-199 行，此时直接用 `DEFAULT_CONFIG`，`http.workers` 应为默认的 **8**。
   - 再运行 `python scripts/check_links.py --config check-links.toml --no-http --verbose`，此时 `http.workers` 被 TOML 覆盖为 **16**。
   - 想象一下：如果合并是「整段替换」，那么 TOML 里**没有写** `http.timeout` 就会让超时丢失；正因为是深度合并，`timeout=15` 这个默认值依然保留。

需要观察的现象 / 预期结果：

- 两次运行的**本地结果应完全一致**（因为 `workers` 只影响远程并发，`--no-http` 时体现不出来）。这恰好说明：深度合并在不触碰你需要保留的默认值的前提下，完成了覆盖。
- 若想确认 `workers` 取值确实变了，可在 `check_links.py` 第 618 行 `concurrency = ...` 后临时加一行 `print(concurrency)`（属示例修改，验证后请还原），分别用两种方式启动观察 8 与 16。**待本地验证**。

> 注意：本实践不修改任何源码即可完成前两步；第三步的 `print` 是可选的临时调试，务必还原。

#### 4.3.5 小练习与答案

**练习 1**：`deep_merge` 第 187-188 行为什么先对 `base` 做一次拷贝（连里面的 dict 也 `.copy()`）？不拷贝会怎样？

> **参考答案**：因为 `base` 通常是模块级的 `DEFAULT_CONFIG` 常量。若不拷贝直接在上面改，第一次合并就会把 `DEFAULT_CONFIG` 本身污染掉，后续所有调用都会拿到被改过的「默认值」。先拷贝保证「默认配置永远是默认配置」。

**练习 2**：`[[ignore]]` 在 TOML 里为什么用双方括号，而 `[scan]` 用单方括号？

> **参考答案**：单方括号 `[scan]` 定义一个**表（dict）**，整个配置里只能有一个 `scan`；双方括号 `[[ignore]]` 定义的是**数组里的一个元素**，可以出现多次，每次追加一条忽略规则到 `ignore` 数组里。所以 `load_ignore_rules` 第 226 行是 `for item in config.get('ignore', [])` 遍历一个列表。

---

### 4.4 `argparse` 入口

#### 4.4.1 概念说明

工具最终要被人或 CI 调用，调用方式就是**命令行参数**。Python 标准库 `argparse` 负责解析这些参数。本工具暴露四个开关：

| 参数 | 类型 | 默认 | 作用 |
| --- | --- | --- | --- |
| `--config` | 文件路径 | `None` | 指定 TOML 配置文件；不传则用 `DEFAULT_CONFIG`。 |
| `--root` | 目录 | `.` | 仓库根目录，决定扫描与相对路径的基准。 |
| `--no-http` | 开关 | `False` | 跳过所有远程链接校验（本地开发/CI 加速常用）。 |
| `--verbose` | 开关 | `False` | 打印逐条检查进度与最终统计。 |

而 `main()` 的**返回值（退出码）**是这个工具与 CI 的契约，三档语义非常关键：

- **0**：没有坏链，一切正常。
- **1**：发现了坏链（`issues` 非空）。
- **2**：配置出错（如配置文件不存在），来自第 729 行捕获的 `ValueError`。

CI（`.github/workflows/check-links.yml`）正是跑 `python scripts/check_links.py --config check-links.toml --verbose`，只要退出码非 0，这一步就判失败、PR 无法合并。

#### 4.4.2 核心流程

`main()` 的执行顺序：

```
main():
  ├─ argparse 解析四个参数
  ├─ root = Path(args.root).resolve()         ← 解析成绝对路径
  ├─ try:
  │     config = load_config(root, args.config)        ← 第 4.3 节
  │     issues, stats = check_links(root, config, ...)  ← 第 4.1 节主流程
  │  except ValueError:
  │     打印 "Configuration error: ..." 到 stderr
  │     return 2                                          ← 配置错误
  ├─ print_issues(issues)                    ← 打印「No broken links」或问题清单
  ├─ if verbose: 打印 stats 总结行
  └─ return 1 if issues else 0               ← 有坏链→1，否则→0
```

整个脚本最末尾的两行是 Python 的标准入口惯用法——把 `main()` 的返回值交给 `SystemExit`，从而成为进程退出码：

```python
if __name__ == '__main__':
    raise SystemExit(main())
```

#### 4.4.3 源码精读

**`main` 函数**——四个参数的定义、`try/except` 把配置错误隔离成退出码 2、以及最后用 `1 if issues else 0` 把「问题数」翻译成退出码：

[scripts/check_links.py:711-737](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L711-L737) —— 命令行入口。注意第 729 行用 `file=sys.stderr` 把错误打到标准错误流，第 737 行的退出码判定。

**`print_issues`**——决定「人」看到的输出格式：无问题时打印 `No broken links found.`，有问题时先打印总数，再逐条打印 `文件:行:列` + 类别 + 详情。`verbose` 总结行（第 733-736 行）用 `.format(**stats)` 把统计字典填进模板：

[scripts/check_links.py:700-708](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L700-L708) —— 问题打印函数。

**入口惯用法**——`raise SystemExit(main())` 让 `main()` 的返回整数成为进程退出码：

[scripts/check_links.py:740-741](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L740-L741) —— `__main__` 守卫。

**CI 调用**——`.github/workflows/check-links.yml` 里就是一行命令，不传 `--no-http`（即 CI 会真发请求校验远程链接），传了 `--verbose`：

```yaml
- name: Check documentation links
  run: python scripts/check_links.py --config check-links.toml --verbose
```

> 配套事实：该工作流的触发条件（`paths` 过滤）与失败判定在 u4-l5 详讲。本讲只需知道「CI 跑的就是这个入口」。

#### 4.4.4 代码实践（本讲主实践）

> 实践目标：用真实命令验证「退出码三档语义」，并记录基线统计。

操作步骤：

1. **基线运行**（退出码 0）：
   ```bash
   python scripts/check_links.py --config check-links.toml --no-http --verbose
   echo "exit=$?"
   ```
   记录输出里的 `Checked A file(s), B link(s), ...` 那一行，把 A、B 记下来作为**基线**。
2. **配置错误**（退出码 2）：
   ```bash
   python scripts/check_links.py --config does-not-exist.toml
   echo "exit=$?"
   ```
   预期看到 stderr 打印 `Configuration error: configuration file not found: ...`，退出码为 `2`（对应第 728-730 行）。
3. **不传 config**（体会默认配置）：
   ```bash
   python scripts/check_links.py --no-http --verbose
   echo "exit=$?"
   ```
   此时用 `DEFAULT_CONFIG`，结果应与第 1 步一致（因为 `check-links.toml` 主要只覆盖了 `http.*`，而 `--no-http` 用不到）。

需要观察的现象：

| 步骤 | 预期退出码 | 关键输出 |
| --- | --- | --- |
| 1 基线 | `0` | `Checked A file(s), B link(s), 0 remote occurrence(s) (0 unique).` |
| 2 错配置 | `2` | `Configuration error: configuration file not found: ...`（stderr） |
| 3 默认配置 | `0` | 与步骤 1 相同 |

预期结果：

- 三档退出码 `0 / 2 / 0` 如上表。
- 第 1 步的精确 `files` / `links` 数值随当前 `docs/` 体量变化，**待本地验证**——记下后，今后改文档时可对比这个基线判断「我是不是新增了链接」。

> 想看到退出码 1（真有坏链）又不想破坏仓库？见第 5 节「综合实践」，那里用一个隔离的临时目录安全制造一条坏链。

#### 4.4.5 小练习与答案

**练习 1**：为什么配置错误要单独用退出码 `2`，而不是和「有坏链」一样用 `1`？

> **参考答案**：因为两种「失败」性质不同。有坏链（1）是「检查正常跑完了，发现了真实问题」，作者需要去修链接；配置错误（2）是「检查根本没正常跑」，作者需要去修配置或命令。分开退出码，CI 日志和人都能一眼区分「是文档出了问题」还是「检查器自身配置出了问题」。

**练习 2**：`main()` 里为什么用 `try/except ValueError` 包住 `load_config` + `check_links`，而不是只包 `load_config`？

> **参考答案**：因为不只 `load_config`（文件不存在）会抛 `ValueError`，`check_links` 链路里的 `load_ignore_rules`（第 228 行：`each [[ignore]] entry must be a TOML table`）等也会抛 `ValueError` 表示配置非法。把这些「配置类错误」统一在 `main` 顶层捕获、统一翻译成退出码 2，调用方就不必区分是哪一步配置错了。

---

## 5. 综合实践

> 综合目标：在一个**隔离的临时目录**里，亲手走一遍「写配置 → 跑检查 → 看退出码 1 → 用 ignore 让它通过」的完整闭环，把本讲的「流程 / dataclass / 配置 / 入口」四个模块串起来，且不污染真实仓库。

操作步骤：

1. 建一个临时目录和一个含坏链的 Markdown：
   ```bash
   mkdir -p /tmp/lc-demo/docs
   printf '# Demo\n\n[好链接](/lv1-main/)\n\n[坏链接](/no-such-page/)\n' > /tmp/lc-demo/docs/README.md
   ```
   > 说明：`/lv1-main/` 在真实仓库里存在，但这里我们把 `--root` 指向临时目录，所以它也会变成「坏链」——没关系，我们关注的是 `/no-such-page/`。
2. 写一个最小配置 `/tmp/lc-demo/test.toml`：
   ```toml
   [scan]
   paths = ["docs"]
   docs_root = "docs"
   ```
3. 第一次运行，预期退出码 1：
   ```bash
   python scripts/check_links.py --root /tmp/lc-demo --config test.toml --no-http --verbose
   echo "exit=$?"
   ```
   - 注意：`--config` 是相对于 `--root` 解析的（见 `load_config` 第 201-202 行），所以写 `test.toml` 即可。
   - 预期看到 `FAIL` 行与 `Found N broken link(s):`，退出码 `1`。
4. 在 `test.toml` 末尾加一条忽略规则，让坏链「被放过」：
   ```toml
   [[ignore]]
   url = "/no-such-page/"
   reason = "demo: pretend this is fine"
   ```
   再次运行步骤 3 的命令，预期：不再有坏链、退出码变为 `0`。
   - 这条规则在**第一层过滤**（`filter_ignored_links`）就把该链接剔除了，所以它压根不会被校验——印证了第 4.1.5 节练习 2 提到的「两层过滤」。

需要观察的现象：

| 阶段 | 输出特征 | 退出码 |
| --- | --- | --- |
| 加 ignore 前 | `Found 1+ broken link(s):` 含 `/no-such-page/` | `1` |
| 加 ignore 后 | `No broken links found.` | `0` |

预期结果：两次运行的退出码分别为 `1` 和 `0`，完整演示了「坏链 → 退出码 1 → ignore → 退出码 0」的闭环。完成后可删除 `/tmp/lc-demo`，本仓库未受任何影响。

---

## 6. 本讲小结

- **职责**：检查器是仓库唯一的程序代码，它「懂 Docsify 路由」，按「扫描文件 → 提取链接 → 过滤 → 本地校验 / 远程异步校验 → 双层过滤 → 统计」的主流程运行（`check_links`，[L657-697](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L657-L697)）。
- **数据建模**：用六个 `frozen` `dataclass` 撑起全工具——`Link`（链接）、`Issue`（问题，三类 category）、`RemoteResponse`/`RemoteResult`（远程原始返回 vs 判定结论）、`FileTypes`（后缀集合）、`IgnoreRule`（忽略规则）。
- **配置**：默认值在 `DEFAULT_CONFIG`（[L37-56](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L37-L56)），`check-links.toml` 按需覆盖；`deep_merge` 递归按键合并，保证只改一个值不会丢失其余默认值。
- **依赖**：`requirements.txt` 三个包对应三大能力——`httpx`（远程请求）、`markdown-it-py[linkify]`（解析 Markdown）、`selectolax`（解析 HTML）。
- **入口与契约**：`argparse` 暴露 `--config / --root / --no-http / --verbose` 四参；`main()` 用退出码 `0 / 1 / 2` 分别表示「正常 / 有坏链 / 配置错误」，这正是 CI 判定 PR 是否能合并的依据。
- **延展**：本讲只看了骨架；链接如何被**提取**、本地链接如何按 **Docsify 路由**校验、远程链接如何**异步**校验、忽略规则如何**合取匹配**，留给后续四讲。

---

## 7. 下一步学习建议

- **u4-l2 链接提取子系统**：本讲把 `extract_links` 当作黑盒，下一讲打开它——看 `markdown-it-py` 的 token 流如何定位 Markdown 链接、`selectolax` 如何抽 HTML 的 `href`/`src`、以及裸 URL 正则如何兜底。建议先读 [scripts/check_links.py:353-393](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L353-L393) 找找感觉。
- **u4-l3 Docsify 路由解析与本地链接校验**：本讲的「坏链」实践里，`/no-such-page/` 为什么会被判坏？下一讲讲透 `docsify_candidates` 如何把一个路由翻译成多个候选文件。
- **u4-l4 远程链接的异步校验与片段验证**：本讲提到的 `RemoteResponse → RemoteResult` 翻译、HEAD/GET 回退、锚点验证，下一讲逐一拆解。
- **u4-l5 忽略规则、主流程编排与 CI 集成**：本讲的 `IgnoreRule.matches`、两层过滤、CI 触发条件，下一讲收口。
- **复习**：若对 Docsify 路由风格（`/path/` → `README.md`、`?id=` 锚点）还不够熟，建议回看 u1-l4 与 u3-l2。
