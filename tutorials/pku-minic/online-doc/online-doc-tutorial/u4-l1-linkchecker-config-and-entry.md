# 链接检查器总览、配置与入口

> 本讲属于第四单元「链接检查器架构（核心代码）」的第一篇。从本讲起，我们正式进入仓库里**唯一有分量的程序代码** `scripts/check_links.py`。本讲只打地基：搞清楚这个工具**干什么、由哪些数据结构撑起、怎么读配置、怎么从命令行启动**。链接提取、Docsify 路由校验、远程异步校验、忽略规则与 CI 等细节留给后续四讲。

---

## 1. 本讲目标

学完本讲，你应当能够：

- 说清链接检查器要解决什么问题，并能画出它从「扫描文件」到「输出问题」的主流程。
- 认识 `Link` / `Issue` / `RemoteResponse` / `RemoteResult` / `FileTypes` / `IgnoreRule` 这六个核心 `dataclass`，知道各自承载什么信息。
- 理解 `DEFAULT_CONFIG` 默认配置、TOML 配置文件，以及 `deep_merge` 如何把二者「递归合并」（而不是简单覆盖整个段）。
- 掌握 `argparse` 命令行入口的四个参数，以及 `main()` 返回的退出码 `0/1/2` 各自意味着什么、如何决定 CI 是否失败。

---

## 2. 前置知识

本讲默认你已经读过：

- **u1-l3 仓库目录结构一览**：知道 `scripts/check_links.py` 是仓库唯一的工程代码，配合 `check-links.toml`、`requirements.txt`、`.github/workflows/check-links.yml` 构成链接检查闭环；知道它扫描 `README.md` 与整个 `docs/`，但排除 `docs/assets/**`。
- **u1-l4 / u3-l2**：了解 Docsify 的路由风格——`/path/to/page` 指向 `docs/path/to/page.md`、`/path/to/` 指向目录的 `README.md`、`?id=标题` 指向标题锚点。**这正是检查器要校验的对象**：文档里写出的每一条站内路由链接，都必须真实对应一个文件或锚点。

此外，本讲会用到一点 Python 基础，建议先了解（不了解也能跟着读）：

| 概念 | 一句话解释 |
| --- | --- |
| `dataclass` | 用装饰器自动生成 `__init__` 等方法的「数据类」，适合装纯粹的数据。 |
| `frozen=True` | 让 dataclass 实例**不可变**，像元组一样创建后不能改字段。 |
| 类型注解 | `x: int`、`list[str]`、`str \| None` 这类标注，只给人和工具看，运行时不强制。 |
| `tomllib` | Python 3.11+ 标准库内置的 TOML 解析器（无需 `pip install`）。 |
| `argparse` | 标准库里用来解析命令行参数（如 `--config`、`--verbose`）的模块。 |

> 提示：本仓库的 CI 跑在 `ubuntu-latest` 上，本地复现请使用 **Python 3.11 及以上**，否则 `import tomllib` 会失败。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [scripts/check_links.py](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py) | 链接检查器全部逻辑，单文件约 740 行。 | 文件头注释、`DEFAULT_CONFIG`、六个 dataclass、`deep_merge`/`load_config`、`check_links` 主流程、`main` 入口。 |
| [check-links.toml](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/check-links.toml) | 项目自定义配置：扫描范围、HTTP 行为、忽略规则。 | 配置分段与「覆盖默认值」的关系。 |
| [requirements.txt](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/requirements.txt) | 检查器依赖的三个第三方库。 | 三个依赖各自服务于哪一步。 |

`requirements.txt` 里的三个包分别对应检查器内部的三件事（后续讲义会逐个用到）：

- **`httpx`**：发 HTTP 请求校验**远程**链接（第四讲异步校验用）。
- **`markdown-it-py[linkify]`**：把 Markdown 解析成 token 流，从中抽取链接/图片，并自动识别「裸 URL」（第二讲链接提取用）。
- **`selectolax`**：解析 HTML 片段，抽取 `href`/`src` 等链接属性（第二、三讲用）。

> 本讲会**导入并用到**这三个库（因为 `check_links.py` 顶部就 `import` 了它们），但不会深入它们的用法。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **工具职责与流程概览**——它解决什么问题、整体怎么跑。
2. **核心 dataclass**——撑起整个程序的六块「数据积木」。
3. **TOML 配置与 `deep_merge`**——默认值与配置文件如何合并。
4. **`argparse` 入口**——命令行参数与退出码契约。

---

### 4.1 工具职责与流程概览

