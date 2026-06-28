# 配置总览：命名空间、类型与占位符

## 1. 本讲目标

本讲是整个「配置」主题的地基。读完本讲，你应该能够：

1. 说清楚 texlab 的配置**由谁持有、由谁查询**——这是后续所有配置讲义的公共前提。
2. 拿到 Configuration.md 里任意一个配置项，都能读出它的**三要素**：Type（类型）、Default value（默认值）、Placeholders（占位符，仅部分项有）。
3. 准确说出 `%f`、`%p`、`%l` 三个占位符各自的含义，以及它们分别只出现在哪些配置项里。

本讲**不**逐项讲解每个配置的作用（那是后续讲义的任务），只建立「如何读配置、配置从哪里来」的通用语言。掌握这层语言后，再去看 `build.*`、`chktex.*`、`experimental.*` 等具体项就会事半功倍。

## 2. 前置知识

本讲承接 [u1-l1 texlab 是什么](u1-l1-texlab-overview.md)。你需要先记住以下两点（已在 u1-l1 建立）：

- texlab 是一个 **LSP 服务器**，它和编辑器之间走的是 LSP（基于 JSON-RPC 的语言服务器协议）。
- LaTeX 工作流里有四个角色：编辑器（LSP 客户端）、texlab（LSP 服务器）、TeX 引擎、PDF 阅读器。texlab 与引擎、阅读器之间靠**调用外部命令**交互。

此外需要补充两个通俗概念：

- **客户端 / 服务器（client / server）**：发起请求的一方叫客户端，应答的一方叫服务器。在 LSP 里，编辑器是客户端、texlab 是服务器。但 LSP 有一个反直觉的设计——**配置流向**恰好和「编辑器问 texlab 要补全」相反，本讲第 4.1 节会专门讲这一点。
- **JSON-RPC**：一种用 JSON 表示请求和响应的远程调用协议。texlab 与编辑器之间所有的配置查询、补全请求、编译请求，底层都是 JSON-RPC 消息。

> 提示：本仓库是 texlab 的**官方 wiki**（纯文档，7 个页面），所以本讲的「源码」就是这 7 篇 wiki 文档本身。我们会重点精读 `Configuration.md`。

## 3. 本讲源码地图

本讲涉及的关键文件如下表：

| 文件 | 作用 | 本讲用到的地方 |
| --- | --- | --- |
| `Configuration.md` | 列出 texlab 全部配置项，每项给出 Type / Default value / Placeholders | 几乎所有源码精读都取自这里 |
| `Home.md` | wiki 首页 | 仅作导览参考（内容极少） |

后续讲义会陆续引入 `Previewing.md`、`Tectonic.md`、`LSP-Internals.md`、`Workspace-commands.md`、`Project-Detection.md`，本讲暂不需要。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 配置查询机制**——配置到底存在哪里、texlab 怎么拿到它。
- **4.2 `texlab.*` 命名空间**——所有配置项的统一命名规则与三要素读法。
- **4.3 占位符 `%f` / `%p` / `%l`**——参数模板里被服务器替换的动态片段。

### 4.1 配置查询机制

#### 4.1.1 概念说明

很多人第一次接触 LSP 时会猜测：「texlab 应该会去读一个 `texlab.json` 或 `config.toml` 配置文件吧？」**这是错的。** texlab 自己并不定义配置文件格式，也不直接读任何配置文件。

事实恰恰相反：**配置由 LSP 客户端（也就是你的编辑器）持有，texlab 作为服务器，在需要时去「查询」客户端。** 这一点由 `Configuration.md` 开篇第一句话直接点明：

> This page describes the configuration settings that the server will query from the LSP client.
>
> （本页描述的是服务器将**从 LSP 客户端查询**的配置项。）

详见 [Configuration.md:1](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L1)。

为什么要这样设计？因为不同编辑器用的配置格式千差万别：

- VS Code 用 **JSON（JSONC，带注释）**——`settings.json`。
- Neovim 用 **Lua**——`setup({})` 里传表。
- Helix 用 **TOML**。
- 还可能有用 YAML、自定义 DSL 的。

