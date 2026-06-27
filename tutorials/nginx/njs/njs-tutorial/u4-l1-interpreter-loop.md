# 解释器主循环 njs_vmcode_interpreter

## 1. 本讲目标

本讲进入 njs 引擎的「执行核心」。前面 u3 系列讲完了编译前端：源码最终变成一串字节码（vmcode）挂在 `vm->start` 上。本讲要回答的问题是——**这串字节码是如何被一条条「跑」起来的？**

学完本讲，你应当能够：

- 说清「取指 → 分发 → 执行 → 推进 PC」这个寄存器式 VM 的主循环在 njs 里是如何实现的；
- 理解 `computed goto`（标签作为数据的跳转表）相比传统 `switch` 分发为什么更快，以及 njs 如何用宏在两种分发方式之间切换；
- 看懂一条指令的操作数（operand）是如何通过 `njs_index_t` 位编码定位到 `vm->levels[][]` 数组里的值的；
- 能跟着 `NJS_VMCODE_ADDITION` 的处理分支，完整描述一次 `a + b` 在解释器内部经历了哪些步骤。

本讲只聚焦**执行引擎这一层**，不展开对象模型（u5）、函数调用帧（u4-l3）、异常细节（u4-l4）和异步（u4-l5）——它们会各自单独成篇。

## 2. 前置知识

阅读本讲前，请确认你已经具备以下认知（这些都在前置讲义中建立过，这里只做最小回顾）：

- **字节码是什么**：源码经过词法分析 → 解析 → 生成器（u3-l1～l5）之后，被翻译成一串变长指令。每条指令 = 1 字节操作码（opcode）+ 若干操作数（operand）。操作数通常是 `njs_index_t`，指向某个存值的「槽位」。详见 u3-l5。
- **index 的位编码**：一个 32 位的 `njs_index_t` 被拆成三段：`value(24 位) | type(4 位) | var_type(4 位)`。`type` 段决定值存在哪一级存储（LOCAL/CLOSURE/GLOBAL/STATIC），`value` 段是该级里的下标。详见 u3-l3。
- **返回码约定**：njs 的 C 函数用 `njs_int_t` 返回 `NJS_OK(0)` / `NJS_ERROR(-1)` / `NJS_AGAIN(-2)` / `NJS_DECLINED(-3)` 等来表示结果，而不是抛异常。在解释器里，负数返回值有特殊含义（见下文）。
- **VM 是被谁驱动的**：JS 代码不会自己启动，而是由 `njs_vm_start` 调用 `njs_vmcode_interpreter` 进入解释器，从 `vm->start` 开始执行全局字节码（u2-l1）。

> 术语提示：本讲里「解释器」「VM 执行核心」「主循环」指的都是 `njs_vmcode_interpreter` 这一个函数。它是 njs 引擎里最长、最核心的函数之一。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/njs_vmcode.c` | 解释器主循环本体，含 `njs_vmcode_interpreter` 及上百个 `CASE` 处理分支，是本讲的绝对主角。 |
| `src/njs_vmcode.h` | 字节码指令的 C 结构体定义（1addr/2addr/3addr/move/jump/return/stop…）、`NJS_VMCODE_*` 操作码枚举，以及解释器函数的声明。 |
| `src/njs_scope.h` | `njs_index_t` 的位编码宏、`njs_scope_value` / `njs_scope_valid_value` 等槽位寻址内联函数——解释器靠它们读取操作数。 |
| `src/njs_vm.h` | `NJS_LEVEL_LOCAL/CLOSURE/GLOBAL/STATIC` 枚举，以及 `njs_vm_s.levels[]` 这个四级存储指针数组的定义。 |
| `src/njs_vm.c` | `njs_vm_start`——调用解释器的入口，把 `vm->start` 交给 `njs_vmcode_interpreter`。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**分发主循环**、**computed goto 分发**、**操作数访问**。三者恰好对应「怎么循环、怎么跳转、怎么取值」三件事。

### 4.1 分发主循环：取指—执行—推进 PC

#### 4.1.1 概念说明

njs 是一台**寄存器式（register-based）虚拟机**。与栈式 VM（每条指令从操作数栈弹栈、压栈）不同，寄存器式 VM 把中间结果存在一组带编号的「寄存器/槽位」里，指令直接用编号引用操作数和结果。Java 字节码、Python 字节码偏向栈式；而 Lua、V8 Ignition、njs 都偏向寄存器式——好处是指令条数更少、临时值的搬运更少。

寄存器式 VM 的执行核心是一个**取指—分发—执行—推进**的循环：

1. **取指（fetch）**：从一个程序计数器 `pc`（指向当前字节码地址）读出 1 字节操作码。
2. **分发（dispatch）**：根据操作码跳转到对应的处理代码。
3. **执行（execute）**：读取操作数、运算、把结果写回某个槽位。
4. **推进（advance）**：把 `pc` 向前移动「本条指令的字节长度」，回到第 1 步。

在 njs 里，整个循环就实现在 `njs_vmcode_interpreter` 这一个函数里——它是一个巨大的、被 `CASE` 宏划分成上百个分支的函数体。

#### 4.1.2 核心流程

`njs_vmcode_interpreter` 的执行流程可以用下面的伪代码概括：

```
njs_vmcode_interpreter(vm, pc, rval, ...):
    把 pc 当作「待执行指令的指针」
    取出当前指令的 vmcode（读 pc 处的字节码结构）

    loop:                                    # 主循环入口
        根据 vmcode->code（操作码字节）分发
        CASE ADDITION:                       # 各操作码的处理分支
            读 operand2 -> value1
            读 operand3 -> value2
            value1 + value2 -> 写 retval
            ret = sizeof(njs_vmcode_3addr_t) # 本条指令长度
            BREAK                            # = pc += ret; 跳回 loop
        CASE STOP:
            把结果拷到 *rval
            return NJS_OK                    # 退出函数
        CASE RETURN:
            从函数返回
            return NJS_OK
        ...
        出错时:
            goto error;                      # 跳到异常展开
