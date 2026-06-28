# 忽略规则、主流程编排与 CI 集成

> 第四单元（链接检查器架构）第五讲，也是本单元的收尾讲。前四讲分别拆解了「骨架与配置」「链接提取」「本地/Docsify 路由校验」「远程异步校验」。本讲把这些零件装回一条完整流水线，并补上最后两块：**忽略规则系统**与**CI 集成**。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `IgnoreRule` 的「合取匹配」语义：一条规则写了多个字段时，为什么是「全部满足」才算命中。
- 区分**链接层字段**（url/path/line 等）与**问题层字段**（category/status），并理解它们分别在两道过滤里起作用。
- 顺着 `check_links` 把「扫描 → 提取 → 过滤 → 本地校验 → 远程校验 → 过滤 → 统计」整条主流程走一遍，并读懂统计数字的含义。
- 看懂 `.github/workflows/check-links.yml`，说出 CI 在什么条件下会因链接检查失败而亮红。

## 2. 前置知识

本讲默认你已读过本单元前四讲（u4-l1 ~ u4-l4）。这里回顾几个会反复用到的概念：

- **Link**：一条被提取出来的链接，带 `source`（来源文件）、`line`、`column`、`url`。它有两个常用派生属性：`is_remote`（是否 `http(s)://` 或 `//` 开头）和 `normalized_url`（把 `//` 协议相对地址补成 `https://...`）。
- **Issue**：一条校验失败的记录，含 `link`、`category`（如 `local-missing-target`、`remote-http`）、`message`、可选的 `status`（HTTP 状态码）。
- **should_skip**：`mailto:`、`data:`、`javascript:` 等非网络协议的链接，检查器直接跳过，永不产生 Issue。
- **DEFAULT_CONFIG / deep_merge / check-links.toml**：默认配置与 TOML 配置按「键」递归合并，`[scan]`/`[http]`/`[[ignore]]` 三段（u4-l1 已讲）。

如果你还记得 u4-l1 提过的「两层过滤：先滤 Link、再滤 Issue」这句话——本讲就是要把它讲透。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `scripts/check_links.py` | 检查器全部逻辑。本讲精读其中 `IgnoreRule`、`load_ignore_rules`、`filter_ignored_links`/`filter_ignored_issues`、主流程 `check_links`、入口 `main`。 |
| `check-links.toml` | 检查器的 TOML 配置。本讲关注 `[[ignore]]` 段——仓库里唯一一条忽略规则（kira-cpp）就在这里。 |
| `.github/workflows/check-links.yml` | GitHub Actions 工作流，定义「何时跑、怎么跑、何时算失败」。 |

## 4. 核心概念与源码讲解

### 4.1 IgnoreRule 合取匹配

#### 4.1.1 概念说明

文档站里有少量「明知会失败、但故意保留」的链接。最典型的就是 `check-links.toml` 里的 kira-cpp：文档正文用它当例子，但它是一个尚未建好的示例仓库，GitHub 会返回 404。如果不加处理，每次跑检查器都会报这条坏链。

**忽略规则（IgnoreRule）** 就是用来告诉检查器「这条链接/这个问题我知道了，别再报」。它解决的问题是：**在不删改文档正文的前提下，让检查器对特定的链接或问题网开一面**。

一条忽略规则由若干「匹配字段」组成。核心语义是**合取（conjunction，即逻辑「与」）**：

> 一条规则里**所有**被填写的字段都必须匹配，这条规则才算命中。没有填写的字段（值为 `None`）视为「不约束」，自动跳过。

这和数据库查询里 `WHERE a AND b AND c` 的语义一致——条件越多，匹配面越窄。

#### 4.1.2 核心流程

判断一条规则是否「命中」某个对象时，`matches` 按字段逐个检查，**任何一个被填写的字段不满足，立刻返回「不命中」**：