如果 texlab 自己读配置文件，就得为每种编辑器各写一套解析器。LSP 的解法是：**让编辑器负责解析自己格式的配置，texlab 只通过一个标准的 LSP 请求把值要过来。** 这样 texlab 就和具体的配置文件格式彻底解耦了。

#### 4.1.2 核心流程

配置从「你写下来」到「texlab 用上」，经过下面这条链路（注意方向，这是 LSP 里少数「服务器主动请求客户端」的场景）：

```
┌─────────────┐   1. 用户在编辑器配置里写 texlab.*  ┌─────────────┐
│   编辑器     │ ─────────────────────────────────▶ │  （配置存储） │
│ (LSP 客户端) │                                     └─────────────┘
└──────┬──────┘
       │ 2. 启动时 initialize；客户端声明支持 configuration 能力
       │
       │ ◀──── 3. texlab 发送 workspace/configuration 请求
       │         （带着要查的 section，如 "texlab"）
       │
       │ 4. 客户端返回对应键的值（未设置则返回默认值）────▶
       ▼
   texlab 拿到值，驱动构建 / 预览 / 诊断等行为
```

伪代码描述这个过程：

```text
# texlab（服务器）侧，伪代码
need_value_for(key="texlab.build.executable"):
    resp = lsp_client.workspace_configuration(
             items=[{ section: "texlab.build.executable" }])
    return resp[0]   # 若用户没设，客户端可能返回 null，texlab 再回退到内置默认值
```

关键点：

1. **方向反转**：平时是编辑器（客户端）问 texlab 要补全、要诊断；而配置查询是 texlab（服务器）反过来问编辑器要值。
2. **按需查询**：texlab 并不一定在启动时一次性读走全部配置，而是在用到某项功能时才查询对应键。
3. **默认值兜底**：如果客户端返回空（用户没配置），texlab 会用 Configuration.md 里写的 `Default value`。这就是为什么即使你什么都不配，texlab 也能用 `latexmk` 编译——因为默认值就是它。

#### 4.1.3 源码精读

本仓库（wiki）只点明了「查询方向」，并未展开 LSP 请求细节。我们严格依据 wiki：

- [Configuration.md:1](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L1)：明确「配置由服务器向 LSP 客户端查询」。这是整本配置讲义的总纲，务必记住这句话。

至于「查询用的是 LSP 的 `workspace/configuration` 请求」这一点，属于 **LSP 协议本身的规范**（不是 wiki 的原文断言），texlab 遵循该规范实现。具体 texlab 内部到底查哪个 `section`、是否缓存、何时刷新，本仓库未给出，属于 **待确认** 的实现细节，本讲不臆造。

> 阅读提示：正因为配置在客户端手里，所以**本讲以及后续讲义里出现的所有 JSON 示例，本质是「编辑器配置」的写法**（最常见的是 VS Code 风格的 JSONC），而不是 texlab 自己规定的文件。换一个编辑器，同样的配置值要用别的语法表达。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「texlab 向客户端查询配置」这一反直觉方向，从而记住配置归客户端管。

**操作步骤**（以 VS Code 为例，其它编辑器思路相同）：

1. 安装 texlab 与任意一个能驱动它的 VS Code 扩展（如 LaTeX Workshop，或直接用支持 LSP 的通用扩展）。
2. 在 VS Code 的 `settings.json` 中打开 LSP 详细日志：
   ```jsonc
   // 示例配置（VS Code 风格 JSONC，仅用于打开日志，具体字段名以你所装扩展为准）
   "texlab.trace.server": "verbose"
   ```
3. 打开任意一个 `.tex` 文件，等待 texlab 启动。
4. 在「输出」面板切换到 texlab 的 LSP 通道，查看 JSON-RPC 消息流。

**需要观察的现象**：日志里应能看到从 texlab（server）发往编辑器（client）的请求，其 `method` 字段形如 `workspace/configuration`（**待本地验证**：不同扩展/版本的字面日志格式可能略有差异）。