#### 4.1.1 概念说明

文档站里有大量链接：站内路由（`/lv1-main/`、`/misc-app-ref/koopa?id=类型`）、图片、外链（GitHub、规范网页）。只要有人改了文件名、删了标题、或者某个外链失效了，链接就会「断」。**链接检查器**就是一个能在本地或 CI 里自动把这些断链找出来的脚本。

它的设计目标可以从文件头注释一眼读出——这段注释同时交代了它「懂 Docsify 路由」和「远程链接先 HEAD 后 GET」两条核心约定：

[scripts/check_links.py:3-15](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L3-L15) —— 文件头 docstring，说明工具职责与 Docsify 路由约定、远程校验策略。

注意这三条路由约定，它们和 u1-l4 / u3-l2 学到的 Docsify 路由完全对齐：

- `/path/to/page` → 解析成 `docs/path/to/page.md`；
- `/path/to/` → 解析成 `docs/path/to/README.md`；
- `?id=heading` → 解析成目标 Markdown 文件里的某个标题锚点。

#### 4.1.2 核心流程

整个工具的主流程封装在一个函数 `check_links` 里。先用文字描述它做的事，再看代码：

```
配置 ──► 1. 发现文件 discover_files
         │   （按 scan.paths 找，按 scan.exclude 排除，只留 .md/.html）
         │
         ├──► 2. 加载忽略规则 load_ignore_rules
         │
         ├──► 3. 提取链接 extract_links + filter_ignored_links
         │   （从每个文件抽出所有链接，先过一遍「链接级」忽略）
         │
         ├──► 4. 逐条分发：
         │      • should_skip（mailto:/data: 等）→ 跳过
         │      • is_remote（http(s)://）→ 暂存进 remote_links（除非 --no-http）
         │      • 否则 → 本地校验 check_local_link
         │
         ├──► 5. 若有远程链接且未 --no-http → 异步批量校验 check_remote_links
         │
         ├──► 6. filter_ignored_issues（再过一遍「问题级」忽略）
         │
         └──► 7. 统计 stats + 排序后返回 issues
```

这里有两个关键设计，先记住结论，细节在后四讲：

- **链接/问题双层过滤**：先在「链接」层面忽略（第 3 步，`filter_ignored_links`），再在「问题」层面忽略（第 6 步，`filter_ignored_issues`）。前者能让一条链接**根本不被检查**（比如不校验某个外链），后者能让一条已查出的问题**不报出来**（比如按状态码忽略）。这是 u4-l5 忽略规则的重点。
- **本地同步、远程异步**：本地校验在普通 `for` 循环里逐条做（读本地文件很快）；远程校验攒一批后用 `asyncio` 并发发出（u4-l4 主题）。`--no-http` 会让第 5 步整段跳过，纯本地快速跑。

#### 4.1.3 源码精读

`check_links` 的签名与编排逻辑：

[scripts/check_links.py:657-666](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L657-L666) —— 主流程开头：构建文件类型、发现文件、加载忽略规则、提取并过滤链接，并打印 `Scanning N link(s)...`。

分发循环（对应上面流程图的第 4 步）：

[scripts/check_links.py:670-683](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L670-L683) —— 逐条分发：跳过特殊协议、远程链接暂存、本地链接当场校验并打印 `PASS`/`FAIL`。

远程汇总与统计（第 5–7 步）：

[scripts/check_links.py:685-697](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L685-L697) —— 用 `asyncio.run` 跑远程校验，再做问题级忽略过滤，最后拼出 `stats` 字典并按「文件、行号、url」排序返回。

注意 `stats` 这个字典（第 690–696 行）——它就是 `--verbose` 最后那行总结输出的数据来源，本讲实践任务要重点观察它。

输出问题的函数：

[scripts/check_links.py:700-708](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L700-L708) —— `print_issues`：无问题打印 `No broken links found.`，否则逐条打印 `文件:行:列` + 类别 + 信息。

#### 4.1.4 代码实践

**实践目标**：跑通工具，读懂它的输出格式。

**操作步骤**：

1. 确认已装依赖：`pip install -r requirements.txt`（需要 Python 3.11+）。
2. 在仓库根目录运行（加 `--no-http` 跳过远程，只做本地快速校验）：
   ```bash
   python3 scripts/check_links.py --config check-links.toml --no-http --verbose
   ```

**需要观察的现象**：

