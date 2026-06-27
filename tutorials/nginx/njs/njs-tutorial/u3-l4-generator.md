# 字节码生成器 njs_generator

## 1. 本讲目标

本讲是「编译前端」流水线的第四站：把抽象语法树（AST）翻译成 njs 解释器能执行的字节码（vmcode）。

学完后你应该能够：

1. 说清生成器 `njs_generator_t` 是怎样用「显式栈 + 状态机（trampoline）」遍历 AST 的，并能把它和上一讲解析器 `njs_parser` 用的是同一种手法对应起来。
2. 看懂「指令发射」的全过程：`code_start/code_end` 代码缓冲如何按需扩容、`njs_generate_code` 宏如何把一条结构化的 `njs_vmcode_*_t` 写进缓冲、二元运算和 `return` 这两类典型节点各自发射什么指令。
3. 理解「临时索引」的分配—释放—复用机制（`index_cache` 自由表）、`dest` 直写优化，以及生成器如何为跨作用域变量登记「闭包索引」。

承接 u3-l3：上一讲确定了 AST 节点的 `index`（按 `value(24)|level(4)|var_type(4)` 编码、对应 `vm->levels[level][value]`），本讲正是把 `index` 写进每条字节码指令的操作数里。下一讲 u3-l5 会讲字节码格式与反汇编，u4 会讲这些指令在解释器里如何被执行。

## 2. 前置知识

在进入生成器之前，请确认你已建立以下几个概念（都在前面讲义中讲过）：

- **AST 节点 `njs_parser_node_t`**：`token_type` 表语义，`left`/`right` 是孩子，`dest` 指向「要消费本节点结果」的父节点，`index` 是运行期存储槽位，`u.operation` 对运算节点保存对应字节码操作码（u3-l2）。
- **变量与作用域**：`var`/`let`/`const` 的区别、`GLOBAL`/`FUNCTION`/`BLOCK` 三类解析期作用域、32 位 `index` 的位编码（u3-l3）。
- **字节码即一串变长结构体**：njs 的字节码不是一串裸字节，而是一串形如 `njs_vmcode_3addr_t`、`njs_vmcode_return_t` 的 C 结构体紧挨着排布，每条以 1 字节操作码 `njs_vmcode_t` 开头（u3-l5 会展开）。
- **「显式栈模拟递归」**：解析器没有用 C 函数递归，而是用一个栈压入「续体（continuation）」、用状态函数指针驱动主循环。生成器采用**完全相同**的套路，所以下文会把这套术语再走一遍。