**预期结果**：你会直观感受到——平时是客户端发请求给服务器，而配置这一项是服务器反过来向客户端要值。这印证了 [Configuration.md:1](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L1) 的描述。

> 若你的编辑器看不到明显日志，可改为「源码阅读型实践」：在 Configuration.md 中找一处 `Default value`，说明「如果我不配置这一项，texlab 会从哪里拿到值」——答案是内置默认值，而不是某个文件。

#### 4.1.5 小练习与答案

**练习 1**：texlab 的配置到底存在哪里？是服务器硬盘上的某个文件吗？

> **参考答案**：不是。配置由 **LSP 客户端（编辑器）** 持有，texlab 通过查询客户端获取（依据 [Configuration.md:1](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L1)）。具体写到哪个文件，由编辑器决定（VS Code 写 `settings.json`、Neovim 写 Lua 配置等）。

**练习 2**：为什么 LSP 不让 texlab 自己读一个固定的配置文件？

> **参考答案**：因为不同编辑器的配置格式不同（JSON / Lua / TOML / YAML……）。让 texlab 自己解析就得为每种格式写解析器；LSP 选择让编辑器各自解析，texlab 只用一个标准请求取值，实现解耦。

---

### 4.2 `texlab.*` 命名空间

#### 4.2.1 概念说明

既然配置值要被 texlab「认得」，所有键就必须有一套**统一的命名**。texlab 的做法是：所有配置项都共享前缀 `texlab.`，并按功能分类形成层级路径。

- 顶层前缀永远是 `texlab`，保证不会和编辑器里其它扩展的配置撞车。
- 第二层是**分类（category）**，如 `build`、`forwardSearch`、`chktex`、`diagnostics`、`symbols`、`hover`、`inlayHints`、`experimental` 等。
- 个别项还会更深，如 `texlab.experimental.labelDefinitionPrefixes` 有三层。

而 Configuration.md 在描述**每一个**配置项时，都遵循同一套模板——我称之为「三要素」：

| 要素 | 含义 | 是否必有 |
| --- | --- | --- |
| **Type** | 该值的 JSON 类型，如 `string`、`boolean`、`string[]` | 必有 |
| **Default value** | 用户不设置时 texlab 使用的默认值 | 必有 |
| **Placeholders** | 参数里可被替换的占位符（如 `%f`） | **仅部分项有** |

学会读这三要素，你就能独立读懂 Configuration.md 里**任何**一项配置，不必每篇讲义都重学。

#### 4.2.2 核心流程

阅读一个配置项的标准动作：

```text
看到 ## texlab.xxx
  ├─ 读它的说明文字 → 搞清「这项控制什么行为」
  ├─ 看 Type → 知道要填什么形态的值（字符串？布尔？数组？）
  ├─ 看 Default value → 不填时是什么（决定你是否必须显式配置）
  └─ 看 Placeholders（若有）→ 参数里能用哪些占位符
```

关于 Type，Configuration.md 里出现的类型可以归成下表（帮助你建立「类型词汇表」）：

| Type | 含义 | 出现示例 |
| --- | --- | --- |
| `string` | 字符串 | `build.executable` |
| `string \| null` | 字符串，或显式空 | `forwardSearch.executable` |
| `boolean` | 真 / 假 | `build.onSave` |
| `integer` / `int \| null` | 整数 | `diagnosticsDelay`、`inlayHints.maxLength` |
| `string[]` | 字符串数组 | `build.args` |
| `string[] \| null` | 字符串数组，或显式空 | `forwardSearch.args` |
| `(string, string)[]` | 「二元组」数组 | `experimental.labelDefinitionPrefixes` |
| `SymbolEnvironmentOptions[]` | 自定义对象数组（带 TS 接口定义） | `symbols.customEnvironments` |

> 说明：`| null` 表示该项允许显式设为「无」（区别于「没设置」）。在需要「关闭某功能」时很有用，例如 `forwardSearch.executable` 默认 `null` 即表示「未配置正向搜索」。

