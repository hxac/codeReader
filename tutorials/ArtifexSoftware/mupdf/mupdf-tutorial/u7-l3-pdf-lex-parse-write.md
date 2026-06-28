# 讲义标题：词法分析、解析与文件写入

## 1. 本讲目标

PDF 本质上是一种**以文本为主**的格式：一个 `.pdf` 文件就是一连串形如 `12 0 obj << /Type /Page /MediaBox [0 0 595 842] >> endobj` 的对象，外加一张交叉引用表（xref）和文件尾。要让程序「读懂」PDF，就必须把字节流先翻译成内存里的对象树；要「写出」PDF，又要把对象树重新翻译回字节流。本讲就沿着这条**读入 ↔ 写出**的往返（round trip）展开。

读完本讲，你应该能够：

1. 说清 PDF 词法分析器 `pdf_lex` 如何用一个字符的向前看（lookahead）把字节流切成 `pdf_token`，以及它如何区分数字、名字、字符串、关键字。
2. 理解递归下降解析器 `pdf_parse_dict` / `pdf_parse_array` / `pdf_parse_ind_obj` 如何把 token 流组装成 `pdf_obj` 对象树，尤其是「`3 0 R` 这种间接引用」为什么要做向前看。
3. 掌握写入器 `pdf-write.c` 的重写管线：标记清扫式垃圾回收 → 去重 → 压缩 xref → 逐对象序列化 → 写 xref/trailer/startxref/`%%EOF`，并理解其中的流压缩与对象整理能力。

## 2. 前置知识

本讲是 u7 单元（PDF 对象模型深入）的第三篇，承接前两讲。开始之前请确认你理解：

- **`pdf_obj` 七种类型与单例优化**（u7-l1）。本讲的解析器产出的就是 `pdf_obj` 树，写入器消费的也是它。名词如 `PDF_NULL`、`PDF_NAME(Type)`、间接引用壳 `PDF_INDIRECT` 都来自那一讲。
- **xref 交叉引用表与 `pdf_resolve_indirect`**（u7-l2）。解析器只能构造出「`3 0 R`」这种**间接引用壳**，真正把它换成实体对象的工作由 xref 层的 `pdf_cache_object` 完成；而写入器最终又要重新生成一张 xref 表。本讲在两端都与 xref 衔接。
- **`fz_try` / `fz_catch` 异常机制**（u2-l3）。词法/解析/写入里随处可见 `fz_throw(FZ_ERROR_SYNTAX, ...)`，损坏的 PDF 会不断抛出语法异常，解析器靠它做「跳过坏对象继续」。
- **`pdf_document` 与 `fz_stream`**（u3、u8）。词法器从一个 `fz_stream *f` 逐字节读取；`pdf_document *doc` 则承载 xref 与 `lexbuf` 等解析上下文。

一句话回顾：PDF 文件 = 一堆带编号的对象 + 一张把它们编号映射到文件偏移的 xref 表。读入时，lex 切 token、parse 组对象、resolve 解间接引用；写出时，反过来序列化对象并重建 xref。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `include/mupdf/pdf/parse.h` | 76 | 公开接口：`pdf_token` 枚举、`pdf_lex`、各 `pdf_parse_*`、反向序列化 `pdf_append_token` 的声明 |
| `source/pdf/pdf-lex.c` | 736 | 词法分析器：字节流 → `pdf_token`，是「读」的第一步 |
| `source/pdf/pdf-parse.c` | 982 | 语法分析器：token 流 → `pdf_obj` 对象树（递归下降） |
| `source/pdf/pdf-write.c` | 3293 | 文件重写器：对象树 → 合法 PDF 文件（垃圾回收、去重、压缩 xref、序列化） |
| `include/mupdf/pdf/document.h` | — | `pdf_lexbuf` 缓冲区结构、`pdf_write_options` 选项结构、`pdf_default_write_options` |
| `source/pdf/pdf-object.c` | — | `pdf_print_obj` / `pdf_print_encrypted_obj`：把单个 `pdf_obj` 序列化为文本（写入器的基础设施） |
| `source/tools/pdfclean.c` | — | `mutool clean` 命令实现，是本讲「写」侧的实战入口 |

整条链路的对应关系：

```
读入:  fz_stream --pdf_lex--> pdf_token --pdf_parse_*--> pdf_obj 树 --(u7-l2 resolve)--> 实体
写出:  pdf_obj 树 --pdf_print_obj--> 文本 --writeobject--> "N G obj ... endobj" --writexref--> xref+trailer+%%EOF
```

注意两端的对称性：`pdf_lex`（token 化）的逆运算正好是同文件里的 `pdf_append_token`（token 反序列化），而对象级的逆运算是 `pdf_print_obj`。理解了这种「读 = 解析、写 = 序列化」的对偶，本讲就抓住主线了。

## 4. 核心概念与源码讲解

### 4.1 词法分析：字节流如何变成 token

#### 4.1.1 概念说明

PDF 对象的语法由「字符类别」定义。PDF 规范把字节分成四类：空白符（`IS_WHITE`）、数字起始符（`IS_NUMBER`，含 `+-.0-9`）、十六进制字符（`IS_HEX`）、分隔符（`IS_DELIM`，如 `()<>[]{}/%`）。词法分析器（lexer）的任务就是：每次从输入流读一个起始字节，根据它属于哪一类，决定要拼出哪一种 token，然后把后续「属于该 token」的字节吃进来，遇到分隔符或空白就停下。