- `--verbose` 会逐条打印 `Checking local link: ... PASS/FAIL`，开头打印 `Scanning N link(s)...`。
- 结尾会打印一行总结，格式**固定**（来自 `main` 的第 732–736 行）：
  ```
  Checked {files} file(s), {links} link(s), {remote_links} remote occurrence(s) ({unique_remote_links} unique).
  ```
  其中 `--no-http` 时 `remote occurrence(s)` 与 `unique` 都应为 `0`（因为远程链接根本没被收集）。
- 如果本地链接全部健康，最后一行是 `No broken links found.`。

**预期结果**：命令成功跑完；`files` 应是 `README.md` 加上 `docs/` 下所有 `.md`/`.html`（扣除 `docs/assets/**`）的数量；`remote occurrence(s)` 为 `0`。**具体数字待本地验证**（取决于当前 `docs/` 的文件数，无法在讲义里给出确定值）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--no-http` 模式下，总结里的 `remote occurrence(s)` 一定是 `0`？

> **参考答案**：分发循环里只有 `if not no_http` 时才会把远程链接 `setdefault` 进 `remote_links`（第 673–675 行）；`--no-http` 时远程链接既不收集、也不校验，所以 `stats['remote_links']` 恒为 `0`。

**练习 2**：如果想让某个外链「连查都不查」（而不是查完再忽略），应该用第 3 步的 `filter_ignored_links` 还是第 6 步的 `filter_ignored_issues`？

> **参考答案**：用 `filter_ignored_links`（链接级过滤）。它在提取之后、校验之前生效，被命中的链接不会进入 `remote_links`，也就不会真的发请求。

---

### 4.2 核心 dataclass

#### 4.2.1 概念说明

整个程序的数据在函数间流动，需要一个统一的「容器」来描述「一条链接」「一个问题」「一次远程请求的结果」。Python 的 `dataclass` 正合适：它自动生成构造函数，字段一目了然。`check_links.py` 一口气定义了六个 dataclass，按用途分三组：

| 分组 | dataclass | 装的是什么 |
| --- | --- | --- |
| 输入 | `Link` | 一条被发现的链接（来自哪个文件、第几行第几列、url 是什么）。 |
| 输出 | `Issue` | 一条查出的问题（哪条链接、什么类别、什么描述、HTTP 状态码）。 |
| 远程校验中间量 | `RemoteResponse` / `RemoteResult` | 一次 HTTP 请求的「原始响应」/「判定结论」。 |
| 配置/规则 | `FileTypes` / `IgnoreRule` | 哪些扩展名算文档 / 一条忽略规则怎么匹配。 |

> 风格小贴士：这六个 dataclass 用的是 **2 空格缩进**（仓库其余 Python 风格未必一致）。读源码时别误以为是排版错误——这是作者的选择，照原样理解即可。

#### 4.2.2 核心流程

`Link` 是最核心的一个，它在「提取 → 校验 → 输出」全程被传递。它的特别之处在于：虽然字段只有四个（`source`/`line`/`column`/`url`），但它用 `@property` 派生出了三个判断：

- `normalized_url`：以 `//` 开头的协议相对 URL 补上 `https:` 前缀（远程去重要用）。
- `is_remote`：是否是 `http://`/`https://`/`//` 开头。
- `should_skip`：是否是空 url 或 `mailto:`/`data:`/`file:` 等特殊协议（这些不该当网页链接校验）。

创建 `Link` 时还会触发 `__post_init__`，把 url 做 `html.unescape`（反转义 `&amp;` 之类）并 `strip`——这样后续比较的都是「干净」的 url。

`Issue` 的 `category` 字段取值是固定的字符串常量，整个程序只出现三种：

| category | 含义 | 出处 |
| --- | --- | --- |
| `local-missing-target` | 站内链接的目标文件不存在 | `check_local_link` |
| `local-missing-anchor` | 目标文件在，但锚点（标题）不存在 | `check_local_link` |
| `remote-http` | 远程链接校验失败 | `check_remote_links` |

记住这三个词，看 `--verbose` 输出时就能对上号。

#### 4.2.3 源码精读

`Link` 全貌（注意 `frozen=True` 与 `__post_init__`）：

[scripts/check_links.py:78-109](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L78-L109) —— `Link` dataclass：四字段 + `__post_init__` 清洗 url + 三个 `@property` 判断。

其中 `__post_init__` 用了一个小技巧：因为 `frozen=True` 的实例不能直接赋值，所以用 `object.__setattr__` 绕过冻结限制来清洗 `url`：

[scripts/check_links.py:96-97](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L96-L97) —— 在冻结实例上改写 `url`：先 `html.unescape` 再 `strip`。

