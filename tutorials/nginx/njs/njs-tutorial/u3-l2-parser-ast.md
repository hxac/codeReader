# 递归下降解析器与 AST

> 本讲对应大纲 `u3-l2`，承接 [u3-l1 词法分析器 njs_lexer](u3-l1-lexer.md)。上一讲我们让源码变成了 token 流；本讲要解决的问题是：**这些 token 是如何被组织成一棵语法树（AST）的？** 这棵树又是如何为下一站的「字节码生成器」做好准备的。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 njs 解析器「递归下降 + 显式栈」的工作方式，并能解释为什么它不直接用 C 的函数递归。
2. 描述 AST 节点 `njs_parser_node_t` 的每一个关键字段（`token_type`、`u` 联合、`left/right/dest`、`scope`、`index`）。
3. 看懂一条表达式（如 `a + b * c`）在解析过程中是如何一步步被组装成一棵二叉树的，并能指出「运算优先级」是如何体现在树的形状上的。
4. 理解节点的 `index` 字段是把 AST 和「字节码 / 运行期存储」连接起来的桥梁，并会按位把它拆开来看。

本讲只聚焦**解析器本身**：它消费 token、产出 AST。涉及到的字节码生成（`njs_generator`）和执行（解释器）只作为「下游消费者」被点到，详细讲解留给后续单元。

---

## 2. 前置知识

在进入源码之前，先用最直白的方式建立几个概念。

### 2.1 什么是「解析（parsing）」

词法分析把源码切成一个个 token（`a`、`+`、`b`、`*`、`c`）。但 token 序列本身是「扁平」的，它没有告诉我们 `a + b * c` 到底是 `(a + b) * c` 还是 `a + (b * c)`。

**解析器**的任务就是：根据语法规则，把扁平的 token 序列组织成一棵**有结构的树**——抽象语法树（AST，Abstract Syntax Tree）。这棵树天然地表达了运算的优先级和结合方式。

### 2.2 什么是「递归下降」

JavaScript 的语法（ECMAScript 规范）是用一组**产生式（production rule）**描述的，比如：

```
AdditiveExpression :
    MultiplicativeExpression
    AdditiveExpression + MultiplicativeExpression
    AdditiveExpression - MultiplicativeExpression
```

这条规则的意思是：「一个加法表达式，要么本身就是一个乘法表达式，要么是『左边的加法表达式』加上/减去『右边的乘法表达式』」。

「递归下降（recursive descent）」是一种最直观的解析策略：**为每一条语法规则写一个函数**。`解析加法表达式()` 的函数内部会去调用 `解析乘法表达式()`，而乘法又会调用更低一层的规则……规则之间相互嵌套，函数也就相互嵌套（递归下降）。

> 小提示：ECMAScript 规范里有一长串「优先级阶梯」（ladder）：`表达式 → 赋值 → 条件 → … → 加法 → 乘法 → 指数 → 一元 → … → 基础表达式`。**越靠下的规则优先级越高（绑得越紧）**。这条阶梯在 njs 源码里几乎是一一对应的。

### 2.3 njs 为什么用「状态机 + 显式栈」而不是直接递归

理论上 `解析加法()` 直接 `return 解析乘法()` 就行了。但 njs 没有这样做——它把每一个「解析函数」改写成一个**状态函数（state function）**，并用一个**显式的栈**来模拟函数调用与返回。

这样做的好处是：解析过程不会真正消耗 C 的调用栈。对于层层嵌套的源码（比如一百层括号、超长表达式），直接递归可能撑爆 C 栈；而显式栈把「我解析完这一半之后要回到哪一步」这种「续体（continuation）」存在堆上，深度只受内存池限制。

> 如果你暂时觉得「状态机 + 栈」抽象，可以先把它理解为：**原来的「函数调用」变成了「把下一个要执行的函数地址压栈」，原来的「函数返回」变成了「从栈里弹出一个函数地址继续执行」**。本质完全一样，只是把调用栈从 C 栈搬到了 njs 自己管理的队列里。

---

## 3. 本讲源码地图

本讲主要涉及两个文件：

| 文件 | 作用 |
|---|---|
| `src/njs_parser.h` | 解析器的数据结构定义：AST 节点 `njs_parser_node_t`、解析器本体 `njs_parser_t`、作用域 `njs_parser_scope_t`，以及一堆驱动状态机的内联函数（`njs_parser_next` / `njs_parser_after` / `njs_parser_stack_pop`）。 |
| `src/njs_parser.c` | 解析器的全部实现：入口 `njs_parser_init` / `njs_parser`、主循环、以及按优先级阶梯排列的成百上千个状态函数（`njs_parser_additive_expression` 等）。 |

