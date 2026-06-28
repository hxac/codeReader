# 变量声明与作用域

## 1. 本讲目标

本讲承接上一讲「递归下降解析器与 AST」。你已经知道解析器会把源码组织成 `njs_parser_node_t` 构成的语法树，并且每个节点的 `index` 字段是连接「编译期 AST」与「运行期存储位置」的桥梁。本讲就来回答：**这个 `index` 到底是怎么算出来的？`var`/`let`/`const` 在源码层面写法不同，引擎内部又如何区分它们所属的作用域？**

学完本讲你应该能够：

1. 说清 `njs_variable_t` 的结构，以及 `VAR`/`LET`/`CONST`/`FUNCTION`/`CATCH` 五种变量类型各自的语义。
2. 区分解析期的三类作用域 `NJS_SCOPE_GLOBAL`/`FUNCTION`/`BLOCK`，理解为何 `var` 是函数作用域而 `let`/`const` 是块级作用域。
3. 把一个 32 位 `index` 按 `value(24) | type(4) | var_type(4)` 拆开，说出它指向运行期的哪一块存储（local/closure/global/static）。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**作用域是「变量名 → 存储槽位」的字典。** JavaScript 里每出现一个变量声明，编译器都要决定「这个名字归属哪个作用域」「它在运行期放在哪个槽位」。njs 在**解析期**就把这两件事一次性算好：用一棵红黑树登记变量名到 `njs_variable_t`，用一个 32 位整数 `index` 指明运行期存储位置。

**`var` 与 `let`/`const` 的本质差别是「归属哪一层作用域」。** 标准里 `var` 声明会被「提升」到最近的函数/全局作用域，而 `let`/`const` 留在它们所在的块 `{ }` 里。njs 在解析期用一个简单规则实现这一点：声明 `var` 时，会沿父链向上找到最近的 `FUNCTION`/`GLOBAL` 作用域再登记；声明 `let`/`const` 时，就在当前块作用域登记。

**`index` 是一张「运行期地址」的编码。** njs 是寄存器式 VM，所有局部值都存在 `vm->levels[level][i]` 这样的二维数组里。`index` 把「属于哪一级存储（level）」和「在该级里的下标（i）」以及「变量类型（var_type）」打包进一个 32 位整数，直接写进字节码操作数。

> 提示：上一讲（u3-l2）已经介绍了 `njs_parser_node_t`、它的 `left/right/dest` 树形组织，以及 `index` 的位编码概貌。本讲把镜头拉近，专门讲 `index` 是「由谁、在何时、用什么规则」分配出来的。

## 3. 本讲源码地图

本讲聚焦四个文件，再加上 `njs_parser.h` / `njs_vm.h` 里的两个关键结构。

| 文件 | 作用 |
|---|---|
| [src/njs_variable.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.h) | 变量类型枚举、`njs_variable_t` 结构、变量引用结构、对外声明 |
| [src/njs_variable.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c) | 变量登记、作用域查找、闭包变量索引分配的核心实现 |
| [src/njs_scope.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h) | `index` 的位布局常量、`njs_scope_index` 编码与解码内联函数 |
| [src/njs_scope.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.c) | 临时索引、全局/静态常量索引的分配 |
| [src/njs_parser.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.h) `njs_parser_scope_s` | 解析期作用域结构（变量/标签/引用三棵红黑树） |
| [src/njs_vm.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h) | `njs_scope_t`、`njs_level_type_t` 两枚举与 `vm->levels[]` |

---

## 4. 核心概念与源码讲解

### 4.1 变量类型与结构

#### 4.1.1 概念说明

njs 内部用一个统一的 `njs_variable_t` 表示任意一种变量声明——无论是 `var x`、`let y`、`const z`、`function f(){}` 还是 `catch(e)`。区分它们的不是「不同的结构体」，而是结构体里一个 8 位字段 `type`，取值来自枚举 `njs_variable_type_t`。这样做的好处是：作用域查找、闭包分析、索引分配这一整套逻辑可以共用同一份代码，只在少数几个分支处根据 `type` 做不同处理。

#### 4.1.2 核心流程

变量在解析期经历的生命周期是：

