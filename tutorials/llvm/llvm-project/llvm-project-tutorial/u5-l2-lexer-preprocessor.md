# 词法分析 Lex 与预处理

## 1. 本讲目标

承接 u5-l1：我们已经知道 `clang` 命令由外层 **Driver** 编排、最终交给内层 **cc1** 真正编译。那么 cc1 拿到一个 `.c` 源文件后，第一件事是什么？是把「一段连续的字符文本」变成「一个一个带类型的记号（Token）」，并对 `#define`、`#include`、`#if` 这些以 `#` 开头的预处理指令做处理。这一步由 **Lexer（词法分析器）** 与 **Preprocessor（预处理器）** 协作完成，它们的产物是一条 Token 流，喂给后续的 Parser（语法分析器，见 u5-l3）。

学完本讲，你应该能够：

- 说清 **Token** 的内存表示，以及 Lexer 如何用一个「按首字符分发」的大 `switch` 把字符流切成 Token；
- 理解 **Lexer 与 Preprocessor 的分工**：Lexer 只负责「字面切分」，不认识宏、不认识 `#` 指令；指令处理与宏展开都在 Preprocessor 里；
- 描述 Preprocessor 的「**主循环**」如何通过一个函数指针在多个 Token 来源（源文件 / 宏展开体 / 缓存）之间切换；
- 用源码定位 `#define`/`#include`/`#if` 的处理入口，以及宏展开的「**Token 再注入**」机制；
- 用 `clang -E` 等开关亲自观察预处理结果，对照源码理解 Token 流的生成过程。

## 2. 前置知识

### 2.1 翻译阶段（Translation Phases）

C/C++ 标准把「源文件 → 可执行程序」抽象成若干个**翻译阶段**。Clang 并没有逐一对应实现，但你可以用下面这张简化表建立直觉，本讲只涉及前三到四个阶段：

| 阶段 | 标准要求 | Clang 中的承担者 |
|------|----------|------------------|
| 1 | 把源文件的字节映射成「源字符集」，处理三字符组（trigraph） | Lexer 的字符读取（`getCharAndSize`） |
| 2 | 行续接：把行尾的反斜杠+换行「粘」成一行的「物理行」 | Lexer 的字符读取（escaped newline 折叠） |
| 3 | 把字符切成**预处理单词（preprocessing token）** | Lexer 的 `LexTokenInternal` |
| 4 | 执行预处理：宏展开、`#include`、条件编译 | Preprocessor |
| 5+ | 拼接相邻字符串、把预处理单词转成正式单词、语法/语义分析…… | Parser / Sema（后续讲义） |

关键结论：**阶段 1–3 由 Lexer 完成，阶段 4 由 Preprocessor 完成**。Lexer 的输出是「预处理单词」而不是最终的「单词」，二者概念不同（预处理阶段还没有真正区分整数字面量的进制、还没做名字查找等）。这一点在 `LexTokenInternal` 的源码注释里写得很清楚。

### 2.2 三个核心概念

- **Token（单词/记号）**：源码被切分后的最小单位，比如 `int`、`x`、`42`、`+`、`;`。每个 Token 带有自己的种类、在源码中的位置、长度和一些标志位。
- **Lexer（词法分析器）**：只做「字面切分」的状态机，从字符缓冲区向前扫描产出 Token，不解释 Token 的含义。
- **Preprocessor（预处理器）**：在 Lexer 之上负责「解释」Token 流——识别并执行 `#` 指令、展开宏、处理头文件包含。

一个容易混淆的点：**Lexer 内部持有一个指向 Preprocessor 的回指针**，但它只在两个特殊时刻回调 Preprocessor（遇到行首的 `#`、遇到需要特殊处理的标识符）。这并不是「职责倒挂」，而是为了让 Preprocessor 能在「第一时间」接管指令与宏，而不必先把整行 Token 全切出来。这一点是本讲反复出现的核心设计。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `clang/lib/Lex/` 与 `clang/include/clang/Lex/` 下。注意：**Preprocessor 的实现并不是一个文件**，而是一组以 `PP` 开头的文件，按职责拆分。

| 文件 | 作用 |
|------|------|
| `clang/include/clang/Lex/Token.h` | `Token` 结构体的定义：种类、位置、长度、标志位 |
| `clang/lib/Lex/Lexer.cpp` | Lexer 的核心：`Lex`、`LexTokenInternal`（大 switch）、各种 `LexXxx` |
| `clang/include/clang/Lex/Lexer.h` | Lexer 类声明，含字符读取接口与两阶段说明 |
| `clang/lib/Lex/Preprocessor.cpp` | Preprocessor 主循环 `Lex`、`HandleIdentifier` |
| `clang/include/clang/Lex/Preprocessor.h` | Preprocessor 类声明，含 `CurLexerCallback` 调度指针 |
| `clang/lib/Lex/PPDirectives.cpp` | 预处理指令处理：`HandleDirective`、`#define`/`#include`/`#if` |
| `clang/lib/Lex/PPMacroExpansion.cpp` | 宏展开：`HandleMacroExpandedIdentifier` |
| `clang/lib/Lex/PPLexerChange.cpp` | 文件包含带来的 Lexer 栈切换：`EnterSourceFile` |
| `clang/lib/Lex/HeaderSearch.cpp` | 头文件搜索路径：`LookupFile` 解析 `#include` 的文件名 |
| `clang/include/clang/Lex/MacroInfo.h` | `MacroInfo`：一个 `#define` 的内存表示 |