```

几个关键点要记住，后面源码精读都会对上：

- **`pc` 是 `u_char *`**，直接指向字节码缓冲区里的某个字节。
- **`ret`（`njs_jump_off_t`）有两种含义**：对普通指令，它是「本条指令占多少字节」，用于推进 `pc`；对跳转类指令（JUMP 等），它是「跳转偏移量」，直接用来改写 `pc`。
- **循环不是显式的 `while`**，而是靠每个分支末尾的宏「自我跳转」回到取指动作。
- **退出函数只有两条路**：`return NJS_OK`（正常结束：STOP/RETURN）或 `return NJS_ERROR`（从 `error:` 标签返回的异常）。

#### 4.1.3 源码精读

**入口与签名**。解释器的入口签名只有一行，但它承接了「从哪里开始执行」(`pc`)、「结果写哪」(`rval`)、以及 Promise/async 上下文（u4-l5 才用）：

[src/njs_vmcode.c:82-84](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L82-L84) — `njs_vmcode_interpreter` 的函数签名。

谁调用它？全局代码的执行入口 `njs_vm_start` 把编译产物 `vm->start` 当作初始 `pc` 传进来：

[src/njs_vm.c:694-702](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L694-L702) — `njs_vm_start` 调用 `njs_vmcode_interpreter(vm, vm->start, retval, NULL, NULL)`，并把 `NJS_ERROR` 之外的结果统一映射为 `NJS_OK`。

**分发总入口**。函数体开头先做一次取指，把 `pc` 转成通用指令结构 `vmcode`，然后进入分发：

[src/njs_vmcode.c:236-240](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L236-L240) — `vmcode = (njs_vmcode_generic_t *) pc;` 之后用 `SWITCH (vmcode->code)` 进入按操作码字节分发的巨型分支块。注意 `SWITCH` 是一个宏，它的展开方式取决于是否启用了 computed goto（见 4.2）。

**推进 PC 的通用机制：`BREAK` 与 `NEXT`**。每个分支处理完指令后，都要做「算出本条指令长度 → 推进 `pc` → 重新取指并分发」三件事，njs 用宏把这三步打包。在 computed goto 模式下（4.2 会展开宏本身）：

- `BREAK` = `pc += ret; NEXT;`
- `NEXT` = 重新从 `pc` 取 `vmcode`，然后 `SWITCH(vmcode->code)` 再次分发。

也就是说，`BREAK` 这个名字其实做的是「推进 PC 并跳回循环顶部」。它依赖 `ret` 已经被填好「指令长度」（对 ADDITION 是 `sizeof(njs_vmcode_3addr_t)`）或「跳转偏移」（对 JUMP 是偏移量）。

**三个退出分支**。先看最简单的 `MOVE`（纯搬运指令），它展示了「取操作数 → 写回 → 推进」的标准三段式：

[src/njs_vmcode.c:242-250](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L242-L250) — `NJS_VMCODE_MOVE`：把 `operand2` 指向的槽位值原样拷到 `operand1` 指向的槽位，然后 `pc += sizeof(njs_vmcode_move_t)` 推进，`NEXT` 回到分发。这正是反汇编里 `MOVE 0123 0133`（把 `0x0133` 的值拷到 `0x0123`）的执行体。

再看 `STOP`（全局代码执行到底的自然出口）：

[src/njs_vmcode.c:1311-1319](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1311-L1319) — `NJS_VMCODE_STOP`：读出 `operand1` 指向的值，拷给外部传入的 `*rval`，然后 `return NJS_OK` 直接退出解释器。这是脚本「跑完最后一个表达式」时的终止方式。

`RETURN`（函数返回）的退出路径类似，但走 `njs_vmcode_return` 助手做帧清理（u4-l3 详述）：

[src/njs_vmcode.c:1471-1482](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1471-L1482) — `NJS_VMCODE_RETURN`：读 `operand1` 指向的返回值，调用 `njs_vmcode_return(vm, rval, value2)` 完成返回，`return NJS_OK`。

**异常出口 `error:`**。任何分支检测到错误（例如属性查找返回 `NJS_ERROR`）都会 `goto error`。`error:` 标签不是简单地返回，而是沿调用帧链向上寻找最近的 `try/catch` 处理点（`frame->exception.catch`）：

[src/njs_vmcode.c:1842-1885](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1842-L1885) — `error:` 标签：先给异常对象附上调用栈信息，再用一个 `for(;;)` 循环沿 `vm->top_frame` 链回溯；若某一帧注册了 `catch` 指针，就把 `pc` 跳到 catch 处理代码并 `NEXT` 继续执行；若回溯到顶都没有捕获点，才 `return NJS_ERROR` 退出。

#### 4.1.4 代码实践

> 这正是本讲义规格里要求的实践任务：定位 ADDITION 与 RETURN 分支，描述一条 ADD 指令的完整执行路径。

**实践目标**：跟着源码，把反汇编里一条 `ADD a b c` 指令的执行路径完整走一遍。

**操作步骤**：

1. 用 u1-l3 的方式构建 CLI（`./configure && make njs`，产物 `build/njs`）。
2. 反汇编一段含加法的代码，观察 `ADD` 指令的样子（参考 `docs/agent/engine-dev.md` 的示例）：

   ```bash
   ./build/njs -d
   >> function f(v) { return v + 1 }
   ```

   你会看到形如 `1 | 00000 ADD 0203 0103 0233` 与 `1 | 00032 RETURN 0203` 的输出。`ADD a b c` 表示 `a = b + c`，三个十六进制数都是 `njs_index_t`。
3. 打开源码 [src/njs_vmcode.c:624-731](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L624-L731)（`NJS_VMCODE_ADDITION` 分支），对照下面这段「逐步执行路径」逐行核对。

**一条 ADD 指令的完整执行路径**（请你在源码里逐一找到对应行）：

| 步骤 | 解释器动作 | 对应源码 |
|---|---|---|
| ① 取操作数 | `njs_vmcode_operand(vm, vmcode->operand3, value2)` 读出加数 `c` | 627 行 |
| ② 取操作数 | `njs_vmcode_operand(vm, vmcode->operand2, value1)` 读出加数 `b` | 628 行 |
| ③ 原始化检查 | 若 `b` 不是原始值（如对象），先 `njs_value_to_primitive` 转原始值；`c` 同理 | 630-648 行 |
| ④ Symbol 拦截 | 若任一操作数是 symbol，报「不能转 symbol」错误并 `goto error` | 650-657 行 |
| ⑤ 取结果槽 | `njs_vmcode_operand(vm, vmcode->operand1, retval)` 拿到存放结果 `a` 的槽位指针 | 659 行 |
| ⑥ 快路径 | 若 `b`、`c` 都是数字，直接 `njs_number(b) + njs_number(c)` 写回 `retval`，`pc += sizeof(3addr)`，`NEXT` | 661-668 行 |
| ⑦ 慢路径 | 否则按字符串拼接（`"x" + 1`）等规则处理，最终 `njs_value_assign(retval, &name)` | 670-729 行 |
| ⑧ 推进 PC | 慢路径末尾的 `BREAK`（= `pc += ret; NEXT`）回到主分发 | 731 行 |

**需要观察的现象**：数字相加走 ⑥ 的快路径（`njs_fast_path`），不触发任何字符串分配；而 `"a" + 1` 必然走 ⑦ 的慢路径，触发 `njs_dtoa` 把数字格式化成字符串再拼接。

**预期结果**：你能口头复述出「取两个源操作数 →（必要时转原始值）→ 拿到目的槽位 → 数字快路径算术 / 非数字走字符串拼接 → 推进 PC」这一整条链，并且知道每一步对应 njs_vmcode.c 的哪一行。

> 待本地验证：如果你构建时带了 `--debug-opcode=YES`（见 u10-l4），可以用 `./build/njs -o script.js` 看到 `ADD` 执行时打印的助记符，从而确认它确实被调度到了 ⑥ 还是 ⑦ 路径。

#### 4.1.5 小练习与答案

**练习 1**：`STOP` 和 `RETURN` 都以 `return NJS_OK` 结束解释器，它们的作用场景有何不同？

**参考答案**：`STOP` 是**全局代码**（脚本顶层）执行到末尾的自然终止，它把「最后一个表达式的值」拷给外部的 `*rval`；`RETURN` 是**函数体**执行到 `return` 语句时的终止，它读出被返回的值并交给 `njs_vmcode_return` 做调用帧清理（恢复上一层 scope 等）。前者对应 `njs_vm_start` 的返回，后者对应一次函数调用的返回。

**练习 2**：分支里大量出现 `if (njs_slow_path(ret == NJS_ERROR)) { goto error; }`。这里的 `njs_slow_path` 是什么意思？为什么这样命名？

**参考答案**：`njs_slow_path(x)` 在 `src/njs_clang.h` 里被定义为 `njs_expect(0, x)`（等价于 `__builtin_expect(x, 0)`），即告诉编译器「`x` 大概率为假」。它是 njs 给分支预测的优化提示——错误路径很少走到，标记成 cold 让编译器把它放到代码的「冷区」，让热路径（`njs_fast_path`）更紧凑、缓存更友好。逻辑上它和普通 `if` 完全等价，只是带上了概率提示。

### 4.2 computed goto：用标签地址做分发跳转表

#### 4.2.1 概念说明

「分发」是解释器循环里最高频的动作——每条指令执行完都要分发一次。如果用最朴素的 `switch(opcode)`，CPU 每次都要先跳到 switch 那一个集中的「分发分支」，再跳到具体 case，分支预测器面对单一汇聚点容易预测失败。

**computed goto**（也叫「labels as values」，GCC/Clang 扩展）是更高效的分发方式：用一个数组存下每个 case 入口的「标签地址」（`&&label`），分发时直接 `goto *switch_tbl[opcode]`。这样每个 case 的出口都有自己的间接跳转指令，分散在各处，分支预测器能为每个 opcode 维护独立的预测历史，命中率显著提升。经典的论文实验（Ertl、Casey 等的 VM 对照）显示 computed goto 对解释器常有 10%～25% 的加速。

njs 同时支持两种分发：编译期用宏 `NJS_HAVE_COMPUTED_GOTO` 选择，两套代码并存于同一个函数里。

#### 4.2.2 核心流程

njs 的分发宏在 computed goto 模式下是这样协同的：

```
#define SWITCH(op)   goto *switch_tbl[(uint8_t) op];   # 间接跳转到标签地址
#define CASE(op)     case_##op                          # 每个 case 是一个普通标号
#define NEXT         (重新取 vmcode); SWITCH(vmcode->code)
#define NJS_GOTO_ROW(name)  [(uint8_t)name] = &&case_##name   # 填表：opcode -> 标签地址

