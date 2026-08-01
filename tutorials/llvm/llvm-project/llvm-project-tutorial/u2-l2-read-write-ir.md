# 阅读与编写 LLVM IR（.ll 文本格式）

## 1. 本讲目标

学完本讲，你应该能够：

- 读懂一段简单的 `.ll` 文本 IR，分辨出**模块头**、**函数签名**、**基本块**和**指令**四个层次。
- 理解 **SSA（静态单赋值）** 在文本 IR 中如何体现，看懂寄存器命名 `%name` 与 `%数字` 的区别。
- 看懂 `phi` 指令在控制流汇合点上的含义。
- 会用 `clang -S -emit-llvm` 把一段 C 代码变成 `.ll`，并手工修改其中一条指令后重新验证。

本讲的核心理念是：**`.ll` 文本只是内存 IR 对象的一种「可读序列化形式」**。理解它的最好办法，是同时看「打印它」的代码（AsmWriter）和「解析它」的代码（LLParser）——它们互为镜像。

---

## 2. 前置知识

本讲承接 **u2-l1（三段式编译器设计与 IR 的角色）**，你需要先掌握以下概念：

| 概念 | 一句话回顾 |
|---|---|
| 三段式架构 | 前端（Clang）→ IR → 后端（`llc`），IR 是解耦前后端的桥梁。 |
| IR 的三种形态 | 内存 `Module` 对象、`.ll` 文本汇编、`.bc` 紧凑位码，三者以 `Module` 为中介无损互转（见 u1-l4）。 |
| `clang -emit-llvm` | 在前端出口截断，直接拿到 `.ll`（`-S`）或 `.bc`（`-c`）。 |
| `Module` | 一个 IR 的顶层容器，对应一个「翻译单元」（见 u1-l4、u2-l1）。 |

如果这些概念你还很模糊，建议先回到 u2-l1 把三段式图示过一遍，再继续本讲。

本讲会反复用到两个新术语：

- **AsmWriter（汇编打印器）**：把内存 `Module` 打印成 `.ll` 文本的代码。
- **LLParser（LL 汇编解析器）**：把 `.ll` 文本解析回内存 `Module` 的代码。

---

## 3. 本讲源码地图

本讲涉及的关键文件都围绕「文本 IR 的读与写」：

| 文件 | 作用 | 本讲用来 |
|---|---|---|
| `llvm/lib/IR/AsmWriter.cpp` | 把内存 `Module` 打印成 `.ll` 文本（「写」） | 讲清 `.ll` 的打印格式从何而来 |
| `llvm/lib/AsmParser/LLParser.cpp` | 把 `.ll` 文本解析成内存 `Module`（「读」） | 讲清 `.ll` 的语法规则由谁定义 |
| `llvm/lib/AsmParser/LLLexer.cpp` | 词法分析器，把字符流切成 Token | 解释 `%name` 与 `%数字` 的 Token 区别 |

记住一个对称关系：AsmWriter 负责写、LLParser 负责读，两者必须严格对应，否则 `llvm-dis` 打印出来的文件 `llvm-as` 就读不回去。这对你修改 `.ll` 时的「合法性边界」判断很有帮助。

---

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 `.ll` 文本语法**：模块头、函数签名、类型标注。
- **4.2 基本块与 SSA 指令**：基本块、SSA、`phi`、寄存器命名。

### 4.1 `.ll` 文本语法

#### 4.1.1 概念说明

一段 `.ll` 文件就是一个 **Module（模块）** 的文本表示。它的结构是严格分层的：

```
Module（模块）
├── 模块头：ModuleID、source_filename、target triple、target datalayout
├── 类型定义：%foo = type { i32, ptr }
├── 全局变量：@g = global i32 0
└── 函数列表
     └── Function（函数）
          ├── 函数签名：define <返回类型> @名字(参数列表) { ... }
          └── BasicBlock（基本块）列表
               └── Instruction（指令）列表
```

也就是说，`.ll` 文件从上到下大致是「**头信息 → 全局声明 → 函数定义**」三段式。每个函数内部又是一个「**签名 → 基本块 → 指令**」的小三段。

几个直觉要点：

