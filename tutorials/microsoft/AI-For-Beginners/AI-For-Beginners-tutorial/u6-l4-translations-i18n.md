# 多语言翻译机制

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `translations/` 与 `translated_images/` 两个目录是如何组织的、为什么它们会让仓库变得巨大。
- 描述 `co-op-translator` 这套自动化翻译流水线的工作原理，尤其是它如何用「内容哈希」做到增量翻译、避免重复花钱。
- 用 `git` 的 sparse-checkout（稀疏检出）配合 partial clone（部分克隆）只下载课程本体、跳过全部翻译，显著降低下载量。

本讲是「配套工具与维护机制」单元的第四篇，承接上一讲 [u6-l3 Docsify 文档站点](./u6-l3-docsify-site.md)。上一讲解决的是「把仓库变成在线文档站」，本讲解决的是「这个仓库为什么这么大、以及怎么只拿你真正需要的那一部分」。

## 2. 前置知识

在进入正文前，先用三段话建立直觉。

**什么是 i18n / l10n。** `i18n` 是 internationalization（国际化）的缩写——首字母 i、末字母 n、中间夹 18 个字母；`l10n` 同理指 localization（本地化）。在本课程仓库的语境里，i18n 指「让一份英文课程能被机器翻译成几十种语言并自动保持最新」，l10n 指「真正生成某一种语言（如中文 zh-CN）的具体译文」。

**机器翻译 vs 手工翻译。** 本仓库的翻译几乎全是机器翻译（由微软开源的 `co-op-translator` 工具调用云端翻译服务生成），不是志愿者逐字手翻的。这一点很关键：它决定了翻译可以「自动保持最新」，也决定了译文里会有一张张被重新渲染的图（图里的英文文字也被翻译了）。

**为什么翻译会让仓库爆炸。** 仓库本体（24 课讲义 + Notebook）并不算大，但它为 50 多种语言各存了一份完整镜像，再为每种语言各存了一套翻译后的图。`translations/` 与 `translated_images/` 这两个目录加起来占据了绝大部分下载体积。这正是本讲第三模块「稀疏克隆」要解决的问题。

## 3. 本讲源码地图

本讲涉及的关键文件与目录：

| 文件 / 目录 | 作用 |
| --- | --- |
| `README.md` | 课程总入口；其中「Multi-Language Support」一节列出全部语言入口与稀疏克隆命令。 |
| `etc/TRANSLATIONS.md` | **手工**翻译贡献指南：说明文件命名约定（`README.<语言码>.md`）与测验翻译步骤。 |
| `AGENTS.md` | 仓库维护约定；明确写出「翻译由 co-op-translator 通过 GitHub Actions 自动完成」。 |
| `translations/` | 机器翻译产物根目录；每种语言一个子目录，镜像源仓库结构。 |
| `translations/<locale>/.co-op-translator.json` | 每种语言目录里的「翻译清单」，记录每个源文件的内容哈希与上次翻译时间。 |
| `translated_images/` | 翻译后的图片产物；按语言分子目录存放。 |

> 口诀：**「翻译产物在 translations、翻译后的图在 translated_images、自动化引擎叫 co-op-translator、想跳过它们用 sparse-checkout」**。

## 4. 核心概念与源码讲解

### 4.1 translations 目录结构

#### 4.1.1 概念说明

`translations/` 是机器翻译的「镜像产物区」。它的核心设计思想是：**保持与源仓库完全相同的目录结构，只是整体平移到 `translations/<语言代码>/` 之下**。

举个例子，源文件 `lessons/3-NeuralNetworks/03-Perceptron/README.md` 的中文版就放在 `translations/zh-CN/lessons/3-NeuralNetworks/03-Perceptron/README.md`。路径完全对应，只是开头多了一段 `translations/zh-CN/`。这种「路径镜像」的好处是：任何工具或链接只要在源路径前拼上 `translations/<locale>`，就能定位到对应译文。

语言代码遵循 **BCP 47** 规范（兼容 ISO 639-1 的两位字母代码）：

- `zh-CN`：简体中文（语言 `zh` + 地区 `CN`）
- `pt-BR`：巴西葡萄牙语、`pt-PT`：欧洲葡萄牙语
- `ar`：阿拉伯语、`ja`：日语、`fr`：法语……