至于分类（第二层），Configuration.md 里大致可以这样归并（本表只列代表性项，不展开语义，后续讲义细讲）：

| 分类前缀 | 代表配置项 | 一句话主题 |
| --- | --- | --- |
| `texlab.build.*` | `executable`、`args`、`onSave`、`pdfDirectory` | 怎么编译 |
| `texlab.forwardSearch.*` | `executable`、`args` | 怎么正向搜索（编辑器→PDF） |
| `texlab.chktex.*` | `onOpenAndSave`、`onEdit`、`additionalArgs` | 静态检查 chktex |
| `texlab.diagnostics*` | `diagnosticsDelay`、`diagnostics.allowedPatterns` | 诊断延迟与过滤 |
| `texlab.symbols.*` | `allowedPatterns`、`customEnvironments` | 文档符号 |
| `texlab.*Formatter` / `latexindent.*` | `bibtexFormatter`、`latexFormatter` | 格式化 |
| `texlab.completion.*` | `matcher` | 补全匹配算法 |
| `texlab.hover.*` | `symbols` | 悬停展示 |
| `texlab.inlayHints.*` | `labelDefinitions`、`maxLength` | 内联提示 |
| `texlab.experimental.*` | `mathEnvironments`、`labelDefinitionCommands` | 实验性扩展 |

注意有个**已弃用**项 `texlab.auxDirectory`（见 Configuration.md 标注 `(DEPRECATED)`），它被 `texlab.build.auxDirectory` 等取代，新配置应避免使用。

#### 4.2.3 源码精读

我们用最简单的 `texlab.build.executable` 来演示「三要素读法」：

- [Configuration.md:5-12](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L5-L12)：定义 `texlab.build.executable`，说明它是「LaTeX 构建工具的可执行文件」，`Type: string`，`Default value: latexmk`。
  - 这就告诉我们：默认用 `latexmk` 编译；若你想换成 `tectonic` 或别的，把这一项设成对应可执行名即可。

再看一个带自定义对象类型的例子，体会 Type 可以很复杂：

- [Configuration.md:270-285](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L270-L285)：`texlab.symbols.customEnvironments` 的 Type 是 `SymbolEnvironmentOptions[]`，并给出了 TypeScript 接口 `SymbolEnvironmentOptions`（含 `name`、可选 `displayName`、可选 `label` 字段）。
  - 这说明：当 Type 是某个**自定义接口数组**时，wiki 会紧跟一段 TS 接口定义告诉你对象长什么样。读到这种 Type，记得往下翻接口。

最后看一个三元组数组的例子：

- [Configuration.md:512-527](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L512-L527)：`texlab.experimental.labelDefinitionPrefixes`，`Type: (string, string)[]`，默认 `[]`。它把「命令名」和「标签前缀」成对关联，如 `[["thm", "thm:"]]`。

> 小结：不管项多复杂，抓住 **Type / Default value / Placeholders** 三要素就能读懂。

#### 4.2.4 代码实践

**实践目标**：用「三要素」法独立阅读 Configuration.md，验证你已掌握通用读法。

**操作步骤**：

1. 打开 [Configuration.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md)。
2. 任意挑选 3 个你还没细看过的配置项（建议分别来自 `build`、`diagnostics`、`experimental` 三个分类）。
3. 对每一项，填出下表：

| 配置项 | Type | Default value | Placeholders（若无写「无」） | 一句话作用 |
| --- | --- | --- | --- | --- |

**需要观察的现象**：你会确认绝大多数项都有 Type 和 Default value，而 Placeholders 只在极少数项出现。

**预期结果**：你能不看任何讲义，仅凭三要素就读懂这三项的含义与默认行为。这就是本讲的「通用语言」目标。

#### 4.2.5 小练习与答案

**练习 1**：描述一个 texlab 配置项需要哪「三要素」？哪个不是每项都有？

