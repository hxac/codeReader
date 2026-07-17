# Verilog 前端：词法与语法分析

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `read_verilog` 这条命令在 Yosys 内部到底「干了一件什么事」，以及它产出的不是 RTLIL 而是 **AST**（抽象语法树）。
- 理解 **flex 词法分析器**（`verilog_lexer.l`）如何把一段字符流切成一个个 token，以及关键字、标识符、数字、注释分别对应哪些规则。
- 理解 **bison 语法分析器**（`verilog_parser.y`）如何用一组产生式（production）把 token 归约（reduce）成一棵 AST，并且能亲手在源码里找到 `module` / `always` / `if` 对应的产生式。
- 知道词法器和语法器是如何被 CMake 用 `flex_target` / `bison_target` 生成、又被 `verilog_frontend.cc` 串起来的。

本讲只讲「Verilog 文本 → AST」这一段；「AST → RTLIL」由 `frontends/ast/` 负责，是后续讲义（u5-l3、u5-l4）的内容。

## 2. 前置知识

本讲假设你已经具备以下认知（这些都在前置讲义中建立）：

- **Yosys 的数据流**：前端（读 HDL）→ 一串 pass 变换 RTLIL → 后端（写出网表）。本讲聚焦「前端」的第一种也是最常用的一种——Verilog 前端（来自 u1-l1、u1-l4）。
- **Frontend 机制**：`read_verilog` 本质上是一个 `Frontend` 子类，`Frontend("verilog")` 这个构造会自动把命令名拼成 `read_verilog`，并同时登记进 `pass_register`（当命令）和 `frontend_register`（当前端种类）（来自 u4-l1）。
- **命令执行链**：`driver.cc` 调用 `run_frontend` → 找到对应 `Frontend` → 调用它的 `execute(istream*&, filename, args, design)`（来自 u4-l4）。

如果你完全没有编译原理背景，下面三个概念请先记住：

| 概念 | 通俗解释 |
|------|----------|
| **词法分析（lexing）** | 把「一段字符」切成「一个个有意义的单词（token）」。比如把 `module` 这 6 个字符识别成「关键字 module」。 |
| **语法分析（parsing）** | 按「语法规则」把一串 token 组装成一棵树（AST）。比如 `module 名字 ; ... endmodule` 组装成一个「模块节点」。 |
| **AST（抽象语法树）** | 一棵用节点表示程序结构的树，每个节点表示一个语法结构（模块、赋值、if、加法……）。 |

本讲会反复出现两个外部工具：

- **flex**：词法分析器生成器。你写一堆「正则 → 动作」的规则到 `.l` 文件，flex 帮你生成一个 C++ 词法器（自动机），输入字符流、输出 token。
- **bison**：语法分析器生成器。你写一堆「产生式 → 动作」的规则到 `.y` 文件，bison 帮你生成一个 LALR(1) 语法器，输入 token 流、按规则归约、执行动作。

> 名字冷知识：`.l` 来自 **l**exer，`.y` 来自 **y**acc（Yet Another Compiler Compiler，bison 的前辈）。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `frontends/verilog/` 目录下：

| 文件 | 角色 | 本讲用法 |
|------|------|----------|
| `verilog_frontend.cc` | 注册 `read_verilog` 命令，串联预处理→词法→语法→AST→RTLIL | 讲命令入口与整体流程 |
| `verilog_frontend.h` | 前端公开声明（含 `const2ast` 常数解析） | 简要提及 |
| `verilog_lexer.l` | **flex 词法规则** | 讲词法分析 |
| `verilog_lexer.h` | `VerilogLexer` 类声明 | 简要提及 |
| `verilog_parser.y` | **bison 语法规则**，直接构造 AST | 讲语法分析与 AST 构造 |
| `verilog_location.h` | 自实现的 `Location`/`Position` 源码位置类型 | 讲错误定位 |
| `preproc.cc` / `preproc.h` | Verilog 预处理器（`` `define `` / `` `include `` / `` `ifdef ``） | 仅在流程中提到，详解见 u5-l2 |
| `const2ast.cc` | 把常数文本解析成 `AST_CONSTANT` | 仅提及，详解见 u5-l2 |
| `CMakeLists.txt` | 用 `flex_target`/`bison_target` 生成 `.cc`，再组装成前端 | 讲构建集成 |
| `docs/source/yosys_internals/flow/verilog_frontend.rst` | 官方文档，含 AST 节点类型对照表 | 作为权威参考 |

> 重要事实：`verilog_frontend.cc` 的注释里明确写道——**这个前端不直接生成 RTLIL，而是先生成 AST，再交给 `frontends/ast/` 的 `AST::process()` 把 AST 转成 RTLIL**。这是理解整条流水线的关键。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 `verilog_frontend.cc`：注册与 `read_verilog` 执行流程**（总指挥）
2. **4.2 `verilog_lexer.l`：词法分析**（字符 → token）
3. **4.3 `verilog_parser.y`：语法分析与 AST 构造**（token → AST）

---

### 4.1 `verilog_frontend.cc` 注册与执行流程

#### 4.1.1 概念说明

`read_verilog` 是 Yosys 用得最多的命令。当你敲下 `read_verilog counter.v` 时，发生的事情并不是「直接解析出 RTLIL」，而是分成几个清晰的小步：

```
 counter.v (字符流)
    │  ① 预处理器 preproc.cc  （处理 `define / `include / `ifdef）
    ▼
 预处理后的字符流
    │  ② 词法器 verilog_lexer.l （flex 生成）
    ▼
 token 流
    │  ③ 语法器 verilog_parser.y （bison 生成）边归约边构造
    ▼
 AST（抽象语法树，根节点是 AST_DESIGN）
    │  ④ AST::process()  （在 frontends/ast/，本讲不讲）
    ▼
 RTLIL
```