MuPDF 把这一切实现成 `pdf-lex.c` 里的一组小函数 + 一个顶层 `pdf_lex`。它的产出不是字符串，而是一个**枚举值 `pdf_token`**，附带数据放进一个可复用的 `pdf_lexbuf` 缓冲区里。

#### 4.1.2 核心流程

`pdf_lex` 是一个状态极简的循环：每次读一个字节 `c`，按 `c` 的类别分发：

```
c == EOF          -> 返回 PDF_TOK_EOF
IS_WHITE          -> lex_white() 吃掉连续空白，继续循环
'%'               -> lex_comment() 吃掉一行注释，继续循环
'/'               -> lex_name() 拼名字，返回 PDF_TOK_NAME
'('               -> lex_string() 拼字面字符串，返回 PDF_TOK_STRING
'<'+'<'           -> 返回 PDF_TOK_OPEN_DICT（字典开始）
'<'(其它)         -> lex_hex_string() 拼十六进制字符串
'[' / ']'         -> PDF_TOK_OPEN_ARRAY / CLOSE_ARRAY
IS_NUMBER         -> lex_number() 拼数字，返回 INT 或 REAL
default(普通字符) -> 回退一字节，lex_name() 拼一个"词"，再交给 pdf_token_from_keyword 判断是关键字还是非法
```

关键点有两个：

1. **关键字靠 `pdf_token_from_keyword` 识别**。像 `obj`、`endobj`、`stream`、`R`、`true`、`null`、`trailer` 这些关键字，在词法层和普通「名字」长得一模一样（都是一串非分隔符字符）。词法器先把这一串字符按名字规则读进 `buf->scratch`，再用 `pdf_token_from_keyword` 做字符串匹配，命中就返回对应 token，否则返回 `PDF_TOK_KEYWORD`（保留给内容流里的操作符）。
2. **`pdf_lexbuf` 是可增长的复用缓冲区**。词法器把 token 的文本（名字、字符串内容等）写进 `lexbuf.scratch`；它默认用结构体内嵌的小数组 `buffer[256]`，超长时调用 `pdf_lexbuf_grow` 倍增到堆上。`pdf_document` 自带一个 `lexbuf.base`，跨多次 lex 复用，避免反复分配。

#### 4.1.3 源码精读

先看 token 枚举——这是整条链路的「词汇表」，词法、解析、写入三方都用它：

