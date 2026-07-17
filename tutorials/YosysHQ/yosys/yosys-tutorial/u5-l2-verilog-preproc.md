# Verilog 预处理器与常数表达式

## 1. 本讲目标

上一讲（u5-l1）我们走通了 Verilog 前端「文本 → 词法 → 语法 → AST」的主干，并强调 `read_verilog` **并不直接**产出 RTLIL，而是先建一棵 AST。本讲聚焦在这条流水线的最前端——**预处理器**，以及紧随其后的**常数解析**。读完本讲你应当能够：

1. 说清楚预处理器在 `read_verilog` 内部的位置，以及它「先于词法分析」运行的原因。
2. 理解 `` `define ``、带参数宏、`` `undef ``、`` `include ``、`` `ifdef `` / `` `ifndef `` / `` `elsif `` / `` `else `` / `` `endif `` 是如何被处理的。
3. 掌握 `define`（per-call `-D`）与全局宏（`verilog_defines` 命令、`verilog_defaults` 命令）两套 define 机制的差别与汇合点。
4. 理解 `const2ast` 如何把一段 Verilog 常数字面量（如 `8`、`8'h3F`、`4'b10xz`）翻译成一个 `AST_CONSTANT` 节点。
5. 会用 `read_verilog -ppdump` 观察预处理后的真实输出，定位宏展开与条件编译的实际效果。

## 2. 前置知识

本讲默认你已掌握 u5-l1 的内容，尤其是这几个概念：

- **AST（抽象语法树）**：Verilog 文本经词法/语法分析后得到的树形中间结构，根节点是 `AST_DESIGN`，常量叶节点类型是 `AST_CONSTANT`。
- **token / 产生式 / 归约**：词法器把文本切成 token，语法器按产生式把 token 归约成 AST 节点。
- **前端三件套**：`verilog_frontend.cc`（注册 `read_verilog`）、`verilog_lexer.l`（flex 词法）、`verilog_parser.y`（bison 语法）。

此外需要一点 C++ 直觉：

- **pushback buffer（回退缓冲区）**：一个「可以往前塞字符、再被重新读出」的字符队列。这是理解整个预处理器工作方式的关键，下面会详细讲。
- **四值逻辑**：Verilog 的每一位可以是 `0/1/x/z`（以及 Yosys 内部额外的 `a/m`），对应 `RTLIL::State` 的 `S0/S1/Sx/Sz/Sa/Sm`（见 u3-l3）。

> 关于命令名的小提醒：本讲实践里提到的「观察预处理输出」对应的是 `read_verilog -ppdump`（帮助文本里写的是 `-ppdump`，意为 *dump Verilog code after pre-processor*）。Yosys 并没有一个叫 `-pp` 的选项，请以源码里的 `-ppdump` 为准。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `frontends/verilog/preproc.h` | 预处理器对外接口：`define_map_t`（宏表）与入口函数 `frontend_verilog_preproc` 的声明。 |
| `frontends/verilog/preproc.cc` | 预处理器全部实现：字符流模型、token 切分、`` `define ``/宏展开/`` `undef ``、`` `ifdef `` 家族、`` `include ``，以及主循环。 |
| `frontends/verilog/const2ast.cc` | 「adhoc 常数解析器」：把 Verilog 常数字面量解析为 `AST_CONSTANT` 节点。 |
| `frontends/verilog/verilog_frontend.cc` | 把预处理器接入 `read_verilog`：解析 `-D/-I/-ppdump/-nopp` 等参数，调用 `frontend_verilog_preproc`，并注册 `verilog_defaults`、`verilog_defines` 两个相关命令。 |
| `frontends/verilog/verilog_frontend.h` | `ConstParser` 临时上下文类的声明。 |
| `frontends/verilog/verilog_parser.y` | 语法器在归约到「整数常量」产生式时调用 `ConstParser::const2ast`。 |
| `frontends/ast/ast.cc` | `AstNode::mkconst_bits` 等工厂方法，真正构造 `AST_CONSTANT` 节点。 |

记忆要点：**预处理器处理 directive（以反引号 `` ` `` 开头的编译指示），常数解析器处理字面量数字**——二者都是「在词法/语法之前/之中」对文本做的预处理工作，但职责分明。

## 4. 核心概念与源码讲解

### 4.1 预处理器的位置与字符流模型

#### 4.1.1 概念说明

很多初学者会问：词法器（flex）本身就能识别 token，为什么还要单独写一个预处理器？原因是 Verilog 有一批**编译指示（compiler directive）**，它们的语义是「文本到文本」的改写——必须在词法分析之前就把文本处理好。典型例子：

- `` `define WIDTH 8 `` 之后，源码里所有 `` `WIDTH `` 都要被替换成 `8`；
- `` `ifdef SIM ... `endif `` 之间的整段文本可能要被**整块丢弃**；
- `` `include "foo.v" `` 要把另一个文件的文本**插入**到当前位置。