> 一个贯穿全讲的直觉：**生成器 = 一台把 AST「拍平」成线性字节码的、不会栈溢出的状态机**。它一边深度优先遍历 AST，一边往一块连续内存里追加指令；遇到需要「先算完左子树、回来再算右子树」的情况，就往自己的栈里压一个「等会儿回到这里接着做」的续体。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/njs_generator.h` | 生成器结构体 `njs_generator_t`、状态函数指针类型、对外入口 `njs_generate_scope`/`njs_generator_init` 的声明。 |
| `src/njs_generator.c` | 生成器全部实现：状态机骨架、指令发射宏、各类节点的生成函数、临时索引与闭包协作。本讲的主战场。 |
| `src/njs_vmcode.h` | 字节码指令的结构体布局（`njs_vmcode_3addr_t` 等）与操作码枚举 `NJS_VMCODE_*`。 |
| `src/njs_variable.c` | 闭包索引的创建（`scope->closures` 数组），与生成器协作登记跨作用域变量。 |
| `external/njs_shell.c` | CLI 的 `-g`（generator 跟踪）、`-d`（反汇编）选项，用于本讲实践。 |

## 4. 核心概念与源码讲解

### 4.1 生成器状态机

#### 4.1.1 概念说明

「生成器（generator）」在这里**不是** ES6 的生成器函数，而是「字节码生成器」，即编译器后端的 AST→字节码 pass。它要解决的问题是：

- AST 是一棵树，但字节码是**线性**的一维指令序列。需要一次深度优先遍历，把「先求值谁、后求值谁」的顺序固化下来。
- 真实地求值顺序常常不是简单的「左→右」。比如 `return a+b`，必须先把 `a+b` 这个表达式（`return` 的右子树）整体算完，再发射 `RETURN` 指令。这要求生成器能「暂停」在 `return` 节点、先去处理子树、然后「回来」继续。
- 如果用 C 函数递归来实现「先处理子树再回来」，遇到极深的嵌套表达式可能栈溢出。njs 选择**显式栈**：把「回来后要做的事」打包成一个「续体」压入自己的 `stack` 队列，靠一个 `do { ... } while` 主循环不断驱动。这与上一讲解析器 `njs_parser_after`/`njs_parser_stack_pop` 的手法一一对应，只是换了 `generator` 前缀。

核心三件套：

- **状态函数指针** `njs_generator_state_func_t`：每个状态就是一个 `njs_int_t (*)(vm, generator, node)` 函数。生成器结构体里只记「当前要执行哪个状态、作用在哪个节点」。
- **续体栈** `generator->stack`：一个 `njs_queue_t`。`njs_generator_after` 往里压续体，`njs_generator_stack_pop` 从里弹续体并切换状态。
- **trampoline 主循环**：`njs_generate_scope` 里那段 `do { state(); } while (state != NULL)`，不断把当前状态「弹」起来执行，直到没有状态为止。

#### 4.1.2 核心流程

下面是整个 `njs_generate_scope` 的骨架伪代码（省略了大量边界处理）：

```
njs_generate_scope(vm, generator, scope, name):
    申请 128 字节初始代码缓冲 code_start/code_end
    发射「函数局部变量/arguments」前奏 (njs_generate_lambda_variables)
    初始化空续体栈 stack
    置首个状态 = njs_generate，作用节点 = scope->top（AST 根节点）
    压入终结续体 njs_generate_scope_end   # 整段代码生成完后的收尾

    do:                                    # trampoline 主循环
        ret = generator->state(vm, generator, generator->node)
    while (generator->state != NULL)

    把已生成字节码登记进 vm->codes，返回 njs_vm_code_t
```

其中 `njs_generate` 是「大分发器」，按当前节点的 `token_type` 调用对应的生成函数。每个生成函数几乎都遵循同一个模式：

```
某个节点的生成函数(node):
    njs_generator_next(generator, njs_generate, node->left)   # 「下一步先去处理左子树」
    njs_generator_after(generator, ..., node, 本节点的_end函数) # 「左子树处理完，回来执行我」
