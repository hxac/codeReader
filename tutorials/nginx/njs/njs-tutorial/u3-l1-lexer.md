# 词法分析器 njs_lexer

## 1. 本讲目标

本讲是「编译前端」单元的第一站。在 [u2-l1](u2-l1-vm-lifecycle-api.md) 里我们已经知道，`njs_vm_compile` 内部要走一条「源码字符串 → 词法 token → AST → 字节码」的流水线，而本讲要拆解的就是这条流水线的最前端——**词法分析器（lexer）**。

学完本讲，你应该能够：

1. 说清楚 `njs_lexer_t` 这个结构体里都装了什么状态，以及它如何用一张 256 项的字节查找表把「一个字符」映射成「一个基础 token 类型」。
2. 看懂 `njs_token_type_t` 这套 200 多个 token 常量是怎么编号的，以及关键字（`function`、`if`、`await`……）是如何被识别成对应 token 的。
3. 理解「预读队列（preread queue）」机制：为什么解析器需要 `njs_lexer_peek_token` 偷看下一个 token 却不消费它，以及 `njs_lexer_token` / `njs_lexer_consume_token` 三者如何协作。

本讲只聚焦词法层，不涉及语法树的构造（那是 [u3-l2](u3-l2-parser-ast.md) 的内容），也不涉及字节码生成。

## 2. 前置知识

- **token（记号）**：词法分析的产物。源码是一串字节，词法器把它切成一个个有意义的最小单元，比如 `function`、`123`、`=>`、`{`、标识符 `myVar`。每个 token 带一个「类型」和「原始文本」。
- **字符驱动 vs. 状态机**：最朴素的词法器是「读一个字符，查表决定它属于哪类 token，再决定要不要继续读」。njs 的核心就是这个思路——用一张 `njs_tokens[256]` 表做 O(1) 的单字节分发。
- **多字符运算符**：JS 里有大量「前缀相同、长度不同」的运算符，比如 `=` / `==` / `===`、`<` / `<<` / `<=`、`+` / `++` / `+=`。词法器需要「贪心」地多读几步才能确定到底切出哪个 token。
- **预读（lookahead / peek）**：解析器常常需要「先看一眼下一个 token 是什么，再决定走哪条语法分支」，但看的时候不能真的把它吃掉。njs 用一个 FIFO 队列 `preread` 来缓存已经词法化、但还没被消费的 token。
- 本讲默认你已经读过 [u2-l1](u2-l1-vm-lifecycle-api.md)（知道 `njs_vm_compile` 的存在）和 [u2-l4](u2-l4-atom-table.md)（知道「atom 驻留」是怎么回事——词法器产出的标识符名会被驻留成 32 位 atom_id）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/njs_lexer.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.h) | 词法器的全部类型定义：token 枚举 `njs_token_type_t`、单个 token 的结构 `njs_lexer_token_t`、词法器状态 `njs_lexer_t`，以及对外 API 声明和一组判别内联函数。 |
| [src/njs_lexer.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c) | 词法器的全部实现：256 项字节分发表、多字符运算符表、核心扫描函数 `njs_lexer_make_token`、标识符/字符串/数字扫描，以及 token 的「生产—入队—预读—消费」四个函数。 |
| [src/njs_lexer_keyword.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer_keyword.c) | 关键字查找：完美哈希函数 `njs_lexer_keyword_hash`、链式哈希查找 `njs_lexer_keyword_entry`、对外入口 `njs_lexer_keyword`。 |
| [utils/lexer_keyword.py](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/utils/lexer_keyword.py) | 关键字表**生成器**：在构建期把一份关键字清单生成为 `src/njs_lexer_tables.h`（`njs_lexer_kws` 数组 + `njs_lexer_keyword_entries` 哈希表）。 |

> 说明：`src/njs_lexer_tables.h` 不在 git 仓库里，它是构建时由 `utils/lexer_keyword.py` 生成的产物，被 `njs_lexer_keyword.c` 通过 `#include <njs_lexer_tables.h>` 引入。本讲引用生成器脚本而非该产物文件。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**词法器状态机**、**token 类型与关键字**、**预读队列**。

### 4.1 词法器状态机

#### 4.1.1 概念说明

词法器（`njs_lexer_t`）是一个带状态的扫描器：它手里攥着一段源码的字节区间 `[start, end)`，一个游标指针 `start` 指向「还没读到的下一个字节」，以及一个行号 `line` 用来给每个 token 打上源码行号（出错时能指到第几行）。