> 提示：Preprocessor「一个类、多个 cpp」的拆分方式（PPDirectives / PPMacroExpansion / PPLexerChange / PPExpressions / Pragma …）是 Clang 控制单文件体积的常见手法。读源码时按「想看哪个机制」去对应文件即可。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：4.1 讲 Lexer 如何切 Token；4.2 讲 Preprocessor 的调度中枢；4.3 讲 `#define`/`#include`/`#if` 三类指令；4.4 讲宏展开的「Token 再注入」机制。

### 4.1 Token 与 Lexer：源码如何切分成 Token 流

#### 4.1.1 概念说明

词法分析的目标，是把像 `int x = 42;` 这样的字符序列切成 5 个 Token：`int`、`x`、`=`、`42`、`;`。注意几点：

- Token **不关心语义**：`int` 此时只是一个「标识符类」的预处理单词，到底是不是 C 关键字、`x` 是不是宏，这一阶段不判定。
- 每个 Token 要记住自己在**源码中的位置**（`SourceLocation`），这样后续报错、调试信息才能指回源码。
- Lexer 是一个**只向前扫描**的状态机：它不缓存已切出的 Token，也不回退，靠一个游标指针 `BufferPtr` 记录「下一个待读字符」。

`Token` 结构体的设计哲学是「**信息优先于空间**」：宁可字段多一点、占空间大，也要尽量在一次切词中返回尽可能多信息（注释原话）。我们重点看它的几个字段。

#### 4.1.2 核心流程

Lexer 切一个 Token 的流程可以概括为：

```text
Lexer::Lex(Result)
  └─ startToken()             // 清空 Result
  └─ 设置空白/行首标志位        // 把 Lexer 自己的 IsAtStartOfLine 等状态搬到 Result 上
  └─ LexTokenInternal(Result) // 真正的切词
       1. 跳过空白与注释
       2. 读第一个字符 Char = getAndAdvanceChar(...)
       3. switch (Char):
            数字      → LexNumericConstant
            字母/_    → LexIdentifierContinue   （标识符/关键字）
            引号      → LexStringLiteral / LexCharConstant
            标点      → 直接判定种类（多为 1~3 字符的多字符标点）
            行首的 #  → 跳转 HandleDirective（交给 Preprocessor，见 4.3）
       4. FormTokenWithChars(...)  // 用 [起点, 终点) 区间落定 Token 的位置和长度
       5. 返回 true（表示产出了一个 Token）
```

两个细节值得记住：

