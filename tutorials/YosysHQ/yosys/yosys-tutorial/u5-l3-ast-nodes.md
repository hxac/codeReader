# AST 节点模型与 AstModule

## 1. 本讲目标

在 u5-l1 中我们看到，`read_verilog` 并不直接产出 RTLIL，而是先用 flex/bison 把源码解析成一棵**抽象语法树（AST）**，再由 `frontends/ast/` 把这棵树翻译成 RTLIL。本讲就钻进这棵树本身，回答三个问题：

1. **树上的节点都有哪些种类？** —— `AstNodeType` 枚举是 AST 的「词汇表」。
2. **一个节点在内存里长什么样？** —— `AstNode` 结构体是 AST 的「万能积木」。
3. **带参数的模块为什么不会马上变成 RTLIL？** —— `AstModule` 把 AST「留档」，等到参数确定时再展开。

学完后你应当能够：读懂一段 AST 转储（`-dump_ast1`/`-dump_ast2`），说出某个运算符会落到哪个内部 `$` 单元，并理解 Yosys 用「延迟展开 + 派生（derive）」支持参数化模块的整体思路。

## 2. 前置知识

- **抽象语法树（AST）**：把源码按语法结构拆成的一棵树。树叶是常量、标识符；内部节点是运算符、语句、声明。后续处理（类型推导、化简、代码生成）都在树上做。
- **标记联合（tagged union）思想**：用「一个类型标签 + 一组字段」来表示多种事物。Yosys 的 `AstNode` 就是这种设计——同一个结构体既能表示一个模块，也能表示一次加法。
- **`unique_ptr` 与所有权**：C++ 的 `std::unique_ptr<T>` 表示「独占拥有」。父节点用 `vector<unique_ptr<AstNode>>` 持有子节点，意味着**父节点销毁时子节点自动销毁**，整棵树无需手动 `delete`。
- 本讲承接 u5-l1（词法/语法分析、`AST_DESIGN` 根节点、bison 动作里 `new AstNode`）与 u3-l4（内部 `$` 单元库，如 `$and`/`$add`/`$mux`）。如果对 RTLIL 的 Wire/Cell 还不熟，建议先看 u2-l3。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `frontends/ast/ast.h` | 声明 `AstNodeType` 枚举、`AstNode` 结构体、`AstModule` 类，以及入口函数 `AST::process`。本讲的主战场。 |
| `frontends/ast/ast.cc` | 上述声明的实现：`type2str`、节点构造/克隆/转储、常量工厂、`process`/`process_module`/`derive`/`derive_common` 等编排逻辑。 |
| `frontends/ast/genrtlil.cc` | （辅助引用）`AstNode::genRTLIL()` 的实现，演示 AST 运算符如何映射到 `$` 单元。 |
| `frontends/verilog/verilog_frontend.cc` | （辅助引用）`read_verilog` 的 `-dump_ast1`/`-dump_ast2` 选项，是观察 AST 的入口。 |