```

这正是「续体传递」：先把指针拨向左子树，再把自己（的续体）压栈。等左子树整条链跑完，栈顶就是自己，主循环自然回到 `_end` 函数继续。

#### 4.1.3 源码精读

**结构体定义**——注意 `state`（当前状态）、`stack`（续体栈）、`code_start/code_end`（字节码缓冲）这三个字段，它们是整台机器的全部驱动源：

[src/njs_generator.h:14-41](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.h#L14-L41) — 状态函数指针类型 `njs_generator_state_func_t`，以及 `struct njs_generator_s`。其中 `index_cache`（临时索引自由表）、`closures`（闭包索引数组）、`lines`（源码行号映射）会在 4.3 节用到。

**初始化**——清零、初始化续体栈、记录文件名/嵌套深度/runtime 标志：

[src/njs_generator.c:631-644](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L631-L644) — `njs_generator_init`。

**三个状态切换原语**——这是理解整个状态机的钥匙：

[src/njs_generator.c:647-653](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L647-L653) — `njs_generator_next`：把「当前状态/当前节点」直接覆盖为新的值，相当于「下一步立刻去做这件事」。

[src/njs_generator.c:656-684](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L656-L684) — `njs_generator_after`：往栈里插入一个续体（`njs_generator_stack_entry_t`），记录「等会儿回到哪个状态、作用在哪个节点、用什么 context」。`njs_queue_insert_before` 把续体插到队首之前，从而保证「后压的续体先执行」（深度优先）。

[src/njs_generator.c:687-710](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L687-L710) — `njs_generator_stack_pop`：弹出栈顶续体，恢复它的 `context`，再用 `njs_generator_next` 把状态切回续体记录的状态/节点。这一弹一切换，就完成了「从子树回到父节点」。

> 对照记忆：把 `njs_generator_after` 想成「函数调用前压返回地址」，把 `njs_generator_stack_pop` 想成「函数返回时弹出返回地址跳回去」，就能完全套用普通的调用栈直觉。

**入口 `njs_generate_scope` 与 trampoline 主循环**：

[src/njs_generator.c:4876-5007](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L4876-L5007) — 注意 4890 行先申请 128 字节初始缓冲；4942 行把首状态置为 `njs_generate(scope->top)`；4944 行压入 `njs_generate_scope_end` 终结续体；4951–4957 行就是 trampoline 主循环。结尾把产物封装成 `njs_vm_code_t`（`code->start/end/name`）登记进 `vm->codes`。

**大分发器 `njs_generate`**：

[src/njs_generator.c:713-937](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L713-L937) — 一个巨大的 `switch (node->token_type)`，把每种 AST 节点派发给对应生成函数。比如 `NJS_TOKEN_RETURN → njs_generate_return_statement`、`NJS_TOKEN_ADDITION（及其他二元运算）→ njs_generate_3addr_operation`、`NJS_TOKEN_STATEMENT → njs_generate_statement`。读这段代码就能建立「token_type → 生成函数」的完整心智地图。

#### 4.1.4 代码实践

**实践目标**：用 generator 跟踪开关 `-g` 亲眼看生成器「逐节点遍历 AST、逐条发射指令」。

**操作步骤**：

1. 先用调试开关构建（需要 `--debug-generator=YES` 才会编入 `-g` 与跟踪打印，见 `docs/agent/engine-dev.md` 的构建选项表）：

   ```bash
   ./configure --debug-generator=YES && make njs
   ```

2. 运行（注意 `-g` 是内置 njs 引擎专属，需用默认引擎，不要加 `-n QuickJS`）：

   ```bash
   ./build/njs -g -c 'function f(a,b){return a+b}'
   ```

**需要观察的现象**：终端会以 `GENERATOR ...` 为前缀打印生成器每一步的动作，比如 `INDEX REUSE`、`INDEX RELEASE`、`LOOKUP TRY` 等（这些打印来自 `njs_debug_generator` 宏，仅在 `NJS_DEBUG_GENERATOR` 编译时存在）。

**预期结果**：你能看到生成器先进入函数体、处理 `return`、再处理 `a+b` 这个二元运算的先后顺序，顺序与 4.1.2 的深度优先描述一致。具体的打印文本与指令助记符「待本地验证」（取决于版本与缓冲细节）。

> 说明：`-g` 选项在 [external/njs_shell.c:647-651](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L647-L651) 中被 `#ifdef NJS_DEBUG_GENERATOR` 包住；它把 `opts->generator_debug` 置 1，再传给 `vm_options.generator_debug`，运行时由 `njs_debug_generator` 宏据此决定是否打印。

#### 4.1.5 小练习与答案

**练习 1**：为什么生成器不用普通的 C 函数递归来遍历 AST？
**参考答案**：C 函数递归每层都消耗真实调用栈，遇到非常深的表达式或嵌套作用域（比如被工具生成的超长表达式）容易栈溢出；显式续体栈把「调用栈」搬到堆上（续体 `njs_generator_stack_entry_t` 由 `njs_mp_alloc` 分配），容量受内存池而非系统栈大小限制，更安全也更易控制遍历顺序。

**练习 2**：`njs_generator_next` 和 `njs_generator_after` 的区别是什么？
**参考答案**：`next` 是「下一步立刻去做」（直接覆盖当前状态/节点，不压栈）；`after` 是「等当前这条链跑完、再回来做我」（往栈里压一个续体）。前者相当于尾调用，后者相当于「先递归子问题、回来继续」里的「回来继续」部分被显式存进了栈。

### 4.2 指令发射

#### 4.2.1 概念说明