static const void * const switch_tbl[NJS_VMCODES] = {
    [NJS_VMCODE_PUT_ARG] = &&case_NJS_VMCODE_PUT_ARG,
    [NJS_VMCODE_STOP]    = &&case_NJS_VMCODE_STOP,
    ...                                          # 84 项，与枚举一一对应
};
```

也就是说：

- `switch_tbl` 是一张「opcode 字节 → 处理代码标签地址」的跳转表，用「指定初始化」`[下标] = 值` 的写法填充，下标就是 `NJS_VMCODE_*` 枚举值（`uint8_t`）。
- `SWITCH(op)` 展开成一次间接跳转 `goto *switch_tbl[op]`，直接落到对应标号。
- 每个 `CASE(op)` 展开成一个 C 标号 `case_##op`，分支体之间是顺序排列的代码，靠各自的 `NEXT`（结尾再跳一次）形成循环。

对照「switch 模式」的差异也很有启发：那里 `SWITCH(op)` 就是普通 `switch(op)`、`CASE(op)` 就是 `case op:`、循环靠一个集中的 `next:` 标号 + `goto next` 来回跳。

#### 4.2.3 源码精读

整组分发宏被 `#if !defined(NJS_HAVE_COMPUTED_GOTO) ... #else ... #endif` 一分为二。先看 switch 版（更易读，作为理解基线）：