1. **登记（declare）**：解析器遇到 `var x` 这类声明，调用 `njs_variable_add`，按变量类型决定它该挂到哪一层作用域，并把名字插入该作用域的变量红黑树。
2. **引用（reference）**：解析器遇到对 `x` 的使用，调用 `njs_parser_variable_reference` 在当前作用域的「引用红黑树」里记一笔。
3. **求索引（resolve）**：稍后由生成器触发 `njs_variable_reference`（注意是另一个函数），沿作用域父链找到变量真实声明处，并算出运行期 `index`。

枚举值的顺序不是任意的，下面会看到它被用来做「能否在初始化前访问」的判断。

#### 4.1.3 源码精读

变量类型枚举，[src/njs_variable.h:11-17](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.h#L11-L17) 定义了五种类型：

```c
typedef enum {
    NJS_VARIABLE_CONST = 0,
    NJS_VARIABLE_LET,
    NJS_VARIABLE_CATCH,     // catch(e) 里的 e
    NJS_VARIABLE_VAR,
    NJS_VARIABLE_FUNCTION,
} njs_variable_type_t;
```

注意 `CONST=0`、`LET=1` 排在最前。这个顺序在 TDZ（临时死区）检查里被复用：访问一个尚未初始化的 `const`/`let` 应当抛 `ReferenceError`，而 `var` 在初始化前是 `undefined`。

变量结构体，[src/njs_variable.h:20-35](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.h#L20-L35)：

```c
typedef struct {
    uintptr_t             atom_id;          // 变量名驻留成的 32 位原子 id

    njs_variable_type_t   type:8;           // 上面五种类型之一
    njs_bool_t            argument;         // 是否是函数形参
    njs_bool_t            arguments_object; // 是否对应 arguments 对象
    njs_bool_t            self;             // 函数自引用（递归）
    njs_bool_t            init;             // 是否已初始化
    njs_bool_t            closure;          // 是否被内层函数闭包捕获

    njs_parser_scope_t    *scope;           // 真正归属的作用域
    njs_parser_scope_t    *original;        // 声明时所在的作用域

    njs_index_t           index;            // 运行期存储地址（本讲主角）
    njs_value_t           value;            // 编译期已知的值（如常量初值）
} njs_variable_t;
```

理解 `scope` 与 `original` 的差别是理解「变量提升」的关键：声明写在某个块作用域里（`original` 是块），但 `var`/`function` 会被提升到外层函数作用域（`scope` 变成函数）。

变量在红黑树里以 `njs_variable_node_t` 包装，[src/njs_variable.h:54-58](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.h#L54-L58)，以 `atom_id` 为键：

```c
typedef struct {
    NJS_RBTREE_NODE  (node);
    uintptr_t        key;        // = atom_id
    njs_variable_t   *variable;
} njs_variable_node_t;
```

#### 4.1.4 代码实践

**实践目标**：把五种变量类型与具体的 JS 写法对上号。

1. 打开 [src/njs_variable.h:11-17](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.h#L11-L17)。
2. 为下面每段 JS 推测它会产生哪一种 `njs_variable_type_t`：

   ```js
   var a = 1;            // ?
   let b = 2;            // ?
   const c = 3;          // ?
   function f(){}        // ?
   try{}catch(e){}       // ?
   ```

3. **需要观察的现象**：`CONST=0` 与 `LET=1` 这两个最小值会被 [src/njs_scope.h:94](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L94) 的判断 `njs_scope_index_var(index) <= NJS_VARIABLE_LET` 复用，用于 TDZ 检查。
4. **预期结果**：`a→VAR`、`b→LET`、`c→CONST`、`f→FUNCTION`、`e→CATCH`。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 `NJS_VARIABLE_CONST` 和 `NJS_VARIABLE_LET` 排在枚举最前（值最小）？
**答案**：这样「是否受 TDZ 约束」可以用一个 `<= NJS_VARIABLE_LET` 的范围比较一次性判断，无需逐个列举，见 `njs_scope_valid_value` 的检查。

**练习 2**：`njs_variable_t` 里的 `scope` 和 `original` 何时会不同？
**答案**：当声明是 `var`/`function` 且写在块 `{ }` 内时，`original` 指向该块作用域，而 `scope` 指向被提升到的外层函数/全局作用域。

---

### 4.2 解析期作用域

#### 4.2.1 概念说明

解析期作用域 `njs_parser_scope_t` 是「变量名查找」的舞台。njs 把作用域分成三类，由枚举 `njs_scope_t` 区分：

- `NJS_SCOPE_GLOBAL`：全局作用域（脚本最外层，或模块作用域）。
- `NJS_SCOPE_FUNCTION`：函数作用域。
- `NJS_SCOPE_BLOCK`：块作用域（`{ }`、`for`、`if` 等引入）。

这恰好对应标准里的「全局/函数/块」三种词法环境。关键设计是：**作用域之间用 `parent` 指针串成一条父链**，变量查找就是沿这条链向上走；而 `var`/`function` 声明会在登记时被「拉」到最近的 `GLOBAL`/`FUNCTION` 节点上，从而实现提升。

每个作用域内部维护三棵红黑树：

| 红黑树 | 键 | 存什么 |
|---|---|---|
| `variables` | atom_id | 本作用域**声明**的变量 |
| `labels` | atom_id | 本作用域的 `label:` 标签 |
| `references` | atom_id | 本作用域**引用**（使用）过的名字 |

`references` 树是闭包分析的依据——它记录了「这个作用域用到了哪些外层名字」。

#### 4.2.2 核心流程

**作用域的创建与销毁**（解析器走递归下降时进出作用域）：

```
进入 function/for/{ }
  └─ njs_parser_scope_begin(type)
        ├─ 分配 njs_parser_scope_t
        ├─ 初始化 variables / labels / references 三棵红黑树
        ├─ scope->parent = parser->scope   （接上父链）
        ├─ parser->scope = scope           （成为当前作用域）
        ├─ 若 FUNCTION/GLOBAL 且需要：登记 this 到 index 0
        └─ scope->items = 1                （槽位计数从 1 起）
...解析函数体/块体...
退出
  └─ njs_parser_scope_end()
        └─ parser->scope = scope->parent   （回到外层）
```

**`var` 提升的实现**：登记一个 `var` 时，函数 `njs_variable_scope` 会沿 `parent` 链向上查找，只要遇到 `GLOBAL` 或 `FUNCTION` 就停下并把变量挂在那里——这就是「跳过中间的块作用域」。而 `let`/`const` 不做这个向上查找，直接登记在当前作用域。

#### 4.2.3 源码精读

作用域结构，[src/njs_parser.h:12-29](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.h#L12-L29)：

```c
struct njs_parser_scope_s {
    njs_parser_scope_t  *top;          // 本作用域 AST 的根节点
    njs_parser_scope_t  *parent;       // 父作用域（查找链）
    njs_rbtree_t         variables;    // 声明的变量
    njs_rbtree_t         labels;       // 标签
    njs_rbtree_t         references;   // 引用的名字（闭包依据）

    njs_arr_t           *closures;     // 闭包索引数组
    njs_arr_t           *declarations; // 函数声明表

    uint32_t             items;        // 已分配的槽位数（给 index 用）

    njs_scope_t          type:8;       // GLOBAL / FUNCTION / BLOCK
    uint8_t              arrow_function;
    uint8_t              dest_disable;
    uint8_t              async;
};
```

三类作用域的枚举，[src/njs_vm.h:22-26](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L22-L26)：

```c
typedef enum {
    NJS_SCOPE_GLOBAL = 0,
    NJS_SCOPE_FUNCTION,
    NJS_SCOPE_BLOCK
} njs_scope_t;
```

作用域创建函数，[src/njs_parser.c:685-723](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L685-L723) 负责建结构、初始化三棵树、接父链、登记 `this`、把 `items` 置为 1：

```c
scope->type = type;
njs_rbtree_init(&scope->variables, njs_parser_scope_rbtree_compare);
njs_rbtree_init(&scope->labels,    njs_parser_scope_rbtree_compare);
njs_rbtree_init(&scope->references,njs_parser_scope_rbtree_compare);
parent = parser->scope;
scope->parent = parent;
parser->scope = scope;
if (type == NJS_SCOPE_FUNCTION || type == NJS_SCOPE_GLOBAL) {
    if (init_this) { /* 把 this 登记到 index 0 */ }
}
scope->items = 1;
```

注意 `items = 1`：每个函数/全局作用域的第 0 号槽位预留给 `this`（当 `init_this` 为真），所以后续变量从 1 开始编号。

`var` 提升的核心在 `njs_variable_scope`，[src/njs_variable.c:110-145](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L110-L145)，关键是这段向上查找的循环会**停在 GLOBAL/FUNCTION**：

```c
do {
    node = njs_rbtree_find(&scope->variables, &var_node.node);
    if (node != NULL) { /* 找到已声明的同名变量 */ }
    if (scope->type == NJS_SCOPE_GLOBAL
        || scope->type == NJS_SCOPE_FUNCTION)
    {
        return scope;          // ← var/function 就停在这里
    }
    scope = scope->parent;
} while (scope != NULL);
```

而 `let`/`const` 的处理在 `njs_variable_scope_find`，[src/njs_variable.c:162-184](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L162-L184)：对 `CONST`/`LET` 分支，只要 `root != scope`（即同名声明在外层）就报 `"has already been declared"`，从而实现块级重复声明报错。

块作用域在解析器里的开启点，例如 [src/njs_parser.c:5168](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L5168)、[src/njs_parser.c:6729](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L6729) 都是 `njs_parser_scope_begin(parser, NJS_SCOPE_BLOCK, 0)`；函数作用域则在 [src/njs_parser.c:7406](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L7406)、[src/njs_parser.c:7720](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L7720) 等处以 `NJS_SCOPE_FUNCTION` 开启。

引用的登记在 `njs_parser_variable_reference`，[src/njs_parser.c:9476-9507](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_parser.c#L9476-L9507)：它在当前作用域的 `references` 树里插一个 `njs_parser_rbtree_node_t`（键为 atom_id，`index` 初值 `NJS_INDEX_NONE`），这就是闭包分析的「引用记录」。

#### 4.2.4 代码实践

**实践目标**：用源码确认 `let`/`const` 与 `var` 在作用域归属上的差异，并搞清闭包引用是如何被记录的。

1. 阅读 `njs_variable_scope` 的循环（[src/njs_variable.c:122-142](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L122-L142)），回答：下面代码里 `x` 登记到哪一层作用域？

   ```js
   function g() {
     if (true) {
       var x = 1;   // x 登记到 g 的函数作用域，还是 if 的块作用域？
     }
   }
   ```

2. 阅读闭包判别函数 `njs_variable_closure_test`，[src/njs_variable.c:362-379](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L362-L379)：它沿父链走，只要在「引用处作用域」到「变量声明处作用域」之间隔着一个 `FUNCTION` 作用域，就判定为闭包。
3. **需要观察的现象**：闭包变量最终会被记录在**每个中间函数作用域**的 `closures` 数组与 `references` 红黑树里（见下一模块 `njs_variable_closure`）。
4. **预期结果**：`var x` 被提升到 `g` 的函数作用域；闭包捕获会在每层中间函数里留一条 `references` 记录。
5. 若想运行验证，可构建 CLI 后用反汇编观察（待本地验证）：`./build/njs -d`，输入一个内层函数捕获外层 `let` 的例子，观察字节码里出现 `CLOSURE` 级别的操作数。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `njs_parser_scope_begin` 把 `items` 初始化为 1 而不是 0？
**答案**：因为函数/全局作用域的第 0 号槽位被 `this` 占用（`init_this` 时把 `this` 登记到 index 0），后续变量从槽位 1 开始。

**练习 2**：`references` 红黑树与 `variables` 红黑树分别记录什么？为什么闭包分析需要单独的 `references` 树？
**答案**：`variables` 记录「本作用域声明了谁」，`references` 记录「本作用域用到了谁」。闭包分析需要知道哪些外层变量被本函数捕获，正是靠遍历 `references` 树，所以必须单独维护。

---

### 4.3 索引分配

#### 4.3.1 概念说明

`index` 是 `njs_index_t`（32 位整数），它把「运行期存储位置」编码成一个数。回忆上一讲：运行期值存在 `vm->levels[level][i]` 这样的二维数组里。`index` 就是把这里的 `level` 和 `i` 以及「变量类型 var_type」打包：

```
 高 24 位            中 4 位        低 4 位
┌──────────────────┬──────────────┬──────────────┐
│   value (i)      │  type(level) │  var_type    │
└──────────────────┴──────────────┴──────────────┘
   24 bits            4 bits         4 bits
```

`type` 字段取值来自 `njs_level_type_t`，对应四类存储：

- `NJS_LEVEL_LOCAL(0)`：当前帧的局部变量。
- `NJS_LEVEL_CLOSURE(1)`：从父帧捕获的闭包变量。
- `NJS_LEVEL_GLOBAL(2)`：全局变量。
- `NJS_LEVEL_STATIC(3)`：编译期就确定的常量（如字面量 `42`），存在 `vm->scope_absolute`。

有了这套编码，解释器拿到一条指令的操作数 `index`，只需一次移位就能定位到 `vm->levels[type][value]`，无需任何查表。

#### 4.3.2 核心流程

`index` 的分配分四种来源：

| 来源 | 分配函数 | level | 何时调用 |
|---|---|---|---|
| 普通变量声明 | `njs_variable_scope_add` | LOCAL | 解析到 `var/let/const` 声明 |
| 临时值 | `njs_scope_temp_index` | LOCAL | 生成器为表达式中间结果分配 |
| 闭包变量 | `njs_variable_closure` | CLOSURE | 生成器发现跨函数引用时 |
| 编译期常量 | `njs_scope_global_index` | STATIC | 生成器遇到字面量 |

普通变量声明的 `index` 计算很直接：

\[ \text{index} = (\text{items} \ll 8)\ \|\ (\text{level} \ll 4)\ \|\ \text{var\_type} \]

其中 `items` 是该函数/全局作用域已分配的槽位数，分配后自增。一个特例：若变量声明在 `GLOBAL` 作用域且 level 是 `LOCAL`，会被改写成 `GLOBAL`（全局变量的局部存储就是全局存储），见 `njs_scope_index` 的特判。

#### 4.3.3 源码精读

位布局常量，[src/njs_scope.h:10-23](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L10-L23)：

```c
#define NJS_SCOPE_VAR_OFFSET    0
#define NJS_SCOPE_VAR_SIZE      4     // 低 4 位：var_type
#define NJS_SCOPE_TYPE_OFFSET   4
#define NJS_SCOPE_TYPE_SIZE     4     // 中 4 位：level
#define NJS_SCOPE_VALUE_OFFSET  8
#define NJS_SCOPE_VALUE_SIZE    24    // 高 24 位：value(i)
#define NJS_SCOPE_VALUE_MASK    ((1 << 24) - 1)
```

编码函数 `njs_scope_index`，[src/njs_scope.h:36-53](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L36-L53)，注意 GLOBAL+LOCAL 被改写成 GLOBAL 的特判：

```c
if (scope == NJS_SCOPE_GLOBAL && type == NJS_LEVEL_LOCAL) {
    type = NJS_LEVEL_GLOBAL;       // 全局变量即存于全局存储
}
return (index << NJS_SCOPE_VALUE_OFFSET) | (type << NJS_SCOPE_TYPE_OFFSET)
        | var_type;
```

三个解码函数，[src/njs_scope.h:56-75](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L56-L75)，运行期用它定位值：

```c
njs_variable_type_t njs_scope_index_var(njs_index_t index) {
    return index & NJS_SCOPE_VAR_MASK;            // 低 4 位
}
njs_level_type_t    njs_scope_index_type(njs_index_t index) {
    return (index >> NJS_SCOPE_TYPE_OFFSET) & NJS_SCOPE_TYPE_MASK; // 中 4 位
}
uint32_t            njs_scope_index_value(njs_index_t index) {
    return index >> NJS_SCOPE_VALUE_OFFSET;       // 高 24 位
}
```

定位值的内联函数 `njs_scope_value`，[src/njs_scope.h:78-83](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L78-L83)——一句即可说明 index 的全部意义：

```c
njs_inline njs_value_t * njs_scope_value(njs_vm_t *vm, njs_index_t index) {
    return vm->levels[njs_scope_index_type(index)]
                     [njs_scope_index_value(index)];
}
```

`vm->levels` 的定义，[src/njs_vm.h:124](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L124)：`njs_value_t **levels[NJS_LEVEL_MAX]`，即四类存储各一个指针数组。level 枚举见 [src/njs_vm.h:109-115](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L109-L115)。

变量声明分配 index，`njs_variable_scope_add`，[src/njs_variable.c:276-285](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L276-L285)，注意它从「函数作用域」取 `items`，而不是当前块作用域——这正是变量存储按函数对齐的体现：

```c
if (index == NJS_INDEX_NONE) {
    root = njs_function_scope(scope);          // 找到最近的 FUNCTION/GLOBAL
    var->index = njs_scope_index(root->type, root->items,
                                 NJS_LEVEL_LOCAL, type);
    root->items++;                             // 占一个槽位
}
```

临时值分配 `njs_scope_temp_index`，[src/njs_scope.c:15-25](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.c#L15-L25)，同样从函数作用域取 `items`：

```c
scope = njs_function_scope(scope);
return njs_scope_index(scope->type, scope->items++, NJS_LEVEL_LOCAL,
                       NJS_VARIABLE_VAR);
```

闭包索引分配 `njs_variable_closure`，[src/njs_variable.c:410-499](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L410-L499)：它在从引用处到声明处的每一层**函数**作用域里，新建一个 `NJS_LEVEL_CLOSURE` 的 index，追加进该作用域的 `closures` 数组（值为「上一层」的 index，形成链），并在 `references` 树里记一笔：

```c
index = njs_scope_index(scope->type, scope->closures->items,
                        NJS_LEVEL_CLOSURE, var->type);
idx = njs_arr_add(scope->closures);
*idx = prev_index;          // 指向上层存储位置，运行期据此串成闭包链
```

编译期常量分配 `njs_scope_global_index`，[src/njs_scope.c:57-95](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.c#L57-L95)：把字面量（如 `42`）放进 `vm->scope_absolute`，并令 `vm->levels[NJS_LEVEL_STATIC]` 指向它，返回一个 STATIC 级 index：

```c
vm->levels[NJS_LEVEL_STATIC] = vm->scope_absolute->start;
*retval = njs_scope_index(NJS_SCOPE_GLOBAL, index, NJS_LEVEL_STATIC,
                          NJS_VARIABLE_VAR);
```

**把 index 和反汇编对上号**：以 `var a = 42;` 为例，引擎生成的指令是 `MOVE 0123 0133`（见 [docs/agent/engine-dev.md:237](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L237)）。两个操作数解码如下：

| 操作数 | 原值 | var_type(低4) | level(中4) | value(高24) | 含义 |
|---|---|---|---|---|---|
| `0123` | 0x123 | 3 = VAR | 2 = GLOBAL | 1 | 全局变量 `a`（槽位 1） |
| `0133` | 0x133 | 3 = VAR | 3 = STATIC | 1 | 静态常量池槽位 1（即字面量 `42`） |

即 `MOVE a <- 42`。你可以亲手验算：`a` 的 index 由 `njs_scope_index(GLOBAL, items=1, LOCAL, VAR)` 得到，因为 GLOBAL+LOCAL 被改写成 GLOBAL，结果正是 `0x123`。

#### 4.3.4 代码实践

**实践目标**：亲手解码字节码操作数，把「源码—作用域—index—反汇编」四者串起来。

1. 阅读 [docs/agent/engine-dev.md:230-247](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L230-L247) 的字节码示例。
2. 解码 `MOVE 0123 0133` 与 `ADD 0203 0103 0233` 中的每个操作数（用「低 4 位 var_type、中 4 位 level、高 24 位 value」拆解）。
3. **需要观察的现象**：`a`、`v`（形参）、`1`（字面量）分别落在 GLOBAL、LOCAL、STATIC 三个不同 level。
4. **预期结果**：`0203` = LOCAL/CONST? 不——解码应为 level=0(LOCAL)、var_type=3(VAR)、value=2，即函数 `f` 的局部槽位 2（表达式中间结果）；`0233` = STATIC、value=2，即静态常量 `1`。`(待本地验证)`：具体 value 编号取决于 `this` 占 0 后的排列，建议构建后用 `./build/njs -d` 实跑确认。
5. 构建 CLI（参考上一讲 u1-l3）后运行 `./build/njs -d`，输入 `var a = 42; function f(v) { return v + 1 }`，把实际反汇编输出与上表对照。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `njs_variable_scope_add` 要从 `njs_function_scope(scope)` 而不是直接从 `scope` 取 `items`？
**答案**：因为变量可能声明在块作用域里，但运行期存储按「最近的函数/全局作用域」对齐（一个函数只有一个局部值数组），所以槽位计数器 `items` 必须挂在函数/全局作用域上。

**练习 2**：闭包变量的 `index` 为什么是 `NJS_LEVEL_CLOSURE` 而不是 `LOCAL`？
**答案**：闭包变量来自父帧，不属于当前帧的局部数组；用独立的 CLOSURE level 让解释器去 `vm->levels[CLOSURE]` 取值，而该数组在函数调用时由调用机制从父帧拷贝/链接过来。

**练习 3**：一个 index 最多能编码多少个不同槽位？为什么是 24 位？
**答案**：value 字段 24 位，故单个 level 最多 \(2^{24} = 16{,}777{,}216\) 个槽位。选 24 位是为了在 32 位整数里同时塞下 level(4) 与 var_type(4)，对真实函数的局部变量数量而言已绰绰有余。

---

## 5. 综合实践

把本讲三块内容（变量类型、作用域、索引分配）串成一个小任务：**跟踪一段包含 `var`、`let` 与闭包的代码，画出它的作用域树与 index 分配表。**

```js
var a = 1;             // 全局 VAR，提升到 GLOBAL
function f(b) {        // FUNCTION 作用域；b 是形参(argument)
  var c = a + b;       // var c 提升到 f；引用外层 a（闭包捕获）
  if (true) {
    let d = c * 2;     // let d 留在 BLOCK 作用域
  }
  return c;
}
```

请完成：

1. **画作用域树**：标注 GLOBAL、FUNCTION(f)、BLOCK(if) 三层，并标出每层 `variables` 树里的变量（`a`、`f`、`this`、`b`、`c`、`d`）分别归属哪层。
2. **判断闭包**：用 `njs_variable_closure_test`（[src/njs_variable.c:362-379](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L362-L379)）的规则，判断 `f` 内对 `a` 的引用是否构成闭包捕获，若是，指出会在哪些作用域的 `closures` 数组里留记录。
3. **填 index 表**：参考 `njs_variable_scope_add`（[src/njs_variable.c:276-285](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L276-L285)），为 `a`、`b`、`c`、`d` 各估一个 `(level, value, var_type)`（`this` 占 GLOBAL/FUNCTION 的槽位 0）。
4. **验证**：构建 CLI 后运行 `./build/njs -d`，输入上述代码，把反汇编里的操作数与你估的 index 表对照（`待本地验证`）。

参考要点：`a` 在 GLOBAL、var_type=VAR，level 经「GLOBAL+LOCAL→GLOBAL」改写为 GLOBAL；`d` 是 LET，受 TDZ 保护（见 `njs_scope_valid_value`，[src/njs_scope.h:86-104](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L86-L104)）；`a` 在 `f` 内被引用且中间隔着 FUNCTION 作用域，故 `f` 的 `references` 与 `closures` 都会有 `a` 的记录。

---

## 6. 本讲小结

- njs 用统一的 `njs_variable_t` 表示所有变量，靠 `type:8` 字段（`VAR/LET/CONST/FUNCTION/CATCH`）区分；`CONST=0`、`LET=1` 的排序被复用做 TDZ 判断。
- 解析期作用域 `njs_parser_scope_t` 分 `GLOBAL/FUNCTION/BLOCK` 三类，靠 `parent` 串成查找链，内部维护 `variables`/`labels`/`references` 三棵红黑树。
- `var`/`function` 声明在 `njs_variable_scope` 里被向上「提升」到最近的 GLOBAL/FUNCTION（函数作用域），`let`/`const` 留在当前块（块级作用域），重复声明由 `njs_variable_scope_find` 报错。
- 运行期存储位置编码进 32 位 `index`：`value(24) | level(4) | var_type(4)`，解释器用 `njs_scope_value` 一次解码即定位 `vm->levels[level][value]`。
- 槽位计数器 `items` 挂在函数/全局作用域上（`this` 占 0 号），变量与临时值都从这里取号；闭包变量另走 `NJS_LEVEL_CLOSURE`，编译期常量走 `NJS_LEVEL_STATIC`（`vm->scope_absolute`）。

## 7. 下一步学习建议

本讲讲清了「`index` 是怎么来的」。接下来：

- **u3-l4 字节码生成器 njs_generator**：看生成器如何在本讲建立的变量/索引基础上，为 AST 节点发射真正的字节码指令（包括如何调用 `njs_variable_reference` 触发闭包分配）。
- **u3-l5 字节码格式与反汇编**：把本讲「`index` 解码」的练习系统化，学会读 `-d` 的完整输出。
- **u4-l2 levels/scope/index：作用域寻址**：从执行引擎一侧看 `vm->levels[]` 在函数调用时如何被建立与切换，与本讲的「分配侧」形成闭环。

建议继续精读 [src/njs_variable.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c) 中 `njs_variable_closure`（[410-499 行](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L410-L499)）与 `njs_variable_reference`（[502-555 行](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L502-L555)），它们是连接「解析期作用域」与「生成期闭包」的关键桥梁。