```
对每个被填写的字段 f：
    若 f 是链接层字段（url / url_regex / path / path_glob / line）：
        若 link 不满足 f  →  返回 False（不命中）
    若 f 是问题层字段（category / status / statuses）：
        若 issue 不满足 f  →  返回 False（不命中）
全部通过  →  返回 True（命中）
```

字段分两层，这一点很关键：

- **链接层字段**：`url`、`url_regex`、`path`、`path_glob`、`line`——描述「哪些链接」在范围内。
- **问题层字段**：`category`、`status`、`statuses`——描述「哪些问题」要豁免。

第二层字段只有在「真的拿到了一个 Issue」时才有意义；只给你一条 Link，没法判断它的 `category`/`status`（还没校验呢）。代码用 `issue is None` 来区分这两种调用场景，详见 4.1.3。

#### 4.1.3 源码精读

先看规则本身的数据结构——一个 `frozen` dataclass，字段全部带默认值 `None`/空，外加一个纯文档用的 `reason`：

[scripts/check_links.py:150-160](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L150-L160) 定义了 `IgnoreRule` 的全部字段。注意 `reason` 排在最前，但它**只被存储、从不参与匹配**（你可以在整份脚本里搜索 `reason`，除了定义和赋值两处，再无引用）——它纯粹是写给读 TOML 的人看的注释。

核心是 `matches` 方法，它是「合取匹配」的字面实现：

[scripts/check_links.py:162-183](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L162-L183) 逐字段判定。摘取关键几行：

```python
def matches(self, link: Link, issue: Issue | None = None) -> bool:
    rel_path = link.source.as_posix()
    if self.url is not None and link.url != self.url and link.normalized_url != self.url:
      return False
    ...
    if self.path_glob is not None and not fnmatch.fnmatch(rel_path, self.path_glob):
      return False
    if self.line is not None and link.line != self.line:
      return False
    if issue is None:
      return True          # ← 只校验链接时，到这里就出结果
    if self.category is not None and issue.category != self.category:
      return False
    ...
    return True
```

读法：

1. **`url` 字段同时比对两种形态**：`link.url`（原始）和 `link.normalized_url`（`//` 补全为 `https://`）。只要其中一种相等就算这一关通过。这就是为什么用 `url = "https://..."` 也能命中正文里写成 `//...` 的协议相对链接。
2. **`url_regex` 用 `re.search`**（子串匹配，不是全串匹配），同样对两种 URL 形态都试一遍。
3. **`path` 是精确比对**（posix 相对路径全等），而 **`path_glob` 用 `fnmatch.fnmatch`**（支持 `*`/`?`/`[]` 通配）。
4. **`if issue is None: return True` 是分水岭**：当只传入 `link`（没有 issue）时，问题层字段（category/status/statuses）一律不检查，直接给结论。这行决定了「链接层过滤」和「问题层过滤」的行为差异，是 4.3 节的重点。

`[[ignore]]` 表是怎么变成 `IgnoreRule` 对象的？看加载函数：

[scripts/check_links.py:224-243](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L224-L243) 遍历 `config['ignore']` 列表，校验每一条必须是 TOML 表（dict），把 `statuses` 列表转成元组，然后逐字段 `item.get(...)` 构造 `IgnoreRule`。没有做「至少要填一个字段」之类的校验——这点会在 4.3 节带来一个值得注意的边界行为。

仓库里现存的唯一规则就在配置文件里：