这些操作的对象是「文本」，而不是「token」。如果交给词法器处理，它会先把 `` `WIDTH `` 当成一个 token，等发现它是宏时已经晚了（token 流已经被切死）。所以 Yosys 的做法是：**在词法器之前，跑一遍自研的预处理器**，把 directive 全部展开/求值，产出一份「纯净」的 Verilog 文本，再喂给 flex。

源码注释把分工说得很明确：

> Ad-hoc implementation of a Verilog preprocessor. The directives `` `define ``, `` `include ``, `` `ifdef ``, `` `ifndef ``, `` `else `` and `` `endif `` are handled here. All other directives are handled by the lexer.

也就是说：上面这几个 directive 归预处理器，**其余的 directive（如 `` `timescale ``、`` `celldefine ``）其实是「假处理」或交给词法器**。

#### 4.1.2 核心流程

预处理器的核心是一个**带回退的字符流（pushback buffer）**。它不是「读一个字符就消费掉」，而是维护一个字符串队列，既可以从队首读字符，也可以往队首塞字符。

```
input_buffer:  [ "字符串A", "字符串B", ... ]   ← 队首在左
                       ^
                input_buffer_charp（队首字符串内部的读指针）

next_char()   : 从队首读一个字符，指针右移；队首读完则弹出，读下一个
return_char(c): 把一个字符「塞回」队首（指针左移或新建一个串）
insert_input(s): 把整段文本塞到队首最前面，于是它会被最先读到
```

这个模型是整个预处理器的灵魂，因为几乎所有 directive 都靠 `insert_input` 实现：

- **宏展开**：查到 `` `WIDTH `` 的值是 `8`，就 `insert_input("8")`，于是接下来读到的就是 `8`；
- **文件包含**：把另一个文件的内容 `insert_input` 进来；
- **条件编译**：不满足条件时，读到的 token 直接丢弃（不 `push` 到输出）。

最终，被「放行」的 token 会被拼进另一个全局列表 `output_code`，预处理结束时把 `output_code` 拼成一个大字符串返回——这就是喂给词法器的「纯净文本」。

#### 4.1.3 源码精读

先看全局状态：一个输入缓冲、一个输出缓冲。[frontends/verilog/preproc.cc:L48-L50](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L48-L50) 定义了 `input_buffer`（字符串链表）、`input_buffer_charp`（队首内偏移）、`output_code`（已放行的 token 列表）。

读/塞字符的三个原语在 [frontends/verilog/preproc.cc:L52-L83](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L52-L83)。注意 `next_char` 还顺手把 `\r` 吃掉（行 `ch == '\r' ? next_char() : ch`），统一行尾：

```cpp
static char next_char() {
    if (input_buffer.empty()) return 0;
    ...
    char ch = input_buffer.front()[input_buffer_charp++];
    return ch == '\r' ? next_char() : ch;
}
```

`insert_input` 把一段文本压到队首 [frontends/verilog/preproc.cc:L60-L67](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L60-L67)——宏展开、`include` 全靠它。

`next_token` 负责把字符流切成「有意义的词」 [frontends/verilog/preproc.cc:L101-L214](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L101-L214)。它对几种首字符做了特殊处理，其中两个细节最能体现「文本改写」本质：

1. **注释统一化**：`//` 行注释和 `/* */` 块注释都被改写成 `/* ... */` 形式（[frontends/verilog/preproc.cc:L156-L194](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L156-L194)）。块注释里的换行被替换成空格、再把等量换行 `return_char` 回去——这样既抹平了注释，又保住了行号（错误信息里的行号才不会错位）。
2. **反引号标识符**：以 `` ` `` 或字母/数字/下划线/`$` 开头的连续串被当作一个 token（[frontends/verilog/preproc.cc:L195-L211](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L195-L211)），所以 `` `define ``、`` `WIDTH ``、`` `include `` 都会被切成完整的一个 token，交给主循环判断。

把「文本改写」与「行号保持」并列，是本小节最该记住的两点。

#### 4.1.4 代码实践

**目标**：亲眼看到「预处理器跑在词法器之前」，并验证 pushback 模型。

**步骤**：

1. 在能运行 yosys 的环境里，用 here-doc 直接喂一段含注释和 directive 的 Verilog 给 `read_verilog`，并加上 `-ppdump`：

   ```
   read_verilog -ppdump <<EOF
   `define WIDTH 8
   // 这是行注释
   module m(input [`WIDTH-1:0] a, output [`WIDTH-1:0] y);
   /* 块注释 */
   assign y = a;
   endmodule
   EOF
   ```

2. 观察日志里 `-- Verilog code after preprocessor --` 与 `-- END OF DUMP --` 之间的内容。

**需要观察的现象**：

- `` `WIDTH `` 应当被替换成 `8`（宏展开）；
- 行注释 `// 这是行注释` 应当变成 `/* ... */` 形式；
- `` `define `` 那一行应当**消失**（它不是有效 Verilog，只用于登记宏）。

**预期结果**：dump 出来的文本是一段「没有 directive、宏已展开」的合法 Verilog。如果你看到 `WIDTH` 仍未被替换，请检查反引号是否写对（必须是半角 `` ` ``，不是单引号 `'`）。

