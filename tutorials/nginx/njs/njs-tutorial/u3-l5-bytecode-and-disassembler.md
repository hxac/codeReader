# 字节码格式与反汇编

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 njs 一条字节码指令的内部结构（操作码 + 若干操作数），并解释为什么它是「变长」的。
- 认识 `NJS_VMCODE_*` 操作码枚举，记住 `STOP / JUMP / RETURN / MOVE / ADDITION / PROPERTY_GET` 等核心指令的语义。
- 读懂 `src/njs_disassembler.c` 里 `code_names[]` 反汇编表的工作方式，以及 `njs_disassemble` 输出每一列的含义。
- 会用 `./build/njs -d` 反汇编一段脚本，并把形如 `0123`、`0133` 的十六进制操作数拆解成「存储层级 + 变量类型 + 槽位号」。

本讲是编译前端流水线（u3-l1～u3-l4）的收尾：上一讲 `njs_generator` 把 AST 翻译成了字节码，本讲带你「亲眼看看」这些字节码长什么样、怎么读；下一单元 u4 才进入解释器如何执行它们。

## 2. 前置知识

在动手之前，先建立两个直觉。

**第一，什么是字节码。** 源码是人写的，CPU 看不懂；直接把源码翻译成机器码又太重。njs 的做法和大多数脚本引擎一样：先用编译前端把源码翻译成一种「给虚拟机看的中间指令」，也就是**字节码（bytecode）**；再由一个**解释器（interpreter）**逐条读取、解释执行这些指令。本讲的主角就是「指令长什么样」。

**第二，njs 是「寄存器式」虚拟机。** 还有一类虚拟机叫「栈式」（操作数压栈、出栈），而 njs 走的是寄存器式：每条指令的操作数不是栈位置，而是一个**索引 `njs_index_t`**，直接指向运行期存储数组 `vm->levels[...][...]` 里的某个槽位。这一点至关重要——它决定了 njs 的指令格式「操作码 + 一串索引」，也正是为什么反汇编输出里全是十六进制数字。

上一讲 u3-l4 已经讲过生成器如何发射指令、如何分配临时索引；u3-l3 已经讲过索引的位编码 `value(24 位) | level(4 位) | var_type(4 位)`。本讲会**复用**这两块结论，重点放在「指令格式本身」和「怎么读反汇编」，对索引编码只做应用层面的回顾（完整拆解见 u4-l2）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/njs_vmcode.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h) | 定义所有**指令结构体**（`njs_vmcode_2addr_t` 等）与 **`NJS_VMCODE_*` 操作码枚举**，是本讲的核心。 |
| [src/njs_disassembler.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c) | 反汇编器：`code_names[]` 助记符表 + `njs_disassembler` / `njs_disassemble` 两个输出函数。 |
| [src/njs_vm.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c) | `njs_vm_compile` 末尾在 `options.disassemble` 为真时调用反汇编器——这就是 `-d` 开关的入口。 |
| [src/njs_scope.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h) | 索引的位编码常量与解码内联函数，读 hex 操作数时用。 |
| [src/njs_vm.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h) | `njs_level_type_t` 枚举（LOCAL/CLOSURE/GLOBAL/STATIC）与 `vm->levels` 字段。 |
| [docs/agent/engine-dev.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md) | 官方给出的字节码示例，是本讲代码实践的对照基准。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**指令格式**、**操作码枚举**、**反汇编表与输出**、以及一个把它们串起来的实战模块**「读懂 hex 索引」**。

### 4.1 指令格式：操作码 + 变长操作数

#### 4.1.1 概念说明

一条 njs 字节码指令在内存里就是一个**定长结构体**，但不同指令的结构体大小不同——所以从整体看，字节码是**变长**的。每条指令的布局都遵循同一个套路：

```
[ 操作码 1 字节 ] [ 操作数1 ] [ 操作数2 ] [ ... ]
```

- **操作码（opcode）**：永远是结构体的第一个字段，类型是 `njs_vmcode_t`，本质就是 `uint8_t`，占用 1 个字节。它告诉解释器「这条指令要干什么」。
- **操作数（operand）**：几乎全是 `njs_index_t`（即 `uintptr_t`，64 位平台上 8 字节），指向运行期某个值所在的槽位。少数指令的操作数是跳转偏移 `njs_jump_off_t`（`intptr_t`）或指针（如正则模式、lambda）。

为什么是「变长」？因为不同指令需要的操作数个数不同：

- `STOP` 只需 1 个操作数（脚本的返回值放在哪）。
- `MOVE` 需要 2 个（目的槽、源槽）。
- `ADD`（加法）需要 3 个（结果槽、左操作数槽、右操作数槽）。
- `JUMP` 不用索引，用的是 1 个跳转偏移。

操作数个数不同 → 结构体字段数不同 → `sizeof` 不同 → 指令在字节流里占的宽度不同。

#### 4.1.2 核心流程

解释器与反汇编器读取字节码时，都遵循同一个「取指—推进」循环：