[src/njs_vmcode.c:117-127](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L117-L127) — 未定义 `NJS_HAVE_COMPUTED_GOTO` 时的分发宏：`SWITCH=switch`、`CASE=case`、`NEXT` 落到集中的 `next:` 标号（`NEXT_LBL` 定义该标号）。

再看 computed goto 版（生产环境默认走的快路径）：

[src/njs_vmcode.c:128-139](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L128-L139) — 启用 computed goto 时的宏定义：`SWITCH(op)` = `goto *switch_tbl[(uint8_t) op]`，`CASE(op)` = `case_##op`，并定义了 `NJS_GOTO_ROW` 用来填表。

跳转表本体是一张与操作码枚举一一对应的常量数组，共 84 项（`PUT_ARG=0` 到 `DEBUGGER`）：

[src/njs_vmcode.c:141-228](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L141-L228) — `static const void * const switch_tbl[NJS_VMCODES]`，每一行 `NJS_GOTO_ROW(NJS_VMCODE_xxx)` 把一个操作码映射到 `&&case_NJS_VMCODE_xxx` 标签地址。注意它是 `const`，意味着只读、可放只读段，运行期不会被改写。

操作码枚举本身在头文件里，顺序与跳转表严格一致（`PUT_ARG=0` 起隐式递增，末项 `NJS_VMCODES` 作总数哨兵）：