遍历 AST 只是手段，目的是往字节码缓冲里**写指令**。njs 的指令是一段紧挨着排列的 C 结构体，每条以 1 字节操作码开头，后面跟若干个 4 字节的 `njs_index_t` 操作数。常见布局（定义在 `njs_vmcode.h`）：

| 结构体 | 字段 | 典型用途 |
|---|---|---|
| `njs_vmcode_3addr_t` | `code, dst, src1, src2` | 三地址运算，如加减比较 |
| `njs_vmcode_2addr_t` | `code, dst, src` | 一元运算，如 `typeof`/`void` |
| `njs_vmcode_move_t` | `code, dst, src` | 把一个值搬到另一个槽 |
| `njs_vmcode_return_t` | `code, retval` | 函数返回 |
| `njs_vmcode_stop_t` | `code, retval` | 整段脚本/函数体结束 |

发射指令靠一个统一宏 `njs_generate_code`，它做三件事：① 在 `code_end` 处预留 `sizeof(类型)` 字节（必要时扩容）；② 记录「这条指令对应源码第几行」（供报错栈用）；③ 推进 `code_end` 并写入操作码。结构体里除操作码外的字段（`dst/src/retval` 等）由调用方紧接着手动填。

#### 4.2.2 核心流程：`return a+b` 会发射什么

这是本讲实践任务的核心，我们提前在概念层把它走一遍。源码 `function f(a,b){return a+b}` 的函数体 AST 大致是：

```
RETURN
  └─ ADDITION (u.operation = NJS_VMCODE_ADDITION)
       ├─ left:  NAME(a)
       └─ right: NAME(b)
```

生成顺序（深度优先，先子树后续体）：

1. `njs_generate` 命中 `NJS_TOKEN_RETURN` → `njs_generate_return_statement`：先把状态拨向「处理右子树 `a+b`」，再压续体 `njs_generate_return_statement_end`。
2. `njs_generate` 命中 `NJS_TOKEN_ADDITION` → `njs_generate_3addr_operation`：先处理左子树 `NAME(a)`，压续体 `njs_generate_3addr_operation_name`（因为左子树是 NAME），最终再压 `njs_generate_3addr_operation_end`。
3. `NAME(a)`、`NAME(b)` 经 `njs_generate_name` 解析出各自的变量 `index`（就是参数槽位），不需发射指令。
4. 回到 `njs_generate_3addr_operation_end`：发射一条 **ADDITION**（`njs_vmcode_3addr_t`），`src1 = a 的 index`，`src2 = b 的 index`，`dst = 一个新分配的临时索引`；并把该临时索引记到加法节点的 `node->index` 上。
5. 回到 `njs_generate_return_statement_end`：发射一条 **RETURN**（`njs_vmcode_return_t`），`retval = 加法节点的 index`（即第 4 步那个临时槽）。

所以函数体最终的字节码大致长这样（操作数具体十六进制值「待本地验证」，结构是确定的）：

```
ADDITION   <临时dst>   <a>   <b>     ; a + b，结果存临时槽
RETURN     <临时dst>            ; 把临时槽作为返回值
```

如果外层是脚本，全局作用域还会为函数声明先发射 `FUNCTION`（创建 lambda），并在末尾发射 `STOP`。

#### 4.2.3 源码精读

**发射宏 `njs_generate_code` 与扩容 `njs_generate_reserve`**：

[src/njs_generator.c:540-556](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L540-L556) — `njs_generate_code`：预留空间 → 记行号映射 → 推进 `code_end` → 写操作码 `_code->code = _op`。注意它**不**填 `dst/src`，那些留给具体调用点。

[src/njs_generator.c:948-986](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L948-L986) — `njs_generate_reserve`：缓冲不够时按「<1024 翻倍，否则 +50%」的策略扩容，`njs_mp_alloc` 新块、`memcpy` 搬旧数据、`njs_mp_free` 释放旧块，再更新 `code_start/code_end/code_size`。

**二元运算（三地址）的发射链**：

[src/njs_generator.c:4411-4439](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L4411-L4439) — `njs_generate_3addr_operation`：先下降处理左子树；若左子树是 NAME 走 `_name` 分支，否则直接下降右子树、压 `_end`。

