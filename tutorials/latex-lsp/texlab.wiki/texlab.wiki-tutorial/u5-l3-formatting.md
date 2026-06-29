# 代码格式化：BibTeX 与 LaTeX

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 texlab 把「格式化」按语言拆成两条独立链路（`.bib` 与 `.tex`），并分别由 `texlab.bibtexFormatter` 和 `texlab.latexFormatter` 选择格式化器。
- 读懂每个格式化器取值（`texlab` / `latexindent` / `tex-fmt` / `none`）的含义，并知道 `latexFormatter` 取 `texlab` **尚未实现**这一重要陷阱。
- 把 `texlab.latexindent.*` 三个子项（`local` / `modifyLineBreaks` / `replacement`）与 `latexindent` 的命令行 flag 一一对应起来。
- 学会用 `texlab.formatterLineLength` 控制 BibTeX 格式化的最大行宽（`0` 表示禁用）。
- 在编辑器里触发一次格式化，并对照观察格式化前后的差异。

## 2. 前置知识

本讲是 [u2-l1 配置总览](u2-l1-config-overview.md) 的直接延续，会反复用到那里建立的三套语言，先快速回顾：

- **配置归客户端**：texlab 不读任何配置文件，配置由 LSP 客户端（编辑器）持有，texlab 作为服务器按需查询，缺省值兜底。
- **三要素**：每一项配置都可以用 **Type（类型）/ Default value（默认值）/ Placeholders（占位符）** 读懂。本讲的格式化项**都不使用占位符**（`%f`/`%p`/`%l` 只出现在 `build.args` 与 `forwardSearch.args`，与格式化无关）。
- **`texlab.*` 命名空间**：键按 `texlab.<分类>.<键>` 分层组织。

另外需要一点 LSP 背景：格式化在 LSP 里是标准方法 `textDocument/formatting`（整篇格式化）和 `textDocument/rangeFormatting`（选区格式化）。你在编辑器里按下的「格式化文档」快捷键，最终就是向 texlab 发送 `textDocument/formatting` 请求；texlab 计算出一组 `TextEdit`（文本编辑动作）返回，编辑器再把这些动作套用到缓冲区。**本讲的所有配置项，本质上都是在回答 texlab 收到这个请求后「该调用谁、怎么调用」**。

> 提示：格式化只改变文本的排版样子，**不会影响编译结果**。它和编译（`texlab.build.*`）、诊断（`chktex`）是完全独立的子系统。

## 3. 本讲源码地图

本仓库是纯文档 wiki，本讲的「源码」就是 `Configuration.md` 中关于格式化的 6 个配置小节，全部集中在文件中部：

| 文件 | 行范围 | 作用 |
|---|---|---|
| `Configuration.md` | L289–L296 | `texlab.formatterLineLength`：BibTeX 格式化的最大行宽 |
| `Configuration.md` | L299–L307 | `texlab.bibtexFormatter`：BibTeX 格式化器选择 |
| `Configuration.md` | L310–L319 | `texlab.latexFormatter`：LaTeX 格式化器选择（含未实现提醒） |
| `Configuration.md` | L322–L331 | `texlab.latexindent.local`：`latexindent` 配置文件路径 |
| `Configuration.md` | L334–L343 | `texlab.latexindent.modifyLineBreaks`：是否调整换行 |
| `Configuration.md` | L346–L360 | `texlab.latexindent.replacement`：额外的替换 flag |

## 4. 核心概念与源码讲解

先给一张总览，建立全局认知：

| 配置项 | 取值 | 默认值 | 作用对象 |
|---|---|---|---|
| `texlab.bibtexFormatter` | `texlab` / `latexindent` / `none` | `texlab` | `.bib` 文件 |
| `texlab.latexFormatter` | `texlab` / `latexindent` / `tex-fmt` / `none`（`texlab` 未实现） | `latexindent` | `.tex` 文件 |