`should_skip` 引用的 `SKIPPED_SCHEMES` 集合定义在类体内（第 80–89 行），列出了 `data`/`file`/`ftp`/`mailto`/`tel` 等不校验的协议。

`Issue` 与两个远程结果类都很简短：

[scripts/check_links.py:112-132](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L112-L132) —— `Issue`（链接+类别+信息+状态码）、`RemoteResponse`（原始响应：状态码/内容类型/正文/最终URL）、`RemoteResult`（判定结论：是否ok/信息/状态码）。

区分 `RemoteResponse` 与 `RemoteResult` 很关键：前者是「HTTP 给了我什么」（客观响应），后者是「我认为这条链接算不算过」（主观判定）。第四讲会看到 `evaluate_remote_response` 负责把前者翻译成后者。

`FileTypes` 与 `IgnoreRule`（`IgnoreRule` 的匹配细节是 u4-l5 的主题，这里只看它的字段）：

[scripts/check_links.py:135-147](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L135-L147) —— `FileTypes`：持有「算 Markdown 的扩展名集合」与「算 HTML 的扩展名集合」，并提供 `is_markdown`/`is_html`/`is_candidate` 三个判断。

[scripts/check_links.py:150-183](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L150-L183) —— `IgnoreRule`：八个可选匹配字段（`url`/`url_regex`/`path`/`path_glob`/`line`/`category`/`status`/`statuses`）+ `reason`，`matches()` 做合取匹配。

#### 4.2.4 代码实践

**实践目标**：亲手构造 `Link`，验证它的派生属性与清洗行为。

`scripts/` 目录没有 `__init__.py`，不是 Python 包，所以不能直接 `from scripts.check_links import ...`。下面这段**示例代码**用 `sys.path` 把 `scripts/` 加进导入路径，再 `import check_links`（模块顶层只创建了正则和 Markdown 解析器，`if __name__ == '__main__'` 守卫保证 `main()` 不会被触发，导入是安全的）：

```python
# 示例代码：在仓库根目录执行 python3 scratch_link.py
import sys
from pathlib import Path
sys.path.insert(0, 'scripts')
import check_links as cl

# 1) url 会被 html.unescape + strip
l1 = cl.Link(Path("docs/x.md"), 3, 5, "  https://ex.com/a?b=%3C  ")
print(repr(l1.url))        # 期望：'https://ex.com/a?b=<'  （%3C 被反转义为 <，首尾空格去掉）

# 2) is_remote / should_skip 判断
print(l1.is_remote)        # 期望：True
print(cl.Link(Path("x"), 1, 1, "mailto:a@b.com").should_skip)  # 期望：True
print(cl.Link(Path("x"), 1, 1, "/lv1-main/").is_remote)        # 期望：False（站内路由）

# 3) frozen 实例不能直接改字段
try:
    l1.url = "x"
except Exception as e:
    print(type(e).__name__)  # 期望：FrozenInstanceError
```

**需要观察的现象**：第 1 步 `url` 已被清洗；第 2 步三种判断符合预期；第 3 步直接赋值会抛 `FrozenInstanceError`（这正是 `frozen=True` 的效果）。

**预期结果**：如上注释所述。若 `import check_links` 报 `ModuleNotFoundError: No module named 'httpx'`，说明依赖未装，先 `pip install -r requirements.txt`。

#### 4.2.5 小练习与答案

**练习 1**：`RemoteResponse` 和 `RemoteResult` 都有 `status` 字段，为什么得分成两个类？

> **参考答案**：`RemoteResponse.status` 是「服务器实际返回的状态码」（客观事实），`RemoteResult.status` 是「这次判定所依据的状态码」（可能来自 HEAD 兜底等场景）。更重要的是语义：`RemoteResponse` 描述「响应」，`RemoteResult` 描述「结论（ok/不 ok）」。把客观响应与主观判定分开，能让「状态码相同但结论不同」（如 403 被判为通过）的情况表达清楚。

**练习 2**：`Link` 用了 `frozen=True`，却在 `__post_init__` 里改了 `url`，这不矛盾吗？

> **参考答案**：不矛盾。`frozen=True` 禁止**常规赋值**（`l.url = ...` 会抛错），但 `object.__setattr__(self, 'url', ...)` 是绕过冻结机制的底层接口。作者用它做「构造完成时的一次性清洗」，之后实例依然对外不可变。

---

### 4.3 TOML 配置与 `deep_merge`

#### 4.3.1 概念说明