[src/njs_generator.c:4442-4471](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L4442-L4471) — `njs_generate_3addr_operation_name`：当右子树有副作用时（`njs_parser_has_side_effect`），先把左操作数 `MOVE` 到一个临时槽，避免左值在求值右子树时被破坏；否则直接下降右子树。

[src/njs_generator.c:4474-4525](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L4474-L4525) — `njs_generate_3addr_operation_end`：真正发射 `njs_vmcode_3addr_t`。操作码取自 `node->u.operation`（注意 4486 行对属性访问的特殊化：若取的是字符串/小数字面量属性，升级为 `NJS_VMCODE_PROPERTY_ATOM_GET` 走 atom 快路径）；填 `src1/src2`；用 `njs_generate_dest_index` 分配 `dst`。`IN` 运算会交换两个操作数（4500 行的 `swap`）。

**`return` 的发射链**：

[src/njs_generator.c:5058-5067](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L5058-L5067) — `njs_generate_return_statement`：下降求值返回表达式 `node->right`，压续体 `_end`。

[src/njs_generator.c:5070-5144](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L5070-L5144) — `njs_generate_return_statement_end`：取返回表达式的 `index`（5081 行 `node->right->index`），若无返回值则用 `undefined` 的全局索引；5099 行发射 `NJS_VMCODE_RETURN`。注意它先 `njs_generate_lookup_block` 查是否身处 `try` 块内——若是，则不能直接 `RETURN`，而要改发 `TRY_RETURN` 以便先跑 finally（5107 行起的分支），这是 try/catch 与 return 协作的体现。

**指令结构体布局**：

[src/njs_vmcode.h:119-152](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L119-L152) — `njs_vmcode_generic_t`/`_1addr_t`/`_2addr_t`/`_3addr_t`/`_move_t` 的字段布局。

[src/njs_vmcode.h:311-320](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L311-L320) — `njs_vmcode_return_t` 与 `njs_vmcode_stop_t`，都只有 `code + retval`。

#### 4.2.4 代码实践

**实践目标**：把 4.2.2 的概念走查落到真实反汇编输出上，确认 `return a+b` 真的发射成 `ADDITION` + `RETURN`。

**操作步骤**：

1. 常规构建（`-d` 反汇编不需要 `--debug-generator`）：

   ```bash
   ./configure && make njs
   ```

2. 反汇编（`-d` 同样是内置 njs 引擎专属）：

   ```bash
   ./build/njs -d -c 'function f(a,b){return a+b}'
   ```

**需要观察的现象**：终端会按字节码偏移打印每个函数的指令序列，助记符来自反汇编器 `code_names` 表（如 `ADDITION`、`RETURN`、`STOP`、`FUNCTION`），后面跟十六进制的操作数索引。

**预期结果**：在 `f` 的字节码段里，应能看到一条 `ADDITION`（三地址：dst/src1/src2）紧接一条 `RETURN`（retval），与 4.2.2 的推导一致。全局段则有一条 `FUNCTION`（创建 `f` 的 lambda）和末尾 `STOP`。操作数十六进制与反汇编格式（是否分组、是否带函数名）「待本地验证」，下一讲 u3-l5 会专门讲怎么读这些输出。

> 若想看生成器「边生成边打印」的更细粒度轨迹（而非最终成品），改用 4.1.4 的 `--debug-generator=YES` 构建 + `-g` 运行。

#### 4.2.5 小练习与答案

**练习 1**：`njs_generate_code` 宏写入了操作码，却没有填 `dst/src`，这是为什么？
**参考答案**：因为不同指令的操作数字段不同（三地址有 `dst/src1/src2`、return 只有 `retval`、jump 有 `offset`）。宏只负责所有指令共有的「预留空间 + 记行号 + 推进指针 + 写操作码」，具体操作数由各生成函数紧接着按各自结构体类型填入，避免宏过度泛化。