可以看出，texlab 按**文件类型分流**：`.bib` 走 `bibtexFormatter`，`.tex` 走 `latexFormatter`，两条链路彼此独立。`texlab.latexindent.*` 是一个**共享子命名空间**——只要某一侧用了 `latexindent`，这三个选项就对它生效。

### 4.1 BibTeX 格式化器：bibtexFormatter 与 formatterLineLength

#### 4.1.1 概念说明

BibTeX（`.bib`）文件存放参考文献条目，例如：

```bibtex
@article{knuth1984,
  title={Literate Programming},
author={Donald E. Knuth},
journal={The Computer Journal},volume={27},pages={97--111},year={1984}
}
```

这种「字段挤在一行、缩进混乱」的写法完全合法，但极难阅读。BibTeX 格式化的任务就是把字段对齐、换行整理成统一风格。

texlab 提供一个**内置的 BibTeX 格式化器**（取值 `texlab`），并允许你改用外部的 `latexindent`，或用 `none` 关闭格式化。注意默认值就是 `texlab`——也就是说**开箱即用，texlab 自带的 BibTeX 格式化是默认启用的**，无需安装任何外部工具。

#### 4.1.2 核心流程

收到针对 `.bib` 文件的 `textDocument/formatting` 请求时，texlab 的分流逻辑（示例代码，用于说明语义）：

```text
function formatBibtex(uri, text):
    switch config.bibtexFormatter:          # texlab | latexindent | none
        case "none":        return []                                    # 不格式化，返回空编辑
        case "texlab":      return builtInBibtexFormat(text,             # 内置格式化器
                                    lineLength = config.formatterLineLength)
        case "latexindent": return runLatexindent(uri, text)             # 走外部 latexindent
```

关键点：

- 取 `none` 时 texlab 直接返回空编辑列表，等于「该语言不做格式化」——这让你可以**只关掉某一侧**（例如关掉 BibTeX、保留 LaTeX）。
- 取 `texlab` 时使用内置格式化器，其最大行宽由 `texlab.formatterLineLength` 决定。
- 取 `latexindent` 时交给外部 `latexindent` 处理，行宽等细节改由 `latexindent` 自己的 YAML 配置控制。

#### 4.1.3 源码精读

先看格式化器选择项 [Configuration.md:L299-L307](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L299-L307)，这里说明 BibTeX 格式化器只能是 `texlab`、`latexindent` 或 `none` 三者之一，默认 `texlab`：

> Defines the formatter to use for BibTeX formatting. Possible values are either `texlab`, `latexindent` or `none`. … **Default value:** `texlab`

再看与之配套的行宽项 [Configuration.md:L289-L296](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L289-L296)，它定义格式化 BibTeX 时每行的最大字符数，`0` 表示禁用（不换行），默认 `80`：

> Defines the maximum amount of characters per line (0 = disable) when formatting BibTeX files. … **Default value:** `80`

需要注意两处细节：

1. **命名是扁平的**：这个键叫 `texlab.formatterLineLength`，**不是** `texlab.bibtexFormatter.lineLength`。它和 `bibtexFormatter` 平级，从名字看不出只服务于 BibTeX，但语义上确实**只作用于 BibTeX 格式化**——LaTeX 的行宽规则不在 texlab 这里管，而在 `latexindent` 的 YAML（如 `maximumColumns`）里。
2. **主要消费者是内置 `texlab` 格式化器**：wiki 措辞为「when formatting BibTeX files」。若你把 `bibtexFormatter` 改成 `latexindent`，行宽就改由 `latexindent` 自己的配置决定；`formatterLineLength` 是否仍透传给 `latexindent` 路径，wiki 未明确，建议以本地验证为准。

#### 4.1.4 代码实践

**实践目标**：体验内置 BibTeX 格化器，并观察 `formatterLineLength` 对换行的影响。

**操作步骤**：

1. 新建 `refs.bib`，故意把字段挤成一行（如上文示例那样混乱）。
2. 在支持 texlab 的编辑器里打开它，执行「格式化文档」命令（对应 `textDocument/formatting`）。
3. 把配置改为 `"texlab.formatterLineLength": 30`，再次格式化。
4. 把配置改为 `"texlab.formatterLineLength": 0`，再次格式化。