> 若无法本地构建 yosys，可标注「待本地验证」，转而阅读 `verilog_frontend.cc` 中 `-ppdump` 分支 [frontends/verilog/verilog_frontend.cc:L511-L516](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L511-L516) 确认它就是把预处理结果原样打印。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `next_token` 要把 `//` 注释改写成 `/* */`，而不是直接删掉？
**答**：直接删掉会丢失「这段曾经占了几行」的信息，导致后续报错的行号整体上移。改成块注释、并把换行用 `return_char` 补回去，可以同时满足「抹平注释」与「保持行号」。

**练习 2**：宏展开为什么用 `insert_input` 把文本塞回队首，而不是直接字符串替换整份源码？
**答**：因为宏体里可能**再次包含**别的宏（嵌套展开），塞回队首后，主循环会继续 `next_token`，自然会递归地展开内层宏；而且带参数宏的实参本身也可能是宏调用。pushback 模型天然支持这种递归。

---

### 4.2 宏处理：`define / 带参数宏 / undef 与两套 define 机制

#### 4.2.1 概念说明

宏处理要回答三个问题：**宏怎么登记**、**宏怎么展开**、**宏从哪里来**。

- **登记**：`` `define NAME body `` 把 `NAME → body` 存进一张宏表 `define_map_t`。宏可以带形式参数：`` `define MAX(a,b) ((a)>(b)?(a):(b)) ``。
- **展开**：源码里遇到 `` `NAME ``，就在宏表里查；查到就把 `body`（实参替换后）塞回字符流。
- **来源**：Yosys 有**两套** define 机制，这是初学者最容易混淆的点：
  1. **per-call `-D`**：写在 `read_verilog -D NAME=val` 上的宏，只对这一次 `read_verilog` 有效；
  2. **全局宏**：用 `verilog_defines -D NAME` 命令登记的宏，存在 `design->verilog_defines` 里，**对所有后续 `read_verilog` 都有效**（除非 `-reset`）。

此外还有 `verilog_defaults` 命令，它登记的是「默认参数」（如默认加 `-sv` 或某个 `-D`），每次 `read_verilog` 都会自动把这些参数插到最前面。

#### 4.2.2 核心流程

**登记一个宏**（`read_define`）的大致流程：

```
读到 `define → 读宏名 name → 判断 name 后面紧跟的是 '(' 还是空白
  ├─ 紧跟 '(' ：带参宏。用 read_define_args() 读形参列表 (可带默认值)
  └─ 紧跟空白：无参宏
→ 逐 token 读到行末（或 here-doc 结束），拼成 body
   · body 中出现的形参名，被替换成「魔法符号」`macro_<name>_arg<pos>
   · 行尾续行 '\' 会吃掉换行
→ defines_map.add(name, body, args)
```

这里的关键技巧：**宏体里的形参不是直接存原文，而是被改写成 `` `macro_<name>_arg<pos> `` 这样的「魔法宏」**。展开时，再为每个实参临时定义这些魔法宏。于是「实参替换」就复用了「宏展开」同一条路径。

**展开一个宏**（`try_expand_macro`）的大致流程：

```
token 以 ` 开头 → 取宏名 → 查宏表
  ├─ 查不到：返回 false（当普通 token 处理）
  └─ 查到 body：
       · 若是带参宏：读 '(' 用 read_argument 读出实参列表
            对每个形参 i：
              若魔法宏 `macro_<name>_arg<i> 已有定义 → 旧值压栈(macro_arg_stack)，并 insert_input("`__restore_macro_arg ")
              defines.add(魔法宏名, 实参值)
       · insert_input(body)   ← 把宏体塞回字符流，交给主循环继续展开
```

`restore_macro_arg` 配合 `` `__restore_macro_arg `` 魔法 directive，在宏体展开结束后把被覆盖的旧定义还原——保证宏调用的副作用不泄漏到外部。

**两套 define 的汇合点**在预处理器入口 `frontend_verilog_preproc` [frontends/verilog/preproc.cc:L757-L759](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L757-L759)：建一张全新的 `defines` 表，先 `merge(pre_defines)`（即 per-call 的 `-D`），再 `merge(global_defines_cache)`（即 `design->verilog_defines`）。由于 `merge` 是「后者覆盖前者」，**全局宏优先级高于 per-call `-D`**。

#### 4.2.3 源码精读