**练习 2**：`njs_generate_3addr_operation_name` 在什么情况下会额外发射一条 `MOVE`？
**参考答案**：当左操作数是一个会被求值的「左值」（NAME）且**右子树有副作用**时（`njs_parser_has_side_effect(node->right)` 为真）。因为求值右子树可能改变左值指向的内容，所以先把左值搬到临时槽里固化，保证运算用的是求值前的值。

### 4.3 索引与闭包协作

#### 4.3.1 概念说明

每条运算指令都需要 `dst/src` 这些 `index`。`index` 的来源分两类：

- **变量槽**：由解析期变量分配好（参数、`var`/`let`/`const`），存在 `vm->levels[level][value]`（u3-l3）。`NAME` 节点经 `njs_generate_name` 解析后直接复用变量的 `index`，不占新槽。
- **临时槽**：运算的中间结果没有名字，需要一个临时 `index` 暂存。生成器用一个**自由表 `index_cache`** 管理：用完即归还，下次优先复用，避免 `levels` 数组无限增长。

两个关键优化/协作点：

- **`dest` 直写优化**：很多表达式「算完就赋给某个变量」，比如 `var x = a+b`。生成器会让加法直接把结果写进 `x` 的槽（`node->dest->index`），省掉一条 `MOVE`。逻辑在 `njs_generate_dest_index` 里：先释放孩子的临时索引，再看 `dest` 是否可用。
- **闭包索引**：当一个内部函数引用了外层作用域的变量（经典闭包），该变量不能只存在外层的 `levels[LOCAL]` 里，否则内层函数访问不到。njs 给这类变量分配 `NJS_LEVEL_CLOSURE` 级别的索引，并登记进 `scope->closures` 数组；运行期由调用帧机制（u4-l3）把闭包值带进内层。生成器负责「标记哪些变量需要变闭包」并把 `scope->closures` 数组接到 lambda 上。

#### 4.3.2 核心流程

**临时索引的生命周期**（LIFO 自由表）：

```
分配 njs_generate_temp_index_get:
    if index_cache 非空:  弹出一个复用              # 优先复用
    else:                  scope->items++ 分配新 LOCAL 槽
释放 njs_generate_index_release:
    把该 index 压回 index_cache                       # 等下次复用
```

由于深度优先 + 用完即还，临时槽的占用峰值远小于表达式总数。`njs_generate_children` 在下降孩子后，会压一个「释放孩子临时索引」的续体（`njs_generate_node_index_release_pop`），保证孩子算完、父节点拿到结果后及时归还。

**闭包标记流程**（以 `for` 循环里 `let` 被内层函数捕获为例）：

```
njs_generate_for_resolve_closure:
    遍历 AST（njs_parser_traverse）
    对每个 NAME 节点：
        var = 解析变量
        if 跨越了作用域边界（njs_variable_closure_test）:
            var->closure = 1                      # 标记为闭包变量
随后生成 for 循环体时：
    若循环变量是闭包，每轮迭代发射 NJS_VMCODE_LET_UPDATE 同步闭包值
```

#### 4.3.3 源码精读

**临时索引分配与自由表**：

[src/njs_generator.c:6410-6419](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L6410-L6419) — `njs_generate_node_temp_index_get`：把节点标记为 `temporary=1`，并取一个临时索引。

[src/njs_generator.c:6422-6447](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L6422-L6447) — `njs_generate_temp_index_get`：先查 `index_cache` 自由表复用（6432 行）；没有就向当前函数作用域申请新的 `NJS_LEVEL_LOCAL` 槽 `scope->items++`（6445 行）。

[src/njs_generator.c:6499-6526](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L6499-L6526) — `njs_generate_index_release`：把释放的索引压回 `index_cache`，构成 LIFO 自由表。

**`dest` 直写优化**：

[src/njs_generator.c:6361-6385](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L6361-L6385) — `njs_generate_dest_index`：先释放孩子索引（6369 行），再若 `node->dest` 有效且未被 `dest_disable`，直接返回 `dest->index`（6380 行）作为运算目标，省一条 MOVE；否则退回普通临时索引。

**孩子遍历 + 释放续体**：