```
1. 在当前 pc 处读第 1 个字节，得到操作码 opcode。
2. 根据 opcode 选出它对应的指令结构体类型（如 ADD → njs_vmcode_3addr_t）。
3. 把 pc 处的内存按该结构体类型强制转换、读取各操作数。
4. 执行（解释器）或打印（反汇编器）这条指令的语义。
5. pc += sizeof(该结构体)，跳到下一条指令，回到第 1 步。
```

第 5 步是关键：**推进多少字节完全由 `sizeof(结构体)` 决定**。我们待会儿会用反汇编输出里的偏移量亲自验证这一点。

#### 4.1.3 源码精读

先看操作码字段本身的类型定义，就在 [src/njs_vmcode.h:25-26](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L25-L26)：

```c
typedef intptr_t                        njs_jump_off_t;
typedef uint8_t                         njs_vmcode_t;
```

`njs_vmcode_t` 就是 `uint8_t`，所以每条指令都从 1 个字节的操作码开始。

接着是一组按「操作数个数 / 形态」归类的指令结构体。最基础的是一个「通用三操作数」骨架 [src/njs_vmcode.h:119-124](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L119-L124)：

```c
typedef struct {
    njs_vmcode_t               code;
    njs_index_t                operand1;
    njs_index_t                operand2;
    njs_index_t                operand3;
} njs_vmcode_generic_t;
```

实际使用的是更精细的几个。最常见的是「3 地址」与「2 地址」[src/njs_vmcode.h:133-145](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L133-L145)：

```c
typedef struct {
    njs_vmcode_t               code;
    njs_index_t                dst;
    njs_index_t                src;
} njs_vmcode_2addr_t;

typedef struct {
    njs_vmcode_t               code;
    njs_index_t                dst;
    njs_index_t                src1;
    njs_index_t                src2;
} njs_vmcode_3addr_t;
```

- `njs_vmcode_3addr_t` 是所有二元运算（加、减、比较、位运算……）的统一容器，语义是 `dst = src1 ⊕ src2`。
- `njs_vmcode_2addr_t` 用于一元运算与赋值搬运（如 `MOVE`、`TYPEOF`）。

`MOVE` 指令有自己的别名类型，但字段布局和 2 地址完全一样 [src/njs_vmcode.h:148-152](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L148-L152)：

```c
typedef struct {
    njs_vmcode_t               code;
    njs_index_t                dst;
    njs_index_t                src;
} njs_vmcode_move_t;
```

跳转类指令把「索引操作数」换成「跳转偏移」。无条件跳转 [src/njs_vmcode.h:209-212](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L209-L212)：

```c
typedef struct {
    njs_vmcode_t               code;
    njs_jump_off_t             offset;
} njs_vmcode_jump_t;
```

条件跳转则多一个条件槽 [src/njs_vmcode.h:215-219](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L215-L219)：

```c
typedef struct {
    njs_vmcode_t               code;
    njs_jump_off_t             offset;
    njs_index_t                cond;
} njs_vmcode_cond_jump_t;
```

属性访问类指令有 3 个索引（结果、对象、属性名）[src/njs_vmcode.h:238-243](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L238-L243)：

```c
typedef struct {
    njs_vmcode_t               code;
    njs_index_t                value;
    njs_index_t                object;
    njs_index_t                property;
} njs_vmcode_prop_get_t;
```

还有只带一个结果槽的「停止 / 返回」类指令 [src/njs_vmcode.h:311-320](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L311-L320)：

```c
typedef struct {
    njs_vmcode_t               code;
    njs_index_t                retval;
} njs_vmcode_return_t;

typedef struct {
    njs_vmcode_t               code;
    njs_index_t                retval;
} njs_vmcode_stop_t;
```

可以看到 `RETURN` 与 `STOP` 的结构体完全同构（都只有 `code + retval`），它们只是**操作码语义**不同：`RETURN` 用于从函数返回，`STOP` 用于结束整段脚本。

> 顺带一提：[src/njs_vmcode.h:11-22](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L11-L22) 的注释说明了解释器把一些负数返回值当作特殊事件处理（如 `0` 表示 `STOP` 成功结束、`-1` 表示异常），并规定了跳转偏移的下限——这就是 `njs_jump_off_t` 用有符号 `intptr_t` 的原因。

#### 4.1.4 代码实践

**目标**：用结构体对齐知识，估算几类指令在 64 位平台上的字节宽度，并理解反汇编偏移量为何那样递增。

**操作步骤**（源码阅读型，无需运行）：

1. 在 [src/njs_vmcode.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h) 中找到 `njs_vmcode_1addr_t`、`njs_vmcode_move_t`、`njs_vmcode_3addr_t`。
2. 记住两点：操作码 `code` 占 1 字节；`njs_index_t` 是 `uintptr_t`，64 位平台上 8 字节、按 8 字节对齐。因此 `code` 之后会有 7 字节对齐填充。
3. 推算每类指令的 `sizeof`（示例推算，64 位平台）：

   | 结构体 | 字段 | 推算宽度 |
   |---|---|---|
   | `njs_vmcode_1addr_t` / `return_t` / `stop_t` | code + 1 个 index | \(1 + 7_{\text{填充}} + 8 = 16\) 字节 |
   | `njs_vmcode_2addr_t` / `move_t` | code + 2 个 index | \(1 + 7_{\text{填充}} + 8 + 8 = 24\) 字节 |
   | `njs_vmcode_3addr_t` | code + 3 个 index | \(1 + 7_{\text{填充}} + 8 + 8 + 8 = 32\) 字节 |
   | `njs_vmcode_jump_t` | code + 1 个 offset | 16 字节 |