宏表 `define_map_t` 是一个 `name → define_body_t` 的映射，`define_body_t` 含宏体 `body`、是否带参 `has_args`、形参表 `args` [frontends/verilog/preproc.h:L43-L69](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.h#L43-L69)。注意构造函数里**硬编码了 `YOSYS=1`** [frontends/verilog/preproc.cc:L333-L336](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L333-L336)——这就是帮助文档里「`read_verilog` 总是定义宏 `YOSYS`」的出处。

形参表 `arg_map_t` 支持默认值与按名查找，并能生成「魔法符号」名 [frontends/verilog/preproc.cc:L266-L269](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L266-L269)：

```cpp
static std::string str_token(const std::string &macro_name, int pos) {
    return stringf("macro_%s_arg%d", macro_name, pos);
}
```

`read_define` 把宏体里的形参替换成魔法符号 [frontends/verilog/preproc.cc:L723-L729](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L723-L729)；`try_expand_macro` 读实参并为每个形参临时建立魔法宏定义 [frontends/verilog/preproc.cc:L517-L535](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L517-L535)：

```cpp
for (const auto &pr : body->args.get_vals(name, args)) {
    if (const define_body_t *existing = defines.find(pr.first)) {
        macro_arg_stack.push({pr.first, *existing});   // 旧值压栈
        insert_input("`__restore_macro_arg ");          // 展开后还原
    }
    defines.add(pr.first, pr.second);                   // 临时定义魔法宏=实参
}
...
insert_input(body->body);   // 宏体塞回字符流
```

`get_vals` 内部还实现了 SystemVerilog 的实参默认值规则（给了非空白实参就用实参，否则用默认值）[frontends/verilog/preproc.cc:L273-L313](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L273-L313)。

接驳到 `read_verilog` 的两套 define 机制：`-D` 解析在 [frontends/verilog/verilog_frontend.cc:L461-L479](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L461-L479)，自动追加 `SYNTHESIS`/`FORMAL` 在 [frontends/verilog/verilog_frontend.cc:L491-L492](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L491-L492)，而把 `defines_map`（pre_defines）与 `design->verilog_defines`（全局缓存）一起传入预处理器在 [frontends/verilog/verilog_frontend.cc:L511-L512](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L511-L512)。全局缓存的存储位置是 `RTLIL::Design` 的成员 `verilog_defines` [kernel/rtlil.h:L1908](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1908)。操作全局缓存的命令 `verilog_defines`（`-D/-U/-reset/-list`）见 [frontends/verilog/verilog_frontend.cc:L636-L708](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L636-L708)；登记默认参数的 `verilog_defaults`（`-add/-clear/-push/-pop`）见 [frontends/verilog/verilog_frontend.cc:L578-L634](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L578-L634)，它会把默认参数插到每次 `read_verilog` 参数列表最前面 [frontends/verilog/verilog_frontend.cc:L297](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L297)。

#### 4.2.4 代码实践

**目标**：用 `tests/simple/macros.v` 的思路，观察带参宏与嵌套宏的展开，并对比 `-D` 与 `verilog_defines`。

**步骤**：

1. 准备 `myparam.v`：

   ```verilog
   `define get_msb(off, len) ((off)+(len)-1)
   `define get_lsb(off, len) (off)
   `define sel_bits(offset, len) `get_msb(offset, len) : `get_lsb(offset, len)
   module m(input [31:0] a, output [7:0] y);
     assign y = a[`sel_bits(16, 8)];
   endmodule
   ```

2. 运行 `read_verilog -ppdump myparam.v`，观察 `a[...]` 是否被展开成 `a[((16)+(8)-1) : (16)]`（即 `a[23:16]`）。这一例正是仓库测试 `tests/simple/macros.v` 的真实用法。

3. 再对比两种 define 来源：先 `verilog_defines -D DBG`，再 `read_verilog -ppdump -D DBG file.v`，在 dump 里搜索 `DBG` 的展开是否出现；然后 `verilog_defines -list` 查看全局宏表。

**需要观察的现象**：带参宏 `sel_bits` 的实参 `16`、`8` 被正确替换进内层宏 `get_msb/get_lsb`；`verilog_defines -list` 能列出 `YOSYS`、`SYNTHESIS`（或 `FORMAL`）以及你刚加的 `DBG`。

**预期结果**：嵌套宏被完全展开为位选表达式。若展开结果不符合预期，多半是宏体里漏了括号或反引号（`` `get_msb `` 的反引号不能省，否则不会被当作宏调用）。

#### 4.2.5 小练习与答案

**练习 1**：为什么宏体里的形参 `offset` 要被存成 `` `macro_sel_bits_arg0 `` 而不是直接存 `offset`？
**答**：为了让「实参替换」复用「宏展开」机制。展开 `sel_bits(16,8)` 时，把 `` `macro_sel_bits_arg0 `` 临时定义成 `16`，于是宏体里出现 `` `get_msb(`macro_sel_bits_arg0, ...) `` 时，会先把 `` `macro_sel_bits_arg0 `` 展开成 `16`，再展开 `get_msb`。这样实参（哪怕本身是复杂表达式或宏）能被正确求值，且展开完用 `` `__restore_macro_arg `` 还原，不污染外部宏表。

**练习 2**：`read_verilog -D FOO=1 file.v` 与先执行 `verilog_defines -D FOO=1` 再 `read_verilog file.v`，二者效果有何异同？
**答**：都能让 `FOO` 在预处理时被定义。区别在于生命周期：`-D` 只对这一次 `read_verilog` 有效（存在局部 `defines_map` 里）；`verilog_defines` 写入 `design->verilog_defines`，对**之后所有** `read_verilog` 都生效，直到 `-reset` 或 `-U`。当两者同时定义同名宏时，全局 `verilog_defines` 优先（`merge` 顺序决定）。

---

### 4.3 条件编译与文件包含：`ifdef 家族与 `include

#### 4.3.1 概念说明

条件编译让同一份源码在不同配置下编译出不同结果，是写可移植 RTL 的常用手段。Verilog 的条件编译 directive 是一组配对：