[src/njs_generator.c:3086-3103](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L3086-L3103) — `njs_generate_children`：下降左子树、压「下降右子树」续体、再压「释放左子树临时索引」续体（3100 行 `njs_generate_node_index_release_pop`）。这三步的顺序保证了「左右都算完、结果都被父节点消费后，临时槽才被归还」。

**闭包标记与同步**：

[src/njs_generator.c:2142-2147](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L2142-L2147) — `njs_generate_for_resolve_closure`：用 `njs_parser_traverse` 遍历 AST，对每个 NAME 节点判断是否跨作用域引用。

[src/njs_generator.c:2119-2139](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L2119-L2139) — `njs_generate_for_resolve_closure_cb`：回调里调用 `njs_variable_closure_test`，命中则 `var->closure = 1`。

[src/njs_generator.c:2104-2110](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L2104-L2110) — 一旦标记了 `closure`，for 循环每轮迭代都会为该 `let` 发射 `NJS_VMCODE_LET_UPDATE`，把新值同步到闭包槽——这正是 JS 里 `for (let i...)` 每轮迭代有独立 `i` 的实现机制。

**闭包索引的真正创建**（在变量层）：

[src/njs_variable.c:462-476](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L462-L476) — 为闭包变量分配 `NJS_LEVEL_CLOSURE` 级别的索引，并追加进 `scope->closures` 数组。这个数组在 `njs_generate_scope` 里被接到生成器上（`scope->closures = generator->closures`，见 [src/njs_generator.c:4933-4938](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L4933-L4938)），最终存到 lambda 上（见下条）。

**嵌套函数的递归生成**：

[src/njs_generator.c:4834-4873](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L4834-L4873) — `njs_generate_function_scope`：遇到函数表达式/声明时，生成器递归调用自身，**新建一个独立的生成器实例**（有自己的 `code_start`/`closures`）、把嵌套深度 `depth+1`（上限 `NJS_FUNCTION_MAX_DEPTH=128`），生成完内层字节码后把 `lambda->start`（字节码起点）、`lambda->closures`、`lambda->nclosures`、`lambda->nlocal` 写回 lambda。这就是「每个函数有自己一段字节码 + 自己一份闭包索引表」的来源。

#### 4.3.4 代码实践

**实践目标**：观察闭包变量如何让 `for (let ...)` 每轮迭代拥有独立值。

**操作步骤**：

1. 阅读 [src/njs_generator.c:2119-2139](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L2119-L2139) 与 [src/njs_generator.c:2104-2110](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L2104-L2110)，理解 `var->closure` 标记与 `LET_UPDATE` 的关系。
2. 反汇编一段制造闭包的经典例子：

   ```bash
   ./build/njs -d -c 'var a=[]; for (let i=0;i<3;i++){ a.push(()=>i) }'
   ```

**需要观察的现象**：在循环体对应的字节码里寻找 `LET UPDATE` 之类的指令（出现在每次迭代更新 `i` 的位置），以及为箭头函数生成的 `FUNCTION` 段。

**预期结果**：能定位到 `LET UPDATE`，说明 `i` 被当作闭包变量、每轮同步。运行 `./build/njs -c 'var a=[]; for (let i=0;i<3;i++){ a.push(()=>i) }; console.log(a.map(f=>f()).join(","))'` 应回显 `0,1,2`（每轮独立），若把 `let` 换成 `var` 则回显 `3,3,3`（共享）。具体反汇编行「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：临时索引为什么用「自由表」而非「用完就忘、不断递增」？
**参考答案**：若只增不减，`levels[LOCAL]` 数组会按表达式总数线性膨胀，浪费内存。自由表让已释放的临时槽被后续表达式复用，临时槽数量只与「同一时刻活跃的中间结果数」（表达式深度）相关，而非表达式总数。

**练习 2**：`njs_generate_dest_index` 的 `dest` 直写为什么不是无条件生效？
**参考答案**：有些场景必须强制走临时索引，例如求值顺序敏感、或目标变量在求值过程中可能被改变（`dest_disable` 标志）。`njs_generate_variable_wo_dest` 会临时打开 `dest_disable` 来禁止直写，保证语义正确（比如 `let x = ...` 声明前不能被自身引用）。

## 5. 综合实践

