# Docsify 扩展 Markdown 写作规范

## 1. 本讲目标

本讲面向「想给这套在线文档贡献内容」的读者，解决一个问题：**本仓库的 Markdown 在标准语法之外，约定了哪些 Docsify 扩展写法？**

学完后你应当能够：

- 用 `?>` / `!>` 写出两种语义不同的提示框；
- 正确选择代码围栏的语言标记，让代码被 Prism 正确着色（并知道哪些语言不会被着色）；
- 用行内 KaTeX 语法写出数学公式；
- 用 Docsify 路由风格（`/path/`、`/path`、`/path?id=锚点`）写站内链接。

## 2. 前置知识

在进入本讲前，请确认你已经了解：

- **Markdown 基础**：知道标题、列表、链接 `[文本](地址)`、代码围栏 ` ```语言 ` 怎么写。
- **Docsify 的渲染方式**（见 [u1-l4](u1-l4-entry-and-navigation.md) 与 [u2-l1](u2-l1-docsify-config-and-theme.md)）：浏览器端的 docsify 库把 `.md` 文本渲染成网页，标准 Markdown 之外它还额外支持一些「扩展语法」。
- **代码高亮由 Prism 提供**（见 [u2-l2](u2-l2-koopa-syntax-highlight.md)）：docsify 内置 Prism，会根据代码围栏的语言标记决定如何上色。
- **本地能跑起站点**（见 [u1-l2](u1-l2-run-locally.md)）：本讲几乎所有实践都需要 `docsify serve docs` 启动本地预览来观察渲染效果。

一句话回顾：docsify 在标准 Markdown 之上，约定了少量扩展语法，本仓库把这些扩展当作「写作规范」来统一使用。

## 3. 本讲源码地图

本讲涉及的文件不多，但都是「示例库」与「引擎」两类：

| 文件 | 作用 |
| --- | --- |
| `docs/lv1-main/README.md` | 浓缩了三种代码围栏（`c`/`koopa`/`ebnf`）和行内公式的最佳示例。 |
| `docs/lv1-main/structure.md` | 包含 `?>` 提示框、行内公式、Docsify 路由链接（含中文锚点）的示例。 |
| `docs/preface/lab.md` | 包含 `!>` 警告框、`?>` 提示框的典型用法。 |
| `docs/index.html` | 加载 Prism 各语言组件与 KaTeX 插件，决定「哪些围栏会着色」「公式靠谁渲染」。 |
| `docs/toc.md` | 侧边栏里大量使用 Docsify 路由风格链接，是写内部链接的范本。 |

> 说明：本讲的「源码」主要是 Markdown 内容文件本身——因为这些扩展语法就写在正文里。`index.html` 则解释了它们为什么能被渲染。

## 4. 核心概念与源码讲解

### 4.1 提示框语法（`?>` / `!>`）

#### 4.1.1 概念说明

标准 Markdown 没有专门的「提示框 / 警告框」。Docsify 在渲染前会对正文做一道预处理：**当一行以 `?>` 或 `!>` 开头时，把这一段渲染成一个带边框/底色的强调块**，而不是普通段落。

本仓库对这两种标记形成了稳定的语义约定：

- `?>` —— **提示框（tip）**，语气轻，用于补充说明、建议、思考题、待补充（TODO）。
- `!>` —— **警告框（warning）**，语气重，用于必须强调的重要事项、禁止行为、学术诚信。

`?>` 轻提示、`!>` 重警告，这是作者按语义轻重自行选择的，两种标记本身只决定样式，不决定内容。

#### 4.1.2 核心流程

从写作到渲染的过程：

1. 作者在 `.md` 某行行首写 `?> ` 或 `!> `，后跟正文。
2. docsify 在解析 Markdown 前，识别这两个前缀，把该段标记为「强调块」。
3. 渲染时套用主题（docsify-themeable）提供的样式，显示为带边框/底色的框。
4. 框内的正文**仍然是 Markdown**——可以加粗、放链接、放行内代码。

#### 4.1.3 源码精读

**重警告 `!>` 的典型用法**——学术诚信提示，语气最重：

[docs/preface/lab.md:67-L67](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/preface/lab.md#L67) —— 整行 `!> 学术诚信远比课程实践本身重要.`，渲染为红色警告框，紧跟在「学术诚信」标题下，起到开门见山的强调作用。

**带 Markdown 格式的 `!>`**——框内还包含加粗的「**注意:**」：

[docs/lv1-main/testing.md:36-L36](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/lv1-main/testing.md#L36) —— `!> **注意:** 请务必将你创建的 repo 的可见性设为 "Private" ...`，说明框内正文照样能被 Markdown 解析。

**轻提示 `?>` 的典型用法**——补充说明：

[docs/lv1-main/structure.md:79-L79](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/lv1-main/structure.md#L79) —— `?> 在这里和之前解释 token 流的部分, 我们都用了 "可能" ...`，作为对正文的轻量补充。

**`?>` 用于 TODO 占位**：

[docs/preface/lab.md:55-L55](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/preface/lab.md#L55) —— `?> **TODO:** 待补充`，本仓库统一用 `?>` 标记尚未写完的章节。

#### 4.1.4 代码实践

1. **实践目标**：亲手看到 `?>` 与 `!>` 的样式差异。
2. **操作步骤**：
   - 在 `docs/lv1-main/` 下新建一个测试文件（例如 `docs/lv1-main/_callout-test.md`），写入：
     ```markdown
     # 提示框测试

     ?> 这是一个轻提示 (tip)。

     !> 这是一个重警告 (warning)。
     ```
   - 运行 `docsify serve docs`，在浏览器地址栏把路径改成 `/#/lv1-main/_callout-test` 打开它。