```
`ifdef X      // 若 X 已定义，编译本分支
`ifndef X     // 若 X 未定义，编译本分支
`elsif Y      // 否则若 Y 已定义……
`else         // 否则……
`endif        // 结束
```

文件包含 `` `include "foo.v" `` 则把 `foo.v` 的文本插入到当前位置。

Yosys 预处理器要正确处理**嵌套**的条件编译——这是最容易写错的地方。仓库里有专门针对嵌套的测试 `tests/simple/ifdef_1.v` 和 `tests/simple/macros.v`，本讲的练习就源于它们。

#### 4.3.2 核心流程

条件编译用一个**两层计数器 + 一个标志位**来维护状态（见主循环开头 [frontends/verilog/preproc.cc:L767-L773](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L767-L773)）：

- `ifdef_pass_level`：当前处于多少层「已满足」的条件分支里（正在输出代码）；
- `ifdef_fail_level`：当前处于多少层「未满足」的条件分支里（正在丢弃代码）；
- `ifdef_already_satisfied`：对于最外层那个未满足分支，是否已经有分支被满足过（用于决定后续 `elsif/else` 还要不要尝试）。

核心不变式：**未满足的分支总是嵌套在已满足的分支之内**——即便内层某个条件为真，只要外层条件失败，整段都不输出。

各 directive 的语义（精简版）：

| directive | 当前在 pass（fail_level==0） | 当前在 fail |
| --- | --- | --- |
| `` `ifdef X `` | X 定义→pass_level++；未定义→fail_level=1, already_satisfied=false | fail_level++（更内层，继续丢） |
| `` `ifndef X `` | X **未**定义→pass_level++；定义→fail_level=1 | fail_level++ |
| `` `elsif X `` | 翻转为 fail（因为 if 分支已满足） | 仅当 fail_level==1 且未满足过 且 X 定义→翻转为 pass |
| `` `else `` | 翻转为 fail | 仅当 fail_level==1 且未满足过→翻转为 pass |
| `` `endif `` | pass_level-- | fail_level-- |

主循环里，只要 `ifdef_fail_level > 0`，除换行外的 token 全部丢弃（换行保留以维持行号）[frontends/verilog/preproc.cc:L856-L860](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L856-L860)。循环结束前还会检查「条件未闭合」[frontends/verilog/preproc.cc:L984-L986](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L984-L986)。

`` `include `` 的查找顺序（见 [frontends/verilog/preproc.cc:L862-L918](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L862-L918)）：

1. 直接按字面路径打开；
2. 若失败、是相对路径、且当前文件带目录 → 试「当前文件所在目录 + 文件名」；
3. 仍失败 → 依次试 `-I` 给出的每个 include 目录；
4. 全失败 → 输出 `` `file_notfound `` 标记（而不是立即报错，留给后续处理）。

找到文件后，用 `input_file` 把它包上 `` `file_push "name" `` / `` `file_pop `` 再注入字符流——这两个魔法 directive 用于维护「当前文件名」栈，使错误信息能指回正确的源文件 [frontends/verilog/preproc.cc:L389-L403](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L389-L403)。

#### 4.3.3 源码精读

`ifdef` 处理 [frontends/verilog/preproc.cc:L828-L840](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L828-L840)：注意判定 `ifdef_fail_level > 0 || !defines.find(name)`——只要外层已在丢弃，内层无条件进入 fail。`else` [frontends/verilog/preproc.cc:L796-L809](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L796-L809) 与 `elsif` [frontends/verilog/preproc.cc:L811-L826](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L811-L826) 都严格依赖 `ifdef_already_satisfied` 来实现「一个 if 组里最多只有一个分支被编译」。

`` `include `` 的三级查找已在上面列出，关键是它**不直接报错**，而是吐出 `` `file_notfound `` [frontends/verilog/preproc.cc:L911-L912](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L911-L912)。文件名栈由 `` `file_push `` / `` `file_pop `` 维护 [frontends/verilog/preproc.cc:L920-L936](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L920-L936)。

一个反直觉点：`` `undef `` 同时从本次的 `defines` 表和全局缓存 `global_defines_cache` 里删除 [frontends/verilog/preproc.cc:L943-L951](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L943-L951)——也就是说源码里的 `` `undef `` 也会影响后续 `read_verilog` 看到的全局宏（因为缓存被改了）。

#### 4.3.4 代码实践

**目标**：亲手验证嵌套条件编译的「最多一个分支被编译」语义，并体验 `include` 查找。

**步骤**：

1. 用 `read_verilog -ppdump` 喂一段来自 `tests/simple/macros.v:test_ifdef` 的简化逻辑（核心是连续若干个 `ifdef/elsif/else`），观察 dump 里实际「存活」的是哪几行赋值。

2. 验证「外层失败则内层全部丢弃」：先 `` `undef X ``，再写

   ```
   `ifdef X
     `define INNER 1
   `else
     `ifdef INNER
       // 若这行出现在 dump 里，说明逻辑错了
     `endif
   `endif
   ```

   预期 `INNER` 那行**不应**出现（外层 `X` 未定义 → 整个 else 内的 `ifdef INNER` 即使在 else 里，但因 `INNER` 此刻未定义也不会进入；更要紧的是体会外层 fail 时内层被整体跳过的计数器行为）。