> 注意：本课程仓库**主要的**翻译机制是机器翻译（见 4.2）。`etc/TRANSLATIONS.md` 里描述的 `README.<语言码>.md` 命名约定，是为**少量手工翻译贡献者**准备的老约定。两者并不冲突：机器翻译走整目录镜像，手工翻译走同目录下的 `README.<语言码>.md` 文件名后缀。我们以机器翻译的目录镜像为主线讲解。

#### 4.1.2 核心流程

```
源仓库结构                        翻译镜像
─────────────                    ─────────────────────────────────────
README.md              ──机器翻译──> translations/zh-CN/README.md
lessons/.../README.md  ──机器翻译──> translations/zh-CN/lessons/.../README.md
lessons/.../X.png      ──OCR+翻译──> translated_images/zh-CN/X.<hash>.webp
```

翻译流程对外表现为：源文件的每一个路径，在 `translations/<locale>/` 下都有一个对应译本；图片不放在 `translations/` 里，而是集中放到 `translated_images/<locale>/`，译文的 Markdown 再用相对路径指过去。

#### 4.1.3 源码精读

**① 语言入口与稀疏克隆命令（README 多语言区段）。** README 顶部专门有一节多语言支持，先用一张语言表列出全部入口，再给出「想本地克隆、跳过翻译」的命令：

[README.md:24-50](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L24-L50)

这一段里，`<!-- CO-OP TRANSLATOR LANGUAGES TABLE START -->` 与 `... END` 这对注释是**由 co-op-translator 自动维护的标记**：每当新增一种语言，工具会在这两个标记之间重写语言表。本讲第三模块要用的 sparse-checkout 命令也写在这里。

**② 手工翻译的命名约定（etc/TRANSLATIONS.md）。** 这份指南面向愿意手翻的贡献者，规定译文名要带语言后缀，并强调**不要翻译代码、只翻译 README / assignment / 测验文字**：

