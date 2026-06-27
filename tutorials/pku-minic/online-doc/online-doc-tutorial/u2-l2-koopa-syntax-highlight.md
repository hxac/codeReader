# Koopa IR 语法高亮插件

## 1. 本讲目标

本讲聚焦仓库里一个**只有 30 多行、却让整站「Koopa 代码」变得五彩斑斓**的小文件：`docs/assets/js/prism-koopa.js`。

读完本讲，你应当能够：

- 说清 **Prism** 这类语法高亮库的工作模型：把一段文本切成一个个 **token（词法单元）**，再给每个 token 套上带 class 的标签；
- 读懂 `Prism.languages.koopa` 这个「语法对象」的整体结构（键 = token 类型，值 = 正则或正则数组）；
- 逐条解释 Koopa IR 的 8 类 token（注释 / 字符串 / 标号 / 关键字 / 内建 / 类型 / 变量 / 数字 / 标点）各自匹配什么、为什么这样写；
- 讲清 Docsify 是如何通过 `index.html` 里一行 `<script>` 把这个自定义语言注册进高亮系统的，以及脚本加载顺序为何重要。

本讲承接 [u2-l1](u2-l1-docsify-config-and-theme.md)：上一讲我们俯瞰了 `index.html` 的配置与主题系统，本讲把镜头推进到其中「加载 Prism 语言组件」的那一组 `<script>` 标签上。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么是「语法高亮」？** 你在文档里看到的彩色代码，并不是手写的颜色，而是浏览器在渲染时把代码文本送进一个高亮库，库把文本切成一段段有含义的小片段（关键字、数字、字符串……），再用 CSS 给每一类片段上色。本仓库用的是 [Prism](https://prismjs.com/) 高亮库。

**Prism 的核心抽象是「语言定义（language definition）」。** Prism 内置了常见语言（C、JavaScript、CSS 等）。要让 Prism 认识一门新语言，只需往全局对象 `Prism.languages` 上挂一个以语言名命名的属性即可，例如 `Prism.languages.koopa = { ... }`。挂上去之后，任何 ```koopa 代码块就会被这套规则高亮。

**Koopa IR 是什么？** 这是北大编译实践课程自设计的一套中间表示（Intermediate Representation），形如 LLVM IR 但大幅简化。它用 `@name` 表示具名符号、`%name` 或 `%数字` 表示临时值、`%标号:` 表示基本块标号，指令形如 `%0 = add %1, %2`、`store %0, @x`、`br %cond, %then, %else` 等。因为它是课程专属语言，Prism 不内置，所以仓库需要自己写一份高亮规则——这就是 `prism-koopa.js` 存在的原因。

> 关于 Koopa IR 的完整语法，见 [docs/misc-app-ref/koopa.md](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/misc-app-ref/koopa.md)。本讲只引用其中与高亮规则相关的部分。

## 3. 本讲源码地图

本讲涉及两个文件，一主一辅：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [docs/assets/js/prism-koopa.js](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/prism-koopa.js#L1-L32) | 32 | **核心**：用 30 多行定义 Koopa IR 的全部高亮规则 |
| [docs/index.html](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L47-L65) | - | **辅助**：用一组 `<script>` 标签按顺序加载 Prism 核心与各语言组件（含本地的 `prism-koopa.js`） |

此外会少量引用 Koopa 规范文档来佐证「某条正则对应规范里的哪条规则」。

---

## 4. 核心概念与源码讲解

### 4.1 Prism 语法高亮原理与「语法对象」结构

#### 4.1.1 概念说明

Prism 把「如何识别一门语言」抽象成一个普通 JavaScript 对象。这个对象的**键就是 token 类型名**（如 `keyword`、`number`），**值就是告诉 Prism「这类 token 长什么样」的规则**。规则可以是一个正则、一个正则数组，或一个带附加选项的对象。

为什么要把语言定义写成「键值对」而不是一大坨 `if/else`？因为这样有几个好处：

1. **声明式**：你只描述「什么算关键字」「什么算数字」，扫描顺序由 Prism 统一调度；
2. **可组合**：同一类 token 可以给出多个候选正则（写成数组），按顺序尝试；
3. **可复用样式**：token 类型名会直接变成 CSS class，主题样式表只需对 class 名上色，与具体语言解耦。

这套对象有个专门的名字——**语法对象（grammar object）**，挂到 `Prism.languages.<语言名>` 上即完成注册。

#### 4.1.2 核心流程

Prism 高亮一段代码的流程，可以用三步概括：

1. **取规则**：根据代码块的语言名（如 `koopa`）找到对应的语法对象 `Prism.languages.koopa`。
2. **切词（tokenize）**：从左到右扫描文本，在每个位置依次尝试语法对象里的 token 类型（按对象键的顺序）。Prism 选最早出现的匹配；一旦某段文本被某个 token 吃掉，就不再参与后续匹配。
3. **输出**：把每个 token 包成 `<span class="token <类型名>">…</span>`，连同主题 CSS，浏览器就上色了。

关键推论：**对象里键的顺序≈优先级**。先列出的 token 类型有机会先「吃」到文本。这正是 Koopa 规则里「标号 `label` 必须排在变量 `variable` 前面」的原因（详见 4.2）。

选 earliest match 这件事，可以粗略写成：

\[
\text{pos}(t) \;=\; \min_{\text{type} \in \text{grammar}}\; \text{firstMatchPos}(\text{text},\, r_{\text{type}})
\]

即在当前待处理文本里，取所有 token 类型中最靠左的那个匹配位置 `pos(t)` 作为下一个切分点。

#### 4.1.3 源码精读

打开 `prism-koopa.js`，第一眼就能看到整体骨架——一个赋值语句，把对象挂到 `Prism.languages.koopa`：

挂载语法对象（这一行完成了「注册 koopa 语言」）：

[docs/assets/js/prism-koopa.js:1](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/prism-koopa.js#L1-L1)

```js
Prism.languages.koopa = {
  'comment':   [ … ],          // 值是「正则数组」
  'string':    { … },           // 值是「带选项的对象」
  'label':     { … },
  'keyword':   /\b…\b/,         // 值就是「一个正则」
  'builtin':   /\b…\b/,
  'type':      { … },
  'variable':  { … },
  'number':    /\b\d+\b/,
  'punctuation': /[{}[\](),:*=]/
};
```

这里能看到语法对象值的**三种写法**，全部出现在同一个文件里：

| 写法 | 出现于 | 含义 |
| --- | --- | --- |
| 单个正则 `/\b…\b/` | `keyword`/`builtin`/`number`/`punctuation` | 最简形式，一个正则描述该 token |
| 正则数组 `[ {…}, {…} ]` | `comment` | 同一类 token 有多个候选，按顺序尝试 |
| 对象 `{ pattern, alias, lookbehind, greedy }` | `string`/`label`/`type`/`variable` | 正则之外还要附加上色别名、后视等选项 |

其中两个选项会在 4.2 反复出现，先建立印象：

- `alias`：给这个 token 起一个**样式别名**。例如 `type` 的真实类型名是 `type`，但加了 `alias: 'class-name'` 后，它会借用 Prism 主题里 `class-name` 的配色——这样不用为每种语言单独写一套 CSS。
- `greedy: true`：允许该 token 在「非词首」位置也能被匹配，避免块注释、字符串这类跨片段结构漏高亮（4.2.3 详述）。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是用「三种写法」的分类框架去拆解 `prism-koopa.js`。

1. **实践目标**：不经运行，仅凭阅读把 9 个 token 类型归类。
2. **操作步骤**：
   - 打开 [prism-koopa.js](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/prism-koopa.js#L1-L32)；
   - 逐个判断每个键的值属于「单个正则 / 正则数组 / 带选项对象」中的哪一种；
   - 对带选项对象，记下它用了哪些选项（`alias` / `greedy` / `lookbehind`）。
3. **需要观察的现象**：你会注意到 `comment` 是唯一一个用数组的（因为它要区分普通注释和文档注释）；`alias` 被 `label`、`type` 用到；`greedy` 被 `comment`、`string` 用到。
4. **预期结果**：得到一张「类型 → 写法 → 选项」的小表。参考答案见 4.1.5。
5. 运行结果：待本地验证（纯阅读，无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Prism.languages.koopa = { ... }` 改成 `Prism.languages.Koopa = { ... }`（首字母大写），```koopa 代码块还能高亮吗？

> **答案**：不能。Docsify/Prism 是用代码围栏里**小写**的语言名 `koopa` 去查 `Prism.languages.koopa` 的。键名大小写敏感，改名后查不到对应规则，代码块就会回退为无高亮的纯文本。语言名必须与围栏里写的一致。

**练习 2**：`keyword` 的值是一个裸正则，而 `string` 的值是一个对象。这两种写法在「匹配能力」上有差别吗？

> **答案**：匹配能力本身一样，对象里的 `pattern` 字段也是一个正则。差别在于**附加能力**：对象写法可以挂 `alias`（换配色）、`greedy`（放宽匹配位置）、`lookbehind`（后视）。裸正则写法只能做最朴素的匹配，不能附带这些选项。`keyword` 不需要这些选项，所以用最简写法；`string` 需要 `greedy`，所以用对象写法。

---

### 4.2 Koopa IR 各类 token 的正则解析

#### 4.2.1 概念说明

Koopa IR 是课程专属语言，没有现成高亮规则。本节的目标是：把 `prism-koopa.js` 里 9 类 token 的正则逐条读懂，理解**它为什么这么写**。

先回顾 Koopa IR 文本里会出现哪些「长相」，它们决定了正则该怎么设计：

```koopa
// 这是普通注释，//! 是文档注释
fun @main(): i32 {          // fun=关键字, @main=变量/标号, i32=类型
%entry:                     // %entry 后跟冒号 → 标号(label)
  %0 = call @getint()       // call=关键字, %0=变量, @getint=变量
  %1 = add %0, 1            // add=内建, 1=数字
  ret %1                    // ret=关键字
}
```

对照可见，Koopa 文本里至少有：注释、字符串、基本块标号、语句级关键字（`fun`/`ret`/`call`…）、二元运算内建（`add`/`eq`…）、类型 `i32`、`@/%` 符号引用、整数、以及 `{ } ( ) , : =` 等标点。9 类 token 正好一一对应。

#### 4.2.2 核心流程

理解这些正则前，先掌握 Prism 里影响匹配行为的三个机制，它们解释了正则之外的那些「修饰」：

1. **键的顺序 = 优先级**。`label`（第 16 行）排在 `variable`（第 27 行）之前。二者都能匹配 `%entry` 这种「`@`/`%` 开头的符号」，但只有标号后跟冒号。让 `label` 先尝试，就能把「带冒号的符号」优先识别成标号，其余的留给 `variable`。
2. **`greedy: true` 放宽匹配起点**。Prism 默认只在一个「干净边界」（字符串开头或上一个 token 之后）尝试匹配。开了 `greedy` 后，即使匹配起点落在已被切分文本的中间也允许。块注释 `/* … */`、字符串 `"…"` 往往不在词首，所以需要 `greedy`。
3. **`lookbehind` 排除前缀字符**。`label` 的正则会先把「标号前面的那个字符」一起捕获进分组，再用 `lookbehind: true` 告诉 Prism「这个分组只是用来判断上下文的，别把它算进高亮内容」。这是 Prism 早期（不支持原生后视断言时）实现「往前看一个字符」的标准手法。

此外还有一个贯穿全局的小技巧：**注释和字符串必须排在最前面**，这样注释/字符串内部的文本（哪怕长得像关键字、数字）也不会被后续规则二次切分。

#### 4.2.3 源码精读

下面逐条讲解。每条都给出在文件中的位置与对应的 Koopa 规范出处。

**(a) 注释 `comment`（普通 + 文档，两类用数组区分）**

[docs/assets/js/prism-koopa.js:2-11](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/prism-koopa.js#L2-L11)

```js
'comment': [
  { pattern: /\/\/!.*|\/\*![\s\S]*?!\*\//, alias: 'doc-comment' },
  { pattern: /\/\/.*|\/\*[\s\S]*?\*\//,    greedy: true }
]
```

- 数组第 1 项匹配**文档注释** `//!...`（行）和 `/*! ... !*/`（块），并打上别名 `doc-comment` 以便单独上色；
- 第 2 项匹配**普通注释** `//...` 与 `/* ... */`，开启 `greedy`。
- 两点细节：`[\s\S]` 表示「任意字符含换行」（`.` 默认不匹配换行，块注释可能跨行，故用 `[\s\S]`）；`*?` 是**非贪婪**量词，确保块注释在遇到的第一个 `*/`（或 `!*/`）就闭合，不会「贪」过头吞掉后面的代码。
- 因为文档注释项排在数组前面，`//!` 会被它优先吃掉，不会退化成普通注释。

**(b) 字符串 `string`**

[docs/assets/js/prism-koopa.js:12-15](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/prism-koopa.js#L12-L15)

```js
'string': { pattern: /"[^"]*"/, greedy: true }
```

- 匹配一对双引号及其内部「非双引号」内容 `"[^"]*"`。
- 开 `greedy` 是因为字符串可能出现在表达式中间，不在词首边界。

**(c) 标号 `label`（本文件最精巧的一条）**

[docs/assets/js/prism-koopa.js:16-20](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/prism-koopa.js#L16-L20)

```js
'label': {
  pattern: /((?:^|[^\w@%]))(?:@|%)(?:[a-zA-Z_][a-zA-Z0-9_]*|\d+)(?=.*:)/,
  lookbehind: true,
  alias: 'function'
}
```

这条要把「基本块标号 `%entry:`」从普通符号引用里挑出来。把它拆开看：

\[
\underbrace{((?:^|[^\w@%]))}_{\text{分组1：前导字符}}
\underbrace{(?:@|%)}_{\text{sigil}}
\underbrace{(?:[a-zA-Z\_][\w]*|\d+)}_{\text{名字}}
\underbrace{(?=.*:)}_{\text{前瞻：同行有冒号}}
\]

- 分组 1 `(?:^|[^\w@%])`：标号前面必须是「行首」或「一个非字母数字、非 `@`、非 `%` 的字符」（比如空格）。它被捕获但不参与上色（配合 `lookbehind: true`）。
- `(?:@|%)`：符号前缀，具名 `@` 或临时 `%`。
- 名字：标识符 `[a-zA-Z_][a-zA-Z0-9_]*` 或纯数字 `\d+`（Koopa 允许 `%0` 这种数字临时值，见规范「符号名称」一节）。
- `(?=.*:)`：**前瞻断言**，要求这一行后面某处有个 `:`——这正是「这是标号定义」的标志（`%entry:`）。
- `alias: 'function'`：标号借用「函数名」的配色，在视觉上更显眼。
- 为什么必须排在 `variable` 前面？因为 `%entry` 也满足 `variable` 的正则；靠「同行有冒号」这条前瞻，`label` 抢先认领标号，剩下的普通符号引用才轮到 `variable`。

**(d) 关键字 `keyword`（语句级指令）**

[docs/assets/js/prism-koopa.js:21](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/prism-koopa.js#L21-L21)

```js
'keyword': /\b(?:alloc|load|store|getptr|getelemptr|br|jump|ret|call|fun|decl|global|zeroinit|undef)\b/
```

- 用 `|` 列出所有语句级关键字：内存分配 `alloc`、访存 `load`/`store`、指针运算 `getptr`/`getelemptr`、控制流 `br`/`jump`/`ret`、函数 `call`/`fun`/`decl`、全局 `global`、初值 `zeroinit`/`undef`。这些都能在 [koopa.md 的 EBNF](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/misc-app-ref/koopa.md#L60-L83) 里找到出处。
- 两端的 `\b` 是**单词边界**：防止把 `call` 匹配进 `@callback` 这种更长的标识符里——`\bcall\b` 只匹配完整的 `call`。

**(e) 内建 `builtin`（二元运算符）**

[docs/assets/js/prism-koopa.js:22](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/prism-koopa.js#L22-L22)

```js
'builtin': /\b(?:ne|eq|gt|lt|ge|le|add|sub|mul|div|mod|and|or|xor|shl|shr|sar)\b/
```

- 这些是 Koopa 的**二元运算**：比较 `ne/eq/gt/lt/ge/le` 与算术/位运算 `add/sub/mul/div/mod/and/or/xor/shl/shr/sar`。与规范「二元运算」一节列出的 17 个操作完全一致（[koopa.md:192](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/misc-app-ref/koopa.md#L192-L192)）。
- 同样用 `\b` 防止子串误匹配（如 `add` 误中 `padd`）。
- 为何与 `keyword` 分成两类？因为它们在 Koopa 语法里处于「二元表达式操作符」位置，语义上不同，分开便于给运算符配不同配色。

**(f) 类型 `type`**

[docs/assets/js/prism-koopa.js:23-26](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/prism-koopa.js#L23-L26)

```js
'type': { pattern: /\bi32\b/, alias: 'class-name' }
```

- 只匹配 `i32`（Koopa 的 32 位有符号整数类型）。虽然 Koopa 还有数组/指针/函数类型，但那些由标点 `[` `]` `*` `(` `)` 拼成，本身不含独立关键字，所以这里只需高亮 `i32`。
- `alias: 'class-name'` 让类型名借用「类名」配色。

**(g) 变量 `variable`（符号引用）**

[docs/assets/js/prism-koopa.js:27-29](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/prism-koopa.js#L27-L29)

```js
'variable': { pattern: /(?:@|%)(?:[a-zA-Z_][a-zA-Z0-9_]*|\d+)/ }
```

- 匹配 `@具名符号` 与 `%临时符号`（含 `%数字`），即 Koopa 规范「符号名称」一节定义的两类符号引用。
- 注意它**没有** `label` 那条 `(?=.*:)` 前瞻，因此它兜底接收所有「没被标号认领」的符号。

**(h) 数字 `number` 与 (i) 标点 `punctuation`**

[docs/assets/js/prism-koopa.js:30-31](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/prism-koopa.js#L30-L31)

```js
'number': /\b\d+\b/,
'punctuation': /[{}[\](),:*=]/
```

- `number`：整数常量，如 `1`、`233`。
- `punctuation`：结构标点——花括号 `{ }`、方括号 `[ ]`（数组类型/初值表）、圆括号 `( )`（函数类型）、逗号 `,`、冒号 `:`（标号分隔）、等号 `=`（赋值）、星号 `*`（指针类型）。其中 `:` 正好配合 `label`：标号名被 `label` 吃掉后，剩下的冒号由 `punctuation` 上色。

> **小结**：9 类 token 的排列顺序不是随意的——注释、字符串打头（防止内部被二次切分），`label` 压住 `variable`（靠同行冒号区分标号与普通符号），关键字、内建、类型、数字、标点各管一摊互不重叠。

#### 4.2.4 代码实践

这是本讲的主实践：**亲手给 `builtin`/`keyword` 列表加一个词，验证它确实驱动了高亮**。

1. **实践目标**：用「增删一个词 → 观察某段代码着色变化」的因果链，证明 `prism-koopa.js` 就是 ```koopa 高亮的规则源头。
2. **操作步骤**：
   - 启动本地站点（见 [u1-l2](u1-l2-run-locally.md)）：`docsify serve docs`；
   - 任选一个含 ```koopa 代码块的页面（如 `/misc-app-ref/koopa`），刷新确认 `add` 当前是「内建色」；
   - 编辑 [docs/assets/js/prism-koopa.js:22](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/prism-koopa.js#L22-L22)，在 `builtin` 的列表里追加一个新词，例如在 `sar` 后面加 `|myadd`：

     ```js
     'builtin': /\b(?:ne|eq|gt|lt|ge|le|add|sub|mul|div|mod|and|or|xor|shl|shr|sar|myadd)\b/
     ```

   - 在某个测试页写一段 ```koopa 代码，里面包含 `myadd`：

     ````markdown
     ```koopa
     %2 = myadd %0, %1
     ```
     ````
   - **硬刷新浏览器**（Ctrl/Cmd+Shift+R），因为浏览器会缓存 JS。
3. **需要观察的现象**：刷新后，`myadd` 这三个字母应从「普通文本色」变成与 `add` 一致的「内建色」，证明它被新加入的规则命中。
4. **预期结果**：`myadd` 被高亮为 builtin；其他原有高亮不受影响。
5. **收尾**：这是本地实验，结束后务必还原，避免污染仓库：

   ```bash
   git checkout -- docs/assets/js/prism-koopa.js
   ```

   （注意：仓库有链接检查 CI，但本实验只改 JS、不动 Markdown 链接，不影响 `check_links.py`。）
6. 运行结果：待本地验证（具体配色取决于当前主题）。

#### 4.2.5 小练习与答案

**练习 1**：把 `keyword` 正则两端的 `\b` 去掉，写成 `/(?:alloc|...|undef)/`，会出什么问题？

> **答案**：会出现子串误匹配。比如代码里的标识符 `@alloc_helper` 会把其中的 `alloc` 部分高亮成关键字，因为它不再要求「单词边界」。`\b` 确保 `alloc` 只作为一个完整单词被命中。`builtin`、`number` 同理依赖 `\b`。

**练习 2**：`label` 的前瞻 `(?=.*:)` 里，`.` 默认不跨行。这意味着「标号定义」必须在什么条件下才会被高亮成 label？

> **答案**：冒号 `:` 必须与 `@名字`/`%名字` 出现在**同一行**。Koopa 的基本块标号写作 `%entry:`，名字与冒号同行，所以能命中。若有人把冒号另起一行写（非标准写法），前瞻 `.*:` 在本行找不到冒号，就会回退成普通 `variable`。这提醒我们：高亮规则与语言的「行内书写约定」绑定。

**练习 3**：为什么 `comment` 和 `string` 要排在语法对象的最前面？

> **答案**：为了避免注释/字符串**内部**的文本被后续规则二次切分。比如注释里写了 `ret`，若 `keyword` 先于 `comment` 匹配，`ret` 就会被错误地高亮成关键字。把它们排最前，整段注释/字符串先被整体吃掉，内部文本不再参与后续匹配。

---

### 4.3 Docsify 如何加载并注册自定义高亮语言

#### 4.3.1 概念说明

光有 `prism-koopa.js` 还不够，浏览器得**真正执行**它，`Prism.languages.koopa` 才存在。这个「执行」由 `index.html` 里的一行 `<script>` 完成。

这里有个容易混淆的点（[u1-l2](u1-l2-run-locally.md) 已澄清）：**Docsify ≠ docsify-cli**。docsify-cli 只是本地静态服务器；真正把 Markdown 渲染成网页、并内置代码高亮的，是 `index.html` 通过 CDN 加载的 `docsify` 库。而 `docsify` 库**内部捆绑了 Prism 核心**（`Prism` 全局对象就来自它），所以我们只需再额外加载「各语言组件」即可，无需单独引入 Prism 核心。

#### 4.3.2 核心流程

加载顺序是本节重点。`index.html` 底部的 `<script>` 按出现顺序**同步**执行（这些 `<script>` 都没有 `async`/`defer`，除了 giscus 那条），因此顺序即执行顺序：

1. 先加载 `docsify.min.js`（含 Prism 核心）→ `window.Prism` 存在；
2. 再依次加载各 Prism 语言组件（C、C++、EBNF、Bash、Makefile、Bison、Rust）；
3. 最后加载本地的 `prism-koopa.js` → 此时 `Prism.languages.koopa` 被定义；
4. 之后才加载 footer、pagination、katex 等其它插件。

关键约束：**`prism-koopa.js` 必须在 Prism 核心（即 `docsify.min.js`）之后加载**。若顺序反了，`prism-koopa.js` 执行时 `Prism` 还未定义，`Prism.languages.koopa = {...}` 会抛 `ReferenceError`，koopa 高亮就彻底失效。

#### 4.3.3 源码精读

看 `index.html` 里这一连串 `<script>`：

Prism 核心随 docsify 主库加载（这一行让 `window.Prism` 可用）：

[docs/index.html:47](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L47-L47)

```html
<script src="//npm.elemecdn.com/docsify/lib/docsify.min.js"></script>
```

随后是一组从 CDN 加载的 Prism 语言组件（C/C++/EBNF/Bash/Makefile/Bison/Rust）：

[docs/index.html:52-58](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L52-L58)

```html
<script src="//npm.elemecdn.com/prismjs@1/components/prism-c.min.js"></script>
<script src="//npm.elemecdn.com/prismjs@1/components/prism-cpp.min.js"></script>
<script src="//npm.elemecdn.com/prismjs@1/components/prism-ebnf.min.js"></script>
<script src="//npm.elemecdn.com/prismjs@1/components/prism-bash.min.js"></script>
<script src="//npm.elemecdn.com/prismjs@1/components/prism-makefile.min.js"></script>
<script src="//npm.elemecdn.com/prismjs@1/components/prism-bison.min.js"></script>
<script src="//npm.elemecdn.com/prismjs@1/components/prism-rust.min.js"></script>
```

紧接着就是本讲的主角——本地组件，注册 koopa 语言（注意它与上面 CDN 组件的写法区别：用相对路径 `assets/js/...` 而非 CDN）：

[docs/index.html:59](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/index.html#L59-L59)

```html
<script src="assets/js/prism-koopa.js"></script>
```

注意三点：

1. **本地 vs CDN**：其它语言组件走 `//npm.elemecdn.com/...` CDN，而 koopa 是仓库自研，所以用相对路径 `assets/js/prism-koopa.js` 加载（[u1-l3](u1-l3-directory-structure.md) 提过 `assets/` 收纳全站共享 JS/CSS）。
2. **顺序正确**：本行在第 47 行 `docsify.min.js`（含 Prism 核心）之后，满足「核心先于组件」的约束。
3. **无显式「注册」调用**：没有任何 `docsify.use(...)` 之类的语句。注册是**隐式**的——`prism-koopa.js` 自己执行 `Prism.languages.koopa = {...}`，Docsify 渲染 ```koopa 代码块时，Prism 自然能查到这条规则。

#### 4.3.4 代码实践

这是一个**运行验证型实践**，目标是亲眼看一次「Prism 把 Koopa 代码切成带 class 的 `<span>`」。

1. **实践目标**：确认 `prism-koopa.js` 已生效，并从 DOM 层面理解高亮产物。
2. **操作步骤**：
   - 启动站点 `docsify serve docs`，打开 `/misc-app-ref/koopa`；
   - 找到一段 ```koopa 代码块（如规范里的 `%0 = load @i`）；
   - 右键「检查元素」，定位到该代码块内部的某个关键字（如 `load`）对应的元素。
3. **需要观察的现象**：你会看到 `load` 被包成类似 `<span class="token keyword">load</span>`；`%0` 是 `<span class="token variable">%0</span>`；`@i` 同为 variable；`=` 是 `<span class="token punctuation">=</span>`。
4. **预期结果**：每个 token 的 class 名与 `prism-koopa.js` 里的键名一一对应，配色由主题 CSS 决定。
5. 运行结果：待本地验证（具体 class 名与版本有关，但 `token <类型名>` 的模式稳定）。

#### 4.3.5 小练习与答案

**练习 1**：如果把第 59 行 `<script src="assets/js/prism-koopa.js"></script>` 整行删掉，站点会发生什么？

> **答案**：所有 ```koopa 代码块将失去彩色高亮，回退为单色等宽文本。因为 `Prism.languages.koopa` 从未被定义，Prism 找不到该语言规则就不上色。其余语言（C/Rust 等）不受影响，因为它们的组件仍在。

**练习 2**：把第 59 行挪到第 47 行（`docsify.min.js`）**之前**，会发生什么？

> **答案**：`prism-koopa.js` 执行时 `window.Prism` 尚不存在（Prism 核心还没加载），`Prism.languages.koopa = {...}` 会抛 `ReferenceError: Prism is not defined`，koopa 高亮失效，且控制台报错。这印证了「核心必须先于语言组件加载」的顺序约束。

**练习 3**：本仓库为什么用本地相对路径加载 `prism-koopa.js`，而用 CDN 加载 `prism-c.min.js` 等？

> **答案**：因为 C、C++、EBNF 等是 Prism 官方提供的通用语言组件，可直接从 CDN 取；而 Koopa IR 是课程自研语言，Prism 没有官方组件，必须由仓库自己维护 `docs/assets/js/prism-koopa.js`，因此用相对路径加载本地文件。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个小任务：**为 Koopa IR 的「函数类型」补一组独立的高亮 token**。

背景：目前 `i32` 被高亮为 `type`，但 Koopa 的指针类型 `*i32`、数组类型 `[i32, 2]` 里的 `*` 和 `[ ]` 只被当作普通标点。本任务尝试让「类型上下文」更醒目，并体会「加一类 token」的完整流程。

1. **阅读规范**：打开 [koopa.md 的「类型」一节](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/misc-app-ref/koopa.md#L16-L36)，确认 Koopa 有哪些类型写法（`i32`、`*T`、`[T, N]`、函数类型 `(T,…): T`）。
2. **设计规则**：在 [prism-koopa.js](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/prism-koopa.js#L1-L32) 的 `type` 规则里，尝试把指针星号也纳入类型高亮，例如把 `type` 改成数组形式，增加一条匹配 `\*i32` 的正则并给个 `alias`。思考：这条新规则应该放在 `punctuation` 之前还是之后？为什么？
3. **加载验证**：确认第 59 行 `<script>` 仍在 `docsify.min.js` 之后；启动站点，硬刷新查看 `alloc *i32` 这类代码的着色变化。
4. **DOM 验证**：用浏览器检查元素，确认新 token 的 `<span class="token …">` 符合预期。
5. **收尾**：`git checkout -- docs/assets/js/prism-koopa.js` 还原。

> 这个任务同时用到三个模块的知识：4.1 的「语法对象结构」（如何再加一类 token）、4.2 的「正则与顺序」（新规则与 `punctuation`/`type` 的优先级关系）、4.3 的「加载与验证」（确认脚本顺序、用 DOM 验证产物）。

## 6. 本讲小结

- Prism 用一个**语法对象**描述一门语言：键是 token 类型，值可以是单个正则、正则数组或带选项（`alias`/`greedy`/`lookbehind`）的对象。
- `prism-koopa.js` 把这个对象挂到 `Prism.languages.koopa`，即完成了 Koopa IR 语言的注册。
- 9 类 token 的顺序有讲究：注释/字符串打头防二次切分；`label` 靠同行冒号前瞻 `(?=.*:)` 压住 `variable`，区分「标号」与「普通符号」；`\b` 防子串误匹配。
- 块注释/字符串用 `[\s\S]*?` 非贪婪跨行匹配，并开 `greedy: true` 以放宽匹配起点。
- Docsify 主库内置 Prism 核心；`prism-koopa.js` 作为本地组件由 `index.html` 第 59 行加载，**必须排在含 Prism 核心的 `docsify.min.js`（第 47 行）之后**。
- 注册是隐式的——无需 `docsify.use(...)`，只要脚本执行了 `Prism.languages.koopa = {...}`，```koopa 代码块就会被这套规则高亮。

## 7. 下一步学习建议

- **横向对比**：阅读同目录的 [docs/assets/js/sidebar.js](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/sidebar.js) 与 [giscus.js](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/assets/js/giscus.js)，它们是「Docsify 插件」的另一类扩展方式（通过 `window.$docsify.plugins` 钩子），与本讲的「Prism 语言组件」形成对照——这正是下一讲 [u2-l3 侧边栏同步与评论插件](u2-l3-sidebar-and-comments.md) 的内容。
- **深入 Prism**：若想理解 `greedy`、`lookbehind` 的精确语义与 tokenize 算法，可阅读 Prism 官方文档的「Extending Prism」与语言定义说明。
- **回到内容**：高亮规则与 Koopa 语法一一对应，建议结合 [u3-l1 实验分层与编译流水线映射](u3-l1-lab-layering-and-pipeline.md) 理解这些指令在编译器前后端中的角色。