[src/njs_vmcode.h:29-116](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L29-L116) — `NJS_VMCODE_*` 枚举定义，跳转表的下标空间就是它定义的。

> 小知识：computed goto 是 GCC/Clang 的语言扩展（ISO C 没有「取标签地址」），所以必须用 `NJS_HAVE_COMPUTED_GOTO` 这类特性检测宏来守护，确保在不支持的编译器上回退到 switch。njs 的构建系统（auto/，见 u1-l2）会在 configure 阶段探测编译器是否支持该扩展并定义此宏。

#### 4.2.4 代码实践

**实践目标**：验证你机器上构建出来的 `build/njs` 到底用了哪种分发，并理解「为什么两种实现可以共存于一个函数」。

**操作步骤**：

1. 阅读上面三段源码，确认两种分发宏互斥（由一个 `#if/#else` 隔开）。
2. 查构建期是否定义了 `NJS_HAVE_COMPUTED_GOTO`。可以在源码里搜它在哪里被定义：

   ```bash
   grep -rn "NJS_HAVE_COMPUTED_GOTO" auto/ src/
   ```

   （这条命令仅用于阅读，不修改任何文件。）
3. 对比两种分发在「循环靠什么回到取指」上的差异：switch 版靠集中的 `next:` 标号，computed goto 版靠每个分支末尾各自的间接跳转。

**需要观察的现象 / 预期结果**：你会看到 `NJS_HAVE_COMPUTED_GOTO` 是 configure 在探测到 GCC/Clang 时定义的；在支持的计算上，函数最终编译出「每个 case 结尾一条独立的 `jmp *` 间接跳转」，而不是一条集中的 switch 跳转。

> 待本地验证：精确的反汇编对照需要 `objdump -d build/njs` 看 `njs_vmcode_interpreter` 的机器码，这超出本讲范围，可作为进阶练习。

#### 4.2.5 小练习与答案

**练习 1**：为什么 computed goto 比 switch 快？用一句话概括核心原因。

**参考答案**：因为 computed goto 让每个 opcode 的处理代码末尾都有**自己独立的间接跳转指令**（分散的 `jmp *switch_tbl[op]`），分支预测器能为每个 opcode 单独维护预测历史；而 switch 把所有分发汇聚到一个集中的跳转指令，所有 opcode 共享同一条预测资源，预测失败率高。

**练习 2**：`switch_tbl` 为什么声明成 `static const void * const`？两个 `const` 各自约束什么？

**参考答案**：`void * const` 表示「指针本身不可改」（数组元素指向固定标签地址），外层 `const`（`void * const` 前的）表示「数组内容整体只读」；`static` 让它只在当前编译单元可见、避免每次进函数重新初始化。合起来保证这张表在编译期就填好、运行期只读、且不随函数调用重复构造。

### 4.3 操作数访问：从 index 到 levels 槽位

#### 4.3.1 概念说明

前面两个模块讲了「怎么循环、怎么跳转」，这个模块回答最后一个关键问题：**分支体里那句 `njs_vmcode_operand(vm, vmcode->operand2, value1)` 到底是怎么把一个 `njs_index_t` 变成 `njs_value_t *` 的？**