**需要观察的现象**：

- 默认（`80`）下，长字段会被折行、字段对齐。
- 设为 `30` 后，行宽变窄，折行更频繁。
- 设为 `0` 后，不再主动换行。

**预期结果**：行宽越小折行越密；`0` 禁用换行。具体排版细节**待本地验证**（内置格式化器的精确对齐风格以实际运行为准）。

#### 4.1.5 小练习与答案

**练习 1**：你只想格式化 LaTeX、不想让 texlab 动你的 `.bib`，该怎么配？
**答案**：把 `texlab.bibtexFormatter` 设为 `"none"`。这样 `.bib` 走到 `none` 分支返回空编辑，而 `texlab.latexFormatter` 仍可保留默认的 `latexindent`。

**练习 2**：`texlab.formatterLineLength` 设成 `0` 是什么效果？为什么它放在 `texlab.` 顶层而不是 `texlab.bibtexFormatter.` 下？
**答案**：`0` 表示禁用换行。它放在顶层是历史命名（扁平结构），但语义上只作用于 BibTeX 格式化；LaTeX 行宽不受它控制。

---

### 4.2 LaTeX 格式化器：latexFormatter

#### 4.2.1 概念说明

LaTeX（`.tex`）文件的格式化比 BibTeX 复杂得多——涉及缩进层级、环境（`\begin{…}`/`\end{…}`）对齐、数学公式换行等。texlab **没有**为 LaTeX 提供一个成熟的内置格式化器，而是默认调用外部的 `latexindent`。

`texlab.latexFormatter` 有四个取值，但其中一个是**陷阱**：

| 取值 | 含义 | 状态 |
|---|---|---|
| `latexindent` | 调用外部 `latexindent` 工具 | ✅ 默认值，可用 |
| `tex-fmt` | 调用外部 `tex-fmt` 工具（第三方 Rust 格式化器） | ✅ 可用 |
| `none` | 不格式化 | ✅ 可用 |
| `texlab` | （预留的）texlab 自带格式化器 | ⚠️ **尚未实现** |

最关键的一句提醒在 wiki 里：**`texlab` 取值目前并未实现**。也就是说，如果你把 `latexFormatter` 显式设成 `"texlab"`，期待得到「texlab 原生 LaTeX 格式化」，实际效果是什么都不发生（等价于没有格式化）。这是排查「LaTeX 格式化没反应」时首先要排除的误配。

#### 4.2.2 核心流程

收到针对 `.tex` 文件的 `textDocument/formatting` 请求时（示例代码）：

```text
function formatLatex(uri, text):
    switch config.latexFormatter:           # texlab | latexindent | tex-fmt | none
        case "none":        return []                                    # 不格式化
        case "texlab":      return []    # 尚未实现，当前等价于无操作
        case "latexindent": return runLatexindent(uri, text)             # 默认路径
        case "tex-fmt":     return runTexFmt(uri, text)                  # 第三方工具
```

注意 `latexindent` 和 `tex-fmt` 都是**外部命令**——texlab 不会内置它们，你需要自行安装（`latexindent` 通常随 TeX Live / MiKTeX 发行；`tex-fmt` 需单独安装）。工具不在 `PATH` 中时，格式化会失败。

#### 4.2.3 源码精读

看选择项本身 [Configuration.md:L310-L319](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L310-L319)，四取值与「未实现」提醒都在这里，默认 `latexindent`：

> Defines the formatter to use for LaTeX formatting. Possible values are either `texlab`, `latexindent`, `tex-fmt` or `none`. **Note that `texlab` is not implemented yet.** … **Default value:** `latexindent`

读这一节时要抓三个事实：

1. LaTeX 的默认格式化器是 `latexindent`（外部工具），不是 texlab 自己。
2. `texlab` 这个取值**写在枚举里但没实现**——这是容易踩的坑。
3. `tex-fmt` 是除 `latexindent` 外的另一个外部选择。