- **注释以 `;` 开头**，到行尾。AsmWriter 打印时会自动加很多 `;` 注释（如 `; ModuleID =`、`; preds =`），这些只是给人看的，不影响语义。
- **全局符号用 `@` 开头**（如 `@add`、`@g`），**局部符号用 `%` 开头**（如 `%3`、`%result`）。这是最容易认错的两个前缀。
- **每个值/参数/操作数前面都带类型**，例如 `add i32 %5, %6` 里的 `i32`。LLVM IR 是「类型显式」的汇编。

#### 4.1.2 核心流程

**写入流程（AsmWriter 视角）**：当一个内存 `Module` 被打印时，`printModule` 按固定顺序逐段输出，伪代码如下：

```
printModule(M):
  输出 "; ModuleID = '...'"
  输出 "source_filename = \"...\""
  输出 "target datalayout = \"...\""
  输出 "target triple = \"...\""
  输出类型定义 / 全局变量 / 别名
  for 每个函数 F:
    printFunction(F)        # 含签名 + 函数体
  输出属性组 / 命名元数据
```

`printFunction` 再往下递归：

```
printFunction(F):
  输出 "define" 或 "declare"            # 是否有函数体
  输出链接类型 / 返回类型 / @名字
  输出 "( 参数类型 参数名, ... )"
  if 有函数体:
    输出 " {"
    for 每个基本块 BB: printBasicBlock(BB)
    输出 "}"
```

**读取流程（LLParser 视角）**：解析是写入的逆过程，由一个顶层 `while` 循环不断读取「顶层实体」。每读到一个关键字（`define`、`declare`、`target`、`@全局`、`%类型`……）就派发到对应的解析函数。关键在于：**解析顺序必须和打印顺序兼容**，所以你也可以把 LLParser 当成「`.ll` 语法的权威说明书」来读。

#### 4.1.3 源码精读

**① 模块头的打印**（AsmWriter）——这段代码直接决定了你看到的 `; ModuleID=`、`source_filename=`、`target datalayout=`、`target triple=` 这几行的来源：