1. **阶段 1/2 与阶段 3 是「交织」在字符读取里的**。Lexer 不先把三字符组、行续接预处理成一遍新文本，而是在「读一个字符」时，由 `getCharAndSize` 家族顺手把 `\`+换行、三字符组折叠掉。所以 `getCharAndSize` 返回的不只是字符，还返回了它在源文件里占的**字节数**（Size）——因为折叠后一个「逻辑字符」可能对应源文件里好几个字节。
2. **缓冲区末尾必须有一个 `\0`**。Lexer 大量依赖「读到 `\0` 就是文件结束」这个约定，`InitLexer` 一进来就断言这一点。

#### 4.1.3 源码精读

先看 `Token` 的字段定义。它用一个联合风格的 `PtrData` 字段按 Token 种类装不同的东西：标识符装 `IdentifierInfo*`，字面量装源码缓冲区里的指针，注解 token（parser 解析后再回填的高级 token）装语义数据。

[clang/include/clang/Lex/Token.h:36-97](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Lex/Token.h#L36-L97) —— `Token` 类的定义与 `TokenFlags` 枚举。重点字段：

- `Loc`：Token 在源码中的位置（`SourceLocation` 的原始编码）。
- `UintData`：普通 token 时存「长度」，注解 token 时存 `SourceRange` 的终点。
- `PtrData`：上面说的联合指针。
- `Kind`：`tok::TokenKind`，决定它是 `tok::numeric_constant`、`tok::identifier`、`tok::plus` 还是别的。
- `Flags`：`StartOfLine`（行首）、`LeadingSpace`（前面有空白）、`NeedsCleaning`（含三字符组/转义换行）等。

`Token::startToken()` 把这些字段清零，是每个新 Token 的起点：

[clang/include/clang/Lex/Token.h:187-193](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Lex/Token.h#L187-L193) —— 每次切词前先 `startToken()` 把上一次的内容清掉。

再看 Lexer 的「对外入口」`Lex`。它本身很薄，主要做两件事：把 Lexer 自己积累的空白/行首状态搬到 `Result` 的标志位上，然后调用 `LexTokenInternal`。

[clang/lib/Lex/Lexer.cpp:3810-3843](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/Lexer.cpp#L3810-L3843) —— `Lexer::Lex`：先 `startToken`，再搬运 `IsAtStartOfLine`/`HasLeadingSpace` 等标志，最后调 `LexTokenInternal`。

`LexTokenInternal` 是整条流水线上最「热」的代码（注释里直说它 extremely performance critical）。它先快速跳过空白，再用一个大 `switch` 按首字符分发：

[clang/lib/Lex/Lexer.cpp:3850-3888](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/Lexer.cpp#L3850-L3888) —— 缓存 `BufferPtr` 到局部变量 `CurPtr`，快速跳过水平空白，再 `getAndAdvanceChar` 读第一个字符准备进入 `switch`。

数字和标识符两个分支最能体现「按首字符分发到专门函数」的模式：

[clang/lib/Lex/Lexer.cpp:3989-3993](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/Lexer.cpp#L3989-L3993) —— 首字符是 `0`-`9`，进入 `LexNumericConstant`（顺便通知「多次包含优化」状态机读到了真实 token）。

[clang/lib/Lex/Lexer.cpp:4126-4138](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/Lexer.cpp#L4126-L4138) —— 首字符是字母或 `_`，进入 `LexIdentifierContinue`。注意 `L`/`u`/`U`/`R` 在此之前各有专门分支，因为它们可能既是标识符首字母、又是宽/Unicode 字符串字面量的前缀。

标识符的切分与「升级」逻辑在 `LexIdentifierContinue` 里，它揭示了 Token 是如何从「生标识符」变成「带 IdentifierInfo 的标识符/关键字」的：

[clang/lib/Lex/Lexer.cpp:2034-2112](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/Lexer.cpp#L2034-L2112) —— 先把连续的标识符字符吃掉，用 `raw_identifier` 落定；若处于 raw 模式就直接返回（不查表）；否则调 `PP->LookUpIdentifierInfo(Result)` 给 Token 装上 `IdentifierInfo` 并把种类升级为 `identifier` 或关键字，最后在「需要特殊处理」时回调 `PP->HandleIdentifier(Result)`（这是宏展开的入口，见 4.4）。

最后是「落定 Token」的辅助函数 `FormTokenWithChars`，它把 `[BufferPtr, TokEnd)` 这段区间写进 Token 的位置和长度，并把游标 `BufferPtr` 推进到 `TokEnd`：

[clang/include/clang/Lex/Lexer.h:644-656](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Lex/Lexer.h#L644-L656) —— 内联的 `FormTokenWithChars`，是几乎所有 `LexXxx` 收尾时共用的工具。

补充：关于「阶段 1/2 在字符读取中完成」，最权威的说明就在 `Lexer.h` 里那段关于两套字符读取接口的注释：

[clang/include/clang/Lex/Lexer.h:664-679](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Lex/Lexer.h#L664-L679) —— 解释 `getAndAdvanceChar` 与 `getCharAndSize`/`ConsumeChar` 两套接口，并明说它们「自动提供阶段 1/2 翻译」。

#### 4.1.4 代码实践

**目标**：用 `clang` 的词法转储开关，亲眼看到一个源文件被切成了哪些 Token，并对照本节的「按首字符分发」理解。

**操作步骤**：

1. 准备一个最小文件 `t.c`：

   ```c
   int x = 42 + 1;
   ```

2. 用 `clang` 的 `-dump-raw-tokens`（在 raw 模式下切词，不做名字查找、不展开宏）观察原始 Token：

   ```bash
   clang -cc1 -dump-raw-tokens t.c
   ```

3. 再用 `-dump-tokens`（走完整预处理后再 dump）对比：

   ```bash
   clang -cc1 -dump-tokens t.c
   ```

**需要观察的现象**：

- `-dump-raw-tokens` 会逐行打印每个 Token 的种类、拼写、所在行列，以及是否在行首（`StartOfLine`）、前面是否有空白（`LeadingSpace`）等标志。
- 你能看到 `int` `x` `=` `42` `+` `1` `;` 各自的 `tok::identifier` / `tok::numeric_constant` / `tok::equal` / `tok::plus` / `tok::semi` 种类。
- `int` 在 raw 模式下是 `raw_identifier`，在 `-dump-tokens` 模式下会变成关键字 `int`——对应 `LexIdentifierContinue` 里「raw 模式直接返回」与「`LookUpIdentifierInfo` 升级种类」两条路径的差别。

**预期结果**：你能从输出里辨认出每个 Token 的种类与位置信息，并解释为什么 raw 模式下关键字被当成普通标识符。

**注意**：`-dump-tokens`/`-dump-raw-tokens` 是 cc1 层开关（要用 `-cc1` 走内层驱动，见 u5-l1）。不同版本输出格式略有差异；若开关不可用，可改用 `-Xclang -dump-tokens`。本地具体输出形式**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Lexer 要求输入缓冲区末尾必须是 `\0`？

> **参考答案**：`LexTokenInternal` 在读到 `\0` 时判定文件结束（`case 0`），并把游标与 `BufferEnd` 比较来区分「真正的 EOF」和「源文件中间出现的非法空字节」。`InitLexer` 还显式断言 `BufEnd[0] == 0`。这个约定让「是否到文件尾」可以用一个简单的字符比较完成，避免每次都额外检查游标越界，是对热路径的优化。

**练习 2**：`tok::raw_identifier` 与 `tok::identifier` 有什么区别？谁在什么时候把前者变成后者？

> **参考答案**：`raw_identifier` 是「还没在标识符表里查过」的生标识符，只带源码指针；`identifier` 是查过表、装上了 `IdentifierInfo` 的标识符（或被进一步升级为关键字）。在 `LexIdentifierContinue` 里，raw 模式直接返回 `raw_identifier`，否则调用 `PP->LookUpIdentifierInfo(Result)` 把它升级。这样跳过区域（如 `#if 0` 块）可以用 raw 模式快速略过，不必白白查表。

### 4.2 Preprocessor 调度中枢：Token 的来源与主循环

#### 4.2.1 概念说明

上一节我们看到 Lexer 能从源文件切出 Token。但真正交给 Parser 的 Token 流，**并不只来自当前源文件**——它至少有三种来源：

1. **当前源文件的 Lexer**（`CurLexer`）：正常的源码切词；
2. **宏展开体的 TokenLexer**（`CurTokenLexer`）：宏被展开后，其替换体是一串预先切好的 Token，由一个 `TokenLexer` 回放（见 4.4）；
3. **缓存的前瞻 Token**（`CachingLex`）：为了做宏展开时的向前看（lookahead），Preprocessor 会先把若干 Token 缓存起来。