3. **需要观察的现象**：两个段落各自变成带边框/底色的框，且 `!>` 的样式比 `?>` 更醒目（警告色）。
4. **预期结果**：能在同一页里直观对比两种框的轻重差别。
5. **待本地验证**：具体颜色取决于主题，以你本地看到的为准。

> 实践结束后请删除这个测试文件，避免污染仓库。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `docs/preface/lab.md` 的「学术诚信」用 `!>` 而不是 `?>`？
**答案**：学术诚信是必须强调、不可违反的红线，属于「重警告」，按本仓库约定应用 `!>`；`?>` 留给轻量的补充说明。

**练习 2**：在 `!>` 框里能不能放一个 Markdown 链接？
**答案**：可以。框内正文仍是 Markdown，`[文本](地址)`、加粗、行内代码都会正常渲染（参见 testing.md 里加粗的「**注意:**」）。

### 4.2 代码围栏与高亮

#### 4.2.1 概念说明

Markdown 的代码围栏写作 ` ```语言 `，`语言` 这个标记被称为 **info string**。在本仓库里，它有一个非常实际的作用：**决定这段代码是否被 Prism 着色**。

- 写 ` ```c `，Prism 用 C 的规则着色；
- 写 ` ```koopa `，用本仓库自写的 Koopa IR 规则着色（见 [u2-l2](u2-l2-koopa-syntax-highlight.md)）；
- 写 ` ```ebnf `，用 EBNF 文法规则着色；
- **写错或写了一个没有加载对应组件的语言（如 `asm`），代码仍能正常显示，但不会被着色**，只是一段等宽纯文本。

因此「围栏语言」不是装饰，而是触发高亮的开关。

#### 4.2.2 核心流程

1. 作者用 ` ```语言 ` 开启代码块，` ``` ` 结束。
2. docsify 把代码块连同语言标记交给 Prism。
3. Prism 查找 `Prism.languages[语言]` 是否存在：
   - 存在 → 按该语言的 token 规则切分并上色；
   - 不存在 → 不做处理，原样显示为普通代码块。
4. 这些 `Prism.languages[语言]` 来自 `docs/index.html` 里加载的 Prism 组件脚本。

所以**「哪些语言能被着色」=「index.html 里加载了哪些 Prism 组件」**。

#### 4.2.3 源码精读

**index.html 加载的 Prism 组件清单**——这决定了能着色的语言集合：

[docs/index.html:52-L59](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L52-L59) —— 依次加载 `prism-c`、`prism-cpp`、`prism-ebnf`、`prism-bash`、`prism-makefile`、`prism-bison`、`prism-rust`（以上来自 CDN），以及本地的 `assets/js/prism-koopa.js`。注意：**这里没有 `prism-asm`，也没有 `prism-flex`**。

**三种「会着色」的围栏示例**——集中在 Lv1 章节首页：