回顾 u3-l3、u3-l5：指令的操作数是一个 32 位 `njs_index_t`，它用位编码同时携带「存在哪一级存储」和「该级里的下标」：

\[ \text{index} = \underbrace{\text{value}(24\,\text{位})}_{\text{槽位下标}} \;\big|\; \underbrace{\text{type}(4\,\text{位})}_{\text{存储层级}} \;\big|\; \underbrace{\text{var\_type}(4\,\text{位})}_{\text{变量类型}} \]

而运行期，VM 维护着一个**四级存储指针数组** `vm->levels[NJS_LEVEL_MAX]`：

- `levels[NJS_LEVEL_LOCAL]` —— 当前函数帧的局部变量；
- `levels[NJS_LEVEL_CLOSURE]` —— 闭包捕获的父帧变量；
- `levels[NJS_LEVEL_GLOBAL]` —— 全局变量；
- `levels[NJS_LEVEL_STATIC]` —— 静态/绝对作用域（内建常量等）。

每一级都是一段连续的 `njs_value_t *` 数组（每个槽位是一个指向 16 字节值的指针，见 u2-l2）。给定一个 `index`，只要拆出 `type` 段选定某一级、拆出 `value` 段作下标，就能定位到那个槽位。这就是「寄存器」在 njs 里的物理实现。

#### 4.3.2 核心流程

操作数访问的标准流程是：

```
njs_vmcode_operand(vm, index, out_ptr)
    └─> out_ptr = njs_scope_valid_value(vm, index)
            └─> value = njs_scope_value(vm, index)
                    └─> return vm->levels[ type(index) ][ value(index) ]
            └─> 若该槽位尚未初始化(invalid):
                    - 若变量是 let/const (TDZ) -> 报 ReferenceError, 返回 NULL
                    - 否则懒初始化为 undefined
            └─> return value
    └─> 若 out_ptr == NULL: goto error   # (TDZ 报错)
```

两个关键设计：

1. **一次位运算即可定位**：`njs_scope_value` 内部就是两次移位 + 两次数组下标，没有任何哈希查找——这正是寄存器式 VM 快的原因。
2. **TDZ（暂时性死区）检查内嵌在取值里**：`njs_scope_valid_value` 会在取值时顺便判断「这个 let/const 槽位是不是在被声明前就被访问了」，若是就报 `ReferenceError`。所以 `njs_vmcode_operand` 不只是「读指针」，还兼任了 TDZ 守卫。

#### 4.3.3 源码精读

**取操作数的入口宏**。分支里频繁出现的 `njs_vmcode_operand(vm, index, _retval)` 是个 do-while 宏，它把「读值 + TDZ 守卫」打包：

[src/njs_vmcode.c:73-79](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L73-L79) — `njs_vmcode_operand` 宏：调用 `njs_scope_valid_value` 取槽位指针，若返回 `NULL`（TDZ 违规）直接 `goto error`。

**位编码常量**。`njs_index_t` 的三段位宽在头文件里写死，这是整个 index 机制的「契约」：

[src/njs_scope.h:10-23](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L10-L23) — 位段定义：`VAR_SIZE=4`、`TYPE_SIZE=4`、`VALUE_SIZE=24`，以及对应的掩码；并据此算出 `value` 段最多能编 \(2^{24}-1\) 个槽位（`NJS_SCOPE_VALUE_MAX`）。

**拆位内联函数**。三个内联函数分别取出 `value` 段、`type` 段、`var_type` 段，全是纯移位：

[src/njs_scope.h:56-75](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L56-L75) — `njs_scope_index_var`（低 4 位）、`njs_scope_index_type`（中 4 位）、`njs_scope_index_value`（高 24 位）。

**定位槽位**。把上面三段组合起来，`njs_scope_value` 一行就把 index 翻译成指针：

[src/njs_scope.h:78-83](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L78-L83) — `njs_scope_value`：`return vm->levels[njs_scope_index_type(index)][njs_scope_index_value(index)]`。这就是「寄存器」的物理地址。

**TDZ 守卫版**。`njs_scope_valid_value` 在 `njs_scope_value` 之上加了「未初始化」处理：

