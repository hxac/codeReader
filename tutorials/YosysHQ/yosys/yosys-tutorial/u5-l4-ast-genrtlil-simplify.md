# AST 简化与 genrtlil：AST 到 RTLIL 的生成

## 1. 本讲目标

在 u5-l1～u5-l3 里，我们已经看清了 Verilog 文本是怎么一步步变成一棵 **AST（抽象语法树）** 的：预处理器改写文本、flex 切词、bison 归约出 `AstNode` 节点。但这棵树还不能直接交给综合 pass 使用——它还停留在「源码的字面结构」上，而 RTLIL 需要的是「确定的网表」：每根线的位宽已经算清楚、每个 `generate` 循环都已经展开、每个运算符都对应一个 `$` 单元。

本讲就把这「最后一段路」走完：**AST 是怎么变成 RTLIL 的？** 围绕这条主线，我们要回答三个问题：

1. **谁来驱动这段转换？** —— `AST::process()` 是前端的最终入口，它编排「简化 → 生成」两步。
2. **`simplify()` 在做什么？** —— 它不生成 RTLIL，而是把 AST **改写**成「语义已确定、可直接生成」的形态：解析名字、推导位宽、折叠常量、展开 generate。
3. **`genRTLIL()` 如何翻译一个节点？** —— 它是一个按 `type` 分发的大函数，把运算符变成 `$` 单元、把声明变成 Wire/Cell。

学完后你应当能够：读懂 `assign y = a & b | c;` 这行代码从 AST（`AST_BIT_OR(AST_BIT_AND(a,b), c)`）到 RTLIL（一个 `$and` 单元 + 一个 `$or` 单元）的完整对应；会用 `-dump_ast1`/`-dump_ast2`/`write_rtlil` 三件套亲手验证这种对应；并理解「两遍扫描 + 工厂函数」这套代码生成模式。

## 2. 前置知识

- **AST 节点模型（u5-l3）**：一个 `AstNode` 由 `type` 标签 + `children` 子节点列表 + 若干内容字段构成。运算符节点（如 `AST_BIT_AND`）把操作数挂在 `children[0]`/`children[1]` 上；`id2ast` 字段记录标识符「指向哪棵声明子树」。本讲全程用到这套模型。
- **RTLIL 的 Wire / Cell / SigSpec（u2-l3）与构造接口（u3-l1）**：`module->addWire(name, width)` 建线，`module->addCell(name, type)` 建单元，`cell->setPort(port, sig)` 接管脚，`module->connect(lhs, rhs)` 写一条 `assign`。`genRTLIL()` 的产物就是这些调用。
- **内部 `$` 单元库（u3-l4）**：`$and`/`$or`/`$add`/`$mux` 等是 Yosys 内部单元；二元门端口为 `A/B→Y`，多路器为 `A/B/S→Y`，参数 `A_WIDTH/B_WIDTH/Y_WIDTH` 携带位宽。本讲会反复看到 AST 运算符如何落到这些单元上。
- **不动点（fixpoint）思想**：一段「可能改写 AST」的函数返回 `bool`（是否改过），调用方用 `while (simplify(...)) {}` 反复跑，直到返回 `false`（再也改不动）即「到达不动点」。这是 simplify 的核心控制结构。
- **工厂函数（factory）模式**：把「创建一个单元 + 建一根输出线 + 接好端口 + 设好参数」这套重复流程，封装成 `binop2rtlil`/`uniop2rtlil` 之类的助手函数。genRTLIL 里大量复用它们。

> 承接关系：本讲承接 u5-l3（AST 节点）与 u2-l3/u3-l1（RTLIL 构造接口），是整个 Verilog 前端的「收口」。学完本讲，`read_verilog` 这条前端流水线就完整了。

## 3. 本讲源码地图

| 文件 | 体量 | 作用 |
|------|------|------|
| `frontends/ast/ast.h` | 17KB | 声明 `simplify()`、`genRTLIL()`、`detectSignWidth()`、`process()`、`AstModule`。是三个最小模块的「接口索引」。 |
| `frontends/ast/ast.cc` | 58KB | 实现 `process()` 与 `process_module()`——前端的最终入口，编排 simplify 与 genRTLIL 的先后。 |
| `frontends/ast/simplify.cc` | 230KB | 实现 `AstNode::simplify()`：AST 的「改写器」，做名字解析、宽度推导、常量折叠、generate 展开、mem2reg。本讲最大的文件。 |
| `frontends/ast/genrtlil.cc` | 86KB | 实现 `AstNode::genRTLIL()` 与 `genWidthRTLIL()`：把 AST 节点翻译成 RTLIL 的 Wire/Cell/Process。 |
| `frontends/verilog/verilog_frontend.cc` | — | `read_verilog` 命令在此注册，并在解析完成后调用 `AST::process()`；`-dump_ast1`/`-dump_ast2` 选项也定义于此。 |