#### 4.2.4 代码实践

**实践目标**：验证默认 LaTeX 格式化可用，并复现「`texlab` 取值未实现」的现象。

**操作步骤**：

1. 准备一份缩进混乱的 `main.tex`（例如 `itemize` 列表项没有缩进对齐）。
2. 确认系统已安装 `latexindent`（终端运行 `latexindent --version`）。
3. 在编辑器中执行「格式化文档」，观察缩进是否被整理。
4. 把配置改为 `"texlab.latexFormatter": "texlab"`，再次格式化。

**需要观察的现象**：

- 第 3 步：默认 `latexindent` 生效，环境层级缩进被修正。
- 第 4 步：设为 `texlab` 后，**没有任何变化**——印证该取值尚未实现。

**预期结果**：默认路径工作正常；`"texlab"` 取值无效果。若第 3 步也无反应，先排查 `latexindent` 是否在 `PATH`。运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：某用户反馈「我把 `latexFormatter` 设成了 `texlab`，结果 LaTeX 完全不格式化，是不是 bug？」你如何回答？
**答案**：不是 bug。wiki 明确 `texlab` 取值「not implemented yet」，当前等价于无操作。应改回默认的 `latexindent` 或用 `tex-fmt`。

**练习 2**：`bibtexFormatter` 和 `latexFormatter` 的取值集合有何不同？
**答案**：`bibtexFormatter` 只有 `texlab`/`latexindent`/`none`；`latexFormatter` 多一个 `tex-fmt`。且 `bibtexFormatter` 的 `texlab` **是已实现的内置格式化器**（也是默认值），而 `latexFormatter` 的 `texlab` **尚未实现**——同名取值在两边的可用性不同，这是最容易混淆的点。

---

### 4.3 latexindent 选项：local / modifyLineBreaks / replacement

#### 4.3.1 概念说明

当某一侧（BibTeX 或 LaTeX）选用 `latexindent` 作为格式化器时，texlab 需要知道**怎么调用它**——传哪些命令行参数。`texlab.latexindent.*` 这个子命名空间就是为此而设，它把三个配置项**一一映射**到 `latexindent` 的三个命令行 flag：

| texlab 配置项 | 类型 / 默认 | 触发条件 | 对应 latexindent flag |
|---|---|---|---|
| `texlab.latexindent.local` | `string`，默认 `null` | 非 `null` 时 | `--local=<值>` |
| `texlab.latexindent.modifyLineBreaks` | `boolean`，默认 `false` | 为 `true` 时 | `--modifylinebreaks` |
| `texlab.latexindent.replacement` | `"-r"`/`"-rv"`/`"-rr"`/`null`，默认 `null` | 非 `null` 时 | 直接追加该 flag |

这是一种典型的「配置项 → 命令行 flag」翻译模式，和 [u2-l2](u2-l2-build-config.md) 里 `build.args` 翻译成编译命令是同一思路。区别在于：`build.args` 是**你自己写死**整个参数数组；而 `latexindent.*` 是 texlab **替你组装**几个常用 flag，你只需填布尔/枚举值。

#### 4.3.2 核心流程

texlab 调用 `latexindent` 前的 flag 组装逻辑（示例代码）：

```text
function collectLatexindentFlags():
    flags = []
    if config.latexindent.local is not null:
        flags.append("--local=" + config.latexindent.local)    # 指定 YAML 配置文件
    if config.latexindent.modifyLineBreaks == true:
        flags.append("--modifylinebreaks")                     # 允许调整换行
    if config.latexindent.replacement is not null:             # "-r" | "-rv" | "-rr"
        flags.append(config.latexindent.replacement)
    return flags

# 最终形如：latexindent <flags> <文件>
```

三个 flag 各自做什么（以 `latexindent` 官方语义为准，详见 latexindent 文档）：