3. （可选）建 `sub.v` 含一行 `wire g = 1;`，主文件写 `` `include "sub.v" ``，分别用「同目录」「`-I` 指定别的目录」两种方式运行，观察是否成功包含；再故意写一个不存在的文件名，看是否出现 `` `file_notfound ``。

**需要观察的现象**：每个 `ifdef/elsif/else` 组里最多一段代码出现在 dump 中；`include` 失败时不会让预处理器崩溃，而是留下 `file_notfound` 标记。

**预期结果**：与 `tests/simple/macros.v` 里 `test_ifdef` 模块的预期行为一致（该模块用大量组合穷举了 `ifdef/ifndef/elsif/else`，是权威参照）。若本地无法运行，可对照源码 [frontends/verilog/preproc.cc:L786-L860](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc#L786-L860) 手动模拟计数器。

#### 4.3.5 小练习与答案

**练习 1**：对于 `` `ifdef A `elsif B ... `else ... `endif ``，若 `A` 未定义、`B` 已定义，哪段会被编译？若 `A` 已定义呢？
**答**：`A` 未定义、`B` 定义 → `elsif B` 分支被编译（此时 `fail_level` 从 1 翻回 0，`already_satisfied` 置真）；`A` 已定义 → `ifdef A` 分支被编译，`elsif/else` 因 `already_satisfied` 保持 fail，整组最多编译一段。

**练习 2**：为什么 `include` 找不到文件时，预处理器选择输出 `` `file_notfound `` 而不是立即 `log_error`？
**答**：因为预处理是「文本到文本」的阶段，此时还无法确定这个 include 是否致命（也许后续会被条件编译丢弃、或被上层脚本处理）。留下标记让后续阶段在真正用到该文件时再决定报错，更稳健，也便于 `-lib` 等场景把缺失文件当作黑盒。

---

### 4.4 常数表达式到 AST：const2ast

#### 4.4.1 概念说明

讲完 directive，回到「字面量」。Verilog 的常数写法很丰富：

- 简单十进制：`8`、`255`；
- 带宽度进制：`8'h3F`、`4'b1010`、`16'o755`、`32'd100`；
- 带符号：`8'sd5`；
- 含未知/高阻位：`4'b10xz`、`8'h?x`；
- 字符串：`"abc"`（每个字符 8 位）；
- 无尺寸常量：`'0`、`'1`、`'x`、`'z`。

词法器（u5-l1）**只**负责把整段常数字面量识别成**一个** token（如 `8'h3F`），它**不**拆分宽度/进制/位。真正把它「解剖」成位向量、并构造 `AST_CONSTANT` 节点的工作，由 `const2ast.cc` 的「adhoc 常数解析器」完成——这是 const2ast.cc 文件头明确说明的分工 [frontends/verilog/const2ast.cc:L29-L34](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/const2ast.cc#L29-L34)。

为什么用「adhoc（手写）」而不是塞进 bison 语法？因为 Verilog 常数语法里 `x`/`z`/`?`、可变进制、可变宽度、下划线分隔符等组合，用单独的手写状态机比塞进 LALR 文法更清晰。

#### 4.4.2 核心流程

`const2ast(code, case_type, warn_z)` 的总体分支（见 [frontends/verilog/const2ast.cc:L159-L253](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/const2ast.cc#L159-L253)）：

```
1. 若 code 以 '"' 开头 → 字符串常量：每个字符按 8 位 LSB 优先展开
2. 抹掉所有 '_'、空格、制表符、回车、换行（Verilog 允许 8'h3F / 1_000）
3. 用 strtol 尝试读出开头的「宽度」len_in_bits
4. 若整串就是纯十进制（endptr 到末尾）→ my_strtobin(base=10)，零扩展到 32 位，signed
5. 否则若 endptr 指向 '\'' → 形如 <bits>'[s][bodh]<digits>
     · 可选 's' 表示有符号
     · 按 b/o/d/h 调 my_strtobin(对应 base)
     · 特殊：'0 '1 'x 'z 为无尺寸单比特常量
6. 否则返回 nullptr（语法器据此报 "Value conversion failed"）
```

核心子程序 `my_strtobin` 把「进制数字符串」转成 `vector<RTLIL::State>`（位列表，LSB 在前），并处理 `x/z/?`：`?` 和 `z` 视作高阻 `Sz`，`x` 视作未知 `Sx`；而在 `casez/casex` 语句里（`case_type != 0`），它们会被映射成 `Sa`（"任意匹配"语义）[frontends/verilog/const2ast.cc:L83-L157](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/const2ast.cc#L83-L157)。

十进制转二进制用「反复除以 2 取余」的任意精度算法 `my_decimal_div_by_two` [frontends/verilog/const2ast.cc:L56-L69](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/const2ast.cc#L56-L69)——之所以要「任意精度」，是因为 Verilog 常数可以远超 `long` 范围（如 `64'd...`）。

最终位向量经 `AstNode::mkconst_bits` 包成 `AST_CONSTANT` 节点 [frontends/ast/ast.cc:L871-L873](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L871-L873)。

**回答实践任务里的问题：`parameter W = 8` 如何被处理？** 这里的 `8` 是简单十进制字面量，走第 4 条分支：

1. `my_strtobin(data, "8", -1, 10, ...)`：用除 2 取余得到位的 LSB-first 序列。8 的二进制是 `1000`，LSB 在前即 `data = [S0, S0, S0, S1]`（位 0..3 = 0,0,0,1）。
2. 因 `len_in_bits = -1`（无尺寸），`my_strtobin` 把不足 32 位的零扩展到 32 位 [frontends/verilog/const2ast.cc:L131-L134](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/const2ast.cc#L131-L134)，于是 `data` 变成 32 位、高位全 `S0`。
3. 回到 `const2ast`：`data.back()`（最高位）是 `S0`，不补符号位；返回 `mkconst_bits(data, is_signed=true)`，得到一个 **32 位有符号、值为 8** 的 `AST_CONSTANT` 节点。

也就是说，`parameter W = 8` 的右边 `8`，最终是 32 位有符号常数 8。这与 Verilog「未尺寸整数量至少 32 位」的语义一致。

#### 4.4.3 源码精读

`ConstParser` 是一个轻量「临时上下文」类，只携带源位置 `loc`，方法就是 `const2ast` 及其私有助手 [frontends/verilog/verilog_frontend.h:L43-L58](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.h#L43-L58)。语法器在归约到 `integral_number` 等产生式时，就地构造 `ConstParser p{...}` 并调用 `p.const2ast(...)` [frontends/verilog/verilog_parser.y:L3298-L3303](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_parser.y#L3298-L3303)：

```cpp
integral_number {
    ConstParser p{@1};
    $$ = p.const2ast(*$1, extra->case_type_stack.size() == 0 ? 0
                                  : extra->case_type_stack.back(), !mode->lib);
    ...
};
```

注意第二个参数 `case_type` 来自 `case_type_stack`——在 `casez/casex` 内部时它非 0，决定 `x/z/?` 被编码成 `Sa`（匹配通配）而非普通 `Sx/Sz`。第三个参数 `warn_z = !mode->lib`，非库模式下遇到 `z` 会发出「tri-state 支持有限」的警告 [frontends/verilog/const2ast.cc:L161-L166](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/const2ast.cc#L161-L166)。

`my_strtobin` 对 `x/z/?` 的编码 [frontends/verilog/const2ast.cc:L95-L124](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/const2ast.cc#L95-L124)：`x/X → 0xf0`、`z/Z/? → 0xf1`，再在展开位时按 `case_type` 决定落到 `Sx/Sz/Sa`。宽度不足/溢出也会校验并告警 [frontends/verilog/const2ast.cc:L140-L156](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/const2ast.cc#L140-L156)。

带尺寸进制分支对 `'b/'o/'d/'h` 的派发 [frontends/verilog/const2ast.cc:L209-L250](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/const2ast.cc#L209-L250)；字符串分支 [frontends/verilog/const2ast.cc:L171-L185](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/const2ast.cc#L171-L185)。

#### 4.4.4 代码实践

**目标**：用 `read_verilog -dump_ast1` 看 `const2ast` 产出的 `AST_CONSTANT`，并对照源码手算一个十六进制常数的位展开。

**步骤**：

1. 准备 `const_demo.v`：

   ```verilog
   module m(output [7:0] a, output [7:0] b, output [7:0] c);
     parameter W = 8;
     assign a = W;          // 简单十进制
     assign b = 8'h3F;      // 带尺寸十六进制
     assign c = 4'b10xz;    // 含 x/z
   endmodule
   ```

2. 运行 `read_verilog -dump_ast1 const_demo.v`，在 AST dump 里找到这几个常量节点，观察它们的位宽与位值。

3. 手算验证：`8'h3F` = 二进制 `0011_1111`，LSB-first 位串应为 `1111_1100`（16 进制 3F 的位 0..7 = 1,1,1,1,1,1,0,0）。对照 dump 里 `b` 的常量节点。

**需要观察的现象**：`W` 的常量节点是 32 位、值为 8；`8'h3F` 是 8 位、值为 63；`4'b10xz` 含 `Sx/Sz` 位；`c` 因含 `z` 会触发 tri-state 警告。

**预期结果**：AST 中常量叶节点的位数与进制一致，`x/z` 以 `Sx/Sz`（或 `casez/casex` 下的 `Sa`）出现。手算结果应与 dump 吻合。

> 若本地无法运行，可阅读 `my_strtobin` [frontends/verilog/const2ast.cc:L83-L157](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/const2ast.cc#L83-L157)，按 `8'h3F`（base=16）手工模拟一遍 `digits=[3,15]` → 每 4 位展开的过程，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`4'b10xz` 在普通赋值和在 `casez` 里，`const2ast` 产出的位有何不同？
**答**：在普通赋值里（`case_type=0`），`x → Sx`、`z → Sz`；在 `casez` 里（`case_type='z'`），`z/? → Sa`、`x` 仍为 `Sx`；在 `casex` 里（`case_type='x'`），`x/z/? → Sa`。`Sa` 表示「比较时可作通配」，供后续 case 匹配逻辑识别。

**练习 2**：为什么 `my_strtobin` 对十进制要用「除 2 取余」而不是直接 `strtol`？
**答**：因为 Verilog 十进制常数可以是任意宽度（如 `64'd12345678901234567890`），远超 `long` 的表示范围。`strtol` 会溢出。`my_decimal_div_by_two` 把数字当成十进制位数字数组，反复除以 2 取余，逐位求出二进制位，天然支持任意精度。

---

## 5. 综合实践

把本讲三块内容串起来：写一个「可配置」的加法器，用宏定义位宽、用条件编译切换进位输出、用常数赋初值，然后端到端综合。

**任务**：

1. 编写 `cfg_adder.v`：

   ```verilog
   `define WIDTH 8
   module cfg_adder(input [`WIDTH-1:0] a, b,
                     output [`WIDTH-1:0] sum
                     `ifdef WITH_CARRY
                     , output carry
                     `endif
                    );
     `ifdef WITH_CARRY
       wire [WIDTH:0] full = {1'b0, a} + {1'b0, b};
       assign sum   = full[`WIDTH-1:0];
       assign carry = full[`WIDTH];
     `else
       assign sum = a + b;
     `endif
   endmodule
   ```

2. 分别用两种配置预处理并对比：
   - `read_verilog -ppdump cfg_adder.v`（不定义 `WITH_CARRY`）；
   - `read_verilog -ppdump -D WITH_CARRY cfg_adder.v`（定义 `WITH_CARRY`）。
   观察 `carry` 端口和相关赋值是否只在第二种 dump 里出现，且 `` `WIDTH `` 被替换成 `8`。

3. 接着把两份配置分别 `synth` 再 `write_rtlil`，对照观察：带 `WITH_CARRY` 的版本多出一个进位信号和更宽的加法器（`$add` 单元的位宽），体会「同源码、不同宏 → 不同网表」。

4. 最后回到 `const2ast`：综合后用 `write_rtlil` 找到对应 `W=8` 衍生出的常数（如位选 `full[7:0]` 的边界 `7`、`8`），确认它们都是 32 位有符号 `AST_CONSTANT` 经 simplify 后落到的 RTLIL 常数。

**验收**：你能用一句话解释「从 `` `define WIDTH 8 `` 到网表里宽度为 8 的 `$add`，文本依次经过了预处理器（宏展开/条件选择）、const2ast（常数成 AST）、AST simplify/genrtlil（落成 RTLIL）三个阶段」——这就把 u5-l1 与 u5-l2 串成了一条完整链路。

## 6. 本讲小结

- 预处理器在词法分析**之前**运行，原因是 directive 是「文本到文本」的改写；它只处理 `` `define/`include/`ifdef/`ifndef/`else/`endif ``，其余 directive 交给词法器或被忽略。
- 整个预处理器建立在 **pushback 字符流**（`input_buffer` + `insert_input`）之上：宏展开、`include` 都是「把文本塞回队首再重读」，天然支持嵌套递归。
- 带参数宏用「魔法符号 `` `macro_<name>_arg<i> ``」把实参替换也变成宏展开，并用 `` `__restore_macro_arg `` 还原旧定义，保证副作用不泄漏。
- 条件编译用 `pass_level/fail_level/already_satisfied` 三状态正确处理嵌套，核心不变式是「未满足分支总嵌套在已满足分支内」，每个 if 组最多编译一段。
- define 有两套来源：per-call `-D`（局部）与 `verilog_defines`（全局，存于 `design->verilog_defines`），在 `frontend_verilog_preproc` 入口 merge，全局优先；`verilog_defaults` 提供「默认参数」。
- `const2ast` 是手写的任意精度常数解析器，把 `8`/`8'h3F`/`4'b10xz`/`"str"` 等统一翻译成位向量，再由 `mkconst_bits` 包成 `AST_CONSTANT` 节点；`case_type` 决定 `x/z/?` 在 `casez/casex` 里是否编码成通配 `Sa`。

## 7. 下一步学习建议

- 下一讲 **u5-l3（AST 节点模型与 AstModule）** 会系统讲解 `AstNode` 的种类——本讲出现的 `AST_CONSTANT`、`AST_DESIGN` 只是其中两个，建议结合本讲的实践去读 `frontends/ast/ast.h` 的 `AstNodeType` 枚举。
- 之后 **u5-l4（AST 简化与 genrtlil）** 讲 `simplify.cc` 如何对 AST 做常量折叠与宽度推导——你会看到本讲产出的 `AST_CONSTANT` 是如何被折叠进表达式的，从而真正理解「`parameter W = 8` 如何决定线宽」。
- 想深入预处理器边角（如 `` `timescale ``、`` `resetall ``、`` `undefineall ``、here-doc `` `""` `` 字符串展开）的读者，可直接通读 [frontends/verilog/preproc.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/preproc.cc) 的主循环，它集中了所有 directive 的处理。
- 对常数精度与四值逻辑语义感兴趣，可对照 [frontends/verilog/const2ast.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/const2ast.cc) 与 u3-l3 讲过的 `RTLIL::Const` / `RTLIL::State`，体会「字面量 → AST 常数 → RTLIL Const」的连续映射。