辅助理解「index 桥梁」的两个文件（本讲只引用其中关键函数）：

| 文件 | 作用 |
|---|---|
| `src/njs_scope.h` | 定义 `index` 的位编码与解码（`njs_scope_index` / `njs_scope_value`），是把 AST 节点连到运行期存储的「翻译表」。 |
| `src/njs_variable.c` | `njs_variable_resolve`：从 NAME 节点出发，沿作用域链查出变量，从而拿到它的 `index`。 |

> 这两个辅助文件属于「作用域与变量」主题，是 [u3-l3 变量声明与作用域](u3-l3-variable-and-scope.md) 的主战场，本讲只借用其中与 `index` 直接相关的部分。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 递归下降解析**：解析器的驱动主循环与「状态函数 + 显式栈」机制。
- **4.2 AST 节点结构**：`njs_parser_node_t` 的字段，以及表达式、语句是如何被组装进树的。
- **4.3 节点与索引**：节点的 `index` 字段如何编码运行期存储位置，成为连接字节码的桥梁。

---

### 4.1 递归下降解析

#### 4.1.1 概念说明

njs 的解析器是一个「**状态机驱动的递归下降解析器**」。它的核心思想可以浓缩成三句话：

1. **每个语法规则 = 一个状态函数**，签名统一为 `njs_parser_state_func_t`。
2. **「往下走」用 `njs_parser_next`**：把下一个要执行的状态记到 `parser->state`。
3. **「调用子规则后再回来」用 `njs_parser_after`**：把「回来后要执行的状态」压进显式栈；**「回来」用 `njs_parser_stack_pop`**：从栈里弹出一个状态继续执行。

主循环非常简单：取一个 token，调用当前状态函数，根据返回值决定是否继续。

#### 4.1.2 核心流程

整个解析过程可以用下面的伪代码概括：

```
njs_parser(vm, parser):
    建立（或复用）顶层作用域
    初始化空栈 parser->stack
    设定初始状态：parser->state = njs_parser_statement_list
    把「出错检查态」压栈：after(njs_parser_check_error_state)

    循环：
        token = 从词法器取一个 token
        ret = parser->state(parser, token, 栈顶)
    直到 ret == DONE 或 ret == ERROR

    把最终节点标记为 NJS_TOKEN_END，挂到作用域的 top 指针
```

每个状态函数在做的事情无外乎这几类：

- **消费 token、构造节点**：例如遇到名字 `a`，造一个 `NJS_TOKEN_NAME` 节点，赋给 `parser->node`，吃掉这个 token，返回 `NJS_DONE` 表示「这一小段我处理完了」。
- **`njs_parser_next(下一状态)`**：表示「我现在先去执行下一状态（通常是更细的子规则）」。
- **`njs_parser_after(链接, 节点, 是否可选, 回来后的状态)`**：把一个「续体」压栈。`节点` 这个参数会被存起来，等回来时通过 `parser->target` 暴露给「回来后的状态」——这是父子规则之间传递半成品 AST 节点的通道。
- **`njs_parser_stack_pop`**：弹出一个续体，把它的状态设为新的 `parser->state`，把它的节点设为 `parser->target`。等价于「函数返回」。

#### 4.1.3 源码精读

**主循环入口**：[`src/njs_parser.c:581-658`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L581-L658) 中的 `njs_parser`。注意第 608 行设置初始状态为 `njs_parser_statement_list`（从「语句列表」开始解析），第 616–625 行就是上文说的主循环：取 token、调状态函数、直到 `DONE` 或 `ERROR`。

```c
/* src/njs_parser.c:616-625 核心主循环 */
do {
    token = njs_lexer_token(parser->lexer, 0);
    if (njs_slow_path(token == NULL)) {
        return NJS_ERROR;
    }

    parser->ret = parser->state(parser, token,
                                njs_queue_first(&parser->stack));

} while (parser->ret != NJS_DONE && parser->ret != NJS_ERROR);
```

**初始化**：[`src/njs_parser.c:563-578`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L563-L578) 的 `njs_parser_init` 把解析器清零，挂上作用域，并初始化词法器。

**三个驱动函数**（都在 `njs_parser.h`，是内联函数 / 宏）：