4. 把推算结果与下一节反汇编输出里的偏移量对照（`MOVE` 后 `STOP` 出现在 `00024`、`ADD` 后 `RETURN` 出现在 `00032`）。

**预期结果**：偏移量正好等于上一条指令的 `sizeof`，验证了「pc 按 `sizeof(结构体)` 推进」。

> 说明：上表字节宽度是**按 64 位平台结构体对齐推算的示例**，32 位平台上 `njs_index_t` 为 4 字节，宽度会不同；但「按 `sizeof` 推进」的机制在所有平台都成立。本机实际宽度请以反汇编偏移量为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 njs 不把所有指令都做成 `njs_vmcode_generic_t`（固定 3 个操作数）的统一宽度？那样取指不是更简单吗？

**参考答案**：统一宽度虽然取指简单，但会让只需要 1 个操作数的指令（如 `STOP`、`RETURN`）也白白占用 3 个 index 的空间，字节码体积膨胀明显。njs 选择「按需变长」：每条指令只占它真正需要的宽度，体积更紧凑；代价是解释器/反汇编器要靠操作码来判断当前指令宽度。

**练习 2**：`njs_vmcode_return_t` 和 `njs_vmcode_stop_t` 字段完全相同，为什么要定义两个类型？

**参考答案**：为了**语义清晰**。两者内存布局一致，但操作码不同（`RETURN` vs `STOP`），解释器对它们的处理也不同（函数返回 vs 脚本终止）。用不同类型名让生成器、反汇编器、解释器在读代码时一眼分清用途。

---

### 4.2 操作码枚举 NJS_VMCODE_*

#### 4.2.1 概念说明

操作码用一个**匿名枚举**集中定义，所有成员以 `NJS_VMCODE_` 为前缀。这个枚举有两层意义：

1. **它是操作码的编号表**。枚举从 `NJS_VMCODE_PUT_ARG = 0` 开始隐式递增，每个成员的整数值就是写进指令第 1 个字节的那 1 字节数字。也就是说，指令流里出现的「操作码字节」就是这里的枚举值。
2. **它是解释器分发的依据**。下一单元 u4-l1 会讲，解释器主循环用一个「计算跳转（computed goto）」表，按下标取这个枚举值对应的处理标号——所以枚举顺序与跳转表顺序必须一一对应。

按语义，操作码大致可以分成几组：

| 分组 | 代表操作码 | 说明 |
|---|---|---|
| 终止 / 控制 | `STOP`, `JUMP`, `RETURN` | 结束执行、无条件 / 条件跳转、函数返回 |
| 运算 | `ADDITION`, `SUBTRACTION`, `MULTIPLICATION`, `EQUAL`, `STRICT_EQUAL`, `LESS`, `BITWISE_AND`, `LEFT_SHIFT` … | 二元运算，统一走 3 地址 |
| 一元 | `UNARY_NEGATION`, `BITWISE_NOT`, `LOGICAL_NOT`, `TYPEOF`, `VOID`, `DELETE` | 单操作数运算 |
| 赋值搬运 | `MOVE`, `LET`, `LET_UPDATE` | 槽位之间搬值、`let` 初始化 |
| 属性 | `PROPERTY_GET`, `PROPERTY_ATOM_GET`, `PROPERTY_SET`, `PROPERTY_INIT`, `GLOBAL_GET` | 读写对象属性 |
| 调用 | `FUNCTION_FRAME`, `METHOD_FRAME`, `FUNCTION_CALL` | 建调用帧 + 触发函数调用 |
| 异常 / 异步 | `TRY_START`, `CATCH`, `FINALLY`, `THROW`, `AWAIT` | try/catch 与 async/await |
| 字面量构造 | `OBJECT`, `ARRAY`, `FUNCTION`, `REGEXP`, `TEMPLATE_LITERAL` | 创建对象 / 数组 / 函数 / 正则字面量 |

#### 4.2.2 核心流程

操作码本身只是个数字，它的「含义」由两处共同决定：

```
源码层：  NJS_VMCODE_* 枚举名 + 该指令的结构体类型  →  写入 / 读取格式
执行层：  njs_vmcode_interpreter 的分发分支          →  运行期行为（u4 详讲）
```

本讲只关心「写入 / 读取格式」这一层；运行期行为留给 u4。

#### 4.2.3 源码精读

操作码枚举定义在 [src/njs_vmcode.h:29-116](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L29-L116)，开头几行：