[llvm/lib/IR/AsmWriter.cpp:L3111-L3127](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/IR/AsmWriter.cpp#L3111-L3127) — 先打印 `ModuleID` 与 `source_filename`，再打印 `target datalayout`（数据布局，描述指针大小、对齐、字节序等）与 `target triple`（目标三元组，如 `x86_64-unknown-linux-gnu`）。注意 `ModuleID` 和 `;` 注释一样是给人看的，`datalayout`/`triple` 才是影响代码生成的语义信息。

**② 模块头的解析**（LLParser）——与上面镜像。当解析器遇到 `target` 关键字时：

[llvm/lib/AsmParser/LLParser.cpp:L693-L715](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/AsmParser/LLParser.cpp#L693-L715) — 根据跟在 `target` 后面的是 `triple` 还是 `datalayout`，分别调用 `M->setTargetTriple(...)` 或把字符串暂存。`datalayout` 被稍后回调解析（因为它会影响类型的大小计算）。

**③ 函数签名的打印**（AsmWriter）——`define`/`declare` 的分流就在这里：

[llvm/lib/IR/AsmWriter.cpp:L4166-L4224](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/IR/AsmWriter.cpp#L4166-L4224) — 若 `F->isDeclaration()`（无函数体，只是声明）则打印 `declare`，否则打印 `define `；随后依次打印链接属性、调用约定、返回类型、函数名 `@name`、括号内的参数列表（每个参数是 `类型 属性 %名字`），变参函数还会补上 `...`。

**④ 顶层实体的派发**（LLParser）——这是理解整个 `.ll` 语法的「总入口」：

[llvm/lib/AsmParser/LLParser.cpp:L584-L639](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/AsmParser/LLParser.cpp#L584-L639) — 一个大 `switch`，根据当前 Token 的种类派发：`kw_declare`→`parseDeclare`、`kw_define`→`parseDefine`、`kw_module`→`parseModuleAsm`、`LocalVar`→`parseNamedType`（即 `%foo = type ...`）、`GlobalVar`→`parseNamedGlobal`（即 `@g = ...`）、`kw_attributes`→属性组等等。这张表实际上枚举了「一个 `.ll` 文件顶层允许出现哪些东西」。

**⑤ 类型标注的解析**——这是初学者最常困惑的点：为什么每个操作数前面都要写类型？因为 IR 要求「类型显式」。`parseType` 是所有类型语法的总入口：

[llvm/lib/AsmParser/LLParser.cpp:L3177-L3206](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/AsmParser/LLParser.cpp#L3177-L3206) — 处理各种类型写法：`float`/`void` 等内置类型直接取词法值；`ptr`（不透明指针，现代 LLVM 的标准写法）；`[...]` 数组、`<...>` 向量、`{...}` 结构体；以及 `%foo`、`%4` 这种命名/编号的自定义类型。注意第 3197 行专门拒绝 `ptr*`（旧式带元素类型的指针写法），引导你用 `ptr`。

常见的类型写法速查：

| 文本写法 | 含义 |
|---|---|
| `i32`、`i64` | 32/64 位整数 |
| `float`、`double` | 32/64 位浮点 |
| `ptr` | 不透明指针（新写法，替代旧的 `i32*`） |
| `[4 x i32]` | 含 4 个 `i32` 的数组 |
| `<4 x float>` | 含 4 个 `float` 的向量（SIMD） |
| `{ i32, ptr }` | 结构体 |
| `i32 (i32, i32)` | 「接收两个 i32、返回 i32」的函数类型 |

#### 4.1.4 代码实践

> **实践目标**：亲手生成一段 `.ll`，认出模块头、函数签名、参数类型三处结构。

**操作步骤**：

1. 新建文件 `add.c`，写入一个返回两数之和的函数：

   ```c
   int add(int a, int b) {
       return a + b;
   }
   ```

2. 用 Clang 在前端出口截断，生成文本 IR（注意是 `-S -emit-llvm`，`-S` 表示输出汇编文本而非位码）：

   ```bash
   clang -S -emit-llvm add.c -o add.ll
   ```

3. 用任意编辑器或 `cat add.ll` 打开 `add.ll`。

**需要观察的现象**：

- 文件顶部的 `; ModuleID = 'add.c'` 与 `source_filename = "add.c"`。
- `target triple = "..."` 一行，记录目标平台。
- `define` 开头的函数签名，形如 `define dso_local i32 @add(i32 noundef %a, i32 noundef %b) ...`。
- 注意 `@add` 是全局函数名（`@` 前缀），`%a`、`%b` 是局部参数（`%` 前缀），每个参数前都显式写了类型 `i32`。

**预期结果（典型输出，省略了属性组等细节）**：

```ll
; ModuleID = 'add.c'
source_filename = "add.c"
target datalayout = "e-m:e-..."
target triple = "x86_64-unknown-linux-gnu"

define dso_local i32 @add(i32 noundef %a, i32 noundef %b) #0 {
  ; 函数体在下一节继续讲解
  ret i32 0   ; 占位说明，真实内容见 4.2.4
}

attributes #0 = { ... }
```

> 说明：上面的函数体是简化占位。`-O0` 默认输出会包含一长串 `alloca`/`store`/`load`，具体内容见 4.2.4 的实践；不同 Clang 版本和目标平台，`datalayout`/`triple` 字符串会有差异，**完整字符串待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：把第 ② 段实践里的 `add.c` 改成无函数体的声明 `int add(int, int);`（不放定义），再用 `clang -S -emit-llvm` 生成。`define` 会变成什么？

**参考答案**：会变成 `declare dso_local i32 @add(i32, i32)`。注意两点：一是关键字从 `define` 变成 `declare`（无函数体）；二是声明里参数没有名字、只剩类型（`i32, i32`），因为名字在声明里没有意义。这正对应 AsmWriter 第 4196-4209 行「声明时只打印参数类型、不打印参数名」的分支。

**练习 2**：在 `.ll` 里，`@x` 和 `%x` 分别表示什么？如果混用会发生什么？

**参考答案**：`@` 前缀表示**全局**符号（全局变量、函数、别名），`%` 前缀表示**局部**符号（局部寄存器、参数、局部类型/基本块标签）。两者命名空间相互独立。混用通常会导致解析错误——比如把全局变量写成 `%x` 会在当前作用域里找不到对应定义。

---

### 4.2 基本块与 SSA 指令

#### 4.2.1 概念说明

函数体内部由若干 **基本块（BasicBlock）** 组成。基本块的定义是：

- 一个**入口**（可能带标签，如 `entry:`、`if.then:`）。
- 一串**顺序执行的指令**。
- 恰好以一个**终结指令（terminator）**结尾，如 `ret`、`br`、`switch`。终结指令之后控制流一定会离开本块。

换句话说，基本块内部「一旦进入就一定从头执行到尾，中间不会跳进/跳出」。

**SSA（Static Single Assignment，静态单赋值）** 是 LLVM IR 的灵魂。它的规则是：**每个寄存器（`%` 值）在整个函数里只被赋值（定义）一次**。例如：

```ll
%result = add i32 %a, %b    ; %result 在此处被定义一次
```

之后所有地方用到这个加法结果，都只能引用 `%result`，不能再给它赋别的值。SSA 的好处是每个值的「定义点」唯一，极大简化了数据流分析与优化。

但现实代码里有「同一个变量在不同分支取不同值」的情况（如 `if/else` 后取 `a` 或 `b`）。在 SSA 里这由 **`phi` 指令**解决：它在控制流汇合点，根据「是从哪个基本块跳过来的」选择对应的值。形式为：

```ll
%result = phi i32 [ %a, %if.then ], [ %b, %if.else ]
```

读法：「如果来自 `%if.then` 块，则取 `%a`；如果来自 `%if.else` 块，则取 `%b`」。`phi` 必须是基本块的第一条指令。

**寄存器命名的两种形式**：

| 形式 | 例子 | 含义 |
|---|---|---|
| 命名（named） | `%result`、`%a` | 带可读名字，由前端或人工命名 |
| 编号（numbered） | `%3`、`%7` | 无名值，按定义顺序自动编号 |

两者语义等价，名字只是给人看的「糖」。编号规则是：**按值在函数里被定义的先后顺序从小到大分配**，参数先编号，再轮到指令。这正是为什么 `%0`、`%1` 常常是参数，而 `%3`、`%4` 才是函数体里的第一条指令。

#### 4.2.2 核心流程

**指令的打印与 SSA 命名**——AsmWriter 打印每条指令时的逻辑（伪代码）：

```
printInstruction(I):
  若 I 有名字:       输出 "%名字 = "
  否则若结果非 void: 输出 "%编号 = "
  （void 指令如 store/ret/br 不输出 "=" 左值）
  输出操作码 + 操作数（每个操作数带类型）
```

**指令的解析与编号分配**——LLParser 在 `parseBasicBlock` 里逐条解析指令，每解析完一条就调用 `setInstName` 给它登记名字/编号。编号登记表 `NumberedVals` 里，参数在最前面（`%0`、`%1`……），指令随后追加。所以一个 `%数字` 到底指哪个值，完全由「它是第几个被登记的无名值」决定。

**前向引用（forward reference）**：因为 SSA 允许「先用后定义」（例如 `phi` 引用的基本块标签、或 `br` 跳转的目标可能在文本上还没出现），解析器必须支持「先占位、后回填」。`getVal` 在找不到现成定义时，会创建一个临时的占位 `Argument`，等真正定义出现时再把占位「替换」掉。

#### 4.2.3 源码精读

**① 基本块的打印**（AsmWriter）——决定了基本块标签和 `; preds =` 注释的来源：

[llvm/lib/IR/AsmWriter.cpp:L4320-L4349](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/IR/AsmWriter.cpp#L4320-L4349) — 若基本块有名字则打印 `名字:` 作为标签；否则打印编号 `数字:`。随后打印 `; preds = ...` 注释列出前驱块（便于人读，无语义）。注意入口块（`entry`）不打印标签和 preds。

**② 指令的 SSA 命名打印**（AsmWriter）——`%名字 = ` 还是 `%编号 = ` 的分流：

[llvm/lib/IR/AsmWriter.cpp:L4443-L4466](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/IR/AsmWriter.cpp#L4443-L4466) — 有名字就 `printLLVMName`，无名字但非 void 就取 `Machine.getLocalSlot(&I)` 得到编号并打印 `%编号 = `；void 类型指令（`store`/`ret`/`br`）跳过整个左值。随后打印操作码 `I.getOpcodeName()`。

**③ phi 指令的打印**——`phi` 那串 `[ 值, 块 ]` 列表就是这里产生的：

[llvm/lib/IR/AsmWriter.cpp:L4538-L4551](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/IR/AsmWriter.cpp#L4538-L4551) — 对 `PHINode` 特殊处理，先打印结果类型，再用 `zip_equal` 把「入值」和「来源块」配对，逐对打印 `[ value, block ]`，逗号分隔。

**④ 基本块的解析**（LLParser）——与打印镜像：

[llvm/lib/AsmParser/LLParser.cpp:L7393-L7408](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/AsmParser/LLParser.cpp#L7393-L7408) — 先识别块标签（`LabelStr` 命名或 `LabelID` 编号），调用 `defineBB` 注册该块；随后进入循环逐条解析指令，直到读到终结指令。

**⑤ 指令操作码的派发**——和顶层实体派发类似，这里是「指令级」的语法枚举：

[llvm/lib/AsmParser/LLParser.cpp:L7624-L7652](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/AsmParser/LLParser.cpp#L7624-L7652) — `switch` 把操作码 Token 派发到具体解析函数：`kw_ret`→`parseRet`、`kw_br`→`parseBr`、`kw_add`→`parseArithmetic`、`kw_phi`→`parsePHI`……这张表实际上枚举了「一条指令允许以哪些关键字开头」。

**⑥ `%名字` 与 `%数字` 的词法区别**——这是 SSA 命名最底层的根：

[llvm/lib/AsmParser/LLLexer.cpp:L423-L429](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/AsmParser/LLLexer.cpp#L423-L429) — 注释写明：`%` 后跟合法标识符（`%[-a-zA-Z$._][-a-zA-Z$._0-9]*`）是 `LocalVar`（命名局部值），`%` 后跟纯数字（`%[0-9]+`）是 `LocalVarID`（编号局部值）。`@` 前缀同理区分 `GlobalVar`/`GlobalID`。所以 `%3` 和 `%result` 在词法层就是两种不同 Token，解析器据此决定走「按名字查」还是「按编号查」。

**⑦ 按名字 / 按编号查值 + 前向引用**——这是「为什么 SSA 允许先用后定义」的来源：

[llvm/lib/AsmParser/LLParser.cpp:L3981-L4019](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/AsmParser/LLParser.cpp#L3981-L4019) — `getVal(Name, Ty)`：先在函数符号表查名字；查不到就在 `ForwardRefVals` 里找已有占位；都没有就**新建一个占位 `Argument`**并记下位置，等真正定义出现时回填。这一段解释了 SSA 前向引用的机制。

[llvm/lib/AsmParser/LLParser.cpp:L4022-L4052](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/AsmParser/LLParser.cpp#L4022-L4052) — `getVal(ID, Ty)`：编号版本的同样逻辑，用 `NumberedVals.get(ID)` 查编号表，查不到则建占位。

**⑧ 给指令登记名字/编号**——决定了 `%数字` 的编号顺序：

[llvm/lib/AsmParser/LLParser.cpp:L4057-L4110](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/AsmParser/LLParser.cpp#L4057-L4110) — `setInstName`：若指令无名也无显式编号（`NameID == -1`），就用 `NumberedVals.getNext()` 自动分配下一个编号（第 4072 行）。同时回填可能存在的前向引用占位（`replaceAllUsesWith`）。配合构造函数里「先把无名参数塞进 `NumberedVals`」（见 `PerFunctionState` 构造）就能解释：参数占 `%0`、`%1`，指令从 `%2` 之后继续编号。

**⑨ phi 的解析**——`phi` 的 `[ 值, 块 ]` 语法在这里被消费：

[llvm/lib/AsmParser/LLParser.cpp:L8673-L8714](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/AsmParser/LLParser.cpp#L8673-L8714) — `parsePHI`：先解析结果类型，再循环解析每一对 `[ 值, 基本块 ]`（值用 `parseValue`、块用 `parseValue(..., label type)`），最后 `PHINode::Create` + `addIncoming` 组装出 `PHINode`。

#### 4.2.4 代码实践

> **实践目标**：读懂 `add` 函数的 SSA 指令与基本块，并亲手写一个含 `phi` 的 `max` 函数。

**操作步骤**：

1. 延续 4.1.4 的 `add.c`，这次看 `-O0` 默认产出的完整函数体（这是初学者最先会看到的「啰嗦」版本）：

   ```bash
   clang -S -emit-llvm -O0 add.c -o add.ll
   ```

   **预期输出（典型，`add` 的函数体部分）**：

   ```ll
   define dso_local i32 @add(i32 noundef %0, i32 noundef %1) #0 {
     %3 = alloca i32, align 4
     %4 = alloca i32, align 4
     store i32 %0, ptr %3, align 4
     store i32 %1, ptr %4, align 4
     %5 = load i32, ptr %3, align 4
     %6 = load i32, ptr %4, align 4
     %7 = add nsw i32 %5, %6
     ret i32 %7
   }
   ```

   逐行读懂它（只有一个基本块，标签被省略——它是入口块 `entry`）：

   | 指令 | 含义 |
   |---|---|
   | 参数 `%0`、`%1` | 两个 `i32` 参数，未命名故用编号（`%0`/`%1`）。注意 `%2` 没出现，是因为该目标 ABI 下编号有预留/跳号，**具体待本地验证**。 |
   | `%3 = alloca i32` | 在栈上分配一个 `i32` 空间，结果是指针，地址记到 `%3`。 |
   | `store i32 %0, ptr %3` | 把参数 `%0` 存入 `%3` 指向的内存。 |
   | `%5 = load i32, ptr %3` | 从 `%3` 读回 `i32`，结果记到 `%5`。 |
   | `%7 = add nsw i32 %5, %6` | 整数加法，`nsw` 表示「有符号不溢出」承诺。 |
   | `ret i32 %7` | 终结指令，返回 `%7`。 |

2. **尝试修改其中一条指令**：把 `%7 = add nsw i32 %5, %6` 改成减法 `%7 = sub nsw i32 %5, %6`，存盘。然后用 `llvm-as` 把它汇编成位码，再用 `llvm-dis` 还原，验证它仍是合法 IR：

   ```bash
   llvm-as add.ll -o add.bc
   llvm-dis add.bc -o add.check.ll
   diff add.ll add.check.ll     # 观察改写是否被保留
   ```

3. **写一个含 `phi` 的 IR**（手写示例）。新建 `max.ll`，写入下面的「示例代码」：

   ```ll
   ; 示例代码：手写一个用 phi 取两数较大值的函数
   define i32 @max(i32 %a, i32 %b) {
   entry:
     %cmp = icmp sgt i32 %a, %b        ; 有符号比较 a > b，结果是 i1
     br i1 %cmp, label %then, label %else

   then:
     br label %merge

   else:
     br label %merge

   merge:
     %result = phi i32 [ %a, %then ], [ %b, %else ]   ; 按来源块选值
     ret i32 %result
   }
   ```

   用 `llvm-as` 检验它语法合法：

   ```bash
   llvm-as max.ll -o max.bc && echo "OK"
   ```

**需要观察的现象**：

- `add.ll` 里 `%0`/`%1` 是参数，`%3` 起是指令——印证「参数先编号、指令随后」。
- `max.ll` 里 `%cmp` 是命名值、`%result` 也是命名值；`phi` 必须紧跟在 `merge:` 标签后（是块内第一条指令），它的每个入值都标注了来源块。
- 修改 `add` 的 `add`→`sub` 后，`llvm-as` 仍能成功汇编（`sub` 是合法操作码），说明只要不破坏 SSA 与类型规则，局部改写 IR 是被允许的。

**预期结果**：`max.ll` 能被 `llvm-as` 成功汇编成 `max.bc`；`add.ll` 改成 `sub` 后也能成功汇编。两条「修改指令」实践都应通过解析（验证了 LLParser 对合法操作码的接受）。具体运行结果与目标 ABI、Clang 版本相关，**完整输出待本地验证**。

> 若想直观看到 `phi` 被「优化掉」，可在 `max.ll` 上跑一次 `opt -passes=instcombine,simplifycfg`，通常会把 `phi` 折叠成一条 `select`——这是后续 u4（Pass 与优化框架）的内容，此处了解即可。

#### 4.2.5 小练习与答案

**练习 1**：在 `max.ll` 的 `merge` 块里，如果把 `phi` 挪到 `%cmp` 之后、`br` 之前（即 `entry` 块中部），会发生什么？

**参考答案**：解析会失败或语义错误。`phi` 的语义依赖「来自哪个前驱块」，只有当它处于一个「有多个前驱」的汇合块开头时才有意义。把它放在 `entry`（无前驱的入口块）中部，既不符合「phi 必须是块的第一条指令」的约束，也无前驱可参照。实践中 `llvm-as`/`opt` 的校验器（Verifier）会报错。

**练习 2**：为什么 `store i32 %0, ptr %3` 没有 `%xxx =` 左值？

**参考答案**：因为 `store` 的返回类型是 `void`。AsmWriter 在打印指令时，对 void 类型指令不输出左值（见 4.2.3 的第 ② 点，`I.getType()->isVoidTy()` 分支）。同理 `ret`、`br`、`switch` 这些终结指令也没有左值。

**练习 3**：下面这条 IR 合法吗？为什么。`%x = add i32 %y, 1` 后面又出现 `%x = add i32 %y, 2`。

**参考答案**：不合法，违反 SSA。`%x` 被定义了两次。在 SSA 下每个寄存器只能被赋值一次，第二个定义必须换个名字（如 `%x2`）。若用编号，第二个会自动得到不同编号，所以「重复定义」几乎只在你手写命名 IR 时踩到——这正是手写 IR 时最常见的坑。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个小任务：

1. 写一个 C 函数 `int loop_sum(int n)`，用 `for` 循环累加 `0..n`。

   ```c
   int loop_sum(int n) {
       int s = 0;
       for (int i = 0; i < n; i++) s += i;
       return s;
   }
   ```

2. 用 `clang -S -emit-llvm -O1 loop_sum.c -o loop_sum.ll` 生成 IR（`-O1` 会保留循环结构而不是把它优化没，更利于观察）。

3. 在生成的 `.ll` 里完成以下阅读任务（**输出结果待本地验证**，重点是培养阅读能力）：

   - 找出函数里有几个**基本块**，分别给它们起个能反映含义的名字（如 `entry`/`loop.cond`/`loop.body`/`loop.end`）。
   - 找到循环头基本块里的 **`phi` 指令**，读懂它在「第一次进入循环」和「每次循环回来」两种情况下分别取什么初值——这是 SSA 表达「可变循环变量」的标准手法。
   - 用本讲学到的命名规则，解释为什么某些值是 `%数字`、某些是 `%名字`。
   - 把 `phi` 的某个入值故意改成错误类型（如把 `i32` 初值改成 `ptr`），用 `llvm-as` 重新汇编，观察 LLParser 报出的类型错误信息，定位它对应 4.2.3 中哪段解析逻辑。

4. 用一句话总结：**这个函数里 SSA + `phi` 是如何表达「一个在循环中不断更新的变量 `s`」的？**（提示：`s` 在 IR 里不是被反复赋值，而是每个循环层级产生一个新的 `%s.0`、`%s.1`……由 `phi` 在汇合点选择。）

这个任务同时覆盖了「模块头/函数签名」（4.1）与「基本块/SSA/phi」（4.2）两个模块，并让你直面 SSA 最经典的用例——循环变量。

---

## 6. 本讲小结

- `.ll` 文本是内存 `Module` 的可读序列化形式，结构严格分层：**模块头 → 全局声明 → 函数（签名 + 基本块 + 指令）**。
- 模块头里的 `target triple`/`datalayout` 是有语义的；`@` 前缀是全局符号、`%` 前缀是局部符号，每个操作数都**显式带类型**。
- AsmWriter（`printModule`/`printFunction`/`printBasicBlock`/`printInstruction`）负责「写」，LLParser（`parseTopLevelEntities`/`parseType`/`parseBasicBlock`/`parseInstruction`）负责「读」，两者互为镜像，是 `.ll` 语法的权威定义。
- **基本块**有标签、顺序指令、恰好一个终结指令；**SSA** 要求每个 `%` 值只被定义一次。
- **`phi`** 在控制流汇合点按「来源块」选值，是 SSA 表达「分支/循环变量」的标准手段，且必须是块内第一条指令。
- 寄存器命名 `%名字` 与 `%数字` 语义等价；词法层（`LocalVar` vs `LocalVarID`）和解析层（`getVal(Name)` vs `getVal(ID)` + `setInstName`）分别处理两种形式，编号按「定义顺序」分配（参数在前、指令在后）。

---

## 7. 下一步学习建议

本讲让你能「读」和「改」`.ll` 文本，但还没碰 IR 的**内存对象模型**。建议下一讲进入：

- **u3-l1（Module / Function / BasicBlock：IR 的层次结构）**：把本讲看到的文本结构对应到内存中的 C++ 类，学会用代码遍历 `Module → Function → BasicBlock → Instruction`。
- **u3-l2（Value / User / Use：SSA 与 def-use 链）**：把本讲的 SSA 命名上升到「定义-使用链」数据结构，理解优化器如何借此分析数据流。
- 若你想先看「IR 从哪里来」，可跳到 **u5-l5（CodeGen：从 AST 到 LLVM IR）**，看 Clang 如何一边遍历 AST 一边生成你今天读到的这些指令。

在进入下一篇之前，建议你把 `loop_sum.ll` 这份 `.ll` 保留好——它会在 u3 系列里作为遍历练习的素材反复出现。