它的核心工作可以概括成一句话：**读字符 → 查 256 项表得到基础类型 → 按类型决定要不要再多读几步 → 产出一个 token**。这是一个标准的「字符驱动 + 局部状态机」词法器，没有用正则或 DFA 工具生成，全部手写，目的是为了完全可控、可裁剪、零外部依赖（这很 NGINX）。

#### 4.1.2 核心流程

单个 token 的扫描流程（`njs_lexer_make_token`）：

1. 跳过空白：循环读字节，ASCII 用 `njs_tokens[c]` 判断是否为 `NJS_TOKEN_SPACE`，是则继续；非 ASCII（`c & 0x80`）走 UTF-8 解码 + 空白判断。
2. 取第一个非空白字节 `c`，查 `njs_tokens[c]` 得到「基础 token 类型」。
3. 按基础类型分支：
   - `LETTER` → 调 `njs_lexer_word` 扫一个标识符/关键字。
   - `DIGIT` 或 `.`（后跟数字）→ 调 `njs_lexer_number` 扫一个数字字面量。
   - `DOUBLE_QUOTE` / `SINGLE_QUOTE` → 调 `njs_lexer_string` 扫一个字符串。
   - 各种运算符（`=`、`+`、`<`、`&`……）→ 调 `njs_lexer_multi` 做「贪心多字符归约」。
   - `/` → 调 `njs_lexer_division`，因为 `/` 既可能是除法、也可能是 `//` 行注释、`/* */` 块注释、`/=`、或正则字面量的起始，需要特殊处理。
   - `\n` → 行号 `line++`，产出 `NJS_TOKEN_LINE_END`。
4. 把 token 的 `text`（原始文本切片）、`line`（行号）、`type` 填好返回。

#### 4.1.3 源码精读

先看词法器自身的状态结构 `njs_lexer_t`：