```c
enum {
    NJS_VMCODE_PUT_ARG = 0,
    NJS_VMCODE_STOP,
    NJS_VMCODE_JUMP,
    NJS_VMCODE_PROPERTY_ATOM_SET,
    NJS_VMCODE_PROPERTY_SET,
    ...
    NJS_VMCODE_RETURN,        // 隐式 = 10
    ...
    NJS_VMCODE_MOVE,          // 隐式 = 35
    ...
    NJS_VMCODE_PROPERTY_GET,  // 隐式 = 37
    ...
    NJS_VMCODE_ADDITION,      // 隐式 = 48
    ...
    NJS_VMCODES               // 末尾哨兵，等于操作码总数
};
```

因为枚举从 `PUT_ARG = 0` 起隐式递增，可以读出几个常用操作码的编号（由源码顺序推算）：`STOP = 1`、`JUMP = 2`、`RETURN = 10`、`MOVE = 35`、`PROPERTY_GET = 37`、`ADDITION = 48`。末尾的 `NJS_VMCODES` 不对应任何指令，它的值刚好等于操作码总数，常用来界定数组边界。

解释器主函数的声明紧跟其后 [src/njs_vmcode.h:409-410](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L409-L410)：

```c
njs_int_t njs_vmcode_interpreter(njs_vm_t *vm, u_char *pc, njs_value_t *retval,
    void *promise_cap, void *async_ctx);
```

它从入口 `pc` 开始逐条取指执行——这就是「这些操作码最终被谁消费」。具体每个操作码分支怎么跑，是 u4-l1 的主题，本讲不展开。

#### 4.2.4 代码实践

**目标**：在源码里亲手定位本讲关心的几条指令的编号。

**操作步骤**：

1. 打开 [src/njs_vmcode.h:29-116](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L29-L116)。
2. 从 `NJS_VMCODE_PUT_ARG = 0` 开始往下数，分别找到 `STOP`、`JUMP`、`RETURN`、`MOVE`、`ADDITION`、`PROPERTY_GET`，记下它们各自是第几个（从 0 起）。
3. 数到末尾的 `NJS_VMCODES`，确认它的值就是「操作码总数」。

**预期结果**：得到 `STOP=1, JUMP=2, RETURN=10, MOVE=35, PROPERTY_GET=37, ADDITION=48`。

**需要观察的现象**：注意很多「语义相近」的操作码并不相邻（例如 `PROPERTY_GET` 在第 37 位，而 `GLOBAL_GET` 在 43 位），这说明枚举顺序是按历史 / 实现习惯排的，**不要假设语义分组与编号连续**。

#### 4.2.5 小练习与答案

**练习 1**：`NJS_VMCODES` 这个末尾成员有什么用？

**参考答案**：它是「哨兵」成员，不对应真实指令，其整数值等于操作码总数。常用于声明以操作码为下标的数组（如解释器的跳转表、反汇编查找表），确保数组容量覆盖所有合法操作码。

**练习 2**：如果新增一个操作码，应该加在枚举的哪里？随意插在中间会有什么后果？

**参考答案**：应加在 `NJS_VMCODES` 之前。若插在中间，会让其后所有操作码的编号整体后移，导致两处失配：① 反汇编器的 `code_names[]` 表（按下标查）会错位；② 旧字节码文件里硬编码的编号会指向错误指令。所以新增操作码一律追加到末尾、不动既有编号。

---

### 4.3 反汇编表与 njs_disassemble 输出

#### 4.3.1 概念说明

有了指令格式和操作码，还差最后一块：怎么把一段二进制字节码「打印成人能读的文本」？这就是反汇编器（disassembler）的职责。

njs 的反汇编器由三部分组成：

1. **`code_names[]` 助记符表**：把每个操作码映射成 `(助记符字符串, sizeof(指令))`，是「操作码 → 文本」的查询表。
2. **`njs_disassembler(vm)`**：遍历 VM 里所有代码块（`vm->codes`），逐块调用下者。
3. **`njs_disassemble(start, end, count, lines)`**：在 `[start, end)` 字节区间内逐条解析、打印指令。

反汇编器的输出格式固定为若干列：

```
源码行号 | 字节偏移  助记符  操作数1 操作数2 ...
```

其中操作数以**十六进制**打印（格式 `%04Xz`，即至少 4 位十六进制、不足补零）。

#### 4.3.2 核心流程

`njs_disassemble` 是一个大循环，对每条指令的处理分两条路径：

```
读 1 字节操作码 operation
├── 是「特殊指令」(JUMP / IF_TRUE_JUMP / ARRAY / TRY_START / CATCH / FINALLY / IMPORT …)？
│     → 走专属 if 分支，按各自结构体打印（操作数里常含跳转偏移 %z）
└── 否则 → 在 code_names[] 里线性查 operation
      ├── size == sizeof(3addr) → 按 3 操作数打印
      ├── size == sizeof(2addr) → 按 2 操作数打印
      ├── size == sizeof(1addr) → 按 1 操作数打印
      └── 查不到 → 打印 UNKNOWN，只推进 1 字节
pc += 该指令宽度，继续
```

「特殊指令」之所以单独处理，是因为它们的操作数里混入了跳转偏移、长度、构造标志等非索引字段，打印格式与通用的「一串索引」不同。