其中第 ①②③ 步都在 `frontends/verilog/` 内完成，第 ④ 步交给 `frontends/ast/`。本讲的「总指挥」就是 `verilog_frontend.cc`，它负责：解析命令行选项、调预处理、建好 AST 根节点、构造词法器和语法器、调用 `parser.parse()`、最后把生成的 AST 交给 `AST::process()`。

#### 4.1.2 核心流程

`VerilogFrontend::execute()` 的执行步骤可以概括为：

1. 把 `verilog_defaults` 注册的默认选项插到参数列表前面（这样脚本里可以预设公共选项）。
2. 逐个解析命令行选项（`-sv` / `-formal` / `-dump_ast1` / `-lib` / `-D` / `-I` 等），填到 `parse_mode`（影响解析行为）和一堆 `flag_*`（影响后续 AST 处理）里。
3. 根据模式自动 `add` 一个宏 `SYNTHESIS`（或 `-formal` 时的 `FORMAL`），这是「综合模式」约定。
4. 用 `extra_args()` 取出文件名并打开输入流。
5. **除非加 `-nopp`，否则跑预处理器**：`frontend_verilog_preproc(...)` 返回一个 `std::string`，词法器改从这个字符串读。
6. 新建一个 `AST_DESIGN` 根节点 `parse_state.current_ast`（一棵 AST 的总根）。
7. 构造 `VerilogLexer` 和 `frontend_verilog_yy::parser`。
8. **`parser.parse()`**：这是真正驱动词法+语法、构造 AST 的地方。
9. 给所有 `AST_MODULE` 节点补上 `-setattr` 指定的属性。
10. **`AST::process(...)`**：把 AST 转成 RTLIL（本讲不讲细节）。
11. 清理临时状态、日志输出 `Successfully finished Verilog frontend.`。

注意第 5 步：预处理是「提前一次性把整个文件处理成字符串」，而不是和词法器交替进行。这一点和很多编译器（预处理与词法交错）不同。

#### 4.1.3 源码精读

**命令注册**——`VerilogFrontend` 继承自 `Frontend`，构造时给出种类名 `"verilog"`，框架据此自动拼出命令名 `read_verilog`：

[frontends/verilog/verilog_frontend.cc:72-73](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L72-L73) —— 定义 `VerilogFrontend` 并以 `Frontend("verilog", ...)` 注册命令；这与 u4-l1 讲的「一名两表」机制一致：同一个对象既是命令又是前端种类。

**预处理器调用**——除非 `-nopp`，否则把输入流喂给预处理器，得到处理后的字符串，词法器从这个字符串（而不是原始文件）读：

[frontends/verilog/verilog_frontend.cc:511-516](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L511-L516) —— 调用 `frontend_verilog_preproc(...)` 产出 `code_after_preproc`，再用它建一个新的 `istringstream` 作为词法输入；`-ppdump` 可以把这段中间结果打印出来。

**建 AST 根节点 + 构造词法器/语法器**——这是把三个组件「连线」的关键代码：

[frontends/verilog/verilog_frontend.cc:521-525](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L521-L525) —— 先 `new AST::AstNode(top_loc, AST_DESIGN)` 创建总根节点；再用 `VerilogLexer lexer(&parse_state, &parse_mode, filename_shared)` 构造词法器；再用 `frontend_verilog_yy::parser parser(&lexer, &parse_state, &parse_mode)` 构造语法器，三者通过 `parse_state` 共享状态（如 `ast_stack`、当前模块指针）。`set_debug` 对应 `-yydebug` 调试输出。

**真正解析**——一行 `parser.parse()` 驱动整个词法+语法过程，期间 AST 被逐步构造到 `parse_state.current_ast` 这棵 `AST_DESIGN` 树上：

[frontends/verilog/verilog_frontend.cc:547](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L547) —— `parser.parse();`，返回后 `parse_state.current_ast` 已经是完整的 AST。

**把 AST 转成 RTLIL**——`AST::process()` 是 `frontends/ast/` 的入口，本讲只到这里为止：

[frontends/verilog/verilog_frontend.cc:560-561](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L560-L561) —— `AST::process(design, parse_state.current_ast, ...)` 把刚建好的 AST 转换成 RTLIL 并加入 `design`；这一步的具体实现（simplify + genrtlil）见后续讲义 u5-l4。

> 小结：`verilog_frontend.cc` 不实现「怎么认 Verilog 语法」的细节，它只负责「把预处理、词法、语法、AST→RTLIL 这几件事按顺序串起来」。真正的「认语法」逻辑在 4.2（词法）和 4.3（语法）。

#### 4.1.4 代码实践

**实践目标**：用 `-ppdump` 和 `-dump_ast1` 两面「放大镜」，亲眼看到「原始字符 → 预处理后字符 → AST」的中间产物，建立对流程的直觉。

**操作步骤**：