Preprocessor 的核心职责，就是在这几种来源之间**调度**，对上层（Parser）呈现成一条统一的 Token 流。它通过一个**函数指针** `CurLexerCallback` 来决定「下一个 Token 该问谁要」。

#### 4.2.2 核心流程

Preprocessor 的主循环非常短：

```text
Preprocessor::Lex(Result):
  while ( not CurLexerCallback(self, Result) ):   // 返回 false 就再问一次
      continue
  // 此时 Result 里装好了真正要交给上层的 Token
  // 维护 C++20 模块导入序列状态、checkpoint 等
```

`CurLexerCallback` 是个函数指针，取值之一：

```text
CLK_Lexer          → CurLexer->Lex(Result)          // 问源文件 Lexer 要
CLK_TokenLexer     → CurTokenLexer->Lex(Result)     // 问宏展开体要
CLK_CachingLexer   → 从缓存里取
CLK_DependencyDirectivesLexer → 依赖扫描专用快速路径
```

关键设计：**「返回 false」表示「我消耗了输入，但这次没能产出一个最终 Token，请再调我一次」**。两种典型情形会让回调返回 false：

- Lexer 在行首遇到 `#`，把控制权交给 `HandleDirective`，处理完整条指令后并不产出 Token（指令本身不是 Token）；
- 标识符触发宏展开，宏体被推入 TokenLexer，但本次调用还没把宏体的第一个 Token 取出来。

于是「循环 + 返回值约定」就把「处理副作用但暂不产出 Token」这件事自然地表达了出来，**用循环避免了递归**（注释原话："this avoids recursion"）。

#### 4.2.3 源码精读

`CurLexerCallback` 的类型与默认值就在 Preprocessor 的私有成员里：