检查器的行为（扫哪些目录、HTTP 超时多久、忽略哪些链接）不该写死在代码里。本项目用 **TOML** 作为配置格式（`check-links.toml`），并在代码里内置一份 `DEFAULT_CONFIG` 作为兜底默认值。

这里有一个**关键设计**：用户配置不是「整段替换」默认配置，而是「递归合并」。例如默认 `[http]` 段里有 `workers = 8`、`timeout = 15`、`retries = 1` 等很多项；如果用户的 TOML 里 `[http]` 只写了 `workers = 16`，正确的结果是「`workers` 变 16，其余各项保持默认」——而不是「`[http]` 只剩一个 `workers`，其余全丢」。这个「递归合并」由 `deep_merge` 实现。

> 名词解释——**TOML**：一种类似 INI 的配置文件格式，用 `[section]` 分段、`key = value` 赋值、`[[array]]` 表示数组里的一个元素（如本项目的 `[[ignore]]`）。Python 3.11+ 标准库 `tomllib` 可直接解析。

#### 4.3.2 核心流程

配置加载分两步：

```
DEFAULT_CONFIG（代码内置）
        │
        │  deep_merge(默认, 用户TOML)
        ▼
   合并后的最终配置 dict
        │
        ├──► configured_file_types → FileTypes（用 scan.md_extensions 等）
        ├──► load_ignore_rules    → [IgnoreRule]（用 ignore 段）
        └──► check_remote_links   → 用 http 段（超时/并发/可接受状态码）
```

合并规则要点（`deep_merge` 的两条分支）：

- **字典遇字典 → 递归合并**：`override[key]` 和 `base[key]` 都是 dict 时，对这一段再调一次 `deep_merge`。
- **其余情况 → 直接覆盖**：包括「字典遇非字典」「列表遇列表」。**尤其注意：列表是被整体替换的，不是拼接的**。所以 TOML 里写 `paths = [...]` 会**完全替换**默认的 `paths`，而不是追加。

还有一个细节：`deep_merge` 第一行先对 `base` 的每个 dict 值做了 `.copy()`，目的是**不污染全局的 `DEFAULT_CONFIG`**——否则第一次合并就会把默认值改坏，影响后续（虽然本程序每次运行只合一次，但这是防御性写法）。

#### 4.3.3 源码精读

`DEFAULT_CONFIG` 全貌（注意它和 `check-links.toml` 的对应关系）：

[scripts/check_links.py:37-56](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L37-L56) —— 内置默认配置：`scan`（扫描范围/扩展名/排除/文档根）、`http`（超时/重试/并发/User-Agent/可接受状态码/片段校验）、`ignore`（空列表）。

把它和 [check-links.toml:1-24](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/../../../../check-links.toml) 对照看（这里给出仓库内直链 [check-links.toml](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/check-links.toml)），可以读出每个配置段的作用：

| 配置段 | 作用 | 与默认值的差异 |
| --- | --- | --- |
| `[scan]` `paths` | 扫描入口：根目录的 `README.md` 与整个 `docs/` 目录。 | 与默认相同（整体替换，值恰好一致）。 |
| `[scan]` `md_extensions` / `html_extensions` | 只把 `.md` / `.html` 当文档处理。 | 与默认相同。 |
| `[scan]` `exclude` | 排除 `docs/assets/**`（纯静态资源，不校验）。 | 与默认相同。 |
| `[scan]` `docs_root` | Docsify 文档根是 `docs`（路由解析的基准）。 | 与默认相同。 |
| `[http]` `workers` | 远程校验并发数。 | **`16` 覆盖默认 `8`**——典型的「合并而非替换」体现。 |
| `[http]` `timeout`/`retries`/`user_agent` | 单次请求超时 15s、重试 1 次、自定义 UA。 | 与默认相同。 |
| `[http]` `accepted_statuses` | 额外判为通过的状态码：`401/403/429`（需登录/限流，不代表链接坏）。 | 与默认相同。 |
| `[http]` `accepted_status_ranges` | `200–399` 视为通过（含 3xx 重定向）。 | 与默认相同。 |
| `[http]` `check_fragments` / `max_fragment_page_bytes` | 校验远程锚点；抓取页面最多 2MB。 | 与默认相同。 |
| `[[ignore]]` | 忽略 `kira-cpp` 仓库的 404（文档已声明它未完成）。 | 默认是 `[]`，这里**追加**一条规则（注意：`ignore` 是列表，会被整体替换为含一条规则的列表）。 |

`deep_merge` 实现（核心就是上面说的两条分支）：