> **参考答案**：Type（类型）、Default value（默认值）、Placeholders（占位符）。其中 **Placeholders 不是每项都有**，只有少数需要传命令行参数的项（如 `build.args`、`forwardSearch.args`）才列出。

**练习 2**：`texlab.experimental.labelDefinitionPrefixes` 的 Type 是什么？默认值是什么？

> **参考答案**：Type 是 `(string, string)[]`（二元组数组），默认值是 `[]`（见 [Configuration.md:525-527](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L525-L527)）。

---

### 4.3 占位符 `%f` / `%p` / `%l`

#### 4.3.1 概念说明

texlab 调用外部命令（TeX 引擎、PDF 阅读器）时，需要把「当前文件路径、PDF 路径、光标行号」等**动态信息**拼进命令行参数。如果让你为每个文件都写一份不同的 args，那就太笨了。

占位符（Placeholder）就是解决方案：你在 args 里写一个**模板**，里面用 `%f`、`%p`、`%l` 占位，texlab 在真正执行命令前会**把它们替换成实际值**。这样同一份 args 模板就能适用于任何文件。

三个占位符的含义（严格依据 Configuration.md）：

| 占位符 | 含义 | 出现的配置项 |
| --- | --- | --- |
| `%f` | TeX 文件路径 | `build.args`、`forwardSearch.args` |
| `%p` | 当前 PDF 文件路径 | 仅 `forwardSearch.args` |
| `%l` | 当前行号 | 仅 `forwardSearch.args` |

注意一个容易混淆的点：`%f` 同时出现在 `build.args` 和 `forwardSearch.args`，但语义略有侧重——在 `build.args` 里它是「要编译的 TeX 文件」，在 `forwardSearch.args` 里它是「当前 TeX 文件」。而 `%p` 和 `%l` **只**出现在 `forwardSearch.args`，因为只有「正向搜索」（从编辑器跳到 PDF）才需要定位到某个 PDF 的某一行；编译本身不需要 PDF 路径和行号。

#### 4.3.2 核心流程

占位符替换的执行过程：

```text
用户配置: build.args = ["-pdf", "-synctex=1", "%f"]
                                  ↓ texlab 在执行前替换
当前文件: /home/me/paper/main.tex
                                  ↓
实际命令: latexmk -pdf -synctex=1 /home/me/paper/main.tex
```

对 `forwardSearch.args` 同理，只是多了 `%p`、`%l`：

```text
forwardSearch.args = ["--forward", "%l", "%f", "%p"]
光标在第 42 行，PDF 为 /home/me/paper/main.pdf
        ↓
实际命令: <阅读器> --forward 42 /home/me/paper/main.tex /home/me/paper/main.pdf
```

还有一条**极易踩坑**的规则（来自 Configuration.md）：当你要给构建工具传「一个 flag 加它的参数」时（例如 `-foo bar`），不能写成数组里的一个字符串 `"-foo bar"`，而**必须拆成两个数组元素** `["-foo", "bar"]`。这是因为 texlab 把每个数组元素当作命令行的独立一段，不会替你按空格切分。

#### 4.3.3 源码精读

先看 `build.args`，它只用 `%f`：

- [Configuration.md:15-30](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L15-L30)：定义 `texlab.build.args`。
- [Configuration.md:18-22](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L18-L22)：明确两件事——① flag 和它的参数要拆成数组的**独立元素**（`-foo bar` → `["-foo", "bar"]`）；② 「The placeholder `%f` will be replaced by the server」（`%f` 会被服务器替换）。
- [Configuration.md:24-26](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L24-L26)：占位符清单，`%f` = 要编译的 TeX 文件路径。
- [Configuration.md:30](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L30)：默认值 `["-pdf", "-interaction=nonstopmode", "-synctex=1", "%f"]`——注意默认就含 `%f`，且每个 flag 各占一个元素（符合上面的拆分规则）。

再看 `forwardSearch.args`，它同时用到三个占位符：