把本讲三条主线串起来：**状态机遍历 + 指令发射 + 索引管理**。

任务：用 `--debug-generator=YES` 构建 CLI，然后跟踪下面这段代码的生成过程，并回答三个问题。

```js
function makeAdder(base) {
    return function(n) {
        return base + n;
    };
}
```

要求：

1. **状态机视角**：用 `-g` 运行（外层函数体内层函数体），描述生成器处理 `makeAdder` 函数体与内层匿名函数体的先后顺序。提示：内层函数由 [src/njs_generator.c:4834-4873](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L4834-L4873) 的 `njs_generate_function_scope` 递归生成。
2. **发射视角**：用 `-d` 反汇编，在内层函数的字节码里找出那条 `ADDITION`，确认它的 `src1/src2` 一个是参数 `n` 的索引、另一个是闭包变量 `base` 的索引（`base` 跨越了内层函数作用域，应被标记为闭包）。
3. **索引视角**：说明 `base` 为何会出现在 `makeAdder` 的 lambda 的 `closures` 数组里，结合 [src/njs_variable.c:462-476](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_variable.c#L462-L476) 与 [src/njs_generator.c:4868-4870](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L4868-L4870) 解释 `lambda->closures`/`lambda->nclosures` 的来源。

> 若无法本地构建（缺 QuickJS 库等不影响默认引擎），可改为纯源码阅读型实践：手动画出 `makeAdder` 的 AST，沿 `njs_generate` 的 `switch` 标注每个节点会调用哪个生成函数，并列出预期发射的指令序列。

## 6. 本讲小结

- 生成器 `njs_generator_t` 用「显式续体栈 + trampoline 主循环」遍历 AST，`njs_generator_next`/`njs_generator_after`/`njs_generator_stack_pop` 三原语对应「尾跳/压返回地址/弹返回地址」，与解析器是同一套手法。
- 入口 `njs_generate_scope` 申请代码缓冲、设首状态为 `njs_generate(scope->top)`、压终结续体，然后用 `do { state(); } while` 把整棵树跑完，产物封装成 `njs_vm_code_t`。
- 指令发射统一走 `njs_generate_code` 宏（预留 + 记行号 + 写操作码），缓冲由 `njs_generate_reserve` 按「翻倍/+50%」扩容；二元运算发 `njs_vmcode_3addr_t`、`return` 发 `njs_vmcode_return_t`，`return a+b` 即一条 `ADDITION` 接一条 `RETURN`。
- 临时索引靠 `index_cache` 自由表「分配—释放—复用」，`njs_generate_dest_index` 做 `dest` 直写省 MOVE，`njs_generate_children` 配合释放续体及时归还孩子临时槽。
- 跨作用域变量被标记为闭包（`var->closure`），分配 `NJS_LEVEL_CLOSURE` 索引并登记进 `scope->closures`，最终存到 lambda 上；`for (let)` 每轮用 `LET_UPDATE` 同步，正是每轮独立 `i` 的由来。
- 嵌套函数由 `njs_generate_function_scope` 递归生成，新建独立生成器实例，把内层字节码起点与闭包表写回 `lambda->start/closures/nclosures/nlocal`。

## 7. 下一步学习建议

- **下一讲 u3-l5（字节码格式与反汇编）**：本讲只用到「指令大致长什么样」，u3-l5 会系统讲变长编码、`NJS_VMCODE_*` 操作码全集、`code_names` 反汇编表，以及如何精确读 `-d` 输出里的十六进制操作数。建议把本讲的 `return a+b` 反汇编留到 u3-l5 再逐字节精读。
- **u4（字节码执行引擎）**：生成器写出的每条指令在解释器主循环 `njs_vmcode_interpreter` 里如何被取指、分发、执行；尤其是 `levels[level][value]` 如何被 `njs_scope_value` 解码，会把本讲的 `index` 寻址闭环。
- **延伸阅读**：带着「生成器发的指令、解释器怎么吃」的对照视角去读 `src/njs_vmcode.c`，你会更深刻地理解 `dst/src/retval` 这些操作数在运行期的真实含义。