[include/mupdf/pdf/parse.h:28-56](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/parse.h#L28-L56) 定义了 `pdf_token` 枚举：`PDF_TOK_INT/REAL/STRING/NAME/KEYWORD` 是值类 token，`PDF_TOK_OPEN/CLOSE_ARRAY/DICT` 是结构类 token，`PDF_TOK_OBJ/ENDOBJ/STREAM/ENDSTREAM/XREF/TRAILER/STARTXREF` 是 PDF 文件骨架关键字，`PDF_TOK_R` 专门表示间接引用里的那个 `R`。

字符分类靠一组 `case` 宏，这是词法器判断「一个字节属于哪类」的依据：

[source/pdf/pdf-lex.c:30-42](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-lex.c#L30-L42) 用 `IS_WHITE` / `IS_NUMBER` / `IS_HEX` / `IS_DELIM` 四个宏列出了 PDF 的字符类别。注意 `IS_NUMBER` 把 `+ - .` 也算作数字起始符——这正是 PDF 里 `-5`、`3.14`、`.5` 都被当作数字的原因。

顶层分发表是这个文件的心脏：

[source/pdf/pdf-lex.c:585-638](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-lex.c#L585-L638) 是 `pdf_lex`。`switch (c)` 按起始字节分发；尤其看 `default` 分支（[L632-636](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-lex.c#L632-L636)）：先 `fz_unread_byte` 回退一字节，再 `lex_name` 把这一串当名字读进来，最后 `pdf_token_from_keyword(buf->scratch)` 决定它是关键字还是 `KEYWORD`。这就是「名字与关键字同源」的实现。

数字识别在 `lex_number` 里，它要同时判整数还是浮点：

[source/pdf/pdf-lex.c:189-267](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-lex.c#L189-L267) 是 `lex_number`。它用 `isreal` 标记是否遇到过小数点（L194、L219-224），结束时（L247-266）若 `isreal` 就 `fz_atof` 转 float 返回 `PDF_TOK_REAL`，否则 `fast_atoi` 转 int64 返回 `PDF_TOK_INT`。注意 L226-233 对 `0.000000000000-5684342` 这类 Google Docs 生成的畸形数字做了兼容：遇到中间的 `-` 就截断。数字结果存进 `buf->i`（整数）或 `buf->f`（浮点），解析器据此 `pdf_new_int` / `pdf_new_real`。

字面字符串（圆括号包裹）的处理最能体现「逐状态推进」：

[source/pdf/pdf-lex.c:351-458](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-lex.c#L351-L458) 是 `lex_string`。`bal` 计数器（L357）处理嵌套括号——遇到 `(` 加一、`)` 减一，归零才结束（L372-381）；反斜杠转义（L382-438）处理 `\n\r\t\b\f\(\)\\` 以及最多三位的八进制 `\123`（L412-427）。这套转义规则和 JSON/C 的字符串转义很像，但多了 PDF 特有的八进制与行续接。

关键字的二次判定：

[source/pdf/pdf-lex.c:510-553](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-lex.c#L510-L553) 是 `pdf_token_from_keyword`。它先按首字母 `switch` 快速分流，再 `strcmp` 精确匹配：`R`→`PDF_TOK_R`、`true`→`PDF_TOK_TRUE`、`obj`→`PDF_TOK_OBJ` 等等。这是「同一个字节串 `lex_name`，靠这一步区分成不同 token」的落点。

缓冲区增长机制：

[source/pdf/pdf-lex.c:568-583](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-lex.c#L568-L583) 是 `pdf_lexbuf_grow`，把 `scratch` 倍增。首次增长时把内嵌 `buffer` 的内容拷到新堆内存（L572-576），之后直接 `fz_realloc`（L578-580）。这保证了短 token 用栈上小数组、超长字符串/名字才落到堆。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：亲手把一段 PDF 文本「过一遍」`pdf_lex`，验证你理解了字符分类与分发。

**操作步骤**：

1. 取一段真实 PDF 片段，例如某文件尾的 trailer：
   ```
   trailer
   << /Size 12 /Root 2 0 R /Info 1 0 R >>
   startxref
   567
   %%EOF
   ```
2. 假装自己是 `pdf_lex`，对这段文本逐 token 走一遍，把每个 token 的 `pdf_token` 枚举值和它在 `lexbuf` 里留下什么（`scratch` 文本 / `i` / `f`）填进下表：

   | 输入片段 | 起始字节走的分支 | 产出 token | lexbuf 里留下 |
   | --- | --- | --- | --- |
   | `trailer` | default → lex_name → keyword | `PDF_TOK_TRAILER` | scratch=`"trailer"` |
   | `<<` | `<` 再读一个 `<` | `PDF_TOK_OPEN_DICT` | — |
   | `/Size` | `/` → lex_name | `PDF_TOK_NAME` | scratch=`"Size"` |
   | `12` | IS_NUMBER → lex_number | `PDF_TOK_INT` | i=`12` |
   | `/Root` | `/` → lex_name | `PDF_TOK_NAME` | scratch=`"Root"` |
   | `2` | IS_NUMBER → lex_number | `PDF_TOK_INT` | i=`2` |
   | `0` | IS_NUMBER → lex_number | `PDF_TOK_INT` | i=`0` |
   | `R` | default → lex_name → keyword | `PDF_TOK_R` | — |
   | … | … | … | … |

3. 用 `mutool show` 把同一个 PDF 的 trailer 打印出来，对照你推演的「键值」结构：
   ```bash
   mutool show trailer your.pdf
   ```
   预期会看到 `/Size`、`/Root`、`/Info` 等键与它们的（已 resolve 后的）值——这正是解析器把上面那些 token 组装成字典、再经 `pdf_resolve_indirect` 解开 `2 0 R` 之后的结果。

**需要观察的现象**：`/Root` 后面跟着 `2 0 R` 三个 token（INT、INT、R），它们组合表示「指向 2 号对象」的间接引用——这正是下一小节解析器要靠「向前看两个 INT」才能识别的语法。

**预期结果**：你能准确预测每个 token 的类别，并理解 `R` 之所以是独立 token，是为了让解析器能把「整数 整数 R」整体识别成一个间接引用。具体命令输出**待本地验证**（取决于你用的 PDF）。

#### 4.1.5 小练习与答案

**练习 1**：`pdf_lex` 遇到字节 `<` 时，为什么还要再读一个字节？请结合 `<<`（字典）和 `<48656C6C6F>`（十六进制字符串）说明。

**答案**：因为 `<` 既是字典开始符 `<<` 的首字节，也是十六进制字符串的开始符。`pdf_lex` 在 [L608-614](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-lex.c#L608-L614) 多读一字节：若是 `<` 则返回 `PDF_TOK_OPEN_DICT`，否则 `fz_unread_byte` 回退并交给 `lex_hex_string`。这就是「一个字符向前看」的典型用法。

**练习 2**：为什么词法器把 `obj`、`R`、`true` 这些关键字和普通「名字」用同一段代码（`lex_name`）读取？

**答案**：因为在字符层面它们无法区分——都是「非分隔符、非空白的一串字符」。`lex_name` 负责把这串字符读进 `scratch`，分类推迟到 `pdf_token_from_keyword`（[L510-553](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-lex.c#L510-L553)）。这种「先读后分」避免了在词法层维护关键字表的状态机。

---

### 4.2 语法解析：token 流如何组装成对象树

#### 4.2.1 概念说明

词法器只给你一个个孤立的 token，但 PDF 对象是有**嵌套结构**的：字典里有数组、数组里有字典、值可以是间接引用。语法分析器（parser）的工作就是用**递归下降**把这些 token 组织成 `pdf_obj` 树。

MuPDF 的解析器核心是三个互相递归的函数：

- `pdf_parse_dict`：读到 `<<` 开始，遇 `>>` 结束；内部反复读「名字（key）+ 值（value）」。
- `pdf_parse_array`：读到 `[` 开始，遇 `]` 结束；内部反复读值。
- `pdf_parse_ind_obj`：解析文件级对象信封 `N G obj <对象> endobj|stream`。

其中最微妙的一点是**间接引用的向前看**。在 PDF 里，`3 0 R` 表示「3 号 0 代对象」，它由**连续三个 token**（INT、INT、R）组成。但解析器读到第一个 INT 时并不知道它是普通整数、还是某个间接引用的开头——必须继续读，看后面是不是「INT R」才能判定。MuPDF 用一个小状态机（`a`、`b`、`n` 三个变量）实现这套向前看。

#### 4.2.2 核心流程

以 `pdf_parse_dict` 为例的递归下降骨架：

```
pdf_parse_dict(doc, f, buf):
    新建空字典 dict
    循环:
        tok = pdf_lex(f, buf)            # 读 key
        若 tok == CLOSE_DICT: 跳出（字典结束）
        若 tok != NAME: 抛 SYNTAX 异常（字典的 key 必须是名字）
        key = pdf_new_name(buf->scratch)

        tok = pdf_lex(f, buf)            # 读 value 的第一个 token
        根据 tok 分派:
            OPEN_ARRAY -> val = pdf_parse_array(...)   # 递归
            OPEN_DICT  -> val = pdf_parse_dict(...)    # 递归
            NAME/REAL/STRING/TRUE/FALSE/NULL -> 直接构造
            INT -> 向前看：可能只是整数，也可能是 "INT INT R" 间接引用
        pdf_dict_put(dict, key, val)
    返回 dict
```

间接引用的向前看逻辑（INT 分支）是关键：读到 INT 后，先存为 `a`，再读下一个 token；如果它是 `CLOSE_DICT` 或 `NAME`，说明 `a` 就是个普通整数（作为值），回填后继续；如果它是又一个 INT，存为 `b`，再读第三个——若是 `R`，则整体是 `pdf_new_indirect(doc, a, b)`，否则报「无效间接引用」。这套「试探-回退」在数组和对象信封里各有一份近乎相同的实现。

文件级对象信封 `pdf_parse_ind_obj` 还多做一件事：解析完对象本体后，判断后续是 `stream`（流对象）还是 `endobj`（普通对象）。若是 `stream`，它把**流数据的起始文件偏移** `stm_ofs` 记录下来返回给调用者（xref 层据此按需读取流）——注意它**不**读流内容，只记位置。

#### 4.2.3 源码精读

`pdf_parse_array` 展示了递归 + 间接引用向前看的完整实现：

[source/pdf/pdf-parse.c:555-657](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L555-L657) 是 `pdf_parse_array`。变量 `a`、`b`、`n`（L560）实现向前看：`n` 表示「已积攒了几个连续 INT」。看 [L574-588](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L574-L588)：当遇到非 INT/非 R 的 token 时，把积攒的 `a`、`b` 作为普通整数压入数组；[L607-612](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L607-L612) 处理 `R`——只有当 `n==2`（即前面有两个 INT）时才合法，否则抛 `cannot parse indirect reference`。`pdf_array_push_drop` 在压入后顺手 drop，保持引用计数平衡（呼应 u2-l2）。

`pdf_parse_dict` 的结构与数组类似，但 key 固定为名字：

[source/pdf/pdf-parse.c:659-756](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L659-L756) 是 `pdf_parse_dict`。注意 [L683-684](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L683-L684)：它特别处理内容流里的 `BI ... ID ... EI` 内联图像语法——遇到 `ID` 关键字就提前结束，因为内联图像的图像数据不是普通 token。INT 分支（[L710-734](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L710-L734)）的向前看与数组版本同理：读到 INT 后再看一个 token，是 `CLOSE_DICT/NAME/ID` 就当普通整数并 `goto skip` 回到循环顶（L720），是 INT 再看是不是 `R`。

文件级对象信封解析器最「有料」：

[source/pdf/pdf-parse.c:782-929](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L782-L929) 是 `pdf_parse_ind_obj_or_newobj`（`pdf_parse_ind_obj` 是它的薄封装，见 [L931-936](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L931-L936)）。它依次期望 `INT(对象号) INT(代号) obj`（L796-836），任一不符就置 `*try_repair=1` 并抛异常——这正是 xref 层触发 `pdf_repair_xref`（见 u7-l2）的信号。读到 `stream` 关键字后（[L893-907](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L893-L907)），它仔细处理 `stream` 后的换行：吞掉空格，若是 `\r` 还要确认紧跟 `\n`（PDF 规范要求），然后用 `fz_tell` 记下流数据起始偏移 `stm_ofs` 返回——它**不读流字节**，只把位置告诉调用方。

`pdf_parse_stm_obj` 是给对象流（ObjStm）用的简化版，因为对象流里的对象没有 `N G obj` 信封：

[source/pdf/pdf-parse.c:758-780](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L758-L780) 是 `pdf_parse_stm_obj`，它直接根据第一个 token 构造对象，没有间接引用向前看（对象流内不允许间接引用）。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：跟踪一段含间接引用的字典是如何被解析的，验证你理解了「INT 向前看」。

**操作步骤**：

1. 准备一个字典片段（典型 PDF 的 Catalog）：
   ```
   <<
     /Type /Catalog
     /Pages 3 0 R
     /Count 5
     /Lang (zh-CN)
   >>
   ```
2. 打开 [pdf-parse.c 的 `pdf_parse_dict`](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L659-L756)，手动跟踪解析过程：
   - 读 `<<` → 进入函数。
   - 读 `/Type`（NAME）作 key → 读 `/Catalog`（NAME）作 value → `pdf_dict_put`。
   - 读 `/Pages`（NAME）作 key → 读 `3`（INT，存 `a=3`）→ 再读 `0`（INT，存 `b=0`）→ 再读 `R` → 命中 [L726-730](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L726-L730)，构造 `pdf_new_indirect(doc, 3, 0)`。
   - 读 `/Count`（NAME）作 key → 读 `5`（INT，存 `a=5`）→ 再读 `/Lang`（NAME）→ 走 [L714-721](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L714-L721)，把 `5` 当普通整数 `pdf_dict_put_int`，`goto skip` 回到循环顶，`/Lang` 当作下一个 key 复用。
3. 对照 `mutool show your.pdf 2`（2 号对象通常是 Catalog）确认你追踪的结构与现实一致。

**需要观察的现象**：`/Count 5` 后面跟的是名字 `/Lang` 而非数字，所以解析器判定 `5` 只是整数、不会把它误认成间接引用的开头——这正是向前看机制的正确性来源。`goto skip`（L720）复用了那个「多读出来的」`/Lang` token，避免丢失。

**预期结果**：你能在脑中（或纸上）画出解析这五行文本时，每一步 `tok` 取值、`a/b` 取值、以及最终字典的 4 个键值对。输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`pdf_parse_ind_obj` 读到 `stream` 关键字后，为什么只记下 `stm_ofs` 而不读流内容？

**答案**：流可能很大（嵌入字体、图像），且可能被 `FlateDecode` 等压缩。解析器此时只关心**流数据在文件里的起始位置**，真正的解码/解压交给 xref 层和 u8 单元的 filter 管线按需进行（懒加载，呼应 u7-l2 的 `pdf_cache_object`）。立刻读流会浪费内存、也违背 PDF 的按需加载设计。

**练习 2**：如果 PDF 文件损坏，`5 0 obj << /A /B >>` 里少了 `obj` 写成了 `5 0 << /A /B >>`，解析器会怎样？

**答案**：`pdf_parse_ind_obj_or_newobj` 在 [L831-836](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c#L831-L836) 发现第三个 token 不是 `PDF_TOK_OBJ`，就置 `*try_repair=1` 并 `fz_throw(FZ_ERROR_SYNTAX, "expected 'obj' keyword")`。xref 加载流程捕获该异常后，会进入 `pdf_repair_xref` 修复模式——扫描整个文件重新拼装 xref 表。这是 MuPDF 对损坏 PDF 韧性的关键一环。

---

### 4.3 文件重写：对象树如何序列化回 PDF

#### 4.3.1 概念说明

读入侧（lex + parse）建好了对象树，但很多操作——`mutool clean`、`mutool merge`、增量保存、加密、改写——都需要把对象树**重新写成一个合法 PDF 文件**。这就是 `pdf-write.c` 的职责。它不是简单地把对象逐个打印出来，而是一条完整的**重写管线**，包含三大优化能力：

1. **垃圾回收（garbage collection）**：很多 PDF 经过编辑后会留下「从 trailer 根对象出发不可达」的孤儿对象。`do_garbage` 选项用标记清扫（mark and sweep）找出所有可达对象，丢弃其余。
2. **去重与压缩 xref（deduplicate / compact）**：找出内容完全相同的对象合并成一个；然后把仍存活的对象重新连续编号，让 xref 表更紧凑。
3. **流压缩（compress）**：给未压缩的流加上 `FlateDecode`（zlib）/ `BrotliDecode` / `CCITTFaxDecode` 过滤器，缩小体积。

最后，逐对象序列化（`writeobject` 调 `pdf_print_obj`），重建 xref 表与 trailer，写出 `startxref` 与 `%%EOF` 收尾。

理解 `pdf-write.c` 的关键，是抓住它**对偶于读入侧**：读入是「字节 → token → 对象」，写出是「对象 → 文本 → 文件」。对象级的序列化靠 `pdf_print_obj`（在 `pdf-object.c`），它把 `pdf_obj` 树按 PDF 语法重新打印成 `<< /Key /Value >>` 这样的文本。

#### 4.3.2 核心流程

`pdf-write.c` 的总入口是 `pdf_write_document`，真正干活的是 `do_pdf_save_document`，其管线可概括为：

```
do_pdf_save_document(doc, opts):
    1. pdf_check_document()        # 预加载所有对象，触发可能的 repair
    2. initialise_write_state()    # 建 use_list/ofs_list/gen_list/renumber_map 四张表
    3. 处理加密/解密/ID            # 按 do_encrypt 选项调整 Encrypt 字典
    4. preloadobjstms()           # 把对象流里的对象都拉进内存
    5. do {                         # 反复直到收敛
         若 do_garbage >= 1:
             bake_stream_length()  # 把流上的间接 /Length 烤成内联整数
             markobj(trailer)       # 从 trailer 根做标记清扫，填 use_list
         若 do_garbage >= 3:
             changed = removeduplicateobjs()   # 内容相同的对象合并
         若 do_garbage >= 2:
             compactxref()          # 存活对象重新连续编号
             renumberobjs()         # 把新编号写回所有间接引用
       } while (changed)
    6. 若 do_use_objstms: gather_to_objstms()   # 把小对象塞进对象流
    7. writeobjects()              # 逐对象序列化: "N G obj\n <对象> \nendobj\n"
    8. writexref() 或 writexrefstream()   # 写 xref 表 + trailer + startxref + %%EOF
```

四张核心表（`pdf_write_state` 的 `use_list`/`ofs_list`/`gen_list`/`renumber_map`）贯穿始终：`use_list[num]` 标记对象是否存活，`ofs_list[num]` 记录它在输出文件里的偏移，`gen_list[num]` 是代号，`renumber_map[num]` 把旧编号映射到压缩后的新编号。

垃圾回收的级别由 `do_garbage` 控制：`1`=只清扫、`2`=再压缩 xref（renumber）、`3`=再去重（deduplicate）、`4`=深度比较去重。这就是 `mutool clean -g`（加一个 `-g` 升一级）的语义来源。

#### 4.3.3 源码精读

先看写入状态结构体，理解四张表：

[source/pdf/pdf-write.c:43-82](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L43-L82) 是 `pdf_write_state`。`use_list`/`ofs_list`/`gen_list`/`renumber_map`（L63-67）就是上文说的四张表；其余字段控制各种行为开关（`do_garbage`/`do_compress`/`do_encrypt` 等）与加密上下文。

标记清扫的入口：

[source/pdf/pdf-write.c:188-236](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L188-L236) 是 `markobj`。它从某个对象（最初是 trailer）出发，递归遍历所有间接引用，把遇到的每个对象编号在 `use_list` 里置 1。配套的 `markref`（[L139-](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L139)）处理单个引用：已标记的返回 NULL 避免重复，无效引用（编号越界或解出来是 null）置 `*duff` 让调用方删除该引用。这正是「从根可达」的形式化。

去重逻辑：

[source/pdf/pdf-write.c:237-293](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L237-L293) 是 `removeduplicateobjs`。它对每个存活对象 `a`，与编号更小的对象 `b` 比较内容：`do_garbage>=4` 用 `pdf_objcmp_deep`（递归深入比较，把间接引用解开），否则用浅比较 `pdf_objcmp`（[L265-274](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L265-L274)）。内容相同就把两者都映射到较小编号（[L281-284](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L281-L284)），并停用较晚的那个。**关键保护**在 [L277-278](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L277-L278)：`/Type /Page` 的对象**永远不去重**——否则多页文档的页会被错误合并成一页。

压缩 xref（重编号）：

[source/pdf/pdf-write.c:301-337](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L301-L337) 是 `compactxref`。它把所有存活对象聚拢到低编号：遍历 `renumber_map`，存活且未移动的重新赋连续 `newnum`，未存活的映射到 0（[L316-336](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L316-L336)）。注释（L295-299）点明前提：`renumber_map[n] <= n` 总成立，所以原地更新是安全的。

逐对象序列化与流压缩决策：

[source/pdf/pdf-write.c:1224-1323](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L1224-L1323) 是 `writeobject`。非流对象走 [L1303-1305](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L1303-L1305)：打印 `N G obj\n`、调 `pdf_print_encrypted_obj` 序列化对象、再 `\nendobj\n\n`。流对象走 `copystream`/`expandstream`（L1296-1299），压缩决策在 [L1285-1294](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L1285-L1294)：图片/字体流按 `do_compress_images/fonts` 单独决定，XML 元数据和 JPX 流强制不压缩。

`copystream` 是流压缩的具体落点：

[source/pdf/pdf-write.c:937-1019](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L937-L1019) 是 `copystream`。读原始流（`pdf_load_raw_stream_number`，L954），若 `do_deflate` 且当前无 Filter（L965）：单色位图走 CCITT fax（[L967-974](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L967-L974)），`do_deflate==1` 走 Flate（[L975-980](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L975-L980)），否则走 Brotli（[L982-990](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L982-L990)），并相应改写对象的 `/Filter` 字典。这就是「写入器对流的压缩」的真正实现。

xref 表与文件尾：

[source/pdf/pdf-write.c:1325-1337](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L1325-L1337) 是 `writexrefsubsect`，逐行打印 `%010lu %05d n \n`（在用）或 `f `（空闲）——这是 PDF 老式文本 xref 表的格式，10 位偏移、5 位代号、状态字母。[source/pdf/pdf-write.c:1339-1442](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L1339-L1442) 是 `writexref`：写 `xref\n`、各子段、`trailer\n`、trailer 字典（`pdf_print_obj`，注意 trailer **不加密**）、最后 `startxref\n<偏移>\n%%EOF\n`（[L1430](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L1430)）。到这一步，一个完整可读的 PDF 文件就重建好了。

总管线的编排：

[source/pdf/pdf-write.c:2603-2874](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L2603-L2874) 是 `do_pdf_save_document`。注意 [L2719-2749](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L2719-L2749) 的 `do { ... } while (changed)` 循环：去重可能产生新的可合并对象，所以要反复直到收敛（`changed` 为假）。这是「管线可能需要多趟」的体现。

选项怎么从字符串解析成结构体：

[source/pdf/pdf-write.c:2115-2130](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L2115-L2130) 是 `pdf_parse_write_options`，它把 `"garbage=3,compress,ascii"` 这种逗号串经 `fz_new_options` 拆成 `fz_options`，再由 `pdf_apply_write_options`（[L2047-2113](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L2047-L2113)）逐键填入 `pdf_write_options`。`garbage` 键既接受布尔也接受整数也接受 `compact/deduplicate` 枚举（[L2100-2105](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L2100-L2105)），这是 u6-l2 提到的 `-O` 选项统一解析机制。完整选项清单见 [fz_pdf_write_options_usage（L1982-2008）](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L1982-L2008)。

对象级序列化的底层：

[source/pdf/pdf-object.c:3755-3758](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L3755-L3758) 是 `pdf_print_obj`，它转调 `pdf_print_encrypted_obj`（[L3739-3753](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-object.c#L3739-L3753)），后者调 `pdf_sprint_encrypted_obj` 把 `pdf_obj` 树格式化成文本。这就是「对象 → 文本」的逆运算，与 4.1 的 `pdf_lex`（文本 → token）恰好对偶。

#### 4.3.4 代码实践（可运行）

**实践目标**：用 `mutool clean` 重写一个 PDF，对比清理前后文件大小与对象数量，亲眼看到垃圾回收、去重、流压缩的效果。

**操作步骤**：

1. 准备一个体积偏大、经过多次编辑的 PDF（`input.pdf`）。先记录原始信息：
   ```bash
   ls -l input.pdf                       # 原始字节数
   mutool show input.pdf trailer         # 看 /Size（≈ 对象总数 + 1）
   ```
2. 用默认选项重写（仅整理，不做强优化）：
   ```bash
   mutool clean input.pdf out-default.pdf
   ls -l out-default.pdf
   ```
3. 逐级加强垃圾回收（`-g` 每加一个升一级：1=清扫、2=压缩 xref、3=去重）：
   ```bash
   mutool clean -gg input.pdf out-gc.pdf     # 两个 g => do_garbage=2
   mutool clean -ggg input.pdf out-dedup.pdf # 三个 g => do_garbage=3
   ```
4. 加上流压缩与内容流清理：
   ```bash
   mutool clean -gggg -z -c input.pdf out-full.pdf   # -z 压缩流, -c 清理内容流
   ls -l out-*.pdf
   ```
   `-z` 对应 `pdfclean.c` 里 `opts.write.do_compress += 1`（[source/tools/pdfclean.c:147](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/pdfclean.c#L147)），最终走进 `copystream` 的 Flate 分支。
5. 反向验证：把压缩后的流再解出来，确认内容无损：
   ```bash
   mutool clean -d out-full.pdf out-decompressed.pdf   # -d 解压所有流
   mutool show out-decompressed.pdf trailer
   ```

**需要观察的现象**：

- `out-default.pdf` 通常和原始差不多（甚至略大，因为 MuPDF 重写了文件结构、可能解开了对象流）。
- `out-gc.pdf`（`-gg`）应比 `out-default.pdf` 小，因为孤儿对象被清扫、xref 被压缩重编号。
- `out-dedup.pdf`（`-ggg`）对有大量重复对象（如重复字体、重复颜色空间）的 PDF 会进一步缩小。
- `out-full.pdf`（`-gggg -z -c`）对含未压缩流的 PDF 缩小最明显——这是 `copystream` 加 `FlateDecode` 的功劳。
- 各文件的 `trailer` 里 `/Size` 会随垃圾回收下降，反映存活对象数减少。

**预期结果**：你能得到一张「选项 → 文件大小 → /Size」的对照表，直观看到垃圾回收与压缩各自的贡献。具体数字**待本地验证**（取决于 `input.pdf` 的内容）。若你的 PDF 本来就已高度压缩且无冗余，各文件大小可能接近——这本身也说明「写入器的优化只在有冗余时才见效」。

> **小提示**：若想观察 xref 重编号的效果，对比 `mutool show input.pdf` 与 `mutool show out-gc.pdf` 列出的对象编号——压缩后对象会聚拢到 1、2、3… 连续编号，中间不再有空缺。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `removeduplicateobjs` 要专门跳过 `/Type /Page` 的对象，即使两个页对象内容完全相同？

**答案**：因为 PDF 的页树里**每个页对象必须独立存在**，页码由页树结构决定。若把两个内容相同的页去重为一个，页树里两处 `/Kids` 引用会指向同一个对象，导致文档页数减少、两页内容同步变化。`pdf-write.c` 在 [L277-278](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c#L277-L278) 用 `pdf_name_eq(... Type ... Page)` 显式排除，这是「正确性优先于体积」的典型权衡。

**练习 2**：`do_pdf_save_document` 里那个 `do { ... } while (changed)` 循环，为什么去重可能需要多趟？

**答案**：去重把两个对象合并为一个后，原本指向「被合并方」的间接引用会被改写；改写后可能又产生新的内容相同的对象对（例如两个字典原本因指向不同间接引用而不同，去重改写后变得相同）。所以一趟去重后要重新标记清扫、再比较，直到一趟里没有新合并（`changed` 为 0）才算收敛。这保证了去重的彻底性。

**练习 3**：`pdf_append_token`（在 `pdf-lex.c`）和 `pdf_print_obj`（在 `pdf-object.c`）有什么区别？为什么都在「写出」时有用？

**答案**：`pdf_append_token`（[pdf-lex.c:695-736](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-lex.c#L695-L736)）把**单个 token** 反序列化回文本（如把 `PDF_TOK_INT` + `buf->i` 打印成 `"12"`），是 `pdf_lex` 的逐 token 逆运算，常用于内容流操作符的低级重写。`pdf_print_obj` 把**整个 `pdf_obj` 对象树**序列化成文本（递归打印字典/数组/值），是 `pdf_parse_*` 的对象级逆运算，被 `writeobject`/`writexref` 用来写对象体与 trailer。二者粒度不同，但都是「读入侧函数的逆」——这正是 PDF 读写出入对称的体现。

## 5. 综合实践

把三个模块串起来，完成一次「读入 → 分析 → 重写」的完整往返。

**任务**：拿一个 PDF，先用读入侧工具观察它的对象结构，再用写入侧重写并量化优化效果，最后回到读入侧确认无损。

**步骤**：

1. **读入观察**（对应 4.1/4.2）：
   ```bash
   mutool show input.pdf trailer        # 看 trailer 字典（解析器的产物）
   mutool show input.pdf 1              # 看 1 号对象，注意里面的 "N G R" 间接引用
   ```
   - 解释你看到的 `mutool show input.pdf 1` 输出里，哪些是字典键（NAME token）、哪些值是间接引用（解析时走了 INT-INT-R 向前看）。

2. **重写优化**（对应 4.3）：
   ```bash
   mutool clean -gggg -z -c -f -i input.pdf optimized.pdf
   ```
   - `-gggg`（`do_garbage=4`，深度去重）、`-z`（压缩流）、`-c`（清理内容流）、`-f`/`-i`（压缩字体/图片流）。
   - 记录 `input.pdf` 与 `optimized.pdf` 的大小、`/Size`、对象数。

3. **无损验证**（回到读入侧）：
   ```bash
   mutool draw -o proof-%d.png optimized.pdf    # 渲染每一页为 PNG
   ```
   - 把 `optimized.pdf` 的渲染结果与原始 PDF 对照，确认内容一致——证明 lex/parse 重建的对象树经 write 序列化后仍表达同一份文档。

4. **写一段总结**：说明在这次重写中，「垃圾回收」「去重」「流压缩」三者各自贡献了多少字节（用各选项组合的文件大小相减估算），并指出哪一类优化对你的 PDF 最有效。

**预期**：你会真切体会到——PDF 文件的「大」往往来自冗余对象与未压缩流，而 `pdf-write.c` 的重写管线正是对症下药；同时，「读入侧解析出的对象树」与「写出侧序列化回的文件」是同一份逻辑文档的两种表示，往返无损是这条链路正确性的根本要求。所有具体数值**待本地验证**。

## 6. 本讲小结

- PDF 是文本格式，处理它就是「读入 = 字节 → token → 对象树」「写出 = 对象树 → 文本 → 文件」的往返。
- `pdf_lex`（[pdf-lex.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-lex.c)）用一个字符的向前看和字符分类宏（`IS_WHITE/NUMBER/HEX/DELIM`）把字节流切成 `pdf_token`；名字与关键字同源，靠 `pdf_token_from_keyword` 二次判定。
- `pdf_parse_dict/array/ind_obj`（[pdf-parse.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-parse.c)）用递归下降组装 `pdf_obj` 树；间接引用「INT INT R」靠 `a/b/n` 向前看识别；`pdf_parse_ind_obj` 遇 `stream` 只记偏移、不读流（懒加载）。
- 解析出错时置 `try_repair` 并抛 `FZ_ERROR_SYNTAX`，是 xref 层触发 `pdf_repair_xref` 修复的信号（呼应 u7-l2）。
- `do_pdf_save_document`（[pdf-write.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-write.c)）是重写管线：标记清扫垃圾回收 → 去重（跳过 Page）→ 压缩 xref 重编号 → 流压缩（Flate/Brotli/CCITT）→ 逐对象 `pdf_print_obj` 序列化 → 写 xref+trailer+`%%EOF`，去重可能多趟直到收敛。
- `pdf_append_token`（token 级）与 `pdf_print_obj`（对象级）分别是 `pdf_lex` 与 `pdf_parse_*` 的逆运算，体现了 PDF 读写两端的对称性。

## 7. 下一步学习建议

本讲把 PDF 的「文本处理」链路（lex → parse → write）讲完了。接下来：

- **u7-l4 资源、页面与内容流解释**：本讲的 lex/parse 是「文件级」语法；u7-l4 会进入**页面内容流**的词法与解释——内容流操作符（`m`/`l`/`re`/`Tj` 等）也走 lex，但由 `pdf-op-run.c` 解释成 device 回调，是渲染的真正起点。
- **u8 单元（流、过滤与压缩）**：本讲只提到 `copystream` 给流加 `FlateDecode`；u8 会深入 `fz_stream` 与 filter 管线，讲清流的解压/解码/预测是如何链式串联的，是读入侧流的底层。
- **继续阅读源码**：想加深理解，建议通读 `pdf-lex.c` 的 `lex_string`（转义处理最绕）、`pdf-parse.c` 的 `pdf_parse_ind_obj_or_newobj`（信封解析最全）、`pdf-write.c` 的 `removeduplicateobjs` + `compactxref`（对象整理的精华）。三者分别对应「读的边界」「读的主体」「写的优化」。