- [Configuration.md:141-155](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L141-L155)：定义 `texlab.forwardSearch.args`。
- [Configuration.md:144](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L144)：明确「The placeholders `%f, %p, %l` will be replaced by the server」（`%f, %p, %l` 会被服务器替换）。
- [Configuration.md:148-150](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L148-L150)：占位符清单——
  - `%f`：当前 TeX 文件路径
  - `%p`：当前 PDF 文件路径
  - `%l`：当前行号
- [Configuration.md:152-154](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L152-L154)：`Type: string[] | null`，默认 `null`——也就是**默认未配置正向搜索**，需要你显式填。

> 对比记忆：`build.args` 默认值已带 `%f`，开箱即可编译；而 `forwardSearch.args` 默认 `null`，必须你自己根据所用 PDF 阅读器填写（这部分配方在后续 `Previewing.md` 讲义）。

#### 4.3.4 代码实践

**实践目标**：理解占位符替换的结果，从而能正确书写 args 模板。

**操作步骤**：

1. 假设你有文件 `/work/paper/main.tex`，光标停在第 `10` 行，编译产物为 `/work/paper/main.pdf`。
2. 给定如下配置（示例代码，用于推演替换结果）：
   ```jsonc
   // 示例配置（仅用于推演占位符替换）
   "texlab.build.args":        ["-pdf", "-synctex=1", "%f"],
   "texlab.forwardSearch.args": ["--forward-search", "%l", "1", "%f", "%p"]
   ```
3. 在纸上（或注释里）写出两段 args 被替换后的**实际命令行片段**。

**需要观察的现象**：`%f`、`%p`、`%l` 被分别替换为上述具体值，其余字符原样保留。

**预期结果**：

- `build.args` 替换后形如：`-pdf -synctex=1 /work/paper/main.tex`
- `forwardSearch.args` 替换后形如：`--forward-search 10 1 /work/paper/main.tex /work/paper/main.pdf`

（命令是否真的被调用、阅读器是否真的跳转，**待本地验证**；本实践只验证「替换结果」这一步。）

#### 4.3.5 小练习与答案

**练习 1**：`%p` 和 `%l` 为什么只出现在 `forwardSearch.args`，而不出现在 `build.args`？

> **参考答案**：因为编译只需要知道「编译哪个 TeX 文件」（`%f`），不需要 PDF 路径和行号；而正向搜索要把光标位置跳到 PDF 的对应行，所以需要 `%p`（PDF 路径）和 `%l`（行号）。依据见 [Configuration.md:24-26](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L24-L26) 与 [Configuration.md:148-150](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L148-L150)。

**练习 2**：要给构建工具传 `-outdir build`，`build.args` 该怎么写？为什么不能写成 `"-outdir build"`？

> **参考答案**：应写成两个独立元素 `["-outdir", "build"]`。因为 Configuration.md 明确要求「flags and their arguments need to be separate elements」，texlab 不会替你按空格切分单个元素（见 [Configuration.md:18-22](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L18-L22)）。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿任务（这也是本讲规格里指定的实践）。

**任务**：为你常用的编辑器写一份**最小的 texlab 配置**，至少包含 `build.executable`、`build.args`（带 `%f`）、`forwardSearch.args`（带 `%f`、`%p`、`%l`），并为每个字段用注释标注其 **Type** 与 **Default value**。

下面是一份 VS Code 风格（JSONC，带注释）的**示例答案**。请把它当作模板，按你自己的编辑器和 PDF 阅读器调整：