#### 4.3.3 源码精读

先看助记符表的元素类型 [src/njs_disassembler.c:11-15](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L11-L15)：

```c
typedef struct {
    njs_vmcode_t               operation;   // 操作码
    size_t                     size;        // 对应指令的 sizeof
    njs_str_t                  name;        // 助记符字符串
} njs_code_name_t;
```

`code_names[]` 表把操作码一一挂上助记符和宽度 [src/njs_disassembler.c:18-166](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L18-L166)，挑几条本讲用得上的看：

```c
{ NJS_VMCODE_PROPERTY_GET, sizeof(njs_vmcode_prop_get_t),
      njs_str("PROP GET        ") },
...
{ NJS_VMCODE_RETURN, sizeof(njs_vmcode_return_t),
      njs_str("RETURN          ") },
{ NJS_VMCODE_STOP, sizeof(njs_vmcode_stop_t),
      njs_str("STOP            ") },
...
{ NJS_VMCODE_ADDITION, sizeof(njs_vmcode_3addr_t),
      njs_str("ADD             ") },
...
{ NJS_VMCODE_MOVE, sizeof(njs_vmcode_move_t),
      njs_str("MOVE            ") },
```

注意两件事：① 助记符字符串都被填充到固定宽度（便于输出对齐）；② `size` 字段直接存了 `sizeof(...)`，反汇编器推进 `pc` 时用的就是它。

`njs_disassembler` 负责遍历所有代码块 [src/njs_disassembler.c:169-186](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L169-L186)：

```c
code = vm->codes->start;
n = vm->codes->items;

while (n != 0) {
    njs_printf("%V:%V\n", &code->file, &code->name);   // 打印 "文件:函数名"
    njs_disassemble(code->start, code->end, -1, code->lines);
    code++;
    n--;
}
```

`vm->codes` 是一个 `njs_vm_code_t` 数组（每个元素代表一段编译产物，如 `shell:main`、`shell:f`），其结构体定义见 [src/njs_vm.h:201-204](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L201-L204)，含 `file`、`name`、字节码区间 `[start,end)`、行号表 `lines`。

`njs_disassemble` 的主循环 [src/njs_disassembler.c:229](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L229) 逐条取指。无条件跳转走专属分支 [src/njs_disassembler.c:269-278](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L269-L278)：

```c
if (operation == NJS_VMCODE_JUMP) {
    jump = (njs_vmcode_jump_t *) p;
    njs_printf("%5uD | %05uz JUMP              %z\n",
               line, p - start, (size_t) jump->offset);
    p += sizeof(njs_vmcode_jump_t);
    continue;
}
```

通用指令（运算、搬运等）在跳过所有特殊分支后，落入 `code_names[]` 线性查找，并按 `size` 选 1/2/3 地址打印格式 [src/njs_disassembler.c:531-569](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L531-L569)：

```c
do {
    if (operation == code_name->operation) {
        name = &code_name->name;

        if (code_name->size == sizeof(njs_vmcode_3addr_t)) {
            code3 = (njs_vmcode_3addr_t *) p;
            njs_printf("%5uD | %05uz %*s  %04Xz %04Xz %04Xz\n",
                       line, p - start, name->length, name->start,
                       (size_t) code3->dst, (size_t) code3->src1,
                       (size_t) code3->src2);
        } else if (code_name->size == sizeof(njs_vmcode_2addr_t)) {
            ...   // 打印 dst、src 两个操作数
        } else if (code_name->size == sizeof(njs_vmcode_1addr_t)) {
            ...   // 打印单个 index
        }
        p += code_name->size;
        goto next;
    }
    code_name++; n--;
} while (n != 0);
```

如果遍历完 `code_names[]` 也没匹配上，就打印 `UNKNOWN` 并只推进 1 字节 [src/njs_disassembler.c:571-574](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L571-L574)——这是容错兜底，正常编译产物不会走到这里。

关于格式串：`%5uD` 是源码行号（5 位右对齐），`%05uz` 是当前指令在该代码块内的字节偏移，`%04Xz` 把索引按至少 4 位十六进制打印，`%z` 打印有符号跳转偏移。这些都是 njs 自带的 `njs_printf` 格式说明符。

最后，`-d` 开关之所以能触发反汇编，是因为 `njs_vm_compile` 末尾有这一段 [src/njs_vm.c:299-301](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L299-L301)：

```c
if (vm->options.disassemble) {
    njs_disassembler(vm);
}
```

CLI 的 `-d` 选项会把 `vm->options.disassemble` 置真，于是编译完成、字节码挂到 `vm->start` 之后立刻被打印出来。

#### 4.3.4 代码实践

**目标**：弄懂反汇编输出每一列的含义，为下一节的「读 hex 操作数」做准备。

**操作步骤**（源码阅读型）：