1. 准备一个小 Verilog 文件 `tiny.v`（示例代码，非项目原有）：

   ```verilog
   `define WIDTH 4
   module tiny(input clk, input [`WIDTH-1:0] d, output reg [`WIDTH-1:0] q);
       always @(posedge clk) q <= d;
   endmodule
   ```

2. （前提：已按 u1-l2 构建出 `./build/yosys`）。运行：

   ```bash
   ./build/yosys -p "read_verilog -ppdump -dump_ast1 tiny.v"
   ```

**需要观察的现象**：

- `-ppdump` 会在日志里打印一段 `-- Verilog code after preprocessor --`，你能看到 `` `define WIDTH 4 `` 已经生效，`[`WIDTH-1:0]` 被替换成 `[4-1:0]`（或展开形式）。
- `-dump_ast1` 会打印一棵以 `AST_DESIGN` 为根、含 `AST_MODULE`（名字 `tiny`）、`AST_WIRE`（各端口）、`AST_ALWAYS`（含 `AST_BLOCK`、`AST_ASSIGN_LE`）的树。

**预期结果**：日志里依次出现「预处理后的代码」与「简化前的 AST 转储」。若想看 AST 转储的内存地址（便于 diff），可去掉 `-dump_ast1` 改用 `-debug`（等价于一组 dump 选项，见 `verilog_frontend.cc:338-346`）。

**待本地验证**：上述输出文本因版本而异，请以本地 `yosys` 实际打印为准；重点是确认「预处理在前、AST 在后」这个顺序。

#### 4.1.5 小练习与答案

**练习 1**：`read_verilog` 为什么不直接产出 RTLIL，而要先产 AST？

**参考答案**：因为 Verilog 的语义里有大量需要「上下文/参数」才能决定的东西（参数化模块、generate、位宽推导、函数内联等）。先建一棵贴近源码结构的 AST，再由专门的 simplify 阶段把这些「未决」的事情算清楚，最后才生成 RTLIL。这样「读语法」和「理解语义」解耦，词法器/语法器可以保持简单。

**练习 2**：`-nopp` 选项会跳过哪一步？什么场景下你会用它？

**参考答案**：跳过预处理器（`frontend_verilog_preproc`），词法器直接读原始文件流。当代码里不含任何 `` ` `` 指令、或你已自行预处理过、或想排查「预处理是否改变了语义」时可以使用。

---

### 4.2 `verilog_lexer.l`：词法分析

#### 4.2.1 概念说明

词法器是「字符流 → token 流」的转换器。Yosys 的 Verilog 词法器用 **flex** 生成，源文件是 `verilog_lexer.l`。它的 `.l` 文件由三段组成：

```
%{
   ...C++ 代码（include、宏、辅助函数）...
%}

   ...flex 声明（%option、命名正则、起始状态%x）...

%%
   ...规则区：每条形如  正则  { 动作 } ...
%%
   ...结尾 C++ 代码（通常为空）...
```

flex 的匹配原则有两条，理解它们就能读懂词法器：

- **最长匹配优先**：在所有能匹配当前输入的规则里，选匹配字符最多的那条。
- **规则顺序优先**：当多条规则匹配同样长度时，选文件里**先出现**的那条。

正因为「先出现的优先」，所以 `verilog_lexer.l` 里关键字（如 `"module"`）必须写在通用标识符规则 `[a-zA-Z_$][a-zA-Z0-9_$]*` **之前**，否则 `module` 会被当成普通标识符吃掉。

#### 4.2.2 核心流程

词法器是一个「按需被语法器拉取」的状态机：

1. 语法器（bison）每需要一个 token，就调用 `frontend_verilog_yylex(lexer)`，它转发到 `VerilogLexer::nextToken()`。
2. `nextToken()` 内部用 flex 生成的自动机，从输入流读字符、匹配规则。
3. 命中规则后执行其动作，动作通常是 `return parser::make_TOK_XXX(...)`——即「造一个 token 对象交还给语法器」。
4. 每次匹配前后，宏 `YY_USER_ACTION` 自动更新 `out_loc`（当前源码位置：文件名、行、列），用于错误定位和 AST 节点定位。
5. 匹配到 EOF 时返回 `FRONTEND_VERILOG_YYEOF`，语法器随之收尾。

词法器还要处理几类「非 token」的特殊输入：

- **注释**：`// ...`、`/* ... */` 用一个起始状态（start condition）`COMMENT` 吃掉，不产生 token。
- **空白**：`[ \t\r\n]` 直接忽略。
- **编译器指令的「非预处理」部分**：如 `` `timescale ``、`` `celldefine ``、`` `default_nettype `` 直接在这里吞掉或就地处理（预处理器的 `` `define ``/`` `include `` 在更早的 preproc 阶段已处理）。
- **Synopsys 风格注释**：`// synopsys translate_off`、`// synopsys full_case` 等遗留「热注释」，用 `SYNOPSYS_TRANSLATE_OFF` / `SYNOPSYS_FLAGS` 起始状态专门识别，并发出「建议改用 `ifdef` / 属性」的警告。
- **基于进制的常数**：`4'b1010` 这种要跨两个 token 识别（先 `'b`，再数字位串），用一个起始状态 `BASED_CONST` 来衔接。

#### 4.2.3 源码精读

**flex 生成 C++ 词法器**——这两行 `%option` 决定了 flex 生成一个继承自 `FlexLexer` 的 C++ 类 `VerilogLexer`：