```jsonc
// ===== 示例配置（VS Code 风格 JSONC）=====
// 注意：texlab 不规定配置文件格式；这里的 JSONC 是 VS Code 的写法。
//       换 Neovim/Helix 等编辑器时，同样的值要用对应语法表达，
//       最终都通过 LSP 的配置查询交给 texlab。
{
  // Type: string ；Default value: "latexmk"
  // 决定用哪个程序来编译。这里保持默认 latexmk。
  "texlab.build.executable": "latexmk",

  // Type: string[] ；Default value: ["-pdf", "-interaction=nonstopmode", "-synctex=1", "%f"]
  // 传给编译工具的参数。注意：
  //  1) flag 与它的参数必须拆成独立元素（如 ["-outdir", "build"]）；
  //  2) %f 会被 texlab 替换为「要编译的 TeX 文件路径」。
  "texlab.build.args": [
    "-pdf",
    "-interaction=nonstopmode",
    "-synctex=1",
    "%f"
  ],

  // Type: string | null ；Default value: null
  // 正向搜索所用阅读器。需支持 SyncTeX。示例用占位名，请换成你机器上的真实阅读器。
  "texlab.forwardSearch.executable": "<你的PDF阅读器，如 zathura / SumatraPDF / Skim>",

  // Type: string[] | null ；Default value: null
  // 正向搜索参数，含三个占位符：
  //  %f = 当前 TeX 文件路径；%p = 当前 PDF 路径；%l = 当前行号。
  // 下面是「示意」写法，真实 flag 因阅读器而异（详见后续 Previewing 讲义）。
  "texlab.forwardSearch.args": [
    "--synctex-forward",
    "%l:1:%f",
    "%p"
  ]
}
```

**自检清单**（逐条对照本讲内容）：

- [ ] 你能解释为什么这些配置写在编辑器里，而不是某个 texlab 的配置文件里。（对应 4.1：配置由客户端持有）
- [ ] 你为每个字段标注了 Type 与 Default value，且与 Configuration.md 一致。（对应 4.2：三要素）
- [ ] 你能说清 `%f`、`%p`、`%l` 各自含义，并解释为何 `%p`、`%l` 只在 `forwardSearch.args` 出现。（对应 4.3：占位符）
- [ ] 你的 `build.args` 中每个 flag 与其参数都是独立的数组元素。

> 提示：本实践只要求**写出并读懂**配置。真正的「保存即编译、跳转到 PDF」要在后续 [u2-l2 构建配置](u2-l2-build-config.md) 与 [u3 系列预览讲义](u3-l1-build-and-preview-workflow.md) 里才能端到端跑通。

## 6. 本讲小结

- **配置归客户端**：texlab 的配置由 LSP 客户端（编辑器）持有，texlab 作为服务器去查询获取（[Configuration.md:1](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L1)），texlab 本身不读配置文件、不规定配置格式。
- **统一命名空间**：所有配置项都以 `texlab.` 为前缀，按 `texlab.<分类>.<键>` 分层组织，避免与其它扩展冲突。
- **三要素读法**：读懂任何一项只需抓住 Type（类型）、Default value（默认值）、Placeholders（占位符，仅部分项有）。
- **占位符**：`%f`（TeX 文件路径）出现在 `build.args` 和 `forwardSearch.args`；`%p`（PDF 路径）和 `%l`（行号）**只**出现在 `forwardSearch.args`，由 texlab 在执行前替换。
- **拆分规则**：在 `args` 数组里，flag 与它的参数必须拆成独立元素（`-foo bar` → `["-foo", "bar"]`）。
- **默认值兜底**：不配置时 texlab 用内置默认值（如 `build.executable` 默认 `latexmk`、`build.args` 默认已含 `%f`），所以零配置也能编译；但 `forwardSearch.args` 默认 `null`，需自行填写。

## 7. 下一步学习建议

本讲建立的是「配置通用语言」。接下来建议：

1. **[u2-l2 构建配置 `texlab.build.*`](u2-l2-build-config.md)**：本讲只示范了 `build.executable` / `build.args`，下一讲会逐项讲清 `onSave`、`forwardSearchAfter`、`useFileList`、`auxDirectory`/`logDirectory`/`pdfDirectory`、`filename` 等，以及它们与 `latexmkrc` 自动推断的关系。这是配置主题里最重要、最常用的一篇。
2. **之后再进入 u3（编译与预览）**：在那里把 `build.*`、`forwardSearch.*` 与 SyncTeX 串成端到端工作流。
3. **想直接查具体项**：随时回到 [Configuration.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md)，用本讲教的「三要素」法独立阅读即可。