> 提示：`frontends/ast/` 不是一个独立前端，而是被 `frontends/verilog/` 复用的「AST 库」。它的注释明确写道：*"The AST frontend library is not a frontend on its own but provides an abstract syntax tree (AST) abstraction"*（见 [frontends/ast/ast.h:20-23](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.h#L20-L23)）。

---

## 4. 核心概念与源码讲解

### 4.1 AstNodeType 枚举：AST 的词汇表

#### 4.1.1 概念说明

`read_verilog` 解析源码时，bison 的每一个产生式（production）动作都会 `new` 出一个 `AstNode`，并用一个 `AstNodeType` 枚举值标注它「是哪一种语法成分」。因此 `AstNodeType` 本质上是 **Verilog 语法到 AST 节点的一张映射表**——它列出了 Yosys 能识别的全部语法结构。

可以把这个枚举想象成一套「零件编号」：

- `AST_DESIGN`：整棵树的根，对应一个完整的 Verilog 文件集合。
- `AST_MODULE` / `AST_TASK` / `AST_FUNCTION` / `AST_PACKAGE`：各类顶层声明。
- `AST_WIRE` / `AST_MEMORY` / `AST_PARAMETER` / `AST_LOCALPARAM`：变量、存储器、参数声明。
- `AST_ALWAYS` / `AST_INITIAL` / `AST_BLOCK` / `AST_CASE` / `AST_COND`：行为级语句（always 块、initial、顺序块、case）。
- `AST_ASSIGN` / `AST_ASSIGN_EQ` / `AST_ASSIGN_LE`：连续赋值（`assign`）、阻塞赋值（`=`）、非阻塞赋值（`<=`）。
- `AST_BIT_AND`/`AST_ADD`/`AST_TERNARY` …：表达式运算符。
- `AST_CONSTANT` / `AST_IDENTIFIER`：常量与标识符，通常是树叶。
- `AST_GENFOR`/`AST_GENIF`/`AST_GENBLOCK`：generate 结构。

#### 4.1.2 核心流程

枚举本身只是一个列表，但它和两条机制紧密咬合：

1. **构造时打标签**：bison 动作里 `mkast(type, ...)` 创建节点时把 `type` 写入 `AstNode::type` 字段，这棵树的「身份」就确定了。
2. **`type2str()` 做反查**：调试、转储（`-dump_ast`）、报错时需要把整数枚举还原成可读字符串，`type2str()` 负责这件事。它的注释特别提醒：**新增枚举值时必须同步扩展 `type2str`**（见 [frontends/ast/ast.h:39-40](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.h#L39-L40)），否则会命中 `log_abort()`。

`type2str` 的实现很巧妙：用 X-macro（宏 `X(...)` 展开成 `case _item: return #_item;`），让枚举值到字符串的映射**与枚举定义同源**，避免两处手写清单脱节（见 [frontends/ast/ast.cc:62-187](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L62-L187)）。

> 名字小知识：语句级的 `if`/`case` 在 AST 里都被建模成 `AST_CASE` + `AST_COND`（if 的条件被包成 `AST_REDUCE_BOOL`，详见 u5-l1）；只有表达式里的三目 `?:` 才是 `AST_TERNARY`。这是 u5-l1 已经点过的反直觉点，本讲在 4.1.4 实践里会再次验证。

#### 4.1.3 源码精读

完整的枚举定义在这里（按「设计/声明 → 线网参数 → 运算符 → 语句 → generate → 边沿 → SV 扩展」分组排列）：

[frontends/ast/ast.h:41-166](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.h#L41-L166) —— `AstNodeType` 枚举，列出全部节点类型，是 AST 的「词汇表」。

`type2str` 用 X-macro 做枚举到字符串的映射：

[frontends/ast/ast.cc:62-187](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L62-L187) —— 把每个 `AST_xxx` 还原成字符串 `"AST_xxx"`；`#define X(_item) case _item: return #_item;` 一行宏展开所有分支，未命中的走 `log_abort()`。

本讲实践任务要重点对照的几个运算符节点（位运算、算术、三目）都在枚举中部：

[frontends/ast/ast.h:82-117](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.h#L82-L117) —— 从 `AST_BIT_NOT` 到 `AST_TERNARY`，覆盖位运算、归约运算、移位、比较、算术、逻辑、三目。

#### 4.1.4 代码实践

**实践目标**：亲手让 `read_verilog` 把源码解析成 AST，并对照枚举辨认节点类型。

**操作步骤**：

1. 准备一个小文件 `tiny.v`（示例代码，非项目原有）：
   ```verilog
   module tiny(input a, b, c, output y);
     assign y = (a & b) | c;
   endmodule
   ```
2. 运行：
   ```bash
   ./build/yosys -p "read_verilog -dump_ast1 tiny.v"
   ```
   `-dump_ast1` 会打印**化简之前**的 AST（见 [frontends/verilog/verilog_frontend.cc:116-120](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L116-L120) 与 [frontends/verilog/verilog_frontend.cc:347-352](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L347-L352)）。

**需要观察的现象**：输出里应出现形如 `AST_MODULE`、`AST_WIRE`、`AST_ASSIGN`，以及表达式部分 `AST_BIT_OR` 套着 `AST_BIT_AND` 和 `AST_IDENTIFIER`。

**预期结果**：你会在转储里看到 `(a & b)` 对应一个 `AST_BIT_AND` 节点，它的两个 `children` 是 `AST_IDENTIFIER`（`a`、`b`）；外层 `| c` 是 `AST_BIT_OR`。这印证了「运算符 → 一个 AST 节点，操作数 → 它的子节点」。

> 若手头没有可执行的 yosys，可标注「待本地验证」，转而直接阅读 [frontends/ast/ast.cc:744-775](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L744-L775) 的 `dumpVlog` 二元运算分支，里面用 `if(0){case AST_BIT_AND: txt="&";}` 的「fall-through 技巧」把每个运算符映射到符号，等价地证明了同一份对应关系。

#### 4.1.5 小练习与答案

**练习 1**：`AST_EQ` 和 `AST_EQX` 分别对应 Verilog 里的哪个运算符？为什么需要两个？
> 答案：`AST_EQ` ↔ `==`，`AST_EQX` ↔ `===`。前者按「四值逻辑」比较（`x`/`z` 会传播，结果可能为 `x`），后者按「逐位严格」比较（`x`/`z` 参与比较但结果是确定的 0/1）。Yosys 需要区分它们，因为生成 RTLIL 时 `AST_EQ` → `$eq`、`AST_EQX` → `$eqx` 是不同单元（见 [frontends/ast/genrtlil.cc:1915-1919](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1915-L1919)）。

**练习 2**：在枚举里找 `AST_POSEDGE`/`AST_NEGEDGE`/`AST_EDGE`，它们通常出现在谁的 `children` 里？
> 答案：它们表示敏感列表里的边沿事件，作为 `AST_ALWAYS` 的子节点存在（`always @(posedge clk)` 的 `posedge clk` 就是一个 `AST_POSEDGE`）。可在 `dumpVlog` 的 `AST_ALWAYS` 分支印证（见 [frontends/ast/ast.cc:551-565](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L551-L565)）。

---

### 4.2 AstNode 结构：万能积木

#### 4.2.1 概念说明

`AstNode` 是一个**单一结构体**，用来表示上面枚举里的**所有**节点类型。这是一种典型的「标记联合」设计：用一个 `type` 字段当标签，再配上一大包字段，哪种字段有用取决于 `type`。源码注释直言：*"node content - most of it is unused in most node types"*（见 [frontends/ast/ast.h:190](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.h#L190)）——「大部分字段在大部分节点里都没用」。

举例：

- `AST_CONSTANT` 节点用 `bits`（位向量）和 `integer` 存数值；`str`、`range_left` 等基本闲置。
- `AST_WIRE` 节点用 `str` 存线名、`is_input`/`is_output`/`is_reg` 存端口方向、`range_left`/`range_right` 存位宽。
- `AST_IDENTIFIER` 节点用 `str` 存名字，`id2ast` 指向它所引用的那个 `AST_WIRE`/`AST_PARAMETER` 节点。

这种「字段共享、按需取用」的代价是有些浪费内存，但换来的是**统一接口**：所有节点都能被同一套遍历代码处理。

#### 4.2.2 核心流程

一个 `AstNode` 由四部分组成：

1. **类型标签**：`AstNodeType type`——这个节点的「身份证」。
2. **树结构**：
   - `std::vector<std::unique_ptr<AstNode>> children`——子节点列表（独占所有权，父拥子）。
   - `std::map<RTLIL::IdString, std::unique_ptr<AstNode>> attributes`——属性表（如 `(* keep *)`），值本身也是 `AstNode`（通常是 `AST_CONSTANT`）。
3. **内容字段**（按需使用）：
   - `std::string str`：名字/字符串值。
   - `std::vector<RTLIL::State> bits`：常量的位向量（含 `0/1/x/z`）。
   - `bool is_input/is_output/is_reg/is_logic/is_signed/is_string/...`：各种标志位。
   - `int port_id, range_left, range_right`：端口号、位宽范围。
   - `uint32_t integer`；`double realvalue`：整数/实数值。
   - `std::vector<dimension_t> dimensions`；`int unpacked_dimensions`：数组维度的 packed/unpacked 维。
4. **辅助状态**：
   - `AstNode* id2ast`：**非拥有**指针，由 `simplify()` 设置，指向标识符解析后的目标节点（如某个 `AST_WIRE`），供 `genRTLIL()` 快速查表。
   - `AstSrcLocType location`：源码位置（文件名 + 行列），用于报错。
   - `bool basic_prep`、`lookahead`、`in_lvalue`、`in_param` 等：`simplify` 与 `genRTLIL` 阶段的工作标志。

**生命周期与克隆**：构造函数 `AstNode(loc, type, child1, child2, child3, child4)` 接受至多 4 个内联子节点，方便 bison 动作一行建树；`clone()` 做深拷贝（连同 `children` 和 `attributes` 递归复制）。因为子节点是 `unique_ptr`，析构时整棵子树自动释放。

**两条「后处理」管线**挂在 `AstNode` 上：

- `simplify(...)`：在树上做常量折叠、宽度推导、展开 `generate`、解析标识符（写 `id2ast`）等，把树化简成可以直接生成 RTLIL 的形态。
- `genRTLIL(...)`：把（化简后的）表达式/语句节点翻译成 RTLIL 的 Wire/Cell，返回该表达式对应的 `RTLIL::SigSpec`。

伪代码示意「一个二元运算节点如何被消费」：

```
节点 N = AST_BIT_AND, children=[A, B]
detectSignWidth(width, sign)        # 推导结果位宽
left  = A.genRTLIL(width, sign)     # 递归生成左操作数信号
right = B.genRTLIL(width, sign)     # 递归生成右操作数信号
return binop2rtlil(N, $and, width, left, right)   # 产出 RTLIL $and 单元
```

#### 4.2.3 源码精读

`AstNode` 结构体的骨架（类型、子节点、属性、内容字段）：

[frontends/ast/ast.h:174-210](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.h#L174-L210) —— `children`（`vector<unique_ptr<AstNode>>`，父拥子）、`attributes`（`map<IdString, unique_ptr<AstNode>>`）、`str/bits/is_signed/range_left/integer/realvalue/dimensions` 等内容字段。

`id2ast` 与 `location`——标识符解析结果与源码位置：

[frontends/ast/ast.h:212-224](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.h#L212-L224) —— `id2ast` 注释说明它由 `simplify` 设置、供 `genRTLIL` 使用；`location` 由构造函数据 `current_filename` 与 `get_line_num()` 自动填入。

构造函数：初始化所有标志位并把传入的子节点压入 `children`：

[frontends/ast/ast.cc:204-251](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L204-L251) —— 给每个布尔/整数字段一个安全默认值（如 `range_left=-1`、`range_right=0`、`port_id=0`），再把 `child1..4` 依次 `push_back`，最后 `fixup_hierarchy_flags()`。

常量工厂 `mkconst_int` / `mkconst_bits` / `mkconst_str`：构造 `AST_CONSTANT` 节点的便捷方法：

[frontends/ast/ast.cc:855-887](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L855-L887) —— `mkconst_int` 把一个 `uint32_t` 按位拆进 `bits`；`mkconst_bits` 直接接收位向量并同步算出 `integer`（取低 32 位）。这两个工厂在派生参数、生成临时线时被大量复用。

`cloneInto` 的深拷贝：递归克隆 `children` 与 `attributes`：

[frontends/ast/ast.cc:262-303](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L262-L303) —— 逐字段复制标量，再 `child->clone()` 递归复制每个子节点，属性表同理；这是 `AstModule::derive` 能「拿原 AST 改参数再展开」的基础。

`genRTLIL` 里运算符到 `$` 单元的映射（承接 4.1 的枚举）：

[frontends/ast/genrtlil.cc:1843-1846](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1843-L1846) —— `AST_BIT_AND→$and`、`AST_BIT_OR→$or`、`AST_BIT_XOR→$xor`、`AST_BIT_XNOR→$xnor`，经 `binop2rtlil` 产出二元单元。

[frontends/ast/genrtlil.cc:1935-1937](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1935-L1937) —— `AST_ADD→$add`、`AST_SUB→$sub`、`AST_MUL→$mul`。

[frontends/ast/genrtlil.cc:1981-2017](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1981-L2017) —— `AST_TERNARY`：条件为常量时直接折掉；否则经 `mux2rtlil` 产出 `$mux`（条件若多位先 `$reduce_bool` 归一）。

#### 4.2.4 代码实践

**实践目标**：验证「AST 运算符 → 内部 `$` 单元」的对应关系。

**操作步骤**：

1. 用 4.1.4 的 `tiny.v`（`assign y = (a & b) | c;`），先看 AST：
   ```bash
   ./build/yosys -p "read_verilog -dump_ast1 tiny.v"
   ```
   应看到 `AST_BIT_OR` 套 `AST_BIT_AND`。
2. 再生成 RTLIL 网表并写出：
   ```bash
   ./build/yosys -p "read_verilog tiny.v; write_rtlil"
   ```
   或保存到文件后查看 cell 类型。

**需要观察的现象**：`write_rtlil` 输出里会出现一个 `$and` 单元（A/B 端口接 `a`、`b`，Y 接中间线）和一个 `$or` 单元（输入为该中间线与 `c`，Y 接 `y`）。

**预期结果**：与 [frontends/ast/genrtlil.cc:1843-1846](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1843-L1846) 的映射一一吻合——AST 的 `AST_BIT_AND`/`AST_BIT_OR` 分别变成 RTLIL 的 `$and`/`$or`。这说明 AST 与 RTLIL 之间是「结构同构」的：一个运算符节点对应一个 RTLIL cell。

> 把 `tiny.v` 改成 `assign y = sel ? (a+b) : (a*b);` 重做，应观察到 `$add`、`$mul`、`$mux` 三种单元，对应 `AST_ADD`、`AST_MUL`、`AST_TERNARY`（见 4.2.3 引用的三段 `genrtlil.cc`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `children` 用 `vector<unique_ptr<AstNode>>` 而不是 `vector<AstNode*>` 或 `vector<AstNode>`？
> 答案：`vector<AstNode>` 不行——`AstNode` 是 incomplete/in-place 的大小问题且需要多态持有子树，值语义无法表达「节点共享与树形归属」；`vector<AstNode*>` 能表达树，但需手动 `delete`，容易泄漏或重复释放。`unique_ptr` 表达「父独占子」，父析构即整棵子树释放，既安全又零运行期开销。

**练习 2**：`id2ast` 为什么是裸指针（`AstNode*`）而不是 `unique_ptr`？
> 答案：`id2ast` 是「引用」而非「拥有」——它指向某个已被 `children` 持有的节点（如 `AST_WIRE`）。若它也用 `unique_ptr`，就会出现两个 `unique_ptr` 指向同一对象，导致双重释放。所以引用关系一律用裸指针，所有权唯一地由 `children`/`attributes` 承担。

**练习 3**：`AST_CONSTANT` 节点同时有 `bits`、`integer`、`str` 三个字段，它们各自什么时候有意义？
> 答案：`bits`（`vector<State>`）是权威表示，任何常数都有；`integer`（`uint32_t`）是低 32 位的缓存，方便快速取整（见 `mkconst_bits` 的填充逻辑）；`str` 仅在 `is_string` 为真时有意义（字符串字面量，由 `mkconst_str` 设置）。这正是「标记联合、按需取用」的体现。

---

### 4.3 AstModule：把 AST 留档，延迟生成 RTLIL

#### 4.3.1 概念说明

Verilog 支持**参数化模块**：`module counter #(parameter W = 8) (...)`。模块里 `W` 到底是几，要等它被例化、参数被覆盖后才知道；而 RTLIL 是「位宽确定」的网表表示，不能含糊。于是 Yosys 面对一个问题：**读 Verilog 时参数还没确定，什么时候才把模块展开成 RTLIL？**

`AstModule` 就是答案。它是 `RTLIL::Module` 的子类，额外持有一份**原始 AST**（`std::unique_ptr<AstNode> ast`）。其策略是「**懒展开**」：

- **读入阶段**：如果一个模块含「无默认值的参数」，Yosys 暂不展开它，只把 AST 存进一个 `AstModule`，并把模块名加前缀 `$abstract`，表示「这是个抽象模板，还没实例化」。
- **派生阶段**：之后 `hierarchy` pass 在处理例化时，会带着具体参数调用 `module->derive(design, params, ...)`。`derive` 会克隆模板 AST、把参数值写进去，再真正运行 `simplify + genRTLIL`，产出一个位宽确定的派生模块（名字形如 `$paramod...`）。

这样，**同一份 AST 模板可以按不同参数被展开成多个不同的 RTLIL 模块**，且每个具体参数组合只展开一次（结果会被缓存）。

#### 4.3.2 核心流程

整个「留档—延迟—派生」流程串起来是：

```
read_verilog
   └─ AST::process()                         # 遍历 AST_DESIGN 的子节点
        └─ 对每个 AST_MODULE：
             · 若含无默认值参数 → defer_local=true
             · 名字前加 "$abstract"
             └─ process_module(defer=true)
                  · new AstModule
                  · module->ast = clone(原AST)   # 只留档
                  · 不跑 simplify / genRTLIL     # 关键：不展开
                  · 记录 avail_parameters
   └─ 其它无参数模块：process_module(defer=false) 立即展开

（之后）hierarchy pass 遇到例化 counter #(.W(16))
   └─ design->module("$abstract\\counter")->derive(design, {W=16})
        └─ derive_common()
             · modname = derived_module_name(...)   # 算出 $paramod... 名
             · 若 design 已有同名 → 命中缓存，直接返回
             · 否则 new_ast = ast->clone()           # 复制模板
             · 把参数值写成 AST_CONSTANT 注入 new_ast
        └─ process_module(new_ast, defer=false)     # 这次真正 simplify+genRTLIL
        └─ 返回新模块名
```

派生模块名的算法很关键：把参数序列化拼接（带类型标记 `t/s/r/u` + 位宽 + 值），若拼接串不超过 60 字符就用可读名 `$paramod\\counter\W=8'001...`；超过则用 sha1 摘要做短名 `$paramod$<sha1>\counter`（见 [frontends/ast/ast.cc:1787-1796](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1787-L1796)）。这样「相同参数 → 相同名 → 命中缓存」自然成立。

派生参数序列化的格式为：先按标志位输出类型字母（`t`=string、`s`=signed、`r`=real、`u`=unsized，否则输出十进制位宽），再 `'` 加位串。形式化地，一个参数值 \(v\) 的序列化串可写作：

\[
\text{tag}(v) \cdot \text{width}(v) \cdot "'" \cdot \text{bits}(v)
\]

整串拼接后若长度 \(>60\)，则模块名取 \(\text{sha1}(\text{para\_info})\)，否则原样保留。这保证了**参数集合到名字的映射是确定且抗碰撞的**。

> 还有一条「兜底」机制：`reprocess_if_necessary()`。某些例化的子模块可能在第一次展开时还没就绪（带 `reprocess_after` 属性），等该子模块后来出现在 design 里时，`AstModule` 会用留存的 AST 重新展开自身（见 [frontends/ast/ast.cc:1574-1590](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1574-L1590)）。这正是「留档 AST」的又一回报——随时能从头再来。

#### 4.3.3 源码精读

`AstModule` 类声明：在 `RTLIL::Module` 基础上增加 `ast` 字段与一组 `derive`/`expand_interfaces`/`reprocess_if_necessary`/`clone` 虚函数：

[frontends/ast/ast.h:396-406](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.h#L396-L406) —— `std::unique_ptr<AstNode> ast` 持有原始 AST；注释点明「参数化模块由 AST 库直接支持，因此需要自己的 `RTLIL::Module` 派生类并重载若干虚函数」。

入口 `AST::process`：遍历 `AST_DESIGN` 的子节点，对含无默认值参数的模块延迟展开并加 `$abstract` 前缀：

[frontends/ast/ast.cc:1441-1453](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1441-L1453) —— 检测到无默认值参数即 `defer_local=true`，随后 `child->str = "$abstract" + child->str`。

`process_module` 的「留档 vs 展开」分叉：

[frontends/ast/ast.cc:1116-1135](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1116-L1135) —— `new AstModule`、设名字、克隆「化简前的 AST」存为 `ast_before_simplify`。

[frontends/ast/ast.cc:1295-1304](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1295-L1304) —— `defer` 为真时的分支：只登记 `avail_parameters`，**不**跑 simplify/genRTLIL（对比 `!defer` 分支 [frontends/ast/ast.cc:1153-1294](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1153-L1294) 会真正展开）。最后统一把 AST 存进 `module->ast`（[frontends/ast/ast.cc:1308](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1308)）。

派生入口 `AstModule::derive`：

[frontends/ast/ast.cc:1752-1768](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1752-L1768) —— 调 `derive_common` 算名字并准备改写后的 AST；若 design 里还没有该名模块，则 `process_module(defer=false)` 真正展开，否则报「命中缓存」。

`derive_common`：克隆模板 AST 并把参数覆盖写进去：

[frontends/ast/ast.cc:1799-1895](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1799-L1895) —— 按 AST 中 `AST_PARAMETER` 的声明顺序匹配参数（支持按名 `W=` 与按序 `$1`），把值用 `mkconst_bits`/`mkconst_str`/`AST_REALVALUE` 注入对应参数节点的 `children[0]`；未命中的参数补成 `AST_DEFPARAM`。

派生名的计算：

[frontends/ast/ast.cc:1787-1796](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1787-L1796) —— `derived_module_name`：参数串 ≤60 用可读名，否则 `"$paramod$" + sha1(para_info)`。

`loadconfig`：派生前把本模块保存的选项写回 `flag_*` 全局量，保证重新展开时用同一套前端开关：

[frontends/ast/ast.cc:1919-1937](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1919-L1937) —— 把 `nolatches/nomem2reg/lib/...` 等成员变量回填到 `AST_INTERNAL::flag_*`。

#### 4.3.4 代码实践

**实践目标**：观察一个参数化模块如何先以 `$abstract` 留档、再被 derive 成具名派生模块。

**操作步骤**：

1. 准备 `param.v`（示例代码，非项目原有）：
   ```verilog
   module regn #(parameter W = 8)(input clk, input [W-1:0] d, output reg [W-1:0] q);
     always @(posedge clk) q <= d;
   endmodule

   module top(input clk, input [15:0] d, output [15:0] q);
     regn #(.W(16)) u (.clk(clk), .d(d), .q(q));
   endmodule
   ```
2. 只读入、不展平层次，看 design 里的模块名：
   ```bash
   ./build/yosys -p "read_verilog param.v; ls"
   ```
3. 再执行 `hierarchy` 触发派生：
   ```bash
   ./build/yosys -p "read_verilog param.v; hierarchy -top top; ls"
   ```

**需要观察的现象**：第 2 步里应出现 `$abstract\regn`（参数未定的模板）和 `top`；第 3 步执行 `hierarchy` 后，会多出一个名字含 `$paramod` 的派生模块（对应 `W=16`），`$abstract\regn` 被实际例化替换。

**预期结果**：派生模块名遵循 [frontends/ast/ast.cc:1787-1796](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1787-L1796) 的规则，因参数较少应是可读形式，如 `$paramod\regn\W=16'00010000`（具体位串形式「待本地验证」）。这印证了「AST 留档 → derive 按参数展开」的整套机制。

> 若想进一步确认「只展开一次」的缓存效果：把第 3 步改成例化两个相同 `W=16` 的 `regn`，`ls` 里派生模块应当仍只有一个。

#### 4.3.5 小练习与答案

**练习 1**：为什么含「无默认值参数」的模块一定要延迟（defer），而 `parameter W = 8`（有默认值）的可以立即展开？
> 答案：有默认值的模块即使不被覆盖，参数也有确定值，能立即算出位宽、生成确定 RTLIL；无默认值的模块在未被覆盖前参数悬空，无法确定位宽（如 `[W-1:0]`），强行展开会出错。所以 `process()` 用 `param_has_no_default` 检测，命中才 `defer`（见 [frontends/ast/ast.cc:1442-1449](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1442-L1449)）。注意：即使有默认值，只要它被例化时覆盖了参数，仍会走 `derive` 生成新的派生模块。

**练习 2**：`derive_common` 里同时支持「按名」和「按序」两种参数匹配，它们分别对应 Verilog 的哪种写法？
> 答案：按名匹配（`parameters.find(child->str)`）对应 `regn #(.W(16))`；按序匹配（`parameters.find("$"+序号)`）对应 `regn #(.W, .D)` 这类位置式参数传递或 defparam。`derive_common` 先查名字，找不到再查 `$序号`（见 [frontends/ast/ast.cc:1848-1865](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1848-L1865)）。

**练习 3**：`reprocess_if_necessary` 解决的是什么问题？为什么普通（非 `AstModule`）模块不需要它？
> 答案：它解决「模块 A 例化了当时还不存在的模块 B」的问题——B 后来才进入 design，A 需要「重做」。因为 `AstModule` 保留了原始 AST，可以重新展开；而普通 `RTLIL::Module`（如从 liberty/blackbox 来的）没有可重展开的 AST，本身也不依赖这种延迟解析，故不需要该机制。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「**从源码到 AST 再到 RTLIL 的全程追踪**」：

1. **写设计**（示例代码）：一个含算术、三目、参数的小 ALU：
   ```verilog
   module alu #(parameter W = 4)(input [W-1:0] a, b, input op, output [W-1:0] y);
     assign y = op ? (a + b) : (a & b);
   endmodule
   ```
2. **看 AST（化简前后）**：
   ```bash
   ./build/yosys -p "read_verilog -debug alu.v"
   ```
   `-debug` 等价于同时打开 `-dump_ast1 -dump_ast2 -dump_vlog1 -dump_vlog2`（见 [frontends/verilog/verilog_frontend.cc:113-114](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L113-L114)）。对照 4.1 的枚举，在转储里找到 `AST_TERNARY`、`AST_ADD`、`AST_BIT_AND`、`AST_PARAMETER`。
3. **看 RTLIL**：执行 `write_rtlil`，确认 `AST_TERNARY→$mux`、`AST_ADD→$add`、`AST_BIT_AND→$and`（对照 4.2.3 引用的 `genrtlil.cc`）。
4. **看派生**：再写一个 `top` 例化 `alu #(.W(8))`，跑 `hierarchy -top top`，用 `ls` 找到 `$paramod$...\alu`（或可读名），验证 4.3 的「留档→derive」流程。
5. **形成一张对照表**：左列写 Verilog 源码片段，中列写对应的 `AstNodeType`，右列写最终生成的 `$` 单元。这张表就是你把本讲三条主线（枚举词汇表 / 万能节点结构 / 延迟派生）融会贯通的成果。

> 反思点：为什么 Yosys 不直接「Verilog → RTLIL」，而要中间插一棵 AST？因为 AST 保留了源码的**结构**与**参数符号**，既能做化简/类型推导等高层变换，又能让参数化模块「等到用的时候再展开」。这就是 `AstModule` 设计的根本动机。

## 6. 本讲小结

- `AstNodeType` 枚举是 AST 的「词汇表」，把 Verilog 的每一种语法成分（声明、语句、运算符、常量、generate…）映射成一种节点类型；`type2str` 用 X-macro 保证枚举与字符串同源。
- `AstNode` 是单一的「万能积木」结构体，用「`type` 标签 + `children`/`attributes` 树结构 + 一大包按需取用的内容字段」表达所有节点；子节点用 `unique_ptr` 实现安全的独占所有权。
- `id2ast` 是非拥有的引用指针，由 `simplify` 设置、供 `genRTLIL` 快速解析标识符；`mkconst_*` 是构造常量节点的标准工厂。
- AST 运算符与 RTLIL `$` 单元结构同构：`AST_BIT_AND→$and`、`AST_ADD→$add`、`AST_MUL→$mul`、`AST_TERNARY→$mux` 等，映射集中在 `genrtlil.cc`。
- `AstModule` 继承 `RTLIL::Module` 并额外持有原始 AST，使参数化模块可以「留档 + 延迟展开」：含无默认值参数的模块先存为 `$abstract` 模板，待 `hierarchy` 调 `derive` 时按具体参数克隆改写、再生成确定 RTLIL。
- 派生模块名由参数序列化（带类型标记）拼接而成，过长则用 sha1 摘要，保证「相同参数→相同名→缓存命中」。

## 7. 下一步学习建议

- **下一讲 u5-l4（AST 简化与 genrtlil）** 将深入 `simplify.cc` 与 `genrtlil.cc`，讲解本讲多次提到的 `simplify()`（常量折叠、宽度推导、generate 展开）和 `genRTLIL()`（表达式→`$` 单元、always→process）的内部实现，是本讲的自然延续。
- 若想看清「运算符→单元」的全貌，可通读 [frontends/ast/genrtlil.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc) 中 `genRTLIL()` 的 `switch(type)`，特别是 `binop2rtlil`/`uniop2rtlil`/`mux2rtlil` 三个 helper。
- 想理解「派生」的调用方，可回到 `passes/hierarchy/hierarchy.cc`，看它如何扫描例化、收集参数并调用 `module->derive(...)`（这将与 u6-l1 层次管理讲义衔接）。
- 配合官方文档 `docs/source/yosys_internals/flow/verilog_frontend.rst` 阅读前端整体流程图。