[src/njs_scope.h:86-104](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_scope.h#L86-L104) — `njs_scope_valid_value`：若槽位是 invalid 且变量类型 `<= NJS_VARIABLE_LET`（即 const/let），抛 `ReferenceError "cannot access variable before initialization"` 返回 `NULL`；否则把空槽位懒填成 undefined 后返回。

**四级存储的定义**。最后看 `levels` 这个数组和它四级下标的来源：

[src/njs_vm.h:109-115](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L109-L115) — `njs_level_type_t` 枚举：`LOCAL=0, CLOSURE=1, GLOBAL=2, STATIC=3`（`MAX` 作哨兵）。

[src/njs_vm.h:118-124](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L118-L124) — `njs_vm_s` 结构体里 `njs_value_t **levels[NJS_LEVEL_MAX]`——四级存储指针数组的定义。函数调用时（u4-l3），解释器会把 `levels[NJS_LEVEL_LOCAL]` 指向新帧的局部数组，从而实现「换一组寄存器」。

> 闭环回顾：u3-l3 讲了解析期如何给变量分配 `index`，u3-l5 讲了字节码里如何把 `index` 写进指令操作数，本讲 4.3 讲了运行期如何用同一个 `index` 把值取出来。三讲合起来，正是「变量从声明到执行」的完整链路。

#### 4.3.4 代码实践

**实践目标**：把反汇编里一个十六进制 index 拆解成「层级 + 变量类型 + 槽位号」，亲手走一遍 `njs_scope_value` 的运算。

**操作步骤**：

1. 反汇编一个简单脚本，取一行带操作数的指令，例如（参考 `docs/agent/engine-dev.md`）：

   ```
   1 | 00000 MOVE     0123 0133
   ```

   `0x0133` 就是源操作数的 `njs_index_t`（十六进制）。
2. 把 `0x0133` 按位段拆开（位宽见 4.3.3）：
   - 低 4 位（var_type）= `0x0133 & 0xF` = `0x3`
   - 中 4 位（type）= `(0x0133 >> 4) & 0xF` = `0x3`
   - 高 24 位（value）= `0x0133 >> 8` = `0x1`
3. 对照 [src/njs_vm.h:109-115](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L109-L115) 的层级枚举：`type=3` 对应 `NJS_LEVEL_STATIC`；对照 u3-l3 的变量类型枚举：`var_type=3` 对应 `NJS_VARIABLE_VAR`。
4. 于是这条 `MOVE` 的含义就是「把 `levels[STATIC][1]` 的值拷到目的槽位」——即把一个静态作用域的常量搬进当前帧。

**需要观察的现象 / 预期结果**：你能对任意一条指令的 hex 操作数做「拆三段 → 查两张枚举表 → 得到物理位置」的解码，不再觉得那些十六进制数是黑盒。

> 待本地验证：你可以在 `./build/njs` 里跑一个更明确的脚本（如 `var a = 42;`），把反汇编里的 `MOVE` 源操作数拆开，验证它确实指向 STATIC 级的内建常量槽位（因为字面量 `42` 会被放进静态作用域）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `njs_scope_valid_value` 在槽位「未初始化」时，要区分 `var_type <= NJS_VARIABLE_LET` 两种处理？

**参考答案**：因为 `let`/`const` 有 **TDZ（暂时性死区）** 语义——在声明语句真正执行前访问它们必须抛 `ReferenceError`；而 `var` 没有这个限制（它被提升且默认值是 undefined）。`njs_scope_valid_value` 用 `var_type <= NJS_VARIABLE_LET`（const=0, let=1）一刀切地识别 TDZ 变量，对它们报错返回 `NULL`；对其他类型则把空槽位懒填成 undefined。这也是为什么 const/let 的枚举值被刻意排在最前（u3-l3 提到过这个排序）。

**练习 2**：一次 `njs_vmcode_operand(vm, index, out)` 调用，最坏情况下会引发什么副作用？

**参考答案**：最坏情况是该 index 指向一个 TDZ 变量（let/const 未初始化），此时 `njs_scope_valid_value` 会调用 `njs_reference_error` 往 `vm->exception` 上挂一个错误对象并返回 `NULL`；宏随即 `goto error` 跳到异常展开（4.1.3 的 `error:` 标签）。也就是说，「读一个操作数」在最坏路径上可能直接触发异常抛出与帧回溯。

## 5. 综合实践

把本讲三个模块串起来，做一个「**一条 `return a + b` 指令的全链路追踪**」小任务。

**任务背景**：函数 `function f(a, b) { return a + b }` 编译后，函数体字节码大致是 `ADD <dst> <a> <b>` 紧跟 `RETURN <dst>`（参考 `docs/agent/engine-dev.md` 的 `ADD a b c` / `RETURN` 示例）。

**要求你完成**：

1. **构建并反汇编**：用 `./build/njs -d`（或 `./build/njs -d script.js`）得到 `f` 的字节码，抄下 `ADD` 和 `RETURN` 两条指令及其 hex 操作数。
2. **拆 index**：用 4.3.4 的方法，把 `ADD` 的三个操作数分别拆成 `层级 / 变量类型 / 槽位号`，判断 `a`、`b` 各属于哪一级存储（函数参数通常是 LOCAL 级）。
3. **走执行路径**：对照 [src/njs_vmcode.c:624-731](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L624-L731) 的 ADDITION 分支，标出 `f(2, 3)` 这一次调用走的是「数字快路径」（6.⑥）还是「字符串慢路径」（6.⑦），并说明依据。
4. **验证分发与退出**：说明 ADDITION 分支末尾的 `BREAK`（4.1）如何把 `pc` 推进到 `RETURN` 指令，再由 RETURN 分支（[src/njs_vmcode.c:1471-1482](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1471-L1482)）以 `return NJS_OK` 退出本次函数执行。
5. **（可选进阶）** 构建 `--debug-opcode=YES` 版本，用 `./build/njs -o` 跑 `f(2,3)`，观察是否如你预测的那样打印 `ADD` 后紧跟 `RETURN`。

**预期结果**：你能用一张时序图把「`njs_vmcode_interpreter` 进入 f 的字节码 → 取 ADD 指令 → 用 `njs_vmcode_operand`+`njs_scope_value` 从 levels 取出 a、b → 数字快路径相加写回 dst 槽 → BREAK 推进 → 取 RETURN → 退出」整条链画出来。这张图就是 njs 解释器最核心的工作模型。

> 待本地验证：步骤 1-4 在阅读层面即可完成；步骤 5 的实际输出取决于本地是否带 `--debug-opcode=YES` 构建。

## 6. 本讲小结

- njs 的执行核心是 `njs_vmcode_interpreter` 这一个巨大函数，它实现了一个**取指 → 分发 → 执行 → 推进 PC** 的循环；`pc` 指向当前字节码，`ret` 表示本条指令长度（或跳转偏移），`BREAK` 宏负责「`pc += ret` 后回到分发」。
- 退出解释器只有两条路：`STOP`/`RETURN` 的 `return NJS_OK`（正常结束），以及 `error:` 标签的 `return NJS_ERROR`（异常）——`error:` 会沿调用帧链寻找最近的 try/catch 处理点。
- 分发有两种实现，由编译期宏 `NJS_HAVE_COMPUTED_GOTO` 切换：朴素的 `switch` 分发，以及更快的 **computed goto**（用 `switch_tbl[]` 跳转表存标签地址，`goto *switch_tbl[op]` 间接跳转），后者让每个 opcode 有独立的跳转指令，利于分支预测。
- 指令操作数是 32 位 `njs_index_t`，按 `value(24) | type(4) | var_type(4)` 编码；运行期 `njs_scope_value` 用两次移位 + 两次数组下标就能定位到 `vm->levels[type][value]` 槽位——这是「寄存器」的物理实现。
- 取操作数的 `njs_vmcode_operand` 宏包了 `njs_scope_valid_value`，除了定位槽位还兼任 **TDZ 守卫**：对未初始化的 let/const 抛 `ReferenceError`。
- 本讲与 u3-l3（解析期分配 index）、u3-l5（字节码写 index）形成闭环，共同覆盖了「变量从声明到执行」的完整链路。

## 7. 下一步学习建议

本讲只讲了「单条指令在单层帧里怎么执行」，刻意回避了几个会显著增加复杂度的主题。建议按顺序继续：

- **u4-l2 levels/scope/index：作用域寻址**——本讲 4.3 已经摸到了 index 的位编码，u4-l2 会把 LOCAL/CLOSURE/GLOBAL/STATIC 四级存储在函数调用、闭包场景下如何切换讲透。
- **u4-l3 函数调用与调用帧**——本讲里 RETURN 的帧清理、`error:` 的帧回溯都依赖 `njs_native_frame_t`/`njs_frame_t` 调用帧链，u4-l3 专门讲它。
- **u4-l4 异常处理**——本讲的 `error:` 标签只是异常机制的入口，try/catch/finally 在字节码层如何跳转、`vm->exception` 如何存取，留待 u4-l4。
- **u4-l5 Promise 与 async/await**——本讲开头的 `njs_vmcode_interpreter` 签名里有 `promise_cap`/`async_ctx` 两个参数，它们如何驱动异步执行，是 u4-l5 的主题。

继续阅读建议：先把本讲的 `MOVE`/`ADDITION`/`STOP`/`RETURN` 四个分支当成「样板」，再去读 `src/njs_vmcode.c` 里其余上百个 `CASE` 分支——它们的套路完全一致，只是操作语义不同。