1. 对照 `njs_disassemble` 里的 `njs_printf` 格式串，记住列含义：

   | 列 | 来源 | 含义 |
   |---|---|---|
   | 第 1 列（如 `1`） | `%5uD`，`line` | 该指令对应的**源码行号** |
   | 第 2 列（如 `00024`） | `%05uz`，`p - start` | 该指令在代码块内的**字节偏移** |
   | 第 3 列（如 `MOVE`） | `code_names[].name` | 助记符 |
   | 第 4 列及以后（如 `0123 0133`） | `%04Xz`，各 index | 操作数（十六进制索引 / 跳转偏移） |

2. 读官方示例（见下节 4.4）。

**预期结果**：看到任意一行反汇编，能立刻指出「源码第几行 / 块内偏移多少 / 什么指令 / 操作数是哪几个」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `JUMP`、`ARRAY`、`TRY_START` 等指令要在 `njs_disassemble` 里单独写 `if` 分支，而不是统一进 `code_names[]` 查表？

**参考答案**：因为这些指令的操作数里含有**跳转偏移、数组长度、构造标志、模块指针**等非索引字段，打印格式各不相同（比如 `JUMP` 要用 `%z` 打印偏移、`ARRAY` 要额外打印 `length` 和可选的 `INIT`）。通用查表分支只能按「1/2/3 个 index」三种套路打印，表达不了这些差异，所以单独处理。

**练习 2**：反汇编输出第 2 列「字节偏移」对调试有什么用？

**参考答案**：偏移量就是该指令在字节码里的位置（`pc - start`）。配合 4.1 节的 `sizeof` 推算，可以**反推每条指令的实际宽度**，验证对指令格式的理解；在排查「跳转目标对不对」「某条指令是否被生成」时，偏移量也是定位字节码位置的直接坐标。

---

### 4.4 实战：读懂 hex 索引（承接索引编码）

这一节把前三节串起来：用 `index` 的位编码，把官方反汇编示例里的 `0123`、`0133` 等十六进制操作数逐个拆开，看懂它们到底指什么。

#### 4.4.1 概念说明

回顾 u3-l3 已建立的索引编码（完整拆解见 u4-l2）。一个 `njs_index_t` 的低 32 位被切成三段：

```
 高 24 位：value（槽位号）   |   中 4 位：level（存储层级）   |   低 4 位：var_type（变量类型）
```

用位运算表达就是：

\[
\text{index} = (\text{value} \ll 8) \;|\; (\text{level} \ll 4) \;|\; \text{var\_type}
\]

两段枚举值（运行期存储层级、变量类型）如下。

存储层级 `njs_level_type_t`，来自 [src/njs_vm.h:109-115](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L109-L115)：

| 枚举 | 值 | 含义 |
|---|---|---|
| `NJS_LEVEL_LOCAL` | 0 | 当前函数的本地槽位 |
| `NJS_LEVEL_CLOSURE` | 1 | 闭包捕获的外层变量 |
| `NJS_LEVEL_GLOBAL` | 2 | 全局变量 |
| `NJS_LEVEL_STATIC` | 3 | 跨克隆共享的静态值（字面量等） |

变量类型（来自 `docs/agent/engine-dev.md` 与 u3-l3）：

| 枚举 | 值 |
|---|---|
| `NJS_VARIABLE_CONST` | 0 |
| `NJS_VARIABLE_LET` | 1 |
| `NJS_VARIABLE_CATCH` | 2 |
| `NJS_VARIABLE_VAR` | 3 |
| `NJS_VARIABLE_FUNCTION` | 4 |

运行期解释器拿到索引后，用 `njs_scope_value` 一次解码就定位到值 [src/njs_scope.h:78-83](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L78-L83)：

```c
njs_inline njs_value_t *
njs_scope_value(njs_vm_t *vm, njs_index_t index)
{
    return vm->levels[njs_scope_index_type(index)]
                     [njs_scope_index_value(index)];
}
```

即 `vm->levels[level][value]`——层级选数组、槽位号选元素。`var_type` 段在运行期主要用于 TDZ（暂时性死区）判断，不参与寻址。

#### 4.4.2 核心流程

把一个十六进制操作数读成人话，固定三步：

```
1. 低 4 位        → var_type（查变量类型枚举）
2. 第 5~8 位      → level（查存储层级枚举）
3. 第 9 位及以上   → value（槽位号）
```

#### 4.4.3 源码精读

索引的位编码常量在 [src/njs_scope.h:10-23](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L10-L23)：

```c
#define NJS_SCOPE_VAR_OFFSET    0
#define NJS_SCOPE_VAR_SIZE      4

#define NJS_SCOPE_TYPE_OFFSET   (NJS_SCOPE_VAR_OFFSET + NJS_SCOPE_VAR_SIZE)  // 4
#define NJS_SCOPE_TYPE_SIZE     4

#define NJS_SCOPE_VALUE_OFFSET  (NJS_SCOPE_TYPE_OFFSET + NJS_SCOPE_TYPE_SIZE) // 8
#define NJS_SCOPE_VALUE_SIZE    24
```

组装索引的函数 [src/njs_scope.h:36-53](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L36-L53)：

```c
return (index << NJS_SCOPE_VALUE_OFFSET) | (type << NJS_SCOPE_TYPE_OFFSET)
        | var_type;
```