- `--local=<file.yaml>`：指定一份本地的 `latexindent` 配置 YAML，覆盖默认规则。
- `--modifylinebreaks`：允许 `latexindent` 在代码块前、中、后**主动修改换行**（例如把过长的行折断、把挤在一起的内容拆行）。默认不修改换行，只做缩进。
- `-r` / `-rv` / `-rr`：`latexindent` 的「替换（replacement）」模式家族，用于按 YAML 中的 `replacements` 规则做文本替换；三者是替换模式的不同变体，精确差异以 `latexindent` 文档为准。

> 注意：这三个 flag **只在你实际用 `latexindent` 时才有意义**。若两侧都设为 `tex-fmt` 或 `none`，这些配置项不会被任何路径消费。

#### 4.3.3 源码精读

`local` 项见 [Configuration.md:L322-L331](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L322-L331)：它定义 `latexindent` 配置文件路径，对应 `--local=file.yaml`，默认 `null`（此时使用项目根目录下的配置）：

> Defines the path of a file containing the `latexindent` configuration. This corresponds to the `--local=file.yaml` flag of `latexindent`. By default the configuration inside the project root directory is used.

`modifyLineBreaks` 项见 [Configuration.md:L334-L343](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L334-L343)：在用 `latexindent` 格式化时，于代码块前、中、后修改换行，对应 `--modifylinebreaks`，默认 `false`：

> Modifies linebreaks before, during, and at the end of code blocks when formatting with `latexindent`. This corresponds to the `--modifylinebreaks` flag of `latexindent`. … **Default value:** `false`

`replacement` 项见 [Configuration.md:L346-L360](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L346-L360)：定义追加给 `latexindent` 的替换 flag，取值 `"-r"` / `"-rv"` / `"-rr"` / `null`，默认 `null`（不追加）：

> Defines an additional replacement flag that is added when calling `latexindent`. … By default no replacement flag is passed. … **Default value:** `null`

读这三节时要建立的对应关系就是上表那三行——**配置项到 flag 是一一映射**，记住这张表即可。

#### 4.3.4 代码实践

**实践目标**：用一个自定义 YAML 让 `latexindent` 按你的规则格式化，并开启换行调整。

**操作步骤**：

1. 在项目根目录新建 `latexindent.yaml`，写入最小规则（示例代码，具体语法见 latexindent 文档）：
   ```yaml
   defaultIndent: "  "      # 两空格缩进
   modifyLineBreaks:
     textWrapOptions:
       columns: 60          # 60 列换行
   ```
2. 在 texlab 配置中设置：
   ```json
   {
     "texlab.latexFormatter": "latexindent",
     "texlab.latexindent.local": "latexindent.yaml",
     "texlab.latexindent.modifyLineBreaks": true
   }
   ```
3. 打开一份排版混乱、行过长的 `main.tex`，执行「格式化文档」。
4. 再尝试把 `"texlab.latexindent.replacement"` 设为 `"-r"`（前提是你的 YAML 里有 `replacements` 规则），观察是否触发文本替换。

**需要观察的现象**：

- 第 3 步：缩进变成两空格、长行被折到约 60 列——说明 `local` 与 `modifyLineBreaks` 都生效。
- 第 4 步：若 YAML 含替换规则，文本按规则被替换。

**预期结果**：自定义 YAML 与换行调整都体现到格式化结果中。`latexindent` 的 YAML 语法细节与替换 flag 的精确行为**待本地验证**（以 latexindent 官方文档为准）。

#### 4.3.5 小练习与答案

**练习 1**：把 `texlab.latexFormatter` 设为 `"tex-fmt"` 后，`texlab.latexindent.modifyLineBreaks` 还会生效吗？为什么？
**答案**：不会。`modifyLineBreaks` 组装出的是 `latexindent` 的 flag，只在格式化器为 `latexindent` 时被消费；改用 `tex-fmt` 后这条配置不被任何路径读取。`tex-fmt` 的行为由它自己的命令行参数/配置决定。

**练习 2**：用户希望 `latexindent` 使用项目根目录之外的一份 YAML，该用哪个配置项？默认行为又是什么？
**答案**：用 `texlab.latexindent.local` 指向那份 YAML 的路径（会被翻译成 `--local=<路径>`）。默认 `null` 时，texlab 使用项目根目录下的 `latexindent` 配置。