[scripts/check_links.py:186-194](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L186-L194) —— `deep_merge(base, override)`：先浅拷贝 base 的 dict 值；遍历 override，dict 遇 dict 则递归合并，其余直接覆盖（列表也是直接覆盖）。

`load_config` 把默认值、TOML 文件、合并三者串起来：

[scripts/check_links.py:197-206](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L197-L206) —— `load_config`：不传 `--config` 时直接返回 `DEFAULT_CONFIG`；否则把相对路径拼到 `root` 下，文件不存在抛 `ValueError`，存在则 `tomllib.load` 后与默认值 `deep_merge`。

> 注意第 198–199 行：如果不传 `--config`，**完全不用 TOML 文件**，直接用代码里的默认值。这就是为什么 `python3 scripts/check_links.py`（不带 `--config`）也能跑，只是行为是默认的、且不会读取 `check-links.toml` 里的 `[[ignore]]` 规则。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「字典递归合并、列表整体替换」的差异，并验证 `workers` 被覆盖成 16。

**示例代码**（在仓库根目录运行）：

```python
# 示例代码：python3 scratch_merge.py
import sys
sys.path.insert(0, 'scripts')
import check_links as cl
from pathlib import Path

# 用真实的 load_config 读取项目配置
cfg = cl.load_config(Path("."), "check-links.toml")

# 1) 字典递归合并：http.workers 被 toml 覆盖成 16，其余 http 项保留默认
print("workers:", cfg["http"]["workers"])   # 期望：16（被覆盖）
print("timeout:", cfg["http"]["timeout"])   # 期望：15（保留默认）

# 2) 列表整体替换：scan.paths 与默认值相同，但 ignore 列表被替换为「含一条规则」
print("ignore count:", len(cfg["ignore"]))  # 期望：1（kira-cpp 规则）

# 3) 直接演示列表不拼接
demo = cl.deep_merge({"a": {"x": 1, "y": 2}, "lst": [1, 2]},
                     {"a": {"x": 9}, "lst": [3]})
print(demo)  # 期望：{'a': {'x': 9, 'y': 2}, 'lst': [3]}  ← lst 不是 [1,2,3]
```

**需要观察的现象**：`workers` 变 16 而 `timeout` 仍是 15（证明 http 段是合并的）；`ignore` 长度为 1（证明列表被替换）；`demo` 的 `lst` 是 `[3]` 而非 `[1,2,3]`（证明列表不拼接）。

**预期结果**：如注释所述。若想进一步观察，可在本地**临时**修改 `check-links.toml`（例如把 `[scan]` 的 `exclude` 改成同时排除另一个目录），重跑 `--no-http --verbose`，对比总结行里 `files` 数量的变化；**改完务必用 `git checkout check-links.toml` 还原**，避免污染仓库。

#### 4.3.5 小练习与答案

**练习 1**：如果某天 `check-links.toml` 的 `[http]` 段被整段删掉，程序会崩溃吗？远程校验还会跑吗？

> **参考答案**：不会崩溃。`deep_merge` 会保留 `DEFAULT_CONFIG` 里完整的 `[http]` 段（因为 toml 没提供该段，递归合并时该 key 不在 override 里，原样保留）。远程校验仍会用默认的 `workers=8`、`timeout=15` 等跑。

**练习 2**：为什么 `ignore` 在 TOML 里写了一条规则后，`DEFAULT_CONFIG['ignore']` 的 `[]`「消失」了，而不是变成「默认空列表 + 新规则」？

> **参考答案**：因为 `ignore` 是**列表**，`deep_merge` 对列表走「直接覆盖」分支（`merged[key] = value`），不做拼接。所以 toml 的 `[[ignore]]` 生成的列表整体替换了默认的空列表。这也提醒写配置时：凡是你写了的列表字段，必须写全。

---

### 4.4 `argparse` 入口

#### 4.4.1 概念说明

命令行工具需要一个「入口」：解析用户敲的参数、把控制权交给主流程、最后用一个**退出码（exit code）**告诉操作系统是成功还是失败。退出码非常关键——CI（GitHub Actions）正是靠它判断「这次链接检查过没过」。

本项目用标准库 `argparse` 做参数解析。它提供四个参数：

| 参数 | 类型 | 作用 |
| --- | --- | --- |
| `--config` | 路径，默认 `None` | TOML 配置文件；不传则用 `DEFAULT_CONFIG`。 |
| `--root` | 目录，默认 `.` | 仓库根目录，所有相对路径以它为基准。 |
| `--no-http` | 开关（`store_true`） | 跳过所有远程链接校验，只做本地快速检查。 |
| `--verbose` | 开关（`store_true`） | 打印逐条检查过程与最后的统计总结行。 |