[clang/include/clang/Lex/Preprocessor.h:834-835](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Lex/Preprocessor.h#L834-L835) —— `LexerCallback` 是个函数指针类型，`CurLexerCallback` 默认指向 `CLK_Lexer`。

四个 `CLK_*` 转发函数把调度目标具象化，它们都很短：

[clang/include/clang/Lex/Preprocessor.h:3168-3180](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Lex/Preprocessor.h#L3168-L3180) —— `CLK_Lexer`/`CLK_TokenLexer`/`CLK_CachingLexer`/`CLK_DependencyDirectivesLexer`，分别转发到 `CurLexer`、`CurTokenLexer`、`CachingLex`、依赖扫描 Lexer。

主循环本体：

[clang/lib/Lex/Preprocessor.cpp:931-936](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/Preprocessor.cpp#L931-L936) —— `Preprocessor::Lex` 的核心就是 `while (!CurLexerCallback(*this, Result));`，注释明说「用循环避免递归」。其后是 C++20 模块导入序列状态机等收尾维护（与本讲主题无关，略）。

那么 `CurLexerCallback` 何时被切换？最典型的两处：进入一个新源文件时（`#include`，见 4.3）；进入宏展开体时（宏展开，见 4.4）。看进入新源文件时如何选 `CLK_Lexer`：

[clang/lib/Lex/PPLexerChange.cpp:107-121](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/PPLexerChange.cpp#L107-L121) —— `EnterSourceFileWithLexer`：把新 Lexer 设为 `CurLexer`，并按「是否依赖扫描 Lexer」把 `CurLexerCallback` 设为 `CLK_DependencyDirectivesLexer` 或 `CLK_Lexer`。

#### 4.2.4 代码实践

**目标**：理解「Token 流是统一的，无论来自源文件还是宏展开」。

**操作步骤**：

1. 写一个含宏的文件 `m.c`：

   ```c
   #define GREETING "hello"
   int main(void) { return 0; }
   ```

2. 预处理并观察 `GREETING` 出现的位置变化：

   ```bash
   clang -E m.c
   ```

3. 想象一个调用点 `const char *s = GREETING;`（请自行加入 `main` 之前或之内），再跑一次 `-E`。

**需要观察的现象**：`#define` 那一行在 `-E` 输出里**消失**了（它是预处理指令，不产出 Token），而 `GREETING` 被替换成了 `"hello"`。从本节视角看：`#define` 走的是 `HandleDirective`（返回 false、不产出 Token），`GREETING` 走的是宏展开（宏体 `"hello"` 经 TokenLexer 回放，成为 Token 流的一部分）。

**预期结果**：你能用「主循环 + 回调返回值」解释为什么指令行「凭空消失」、宏调用点「凭空多出内容」。

**注意**：`-E` 默认会保留行标记（`# 1 "m.c"` 之类），那是行号/文件追踪信息，不是 Token。本地输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`Preprocessor::Lex` 的 `while` 循环为什么不会无限转下去？

> **参考答案**：每个 `CLK_*` 回调要么最终产出一个 Token（返回 true），要么在产出「副作用」后改变内部状态（如推进游标、切换 Lexer/TokenLexer），使得下一次调用更接近产出 Token。最坏情况下，源文件终究会读到 EOF（产出 `tok::eof`），宏展开体也是有限的，所以循环必然终止。这个「用循环代替递归」的设计避免了深层宏嵌套时栈溢出。

**练习 2**：为什么需要 `CLK_CachingLexer` 这个来源？

> **参考答案**：处理函数式宏 `M(a, b)` 时，Preprocessor 需要先向前看若干 Token 来判定 `M` 后面是不是紧跟着 `(`、以及参数列表是什么。为此它会把读出来但暂时不交出去的 Token 缓存起来，之后通过 `CLK_CachingLexer` 优先把缓存里的 Token 发出去。这是一个「前瞻（lookahead）」机制，让宏展开和正常切词可以交错。

### 4.3 预处理指令处理：#define / #include / #if

#### 4.3.1 概念说明

以 `#` 开头、位于行首的行叫**预处理指令（preprocessor directive）**。Lexer 自己不解释它们——它只负责在行首遇到 `#` 时，把控制权「回调」给 Preprocessor。Preprocessor 的 `HandleDirective` 是所有指令的总入口，内部按指令名（`include`/`define`/`if`/`ifdef`/`pragma`/…）分发到各自的处理函数。

本节聚焦三类最常见、最有代表性的指令：

- `#define`：定义宏。它在内存里创建一个 `MacroInfo`，记录替换体是哪些 Token、是不是函数式、有没有可变参数。
- `#include`：包含头文件。它要把文件名解析成磁盘上的真实文件，再为这个文件新建一个 Lexer 推入「包含栈」。
- `#if`/`#ifdef`/`#else`/`#endif`：条件编译。决定一段 Token 流要不要被「跳过」。

#### 4.3.2 核心流程

**指令的整体流程**：

```text
Lexer 在行首读到 '#'
  └─ goto HandleDirective  → FormTokenWithChars(hash) + PP->HandleDirective(Result)
HandleDirective(Result):
  1. CurPPLexer->ParsingPreprocessorDirective = true   // 让 Lexer 把行尾换行变成 EOD token
  2. LexUnexpandedToken(Result)                          // 读指令名，且不做宏展开（C99 6.10.3p8）
  3. switch (II->getPPKeywordID()):
       pp_define → HandleDefineDirective   // 建 MacroInfo
       pp_include→ HandleIncludeDirective  → LookupFile → EnterSourceFile
       pp_if/ifdef/ifndef/elif/else/endif → 条件栈控制 / 跳过
       pp_pragma → HandlePragmaDirective
       …
  4. （指令处理过程中，读到 EOD 即结束本指令）
返回 false（指令本身不产出 Token，主循环再问一次）
```

**`#if 0` 跳过的原理**：Preprocessor 维护一个**条件栈**（`ConditionalStack`，元素类型 `PPConditionalInfo`），记录每一层 `#if` 是否处在「跳过」状态。当处在跳过块里时，它会令 Lexer 进入 **raw 模式**（`LexingRawMode = true`），从而极快地略过那些 Token，不做宏展开、不发诊断，直到对应的 `#else`/`#endif`。这是预处理里很重要的一项性能优化。

**`#include` 的「包含栈」**：每 `#include` 一个文件，就为它新建一个 Lexer，把当前 Lexer 压入 `IncludeMacroStack`，把 `CurLexerCallback` 切到新 Lexer。当新文件读到 EOF，`HandleEndOfFile` 弹栈，回到原文件继续。

#### 4.3.3 源码精读

先看 Lexer 如何把行首的 `#` 交给 Preprocessor：

[clang/lib/Lex/Lexer.cpp:4516-4536](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/Lexer.cpp#L4516-L4536) —— `case '#'`：若是 `##` 则是粘贴运算符，否则在「物理行首 + 非 raw + 非 pragma」条件下 `goto HandleDirective`。

[clang/lib/Lex/Lexer.cpp:4640-4651](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/Lexer.cpp#L4640-L4651) —— `HandleDirective` 标签：落定 `hash` token，调用 `PP->HandleDirective(Result)`，然后返回 false（让主循环继续）。

`HandleDirective` 总入口：

[clang/lib/Lex/PPDirectives.cpp:1311-1338](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/PPDirectives.cpp#L1311-L1338) —— 设置 `ParsingPreprocessorDirective`，用 `LexUnexpandedToken` 读指令名（不展开宏），准备分发。

指令分发的大 switch（节选条件、包含、宏定义三类）：

[clang/lib/Lex/PPDirectives.cpp:1407-1442](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/PPDirectives.cpp#L1407-L1442) —— 按 `getPPKeywordID()` 分发：`pp_if/ifdef/ifndef/elif/else/endif` 走条件编译；`pp_include` 走 `HandleIncludeDirective`；`pp_define` 走 `HandleDefineDirective`；`pp_undef` 走 `HandleUndefDirective`。

`#define` 在内存里的样子——`MacroInfo` 持有参数列表与替换 Token 列表：

[clang/include/clang/Lex/MacroInfo.h:40-95](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Lex/MacroInfo.h#L40-L95) —— `MacroInfo`：`ParameterList`（函数式宏的形参）、`ReplacementTokens`（替换体）、`IsFunctionLike`/`IsC99Varargs`/`IsBuiltinMacro`/`IsDisabled` 等标志。注释说「每个 `#define` 对应一个 `MacroInfo` 实例」。

[clang/include/clang/Lex/MacroInfo.h:202-218](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Lex/MacroInfo.h#L202-L218) —— `isObjectLike()`/`isFunctionLike()`/`isBuiltinMacro()` 三个关键判定。

`#include` 解析文件名用 `HeaderSearch::LookupFile`，它在搜索路径（`-I` 目录、系统目录、header map 等）里找：

[clang/include/clang/Lex/HeaderSearch.h:562-570](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Lex/HeaderSearch.h#L562-L570) —— `LookupFile` 的签名。参数里的 `isAngled` 区分 `<>` 与 `""`：尖括号从系统/角度目录搜，双引号先相对当前文件所在目录搜。`SuggestedModule` 用于 C++20 模块/显式模块。

找到文件后，`EnterSourceFile` 为它新建一个 Lexer 并入栈：

[clang/lib/Lex/PPLexerChange.cpp:68-103](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/PPLexerChange.cpp#L68-L103) —— `EnterSourceFile`：取文件 `MemoryBuffer`，`std::make_unique<Lexer>(...)` 新建词法分析器，再调 `EnterSourceFileWithLexer` 入栈。注意它要求「不能在宏展开体里 `#include`」（开头的断言）。

文件读到末尾时如何回到原文件（弹栈）：

[clang/lib/Lex/PPLexerChange.cpp:323](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/PPLexerChange.cpp#L323) —— `HandleEndOfFile`：处理一个文件读完（或宏展开体读完）时的弹栈收尾。

条件编译跳过块的 raw 模式开关：

[clang/lib/Lex/PPDirectives.cpp:574](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/PPDirectives.cpp#L574) —— 在跳过条件块时设 `CurPPLexer->LexingRawMode = true`，让 Lexer 快速略过不参与编译的 Token。

#### 4.3.4 代码实践

**目标**：用 `clang` 的预处理开关观察三类指令的效果，并理解「包含栈」与「条件跳过」。

**操作步骤**：

1. 准备 `a.h`：

   ```c
   #ifndef A_H
   #define A_H
   int answer = 42;
   #endif
   ```

2. 准备 `use.c`：

   ```c
   #include "a.h"
   #if 0
   int dead_code(void);
   #endif
   int get(void) { return answer; }
   ```

3. 只做预处理：

   ```bash
   clang -E use.c
   ```

4. 用 `-H` 打印实际被包含的头文件链（对应「包含栈」的入栈过程）：

   ```bash
   clang -E -H use.c
   ```

5. 用 `-dM` dump 所有已定义宏（能看到 `A_H`、`answer` 之外大量预定义宏）：

   ```bash
   clang -E -dM use.c | grep -E "A_H|__STDC__"
   ```

**需要观察的现象**：

- `a.h` 的内容（`int answer = 42;`）被内联进 `use.c` 的输出；`#ifndef/#define/#endif` 这些指令行消失——这是「头文件保护（include guard）」在工作。
- `#if 0 ... #endif` 之间的 `dead_code` **完全不出现**在输出里——它被 raw 模式跳过了。
- `-H` 会显示 `. a.h` 这样的层级化包含关系，对应每次 `#include` 调一次 `EnterSourceFile`。

**预期结果**：你能把「输出里出现/消失的内容」逐一对应到 `HandleDirective` 的某条分支：`pp_include`→内联、`pp_define`→不产出 Token、`pp_if/endif`→跳过。

**注意**：再次 `#include "a.h"` 时，由于 `A_H` 已定义、`#ifndef` 为假，整个文件会被跳过——这就是 include guard 防止重复包含的原理。本地输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `HandleDirective` 读指令名时用的是 `LexUnexpandedToken` 而不是普通的 `Lex`？

> **参考答案**：C99 6.10.3p8 规定，指令名（如 `include`、`define`）本身不允许被宏展开。如果用会展开宏的 `Lex`，万一用户 `#define include ...`，行为就会混乱。`LexUnexpandedToken` 读出的指令名保持原样，再做 `getPPKeywordID()` 匹配。

**练习 2**：include guard（`#ifndef X / #define X / ... / #endif`）为什么能防止重复包含？它依赖了本节的哪个机制？

> **参考答案**：第一次包含时 `X` 未定义，`#ifndef` 为真，整个文件被正常处理，`#define X` 让 `X` 成为已定义宏；第二次包含时 `X` 已定义，`#ifndef` 为假，进入「跳过」分支，整文件被 raw 模式快速略过。它依赖两个机制：条件栈的跳过（`#if/#endif`）与宏定义的持久化（`#define` 创建的 `MacroInfo` 在整个翻译单元内有效）。（Clang 还有一个「多次包含优化 MIOpt」，在符合条件时连文件都不用读第二遍，进一步加速。）

### 4.4 宏展开机制：标识符如何被替换

#### 4.4.1 概念说明

宏展开是预处理最精巧的部分。它的本质是：**把一个标识符 Token 替换成另一串预先存好的 Token**。难点在于：

- **函数式宏** `M(a, b)` 要先收集参数 Token，再把参数填进替换体；
- 替换出的 Token 可能**又是个宏**，要继续展开（但禁止无限递归，`#define A A` 不能循环）；
- 替换体里的 `#`（字符串化）、`##`（Token 粘贴）、`__VA_ARGS__`/`__VA_OPT__` 需要特殊处理。

Clang 的实现思路是**「Token 再注入」**：宏的替换体本来就被存成一串 Token（在 `MacroInfo` 里）。展开时，Preprocessor 用一个 `TokenLexer` 包住这串 Token（顺带做参数替换、`#`/`##` 处理），把它作为一个新的 Token 来源推入流中。于是「展开后的 Token」就像是从源文件里直接读出来的一样，自然地汇入主循环——这正是 4.2 里 `CLK_TokenLexer` 来源的用途。

#### 4.4.2 核心流程

```text
Lexer 切到一个标识符 Token
  └─ LexIdentifierContinue → LookUpIdentifierInfo → 若 isHandleIdentifierCase():
       PP->HandleIdentifier(Token)
HandleIdentifier(Token):
  └─ 若 Token 是已定义宏 MI，且未被禁用展开:
       └─ 若是函数式且紧跟 '('（或对象式）:
            HandleMacroExpandedIdentifier(Token, MD)
HandleMacroExpandedIdentifier(Token, M):
  1. 内建宏（__LINE__ 等）→ ExpandBuiltinMacro
  2. 函数式 → ReadMacroCallArgumentList 读参数
  3. 替换体为空      → 直接返回 false（标记 LeadingEmptyMacro）
  4. 替换体仅 1 个平凡 token → 现场替换 Identifier（快速路径）
  5. 一般情况 → EnterMacro(...)：
       创建 TokenLexer 包住替换体 Token（含参数替换）
       把 CurLexerCallback 切到 CLK_TokenLexer
     返回 false（让主循环从 TokenLexer 取下一个 Token）
```

注意「**对象式 vs 函数式**」的判定：`MI->isFunctionLike()` 决定要不要去找 `(`。C99 6.10.3p10 还规定，函数式宏后面若不紧跟 `(`，就不展开（保持原标识符）。

#### 4.4.3 源码精读

标识符触发宏判定的入口在 `HandleIdentifier`：

[clang/lib/Lex/Preprocessor.cpp:873-891](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/Preprocessor.cpp#L873-L891) —— 用 `getMacroDefinition(&II)` 查宏；若存在且未禁用展开，再按「函数式且紧跟 `(`，或对象式」条件调用 `HandleMacroExpandedIdentifier`。

宏展开主函数 `HandleMacroExpandedIdentifier` 的开头几步：

[clang/lib/Lex/PPMacroExpansion.cpp:433-451](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/PPMacroExpansion.cpp#L433-L451) —— 先处理内建宏（`__LINE__`/`_Pragma` 等，走 `ExpandBuiltinMacro`）。

[clang/lib/Lex/PPMacroExpansion.cpp:463-482](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/PPMacroExpansion.cpp#L463-L482) —— 函数式宏读参数：置 `InMacroArgs = true`，调 `ReadMacroCallArgumentList` 收集参数 Token，统计 `NumFnMacroExpanded`/`NumMacroExpanded`。

两个快速路径（避免为简单宏也创建 TokenLexer 的开销）：

[clang/lib/Lex/PPMacroExpansion.cpp:539-585](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/PPMacroExpansion.cpp#L539-L585) —— 替换体为空（`#define EMPTY`）直接返回；替换体仅一个平凡 Token（如 `#define VAL 42`）现场替换 `Identifier`，连位置信息都用 `createExpansionLoc` 重新生成。这两种是「热路径」，所以单独优化。

一般情况收尾——`EnterMacro` 把宏体作为新 Token 源推入：

[clang/lib/Lex/PPMacroExpansion.cpp:587-589](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Lex/PPMacroExpansion.cpp#L587-L589) —— `EnterMacro(Identifier, ExpansionEnd, MI, Args); return false;`。`EnterMacro` 内部会创建 `TokenLexer` 包住宏体 Token（含参数与 `#`/`##` 处理），并切换 `CurLexerCallback` 到 `CLK_TokenLexer`，让主循环从宏体里取 Token。

`EnterMacro` / `EnterTokenStream` 的声明（用于把任意一串 Token 注入主流）：

[clang/include/clang/Lex/Preprocessor.h:1712-1741](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Lex/Preprocessor.h#L1712-L1741) —— `EnterMacro` 与多个重载的 `EnterTokenStream`，它们是「Token 再注入」的统一入口。

#### 4.4.4 代码实践

**目标**：观察对象式宏、函数式宏、嵌套展开、字符串化与 Token 粘贴的效果，对照「再注入」模型解释。

**操作步骤**：

1. 写 `macro.c`：

   ```c
   #define VAL 42
   #define ADD(a, b) ((a) + (b))
   #define STR(x) #x
   #define CAT(a, b) a##b
   int CAT(foo, VAL) = ADD(VAL, 8);
   const char *s = STR(ADD(1, 2));
   ```

2. 预处理：

   ```bash
   clang -E macro.c
   ```

3. 追加一行验证「函数式宏不紧跟 `(` 不展开」：把 `int z = ADD;` 加入后再跑 `-E`。

**需要观察的现象**：

- `CAT(foo, VAL)` → `fooVAL`（注意：`##` 粘贴发生在参数替换*之前*对参数的处理上，`VAL` 作为参数不会被先展开成 `42`，所以得到 `fooVAL` 而非 `foo42`——这是 C 宏的经典坑）。
- `ADD(VAL, 8)` → `((42) + (8))`：对象式宏 `VAL` 作为参数传入后，在替换体里展开成 `42`。
- `STR(ADD(1, 2))` → `"ADD(1, 2)"`：`#` 字符串化在参数展开*之前*，所以参数里的 `ADD(1,2)` 不会被先算成 `3`。
- `int z = ADD;` 保持原样不展开（函数式宏缺 `(`）。

**预期结果**：你能把每个结果归因到 `HandleMacroExpandedIdentifier` 的某一步：参数收集、`#`/`##` 处理、递归展开、`(` 判定。

**注意**：宏语义里「先粘贴/字符串化，还是先展开参数」有精确规则（C 标准 6.10.3.1/6.10.3.2/6.10.3.3），上述现象正是这些规则的体现。本地输出**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`#define A A`（宏体就是它自己的名字）为什么不会无限展开？

> **参考答案**：`MacroInfo` 有一个 `IsDisabled` 标志（见 `MacroInfo.h`）。一旦某个宏正在被展开，它就被标记为禁用；展开体里再次出现 `A` 时，`HandleIdentifier` 发现它「已禁用展开」，于是不再展开，并按 C99 6.10.3.4p2 给它打上 `DisableExpand` 标志，使其「此后也永远不再展开」。这样就打破了递归。

**练习 2**：为什么说宏展开是「Token 再注入」而不是「文本替换」？

> **参考答案**：宏体在 `MacroInfo` 里以**一串 Token**（`ReplacementTokens`）的形式存储，不是字符串。展开时，`TokenLexer` 把这串 Token（经过参数替换、`#`/`##` 处理）作为新的 Token 源推入主流，由 `CLK_TokenLexer` 回放给主循环。整个过程操作的都是 Token（带位置、种类、标志），而非重新做一遍文本拼接——这就是「Token 再注入」。这也解释了为什么宏展开后的诊断仍能精确指回源码位置（靠 `createExpansionLoc` 维护的展开位置链）。

## 5. 综合实践

把本讲四个模块串起来，完成一个「**用 `clang -E` 还原 Token 流生成过程**」的小任务。

**任务背景**：给一段真实的小程序，你不仅要说出 `-E` 的输出，还要能逐行指认「这一步对应源码里的哪个函数」。

**素材** `proj.c`：

```c
#include <stdio.h>          // 系统头文件包含
#define MAX(a, b) ((a) > (b) ? (a) : (b))   // 函数式宏
#define VERSION 3            // 对象式宏

#if VERSION >= 3
const char *feature = "v3";
#else
const char *feature = "legacy";
#endif

int main(void) {
    int x = MAX(VERSION, 2);
    printf("%d %s\n", x, feature);
    return x;
}
```

**要求**：

1. 运行 `clang -E proj.c`，观察输出。把输出里**每一处显著变化**对应到本讲的一个函数：
   - `<stdio.h>` 的内容被内联 → `HandleIncludeDirective` → `LookupFile` → `EnterSourceFile`；
   - `#define` 两行消失 → `HandleDirective`（`pp_define`）不产出 Token；
   - `#if/#else/#endif` 只保留 `feature = "v3"` 一支 → 条件栈 + raw 模式跳过；
   - `MAX(VERSION, 2)` 展开成 `((3) > (2) ? (3) : (2))` → `HandleMacroExpandedIdentifier` 收参数 → `EnterMacro`/`TokenLexer` 再注入，且 `VERSION` 作为参数递归展开成 `3`。
2. 用 `clang -E -H proj.c` 看 `<stdio.h>` 的包含层级；用 `clang -E -dM proj.c | grep VERSION` 确认 `VERSION`、`MAX` 是否在宏表里。
3. 把 `VERSION` 改成 `2`，重新跑 `-E`，验证 `feature` 走 `legacy` 分支、`MAX(2, 2)` 的展开结果。
4. （进阶）写一段把上述对应关系画成「源码行 → 函数 → 输出片段」的三列表格。

**验收标准**：你能脱稿说出「源码里这一行，是经过 Lexer 的哪条 `switch` 分支、又触发了 Preprocessor 的哪个函数，最终在 `-E` 输出里变成什么」。能做到这一点，本讲就达标了。

> 本地具体输出（尤其 `<stdio.h>` 展开后的体量）**待本地验证**。

## 6. 本讲小结

- **Lexer 与 Preprocessor 分工明确**：Lexer 只做字面切分（阶段 1–3，含三字符组/行续接的字符级折叠），不认识宏与 `#` 指令；指令处理与宏展开（阶段 4）都在 Preprocessor 里。Lexer 仅在「行首 `#`」和「特殊标识符」两个时刻回调 Preprocessor。
- **Token 是信息优先的结构体**：携带种类、位置、长度与标志位（行首/前导空白/需清洗等），`PtrData` 按种类联合存放 `IdentifierInfo*`、字面量指针或注解数据。
- **`LexTokenInternal` 是按首字符分发的大 `switch`**：数字→`LexNumericConstant`、字母→`LexIdentifierContinue`、引号→字面量、标点→直接判类，末尾用 `FormTokenWithChars` 落定位置。
- **Preprocessor 的主循环靠函数指针 `CurLexerCallback` 调度**：在「源文件 Lexer / 宏体 TokenLexer / 缓存」之间切换，用「返回 false 就再调一次」的约定表达「处理副作用但不立即产出 Token」，以循环代替递归。
- **`#` 指令、宏展开、`#include` 都以「改变 Token 流」收尾**：指令行不产出 Token；`#include` 把新文件 Lexer 推入包含栈；宏展开把宏体 Token 经 `TokenLexer` 再注入主流——它们共同的特点是「调用后返回 false，让主循环继续」。
- **条件编译用 raw 模式快速跳过**：`#if 0` 块令 Lexer 进入 `LexingRawMode`，不展开、不诊断地略过 Token；include guard 则靠条件栈 + 宏定义持久化防止重复包含。

## 7. 下一步学习建议

- **进入语法分析（u5-l3）**：本讲产出的 Token 流会被 Parser 消费。建议接下来读 `clang/lib/Parse/Parser.cpp` 与 `clang/lib/Parse/ParseAST.cpp`，看 `ParseAST` 如何驱动 Preprocessor 取 Token、再用递归下降把它们组装成 AST。
- **深入宏与模块**：若对宏展开的细节（`__VA_OPT__`、参数预展开时机）感兴趣，可精读 `clang/lib/Lex/TokenLexer.cpp`（宏体回放与 `#`/`##` 处理）和 `clang/lib/Lex/MacroArgs.cpp`。
- **头文件搜索与模块**：想理解 `-I` 搜索顺序、header map、C++20 模块如何影响 `#include`，可读 `clang/lib/Lex/HeaderSearch.cpp`、`clang/lib/Lex/InitHeaderSearch.cpp` 与 `clang/lib/Lex/ModuleMap.cpp`。
- **观测工具清单**：把 `clang -E`（预处理）、`-E -H`（包含树）、`-dM`（dump 宏）、`-dump-tokens`/`-dump-raw-tokens`（Token 转储）当成日后调试前端问题的常备工具。
```