[etc/TRANSLATIONS.md:7-20](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/TRANSLATIONS.md#L7-L20)

关键一句：「`README._[language]_.md`，其中 `_[language]_` 是 ISO 639-1 的两位字母缩写（如 `README.es.md` 为西班牙语）」。这是手工翻译的文件命名规则。

**③ 实测的 translations 目录布局。** 在本地确认 `translations/` 的实际结构：

```bash
ls translations/ | head        # 看到大量语言目录：ar, bg, zh-CN, ja, ...
ls translations/ | wc -l       # 56 个顶层条目（55 个语言目录 + 1 个游离文件）
```

其中 `translations/zh-CN/` 镜像了源仓库：它下面同样有 `lessons/`、`etc/`、`examples/`、`README.md`、`AGENTS.md`、`troubleshoot.md`。

**④ 译文如何引用翻译后的图。** 打开任意一份译文的图片链接，能看到它指向集中的 `translated_images/<locale>/` 目录（相对路径回退四级到仓库根）：

```bash
grep -n 'translated_images' translations/zh-CN/lessons/1-Intro/README.md | head
# 3:![...](../../../../translated_images/zh-CN/ai-intro.bf28d1ac4235881c.webp)
```

`translated_images/` 目录里实测有两种并存的布局：当前在用的是「语言子目录」布局 `translated_images/<locale>/<图名>.<哈希>.<扩展名>`（如 `translated_images/da/ai-intro.bf28d1ac4235881c.webp`），另有一批历史遗留的「扁平」布局 `<图名>.<哈希>.<locale>.<扩展名>`（如顶层的 `ComputeGraph.463c9d8e....en.png`）。文件名里那段长哈希是图片内容的指纹，内容变了哈希就变，用于缓存失效。

#### 4.1.4 代码实践

**目标：** 直观感受 `translations/` 的「镜像」结构，并体会它对仓库体积的影响。

**步骤：**

1. 列出语言目录：`ls translations/`。
2. 选一个语言，对比它与源目录的对应关系：

   ```bash
   ls lessons/1-Intro/
   ls translations/zh-CN/lessons/1-Intro/
   ```

3. 粗略比较「课程本体」与「翻译」两部分各占多少文件：

   ```bash
   find lessons examples etc -type f | wc -l     # 课程本体文件数
   find translations translated_images -type f | wc -l  # 翻译产物文件数
   ```

**需要观察的现象：** 第 2 步两边文件名几乎一一对应；第 3 步翻译产物的文件数会远超课程本体（因为乘以了 50 多种语言）。

**预期结果：** 翻译产物文件数通常是课程本体的几十倍——这正是仓库「大」的根源，也是下一模块要解决的问题。

> 注：第 3 步的具体数字取决于本地是否为完整克隆。若你已按 4.3 的 sparse-checkout 克隆，`translations/` 与 `translated_images/` 不会被下载，此时这两条命令应基本无输出。**待本地验证。**

#### 4.1.5 小练习与答案

**练习 1：** `translations/` 顶层的 `README.ja.md` 是一个游离文件，而 `ja/` 又是一个目录。两者可能是什么关系？

**参考答案：** 它很可能是早期手工翻译遗留的产物——当年按 `etc/TRANSLATIONS.md` 的 `README.<语言码>.md` 约定把日文 README 直接放在了 `translations/` 根下；后来改用整目录镜像后，译文进了 `translations/ja/`，这个老文件就留了下来。它属于需要清理的历史包袱。

**练习 2：** 为什么译文里的图片链接要回退四级（`../../../../`）？

**参考答案：** 译文位于 `translations/zh-CN/lessons/1-Intro/README.md`，相对仓库根要往上走 `translations/`→`zh-CN/`→`lessons/`→`1-Intro/` 共四级，才能回到仓库根下的 `translated_images/`。

---

### 4.2 co-op-translator 自动化

#### 4.2.1 概念说明

`co-op-translator` 是微软开源的一个**专为 GitHub 文档仓库设计**的自动化翻译工具（仓库地址：`Azure/co-op-translator`）。它的工作可以概括为一句话：**扫描源 Markdown → 调用云端翻译 → 把译文写到 `translations/<locale>/` → 同时翻译图片并改写图片链接 → 以机器人账号提交同步 PR**。

本仓库里实际执行同步的是一个名为 `localizeflow[bot]` 的 GitHub Action 机器人。你可以从 git 历史里看到它周期性提交的「同步」提交。设计这套自动化的根本动机是：**50 多种语言靠人手维护是不现实的，必须让机器在源文件一变就自动重译并保持最新**。

它最聪明的一点是**增量翻译**：不为没变化的文件重复花钱调用翻译 API。实现手段就是 4.1 里提到的「翻译清单」`.co-op-translator.json`。

#### 4.2.2 核心流程

对每一种语言 `<locale>`，流水线对每个源文件 \(f\) 做如下判断（\(H\) 为 MD5 内容哈希）：

\[
\text{需要重译}(f) \iff H(f_{\text{源}}) \neq H_{\text{清单记录}}(f)
\]

即：**只有当源文件当前的内容哈希与清单里记录的旧哈希不一致时，才重新翻译**。展开成步骤：

1. 扫描源仓库的所有 Markdown / Notebook。
2. 对每个源文件计算 MD5 哈希 \(H(f_{\text{源}})\)。
3. 读取 `translations/<locale>/.co-op-translator.json` 中该文件记录的 `original_hash`。
4. 二者相等 → 跳过；不等 → 调用翻译服务生成译文，写回 `translations/<locale>/<原路径>`，并把图片送去做 OCR + 翻译，输出到 `translated_images/<locale>/`。
5. 改写译文里的图片链接，指向新生成的翻译图片。
6. 更新清单：写入新的 `original_hash` 与 `translation_date`（ISO 8601 时间戳）。
7. 由 `localizeflow[bot]` 把所有改动聚合成一个「chunk N/M」PR，合入主干。

这个「哈希比对」的增量机制，和软件构建里的 `make`（按文件修改时间/内容决定是否重新编译）是同一个思想：**用廉价的本地指纹比对，避免昂贵的重复计算**。

#### 4.2.3 源码精读

**① 维护约定：翻译由 co-op-translator 自动完成。** `AGENTS.md` 在「Translation Contributions」一节直接点明机制：

[AGENTS.md:198-203](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/AGENTS.md#L198-L203)

其中关键两句：「Translations are automated via GitHub Actions using co-op-translator」（翻译由 GitHub Actions + co-op-translator 自动完成），以及「Manual translations go in `translations/<language-code>/`」。这说明 `translations/` 目录既是机器翻译的产物区，也可放手工订正。

**② 支持语言清单来源。** README 末尾指向 co-op-translator 的支持语言表，说明能扩展哪些语言：

[README.md:52-52](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L52-L52)

**③ 翻译清单的真实结构。** 每个语言目录（如 `translations/da/`）下都有一份 `.co-op-translator.json`，逐文件记录哈希与时间。以丹麦语 `da` 为例，其中关于 `README.md` 的一条记录长这样（节选）：

```json
"README.md": {
  "original_hash": "12c8eb6bf0867d2f1c32daf613ac5b8b",
  "translation_date": "2026-04-06T16:25:23+00:00",
  "source_file": "README.md",
  "language_code": "da"
}
```

完整文件见 [translations/da/.co-op-translator.json](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/translations/da/.co-op-translator.json)。

字段含义：

- `original_hash`：**源文件**（不是译文）被翻译那一刻的 MD5。下次同步时，工具重新算源文件当前的 MD5，跟它比。
- `translation_date`：这次翻译发生的时间（ISO 8601，带时区）。
- `source_file` / `language_code`：指向源文件与本语言，便于工具定位。

> 一个细节：实测 `translations/en/` 目录**没有**这份清单——因为 `en` 是源语言本身，不需要「被翻译」，它只是为结构对称而存在的镜像。`translations/` 下其余 54 个语言目录都各带一份清单。

**④ 从 git 历史看同步提交。** `localizeflow[bot]` 的同步提交有固定标题格式：

```bash
git log --oneline -8 --grep="i18n"
# fa78bc6f Merge pull request #631 from microsoft/update-translations
# abde08c2 chore(i18n): sync translations with latest source changes (chunk 1/1, 6 changes)
# ...
```

挑一条看它到底改了什么：

```bash
git show --stat 47a7bd5b
# commit 47a7bd5b ... Author: localizeflow[bot] <...>
#     chore(i18n): sync translations with latest source changes (chunk 1/1, 6 changes)
#  translations/da/.co-op-translator.json |  4 +-
#  translations/da/etc/Mindmap.md         | 70 +++++-----
#  translations/sv/.co-op-translator.json |  4 +-
#  translations/sv/etc/Mindmap.md         | 38 ++--
#  translations/th/.co-op-translator.json |  4 +-
#  translations/th/etc/Mindmap.md         | 86 ++++++----
```

这次同步只重译了 `da/sv/th` 三种语言的 `etc/Mindmap.md`（因为只有这个源文件变了），并顺带把这三个清单里对应条目的哈希与日期更新了。注意 `+4/-4` 恰好是「改一个哈希 + 改一个日期」——这就是增量机制的直接证据：**没变的文件根本没出现在这次提交里**。

#### 4.2.4 代码实践

**目标：** 通过阅读「翻译清单」与 git 历史，亲手验证增量翻译机制。

**步骤：**

1. 选一个语言目录，统计它记录了多少个文件、哪个文件最近被重译：

   ```bash
   # 看清单里记录的文件数（每个文件一个条目）
   grep -c '"source_file"' translations/da/.co-op-translator.json
   # 找出最近一次翻译时间最新的那个文件
   grep '"translation_date"' translations/da/.co-op-translator.json | sort | tail
   ```

2. 数一下最近几次同步提交各自改了哪些语言、哪些文件：

   ```bash
   git log --oneline -5 --grep="sync translations"
   git show --stat <某条 sync 提交的 hash>
   ```

**需要观察的现象：**

- 第 1 步的「最近翻译时间」应该集中在几个相近的日期——说明同步是分批触发的。
- 第 2 步每次 sync 提交只动了少数几个文件（如上例只有 3 个语言的 `Mindmap.md`），而不是全部 54 种语言的所有文件。

**预期结果：** 你会清楚看到「只有变化的源文件才会触发重译」——这正是 `original_hash` 比对的目的。如果某次同步改了上千个文件，那通常意味着源仓库刚做了一次大改版（如重构目录），导致哈希大面积变化。

> 注：清单里的具体哈希值与日期会随仓库更新而变化，以上命令的输出以你本地仓库实际状态为准。**待本地验证。**

#### 4.2.5 小练习与答案

**练习 1：** 如果有人手工修改了 `translations/zh-CN/lessons/1-Intro/README.md` 里的一段译文，下一次 co-op-translator 同步会发生什么？

**参考答案：** 工具判断的是**源文件**的哈希有没有变，而不是译文。只要源 `lessons/1-Intro/README.md` 没改，工具就会跳过这个文件，**不会**覆盖你的手工修改。只有当源文件发生变化、哈希不一致时，工具才会重新翻译并覆盖译文（你的手工改动会丢失）。因此手工订正最好同时去改源文件，或接受「下次源文件更新时会被重译」的风险。

**练习 2：** 为什么用 MD5 这种哈希，而不是用文件的「修改时间」来判断是否需要重译？

**参考答案：** 修改时间不可靠：`git checkout`、换机器、rebase 都会改文件 mtime，但内容没变，会导致无谓重译。内容哈希只与内容有关，与时间戳、换行符归一化后的细微差异无关（工具会先归一化），判断更稳定。这也是 `make`、`docker layer cache`、前端构建缓存普遍采用内容哈希的原因。

**练习 3：** `localizeflow[bot]` 提交标题里的 `chunk 1/1` 是什么意思？

**参考答案：** 表示这一批翻译变更被分成了 1 个 chunk（分片）来提交。当变更量大时，工具会把改动拆成多个 chunk（如 `chunk 1/3`、`chunk 2/3`……）逐个提交 PR，避免单个 PR 过大、CI 跑不动或触发 GitHub 限制。`1/1` 表示本次变更量小，一个分片就够了。

---

### 4.3 稀疏克隆技巧

#### 4.3.1 概念说明

讲到这里问题就很清楚了：你想本地跑课程的 Notebook，根本不需要 50 多种语言的翻译，但默认 `git clone` 会把整个仓库（含全部翻译与翻译图）拉下来，既慢又占空间。

Git 提供了两个正交的「按需下载」机制，组合起来就能完美解决：

- **Partial clone（部分克隆）`--filter=blob:none`**：克隆时只下载提交（commit）和目录树（tree）对象，**不下载文件内容（blob）**。文件内容在你真正访问（checkout / 读取）到它时才按需拉取。这把「下载时机」从「克隆时」推迟到了「用时」。
- **Sparse checkout（稀疏检出）`git sparse-checkout`**：即使在本地，也只把**指定路径**的文件真正展开到工作区，其余路径对工作区「不可见」。

二者配合：partial clone 决定「什么时候下载文件内容」，sparse-checkout 决定「哪些路径的内容需要被下载到工作区」。结果就是你只为你看得见的路径付费下载。

#### 4.3.2 核心流程

README 给出的标准三步命令：

```bash
git clone --filter=blob:none --sparse https://github.com/microsoft/AI-For-Beginners.git
cd AI-For-Beginners
git sparse-checkout set --no-cone '/*' '!translations' '!translated_images'
```

逐步拆解：

1. `git clone --filter=blob:none --sparse <url>`
   - `--filter=blob:none`：开启 partial clone，不预下载任何文件内容。
   - `--sparse`：克隆后立即进入 sparse-checkout 模式（默认只检出仓库根目录的文件）。
2. `cd AI-For-Beginners`：进入仓库。
3. `git sparse-checkout set --no-cone '/*' '!translations' '!translated_images'`
   - `set`：设定检出规则（覆盖旧规则）。
   - `--no-cone`：使用**全模式（non-cone）**匹配，支持完整的 gitignore 风格通配符。
   - `'/*'`：包含根目录下的一切。
   - `'!translations'` / `'!translated_images'`：`!` 表示**排除**这两个目录。

合起来就是「**全要，但排除翻译相关两个目录**」。最终你拿到 `lessons/`、`examples/`、`etc/`、`README.md` 等全部课程内容，却没有 `translations/` 和 `translated_images/`。

> 小贴士：`--no-cone` 模式更直观但略慢；如果只想「只要某几个目录」，也可以用默认的 cone（锥）模式，例如 `git sparse-checkout set lessons examples etc`，效果相反——只列你要的。本课程用「排除法」是因为要保留的东西多、要排除的少。

#### 4.3.3 源码精读

**稀疏克隆命令的官方出处**，就在 README 的多语言支持区段（前面 4.1 引用过的同一节），同时给了 Bash 与 Windows CMD 两种写法：

[README.md:31-49](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/README.md#L31-L49)

这段命令是仓库维护者**推荐**的本地克隆方式，不是某个第三方的技巧——可以放心使用。

命令块上方还有一句说明：「This repository includes 50+ language translations which significantly increases the download size.」（本仓库含 50+ 语言翻译，显著增大了下载体积），直接点明了为什么要这么做。

#### 4.3.4 代码实践

**目标：** 亲手对比「全量克隆」与「稀疏克隆」的下载体积与结果差异，体会 sparse-checkout 的价值。

**步骤：**

1. **稀疏克隆（推荐做法）：**

   ```bash
   git clone --filter=blob:none --sparse https://github.com/microsoft/AI-For-Beginners.git ai4beg-sparse
   cd ai4beg-sparse
   git sparse-checkout set --no-cone '/*' '!translations' '!translated_images'
   ```

2. 看看工作区里有没有翻译目录（应该没有）：

   ```bash
   ls                    # 能看到 lessons/ examples/ etc/ 等
   ls translations       # 应报错：没有这个文件或目录
   ```

3. 估算下载体积：

   ```bash
   du -sh .git           # 稀疏克隆的 .git 大小
   du -sh .              # 工作区总大小
   ```

4. （可选，对照）**全量克隆**到另一个目录：

   ```bash
   git clone https://github.com/microsoft/AI-For-Beginners.git ai4beg-full
   du -sh ai4beg-full/.git
   du -sh ai4beg-full
   ```

**需要观察的现象：**

- 第 2 步：稀疏克隆下 `translations/` 不存在，但 `lessons/` 等课程目录完整存在。
- 第 3、4 步对比：稀疏克隆的总体积应**远小于**全量克隆（差距通常在一个数量级以上），因为省掉了 50+ 语言镜像与数千张翻译图。

**预期结果：** 你得到了一个「麻雀虽小、五脏俱全」的工作副本——能跑全部 Notebook、能看全部讲义，但下载量大幅下降。

**适用场景说明：**

| 场景 | 推荐方式 |
| --- | --- |
| 学习者本地跑课程 Notebook | ✅ 稀疏克隆（排除翻译） |
| 离线/弱网环境备课 | ✅ 稀疏克隆 |
| 翻译贡献者（要改 `translations/`） | ❌ 不能用稀疏克隆排除翻译——改用全量克隆，或把翻译目录加回检出 |
| 只想读某一课的中文版 | 用 GitHub 网页直接看 `translations/zh-CN/...`，无需克隆 |

> 注：实际体积数字取决于网络与远端仓库当时的压缩状态，本实践只比较相对大小。**待本地验证。**

#### 4.3.5 小练习与答案

**练习 1：** 如果你一开始稀疏克隆排除了 `translations`，后来又想做翻译贡献，如何把它「加回来」而不重新克隆？

**参考答案：** 直接修改 sparse-checkout 规则即可。例如切到 cone 模式后显式包含它：

```bash
git sparse-checkout disable                      # 退出 sparse 模式，检出全部
# 或更精细：
git sparse-checkout set --no-cone '/*'           # 不再排除 translations，全要
```

由于已经用了 partial clone，被加回的目录内容会按需从远端拉取。`sparse-checkout` 的规则随时可改，无需重新克隆。

**练习 2：** `--filter=blob:none` 和 `git sparse-checkout` 能否只用其中一个？各自有什么不足？

**参考答案：** 可以只用其一，但都不够理想。只 partial clone、不 sparse：克隆快了，但一旦你 `ls translations/` 或编辑器索引整个仓库，那些 blob 仍会被逐个按需下载，体积还是回来了。只 sparse、不 partial clone：工作区看不到翻译，但 `.git` 里其实仍存有全部翻译对象，磁盘占用没省。**组合使用**才能既省「工作区」又省「`.git` 仓库」。

**练习 3：** 命令里 `--no-cone` 去掉会怎样？

**参考答案：** 默认是 cone（锥）模式，它只认「目录名」、不支持 `!` 取反这种 gitignore 通配。若去掉 `--no-cone` 直接传 `'/*' '!translations'`，cone 模式会无法正确理解这些规则，导致检出结果不符预期（可能报错或检出过多/过少）。`--no-cone` 就是用来启用完整的取反语法，让「全要但排除某目录」这种表达成立。

---

## 5. 综合实践

**任务：模拟一次「翻译同步」并理解增量机制。**

把本讲三个模块串起来，完成下面这个小调查，并写一份简短报告：

1. **结构**（模块一）：用 `ls translations/` 列出全部语言，挑三种（如 `zh-CN`、`da`、`ar`），分别打开它们的 `README.md`，对比第一段开头，直观感受「同一段英文被译成三种语言」。
2. **机制**（模块二）：用 `git log --oneline --grep="sync translations" | head` 找最近一次同步提交，用 `git show --stat` 看它改了哪些文件。回答：这次同步重译了哪几种语言的哪些文件？为什么只动了这几个？（提示：对照「源文件是否变化」）
3. **体积**（模块三）：按 4.3.4 做一次稀疏克隆，记录 `du -sh` 的结果；再任选一种语言，用 `find translations/<locale> translated_images/<locale> -type f | wc -l` 估算「仅这一种语言的翻译产物有多少文件」。据此写一句结论：为什么课程官方要推荐 sparse-checkout。

**交付：** 一份 200 字以内的报告，包含三组数字与一句结论。结论应能回答「仓库为什么大、翻译怎么保持最新、我该怎么下载」这三个问题。

> 这个综合实践不需要你修改任何源码，全部是只读观察 + 一次克隆操作，安全可重复。

## 6. 本讲小结

- `translations/` 是机器翻译的**镜像产物区**，每种语言一个子目录、路径与源仓库一一对应；翻译后的图集中放在 `translated_images/<locale>/`，由译文用相对路径引用。
- 翻译由开源工具 **`co-op-translator`** 经 **`localizeflow[bot]`** 这个 GitHub Action 自动完成，提交标题固定为 `chore(i18n): sync translations ... (chunk N/M)`；`AGENTS.md` 明确声明了这一机制。
- 增量翻译靠每语言目录下的 `.co-op-translator.json` **清单**实现：它记录每个源文件的 MD5 `original_hash` 与 `translation_date`，**只有源哈希变化才重译**，避免重复花钱——这是 `make` 式「内容指纹驱动」思想的体现。
- `translations/` 与 `translated_images/` 是仓库体积的主因；官方推荐用 **partial clone（`--filter=blob:none`）+ sparse-checkout（排除两目录）** 只下载课程本体，显著降低下载量。
- 两种翻译约定并存：机器翻译走整目录镜像（主线）；手工贡献走 `etc/TRANSLATIONS.md` 的 `README.<语言码>.md` 文件名后缀（辅线，且不翻译代码）。
- sparse-checkout 规则可随时增删（无需重新克隆），翻译贡献者可按需把 `translations/` 加回检出。

## 7. 下一步学习建议

本讲是「配套工具与维护机制」单元的第四篇，建议继续：

- **下一篇 u6-l5（CI 安全与贡献流程）**：把视线从「翻译」转向「安全与协作」——精读 `.github/workflows/` 里的 CodeQL / Scorecard 安全工作流与 `etc/CONTRIBUTING.md`、`AGENTS.md` 的贡献规范，学会如何为这个教育仓库合规地提 PR（例如改了一处英文讲义后，要不要等 co-op-translator 自动同步翻译）。
- **横向回顾 u6-l1 / u6-l2**：测验子系统有一套**独立**的多语言机制（`etc/quiz-app/src/assets/translations/<locale>/` + `vue-i18n` 的全局 locale 状态），与本讲的 markdown 翻译是两条平行管线，对照阅读能加深对「一个仓库内多种 i18n 策略并存」的理解。
- **进阶阅读**：若想深入工具本身，可阅读 `co-op-translator` 上游仓库（README 里给了链接）的「getting_started」，了解它如何调用翻译服务、如何处理图片 OCR，以及如何在自己的文档仓库里接入同一套流水线。