#### 4.4.2 核心流程

`main()` 的执行顺序：

```
解析参数 ──► 解析 root 为绝对路径
        │
        ├──► load_config(root, --config)   # 可能抛 ValueError
        ├──► check_links(root, config, no_http, verbose)
        │
        ├─ 若 ValueError ─► 打印 "Configuration error: ..." 到 stderr，return 2
        │
        └─ 正常 ─► print_issues(issues)
                   若 --verbose 打印统计行
                   return 1 if issues else 0
```

退出码是一份与 shell/CI 的「契约」，可以写成分段函数：

\[
\text{exit\_code} =
\begin{cases}
2 & \text{配置错误（如 TOML 文件不存在、\texttt{[[ignore]]} 不是表）} \\
1 & \text{存在失效链接（}\textit{issues}\neq\varnothing\text{）} \\
0 & \text{全部通过}
\end{cases}
\]

非零退出码会让 shell 判定「命令失败」，CI 据此把这次检查标红（u4-l5 会看到 CI 直接调用本入口）。

> 名词解释——**stderr**：标准错误流。配置错误信息打到 `stderr`（`file=sys.stderr`），与正常输出（`stdout`）分流；这样即使你把 `stdout` 重定向到文件，错误信息也不会被吞掉。

#### 4.4.3 源码精读

`main` 的参数解析部分：

[scripts/check_links.py:711-724](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L711-L724) —— 构建 `ArgumentParser`，注册四个参数，解析后把 `--root` 解析为绝对路径。

执行与错误处理（注意 `try/except ValueError` 对应退出码 2）：

[scripts/check_links.py:725-737](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L725-L737) —— 加载配置并运行；捕获 `ValueError` 打到 stderr 并 `return 2`；正常时打印问题、可选打印统计行，最后 `return 1 if issues else 0`。

注意第 728–730 行：`load_config` 在文件不存在时抛 `ValueError`（见 4.3.3 的第 203–204 行），`load_ignore_rules` 在 `[[ignore]]` 不是 TOML 表时也抛 `ValueError`（第 228 行）——这些都被这里统一捕获成退出码 2，把「配置问题」和「链接问题」清晰区分开。

统计行的格式串（`--verbose` 才打印）：

[scripts/check_links.py:732-736](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L732-L736) —— 用 `stats` 字典填充格式串，打印 `Checked N file(s), M link(s), ...`。

最后是模块入口约定：

[scripts/check_links.py:740-741](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L740-L741) —— `if __name__ == '__main__': raise SystemExit(main())`：只有直接运行本文件时才执行 `main()`，并用其返回值作为进程退出码。这也正是 4.2.4 里「`import check_links` 不会触发检查」的原因。

#### 4.4.4 代码实践

**实践目标**：验证四个参数的效果与三种退出码。

**操作步骤**：

1. 看帮助文本（`argparse` 自动生成，不需装依赖也能看参数说明）：
   ```bash
   python3 scripts/check_links.py --help
   ```
2. 触发退出码 2（指向不存在的配置文件）：
   ```bash
   python3 scripts/check_links.py --config /tmp/does-not-exist.toml
   echo "exit=$?"
   ```
3. 对比 `--no-http` 与完整模式（完整模式会真的发网络请求，较慢）：
   ```bash
   python3 scripts/check_links.py --config check-links.toml --no-http --verbose; echo "exit=$?"
   ```

**需要观察的现象**：

- `--help` 列出 `--config`/`--root`/`--no-http`/`--verbose` 四项及说明。
- 第 2 步：stderr 打印 `Configuration error: configuration file not found: ...`，`exit=2`。
- 第 3 步：正常输出 + 统计行，若无断链 `exit=0`。

**预期结果**：退出码严格符合上面的分段函数。第 2 步一定是 `exit=2`；第 3 步的 `exit` 取决于当前是否存在断链（**待本地验证**，但只要仓库健康应为 `0`）。

#### 4.4.5 小练习与答案

**练习 1**：为什么不传 `--config` 时，`check-links.toml` 里的 `kira-cpp` 忽略规则**不生效**？

> **参考答案**：`load_config` 在 `config_path is None` 时直接 `return DEFAULT_CONFIG`（第 198–199 行），根本没读 TOML 文件，所以 `ignore` 段是默认的空列表，`kira-cpp` 规则不存在。要让忽略规则生效，必须传 `--config check-links.toml`。