这三段正好对应上面的拆解步骤。

#### 4.4.4 代码实践（本讲核心实践）

**目标**：运行官方示例脚本，对照 `docs/agent/engine-dev.md` 的字节码输出，逐条解释 `MOVE / STOP / ADD / RETURN` 的操作数含义。

**操作步骤**：

1. 按上一单元的方式构建 CLI：`./configure && make njs`，得到 `build/njs`。
2. 反汇编目标脚本（与官方文档同一段代码）：

   ```bash
   ./build/njs -d -c 'var a = 42; function f(v) { return v + 1 }'
   ```

   > 注：官方文档演示的是交互式 `./build/njs -d` 后输入；`-d -c '...'` 是一次性等价写法。两种形式编译出的字节码相同，实际输出以本机为准（待本地验证）。
3. 对照官方给出的输出（[docs/agent/engine-dev.md:230-247](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L230-L247)）：

   ```
   shell:main
       1 | 00000 MOVE     0123 0133
       1 | 00024 STOP     0033

   shell:f
       1 | 00000 ADD      0203 0103 0233
       1 | 00032 RETURN   0203
   ```
4. 逐条拆解（按 4.4.2 的三步法）：

   - **`MOVE 0123 0133`**（把源槽 `0x0133` 的值搬到目的槽 `0x0123`）
     - `0x0123`：低 4 位 `3`→VAR；中 4 位 `2`→**GLOBAL**；高字节 `1`→槽 1。即「全局变量槽 1」＝变量 `a`。
     - `0x0133`：低 4 位 `3`→VAR；中 4 位 `3`→**STATIC**；高字节 `1`→槽 1。即「静态槽 1」＝字面量 `42`。
     - 语义：`a = 42`。
   - **`STOP 0033`**
     - `0x0033`：低 4 位 `3`→VAR；中 4 位 `3`→**STATIC**；高字节 `0`→槽 0。即「静态槽 0」＝脚本返回值（`undefined`）。
     - 语义：结束脚本，返回 `undefined`。
   - **`ADD 0203 0103 0233`**（`dst = src1 + src2`）
     - `0x0203`（dst）：低 `3`→VAR；中 `0`→**LOCAL**；高 `2`→槽 2。即「函数本地槽 2」＝临时结果。
     - `0x0103`（src1）：低 `3`→VAR；中 `0`→**LOCAL**；高 `1`→槽 1。即「函数本地槽 1」＝参数 `v`。
     - `0x0233`（src2）：低 `3`→VAR；中 `3`→**STATIC**；高 `2`→槽 2。即「静态槽 2」＝字面量 `1`。
     - 语义：`临时结果 = v + 1`。
   - **`RETURN 0203`**
     - `0x0203`：同上，函数本地槽 2（刚刚 `ADD` 的结果）。
     - 语义：`return (v + 1)`。

5. 顺带用偏移量验证指令宽度（呼应 4.1.4）：
   - `main` 块：`MOVE` 在 `00000`，`STOP` 在 `00024` → `MOVE` 占 24 字节（= `sizeof(njs_vmcode_move_t)`）。
   - `f` 块：`ADD` 在 `00000`，`RETURN` 在 `00032` → `ADD` 占 32 字节（= `sizeof(njs_vmcode_3addr_t)`）。

**预期结果**：四条指令的 hex 操作数全部能拆解成「层级 + 类型 + 槽位」，且语义与源码 `var a=42; function f(v){return v+1}` 完全吻合；偏移量与按 `sizeof` 推算的宽度一致。

**需要观察的现象**：

- 同一个字面量 `42`、`1` 都落在 **STATIC** 层（跨克隆共享），而变量 `a` 落在 **GLOBAL** 层、参数 `v` 与临时结果落在 **LOCAL** 层——这正是 u3-l3 讲过的「四级存储」在字节码里的直接体现。
- 注意 `0x0103` 与 `0x0203` 的**中 4 位都是 0（LOCAL）**，只有高字节（槽位号）不同；而 `0x0133`、`0x0233` 的中 4 位是 3（STATIC）。读 hex 时先看中段判断层级，往往最快。

> 如果本机输出与上述不符（例如行号、槽位编号不同），以本机实际输出为准；指令助记符与拆解方法不变。

#### 4.4.5 小练习与答案

**练习 1**：在官方示例里，字面量 `42` 和 `1` 为什么都用 `STATIC` 层而不是 `LOCAL` 层？

**参考答案**：字面量是编译期就确定的常量，被放进跨克隆共享的静态值表（`NJS_LEVEL_STATIC`），所有克隆出来的 VM 都能只读复用同一份，省内存。`LOCAL` 层是每个函数调用帧私有的可变槽位，适合放变量和临时结果，不适合放不可变的字面量。

**练习 2**：假如把 `var a = 42` 改成 `let a = 42`，`MOVE` 的目的操作数 `0x0123` 会怎样变化？