> 提示：`frontends/ast/` 不是独立前端，而是被 `frontends/verilog/` 复用的「AST 库」（见 [frontends/ast/ast.h:20-23](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.h#L20-L23)）。它的两个核心函数 `simplify` 与 `genRTLIL` 在 `ast.h` 里紧挨着声明，注释点明了分工（见 [frontends/ast/ast.h:261-263](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.h#L261-L263) 与 [frontends/ast/ast.h:308-311](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.h#L308-L311)）。

---

## 4. 核心概念与源码讲解

### 4.1 process()：前端的最终入口

#### 4.1.1 概念说明

当 bison 把整个文件解析完，会得到一棵根为 `AST_DESIGN` 的 AST。但这棵树对 RTLIL 来说还「太原始」——里面有 `generate` 循环没展开、有参数没代值、有运算符没拆成单元。`AST::process()` 就是把 `AST_DESIGN` 翻译成一堆 `RTLIL::Module`（塞进 `design`）的那个总入口。

可以把它的职责想象成一条流水线：

```
AST_DESIGN (整棵树)
   │
   │  process() 遍历每个 AST_MODULE
   ▼
process_module(module_ast)
   │
   ├─ ① dump_ast1   （可选：打印「简化前」的 AST）
   ├─ ② simplify()  （改写 AST：解析名字 / 推导位宽 / 折叠常量 / 展开 generate）  ← 反复跑到不动点
   ├─ ③ dump_ast2   （可选：打印「简化后」的 AST）
   ├─ ④ 处理 blackbox / whitebox 属性
   └─ ⑤ genRTLIL()  （两趟：先 WIRE/MEMORY，再其余节点，最后 INITIAL）
```

关键点有两个：

- **simplify 在前，genRTLIL 在后**。simplify 负责「把 AST 整理干净」，genRTLIL 才能「机械地」翻译。两者职责严格分离：simplify 只改写 AST、绝不碰 RTLIL；genRTLIL 只读 AST（基本不再改它）、只产出 RTLIL。
- **genRTLIL 分两趟跑**。先为所有 `AST_WIRE`/`AST_MEMORY` 生成 RTLIL 的 Wire/Memory，再为其余节点（`AST_ASSIGN`/`AST_CELL`/`AST_ALWAYS` 等）生成 Cell/Process。这样当某个 Cell 引用一根线时，那根线一定已经存在了。

#### 4.1.2 核心流程

`process()` 的总控逻辑（伪代码）：

```
process(design, ast /* = AST_DESIGN */, ...一堆 flag...):
    current_ast = ast
    设好所有 flag_*（dump_ast1/nolatches/mem2reg/...）
    ast->fixup_hierarchy_flags(true)        # 全树标注 in_lvalue/in_param
    for child in ast.children:              # 遍历顶层每个 module/package
        if child 是 AST_MODULE/AST_INTERFACE:
            注入 verilog_globals / verilog_packages 的克隆
            判断是否需要 defer（含无默认值参数 → 存为 $abstract 模板，留给 hierarchy 展开）
            process_module(design, child, defer?)
        elif child 是 AST_PACKAGE:
            child->simplify(...)            # 处理 package 里的 enum 等
            存进 design->verilog_packages
```

`process_module()` 里 simplify 与 genRTLIL 的衔接（伪代码）：

```
process_module(design, ast):
    module = new AstModule;  current_module = module   # 当前正往哪个 RTLIL 模块里写
    if flag_dump_ast1:  ast->dumpAst(...)              # ① 简化前转储
    if not defer:
        while ast->simplify(!flag_noopt, 0, -1, false): {}   # ② 反复简化到不动点（stage 0）
    if flag_dump_ast2:  ast->dumpAst(...)              # ③ 简化后转储
    处理 blackbox/whitebox 属性                          # ④
    for node in ast.children: if node 是 WIRE/MEMORY: node->genRTLIL()   # ⑤ 第一趟
    for node in ast.children: if node 不是 WIRE/MEMORY/INITIAL: node->genRTLIL()  # 第二趟
    for node in ast.children: if node 是 INITIAL: node->genRTLIL()       # 第三趟
```

> 名字小知识：`flag_dump_ast1` 是「简化前」（before simplification），`flag_dump_ast2` 是「简化后」（after simplification）。二者都只是「打印到日志」，不影响转换结果。本讲 4.1.4 实践里会同时用上它们。

#### 4.1.3 源码精读

`process()` 的入口与 flag 装载：

[frontends/ast/ast.cc:1387-1413](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1387-L1413) —— `AST::process` 把命令行 flag 一一拷进 `AST_INTERNAL::flag_*` 全局变量（这就是 simplify/genRTLIL 读取选项的通道），随后 `fixup_hierarchy_flags(true)` 全树标注 `in_lvalue`/`in_param`，再断言根是 `AST_DESIGN`。

顶层遍历每个 module 并决定是否延迟：

[frontends/ast/ast.cc:1441-1471](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1441-L1471) —— 若模块含「无默认值的参数」，则 `defer_local = true` 并把名字改成 `$abstract\模块名`（参数化模板，留给 `hierarchy` pass 的 `derive` 按参数展开，详见 u5-l3 的 AstModule）；否则直接调 `process_module`。

`process_module()` 里 dump_ast1 / simplify / dump_ast2 的三段式（本讲最核心的编排）：

[frontends/ast/ast.cc:1142-1151](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1142-L1151) —— `-dump_ast1`：在 simplify **之前**调 `dumpAst` 打印 AST。

[frontends/ast/ast.cc:1176-1186](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1176-L1186) —— 设简化上下文后 `while (ast->simplify(!flag_noopt, 0, -1, false)) {}`（注意 stage 参数是 `0`，见 4.2），随后 `-dump_ast2` 打印「简化后」的 AST。`flag_noopt`（对应 `read_verilog -noopt`）反相后作为 `const_fold` 传入——默认会做常量折叠。

`genRTLIL` 的两趟扫描：

[frontends/ast/ast.cc:1273-1289](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1273-L1289) —— 先遍历生成所有 `AST_WIRE`/`AST_MEMORY`（建线/建存储器），再遍历生成其余节点（建 Cell/Process/assign）。分两趟是为了保证「引用方出现时，被引用的线一定已存在」；`AST_INITIAL` 还要再排到最后，因为它依赖前面的寄存器。

`read_verilog` 在哪里调用 `process()`：

[frontends/verilog/verilog_frontend.cc:560](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L560) —— 解析得到的 `parse_state.current_ast`（`AST_DESIGN` 根）连同各 `flag_*` 一起传入 `AST::process()`，正式进入本讲的流水线。该文件顶部注释一语道破分工：*"use the Verilog bison/flex parser to generate an AST and use `AST::process()` to convert it to RTLIL"*（见 [frontends/verilog/verilog_frontend.cc:44](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/verilog/verilog_frontend.cc#L44)）。

#### 4.1.4 代码实践

**实践目标**：亲手观察 `-dump_ast1` 与 `-dump_ast2` 的差异，直观感受 simplify 改写了什么。

**操作步骤**：

1. 准备 `demo.v`（示例代码，非项目原有）：
   ```verilog
   module demo(input [3:0] a, b, output [3:0] y);
     parameter P = 2;
     assign y = (a & b) | P'b01;
   endmodule
   ```
2. 打印「简化前」AST：
   ```bash
   ./build/yosys -p "read_verilog -dump_ast1 demo.v"
   ```
3. 打印「简化后」AST：
   ```bash
   ./build/yosys -p "read_verilog -dump_ast2 demo.v"
   ```

**需要观察的现象**：在 `-dump_ast1` 里，`P'b01` 仍是一个引用参数 `P` 的表达式节点；`a & b` 是 `AST_BIT_AND` 套两个 `AST_IDENTIFIER`。在 `-dump_ast2` 里，`P` 应已被它的默认值 `2` 代入，`2'b01` 被折叠成一个确定的 `AST_CONSTANT`，但 `a & b` 因含变量而保留为 `AST_BIT_AND`（常量折叠只对「两边都是常量」的运算生效，见 4.2）。

**预期结果**：`-dump_ast2` 输出里几乎看不到 `AST_PARAMETER` 引用与可算的常数表达式，它们都变成了 `AST_CONSTANT`；含变量的运算符节点则原样保留，留给 genRTLIL 去生成 `$` 单元。

> 若手头没有可执行的 yosys，标注「待本地验证」，转而对照 [frontends/ast/ast.cc:1142-1186](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1142-L1186) 的三段式代码即可理解两次转储的时序。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `genRTLIL` 要分「先 WIRE/MEMORY、再其余节点」两趟，而不能一趟顺序扫描？
> 答案：因为后面的节点（如 `AST_CELL` 实例化、`AST_ASSIGN` 赋值）会通过 `SigSpec` 引用线。若被引用的 `AST_WIRE` 还没生成对应的 RTLIL Wire，genRTLIL 在 `AST_IDENTIFIER` 分支里就找不到它（`current_module->wire(str)` 为空）。先把所有声明建好，再处理引用方，就能保证引用总命中。见 [frontends/ast/ast.cc:1273-1282](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1273-L1282)。

**练习 2**：含「无默认值参数」的模块，`process()` 会怎样处理它？为什么不直接生成 RTLIL？
> 答案：会被 `defer_local = true` 标记、名字改成 `$abstract\模块名`，只存 AST、不跑 genRTLIL（见 [frontends/ast/ast.cc:1442-1453](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1442-L1453)）。因为位宽、内部结构都依赖参数取值，参数没定就没法生成确定的网表。它要等到 `hierarchy` pass 发现实例化点、按实参 `derive` 出一个 `$paramod` 派生模块时，才重新走一遍 simplify + genRTLIL（详见 u5-l3 的 AstModule）。

---

### 4.2 simplify()：把 AST「整理」成可生成的形式

#### 4.2.1 概念说明

`simplify()` 是 AST 库里最重的函数（`simplify.cc` 有 230KB）。它的职责不是生成 RTLIL，而是 **改写 AST 本身**，让这棵树变成「语义已确定、可被 genRTLIL 机械翻译」的形态。可以这样理解它做的四类工作：

1. **名字解析**：Verilog 允许 `output foo; reg foo;` 这样对同一根线多次声明。simplify 把它们合并、建立 `current_scope`（名字→声明节点的映射），并给每个 `AST_IDENTIFIER` 设上 `id2ast` 指针，指向它引用的那棵声明子树。这样 genRTLIL 遇到标识符时，能 O(1) 知道「这是根什么线、多宽」。
2. **宽度推导**：很多运算的位宽要靠上下文推断（自底向上的 `detectSignWidth`）。simplify 负责把推断结果落到节点字段上（如 `range_left/range_right`、`is_signed`）。
3. **常量折叠（const fold）**：当 `const_fold` 开启且一个运算的两侧都已是 `AST_CONSTANT` 时，直接在 AST 层面把它算成一个 `AST_CONSTANT`。例如 `AST_ADD(1, 2)` → `AST_CONSTANT(3)`。这避免生成无谓的 `$add` 单元。
4. **展开 generate**：`AST_GENFOR`（`generate for`）、`AST_GENIF`（`generate if`）、`AST_GENCASE` 在综合期必须展开成若干具体实例——循环要 unroll、条件要二选一。这些工作也由 simplify 完成。

`simplify()` 返回 `bool`：**是否改动过 AST**。调用方据此反复调用直到不动点。这是整个函数的核心控制约定（见 [frontends/ast/ast.h:261-263](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.h#L261-L263) 的注释：*"simplify() creates a simpler AST by unrolling for-loops, expanding generate blocks, etc."*）。

#### 4.2.2 核心流程

`simplify()` 用一个 `stage` 参数把工作分层（关键设计）：

```
simplify(const_fold, stage=0, ...)      # stage 0 = 模块级总入口
    断言本节点是 AST_MODULE/AST_INTERFACE
    while simplify(const_fold, stage=1, ...): {}      # ① 反复跑 stage 1 到不动点
    if 允许 mem2reg:
        mem2reg_as_needed_pass1(...)                  # ② 第一遍：标记哪些 memory 该转寄存器
        for 每个被选中的 memory: 新建一排 AST_WIRE 子节点
        while mem2reg_as_needed_pass2(...): {}        #    第二遍：改写读写处
        mem2reg_remove(...)                            #    删掉原 memory
    while simplify(const_fold, stage=2, ...): {}      # ③ 反复跑 stage 2 到不动点
    return false   # stage 0 自身只做编排，不报告「改过」
```

也就是说，stage 0 是「外层调度」，它内部交替地反复调用 stage 1（常规简化）和 stage 2（处理 mem2reg 之后的收尾），各自到不动点。正因如此，`process_module` 只需调 `simplify(..., 0, ...)` 一次并 `while` 包住即可（[frontends/ast/ast.cc:1179](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1179)）。

进入 stage 1/2 后，函数主体是一个庞大的分发：

```
simplify(const_fold, stage∈{1,2}, ...):
    若是 AST_FUNCTION/AST_TASK: 直接返回（函数体到被调用时才处理）
    若是必须静态求值的节点（WIRE/PARAMETER/RANGE/...）: const_fold = true   # ★ 关键
    若是 AST_MODULE: 清空 current_scope，合并同名 wire，标注 genblk
    先 simplify 所有 children（但对 GENFOR/GENIF/GENBLOCK 跳过其特殊子节点）
    按 type 做改写：
        AST_RANGE:         把 range_left/range_right 算成确定整数
        AST_GENFOR:        展开循环
        AST_GENIF/GENCASE: 按 cond 常量二选一
        ... ...
    若 const_fold 且本节点是可折叠运算且两侧皆常量: newNode = mkconst_*(结果)
    apply_newNode: 用 newNode 覆盖本节点
    return did_something
```

**常量折叠的触发**很关键：只有「必须静态求值」的上下文（线宽、参数、属性、范围等）才强制 `const_fold = true`（见 [frontends/ast/simplify.cc:1219-1223](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/simplify.cc#L1219-L1223)）。普通 `assign` 右侧的表达式默认不折叠（除非 `read_verilog` 不带 `-noopt`，此时 `process` 传入 `const_fold=!flag_noopt=true`）。

**节点替换**用统一的 `apply_newNode` 收口：任何分支只要把改写结果放进 `newNode` 再 `goto apply_newNode`，框架就会用 `newNode->cloneInto(*this)` 把本节点原地替换掉，并记 `did_something = true`（见 [frontends/ast/simplify.cc:4794-4803](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/simplify.cc#L4794-L4803)）。这让「几十种改写」共用一套替换机制。

#### 4.2.3 源码精读

`simplify` 的签名与不动点约定：

[frontends/ast/ast.h:261-263](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.h#L261-L263) —— 注释说明 simplify 通过展开循环、展开 generate 块、设置 `id2ast` 来制造「更简单的 AST」。

stage 0 的三段式调度（①stage1 → ②mem2reg → ③stage2）：

[frontends/ast/simplify.cc:1066-1167](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/simplify.cc#L1066-L1167) —— `stage == 0` 分支：先 `while (simplify(const_fold, 1, ...))` 跑 stage 1（[simplify.cc:1071](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/simplify.cc#L1071)），再做 mem2reg（[simplify.cc:1073-1163](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/simplify.cc#L1073-L1163)），最后 `while (simplify(const_fold, 2, ...))` 跑 stage 2（[simplify.cc:1165](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/simplify.cc#L1165)）。

`const_fold` 的强制开启（静态上下文）：

[frontends/ast/simplify.cc:1219-1223](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/simplify.cc#L1219-L1223) —— 凡是 `AST_WIRE`/`AST_PARAMETER`/`AST_RANGE`/`AST_PREFIX`/`AST_TYPEDEF` 等「必须静态求值」的节点，强制 `const_fold = true`；引用参数/枚举项的 `AST_IDENTIFIER` 也强制开启。

先递归简化所有子节点（但跳过 generate 的「待展开体」）：

[frontends/ast/simplify.cc:1931-1942](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/simplify.cc#L1931-L1942) —— 遍历 children 做 simplify，但对 `AST_GENFOR`/`AST_FOR` 跳过下标 ≥3 的子节点（循环体），对 `AST_GENIF`/`AST_GENCASE` 跳过下标 ≥1（分支体）——因为这些要由后面的展开逻辑整体处理，不能预先简化。

常量折叠（以位运算为例）：

[frontends/ast/simplify.cc:4527-4592](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/simplify.cc#L4527-L4592) —— `if (const_fold)` 块：当两侧都是 `AST_CONSTANT` 时，调用 `RTLIL::const_and`/`const_or`/... 直接算出结果，包成 `mkconst_bits` 赋给 `newNode`。注意这里也用了 Clifford's Device（`if(0){case AST_BIT_AND: const_func=...;}`）把多个 case 聚到同一段代码。

generate-for 的展开（最典型的「改写 AST」）：

[frontends/ast/simplify.cc:2576-2619](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/simplify.cc#L2576-L2619) —— 取出 init/while/next/body 四段，把循环变量当 `AST_LOCALPARAM` 求值为常量，随后进入 `while(1)` 循环：每轮克隆一份 body、`expand_genblock(prefix)` 给内部名字加前缀、按当前循环变量值简化后插入父节点——直到循环条件不满足。这正是「编译期 unroll」。

`apply_newNode` 统一替换收口：

[frontends/ast/simplify.cc:4794-4807](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/simplify.cc#L4794-L4807) —— 所有改写分支 `goto` 到这里，用 `newNode->cloneInto(*this)` 原地替换、设 `did_something=true`；若整轮什么都没改，则置 `basic_prep = true`（标记「基础分析已完成」）。

#### 4.2.4 代码实践

**实践目标**：用一段含 generate-for 的代码，验证 simplify 的「展开」与「折叠」效果。

**操作步骤**：

1. 准备 `gen.v`（示例代码，非项目原有）：
   ```verilog
   module gen(input clk, output reg [3:0] q);
     genvar i;
     generate
       for (i = 0; i < 4; i = i + 1) begin: stage
         // 综合期常量: i+1 会被折叠
         wire dummy = (i + 1) > 0;
       end
     endgenerate
   endmodule
   ```
2. 看「简化前」：
   ```bash
   ./build/yosys -p "read_verilog -dump_ast1 gen.v"
   ```
3. 看「简化后」：
   ```bash
   ./build/yosys -p "read_verilog -dump_ast2 gen.v"
   ```

**需要观察的现象**：`-dump_ast1` 里能看到一个 `AST_GENFOR` 节点，它的 body 是 `AST_GENBLOCK`（名为 `stage`），里面引用 `genvar i`，并含未折叠的 `AST_GT(AST_ADD(i,1), 0)`。`-dump_ast2` 里 `AST_GENFOR` 应已消失，取而代之的是展开后的 `stage[0]`、`stage[1]`、`stage[2]`、`stage[3]` 四个块，每个块里的 `i+1>0` 都被折叠成一个确定的 `AST_CONSTANT`（因为 `i` 在每轮被当成具体常量代入了）。

**预期结果**：`-dump_ast2` 不再出现 `AST_GENFOR`/`AST_GENVAR`，而是 4 份展开后的具体声明；其中可求值的常量表达式（`i+1`、`>0`）都被折叠成 `AST_CONSTANT`。

> 若无法运行，标注「待本地验证」，并对照 [frontends/ast/simplify.cc:2576-2619](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/simplify.cc#L2576-L2619) 理解 unroll 逻辑。

#### 4.2.5 小练习与答案

**练习 1**：`simplify` 为什么返回 `bool` 而不是 `void`？调用方怎么用它？
> 答案：因为很多改写会「暴露出新的可改写机会」（例如展开 generate 后，新出现的常量表达式又能折叠）。返回 `bool`（是否改过）让调用方用 `while (simplify(...)) {}` 反复跑到不动点，保证最终 AST 稳定。典型调用见 [frontends/ast/ast.cc:1179](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1179) 与 [frontends/ast/simplify.cc:1071](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/simplify.cc#L1071)。

**练习 2**：`assign y = a & b;`（a、b 是输入端口）里的 `a & b` 会被常量折叠掉吗？为什么？
> 答案：不会。常量折叠要求运算两侧都是 `AST_CONSTANT`（见 [frontends/ast/simplify.cc:4587](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/simplify.cc#L4587) 的 `children[0]->type == AST_CONSTANT && children[1]->type == AST_CONSTANT`）。`a`、`b` 是变量（`AST_IDENTIFIER` 指向 `AST_WIRE`），不是常量，所以 `AST_BIT_AND` 原样保留，留给 genRTLIL 生成 `$and` 单元。

**练习 3**：`stage 0` 为什么不直接做全部工作，而要分成「stage 1 → mem2reg → stage 2」？
> 答案：因为 mem2reg（把存储器转成一排寄存器）会**结构性**地改写 AST（新增 `AST_WIRE`、改写读写处），改完之后需要再跑一轮常规简化来清理新暴露出的可简化点（例如新生成的寄存器连线）。所以顺序是「先常规简化到不动点 → 做 mem2reg → 再常规简化到不动点」。见 [frontends/ast/simplify.cc:1071-1165](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/simplify.cc#L1071-L1165)。

---

### 4.3 genRTLIL()：把每个 AST 节点翻译成 RTLIL

#### 4.3.1 概念说明

AST 被 simplify 整理干净后，`genRTLIL()` 登场，它把每个 `AstNode` **机械地**翻译成 RTLIL 对象（Wire / Cell / Process / Memory / connect）。它的整体形态是一个按 `type` 分发的巨大 `switch`（见 [frontends/ast/genrtlil.cc:1421](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1421)），每个 `case` 处理一类节点。

可以把节点分成三大类来理解：

| 类别 | 代表节点 | genRTLIL 的产物 |
|------|----------|----------------|
| **声明类** | `AST_WIRE` / `AST_MEMORY` / `AST_PARAMETER` | 调 `addWire` / 新建 `RTLIL::Memory` / 记参数 |
| **赋值/实例类** | `AST_ASSIGN` / `AST_CELL` | `module->connect(...)` / `addCell` + 接端口 |
| **表达式类** | `AST_BIT_AND` / `AST_ADD` / `AST_TERNARY` / `AST_CONSTANT` / `AST_IDENTIFIER` | 返回一个 `RTLIL::SigSpec`；若需运算则建一个 `$` 单元 + 一根输出线 |

表达式类是最有意思的：`genRTLIL()` 对表达式节点 **返回一个 `RTLIL::SigSpec`**（代表「这段信号的值」）。对于 `AST_CONSTANT`，直接返回常量 SigSpec；对于 `AST_IDENTIFIER`，返回它指向的那根 Wire；对于运算符（如 `AST_BIT_AND`），先递归 `genRTLIL()` 得到左右操作数的 SigSpec，再 **建一个 `$and` 单元**、**新建一根输出线**、把单元的 `Y` 接到这根线，最后 **返回这根线**。

这里有一套「工厂函数」把「建单元 + 建输出线 + 接端口 + 设参数」封装起来：

- `uniop2rtlil`：一元运算（`$not`/`$neg`/`$reduce_*`）—— A→Y。
- `binop2rtlil`：二元运算（`$and`/`$or`/`$add`/...）—— A/B→Y。
- `mux2rtlil`：三目运算符 → `$mux` —— A/B/S→Y。
- `widthExtend`：位宽扩展 → `$pos`。

这些工厂函数做的是同一件事：`current_module->addCell(name, type)` 建单元，`current_module->addWire(name+"_Y", width)` 建输出线，`setPort` 接端口，`parameters[...] = Const(...)` 设位宽参数。这正对应 u3-l1 讲过的 RTLIL 构造接口。

> 名字生成：每个生成的单元都有一个形如 `$and$文件名:行号$序号` 的名字（见 [frontends/ast/genrtlil.cc:48](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L48)）。`$` 前缀 + 末尾的 `autoidx++` 保证了全局唯一（`autoidx` 是个递增计数器）。这就是你在 `write_rtlil` 输出里看到大量 `$xxx$文件:行$N` 名字的来源。

#### 4.3.2 核心流程

表达式 `assign y = a & b | c;` 的生成过程（核心示例）：

```
genRTLIL(AST_ASSIGN):
    left  = children[0]->genRTLIL()          # AST_IDENTIFIER(y) → 返回 wire \y
    right = children[1]->genWidthRTLIL(...)  # 求 AST_BIT_OR(...) 的值

  # 求 AST_BIT_OR 的值时（递归 genRTLIL）：
  genRTLIL(AST_BIT_OR):
      # 走 Clifford's Device：type_name = ID($or)
      left  = children[0]->genRTLIL()        # 求 AST_BIT_AND(a,b)
          genRTLIL(AST_BIT_AND):
              type_name = ID($and)
              a = children[0]->genRTLIL()    # AST_IDENTIFIER(a) → wire \a
              b = children[1]->genRTLIL()    # AST_IDENTIFIER(b) → wire \b
              return binop2rtlil($and, ... A=a, B=b ...)   # 建 $and 单元 + \.._Y 线，返回该线
      right = children[1]->genRTLIL()        # AST_IDENTIFIER(c) → wire \c
      return binop2rtlil($or, A=left, B=right)            # 建 $or 单元 + 输出线，返回该线

  current_module->connect(\y, 那根 $or 的输出线)   # 一条 assign
```

最终 RTLIL 里会多出：一根 `$and$..:行$N_Y` 线 + 一个 `$and` 单元、一根 `$or$..:行$M_Y` 线 + 一个 `$or` 单元、一条 `\y = ...` 的 connect。这就把「`a & b | c`」完全展开成了门级。

**Clifford's Device（克利福德装置）**是 genRTLIL 里反复出现的编码技巧：把「多个 case 映射到同一段代码」写成

```cpp
if (0) { case AST_BIT_AND:  type_name = ID($and); }
if (0) { case AST_BIT_OR:   type_name = ID($or); }
if (0) { case AST_BIT_XOR:  type_name = ID($xor); }
if (0) { case AST_BIT_XNOR: type_name = ID($xnor); }
    {
        // 公共代码：用 type_name 建单元
        ...
        return binop2rtlil(this, type_name, width, left, right);
    }
```

每个 `if(0){case ...:}` 都是一个「永远不执行的 if」，但其 `case` 标签是真的——`switch` 跳进某个 case 后，`if(0)` 条件不成立故不执行赋值，于是 **fall-through** 一直滑到公共代码段。这样四个运算符共用一套「求左右操作数 + 建 binop 单元」的逻辑，只差 `type_name` 不同。注释明确把它称作 *Clifford's Device*（见 [frontends/ast/genrtlil.cc:1423-1426](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1423-L1426)）。

**宽度推导**是表达式生成的另一根支柱：在 `genRTLIL` 调用 `binop2rtlil` 之前，需要知道结果多宽、操作数要不要符号扩展。这由 `detectSignWidth` / `detectSignWidthWorker` 自底向上推断（如二元位运算的结果宽度 = max(左,右)），并把 `width_hint/sign_hint` 作为参数下传给子表达式的 `genRTLIL`，使整棵表达式树的位宽一致。

**always 块** 走另一条路：`AST_ALWAYS`/`AST_INITIAL` 不是表达式，genRTLIL 会把它们交给 `ProcessGenerator`（见 [frontends/ast/genrtlil.cc:311-361](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L311-L361)），生成一个 `RTLIL::Process`（行为级进程），再由后续 `proc` pass 翻译成 `$mux`/`$dff`（见 u6-l2）。本讲聚焦表达式与声明，进程生成点到为止。

#### 4.3.3 源码精读

`genRTLIL` 的声明与产物约定：

[frontends/ast/ast.h:308-312](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.h#L308-L312) —— 注释说明：对表达式节点，`genRTLIL` 返回结果信号的 `SigSpec`；生成的 Cell 等都写到 `AST_INTERNAL::current_module`（即 `process_module` 里设的那个 AstModule）。

二元运算工厂 `binop2rtlil`（建 $and/$or/... 的标准流程）：

[frontends/ast/genrtlil.cc:105-133](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L105-L133) —— `addCell(name, type)` 建单元 → `addWire(name+"_Y", result_width)` 建输出线 → 设 `A_SIGNED/B_SIGNED/A_WIDTH/B_WIDTH/Y_WIDTH` 参数 → `setPort(A/B/Y, ...)` 接端口 → 返回输出线。这正是「一个二元门」的全部构造。

一元运算工厂 `uniop2rtlil`：

[frontends/ast/genrtlil.cc:46-70](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L46-L70) —— 与 `binop2rtlil` 同构，只是只有一个 A 输入（如 `$not`/`$neg`/`$reduce_or`）。

多路器工厂 `mux2rtlil`（三目 `?:` → `$mux`）：

[frontends/ast/genrtlil.cc:136-164](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L136-L164) —— 注意端口约定：`A=right`（假支）、`B=left`（真支）、`S=cond`，参数 `WIDTH=left.size()`。

二元位运算的 Clifford's Device 分发（`AST_BIT_AND` 等 → `$and` 等）：

[frontends/ast/genrtlil.cc:1842-1857](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1842-L1857) —— 四个 `if(0){case ...}` 分别设 `type_name`，公共段递归求左右 SigSpec 后 `binop2rtlil`。这就是 `a & b` → `$and` 的落点。

三目运算符 `AST_TERNARY`（含「常量条件直接消解」优化）：

[frontends/ast/genrtlil.cc:1981-2017](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1981-L2017) —— 若 `cond.is_fully_def()`（条件是确定常量），直接返回真/假支之一（不建 `$mux`）；否则 `mux2rtlil` 建一个 `$mux`。这是「编译期短路」的一个例子。

常量节点 `AST_CONSTANT`：

[frontends/ast/genrtlil.cc:1578-1596](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1578-L1596) —— 直接返回 `SigSpec(bitsAsConst())`，**不建任何单元**——常量就是字面值。

标识符节点 `AST_IDENTIFIER`：

[frontends/ast/genrtlil.cc:1601-1648](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1601-L1648) —— 开头 `log_assert(id2ast != nullptr)`（[genrtlil.cc:1611](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1611)）：这正是 simplify 必须先跑的原因——`id2ast` 是 simplify 设的指针，指明该标识符引用的是哪棵声明子树。随后按 `id2ast->type` 分别处理：指向 `AST_WIRE` → 取出对应 Wire 的 SigSpec；指向 `AST_PARAMETER` → 取其常量值；指向 `AST_AUTOWIRE` 且模块里还没有这根线 → 隐式声明（`autowire`）。

连续赋值 `AST_ASSIGN`（模块级 `assign`）：

[frontends/ast/genrtlil.cc:2143-2163](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L2143-L2163) —— 左边 `children[0]->genRTLIL()` 得 lvalue，右边 `children[1]->genWidthRTLIL(left.size(), true)` 按左宽求值（顺便做位宽对齐），最后 `current_module->connect(SigSig(left, right))` 写一条 RTLIL 级 `assign`（即 u2-l3 讲过的 `Module::connections_`）。注意它会警告并丢弃「对常量位赋值」。

线声明 `AST_WIRE`（建 Wire）：

[frontends/ast/genrtlil.cc:1516-1544](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1516-L1544) —— `addWire(str, range_left - range_right + 1)` 按声明位宽建线，并设好 `port_input/port_output/start_offset/upto/is_signed` 等几何与端口属性。

#### 4.3.4 代码实践

**实践目标**：把 `assign y = a & b | c;` 从 AST 到 RTLIL 的对应「眼见为实」。

**操作步骤**：

1. 准备 `expr.v`（示例代码，非项目原有）：
   ```verilog
   module expr(input [3:0] a, b, c, output [3:0] y);
     assign y = (a & b) | c;
   endmodule
   ```
2. 打印简化后的 AST：
   ```bash
   ./build/yosys -p "read_verilog -dump_ast2 expr.v" 2>&1 | grep -A40 "AST_MODULE"
   ```
3. 生成 RTLIL 文本：
   ```bash
   ./build/yosys -p "read_verilog expr.v; write_rtlil"
   ```

**需要观察的现象**：

- `-dump_ast2` 里应看到 `AST_ASSIGN`，其右值是 `AST_BIT_OR`，`AST_BIT_OR` 的 `children[0]` 是 `AST_BIT_AND`（套 `AST_IDENTIFIER \a`、`\b`），`children[1]` 是 `AST_IDENTIFIER \c`。
- `write_rtlil` 输出里应出现两个 cell：一个 `$and`（端口 `A=\a B=\b Y=$and$..._Y`）和一个 `$or`（端口 `A=$and$..._Y B=\c Y=$or$..._Y`），外加一条 `connect \y $or$..._Y`（具体名字里的序号每次可能不同）。

**预期结果**：AST 的两层运算符（`BIT_OR` 套 `BIT_AND`）与 RTLIL 的两个单元（`$or` 吃 `$and` 的输出）一一对应。把两份输出并排放，你能逐个节点/单元地把它们对上——这就验证了「运算符 → `$` 单元、操作数 → 端口 SigSpec」的翻译规则。

> 若无法运行，标注「待本地验证」，直接对照 [frontends/ast/genrtlil.cc:1842-1857](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1842-L1857)（位运算分发）与 [frontends/ast/genrtlil.cc:2143-2163](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L2143-L2163)（assign 落地）理解产物形态。

#### 4.3.5 小练习与答案

**练习 1**：`assign y = a & b | c;` 综合后会生成几个 `$` 单元？分别是哪种？
> 答案：2 个——一个 `$and`（实现 `a & b`）和一个 `$or`（实现 `... | c`，它的 A 输入接 `$and` 的输出）。C 语言/Verilog 里 `&` 优先级高于 `|`，故 AST 是 `BIT_OR(BIT_AND(a,b), c)`，对应「先 and 后 or」两单元。见 [frontends/ast/genrtlil.cc:1842-1857](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1842-L1857)。

**练习 2**：`AST_CONSTANT` 节点的 `genRTLIL` 会建单元吗？`AST_IDENTIFIER` 呢？
> 答案：都不会建「运算单元」。`AST_CONSTANT` 直接返回一个常量 `SigSpec`（[genrtlil.cc:1578-1596](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1578-L1596)）；`AST_IDENTIFIER` 返回它 `id2ast` 指向的那根 Wire 的 SigSpec（[genrtlil.cc:1601-1648](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1601-L1648)）。只有运算符节点（且未被常量折叠掉）才会建 `$` 单元。

**练习 3**：Clifford's Device 里的 `if(0){case AST_BIT_AND: type_name=...;}` 为什么能工作？直接写四个独立 `case` 不行吗？
> 答案：`if(0)` 的条件恒假，所以「进入这个 case 后不执行赋值」，而是 fall-through 到下面所有 case 之后的那段公共代码——从而让多个 case 共用一份「求操作数 + 建 binop」逻辑。直接写四个独立 `case` 当然也行，但会把相同的「求 left/right + 调 binop2rtlil」代码复制四遍；Clifford's Device 是一种用 case 标签 + fall-through 来「共享尾段」的紧凑写法。见 [frontends/ast/genrtlil.cc:1423-1426](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1423-L1426) 的注释链接。

---

## 5. 综合实践

**任务**：用本讲的三件套（`-dump_ast1` / `-dump_ast2` / `write_rtlil`）完整追踪一条「从源码到网表」的转换链，并把每个阶段手工对齐。

设计文件 `mux_add.v`（示例代码，非项目原有）：

```verilog
module mux_add(input [3:0] a, b, input sel, output [3:0] y);
  assign y = sel ? (a + b) : (a & b);
endmodule
```

**操作步骤**：

1. 看简化前 AST，确认表达式结构与参数情况：
   ```bash
   ./build/yosys -p "read_verilog -dump_ast1 mux_add.v"
   ```
   预期看到 `AST_ASSIGN` 右值是 `AST_TERNARY`，三目分支分别是 `AST_ADD`、`AST_BIT_AND`。
2. 看简化后 AST，确认常量折叠与名字解析：
   ```bash
   ./build/yosys -p "read_verilog -dump_ast2 mux_add.v"
   ```
   预期 `AST_IDENTIFIER` 都已能解析（`id2ast` 就绪）；这里没有 generate 与可折叠常量，结构应与 dump1 基本一致。
3. 看 RTLIL 网表：
   ```bash
   ./build/yosys -p "read_verilog mux_add.v; write_rtlil"
   ```
   预期看到三个单元：一个 `$add`（实现 `a+b`）、一个 `$and`（实现 `a&b`）、一个 `$mux`（按 `sel` 选两者，见 [frontends/ast/genrtlil.cc:1981-2017](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1981-L2017)），外加 `A_WIDTH/B_WIDTH/Y_WIDTH` 参数与一条 `\y` 的 connect。
4. 把三份输出并排，画一张映射表：`AST_TERNARY → $mux`、`AST_ADD → $add`、`AST_BIT_AND → $and`、`AST_IDENTIFIER(\a) → \a 这根线`，验证「AST 每个运算符节点 ↔ RTLIL 一个单元」。

**需要观察的现象与预期结果**：三目运算符在 `sel` 非常量时一定生成 `$mux`（不会短路）；`$add` 与 `$and` 的输出分别接到 `$mux` 的 `B`（真支）与 `A`（假支）；`\y` 被 connect 到 `$mux` 的 `Y`。整条链路完整复现了 4.3.2 描述的生成过程。

> 若无法运行，全部步骤标注「待本地验证」，但映射关系可纯靠阅读源码得出：三目见 [genrtlil.cc:1981-2017](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1981-L2017)、加法见 [genrtlil.cc:1934-1962](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1934-L1962)、位与见 [genrtlil.cc:1842-1857](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1842-L1857)。

## 6. 本讲小结

- **`AST::process()` 是 Verilog 前端的最终入口**：它遍历 `AST_DESIGN` 的每个模块，调 `process_module`，后者按「dump_ast1 → simplify → dump_ast2 → 两趟 genRTLIL」的固定顺序编排（[frontends/ast/ast.cc:1142-1289](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1142-L1289)）。
- **simplify 与 genRTLIL 职责分离**：simplify 只改写 AST（不碰 RTLIL），genRTLIL 只读 AST 产出 RTLIL（基本不再改 AST）。先简化、后生成。
- **simplify 是「跑到不动点」的改写器**：返回 `bool`，调用方 `while` 包住；stage 0 内部交替跑 stage 1（常规简化）→ mem2reg → stage 2。它做名字解析/`id2ast` 设置、宽度推导、常量折叠、generate 展开、mem2reg 五类工作。
- **常量折叠有触发条件**：仅在「必须静态求值的上下文」或 `const_fold=true` 且运算两侧皆为 `AST_CONSTANT` 时发生（[simplify.cc:4527-4592](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/simplify.cc#L4527-L4592)）；含变量的运算保留给 genRTLIL。
- **genRTLIL 用工厂函数 + 大 switch 翻译节点**：`binop2rtlil`/`uniop2rtlil`/`mux2rtlil` 封装「建单元 + 建输出线 + 接端口 + 设参数」；表达式节点返回 `SigSpec`，运算符映射到 `$` 单元（`AST_BIT_AND→$and`、`AST_ADD→$add`、`AST_TERNARY→$mux`）。
- **Clifford's Device** 与 **`apply_newNode` 收口** 是两处统一的工程化设计：前者让多个 case 共享代码段，后者让 simplify 的几十种改写共用一套节点替换机制。

## 7. 下一步学习建议

- **进入核心综合流程（u6）**：本讲产物里的 `RTLIL::Process`（来自 `AST_ALWAYS`）和 `$mem`/`$dff` 等还会被进一步变换。建议接着读 **u6-l2（proc）**，看 `proc` pass 如何把 Process 翻译成 `$mux`/`$dff`；以及 **u6-l3（opt）**，看 `opt_expr` 如何在 RTLIL 层面继续做常量折叠与化简（与本讲 AST 层的折叠对照）。
- **其他前端如何殊途同归（u5-l5）**：本讲只讲了 Verilog 前端的 AST→RTLIL。`read_json`/`read_blif`/`read_liberty` 等前端同样产出到同一套 RTLIL，可读 u5-l5 建立全局观。
- **深入 always 的进程生成**：若对 `ProcessGenerator`（`AST_ALWAYS` → `RTLIL::Process`）感兴趣，可直接精读 [frontends/ast/genrtlil.cc:311-620](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L311-L620)，它是 genRTLIL 里最复杂的部分，配合 u6-l2 的 proc pass 一起看收效最好。
- **宽度推导的细节**：本讲只点到 `detectSignWidth`。若要彻底理解 Verilog 的位宽传播规则（自决定上下文、符号扩展），可精读 [frontends/ast/genrtlil.cc:1046-1400](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/genrtlil.cc#L1046-L1400) 的 `detectSignWidthWorker`。