- [`njs_parser_next`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.h#L240-L244)：仅把 `parser->state` 设为新状态——「往下一层走」。
- [`_njs_parser_after`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.h#L329-L348)：分配一个栈条目，填入「回来后的状态」「节点」「是否可选」，插到栈里指定链接之前——「预约一个续体」。
- [`njs_parser_stack_pop`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.h#L284-L308)：弹出栈顶条目，把它的状态设为 `parser->state`、把它的节点设为 `parser->target`，再释放条目——「返回到调用者」。

栈条目本身的结构见 [`njs_parser_stack_entry_t`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.h#L91-L98)，只有四个字段：`state`（回来后执行的函数）、`link`（队列链接）、`node`（父子间传递的半成品节点）、`optional`（标记）。

**优先级阶梯的典型一环**：以「加法」为例。[`src/njs_parser.c:4043-4096`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L4043-L4096) 是加法的两个状态函数，这是理解整个解析器工作方式的关键：

```c
/* src/njs_parser.c:4043-4051 入口：先下降到乘法（更高优先级），
 * 约定乘法处理完后回到 additive_expression_match */
static njs_int_t
njs_parser_additive_expression(parser, token, current)
{
    njs_parser_next(parser, njs_parser_multiplicative_expression);
    return njs_parser_after(parser, current, NULL, 1,
                            njs_parser_additive_expression_match);
}
```

```c
/* src/njs_parser.c:4054-4096 「回来后的状态」：决定要不要消费 + / - */
static njs_int_t
njs_parser_additive_expression_match(parser, token, current)
{
    /* 如果上一轮约定了一个父节点（target），就把刚算完的右子树挂上去 */
    if (parser->target != NULL) {
        parser->target->right = parser->node;
        parser->target->right->dest = parser->target;
        parser->node = parser->target;
    }

    switch (token->type) {
    case NJS_TOKEN_ADDITION:   operation = NJS_VMCODE_ADDITION;   break;
    case NJS_TOKEN_SUBTRACTION: operation = NJS_VMCODE_SUBTRACTION; break;
    default: return njs_parser_stack_pop(parser);  /* 不是 +/-，交还给上层 */
    }

    node = njs_parser_node_new(parser, token->type);
    node->u.operation = operation;
    node->left = parser->node;       /* 左操作数 = 已经算出的子树 */
    node->left->dest = node;         /* 记住：左孩子的归宿是这个父节点 */

    njs_lexer_consume_token(parser->lexer, 1);           /* 吃掉 + 或 - */
    njs_parser_next(parser, njs_parser_multiplicative_expression); /* 去算右操作数 */
    return njs_parser_after(parser, current, node, 1,
                            njs_parser_additive_expression_match);  /* 再回来看是否还有 +/- */
}
```

把这两个函数读通，你就掌握了 njs 解析器的全部「语法」：

- **下降**：`additive` → `multiplicative` → `exponentiation` → … → `primary`，每一层都遵循同样的「入口下降 + 回来匹配」二段式。
- **优先级**：越靠近 `primary`（叶子）的层，优先级越高，因为它「在更里面被先组装」。
- **左结合**：`a + b + c` 会被组装成左倾的树 `((a+b)+c)`，靠的就是「匹配到 `+` 就造一个新父节点、把旧的挂成左孩子、再 `after` 自己」这种循环式续体。

整条阶梯在 `njs_parser.c` 里依次出现：`expression`（逗号）→ `assignment` → `conditional` → 逻辑/位/相等/关系/移位 → [`additive`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L4043-L4096) → [`multiplicative`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L3981-L4037) → exponentiation → … → `primary`。每个二元层都是「入口函数 + `_match` 函数」一对，写法几乎一模一样。

#### 4.1.4 代码实践

**实践目标**：用反汇编确认优先级阶梯的真实存在。

**操作步骤**：

1. 先确保已按 [u3-l3](u3-l3-build-and-run-cli.md) 构建出 `build/njs`。
2. 运行（注意是反汇编，不是 AST，但它能反映求值顺序）：

   ```sh
   ./build/njs -d -c 'a + b * c'
   ```

3. 再运行一个对照：

   ```sh
   ./build/njs -d -c '(a + b) * c'
   ```

**需要观察的现象**：两组输出里，`MULTIPLICATION`（乘法）与 `ADDITION`（加法）指令出现的先后顺序不同。第一组里乘法在前（因为它在树里更深、被先求值），第二组里加法在前。

**预期结果**：反汇编的指令顺序恰好对应「后序遍历 AST」的求值顺序——树根（最后做的运算）对应的指令排在最后输出。这从侧面印证了 4.1.3 里讲的优先级阶梯。

> 说明：`-d` 给出的是**字节码**（下一讲 [u3-l4 生成器](u3-l4-generator.md)、[u3-l5 字节码](u3-l5-bytecode-and-disassembler.md) 的主题），不是 AST 本身。本实践只是借它的求值顺序来「反推」AST 的形状。njs 源码里有一个 `njs_parser_serialize_ast`（声明见 [`njs_parser.h:147`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.h#L147)）可把 AST 序列化输出，但 CLI 默认不暴露这个开关，所以这里用字节码代替。

#### 4.1.5 小练习与答案

**练习 1**：`njs_parser_additive_expression_match` 里，`if (parser->target != NULL) { … }` 这段在什么时候会真正执行？（提示：想想 `a + b + c`。）

> **答案**：在「第二次及以后回到 `_match`」时执行。第一次进入 `_match` 时，栈条目里存的 `node` 是 `NULL`（见 `njs_parser_additive_expression` 里 `after(..., NULL, ...)`），所以 `target` 为 `NULL`、跳过这段。当解析完右操作数再次回到 `_match` 时，`target` 是上一轮造出的父加法节点（非 `NULL`），这段就把新算出的右子树挂到父节点的 `right` 上——这正是实现 `a + b + c` 左结合的关键。

**练习 2**：为什么 `njs_parser_additive_expression` 一进去就先 `njs_parser_next(multiplicative)`？为什么不是先看当前 token 是不是 `+`？

> **答案**：因为加法的左操作数本身可能是一个乘法表达式（优先级更高）。规则 `AdditiveExpression → MultiplicativeExpression` 说明：即便后面没有 `+`，加法层也必须先下降到乘法层把左操作数完整地解析出来。先下降、再在「回来后的 `_match`」里看是否有 `+`，是递归下降表达优先级的标准写法。

**练习 3**：`njs_parser_stack_pop` 和普通函数的 `return` 有什么对应关系？

> **答案**：`njs_parser_stack_pop` 弹出栈顶条目，把条目里的 `state` 设为新的 `parser->state`（相当于「返回到调用点之后继续执行」），把条目里的 `node` 暴露成 `parser->target`（相当于「把子调用的结果交还给调用者」）。它就是把「函数返回」这件事用显式栈重新实现了一遍。

---

### 4.2 AST 节点结构

#### 4.2.1 概念说明

解析器一切工作的产物，都是 `njs_parser_node_t` 节点。可以把它想象成树上的一个「格子」，每个格子携带：

- **它是什么**（`token_type`）：是名字、数字、加法运算，还是语句、函数？
- **它的附加数据**（`u` 联合）：运算节点存「要执行哪条字节码」；常量节点存「字面值」；名字节点存「变量引用」。
- **它的孩子**（`left` / `right`）：把子表达式挂上来，形成树。
- **它的归宿**（`dest`）：一个指向「消费本节点结果的父节点」的反向指针。
- **它属于哪个作用域**（`scope`）、**它运行期存在哪里**（`index`）。

每个节点都从内存池里分配，建好后挂到 `parser->node`，再由父规则取走、塞进自己的 `left/right`。

#### 4.2.2 核心流程

不同类型的节点，组装方式不同：

- **二元运算节点**（加、乘、比较…）：4.1.3 里已看到，`_match` 函数 `njs_parser_node_new` 造一个新节点，`node->u.operation = NJS_VMCODE_*`，左孩子 = 已解析的子树，右孩子在下一轮回来时挂上。
- **基础表达式节点**（名字、数字、字符串）：`njs_parser_primary_expression_test` 遇到对应 token 直接造叶子节点。名字走 `njs_parser_reference`，并在 `u.value.atom_id` 里记下它的「原子 id」（见 [u2-l4 Atom 表](u2-l4-atom-table.md)）。
- **语句节点**（`NJS_TOKEN_STATEMENT`）：语句不是一棵二叉树，而是一条**左倾链表**。每解析完一条语句，就用一个 `STATEMENT` 节点把「上一条」放 `left`、「这一条」放 `right`，最终整段程序是一条向左生长的链，链头就是顶层 `top` 节点。

#### 4.2.3 源码精读

**节点结构本体**：[`src/njs_parser.h:32-63`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.h#L32-L63)。逐字段解读：

```c
struct njs_parser_node_s {
    njs_token_type_t  token_type:16;   /* 这是什么节点：NAME / ADDITION / STATEMENT … */
    uint8_t           ctor:1;          /* 是否作为构造器调用（new） */
    uint8_t           hoist:1;         /* 是否需要提升到作用域顶部（如函数声明） */
    uint8_t           temporary;       /* 是否是临时值 */
    uint32_t          token_line;      /* 源码行号，用于报错定位 */

    union {                            /* 附加数据，按节点类型复用同一块内存 */
        uint32_t                 length;
        njs_variable_reference_t reference;   /* 名字节点：变量引用 */
        njs_value_t              value;       /* 常量节点：字面值 */
        njs_vmcode_t             operation;   /* 运算节点：要执行哪条字节码 */
        njs_parser_node_t       *object;      /* 对象值节点：指向宿主对象 */
        njs_mod_t               *module;      /* 模块节点 */
    } u;

    njs_str_t         name;            /* 文本（如变量名、关键字字面量） */
    njs_index_t       index;           /* 运行期存储位置——见 4.3 */

    njs_parser_scope_t *scope;         /* 本节点所属作用域（含义见下方注释） */

    njs_parser_node_t *left;           /* 左孩子 */
    njs_parser_node_t *right;          /* 右孩子 */
    njs_parser_node_t *dest;           /* 反向指针：消费本节点结果的父节点 */
};
```

其中 `scope` 字段的精确含义，源码注释 [`njs_parser.h:52-57`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.h#L52-L57) 说得很清楚：

- 全局/函数节点 → 指向对应的全局或函数作用域；
- 变量节点 → 指向「引用该变量时所在的作用域」；
- 运算节点 → 指向「用来给临时值分配 index 的作用域」（这条直接关系到 4.3）。

**节点的诞生**：[`njs_parser_node_new`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.h#L175-L188) 从内存池 `njs_mp_zalloc` 分配并清零，然后只设两个字段——`token_type` 和 `scope`（取当前 `parser->scope`）。其余字段由调用者按节点类型补充。

**基础表达式（叶子）**：[`src/njs_parser.c:977-984`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L977-L984) 的 `switch` 里，`NJS_TOKEN_NAME` 等标识符走到 `reference` 标签 [`njs_parser.c:1220-1236`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L1220-L1236)，调用 [`njs_parser_reference`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L8642-L8756) 造一个名字节点，并在第 1227 行把 token 的 `atom_id` 写进 `node->u.value.atom_id`，最后吃掉 token、返回 `NJS_DONE`。

**语句链表**：[`njs_parser_statement_after`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L5031-L5080) 负责把一条条语句串起来。核心是第 5060–5069 行：每来一条新语句，就 `njs_parser_node_new(NJS_TOKEN_STATEMENT)`，把上一条放 `left`、这一条放 `right`，再更新顶层 `top` 指针（`top` 由宏 [`njs_parser_chain_top`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L515-L520) 读写，本质是 `parser->scope->top`）。

```c
/* src/njs_parser.c:5060-5069 把新语句挂到语句链上 */
stmt = njs_parser_node_new(parser, NJS_TOKEN_STATEMENT);
stmt->hoist = new_node->hoist;
stmt->left  = last;          /* 上一条语句 */
stmt->right = new_node;      /* 本条语句 */
*child = stmt;               /* 链接到链表里 */
```

> **判断 lvalue 的小工具**：[`njs_parser_is_lvalue`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.h#L154-L157) 宏通过 `token_type` 判断一个节点能不能放在赋值号左边（名字、属性、属性引用才行）。这能帮助你建立「`token_type` 决定节点语义」的直觉。

#### 4.2.4 代码实践

**实践目标**：手工画出 `a + b * c` 的 AST，标注每个节点的关键字段。

**操作步骤**（纯源码阅读，结合 4.1.3 的执行轨迹）：

1. 从顶层 `njs_parser_additive_expression` 出发，按 4.1.3 描述的下降与回溯过程，记录每一步「谁成了谁的 `left` / `right`」。
2. 为每个节点写出：`token_type`、`u.operation`（运算节点）或 `u.value.atom_id`（名字节点）、`left`、`right`、`dest`。

**需要观察的现象 / 预期结果**：最终树形如下（`→` 表示 `dest` 反向指针）：

```
ADDITION                         token_type=NJS_TOKEN_ADDITION
  u.operation = NJS_VMCODE_ADDITION
  ├─ left:  NAME(a)              token_type=NJS_TOKEN_NAME, atom_id=atom("a")
  │         dest ────────────────→ ADDITION
  └─ right: MULTIPLICATION       token_type=NJS_TOKEN_MULTIPLICATION
            u.operation = NJS_VMCODE_MULTIPLICATION
            dest ───────────────→ ADDITION
            ├─ left:  NAME(b)    atom_id=atom("b"),  dest → MULTIPLICATION
            └─ right: NAME(c)    atom_id=atom("c"),  dest → MULTIPLICATION
```

**优先级在树形上的体现**：`*` 的节点比 `+` 的节点**更深**（更靠近叶子），所以乘法会被先求值；`+` 在**树根**，最后求值。整棵树表达的就是 `a + (b * c)`。这正是「优先级高的运算符在 AST 中位置更深」这一普遍规律的直接体现。

#### 4.2.5 小练习与答案

**练习 1**：节点里的 `u` 为什么是 `union`（联合体）而不是把 `value`、`operation`、`reference` 都做成独立字段？

> **答案**：因为一个节点在同一时刻只可能是「常量」「运算」「名字引用」等某一种，永远不会同时既是字面值又是运算。用 `union` 让这些互斥的数据复用同一块 16 字节左右的内存，能显著减小每个节点的体积——而一棵 AST 动辄成千上万个节点，省下来的内存非常可观。

**练习 2**：`dest` 这个反向指针是干什么用的？从 4.2.3 的源码里找一个证据。

> **答案**：`dest` 是「孩子 → 父」的反向指针，记录「我这个节点的计算结果会被谁消费」。证据在加法 `_match` 里：`node->left = parser->node; node->left->dest = node;`（左孩子指向新造的父加法节点）。它主要服务于后续的字节码生成——生成器据此把临时值的 `index` 在父子节点之间贯通。

**练习 3**：一段有多条语句的程序（如 `a; b; c;`），AST 是「一棵平衡的二叉树」吗？

> **答案**：不是。语句被组装成一条**左倾链表**：每条新语句都新建一个 `NJS_TOKEN_STATEMENT` 节点，把上一条放 `left`、这一条放 `right`，所以 `c; b; a;` 之类会退化成一条一直向左延伸的链（深度等于语句数），而不是平衡树。

---

### 4.3 节点与索引

#### 4.3.1 概念说明

AST 是「编译期」的产物，但代码最终要在「运行期」执行。`njs_parser_node_t` 的 `index` 字段，就是**把编译期节点和运行期存储位置缝在一起的那根线**。

直觉地说：每一个「在运行时需要一个值槽位」的节点（一个变量、一个运算的中间结果、一个函数实参），都会拿到一个 `index`。这个 `index` 不是指针，而是一个**位编码的整数**，运行期的解释器拿到它就能直接定位到「这个值存在哪」。

这一点也呼应了源码注释 [`njs_parser.h:52-57`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.h#L52-L57)：运算节点的 `scope` 就是「用来给临时值分配 index 的作用域」。

#### 4.3.2 核心流程

`index` 是一个 32 位整数，被切成三段（低位在右）：

| 段 | 位宽 | 含义 |
|---|---|---|
| `var_type`（低位） | 4 bit | 变量种类（var / let / const 等） |
| `type`（中位） | 4 bit | 存储层级：`LOCAL` / `CLOSURE` / `GLOBAL` / `STATIC` |
| `value`（高位） | 24 bit | 该层级内的槽位编号 |

打包公式为：

\[
\text{index} = (\text{value} \ll 8)\ \|\ (\text{type} \ll 4)\ \|\ \text{var\_type}
\]

（`NJS_SCOPE_VALUE_OFFSET = 8`、`NJS_SCOPE_TYPE_OFFSET = 4`。）

四种存储层级（见 [`src/njs_vm.h:110-114`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L110-L114)）：

| 层级 | 值 | 含义 |
|---|---|---|
| `NJS_LEVEL_LOCAL` | 0 | 当前函数的本地槽位（最常见） |
| `NJS_LEVEL_CLOSURE` | 1 | 闭包捕获的外层变量 |
| `NJS_LEVEL_GLOBAL` | 2 | 全局变量 |
| `NJS_LEVEL_STATIC` | 3 | 跨克隆共享的静态值 |

运行期，解释器用 [`njs_scope_value`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L78-L83) 把 `index` 解码成一个真实地址：

\[
\text{value 指针} = \text{vm->levels}[\,\text{type}\,][\,\text{value}\,]
\]

也就是说，`index` 高 4 位（type）选出「哪一层」，中间 24 位（value）选出「这一层的第几个槽」。这是下一单元 [u4-l2 作用域寻址](u4-l2-scope-levels-and-index.md) 的核心，本讲先建立「`index` 就是这个编码」的认知。

那么节点是怎么拿到 `index` 的？两条路径：

- **变量节点（NAME）**：通过 [`njs_variable_resolve`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L383-L407) 沿作用域链（每层作用域用红黑树存变量）查出对应的 `njs_variable_t`，变量自身的 `index` 字段就是它的存储槽位。
- **运算结果 / 实参节点**：调用 `njs_scope_temp_index(scope)` 在当前作用域新分配一个临时槽位（例如 [`njs_parser.c:3150`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L3150) 给实参节点分配 `index`）。

#### 4.3.3 源码精读

**位布局定义**：[`src/njs_scope.h:10-26`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L10-L26)，定义了三段的偏移、位宽与掩码，以及两个哨兵 `NJS_INDEX_NONE`（0，表示「还没有 index」）和 `NJS_INDEX_ERROR`（-1，分配失败）。

**打包函数 `njs_scope_index`**：[`src/njs_scope.h:36-53`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L36-L53)，就是上面公式的实现；注意第 47–49 行的小细节：在全局作用域里申请 `LOCAL` 层会被自动改成 `GLOBAL` 层。

```c
/* src/njs_scope.h:51-52 打包：value 在高位、type 居中、var_type 在低位 */
return (index << NJS_SCOPE_VALUE_OFFSET) | (type << NJS_SCOPE_TYPE_OFFSET)
        | var_type;
```

**解码函数 `njs_scope_value`**：[`src/njs_scope.h:78-83`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L78-L83)，运行期把 `index` 翻译成 `vm->levels[type][value]` 的真实值指针。配套的 `njs_scope_index_type` / `njs_scope_index_value`（[`njs_scope.h:63-75`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L63-L75)）是它的「半个」解码。

**NAME 节点查 index**：[`njs_variable_resolve`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L383-L407) 从 `node->scope` 出发，逐层在作用域红黑树里按 `atom_id` 找变量，找到就返回那个 `njs_variable_t`（其 `index` 字段即存储槽位），找不到返回 `NULL`（引用了未声明变量）。

**下游如何消费 index**：字节码生成器 `njs_generator` 读节点的 `index` 并把它写进指令的操作数。例如 [`src/njs_generator.c:1087-1089`](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L1087-L1089) 发射一条变量指令时，`variable->dst = node->index`——也就是说，AST 节点的 `index` 原封不动地变成了字节码指令里「目标槽位」操作数。

#### 4.3.4 代码实践

**实践目标**：手工拆解一个 `index`，验证位编码。

**操作步骤**（纸笔推演，待本地验证）：

1. 假设某局部变量被分配在 `LOCAL` 层（`type=0`）、槽位编号 `value=1`、变量种类 `var_type=0`。
2. 按公式算出它的 `index`：

\[
\text{index} = (1 \ll 8)\ \|\ (0 \ll 4)\ \|\ 0 = 0\text{x}100 = 256
\]

3. 反过来，给定 `index = 0x100`，验证解码：
   - `var_type = 0x100 \& 0xF = 0`
   - `type = (0x100 \gg 4) \& 0xF = 0`（LOCAL）
   - `value = 0x100 \gg 8 = 1`

**需要观察的现象 / 预期结果**：解码得到的 `(type=LOCAL, value=1)` 正好对应运行期的 `vm->levels[0][1]`，即「本地层第 2 个槽位」。这说明 `index` 完全自包含——解释器无需任何额外查表，仅凭位运算就能定位运行期值。

> 如果你想在真实构建里看到具体的 `index` 值，可以 `./build/njs -d -c 'var a = 1; var b = 2;'` 观察反汇编里 `MOVE` 等指令的操作数（那些十六进制数就是编码后的 `index`）。把它们的低 8 位和中 4 位拆开，就能看到层级与槽位。具体字节码指令的含义留到 [u3-l5 字节码与反汇编](u3-l5-bytecode-and-disassembler.md)。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `index` 不直接用一个指针，而要费力气做位编码？

> **答案**：两个原因。一是**紧凑**：32 位整数比 64 位指针省一半空间，AST 和字节码里到处都是 `index`。二是**自描述**：编码里同时携带了「哪一层（type）」「第几个槽（value）」「什么变量种类（var_type）」，解释器一次位运算就能定位，不用维护额外的映射表。这也让「编译期分配槽位 → 运行期直接寻址」这件事变得几乎零成本。

**练习 2**：一个 NAME 节点和一个加法运算节点，分别通过什么方式获得 `index`？

> **答案**：NAME 节点通过 `njs_variable_resolve` 沿作用域链查出它对应的变量，复用变量自身的 `index`（同一个变量被多次引用时共享同一个槽位）。加法这种运算节点的结果是一个「临时值」，通过 `njs_scope_temp_index(scope)` 在当前作用域新申请一个临时槽位作为 `index`。

**练习 3**：源码注释说「运算节点的 `scope` 是用来分配 index 的作用域」。结合本节内容，解释这句话。

> **答案**：运算会产生中间结果，需要一个临时槽位存放，这个槽位由 `njs_scope_temp_index` 在「某个作用域」里分配。运算节点的 `scope` 字段就指明「去哪个作用域分配」。于是「运算节点 → 它的 `scope` → 在该 scope 分配临时 `index`」三者串起来，解释了为什么运算节点要特意保存一个 `scope` 指针。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「**从 token 到 AST 到 index**」的完整追踪。

**任务**：针对表达式 `a + b * c`，写一份「解析纪要」，要求包含：

1. **解析路径**：列出从 `njs_parser_expression` 一路下降到 `primary` 的状态函数链（参考 4.1.3 的优先级阶梯），标注每一层是「入口函数」还是「`_match` 函数」。
2. **AST 结构图**：画出最终的节点树，标注每个节点的 `token_type` 与（运算节点的）`u.operation`、（名字节点的）`atom_id`，以及所有 `left/right/dest` 指向。
3. **优先级论证**：用「树的深度」论证为什么结果是 `a + (b * c)` 而不是 `(a + b) * c`。
4. **index 着色**：在你的图上，用不同颜色/标记区分「会通过变量解析拿到 index 的节点（NAME）」与「需要临时 index 的节点（运算结果）」，并写出各自获取 index 的函数名（`njs_variable_resolve` / `njs_scope_temp_index`）。
5. **下游衔接**：写出一句结论，说明生成器（`njs_generator`）会读取这棵树的哪些字段来产出字节码（提示：`u.operation` 决定指令、`index` 决定操作数、`left/right/dest` 决定操作数来源与去向）。

**验证方法**：

- 步骤 1–3 是纯源码推理，可对照 4.1.3、4.2.4 的内容自检。
- 若已构建 CLI，可运行 `./build/njs -d -c 'a + b * c'`，把反汇编里 `MULTIPLICATION` 出现在 `ADDITION` 之前这一现象，作为「乘法节点更深、先求值」的佐证。
- 完整的字节码指令解读留到 [u3-l5](u3-l5-bytecode-and-disassembler.md)；本实践只要求把「AST 形状」和「index 归属」讲清楚。

---

## 6. 本讲小结

- njs 解析器是**状态机驱动的递归下降解析器**：每条语法规则 = 一个状态函数，用显式栈（`njs_parser_after` 压续体、`njs_parser_stack_pop` 弹回）替代 C 的函数递归，主循环就是「取 token → 调当前状态函数」。
- 优先级靠一条**下降阶梯**实现：`expression → assignment → … → additive → multiplicative → … → primary`，每层都是「入口下降 + `_match` 回来消费本层运算符」的二段式；越靠近 `primary` 优先级越高。
- AST 节点 `njs_parser_node_t` 的核心字段：`token_type`（语义）、`u` 联合（按类型复用的附加数据）、`left/right`（孩子）、`dest`（指向消费本节点的父节点的反向指针）、`scope`、`index`。
- 二元运算组装成二叉树（左结合），多条语句则组装成一条**左倾的 `NJS_TOKEN_STATEMENT` 链表**，链头挂在 `parser->scope->top`。
- **运算优先级 = 树的深度**：`a + b * c` 里乘法节点更深、先被求值，加法在树根、最后求值。
- 节点的 `index` 是连接编译期 AST 与运行期存储的桥梁：一个 32 位整数，按 `value(24) | type(4) | var_type(4)` 位编码，运行期由 `njs_scope_value` 解码成 `vm->levels[type][value]`；NAME 节点经 `njs_variable_resolve` 取得变量槽位，运算结果经 `njs_scope_temp_index` 取得临时槽位，最终被生成器写进字节码操作数。

---

## 7. 下一步学习建议

本讲产出的是一棵带 `index` 的 AST。接下来：

1. **[u3-l3 变量声明与作用域](u3-l3-variable-and-scope.md)**：深入 `njs_variable_t` 与 `njs_parser_scope_t`，搞清楚 `njs_variable_resolve` 沿红黑树查找、闭包变量如何被记录、`let/const` 与 `var` 在作用域上的差异——这是 4.3 里「NAME 节点如何拿到 index」的完整背景。
2. **[u3-l4 字节码生成器 njs_generator](u3-l4-generator.md)**：看生成器如何遍历本讲产出的 AST、读取 `u.operation` 与 `index`、把树「拍平」成线性的字节码指令序列。
3. **[u3-l5 字节码格式与反汇编](u3-l5-bytecode-and-disassembler.md)**：读懂 `-d` 输出里那些 `MOVE`/`ADDITION`/`STOP` 指令及其十六进制操作数（就是本讲的 `index`），从而把「AST → 字节码」这条链彻底打通。
4. 进阶可跳到 **[u4-l2 levels/scope/index：作用域寻址](u4-l2-scope-levels-and-index.md)**，看运行期解释器如何用 `vm->levels` 数组消费这些 `index`。