**参考答案**：`var` 对应 `NJS_VARIABLE_VAR=3`（低 4 位 `3`）；`let` 对应 `NJS_VARIABLE_LET=1`（低 4 位 `1`）。在全局作用域里层级仍是 GLOBAL（中 4 位不变），槽位号也可能不变，所以目的操作数会从 `0x0123` 变成低 4 位为 `1` 的值（如 `0x0121`）。这也解释了为什么 `njs_scope_valid_value` 要用低 4 位的 var_type 来判断「能否在初始化前访问」——`let/const`（≤1）会触发 TDZ 报错，而 `var` 不会。

---

## 5. 综合实践

把本讲四块知识用一次完整任务串起来。

**任务**：自己写一段同时包含「运算、函数调用、属性访问、跳转」的脚本，反汇编它，并画一张「pc 流转图」。

**建议脚本**（示例代码，非项目原有）：

```js
function add(x, y) { return x + y }
var n = add(1, 2);
var len = "hello".length;
if (n > len) { n = len }
```

**操作步骤**：

1. 构建并反汇编：`./build/njs -d -c '<上面这段脚本>'`（待本地验证具体输出）。
2. 在输出里找出并标注以下指令，说明它们的结构体类型与操作数个数：
   - 一条 `ADD`（3 地址，3 个 index）；
   - 一条 `FUNCTION_FRAME` + `FUNCTION_CALL`（函数调用如何分两步）；
   - 一条 `PROP GET`（属性访问，注意它有 `value/object/property` 三个 index）；
   - 一条 `JUMP IF TRUE` 或 `JUMP IF FALSE`（注意它的操作数是 `cond` 索引 + 跳转偏移 `%z`，不是纯 index）。
3. 用 4.4.2 的三步法，把每条指令的 index 操作数拆成「层级 / 类型 / 槽位」。
4. 用偏移量（第 2 列）算出每条指令的实际宽度，与 4.1.4 的 `sizeof` 推算对照。
5. 画出 pc 在指令间的流转：顺序执行时 pc 如何按 `sizeof` 递增；遇到 `JUMP IF ...` 时，当条件成立 / 不成立时 pc 分别跳到哪个偏移。

**预期结果**：你能对着反汇编输出，逐条讲清楚「这条指令是什么、操作数指哪里、下一条会执行哪里」——这就具备了阅读 njs 字节码的能力，也为下一单元 u4「解释器如何执行这些指令」打好了基础。

> 提示：`FUNCTION_FRAME` 建好调用帧后，参数是通过 4.2 表里的 `PUT_ARG`（操作码 0）压入的；如果反汇编里看到 `PUT ARG`，把它和 `FUNCTION_CALL` 联系起来看。

---

## 6. 本讲小结

- njs 字节码指令 = **1 字节操作码 + 若干操作数**；操作数多为 `njs_index_t`（指向 `vm->levels` 槽位），少数为跳转偏移。不同指令操作数个数不同，故**变长**。
- 指令格式由一组结构体描述（[src/njs_vmcode.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h)）：`1addr / 2addr / 3addr / move / jump / cond_jump / prop_get …`，取指时 `pc += sizeof(对应结构体)`。
- 操作码集中在 `NJS_VMCODE_*` 枚举，从 `PUT_ARG=0` 隐式递增，末尾 `NJS_VMCODES` 是总数哨兵；核心指令有 `STOP / JUMP / RETURN / MOVE / ADDITION / PROPERTY_GET`。
- 反汇编器三件套：`code_names[]` 助记符表、`njs_disassembler` 遍历 `vm->codes`、`njs_disassemble` 逐条打印；特殊指令走专属分支，通用指令按 1/2/3 地址查表打印。
- `-d` 开关在 `njs_vm_compile` 末尾（`vm->options.disassemble`）触发反汇编；输出列为「源码行号 | 字节偏移 | 助记符 | 十六进制操作数」。
- 读 hex 操作数靠索引位编码 `value(24) | level(4) | var_type(4)`：例如 `0x0123` = GLOBAL 层槽 1（变量 `a`），`0x0133` = STATIC 层槽 1（字面量 `42`）。

## 7. 下一步学习建议

本讲只回答了「字节码长什么样、怎么读」，还没回答「它怎么被执行」。下一步进入 **u4 字节码执行引擎**：

- **u4-l1 解释器主循环 `njs_vmcode_interpreter`**：看 computed-goto 如何按本讲的 `NJS_VMCODE_*` 操作码分发，以及一条 `ADD` 从取指到写回的完整路径。
- **u4-l2 levels / scope / index**：本讲只用了索引编码的结论，u4-l2 会完整讲四级存储 `LOCAL/CLOSURE/GLOBAL/STATIC` 与 `vm->levels` 寻址细节。
- **u4-l3 函数调用与调用帧**：本讲综合实践里出现的 `FUNCTION_FRAME` / `FUNCTION_CALL`，在那里讲清楚调用帧链。
- **u4-l4 异常处理**：本讲提到的 `TRY_START / CATCH / FINALLY / THROW` 如何在字节码层实现跳转。

建议继续精读的源码：`src/njs_vmcode.c`（解释器，u4-l1 主角）、`src/njs_vmcode.h`（再回看一遍指令结构体，结合执行会有新理解）。