[frontends/verilog/verilog_lexer.l:35-36](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_lexer.l#L35-L36) —— `%option c++` 要求生成 C++ 接口；`%option yyclass="VerilogLexer"` 把动作方法挂到我们自定义的 `VerilogLexer` 类上（该类声明在 `verilog_lexer.h`）。结合 CMake 的 `flex_target`，flex 会把 `.l` 编译成 `verilog_lexer.cc`。

**关键字 → token**——每条关键字就是一条「字符串 → make_TOK_XXX」规则，结构高度一致：

[frontends/verilog/verilog_lexer.l:356](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_lexer.l#L356) —— `"module"` 命中时返回 `TOK_MODULE`。

[frontends/verilog/verilog_lexer.l:375](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_lexer.l#L375) —— `"always"` → `TOK_ALWAYS`。

[frontends/verilog/verilog_lexer.l:379](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_lexer.l#L379) —— `"if"` → `TOK_IF`。

> 注意：这些关键字规则必须排在通用标识符规则之前，否则会被通用规则抢走——这正是「规则顺序优先」的体现。

**SystemVerilog 关键字守卫**——SV 专有关键字（如 `always_ff`）默认不认，只有在 `-sv` 下才作为关键字，否则退化为普通标识符并告警。这套行为被一个宏封装：

[frontends/verilog/verilog_lexer.l:72-78](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_lexer.l#L72-L78) —— `SV_KEYWORD(_tok)` 宏：若 `mode->sv` 为真则返回真正的关键字 token；否则打印告警，并把它当普通标识符（前缀 `\`，见下文）返回 `TOK_ID`。这样 Yosys 在默认 Verilog-2005 模式下不会被 SV 关键字卡住。

**标识符规则**——普通标识符匹配后，会被加上 `\` 前缀变成 `TOK_ID`（`\` 前缀是 RTLIL/AST 里「公有名」的约定，见 u3-l3）。同时若该名字曾被 `typedef`，则识别为 `TOK_USER_TYPE`：

[frontends/verilog/verilog_lexer.l:540-551](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_lexer.l#L540-L551) —— 标识符正则 `[a-zA-Z_$][a-zA-Z0-9_$]*`：先查 `isUserType` 决定是 `TOK_USER_TYPE` 还是 `TOK_ID`，无论如何都给名字加 `\` 前缀。

**基于进制的常数**——`8'h3F` 这种要分两步：先认 `'h`（进 `BASED_CONST` 状态，返回 `TOK_BASE`），再在 `BASED_CONST` 状态里认后面的位串 `3F`（返回 `TOK_BASED_CONSTVAL`）：

[frontends/verilog/verilog_lexer.l:470-480](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_lexer.l#L470-L480) —— 先 `\'[sS]?[bodhBODH]` 匹配进制标记，`BEGIN(BASED_CONST)` 切换状态并返回 `TOK_BASE`；接着在 `<BASED_CONST>` 下匹配位串数字（含 `x/z/?`）返回 `TOK_BASED_CONSTVAL` 并 `BEGIN(0)` 切回默认状态。这两段在语法器里会被拼起来还原成完整常数。

**注释与空白**——用起始状态 `COMMENT` 吃块注释，行内注释和空白直接忽略：

[frontends/verilog/verilog_lexer.l:702-710](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_lexer.l#L702-L710) —— `/*` 进 `COMMENT` 状态、忽略内容、`*/` 退出；`[ \t\r\n]` 忽略空白；`//...` 忽略行注释。注意这些规则对 `INITIAL` 和 `BASED_CONST` 两个状态都生效。

**单字符兜底**——任何没被前面规则命中的单字符（`+`、`(`、`;`、`{` 等）都交给 `char_tok` 映射成对应 token：

[frontends/verilog/verilog_lexer.l:712](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_lexer.l#L712) —— `<INITIAL>. { return char_tok(*YYText(), out_loc); }`，`char_tok` 是一个大 `switch`（定义在 [frontends/verilog/verilog_lexer.l:115-152](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_lexer.l#L115-L152)），把 `!`→`TOK_EXCL`、`(`→`TOK_LPAREN`、`;`→`TOK_SEMICOL` 等一一映射。

**位置追踪**——每个 token 的源码位置由 `YY_USER_ACTION` 在匹配发生时自动更新，这样错误信息和 AST 节点都能精确到行列：

[frontends/verilog/verilog_lexer.l:85-96](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_lexer.l#L85-L96) —— `YY_USER_ACTION` 宏对刚匹配的文本逐字符计数，遇 `\n` 调 `out_loc.lines()`（换行），否则 `out_loc.columns()`（进列），并刷新文件名。

#### 4.2.4 代码实践

**实践目标**：用 `-yydebug` 看到词法器真正吐出的 token 序列，验证「关键字是 token、标识符带 `\` 前缀、数字有专门 token」。

**操作步骤**：

1. 用上面的 `tiny.v`（示例代码），运行：

   ```bash
   ./build/yosys -p "read_verilog -yydebug tiny.v" 2>&1 | head -60
   ```

2. `-yydebug` 会同时打开词法器调试（`lexer.set_debug`）和语法器调试（`parser.set_debug_level(1)`），你会看到大量 `--(end of symbol rule)--` / `Reading a token` 之类的 bison 调试行。

**需要观察的现象**：

- 在 bison 调试输出里能隐约看到 `TOK_MODULE`、`TOK_ID`、`TOK_SEMICOL`、`TOK_ALWAYS`、`TOK_IF`（若用到）等 token 名。
- 标识符名在 AST 层会以 `\` 开头（如 `\clk`、`\d`、`\q`）。

**预期结果**：你能把一段源码对应到一串 token 上，建立起「字符 → token」的直觉。

**待本地验证**：`-yydebug` 输出非常冗长且版本相关，重点感受「token 流」这个概念即可，不必逐行核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `"module"` 这条规则（行 356）必须写在通用标识符规则（行 540）之前？

**参考答案**：因为 flex 是「最长匹配优先，其次规则顺序优先」。`module` 这 6 个字符同时满足 `"module"` 和 `[a-zA-Z_$]...` 两条规则，且匹配长度相同；此时文件里**先出现**的规则胜。若通用标识符在先，`module` 就会被当普通标识符返回 `TOK_ID`，关键字就失效了。

**练习 2**：`4'b101x` 会被切成哪几个 token？

**参考答案**：三个：`TOK_CONSTVAL`（值为 `4`）、`TOK_BASE`（值为 `'b`）、`TOK_BASED_CONSTVAL`（值为 `101x`）。前缀 `4` 由 `{UNSIGNED_NUMBER}` 规则产生，`'b` 触发 `BEGIN(BASED_CONST)` 并返回 `TOK_BASE`，随后在 `BASED_CONST` 状态匹配位串返回 `TOK_BASED_CONSTVAL`。

**练习 3**：词法器如何知道某个标识符是不是 `typedef` 出来的用户类型？

**参考答案**：通过 `parse_state->user_type_stack`（一个类型名查找表的栈）。`isUserType()` 从内层作用域向外层逐层查找，命中则返回 `TOK_USER_TYPE`，否则返回 `TOK_ID`。这就是为什么词法器需要 `parse_state` 指针。

---

### 4.3 `verilog_parser.y`：语法分析与 AST 构造

#### 4.3.1 概念说明

语法器用 **bison** 生成，源文件是 `verilog_parser.y`（约 3600 行，是本讲最大的文件）。它的核心职责是：**读入 token 流，按一组「产生式」把它们归约成一棵 AST，并且边归约边在内存里把 AST 节点造好**。

这点很关键：和「先建语法树再翻译」的编译器不同，Yosys 的 bison 动作（action）**直接 new 出 `AST::AstNode`**。每条产生式的动作就是一段 C++ 代码，里面调用 `extra->pushChild(...)` / `extra->saveChild(...)` 把节点挂到当前正在构造的 AST 子树上。

bison 的归约遵循 LALR(1)：它维护一个「状态栈」，每读一个 token 就查表决定「移进（shift，把 token 压栈）」还是「归约（reduce，把栈顶若干项替换成一条产生式的左部，并执行其动作）」。你不必记住算法细节，只要理解：**当归约发生时，产生式右部各符号的「语义值」（`$1 $2 ...`）已经就绪，动作里可以用它们来构造父节点（`$$`）**。

Yosys 的语法器用了一个值得注意的配置：

- `%define api.value.type variant`：用 C++ 的 `variant` 风格类型（如 `std::unique_ptr<std::string>`、`std::unique_ptr<AstNode>`）作为语义值，而不是传统的 C `union`，更安全。
- `%define api.location.type {Location}`：位置类型用自实现的 `Location`（`verilog_location.h`），而不是 bison 默认的。
- `%parse-param`：把 `ParseState*` 和 `ParseMode*` 作为额外参数透传进所有动作，动作里通过 `extra->...` 访问共享状态。

#### 4.3.2 核心流程

整棵 AST 的构造依靠一个「AST 栈」`parse_state.ast_stack`：

1. **播下种子**：解析开始前，`input:` 规则把 `verilog_frontend.cc` 建好的 `AST_DESIGN` 根节点压进 `ast_stack`。从此栈底就是这棵树的总根。
2. **递归下降式归约**：`design` 规则是右递归的——`design: module design | ... | %empty`，每识别出一个 `module` 就把它挂到栈顶（也就是 `AST_DESIGN`）下面。
3. **进入模块**：`module` 规则识别 `module 名字 ( 端口 ) ; 模块体 endmodule`，在动作里 `new AstNode(AST_MODULE)`，用 `pushChild` 挂上去并把它压栈，之后模块体里的 `wire`/`always`/`assign` 等就挂在这个模块节点下，`endmodule` 时弹栈。
4. **语句嵌套**：`always` → `AST_ALWAYS` + `AST_BLOCK`；块内的 `if`/`case`/赋值各对应一种 AST 节点，靠 `pushChild`/`pop_back` 维护「当前在构造哪个节点的孩子」。
5. **收尾**：所有 token 归约完毕，`ast_stack` 应只剩那个 `AST_DESIGN` 根，里面挂满了模块子树。

这套「栈式构造」是读懂 `verilog_parser.y` 动作代码的总钥匙。两个最常用的辅助函数：

[frontends/verilog/verilog_parser.y:244-253](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_parser.y#L244-L253) —— `saveChild` 把一个新节点挂到「栈顶节点的 children」并返回裸指针；`pushChild = saveChild + 压栈`，即「挂上去并让它成为新的栈顶」，从而后续节点会变成它的孩子。

#### 4.3.3 源码精读

**bison 生成 C++ 语法器**——文件顶部的指令决定生成方式与传参：

[frontends/verilog/verilog_parser.y:36-45](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_parser.y#L36-L45) —— `%language "c++"`、`%define api.value.type variant`（用 `unique_ptr` 等强类型语义值）、`%define api.location.type {Location}`、`%parse-param` 透传 `ParseState* extra` 和 `ParseMode* mode`。CMake 的 `bison_target`（`CMakeLists.txt:9-15`）据此生成 `verilog_parser.tab.cc`。

**共享状态**：`ParseState` 是语法过程的「工作台」，`ParseMode` 是「只读配置」：

[frontends/verilog/verilog_parser.y:69-122](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_parser.y#L69-L122) —— `ParseState` 持有 `ast_stack`、`attr_list`（待附加的属性）、`user_type_stack`、`case_type_stack`、`current_ast`/`current_ast_mod` 等可变状态；`ParseMode` 持有 `sv`/`formal`/`lib`/`specify` 等来自命令行的开关。

**token 声明**——词法器返回的 token 在这里登记，并声明其语义值类型：

[frontends/verilog/verilog_parser.y:492-498](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_parser.y#L492-L498) —— 声明 `TOK_MODULE`、`TOK_ALWAYS`、`TOK_IF`、`TOK_POSEDGE`、`TOK_CASE` 等 token；其中带值的（如 `TOK_ID`、`TOK_CONSTVAL`）声明为 `<string_t>`，即动作里 `$n` 是 `std::unique_ptr<std::string>`。

**种子规则**——`input:`（bison 自动加的起始规则）在解析开始时把 `AST_DESIGN` 压栈，结束时校验栈空：

[frontends/verilog/verilog_parser.y:593-601](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_parser.y#L593-L601) —— `extra->ast_stack.push_back(extra->current_ast)` 把 4.1 里建好的 `AST_DESIGN` 压栈作为根，然后归约 `design`，结束后 `pop_back` 并断言栈空。

**顶层递归**——`design` 是右递归列表，串起所有顶层结构：

[frontends/verilog/verilog_parser.y:603-614](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_parser.y#L603-L614) —— `design: module design | defattr design | task_func_decl design | ... | %empty`，即「一个设计由若干模块/包/接口/typedef 等串联而成」，最后以 `%empty` 收尾。

**`module` 产生式 → `AST_MODULE`**——这是本讲实践要找的第一条：

[frontends/verilog/verilog_parser.y:694-715](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_parser.y#L694-L715) —— 动作里 `extra->pushChild(std::make_unique<AstNode>(@$, AST_MODULE))` 创建模块节点并压栈，`mod->str = *$4`（`$4` 是 `TOK_ID`，即模块名），随后归约 `module_para_opt`/`module_args_opt`/`module_body` 把参数、端口、模块体挂到该节点下，`endmodule` 时 `pop_back` 并校验 `ast_stack.size()==1`（只剩 `AST_DESIGN`）。模块体由 `module_body`（[行 1118-1129](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_parser.y#L1118-L1129)）罗列，其中就包含 `always_stmt`。

**`always` 产生式 → `AST_ALWAYS` + `AST_BLOCK`**——这是本讲实践要找的第二条：

[frontends/verilog/verilog_parser.y:2457-2493](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_parser.y#L2457-L2493) —— `always_stmt` 第一支处理 `always` / `always_ff`：`pushChild(AST_ALWAYS)` 建时序块节点，若是 `always_ff` 还会加 `always_ff` 属性；接着归约 `always_cond`（敏感列表，如 `@(posedge clk)`），再 `pushChild(AST_BLOCK)` 建语句块；其下的 `behavioral_stmt`（块内语句）就挂到这个 `AST_BLOCK` 下。第二/三支分别处理 `always_comb`/`always_latch` 和 `initial`。

**`if` 产生式 → `AST_CASE` + `AST_COND`（关键反直觉点）**——这是本讲实践要找的第三条，也是最容易让人困惑的一条：**Verilog 的 `if` 在 AST 里并不是一个「if 节点」，而是被翻译成一个 `AST_CASE`（一个分支的 case），条件被包成 `AST_REDUCE_BOOL`**：

[frontends/verilog/verilog_parser.y:2897-2940](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_parser.y#L2897-L2940) —— `if_attr TOK_IF TOK_LPAREN expr TOK_RPAREN ...`：动作把条件 `$4` 包成 `AST_REDUCE_BOOL`（把任意值归约为布尔），然后 `new AstNode(AST_CASE, ...)` 创建一个 case 节点，再 `new AstNode(AST_COND, 常量1, block)` 创建一个「条件恒真」的分支作为 then 分支。`optional_else` 处理 else（通常是另一个 `AST_COND` 默认分支）。这种「if 即 case」的统一表示，让后续的 proc pass（见 u6-l2）只需处理一种结构。

> 对比：表达式层面的 `条件 ? a : b` 三目运算符才会产生 `AST_TERNARY`（见 [verilog_parser.y:3256](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_parser.y#L3256)）；而**语句层面**的 `if` 产生 `AST_CASE`。两者不要混淆。

**case 产生式**——和 `if` 共用同一套 `AST_CASE`/`AST_COND` 表示，只是每个分支的条件是真实的 `case` 值：

[frontends/verilog/verilog_parser.y:2942-2950](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_parser.y#L2942-L2950) —— `case_attr case_type TOK_LPAREN expr TOK_RPAREN ...`：`pushChild(AST_CASE, $4)` 创建 case 节点，分支在 `case_body`（[行 3040](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_parser.y#L3040)）里逐个产生 `AST_COND`/`AST_CONDX`/`AST_CONDZ`（取决于 `case`/`casex`/`casez`，由 `case_type_stack` 区分）。

**乘法动作示例**——官方文档给的最简动作样板，展示了 `$1`/`$4` 如何拼成 `$$`：

文档 [docs/source/yosys_internals/flow/verilog_frontend.rst](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/yosys_internals/flow/verilog_frontend.rst) 里 `basic_expr TOK_ASTER attr basic_expr` 的动作是 `$$ = std::make_unique<AstNode>(AST_MUL, std::move($1), std::move($4));`——把左右两个表达式子树合成一个 `AST_MUL` 节点。这是所有表达式产生式的通用写法。

**完整 AST 节点类型对照**——「Verilog 构造 → AST 节点类型」的权威映射表在官方文档里，建议对照阅读：

[docs/source/yosys_internals/flow/verilog_frontend.rst:71-159](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/yosys_internals/flow/verilog_frontend.rst#L71-L159) —— 一张表列出全部 `AstNodeType` 及其对应的 Verilog 构造（`module`→`AST_MODULE`、`always`→`AST_ALWAYS`、`if`/`case`→`AST_CASE`/`AST_COND`、`+`→`AST_ADD`……）。源码定义在 `frontends/ast/ast.h`，是 u5-l3 的主题。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：完成规格里指定的主实践——在 `verilog_parser.y` 中找出 `module`/`always`/`if` 的产生式，说明它们各自构造了哪种 AST 节点，并用 `read_verilog -dump_ast1` 观察 AST 输出。

**操作步骤**：

1. **在源码里定位三条产生式**（纯源码阅读，无需运行）：
   - `module`：`verilog_parser.y:694`，构造 `AST_MODULE`（动作里 `make_unique<AstNode>(@$, AST_MODULE)`）。
   - `always`：`verilog_parser.y:2457`（`always_stmt` 第一支），构造 `AST_ALWAYS`，并再嵌一个 `AST_BLOCK` 作为语句块。
   - `if`：`verilog_parser.y:2897`（`behavioral_stmt` 的一支），构造 `AST_CASE` + `AST_COND`，条件被包成 `AST_REDUCE_BOOL`。

2. **准备示例**（示例代码，非项目原有）`mux_if.v`：

   ```verilog
   module mux_if(input sel, input [3:0] a, input [3:0] b, output reg [3:0] y);
       always @(*) begin
           if (sel) y = a;
           else     y = b;
       end
   endmodule
   ```

3. **运行并转储 AST**：

   ```bash
   ./build/yosys -p "read_verilog -dump_ast1 mux_if.v" 2>&1 | grep -A40 "AST_DESIGN"
   ```

**需要观察的现象**：

- 顶层是 `AST_DESIGN`，下挂一个 `AST_MODULE`（名字 `\mux_if`）。
- 模块里有若干 `AST_WIRE`（端口）和一个 `AST_ALWAYS`。
- `AST_ALWAYS` 下是 `AST_BLOCK`，再下是一个 `AST_CASE`（注意：是 case，不是 if！），里面有两个 `AST_COND` 分支，第一个分支的条件是 `AST_REDUCE_BOOL(...)`、第二个是 default。

**预期结果**：你亲眼验证了「源码里的 `if` 在 AST 里变成了 `AST_CASE`/`AST_COND`」这个反直觉但重要的事实。

**待本地验证**：AST 转储的具体缩进/字段名随版本微调，以本地输出为准；核心是确认 `AST_CASE` 出现在 `if` 处。

#### 4.3.5 小练习与答案

**练习 1**：`module` 产生式动作里的 `extra->pushChild(...)` 和结尾的 `extra->ast_stack.pop_back()` 成对出现，这说明了什么？

**参考答案**：说明 AST 构造是「栈式」的。`pushChild` 把新模块节点挂到当前父节点（`AST_DESIGN`）下**并把它压栈**，使后续 `module_body` 里的子节点都挂到这个模块下；`endmodule` 时 `pop_back` 把模块弹出栈，回到 `AST_DESIGN` 层级，于是下一个模块又能正确挂到 `AST_DESIGN` 下。

**练习 2**：为什么 Verilog 的 `if` 语句在 AST 里没有对应的 `AST_IF` 节点，而是变成 `AST_CASE`？

**参考答案**：Yosys 选择用「case」这一种结构统一表达「分支」，`if-else` 被建模成「条件为真的分支 + default 分支」。这样下游的 `proc` pass（把行为级翻译成 mux/dff，见 u6-l2）只需处理 `AST_CASE` 一种结构，降低了后续实现的复杂度。`if` 的条件被 `AST_REDUCE_BOOL` 归一成布尔值，便于作为 case 的匹配条件。

**练习 3**：动作里写的 `$$`、`$1`、`$4` 分别代表什么？

**参考答案**：`$$` 是当前产生式**左部**的语义值（通常是要 new 出来的父节点）；`$1`、`$4` 是**右部第 1、第 4 个符号**的语义值（已被更早的归约填好，可能是子树 `unique_ptr<AstNode>` 或字符串 `unique_ptr<std::string>`）。在乘法动作里 `$$ = make_unique<AstNode>(AST_MUL, $1, $4)` 即「用左右两个子表达式构造一个乘法节点」。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「全链路观察」：从一个 Verilog 文件出发，分别截取**预处理后文本**、**token**（间接）、**AST** 三个中间产物，并解释它们的对应关系。

**任务**：

1. 准备一个稍复杂的设计（示例代码，非项目原有）`practice.v`，包含模块例化、参数、`always`+`if`+`case`：

   ```verilog
   module practice #(parameter W = 4)(
       input clk, input rst, input [W-1:0] d, output reg [W-1:0] q);
       reg [W-1:0] state;
       always @(posedge clk) begin
           if (rst)        state <= 4'd0;
           else case (d[1:0])
               2'b00: state <= d;
               default: state <= ~d;
           endcase
           q <= state;
       end
   endmodule
   ```

2. 三步观察（前两步是「放大镜」，第三步是「产物」）：

   ```bash
   # ① 预处理后文本（参数 W=4 是否被替换）
   ./build/yosys -p "read_verilog -ppdump practice.v" 2>&1 | sed -n '/Verilog code after preprocessor/,/END OF DUMP/p'

   # ② bison 调试（token 与归约过程，输出很长，挑关键行）
   ./build/yosys -p "read_verilog -yydebug practice.v" 2>&1 | grep -E "TOK_MODULE|TOK_ALWAYS|TOK_IF|TOK_CASE" | head

   # ③ 简化前的完整 AST
   ./build/yosys -p "read_verilog -dump_ast1 practice.v" 2>&1 | sed -n '/AST_DESIGN/,$p'
   ```

3. **写一段分析**（这是本实践的交付物）：
   - 指出 `parameter W = 4` 在①里如何体现，在③里对应哪种 AST 节点（`AST_PARAMETER`）。
   - 指出 `always @(posedge clk)` 在③里对应 `AST_ALWAYS`，其下的 `AST_BLOCK` 里包含哪些子节点。
   - 指出 `if (rst)` 和 `case (d[1:0])` 在③里分别对应什么样的 `AST_CASE`/`AST_COND` 结构，验证「`if` 也是 `AST_CASE`」。
   - 用一句话回答：综合（synth）之后，这些 `AST_CASE`/`AST_ALWAYS` 会被 proc pass 变成什么（提示：`$mux`/`$dff`，见 u6-l2）？

**验收标准**：你能拿着自己写的分析，对照 AST 转储，把 `practice.v` 的每一行源码映射到 AST 里的某个节点类型。这就证明你真正读懂了「Verilog 文本 → AST」这一段。

> 说明：以上命令的精确输出与版本相关，请以本地为准；若某条管道过滤不到内容，直接看完整日志即可。

---

## 6. 本讲小结

- `read_verilog` 是一个 `Frontend` 子类，它把 Verilog **先**变成 AST（`AST_DESIGN` 根），**再**由 `AST::process()` 变成 RTLIL；本讲只覆盖前半段。
- 流水线是：**预处理器（preproc.cc）→ 词法器（verilog_lexer.l，flex）→ 语法器（verilog_parser.y，bison）→ AST**；`verilog_frontend.cc` 是总指挥，负责把它们按顺序串起来。
- 词法器是「正则 → token」的状态机：关键字在通用标识符之前以保优先级；标识符加 `\` 前缀成 `TOK_ID`；`8'h3F` 这类数跨 `TOK_BASE`+`TOK_BASED_CONSTVAL` 两个 token；`YY_USER_ACTION` 维护精确行列位置。
- 语法器是 LALR(1)，用 `variant` 风格语义值、自实现 `Location`、通过 `%parse-param` 透传 `ParseState`/`ParseMode`；动作**直接 new `AstNode`**，靠 `ast_stack` 的压栈/弹栈维护父子关系。
- 关键反直觉点：Verilog **语句级**的 `if`/`case` 都映射成 `AST_CASE`+`AST_COND`（`if` 的条件被包成 `AST_REDUCE_BOOL`），而**表达式级**的三目 `?:` 才是 `AST_TERNARY`。
- flex 与 bison 的产物由 `CMakeLists.txt` 的 `flex_target`/`bison_target` 生成（`verilog_lexer.cc`、`verilog_parser.tab.cc`），再由 `yosys_frontend(verilog ...)` 组装。

## 7. 下一步学习建议

- **继续往下读前端**：本讲只到 AST 为止。接下来 u5-l2 讲 `preproc.cc`（预处理器细节）与 `const2ast.cc`（常数如何变成 `AST_CONSTANT`）；u5-l3 讲 `frontends/ast/ast.h` 的 `AstNode` 模型与 `AstNodeType` 枚举；u5-l4 讲 `simplify.cc` + `genrtlil.cc` 如何把 AST 翻译成 RTLIL。
- **验证 AST 直觉**：现在你已经会读 AST 转储了，建议立刻在 `practice.v` 上多试几个 `-dump_ast1`/`-dump_ast2`（简化前后对比），观察 simplify 做了哪些变换——这正是 u5-l4 的预演。
- **跨到综合侧**：本讲提到 `AST_CASE`/`AST_ALWAYS` 最终会被 `proc` pass 翻成 `$mux`/`$dff`。学完 u5-l4 后可直接跳到 u6-l2（proc），形成「行为级 Verilog → 门级网表」的完整闭环。
- **官方文档**：`docs/source/yosys_internals/flow/verilog_frontend.rst` 的 AST 节点对照表（行 71-159）是最权威的「Verilog 构造 ↔ AST 节点」参考，遇到不确定的节点类型随时回查。