[check-links.toml:21-23](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/check-links.toml#L21-L23) 是 kira-cpp 规则。它只填了链接层字段 `url` 和文档字段 `reason`，没有用任何问题层字段——意思是「凡是 URL 完全等于这个地址的链接，整体跳过，连校验都不做」。

#### 4.1.4 代码实践

**实践目标**：确认「合取匹配」与「`reason` 不参与匹配」两件事。

**操作步骤**：

1. 打开 [scripts/check_links.py:162-183](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L162-L183)，在 `matches` 方法体里搜索 `self.reason`——你会发现一次都没有。
2. 假想一条规则 `url = "https://github.com/pku-minic/kira-cpp"` 且 `line = 42`，对照代码推演：它会命中「第 42 行的那条 kira-cpp 链接」，但**不会**命中「第 43 行的同 URL 链接」（`line` 不满足 → 返回 False）。这就是合取：加了 `line` 约束，命中面变窄。

**需要观察的现象 / 预期结果**：`reason` 字段对命中结果零影响；多写一个字段只会让规则更严格、不会更宽松。本实践为纯源码阅读，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：一条规则同时写了 `url = "..."` 和 `category = "remote-http"`。这两者是「或」还是「与」关系？

**答案**：是「与」（合取）。代码里每个字段都是独立的 `if ... return False`，必须全部通过才返回 `True`。所以这条规则的含义是「URL 等于该值 **并且** 问题类别是 remote-http 才命中」。（不过它到底在哪一层生效，留到 4.3 节揭晓。）

**练习 2**：为什么 `url` 字段的判定要同时比对 `link.url` 和 `link.normalized_url` 两个值？

**答案**：因为正文里同一目标可能写成两种形态——`https://github.com/...` 或协议相对的 `//github.com/...`。`normalized_url` 会把 `//` 开头补成 `https:`，这样一条写死 `https://` 的忽略规则就能同时覆盖两种写法，作者不必各写一条。

### 4.2 主流程编排与统计

#### 4.2.1 概念说明

前面四讲分别造好了「提取」「本地校验」「远程校验」等零件。`check_links` 函数是**总装车间**：它决定零件的调用顺序、数据在它们之间怎么流动，并最后产出一份数据（`stats`）给命令行入口打印。

理解主流程的意义在于：今后你要给检查器加功能（比如新校验类型、新统计项），第一件事就是看懂这条流水线在哪里插入。

#### 4.2.2 核心流程

`check_links` 的编排可以画成一条单向流水线：

```
配置 → 文件类型
        ↓
      discover_files          扫描 README.md 与 docs/，排除 docs/assets/**
        ↓
      load_ignore_rules       把 [[ignore]] 表加载成 IgnoreRule 列表
        ↓
      extract_links           从每个文件抽取 Link（u4-l2）
        ↓
   ★ filter_ignored_links     第一层过滤：剔除「不想检查」的链接（u4-l1/4.3）
        ↓
      遍历每条 Link：
        · should_skip（mailto: 等）→ 跳过
        · is_remote             → 攒进 remote_links 字典（按 normalized_url 去重）
        · 否则                  → check_local_link 即时校验，产出 Issue（u4-l3）
        ↓
      若有远程链接且未 --no-http → check_remote_links 异步批量校验（u4-l4）
        ↓
   ★ filter_ignored_issues     第二层过滤：剔除「查出来但想豁免」的问题
        ↓
      统计 stats + 排序返回
```

两个带 ★ 的环节就是 4.3 节的「双层过滤」。注意一个去重细节：远程链接先按 `normalized_url` 攒批，**同一个 URL 无论在文档里出现多少次，只发一次网络请求**，校验完再把结果广播回每一次出现。

#### 4.2.3 源码精读

主流程函数签名与开头——准备文件类型、文件清单、忽略规则，并立刻做第一层过滤：

[scripts/check_links.py:657-664](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L657-L664) 关键三步：`discover_files` 找文件、`load_ignore_rules` 载入规则、`filter_ignored_links(extract_links(...), ignore_rules)` 把「提取」与「第一层过滤」嵌套在一次调用里。注意 `links` 已经是过滤后的结果，所以后面打印的 `len(links)` 是「净」链接数。

主循环——按链接类型分流：

[scripts/check_links.py:670-687](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L670-L687) 摘取分流逻辑：

```python
for link in links:
    if link.should_skip:
        continue
    if link.is_remote:
        if not no_http:
            remote_links.setdefault(link.normalized_url, []).append(link)
        continue
    ...
    issue = check_local_link(root, docs_root, link, file_types)
    if issue is not None:
        issues.append(issue)

if remote_links and not no_http:
    issues.extend(asyncio.run(check_remote_links(remote_links, config['http'], verbose)))
```

读法：

- `should_skip` 的链接（`mailto:`、`data:` 等）直接 `continue`，既不本地校验也不远程校验。
- 远程链接用 `normalized_url` 做 dict 的 key（`setdefault(...).append(link)`）——这就是「按 URL 去重攒批」的实现；同一个 URL 的多次出现挂进同一个列表。
- 本地链接**即时**校验（同步），远程链接**攒批后一次性**异步校验（`asyncio.run`）。这也是 u4-l1 说的「本地即时、远程攒批」。
- `--no-http` 会同时影响两处：远程链接不进 dict、也不触发异步校验，等于完全跳过远程。

收尾——第二层过滤、统计、排序：

[scripts/check_links.py:689-697](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L689-L697) 产出 `stats` 字典：

```python
issues = filter_ignored_issues(issues, ignore_rules)
stats = {
    'files': len(files),
    'links': len(links),
    'remote_links': sum(len(items) for items in remote_links.values()),
    'unique_remote_links': len(remote_links),
    'issues': len(issues),
}
```

四个统计口径要分清：

| 字段 | 含义 |
| --- | --- |
| `files` | 扫描到的候选文件总数（已排除 `docs/assets/**`）。 |
| `links` | 第一层过滤**之后**的链接数（被忽略的链接不计入）。 |
| `remote_links` | 远程链接的**出现次数**总和（同一 URL 出现 3 次算 3）。 |
| `unique_remote_links` | 远程链接的**去重 URL 数**（dict 的 key 数）。 |
| `issues` | 第二层过滤**之后**的问题数（被忽略的问题不计入）。 |

注意 `remote_links` 在 `--no-http` 时恒为 0，因为远程链接根本没进 dict。

#### 4.2.4 代码实践

**实践目标**：亲眼看到统计数字，并理解 `--no-http` 对远程计数的「清零」效果。

**操作步骤**：

1. 在仓库根目录执行：
   ```bash
   python scripts/check_links.py --config check-links.toml --no-http --verbose
   ```
2. 读末尾那行 `Checked N file(s), M link(s), K remote occurrence(s) (U unique).`

**需要观察的现象 / 预期结果**：`K`（remote occurrence）和 `U`（unique）都应是 `0`——因为 `--no-http` 让远程链接既不入字典、也不被校验。`M` 是过滤后的本地链接数。**待本地验证**：实际数值取决于你本地的文档快照，但 `remote_links=0` 这一点是确定的。

#### 4.2.5 小练习与答案

**练习 1**：为什么远程链接要先攒进 `remote_links` 字典、最后统一校验，而不是像本地链接那样边遍历边校验？

**答案**：两点好处。① **去重省请求**：同一 URL 在文档里出现多次时，按 `normalized_url` 做 key 只发一次网络请求，结果再广播回每一次出现；边遍历边查就会重复请求。② **可并发**：攒成一个集合后，`check_remote_links` 用 `asyncio.gather` 一次性并发派出所有请求（u4-l4），本地链接则是简单的同步函数，无需这套机制。

**练习 2**：`stats['links']` 和 `stats['issues']` 分别是在哪一层过滤之后算的？

**答案**：`links` 在**第一层**过滤（`filter_ignored_links`）之后算；`issues` 在**第二层**过滤（`filter_ignored_issues`）之后算。所以被 `[[ignore]]` 规则挡掉的链接不会进 `links`，被挡掉的问题不会进 `issues`。

### 4.3 链接/问题双层过滤

#### 4.3.1 概念说明

「双层过滤」是本检查器最精巧的设计之一。同样是「忽略」，其实有两种截然不同的诉求：

- **诉求 A——这条链接我根本不想查。** 比如 kira-cpp 必然 404，连发请求都是浪费。最好在校验**之前**就把这条 Link 从清单里删掉。
- **诉求 B——这条链接我想查，但查出来的某个具体问题我想豁免。** 比如某个远程页面对 HEAD 返回 403，我知道它其实是好的，只想豁免这一个 403 问题，而不想跳过整条链接的其他校验。

检查器用两道过滤分别满足这两种诉求：

| 层 | 函数 | 时机 | 传入 | 能用的规则字段 |
| --- | --- | --- | --- | --- |
| 第一层 | `filter_ignored_links` | 校验**之前** | 只传 `link`（issue=None） | 仅链接层字段 |
| 第二层 | `filter_ignored_issues` | 校验**之后** | 传 `link` + `issue` | 链接层 + 问题层字段 |

#### 4.3.2 核心流程

两个过滤器的结构几乎一样，差别只在调用 `matches` 时传不传 `issue`：

```
filter_ignored_links(links):
    保留那些「没有任何一条规则 matches(link)」的链接
    → matches 只校验链接层字段（issue=None 时第 175 行直接出结果）

filter_ignored_issues(issues):
    保留那些「没有任何一条规则 matches(issue.link, issue)」的问题
    → matches 校验链接层 + 问题层全部字段
```

「合取匹配」在两层都成立；区别仅在于第二层多了一组可用的字段（category/status/statuses）。

#### 4.3.3 源码精读

两个函数都只有一行，但这一行信息量很大：

[scripts/check_links.py:649-654](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L649-L654)

```python
def filter_ignored_links(links, ignore_rules):
  return [link for link in links if not any(rule.matches(link) for rule in ignore_rules)]

def filter_ignored_issues(issues, ignore_rules):
  return [issue for issue in issues if not any(rule.matches(issue.link, issue) for rule in ignore_rules)]
```

读法：

- 列表推导里的判据是 `not any(rule.matches(...))`——**只要有一条规则命中，就剔除**；没有任何规则命中，才保留。
- 第一个函数只传 `link`，对应 `matches` 里 `issue=None` 的分支，**问题层字段被跳过**。
- 第二个函数传 `issue.link` 和 `issue`，**两层字段都参与**。

把它们和主流程对上：第一层在 [scripts/check_links.py:661-662](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L661-L662)（提取后立即调用），第二层在 [scripts/check_links.py:689](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L689)（本地+远程校验都跑完之后）。

kira-cpp 是第一层的典型用例：它只填了 `url`（链接层字段），所以那条链接在第一层就被剔除，**根本不会进入本地/远程校验**，自然不会产生 Issue。这就是「诉求 A」。

> **⚠ 一个值得注意的边界行为**：因为 `matches` 在 `issue is None` 时，只要链接层字段都通过（或规则根本没填链接层字段）就会 `return True`，所以一条**只含问题层字段**（例如只有 `category`）的规则，会在第一层对**每一条**链接都返回「命中」，从而把所有链接整体剔除。换句话说，问题层字段实际上只有在第二层（带着 Issue）调用时才真正生效。实践建议：**写忽略规则时务必带上至少一个链接层字段**（url/url_regex/path/path_glob/line），把命中面收窄到具体的链接上——仓库里唯一的 kira-cpp 规则正是这么做的。

#### 4.3.4 代码实践

**实践目标**：用阅读 + 推演，确认「链接层字段在第一层就把链接删掉，问题层字段不再有机会生效」。

**操作步骤**：

1. 读 [scripts/check_links.py:649-654](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L649-L654) 与 [scripts/check_links.py:162-183](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L162-L183)。
2. 推演：若给 kira-cpp 规则再加一行 `category = "remote-http"`，行为会变吗？

**需要观察的现象 / 预期结果**：不会变。因为该规则的 `url` 字段在第一层就把 kira-cpp 链接删掉了，这条链接压根没去校验、没产生 Issue，`category` 这一关永远走不到。结论：**当一条规则带了能命中的链接层字段时，它的问题层字段其实是「冗余」的**——这正是为什么本仓库的规则只写 `url` 就够了。

#### 4.3.5 小练习与答案

**练习 1**：诉求 B（想查、但豁免某个具体状态码）应该怎么写规则？它会在哪一层生效？

**答案**：用问题层字段，比如 `url_regex = "某个会偶发 403 的域名"` 配 `status = 403`。因为带了链接层字段 `url_regex`，第一层会把这些链接整体删掉（见上一节的边界行为），403 豁免仍走不到。要真正实现「查了但豁免状态码」，需要一条**不带链接层字段**的纯问题层规则——但这又会触发「第一层清空所有链接」的边界行为。可见在当前实现下，「查了再豁免具体状态码」这类诉求较难干净地表达；这正是为什么本仓库只用了最简单的「整体跳过」式规则（诉求 A）。这道题意在让你发现设计与诉求之间的张力。

**练习 2**：`filter_ignored_links` 和 `filter_ignored_issues` 的列表推导结构几乎一模一样，唯一的实质差异是什么？

**答案**：调用 `rule.matches` 时传不传 `issue`。前者只传 `link`（命中只看链接层字段），后者传 `(issue.link, issue)`（链接层 + 问题层字段都参与）。这一处差异，决定了「能豁免整条链接」与「能豁免具体问题」两种能力的分野。

### 4.4 CI 触发与判定

#### 4.4.1 概念说明

检查器单跑只是一次手工验证；真正保证「文档里不混进坏链」的是 **CI（持续集成）**——在每次提交/PR 时自动跑一遍检查器，坏了就挡住合并。

本仓库用 **GitHub Actions** 实现 CI。理解 CI 要回答三个问题：**何时触发**（哪些事件、哪些路径）、**跑什么**（哪条命令）、**何时算失败**（退出码语义）。前两个在工作流 YAML 里，第三个在 `main()` 的返回值里。

#### 4.4.2 核心流程

```
事件触发（PR / push master / 手动）
        ↓
jobs.check-links（ubuntu-latest）
        ↓
actions/checkout            拉代码
        ↓
pip install -r requirements.txt   装 httpx / markdown-it-py / selectolax
        ↓
python scripts/check_links.py --config check-links.toml --verbose
        ↓
看退出码：0 = 通过；1 = 有坏链；2 = 配置错
        ↓
非 0 → 该 step 失败 → 整个 job 亮红
```

关键点：CI **不带 `--no-http`**，所以远程链接也会被真正请求一遍——这能在合并前抓出远程 404。

#### 4.4.3 源码精读

先看触发条件——三种事件，且都带 `paths` 过滤：

[.github/workflows/check-links.yml:3-22](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml#L3-L22) 定义了 `pull_request`、`push`（限 `master` 分支）、`workflow_dispatch`（手动）三种触发。三者都带相同的 `paths` 列表：只有改动落到 `README.md`、`docs/**`、`scripts/check_links.py`、`requirements.txt`、`check-links.toml`、`.github/workflows/check-links.yml` 之一时才触发。

再看 job 的三步：

[.github/workflows/check-links.yml:24-32](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml#L24-L32)

```yaml
steps:
  - uses: actions/checkout@v6
  - name: Install dependencies
    run: python -m pip install -r requirements.txt
  - name: Check documentation links
    run: python scripts/check_links.py --config check-links.toml --verbose
```

注意第 3 步**没有 `--no-http`**，且带了 `--verbose`——所以 CI 会打印 `Checked N file(s)...` 那行统计，便于在日志里排查。

最后，「何时算失败」要回到入口函数的退出码：

[scripts/check_links.py:711-737](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L711-L737) 关键三处返回：

```python
except ValueError as error:
    print(f'Configuration error: {error}', file=sys.stderr)
    return 2          # 配置错误
...
return 1 if issues else 0   # 有坏链 → 1；否则 → 0
```

退出码语义：`0` 正常、`1` 有坏链（issues 非空）、`2` 配置错误（如 TOML 文件不存在、`[[ignore]]` 项不是表）。GitHub Actions 的规则是「step 的命令退出非 0 即失败」，所以 `1` 和 `2` 都会让 CI 亮红。

把三段串起来，**CI 因链接检查失败的条件**是：

1. 存在至少一条**未被 `[[ignore]]` 豁免**的坏链（本地缺失目标/锚点，或远程返回不可接受的状态码）→ 退出码 1；或
2. 配置文件本身有问题 → 退出码 2。

反之，被 `[[ignore]]` 规则挡掉的链接（如 kira-cpp）不会进入 issues，不影响 CI；`should_skip` 的协议链接（mailto: 等）也不算坏链。

#### 4.4.4 代码实践

**实践目标**：把工作流 YAML 与入口退出码对上，说出 CI 的失败条件。

**操作步骤**：

1. 打开 [.github/workflows/check-links.yml](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/.github/workflows/check-links.yml)，列出三种触发事件和六类触发路径。
2. 对照 [scripts/check_links.py:737](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L737) 与 [:730](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L730)，把退出码 0/1/2 各对应什么情况写下来。

**需要观察的现象 / 预期结果**：你能复述「PR 或 push 到 master（且改了指定路径）时触发；跑检查器；退出 1（有坏链）或 2（配置错）即判失败」。本实践为纯阅读，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：为什么 CI 命令不带 `--no-http`？带或不带对保障质量有什么区别？

**答案**：不带 `--no-http` 意味着 CI 会真正请求所有远程链接，从而在合并前抓出远程 404/超时等问题。若带了 `--no-http`，远程坏链就完全不会被发现，CI 只能保本地链接。代价是 CI 依赖外网与目标站点可用，偶有误报（所以才有 `accepted_statuses` 把 401/403/429 等放行，见 u4-l4）。

**练习 2**：某次 PR 改动了 `docs/lv3-expr/foo.md` 里的一处错字，CI 会跑吗？为什么？

**答案**：会跑。因为 `docs/**` 在触发路径里，`docs/lv3-expr/foo.md` 匹配该通配。这正是把 `docs/**` 整体列入触发路径的意图——任何文档改动都可能引入或修复链接，都值得复查一遍。

## 5. 综合实践

把本讲的「忽略规则 + 主流程 + CI」串起来，做一个完整闭环实验。我们需要一条「明知会 404」的链接——仓库里现成的就是 kira-cpp。

**实践目标**：亲眼看到 `[[ignore]]` 规则如何把一条真实 404 从 CI 视线里抹掉，并据此说清 CI 的失败条件。

> ⚠ 本实践需要**联网**（要真正请求 GitHub）。请确保已 `pip install -r requirements.txt`。实验结束后**务必把 `check-links.toml` 还原**。

**操作步骤**：

1. **基线**：在仓库根目录跑（**不带** `--no-http`）：
   ```bash
   python scripts/check_links.py --config check-links.toml --verbose
   ```
   预期：末尾打印 `No broken links found.`——因为 kira-cpp 已被忽略。
2. **制造失败**：打开 `check-links.toml`，把现有的 kira-cpp `[[ignore]]` 段整段注释掉（每行前加 `#`），保存。
3. **重跑**：再次执行第 1 步的命令。
   - 预期：出现一条坏链报告，形如 `remote-http: HTTP 404`，URL 为 `https://github.com/pku-minic/kira-cpp`。
4. **新增规则**：把原段还原；再在它下面**新增**一条用不同字段写法的规则，验证「合取匹配」的另一种表达：
   ```toml
   [[ignore]]
   url_regex = "github\\.com/pku-minic/kira-cpp"
   reason = "练习用：用 url_regex 复刻 kira-cpp 忽略。"
   ```
5. **再跑**：再次执行第 1 步命令。
   - 预期：`No broken links found.`——新规则用 `url_regex`（子串匹配）同样命中并忽略了该链接。
6. **清理**：删掉第 4 步新增的练习规则（保留原 kira-cpp 段即可），把 `check-links.toml` 还原到提交前状态。可用 `git diff check-links.toml` 确认没有残留改动。

**需要观察的现象 / 预期结果**：

- 第 3 步应看到 kira-cpp 报 404（**待本地验证**：GitHub 对不存在的仓库通常返回 404，与配置里的 `reason` 描述一致）。
- 第 5 步该 404 消失，证明 `url_regex` 与 `url` 两种写法等价地忽略了同一目标。
- 第 2 步（有坏链）对应 `main()` 退出码 1；第 1/5 步（无坏链）对应退出码 0。可在每步后用 `echo $?` 查看上一条命令的退出码加以确认。

**据此回答「CI 在什么条件下会因链接检查失败」**：

- 当且仅当检查器退出码非 0——即存在**未被 `[[ignore]]` 豁免**的坏链（退出码 1），或配置文件本身有误（退出码 2）。
- 触发时机：PR、push 到 master（且改动落在六类触发路径之一）、或手动 `workflow_dispatch`。
- 像本实践第 2 步那样把 kira-cpp 的忽略规则去掉，CI 就会因这条 404 而失败；加回规则（第 4/5 步），CI 又会通过。这就是「忽略规则 ↔ CI 判定」的闭环。

## 6. 本讲小结

- **合取匹配**：`IgnoreRule` 写了多少字段，就要**全部**满足才算命中；不填的字段（`None`）不约束。`reason` 只供人读，不参与匹配。
- **字段分两层**：链接层（url/url_regex/path/path_glob/line）描述「哪些链接」，问题层（category/status/statuses）描述「哪些问题」；`matches` 在 `issue is None` 时只校验链接层。
- **主流程** `check_links`：扫描 → 加载规则 → 提取 → 第一层过滤 → 本地即时校验 + 远程攒批异步校验 → 第二层过滤 → 统计，按 `(文件, 行, URL)` 排序返回。
- **双层过滤**：第一层 `filter_ignored_links` 在校验前删链接（如 kira-cpp 整条不查），第二层 `filter_ignored_issues` 在校验后删具体问题；二者差别仅在调用 `matches` 时传不传 `issue`。写规则时务必带上链接层字段，把命中面收窄。
- **统计口径**：`links` 是第一层过滤后的数，`remote_links`/`unique_remote_links` 在 `--no-http` 时为 0，`issues` 是第二层过滤后的数。
- **CI 判定**：PR/push master（限路径）/手动三种触发；跑 `check_links.py --config check-links.toml --verbose`（**含远程校验**）；退出码 1（有坏链）或 2（配置错）即判失败，被 `[[ignore]]` 豁免的链接不计入。

## 7. 下一步学习建议

至此，第四单元（链接检查器架构）五讲已全部完成，你已经把仓库里唯一有分量的程序代码从头到尾读了一遍。接下来可以：

1. **动手扩展检查器**：试着加一种新的 `Issue.category`（例如「图片缺失 alt 文本」），在 `extract_markdown_links` 里识别、在主流程里收集、在 `print_issues` 里输出——这会逼你把本讲的主流程串起来改一遍。
2. **写一条真实忽略规则**：如果你在文档里发现一条偶发失败的远程链接，参照本讲的合取语义，用 `url` 或 `url_regex` 写一条 `[[ignore]]`，并写清楚 `reason`。
3. **横向对比**：把本检查器与社区工具（如 lychee、markdown-link-check）对比，思考「懂 Docsify 路由」「双层过滤」「远程锚点验证」这几个特性各自的价值。
4. **回到文档主线**：如果你是为「读懂编译实验文档」而来，工具部分已告一段落，建议回到第三单元，从 `docs/lv1-main/` 开始顺着 Lab 阅读编译器实现指引。