[docs/lv1-main/README.md:5-L10](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/lv1-main/README.md#L5-L10) —— ` ```c ` 包裹的 SysY 源程序（`int main() { ... }`），按 C 语法着色。

[docs/lv1-main/README.md:14-L19](https://github.com/pku-minic/online-doc/blob/d172f8994fba0f826ac3f1c2f4d6bf63c9ea5/docs/lv1-main/README.md#L14-L19) —— ` ```koopa ` 包裹的 Koopa IR（`fun @main(): i32 { ... }`），由本地 `prism-koopa.js` 着色。

[docs/lv1-main/README.md:27-L31](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/lv1-main/README.md#L27-L31) —— ` ```ebnf ` 包裹的标识符文法，按 EBNF 着色。

**「不会着色」的反例**——用了 `asm` 但没有对应组件：

[docs/lv0-env-config/riscv.md:32-L32](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/lv0-env-config/riscv.md#L32) —— 以 ` ```asm ` 开头的 RISC-V 汇编代码块。因为 index.html 没有加载 `prism-asm`，这段汇编**不会被着色**，只显示为普通等宽代码。这是初学者最容易踩的坑：围栏语言写得很「合理」，却因为没加载组件而看不到颜色。

#### 4.2.4 代码实践

1. **实践目标**：体会「围栏语言 = 高亮开关」，并验证 `asm` 不被着色。
2. **操作步骤**：
   - 新建测试 `.md`，把同一段 `int main() { return 0; }` 分别用 ` ```c `、` ```txt `、` ```asm ` 三种围栏各写一遍。
   - 运行 `docsify serve docs` 打开该页。
3. **需要观察的现象**：` ```c ` 的代码被着色；` ```txt ` 与 ` ```asm ` 的代码都没有颜色（纯文本）。
4. **预期结果**：只有加载了对应 Prism 组件的语言才会着色；`txt`/`asm` 因无组件而保持原样。
5. **待本地验证**：以本地浏览器实际渲染为准。

> 实践结束后请删除测试文件。

#### 4.2.5 小练习与答案

**练习 1**：为什么 ` ```asm ` 的 RISC-V 汇编在网页上没有颜色？
**答案**：`docs/index.html` 只加载了 c/cpp/ebnf/bash/makefile/bison/rust/koopa 的 Prism 组件，没有 `prism-asm`，所以 Prism 找不到 asm 的语法规则，原样显示。

**练习 2**：如果你想新增一种会着色的语言，需要改哪两处？
**答案**：①准备/加载对应的 Prism 组件脚本（CDN 或本地，类似 `prism-koopa.js`）；②在 `index.html` 里补一个 `<script>` 引用它（注意排在含 Prism 核心的 `docsify.min.js` 之后）。

### 4.3 KaTeX 公式

#### 4.3.1 概念说明

编译原理文档里常出现数学符号（整数范围、求和、希腊字母 \(\phi\) 等）。本仓库用 **KaTeX** 在网页上渲染数学公式，对应的语法是 **行内公式**：用一对美元符号 `$...$` 把 LaTeX 公式包起来。

一个重要事实：**本仓库只使用行内 `$...$`，从不使用块级 `$$...$$`**。下面会说明为什么。

#### 4.3.2 核心流程

1. 作者在正文里写 `$<LaTeX>$`，例如 `$[0, 2^{31} - 1]$`。
2. 页面加载时，`docsify-katex` 插件在 Markdown 渲染阶段扫描这些 `$...$`。
3. 命中的片段被交给 KaTeX 编译成带数学字体的 HTML（数学字体由 `katex.min.css` 提供）。
4. 没被 `$` 包裹的普通文本不受影响。

#### 4.3.3 源码精读

**KaTeX 的两件依赖**——插件 + 数学字体，缺一不可：

[docs/index.html:12-L12](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L12) —— 加载 `katex.min.css`，提供数学符号所需的字体。

[docs/index.html:63-L63](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L63) —— 加载 `docsify-katex.js` 插件，负责识别 `$...$` 并调用 KaTeX 渲染。

**行内公式示例**——整数常量范围：

[docs/lv1-main/README.md:79-L79](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/lv1-main/README.md#L79) —— 原文为 `$[0, 2^{31} - 1]$`，渲染成数学排版的区间记号 \([0, 2^{31}-1]\)。

**一处出现多个行内公式**——IR 折算模块数量的对比：

[docs/lv1-main/structure.md:109-L109](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/lv1-main/structure.md#L109) —— 同一行里写了 `$M \times N$` 与 `$M + N$`，分别渲染为 \(M \times N\) 与 \(M + N\)，直观对比「无 IR 要写 M×N 个模块、有 IR 只写 M+N 个」。

**希腊字母行内公式**：

[docs/lv9p-reincarnation/ssa-form.md:104-L104](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/lv9p-reincarnation/ssa-form.md#L104) —— `$\phi$` 渲染为希腊字母 \(\phi\)，用于指代 SSA 形式里的 \(\phi\) 函数。

> **关于「不用 `$$...$$`」**：在全仓库搜索 `$$`，命中的全是 Bison 语义动作里的 `$$`（如 `docs/lv1-main/lexer-parser.md` 里的 `$$ = ast;`），那是 C/Bison 代码，不是数学公式分隔符。也就是说本仓库没有块级公式，统一用行内 `$...$`。这也提醒作者：如果你的代码里出现 `$$`，要把它放在代码围栏内，否则可能被 KaTeX 误判。

#### 4.3.4 代码实践

1. **实践目标**：验证 `$...$` 会被渲染成公式，而非纯文本。
2. **操作步骤**：
   - 新建测试 `.md`，写入一行正文：`和式 $S = \sum_{i=1}^{n} i$ 与希腊字母 $\alpha$。`
   - 运行 `docsify serve docs` 打开该页。
3. **需要观察的现象**：求和符号、上下标、希腊字母都按数学排版显示（斜体变量、正确的上下标位置），而不是显示 `\sum`、`\alpha` 这样的源码。
4. **预期结果**：KaTeX 正确渲染，公式与正文混排自然。
5. **待本地验证**：以本地浏览器实际渲染为准。

> 实践结束后请删除测试文件。

#### 4.3.5 小练习与答案

**练习 1**：如果公式不显示、页面里直接看到 `$...$` 源码，最可能的原因是什么？
**答案**：`docs/index.html` 没有加载 `docsify-katex.js` 或 `katex.min.css`（插件或字体缺一不可）。

**练习 2**：为什么写 Bison 规则时要避免在代码围栏外裸写 `$$`？
**答案**：`$$` 可能被 KaTeX 当作数学分隔符处理。把 Bison 代码放进 ` ```bison ` 围栏内，就不会被 KaTeX 扫描。

### 4.4 Docsify 路由链接

#### 4.4.1 概念说明

普通 Markdown 链接 `[文本](地址)` 里，「地址」既可以是外部网址，也可以是站内地址。**本仓库的站内地址统一用 Docsify 路由风格**，而不是相对文件路径。也就是说，链接里写的是「浏览器地址栏里的路由」，而不是 `../lv1-main/README.md` 这种文件路径。

Docsify 路由有三种基本形态：

| 写法 | 指向 | 路由规则 |
| --- | --- | --- |
| `/preface/` | 目录 `preface/` 的首页 | 末尾带 `/` → 该目录的 `README.md` |
| `/preface/lab` | `preface/lab.md` 这一页 | 不带扩展名 → `路径.md` |
| `/misc-app-ref/sysy-spec?id=文法定义` | `sysy-spec.md` 页内的某个标题 | `?id=` → 页内标题锚点 |

链接里的路径一律以 `/` 开头（相对于文档根 `docs/`），这与 `toc.md` 侧边栏用的写法完全一致。

#### 4.4.2 核心流程

1. 作者用 `[文本](/路由)` 写站内链接。
2. 点击后，docsify 把路由解析成对应的 `.md` 文件去抓取并渲染：
   - `/a/b/` → 抓 `a/b/README.md`；
   - `/a/b` → 抓 `a/b.md`；
   - `?id=xxx` → 渲染完页面后，滚动到标题为 `xxx` 的锚点。
3. 因为是单页应用，切换页面不会整页刷新，只在应用内换内容。

#### 4.4.3 源码精读

**侧边栏里的路由链接**——三种形态的范本：

[docs/toc.md:1-L2](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/toc.md#L1-L2) —— `[写在前面](/preface/)` 是「目录→README」形态；`[实验说明](/preface/lab)` 是「页面」形态。整站侧边栏都遵循这种写法。

**「页面」形态的正文链接**：

[docs/lv9p-reincarnation/README.md:9-L9](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/lv9p-reincarnation/README.md#L9) —— `[**你的编译器超强的:**](/lv9p-reincarnation/awesome-compiler)`，指向 `lv9p-reincarnation/awesome-compiler.md`。

**带中文锚点的路由链接**——最有代表性：

[docs/lv1-main/structure.md:93-L93](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/lv1-main/structure.md#L93) —— `[SysY 语法定义](/misc-app-ref/sysy-spec?id=文法定义)`，点击后先打开 `misc-app-ref/sysy-spec.md`，再滚动到「文法定义」这个小节标题。锚点直接用中文标题文本即可，docsify 会做归一化匹配。

**与外部链接的对比**——区分站内路由与外链：

[docs/lv1-main/structure.md:111-L111](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/lv1-main/structure.md#L111) —— `[LLVM IR](https://llvm.org/docs/)` 是外部网址（`https://` 开头），与上面的 `/...` 站内路由清晰区分。

#### 4.4.4 代码实践

1. **实践目标**：亲手验证三种路由形态的跳转效果。
2. **操作步骤**：
   - 新建测试 `.md`，写入：
     ```markdown
     # 路由链接测试

     - 跳到实验说明: [实验说明](/preface/lab)
     - 跳到文法定义小节: [SysY 文法定义](/misc-app-ref/sysy-spec?id=文法定义)
     ```
   - 运行 `docsify serve docs` 打开该页，分别点击两个链接。
3. **需要观察的现象**：第一个链接切到「实验说明」页；第二个链接切到 SysY 规范页，并自动滚动到「文法定义」标题处。
4. **预期结果**：路由解析正确，中文锚点也能定位。
5. **待本地验证**：以本地浏览器实际跳转为准。

> 实践结束后请删除测试文件。

#### 4.4.5 小练习与答案

**练习 1**：`/preface/` 和 `/preface` 有什么区别？
**答案**：`/preface/`（末尾带斜杠）指向目录 `preface/` 的 `README.md`；`/preface`（不带斜杠）会被当成 `preface.md` 这个文件。要链接到某章节首页，通常用带斜杠的形式。

**练习 2**：站内链接能不能写成 `../lv1-main/README.md` 这种相对文件路径？
**答案**：不推荐。本仓库统一用 Docsify 路由风格（`/lv1-main/` 或 `/lv1-main/某页`），与 `toc.md` 侧边栏保持一致，否则可能在部署后解析失败。

## 5. 综合实践

把本讲四个模块串起来，完成一个「迷你示例页」：

1. **实践目标**：在一篇 `.md` 里同时用上提示框、代码围栏（含 Koopa 高亮）、KaTeX 公式和 Docsify 路由链接。
2. **操作步骤**：
   - 在 `docs/lv1-main/` 下新建 `docs/lv1-main/_convention-demo.md`，内容如下（示例代码）：
     ```markdown
     # 写作规范 Demo

     ?> 这一页演示本仓库的四种 Docsify 扩展写法, 详见 [编译器结构](/lv1-main/structure).

     !> 注意: `asm` 围栏在本站不会被着色, 因为没有加载 prism-asm.

     下面是一段 Koopa IR:

     ```koopa
     fun @main(): i32 {
     %entry:
       ret 0
     }
     ```

     整数常量的取值范围是 $[0, 2^{31} - 1]$.
     ```
     （注意：上面这段示例里的 Koopa 围栏嵌套在演示代码块中，实际写入文件时请确保围栏成对。）
   - 运行 `docsify serve docs`，浏览器访问 `/#/lv1-main/_convention-demo`。
3. **需要观察的现象**：
   - 出现一个轻提示框（`?>`）和一个重警告框（`!>`）；
   - ` ```koopa ` 代码块被着色；
   - `$[0, 2^{31} - 1]$` 渲染为数学公式；
   - 提示框里的「编译器结构」链接能跳转到 `structure` 页。
4. **预期结果**：四种扩展写法在同一页全部正确渲染。
5. **待本地验证**：以本地浏览器实际效果为准；完成后删除该演示文件。

## 6. 本讲小结

- `?>` = 轻提示（补充/建议/TODO），`!>` = 重警告（重要事项/禁止/学术诚信）；框内正文仍是 Markdown。
- 代码围栏的语言标记是「高亮开关」：只有 `index.html` 加载了对应 Prism 组件（c/cpp/ebnf/bash/makefile/bison/rust/koopa）的语言才会着色，`asm`、`txt` 等不会被着色。
- 数学公式用行内 `$...$`，由 `docsify-katex.js` + `katex.min.css` 渲染；本仓库不用块级 `$$...$$`，且需避免在代码围栏外裸写 `$$`。
- 站内链接统一用 Docsify 路由风格：`/dir/`（→README）、`/dir/page`（→page.md）、`/dir/page?id=锚点`（→标题锚点），与 `toc.md` 一致。
- 以上四点共同构成「给本仓库写内容时」需要遵守的 Markdown 写作规范。

## 7. 下一步学习建议

- 想理解这些扩展语法「为什么能渲染」，回到 [u2-l1](u2-l1-docsify-config-and-theme.md) 看 `$docsify` 配置与插件加载，回到 [u2-l2](u2-l2-koopa-syntax-highlight.md) 看 `prism-koopa.js` 如何注册一门新语言。
- 想了解这些路由链接在工程上如何被校验「有没有写错/指空」，进入第四单元，特别是 [u4-l3](u4-l3-docsify-routing-and-local-check.md)（Docsify 路由解析与本地链接校验）。
- 想系统了解文档的内容分层，继续阅读 [u3-l1](u3-l1-lab-layering-and-pipeline.md)（实验分层与编译流水线映射）与 [u3-l3](u3-l3-appendix-and-references.md)（附录与参考资料体系）。