[src/njs_lexer.h:254-274](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.h#L254-L274) — `njs_lexer_t` 持有源码区间 `start/end`、当前行号 `line`、文件名 `file`、预读队列 `preread`、上一 token 类型 `prev_type`，以及一个括号嵌套栈 `in_stack`（供解析器做上下文相关的判别，见 4.3）。注意 `start` 是「下一个待读字节」的游标。

初始化非常朴素，就是把入参存进去、行号设为 1、初始化预读队列和括号栈：

[src/njs_lexer.c:295-308](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L295-L308) — `njs_lexer_init`。这段就是词法器的「开机」，`njs_vm_compile → njs_parser_init` 最终会调用到这里。

整张 256 项的字符分发表是词法器的「主调度表」：

[src/njs_lexer.c:30-177](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L30-L177) — `njs_tokens[256]`，把每个 ASCII 字节直接映射成一个基础 token 类型。例如下标 `'+'` 处填 `NJS_TOKEN_ADDITION`、`'<'` 处填 `NJS_TOKEN_LESS`、字母处填 `NJS_TOKEN_LETTER`、数字处填 `NJS_TOKEN_DIGIT`。表用 `njs_aligned(64)` 对齐以加速缓存访问。这就是「O(1) 单字节分发」的实现。

核心扫描函数 `njs_lexer_make_token` 的前半段——跳空白 + 查表：

[src/njs_lexer.c:541-580](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L541-L580) — 先用 `njs_utf8_decode_init` 初始化 UTF-8 解码上下文，然后循环：ASCII 字符直接 `njs_tokens[c]` 判断是否空白；非 ASCII 走 `njs_utf8_decode` 解出一个码点再判断是否 Unicode 空白。读到第一个「非空白」字节就 `break`，并把它的基础类型赋给 `token->type`。

接下来是一个大 `switch`，按基础类型派发到各子扫描器：

[src/njs_lexer.c:582-704](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L582-L704) — 派发逻辑。比如 `NJS_TOKEN_LETTER` 调 `njs_lexer_word`；`NJS_TOKEN_DIGIT` 和 `NJS_TOKEN_DOT`（后跟数字时 fall-through）调 `njs_lexer_number`；`NJS_TOKEN_ASSIGNMENT`(`=`)、`NJS_TOKEN_ADDITION`(`+`)、`NJS_TOKEN_LESS`(`<`) 等运算符各调一次 `njs_lexer_multi`；`NJS_TOKEN_DIVISION`(`/`) 调 `njs_lexer_division`。读到 `NJS_TOKEN_SPACE`（说明源码已耗尽）就返回 `NJS_TOKEN_END`。

多字符运算符的「贪心归约」由一组静态表 + 一个通用函数完成。以 `=` 家族为例：

[src/njs_lexer.c:289-292](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L289-L292) — `njs_assignment_token[]`：看到 `=` 后，若下一个字符是 `=` 就变成 `NJS_TOKEN_EQUAL`（并允许继续匹配 `===`→`NJS_TOKEN_STRICT_EQUAL`），若是 `>` 就变成 `NJS_TOKEN_ARROW`（箭头函数 `=>`）。这张表用结构体 `njs_lexer_multi_t{symbol, token, count, next}` 表达「下一个字符 → 新 token → 是否还有后续表」的链式状态。

归约执行函数：

[src/njs_lexer.c:904-935](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L904-L935) — `njs_lexer_multi` 沿着 `multi` 表逐字符匹配，匹配上就推进游标、更新 token 类型，并按 `count`/`next` 跳到下一张子表继续，从而把 `=`、`==`、`===`、`=>`、`<`、`<<`、`<<=`、`>>>` 这类同前缀运算符一次性归约到最长匹配。

`/` 的歧义消除（除法 vs 注释 vs 正则）单独处理：

[src/njs_lexer.c:938-998](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L938-L998) — `njs_lexer_division`：若 `/` 后是 `/` 则扫到行尾产出 `NJS_TOKEN_LINE_END`（行注释）；若是 `*` 则扫到 `*/` 产出 `NJS_TOKEN_COMMENT`（块注释）；若是 `=` 则产出 `NJS_TOKEN_DIVISION_ASSIGNMENT`（`/=`）；否则就是除法本身。注意正则字面量的识别不在词法层完成，而由解析器根据上下文回调词法器（超出本讲范围）。

#### 4.1.4 代码实践

**实践目标**：亲手追踪一段极简源码 `a + 1` 经过 `njs_lexer_make_token` 的扫描过程。

**操作步骤**（源码阅读型实践，无需构建）：

1. 在 [src/njs_lexer.c:30-177](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L30-L177) 的 `njs_tokens` 表里，分别查 `'a'`、`'+'`、`'1'`、`' '`（空格）四个字节各自映射到什么基础类型。
2. 模拟游标 `start` 从 `'a'` 开始，按 [src/njs_lexer.c:541-704](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L541-L704) 的逻辑，记录三次扫描分别走哪个 `case`、产出什么 token。

**需要观察的现象**：
- 第 1 个 token：`'a'` → `NJS_TOKEN_LETTER` → 进入 `njs_lexer_word`，由于 `a` 不在关键字表里，最终产出 `NJS_TOKEN_NAME`。
- 空格被第 2 次扫描的开头「跳空白」循环吃掉。
- 第 2 个 token：`'+'` → `NJS_TOKEN_ADDITION` → 进入 `njs_lexer_multi(njs_addition_token)`，由于 `+` 后面不是 `+` 也不是 `=`，保持 `NJS_TOKEN_ADDITION`。
- 第 3 个 token：`'1'` → `NJS_TOKEN_DIGIT` → 进入 `njs_lexer_number`，产出 `NJS_TOKEN_NUMBER`，`token->number = 1.0`。

**预期结果**：三次扫描得到 token 序列 `NAME(a)`、`ADDITION(+)`、`NUMBER(1)`，恰好对应 `a + 1` 的三个词法单元。

#### 4.1.5 小练习与答案

**练习 1**：`njs_tokens[256]` 表里，控制字符（`\0`..`\x1f`）大多被填成什么类型？为什么？

**答案**：大多填成 `NJS_TOKEN_ILLEGAL`（见 [src/njs_lexer.c:32-48](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L32-L48)），只有 `\t`、`\n`、`\r` 等被当作空白（`NJS_TOKEN_SPACE` 或 `NJS_TOKEN_LINE_END`）。这样词法器一遇到非法控制字符就能立刻报「非法 token」，无需额外判分支。

**练习 2**：为什么 `njs_lexer_multi` 要做成「最长匹配」？如果改成「最短匹配」会出什么问题？

**答案**：因为 JS 里大量运算符前缀重叠，若最短匹配，看到 `=` 就会立即返回赋值号，于是 `==`、`===`、`=>` 全部被错误地切成 `=` + `=` + …。最长匹配（贪心读后续字符）才能保证 `===` 被正确归约为一个 `NJS_TOKEN_STRICT_EQUAL`。

---

### 4.2 token 类型与关键字

#### 4.2.1 概念说明

词法器产出的每个 token 都有一个「类型编号」，类型用枚举 `njs_token_type_t` 表示。这个枚举有两类成员：

- **纯词法 token**：由字符直接决定的，如 `NJS_TOKEN_OPEN_PARENTHESIS`(`(`)、`NJS_TOKEN_ARROW`(`=>`)、`NJS_TOKEN_NUMBER`、`NJS_TOKEN_STRING`、`NJS_TOKEN_NAME`（标识符）。
- **关键字 token**：形如 `function`、`if`、`return`、`await` 这种「看起来像标识符、但有特殊语法含义」的词，每种关键字独占一个 token 类型（如 `NJS_TOKEN_FUNCTION`、`NJS_TOKEN_IF`）。

关键字的识别不在 `njs_tokens` 表里（表只能按单字节分发），而是在扫出一个「词（word）」之后，去查一张**关键字哈希表**：命中就把它从 `NJS_TOKEN_NAME` 升级成对应的关键字 token。这张表是用一份关键字清单 + 完美哈希在构建期生成的。

#### 4.2.2 核心流程

关键字识别（`njs_lexer_word`）的流程：

1. 从当前字母开始，用一个位图 `letter_digit[]` 判断「哪些字节可以作为标识符续字符」（字母、数字、`_`、`$`），一直读到不再合法为止，得到一个词的 `[start, length)`。
2. 边读边算 DJB 哈希（增量式 `njs_djb_hash_add`）。
3. 拿 `(指针, 长度, 哈希)` 去 atom 表 `njs_atom_find` 查；查不到就 `njs_string_create` + `njs_atom_add` 驻留成一个新 atom（呼应 [u2-l4](u2-l4-atom-table.md) 的驻留机制）。
4. 看 atom 条目里预填的 `token_type` 字段：若是 `NJS_KEYWORD_TYPE_UNDEF`（不是关键字），token 类型就是 `NJS_TOKEN_NAME`；否则把预填的 `token_id` 直接当作 token 类型，从而完成「词 → 关键字 token」的升级。
5. 把 atom_id 也存进 token，后续属性查找就不用再传字符串。

关键字的完美哈希（构建期生成、运行期查找）：

\[ h(k) = \big((k_0 \times k_{n-1}) + n\big) \bmod T + 1 \]

其中 \(k\) 是关键字字符串，\(k_0\) 与 \(k_{n-1}\) 是首尾字节，\(n\) 是长度，\(T\) 是表大小。生成器遍历若干候选 \(T\)，挑出「冲突最少」的那个，把冲突的关键字用链式（`next` 偏移）串起来，最终生成一张静态表 `njs_lexer_keyword_entries`。

#### 4.2.3 源码精读

先看 token 类型枚举的规模与组织：

[src/njs_lexer.h:11-218](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.h#L11-L218) — `njs_token_type_t`。从 `NJS_TOKEN_ILLEGAL = 0` 起，依次列出各种标点/运算符、赋值类（`NJS_TOKEN_ASSIGNMENT`…`NJS_TOKEN_COALESCE_ASSIGNMENT`，并用 `NJS_TOKEN_LAST_ASSIGNMENT` 做范围标记，见 [src/njs_lexer.h:62](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.h#L62)）、常量类（`NJS_TOKEN_NULL`…`NJS_TOKEN_STRING`，见 [src/njs_lexer.h:117-126](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.h#L117-L126)）、语句/声明类（`NJS_TOKEN_FUNCTION`、`NJS_TOKEN_IF`、`NJS_TOKEN_FOR`…）、保留字（`NJS_TOKEN_ENUM`、`NJS_TOKEN_INTERFACE`…）。总数超过 150 个，正是「200+ token 类型」的来源。

单个 token 的结构：

[src/njs_lexer.h:243-251](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.h#L243-L251) — `njs_lexer_token_t`：`type`（token 类型）、`keyword_type`（是否关键字/保留字）、`line`（源码行号）、`atom_id`（驻留后的原子 id）、`text`（原始文本切片 `njs_str_t`）、`number`（数字字面量的值）、`link`（挂进预读队列的链表节点）。

关键字类型的三值枚举：

[src/njs_lexer.h:221-231](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.h#L221-L231) — `njs_keyword_type_t` 取值 `UNDEF`(非关键字) / `RESERVED`(保留字) / `KEYWORD`(真关键字)，配合 `njs_keyword_t{type, reserved}` 一起被生成器写入表。

标识符与关键字的扫描：

[src/njs_lexer.c:708-781](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L708-L781) — `njs_lexer_word`。第 [737-752](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L737-L752) 行用位图 `letter_digit` 边扫词边累加 DJB 哈希；第 [754-778](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L754-L778) 行先 `njs_atom_find` 再按需 `njs_atom_add`，最后根据 atom 条目的 `token_type` 字段决定是 `NJS_TOKEN_NAME` 还是某个关键字 token。

运行期关键字查找入口：

[src/njs_lexer_keyword.c:46-57](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer_keyword.c#L46-L57) — `njs_lexer_keyword(key, length)`：在生成好的 `njs_lexer_keyword_entries` 表里查一个关键字，返回它的条目（含对应的 `njs_keyword_t`，即 token 类型与是否保留）。

完美哈希函数：

[src/njs_lexer_keyword.c:11-15](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer_keyword.c#L11-L15) — `njs_lexer_keyword_hash`：正是上面公式 \(h(k)\) 的 C 实现 `(key[0] * key[size-1] + size) % table_size + 1`。

链式冲突解决：

[src/njs_lexer_keyword.c:18-43](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer_keyword.c#L18-L43) — `njs_lexer_keyword_entry`：算哈希定位桶，桶里若长度相同就 `strncmp` 比较，命中返回；否则沿 `entry->next` 偏移跳到下一个条目继续（开放寻址 + 链式）。

关键字清单与生成逻辑：

[utils/lexer_keyword.py:3-76](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/utils/lexer_keyword.py#L3-L76) — `global_keywords` 字典就是全部关键字的权威清单：键是关键字文本，值是 `reserved` 标志（1=保留/真关键字，0=上下文关键字）。`function`、`if`、`await`、`class`、`let`、`const` 等都在这里。

[utils/lexer_keyword.py:199-201](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/utils/lexer_keyword.py#L199-L201) — `enum(name)`：把关键字名规整成 token 常量名 `NJS_TOKEN_<NAME大写>`。这正是「关键字文本 → token 常量」的映射规则。

#### 4.2.4 代码实践

**实践目标**：按本讲规格，为 `function`、`=>`、`...` 三个词法单元各找出对应的 token 常量，并说明它们分别由哪条代码路径识别。

**操作步骤**（源码阅读型实践）：

1. 打开 [utils/lexer_keyword.py:3-76](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/utils/lexer_keyword.py#L3-L76) 的 `global_keywords`，确认 `function` 在清单里。再套用 [utils/lexer_keyword.py:199-201](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/utils/lexer_keyword.py#L199-L201) 的 `enum()` 规则，得到 token 常量名 `NJS_TOKEN_FUNCTION`，再到 [src/njs_lexer.h:150](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.h#L150) 确认该常量确实存在。识别路径：`njs_tokens['f']=LETTER` → `njs_lexer_word` → 命中关键字表 → token 升级为 `NJS_TOKEN_FUNCTION`。
2. `=>` 不是关键字，而是多字符运算符：在 [src/njs_lexer.c:289-292](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L289-L292) 的 `njs_assignment_token` 表里看到 `{ '>', NJS_TOKEN_ARROW, 0, NULL }`，对应常量 `NJS_TOKEN_ARROW`（[src/njs_lexer.h:40](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.h#L40)）。识别路径：`njs_tokens['=']=ASSIGNMENT` → `njs_lexer_multi(njs_assignment_token)` → 下一个字符是 `>` → `NJS_TOKEN_ARROW`。
3. `...`（剩余参数/展开）也不是关键字：在 [src/njs_lexer.c:592-606](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L592-L606) 的 `NJS_TOKEN_DOT` 分支里，词法器发现 `.` 后面还跟两个 `.`，就把 token 类型改成 `NJS_TOKEN_ELLIPSIS`（[src/njs_lexer.h:31](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.h#L31)）。

**需要观察的现象 / 预期结果**：

| 词法单元 | token 常量 | 识别路径 |
|---|---|---|
| `function` | `NJS_TOKEN_FUNCTION` | `njs_lexer_word` 查关键字表升级 |
| `=>` | `NJS_TOKEN_ARROW` | `njs_lexer_multi` 经 `njs_assignment_token` 归约 |
| `...` | `NJS_TOKEN_ELLIPSIS` | `njs_lexer_make_token` 的 `DOT` 分支特判 |

> ⚠️ 注意：`utils/lexer_keyword.py` 运行时会**覆写** `src/njs_lexer_tables.h`（见脚本末尾 `open(fn, 'w')`）。仓库里本就没有该文件（构建时生成），但若你想本地跑生成器观察输出，请在干净的 git 工作区里做，跑完用 `git status` / `git checkout` 还原，避免污染源码树。**本实践只需阅读脚本与头文件即可完成，无需真正运行生成器。**

#### 4.2.5 小练习与答案

**练习 1**：标识符 `function` 和 `functionName` 都以 `function` 开头，词法器会不会把后者也误识别成关键字？

**答案**：不会。`njs_lexer_word`（[src/njs_lexer.c:741-752](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L741-L752)）用位图 `letter_digit` 一直读到「非标识符续字符」才停，所以对 `functionName` 它会把整词 `functionName` 一起读出来，再去查关键字表；查不到（长度不同直接返回 NULL，见 [src/njs_lexer_keyword.c:34-36](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer_keyword.c#L34-L36)），于是保持 `NJS_TOKEN_NAME`。这就是「最大匹配 + 整词查表」保证的。

**练习 2**：`async` 在 [utils/lexer_keyword.py:60-61](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/utils/lexer_keyword.py#L60-L61) 里 `reserved=0`，这意味着什么？

**答案**：`reserved=0` 表示 `async` 是「上下文关键字」而非保留字——它既能当关键字用（如 `async function`），也能当普通标识符用（如 `let async = 1`）。对应的判别内联函数 `njs_lexer_token_is_name`（[src/njs_lexer.h:312-318](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.h#L312-L318)）允许「非保留的关键字」当作名字使用，正是为这类上下文关键字设计的。

---

### 4.3 预读队列

#### 4.3.1 概念说明

解析器在判定语法分支时，常常需要「先看下一个 token、但不消费它」。一个典型例子是 JS 的箭头函数与异步函数：

- 看到 `async` 时，解析器必须偷看下一个 token 是不是 `function`，才能决定这是「`async function` 表达式」还是「把 `async` 当普通标识符」。
- 看到 `function` 时，又要偷看下一个是不是 `*`，来区分普通函数与生成器函数。

如果词法器只能「读一个、吞一个」，这种前瞻就做不到。njs 的解法是：**词法器维护一个 FIFO 预读队列 `preread`，所有已经词法化但尚未被消费的 token 都挂在里面**。「读」和「偷看」都从这个队列取，区别只在于是否把取出的 token 从队列摘掉。

#### 4.3.2 核心流程

四个函数的分工：

1. `njs_lexer_next_token`（内部）：调用 `njs_lexer_make_token` 扫一个 token，**循环跳过注释**（`NJS_TOKEN_COMMENT`），然后把 token 挂到 `preread` 队尾，并维护括号嵌套栈。
2. `njs_lexer_token(lexer, with_end_line)`：取「队首」token。若 `with_end_line=0`，则把行结束符 `NJS_TOKEN_LINE_END` 当透明跳过。队列为空就调 `njs_lexer_next_token` 现产。**它只返回、不摘除**——真正的摘除由 `njs_lexer_consume_token` 做。
3. `njs_lexer_peek_token(lexer, current, with_end_line)`：取 `current` **之后**的下一个 token（用于前瞻）。同样只返回不摘除，必要时也会现产补齐。
4. `njs_lexer_consume_token(lexer, length)`：从队首摘除 `length` 个 token 并释放内存（行结束符不计入 `length`）。这才是「真正吃掉 token」的动作。

解析器的典型用法是：`peek` 偷看 → 判定分支 → `consume_token(1)` 吃掉当前 token → 继续。这样前瞻不会破坏后续读取。

此外，词法器还维护一个**括号嵌套栈 `in_stack`**：遇到 `(`/`[`/`{` 时 `push`，遇到 `)`/`]`/`}` 时 `pop`，并在每一层保存一个 `fail` 标志。解析器用它做上下文相关的判别——比如在 `for(...)` 头部解析初始化表达式时，若 `in_fail` 标志为真，则把 `in` 当成语法错误（它是循环分隔符而非 `in` 运算符）。

#### 4.3.3 源码精读

生产 + 入队（跳过注释）：

[src/njs_lexer.c:410-437](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L410-L437) — `njs_lexer_next_token`：分配一个 `njs_lexer_token_t`，`do { njs_lexer_make_token } while (type == COMMENT)` 跳过注释，再 `njs_queue_insert_tail(&lexer->preread, ...)` 入队，最后 `njs_lexer_in_stack` 维护括号栈。

取队首（消费语义的「读」）：

[src/njs_lexer.c:440-477](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L440-L477) — `njs_lexer_token`：先在 `preread` 队列里找第一个非行结束符的 token；队列空了就 `njs_lexer_next_token` 现产。注意它返回的是队列里的指针，**没有 `njs_queue_remove`**，所以不摘除。

前瞻（peek）：

[src/njs_lexer.c:480-515](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L480-L515) — `njs_lexer_peek_token`：从 `current->link` 的下一个节点开始找，结构与 `njs_lexer_token` 几乎一致，区别是起点是「`current` 之后」而不是「队首」，同样不摘除。

真正消费：

[src/njs_lexer.c:518-538](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L518-L538) — `njs_lexer_consume_token`：循环 `length` 次，每次 `njs_queue_remove` 队首 + `njs_mp_free` 释放，并把摘除 token 的类型记到 `lexer->prev_type`；行结束符不消耗 `length` 计数。

括号嵌套栈：

[src/njs_lexer.c:387-407](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L387-L407) — `njs_lexer_in_stack`：开括号 `push`、闭括号 `pop`。栈空间初始 128、按需翻倍扩容（[src/njs_lexer.c:326-356](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L326-L356)）。解析器通过 `njs_lexer_in_fail_get/set`（[src/njs_lexer.c:373-384](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L373-L384)）在当前嵌套层读写一个 fail 标志。

解析器侧的典型「peek + consume」用法：

[src/njs_parser.c:1127-1142](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L1127-L1142) — 处理 `NJS_TOKEN_ASYNC` 分支：先 `peek` 偷看下一个是不是 `function`；是的话 `consume_token(1)` 吃掉 `async`，再 `peek` 看下一个是不是 `*`（生成器）。这正是预读队列存在的意义。

再看一个更短的 peek 例子：

[src/njs_parser.c:1097-1098](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L1097-L1098) — `NJS_TOKEN_FUNCTION` 分支里 `peek` 一下个字符是不是 `*`，区分 `function` 与 `function*`。

#### 4.3.4 代码实践

**实践目标**：追踪解析器解析 `async function f(){}` 起始处时，预读队列里 token 的进出情况。

**操作步骤**（源码阅读型实践）：

1. 假设此时队首 token 是 `async`（`NJS_TOKEN_ASYNC`），跟随 [src/njs_parser.c:1127-1142](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L1127-L1142) 的逻辑逐步推演。
2. 第 1128 行 `next = njs_lexer_peek_token(parser->lexer, token, 1)`：这一步**不会**改变队列，只是让 `next` 指向 `async` 之后的 token，即 `function`。
3. 第 1133 行判断 `next->type == NJS_TOKEN_FUNCTION` 成立。
4. 第 1137 行 `njs_lexer_consume_token(parser->lexer, 1)`：**这时才**把队首的 `async` 摘掉并释放。现在队首变成 `function`。
5. 第 1139 行再次 `peek_token(parser->lexer, next, 0)`：偷看 `function` 之后是不是 `*`，结果是 `(`（不是 `*`），于是走普通 async 函数分支。

**需要观察的现象**：peek 不改变队列内容，consume 才真正缩短队列；多次 peek 同一区间不会重复词法化（因为 token 已经在 `preread` 里）。

**预期结果**：你能画出 `preread` 队列在每一步的快照——初始 `[async, function, (, ), {, }]`，consume 后变成 `[function, (, ), {, }]`，期间 peek 操作不改变它。

#### 4.3.5 小练习与答案

**练习 1**：`njs_lexer_token` 返回的指针，能不能在调用 `njs_lexer_consume_token` 之后继续使用？

**答案**：不能。`njs_lexer_consume_token`（[src/njs_lexer.c:534-536](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L534-L536)）会 `njs_queue_remove` 并 `njs_mp_free` 释放被消费的 token。一旦某 token 被消费，指向它的指针就变成悬空指针。所以解析器总是「peek/token 取指针 → 立刻用完 → consume」的模式，consume 之后不再引用旧指针。

**练习 2**：为什么 `njs_lexer_consume_token` 里行结束符（`NJS_TOKEN_LINE_END`）不消耗 `length` 计数？

**答案**：因为行结束符对解析器是「透明」的——解析器说「消费 1 个 token」时指的是 1 个**有意义的** token，行结束符（被 `with_end_line=0` 过滤掉的那类）不应占用这个计数。所以在 [src/njs_lexer.c:530-532](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L530-L532) 里只有「非行结束符」才让 `length--`，行结束符只被摘除但不减计数。

---

## 5. 综合实践

把三个模块串起来：手动当一次词法器，为下面这段极简源码产出完整 token 序列，并标注每个 token 是由哪条路径产出的。

```js
function add(a, ...rest) {
  return a + rest.length;
}
```

**任务**：

1. 逐字符推演，列出所有 token（含被跳过的空白/注释、被过滤的行结束符），每个 token 标注：`类型`、`text`、`line`（假设从第 1 行开始）、识别路径（`关键字表升级` / `njs_tokens 单字节` / `njs_lexer_multi 归约` / `DOT 特判` / `njs_lexer_number`）。
2. 重点验证这三个关键点：
   - `function` 走关键字表升级为 `NJS_TOKEN_FUNCTION`；
   - `...` 走 [src/njs_lexer.c:592-606](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_lexer.c#L592-L606) 的 DOT 特判变成 `NJS_TOKEN_ELLIPSIS`；
   - `+` 走 `njs_lexer_multi(njs_addition_token)`，因后随空格而非 `+`/`=`，保持 `NJS_TOKEN_ADDITION`。
3. 模拟解析器在 `function` 处做一次 `peek`（看下一个是不是 `*`），写出此时 `preread` 队列的快照，并指出这次 peek 不会改变队列。

**预期产出**：一张 token 表 + 一段队列快照说明。完成后，你应当能口头复述「字符 → njs_tokens → 子扫描器 → 入队 → peek/consume」的完整链路。

> 若本地已按 [u1-l3](u1-l3-build-and-run-cli.md) 构建出 `build/njs`，可额外用 `./build/njs -d -c 'function add(a){return a}'` 观察反汇编输出——注意 `-d` 显示的是**字节码**而非 token，但它能间接验证你的词法分析没有把源码切错（切错会在更早阶段报 SyntaxError）。

## 6. 本讲小结

- 词法器状态 `njs_lexer_t` 攥着源码区间 `[start,end)`、行号 `line`、预读队列 `preread` 和括号栈 `in_stack`；核心扫描函数 `njs_lexer_make_token` 先跳空白、再用 256 项表 `njs_tokens[c]` 做单字节分发。
- 多字符运算符（`=`、`<`、`+`…）由一组 `njs_lexer_multi_t` 静态表 + `njs_lexer_multi` 函数做「贪心最长匹配」归约；`/` 的除法/注释/赋值歧义由 `njs_lexer_division` 单独消除。
- token 类型用 `njs_token_type_t` 枚举表示，共 150+ 个；标识符先被 `njs_lexer_word` 扫成整词并驻留成 atom，再查关键字完美哈希表决定是 `NJS_TOKEN_NAME` 还是某个关键字 token。
- 关键字表由 `utils/lexer_keyword.py` 在构建期生成为 `src/njs_lexer_tables.h`，运行期用哈希 \(h(k)=((k_0\cdot k_{n-1})+n)\bmod T+1\) + 链式冲突解决查找。
- 预读队列 `preread` 把「词法化」和「消费」解耦：`njs_lexer_token`/`njs_lexer_peek_token` 只取不删，`njs_lexer_consume_token` 才真正摘除并释放，从而支持解析器的前瞻判定。

## 7. 下一步学习建议

下一讲 [u3-l2 递归下降解析器与 AST](u3-l2-parser-ast.md) 将承接本讲产出的 token 流，讲解 `njs_parser_t` 如何以递归下降方式把这些 token 组织成 AST 节点 `njs_parser_node_t`。建议在进入下一讲前，确保你能回答：

- 一个 token 从被 `njs_lexer_make_token` 扫出来，到被解析器 `consume_token` 吃掉，中间经过了哪些队列操作？
- 为什么解析器需要 `peek` 而不是直接「读下一个」？

若想加深对关键字表的理解，可以再读一遍 [utils/lexer_keyword.py](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/utils/lexer_keyword.py) 的 `SHS` 类——它演示了如何暴力搜索一个使冲突最少的表大小 `T`，这是「完美哈希」工程化的一种朴素实现。