**练习 2**：CI 里如果某条外链返回 404，进程退出码是多少？CI 会因此失败吗？

> **参考答案**：退出码是 `1`（`issues` 非空）。CI 用非零退出码判定失败，所以这次检查会标红——除非该外链被某条 `[[ignore]]` 规则命中（`kira-cpp` 就是这种「明知 404 但忽略」的例子）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「跑通 + 解读 + 故障注入」的全流程：

1. **跑通并解读输出**：执行
   ```bash
   python3 scripts/check_links.py --config check-links.toml --no-http --verbose
   ```
   记下结尾统计行的 `files`、`links` 两个数字。然后回答：`files` 这个数由 4.3 表格里哪几个 `[scan]` 配置共同决定？（答：`paths`（入口）、`md_extensions`/`html_extensions`（哪些算文档）、`exclude`（排除 `docs/assets/**`）。）

2. **故障注入 A——制造一条本地断链**：在某份 `docs/` 下的 Markdown 里**临时**加一条指向不存在页面的路由（如 `[坏链](/lv99-none/)`），重跑同一条命令。观察输出里多出的 `- 文件:行:列: /lv99-none/` 与 `local-missing-target: target does not exist; tried ...`，并确认 `exit=1`。**记得随后删除这行还原。**

3. **故障注入 B——制造配置错误**：执行 `python3 scripts/check_links.py --config /tmp/nope.toml; echo "exit=$?"`，确认 stderr 的 `Configuration error` 与 `exit=2`。

4. **对照源码解释**：针对步骤 2 的输出，在 [scripts/check_links.py:492-504](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L492-L504) 找到生成 `local-missing-target` 的代码，解释 `tried` 后面列出的候选路径是怎么来的（这是 u4-l3 的预告，本讲只需指出「它来自 `docsify_candidates` 返回的候选列表」即可）。

> 完成后，你就把「流程概览 → dataclass（Issue/category）→ 配置（scan 段驱动 files 计数）→ 入口（退出码契约）」四块拼成了一张完整的图。

---

## 6. 本讲小结

- **职责**：`scripts/check_links.py` 是一个懂 Docsify 路由的文档链接检查器，主流程在 `check_links()` 里：发现文件 → 提取链接 → 双层忽略过滤 → 本地同步校验 / 远程异步校验 → 统计排序输出。
- **数据结构**：六个 `dataclass` 分三组——输入（`Link`，带 `is_remote`/`should_skip` 等派生属性）、输出（`Issue`，`category` 只有 `local-missing-target`/`local-missing-anchor`/`remote-http` 三种）、远程中间量（`RemoteResponse` 客观响应 vs `RemoteResult` 主观判定）；外加配置用的 `FileTypes` 与 `IgnoreRule`。
- **配置合并**：`DEFAULT_CONFIG` 是代码内置兜底；用户 TOML 经 `deep_merge` **递归合并**（字典遇字典递归、列表整体替换）——`workers` 从默认 8 被覆盖成 16 就是典型例证。
- **入口契约**：`main()` 用 `argparse` 解析 `--config/--root/--no-http/--verbose`，退出码 `0`（通过）/`1`（有断链）/`2`（配置错误），非零即让 CI 失败。
- **两个易错点**：① 不传 `--config` 时完全不读 TOML，忽略规则不生效；② 列表型配置是替换不是追加，写了就要写全。

---

## 7. 下一步学习建议

本讲只搭好了「骨架与入口」。建议按顺序继续：

- **u4-l2 链接提取子系统**：深入 `extract_markdown_links` / `extract_html_links` / `bare_html_links`，看 `markdown-it-py` 的 token 流和 `selectolax` 的 HTML 选择器如何填满 4.2 里的 `Link`。
- **u4-l3 Docsify 路由解析与本地链接校验**：精读 `docsify_candidates` 与 `collect_local_anchors`，搞懂综合实践步骤 4 里 `tried` 候选路径和锚点匹配的来龙去脉。
- **u4-l4 远程链接的异步校验与片段验证**：理解 `RemoteResponse`→`RemoteResult` 的翻译（`evaluate_remote_response`）以及 `asyncio` 并发模型。
- **u4-l5 忽略规则、主流程编排与 CI 集成**：回到 `IgnoreRule.matches` 的合取匹配、双层过滤的完整意义，以及 `.github/workflows/check-links.yml` 如何把本讲的退出码契约接到 CI。

读完这五讲，你将具备为这个检查器**新增配置项、新增忽略字段、定位任意一条断链**的能力。