**练习 3**：`replacement` 的默认值是什么？取 `null` 与取 `"-r"` 在调用 `latexindent` 时有何不同？
**答案**：默认 `null`，即不追加任何替换 flag；取 `"-r"` 时 texlab 会在调用 `latexindent` 的命令行里追加 `-r`，启用替换模式。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「BibTeX + LaTeX 双格式化」配置。

**任务**：准备一个含 `main.tex` 与 `refs.bib` 的小论文工程，二者都故意排版混乱（`.bib` 字段挤行、`.tex` 缩进错乱且行过长）。要求：

1. BibTeX 侧用 texlab 内置格式化器，并把最大行宽压到 `40`，观察字段折行。
2. LaTeX 侧用 `latexindent`，提供一份 `latexindent.yaml`，并开启 `modifyLineBreaks`。
3. 分别对两个文件执行「格式化文档」，保存格式化前后各一份，做 diff 对比。

参考配置（示例代码）：

```json
{
  "texlab.bibtexFormatter": "texlab",
  "texlab.formatterLineLength": 40,
  "texlab.latexFormatter": "latexindent",
  "texlab.latexindent.local": "latexindent.yaml",
  "texlab.latexindent.modifyLineBreaks": true,
  "texlab.latexindent.replacement": null
}
```

**验收点**：

- `.bib` 字段对齐、长字段按 40 列折行。
- `.tex` 缩进按 YAML 规则整理、长行被折断。
- 能说清「`.bib` 由 `bibtexFormatter` + `formatterLineLength` 决定，`.tex` 由 `latexFormatter` + `latexindent.*` 决定」这条分流逻辑。

> 进阶：把 `latexFormatter` 临时改成 `"texlab"`，确认 LaTeX 不再被格式化（复现「未实现」现象），再改回 `"latexindent"`。

## 6. 本讲小结

- texlab 把格式化按语言分流：`.bib` 走 `texlab.bibtexFormatter`（默认 `texlab` 内置），`.tex` 走 `texlab.latexFormatter`（默认 `latexindent` 外部），两条链路相互独立。
- `bibtexFormatter` 取 `texlab`/`latexindent`/`none`；`latexFormatter` 多一个 `tex-fmt`，且其 `texlab` 取值**尚未实现**——这是排查「LaTeX 不格式化」的首要怀疑点。
- `texlab.formatterLineLength`（扁平命名，默认 `80`，`0` 禁用）只作用于 BibTeX 格式化的行宽，主要服务于内置 `texlab` 格式化器。
- `texlab.latexindent.local` / `modifyLineBreaks` / `replacement` 三个子项与 `latexindent` 的 `--local=` / `--modifylinebreaks` / `-r|-rv|-rr` flag **一一对应**，且只在某一侧用 `latexindent` 时才被消费。
- 格式化由标准 LSP 方法 `textDocument/formatting` 触发，只改变排版、不影响编译；这些配置项都不使用 `%f`/`%p`/`%l` 占位符。

## 7. 下一步学习建议

- 若你想继续挖掘 texlab 的「编辑器内呈现」类配置，可接着读 [u5-l2 符号、补全、悬停与 Inlay Hints](u5-l2-symbols-completion-hover.md)，那里同样是用一组 `texlab.*` 配置改变「看到」的样子。
- 若你对「调用外部工具 + flag 映射」这一模式感兴趣，可回看 [u2-l2 构建配置](u2-l2-build-config.md) 中 `build.args` 的写法，以及 [u3-l3 Tectonic 引擎](u3-l3-tectonic-engine.md) 中把引擎参数与产物目录「两头对齐」的做法，与本讲的 `latexindent.*` flag 组装是同构的。
- 想深入 `latexindent` 自身能力（YAML 规则、替换模式、换行策略）的读者，建议直接阅读 `latexindent` 官方文档——本讲只覆盖 texlab 侧的「开关」，真正的格式化规则在那里定义。
