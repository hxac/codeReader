# 异常处理：try/catch/throw 与错误对象

## 1. 本讲目标

本讲拆解 njs 引擎里 JavaScript 异常机制的全部实现。学完后你应当能够：

- 说清 `try` / `catch` / `finally` / `throw` 在字节码层到底变成了哪几条指令，以及它们如何靠「跳转偏移 + 调用帧上的 catch 地址」完成捕获与回溯。
- 说清一个被抛出的值在运行期存在哪里（`vm->exception`），以及解释器主循环如何用 `error:` 标签沿调用帧链寻找最近的 `catch`。
- 说清 `Error` / `TypeError` / `RangeError` / `SyntaxError` 等「错误家族」是如何用一个统一的「错误类型编号」串起来的，以及 `njs_vm_error` 一族宏背后的 `njs_vm_error2`。

本讲是上一讲 [u4-l3 函数调用与调用帧](u4-l3-function-call-frames.md) 的直接延续：异常回溯完全建立在调用帧链（`top_frame` / `previous`）之上，所以请确认你已经熟悉 `njs_frame_t` 与 `njs_native_frame_t` 的关系再往下读。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，JS 的异常是一次「非局部跳转」。** 普通指令按 `pc` 顺序往下走；而 `throw` 要能够跳出当前函数、跳出调用者，一路向上，直到遇到一个愿意接住它的 `catch`。这意味着异常机制必须能「跨函数地改写执行位置」，而且不能依赖正常的返回路径。

**第二，异常需要一个「当前持有的值」。** 抛出的不一定是 `Error` 对象，也可以是任意值（`throw "boom"` 合法）。引擎必须在某处记住「正在传播的这个值是什么」，直到它被某个 `catch` 取走。njs 把这个位置放在 VM 的一个字段 `vm->exception` 上。

**第三，错误对象是一族「带原型链的内建对象」。** `TypeError`、`RangeError` 等并不是互不相干的类，它们共享 `Error.prototype`，只是在 `name` 和原型链上不同。njs 用一个连续的枚举段（`NJS_OBJ_TYPE_ERROR` … `NJS_OBJ_TYPE_AGGREGATE_ERROR`）给它们编号，让「创建一个某类型的错误」退化为一次「按编号查原型」。

如果你还记得 u4-l1 讲过的解释器主循环只有两个正常出口——`STOP`/`RETURN` 走 `return NJS_OK`，其它一切异常都汇聚到 `error:` 标签——那么本讲就是在回答：到达 `error:` 之后，引擎怎么决定「该跳去哪」。

## 3. 本讲源码地图

本讲涉及的关键文件与职责：

| 文件 | 作用 |
|---|---|
| `src/njs_vmcode.h` | 定义异常相关字节码指令的**操作码枚举**与**指令结构体**（`njs_vmcode_try_start_t` 等）。 |
| `src/njs_vmcode.c` | 解释器主循环里对这些指令的**分发处理**，以及 `njs_vmcode_try_start/try_end/finally/error` 等**实现函数**和 `error:` 回溯逻辑。 |
| `src/njs_vm.c` | `vm->exception` 的读写：`njs_vm_throw` / `njs_vm_exception` / `njs_vm_exception_get`，以及按编号造错的 `njs_vm_error2`。 |
| `src/njs_vm.h` | 错误类型枚举 `NJS_OBJ_TYPE_*` 与原型访问宏 `njs_vm_proto`。 |
| `src/njs_error.c` / `src/njs_error.h` | `Error` 家族的**创建**（`njs_error_new` / `njs_error_alloc`）与一族**便捷抛错宏**（`njs_type_error` 等）。 |
| `src/njs_function.h` | 调用帧上的 `njs_exception_t`（catch 地址链）与 `njs_frame_s`。 |
| `src/njs.h` | 对外暴露的 `njs_vm_error` 一族宏与 `njs_vm_exception_get` 声明。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 异常指令**（try/catch/finally/throw 在字节码层的样子与执行）、**4.2 exception 存储**（异常值如何挂在 `vm->exception` 上并被回溯与读取）、**4.3 Error 家族**（错误类型编号与构造器的关系）。

---

### 4.1 异常指令：TRY_START / THROW / CATCH / FINALLY

#### 4.1.1 概念说明

JavaScript 的 `try { ... } catch (e) { ... } finally { ... }` 看起来是一整块语法，但在字节码层它被拆成一组**带跳转偏移的指令**。njs 为此定义了专门的操作码（见 `NJS_VMCODE_*` 枚举里 `TRY_START` 一段）：

- `TRY_START`：标记 try 块开始，登记「如果在这个块里发生异常，跳到哪个 catch/finally」。
- `THROW`：显式 `throw expr;`。
- `TRY_END`：标记 try 块结束，撤销刚才登记的 catch 地址。
- `CATCH`：放在 catch 块（或 finally 块）开头，负责把当前异常值取出来交给用户变量。
- `FINALLY`：放在 finally 块结尾，统一处理「正常结束 / break / continue / return」四种退出方式。
- `TRY_BREAK` / `TRY_CONTINUE` / `TRY_RETURN`：try 块**内部**出现 `break`/`continue`/`return` 时发射的「跳板」指令。
- `ERROR`：编译期就已知的错误（如 `ReferenceError: x is not defined`），直接把「错误类型 + 文案」编码进指令。

核心思想是：**异常处理 = 在调用帧上维护一个「catch 地址」，throw 时跳到它**。try 块进入时设置 catch 地址，结束时撤销；嵌套的 try 用链表保存外层 catch 地址。

#### 4.1.2 核心流程

一段 `try { throw "boom"; } catch(e){ ... }` 在字节码层大致是这样布局（`<offset>` 表示跳板要跳到的相对位置）：

```
TRY_START   exception_slot, exit_slot, <到 catch 的 offset>
            ...... try 块体 ......
THROW       value_slot            ; throw "boom" → 设置 vm->exception，goto error
TRY_END     <offset>              ; try 正常结束，撤销 catch 地址
CATCH       exception_slot, <到 finally/末尾 的 offset>
            ...... catch 块体 ......
FINALLY     retval, exit_slot, continue_off, break_off
```

执行流程：

1. `TRY_START` 把「catch 指令的地址」记到当前调用帧 `frame->exception.catch`，并把两个临时槽 `exception_slot`、`exit_slot` 初始化为「无效值」。
2. try 块体正常跑完时，`TRY_END` 撤销 catch 地址，控制流自然落入后面的代码。
3. 如果 try 体内（或其调用的函数内）发生异常，解释器跳到 `error:` 标签，沿调用帧链向上找第一个 `catch != NULL` 的帧，把 `pc` 设成那个 catch 地址，重新分发——于是执行流「瞬移」到了 `CATCH` 指令。
4. `CATCH` 把 `vm->exception` 取出存进用户变量 `e`，然后根据自身 `offset` 决定是直接结束（无 finally）还是把 catch 地址改指向 finally 块。
5. `FINALLY` 最后统一收尾：判断 exit 槽里的标记，决定是正常往下、跳到 break/continue 目标、还是完成一次 return。

#### 4.1.3 源码精读

先看指令结构体定义。异常指令的操作数大多是「跳转偏移 + 若干索引槽」：

[njs_vmcode.h:323-328](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L323-L328) — `TRY_START` 的结构体 `njs_vmcode_try_start_t`，字段依次是 `offset`（到 catch/finally 的跳转偏移）、`exception_value`（临时槽，存被捕获的异常）、`exit_value`（临时槽，存「退出原因」标记）。

[njs_vmcode.h:338-342](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L338-L342) — `CATCH` 的结构体 `njs_vmcode_catch_t`，`offset` 用来判断本 catch 后面有没有 finally、`exception` 是要写入的用户变量索引。

[njs_vmcode.h:365-371](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L365-L371) — `FINALLY` 的结构体 `njs_vmcode_finally_t`，携带 `retval`、`exit_value`，以及 `continue_offset` / `break_offset` 两个跳转目标。

[njs_vmcode.h:345-348](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L345-L348) — `THROW` 的结构体 `njs_vmcode_throw_t`，只有一个 `retval`（要抛出的值所在槽）。

[njs_vmcode.h:374-381](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L374-L381) — `ERROR` 的结构体 `njs_vmcode_error_t`，把一个 `njs_object_type_t type` 和一段文案（`name` 或 `message`）直接嵌进指令，运行时再物化成错误对象。

再看主循环里的分发。注意每条异常指令的处理末尾几乎都 `goto error`——这与 u4-l1 讲的「异常统一汇聚到 `error:` 标签」一致：

[njs_vmcode.c:1687-1695](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1687-L1695) — `THROW` 分支：取出要抛出的值，调用 `njs_vm_throw(vm, value2)` 把它写进 `vm->exception`，然后 `goto error` 进入回溯。

[njs_vmcode.c:1674-1685](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1674-L1685) — `TRY_START` 分支：解析出 `exception_value` 槽与跳转 `offset`，交给 `njs_vmcode_try_start()` 登记 catch 地址。

[njs_vmcode.c:1733-1750](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1733-L1750) — `CATCH` 分支：用 `njs_vm_exception(vm)` **取出**当前异常写入用户变量；随后判断 `offset`——若等于 `sizeof(njs_vmcode_catch_t)` 说明这是个「没有 finally」的 catch，调 `njs_vmcode_try_end` 撤销 try 块；否则把 `frame->exception.catch` 改指向 finally 块开头（这样若 catch 体里又抛错，会落到 finally）。

[njs_vmcode.c:1752-1768](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1752-L1768) — `FINALLY` 分支：调用 `njs_vmcode_finally()` 统一收尾，返回 `NJS_OK` 即正常退出本层，返回 `NJS_ERROR` 则继续 `goto error`。

接着看实现函数。`njs_vmcode_try_start` 揭示了「嵌套 try」如何用链表保存外层 catch 地址：

[njs_vmcode.c:2773-2806](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2773-L2806) — 若当前帧已有 catch 地址（说明外层也有 try），就 `njs_mp_alloc` 一个 `njs_exception_t` 把旧的存起来挂在 `frame->exception.next` 上；然后把 `frame->exception.catch` 设成 `pc + offset`（即 catch/finally 指令地址），并把 `exception_value`、`exit_value` 两个槽初始化为「无效 + 数字 0」。`njs_set_invalid` 配合 `njs_number(...) = 0` 是 njs 复用同一块内存表示「无效标记 + 退出码」的惯用法（回顾 u2-l2 讲过的 16 字节值表示）。

[njs_vmcode.c:2847-2865](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2847-L2865) — `njs_vmcode_try_end`：弹出 catch 地址。若 `next` 非空就恢复外层 catch，否则置 `NULL`。

[njs_vmcode.c:2876-2926](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2876-L2926) — `njs_vmcode_finally` 是 finally 语义的核心：先看 `exception_value` 是否有效（try 块无 catch、异常一路传到 finally 时会被重新装回 `exception_value`），有效就 `njs_vm_throw` 重新抛出；否则看 `exit_value`——有效值表示 `return`（调 `njs_vmcode_return` 完成返回）；数字 `>0` 表示 `break`（用 `break_offset`）、`<0` 表示 `continue`（用 `continue_offset`）；`0` 表示正常结束，直接返回本指令长度继续往下走。

[njs_vmcode.c:2929-2942](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2929-L2942) — `njs_vmcode_error`：把编译期嵌好的 `njs_vmcode_error_t` 翻译成一次抛错。`REF_ERROR` 走 `njs_reference_error` 带上变量名，其余类型走 `njs_throw_error` 带上文案。

最后是回溯主逻辑 `error:` 标签（u4-l1 提过它，这里展开看异常如何被 catch 接住）：

[njs_vmcode.c:1842-1886](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1842-L1886) — 进入 `error:` 后，若 `vm->exception` 是错误对象，先记下当前 `pc` 并调用 `njs_error_stack_attach` 附上调用栈；随后进入 `for(;;)` 循环沿 `top_frame -> previous` 链向上爬：对每一个非原生（字节码 lambda）帧，检查 `frame->exception.catch`，非空就把 `pc` 设为它并 `NEXT`（重新分发，于是跳到那条 `CATCH` 指令）；若一路爬到链尾仍无 catch，则 `return NJS_ERROR`（异常未被捕获，交给上层 VM 处理）。这正解释了「异常能跨函数向上跳」——它其实在沿调用帧链找 catch 地址。

> 补充：生成器侧如何发射这些指令见 [njs_generator.c:5675-5730](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c#L5675-L5730) 的 `njs_generate_try_statement`，它会申请 `exception_index` 与 `exit_index` 两个临时槽并填进 `TRY_START`，是理解「两个槽从哪来」的入口。

#### 4.1.4 代码实践

**实践目标**：用 CLI 反汇编亲眼看到 try/catch 的跳转结构。

**操作步骤**：

1. 按 u1-l3 构建 CLI（`./configure && make njs`，得到 `build/njs`）。
2. 把下面这段写进 `try.js`：
   ```js
   try {
       throw "boom";
   } catch (e) {
       console.log(e);
   }
   ```
3. 运行 `./build/njs -d try.js`（`-d` 触发反汇编，见 u3-l5）。

**需要观察的现象**：输出里应能看到形如 `TRY START ...`、`THROW ...`、`TRY END ...`、`CATCH ...`、`TRY FINALLY ...` 的若干行。这些助记符由反汇编器打印，格式见 [njs_disassembler.c:420-431](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L420-L431)（`TRY START`）与 [njs_disassembler.c:470-480](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L470-L480)（`CATCH`）、[njs_disassembler.c:493-505](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L493-L505)（`TRY FINALLY`）。

**预期结果**：`TRY START` 行末尾的 `offset` 正好等于「从 TRY_START 跳到 CATCH 那一行的字节距离」；`CATCH` 行的第二个数（offset）应等于 `sizeof(njs_vmcode_catch_t)`（因为本例没有 finally，走的是「直接 try_end」分支）。

> 由于本例没有 finally 块，反汇编里**可能不会**出现 `TRY FINALLY` 行；若想稳定看到它，请把例子改成带 `finally { }` 的版本再重跑。具体十六进制偏移随构建而变，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果在一个 `try` 块里再嵌一个 `try` 块，外层的 catch 地址会丢失吗？

**答案**：不会。`njs_vmcode_try_start` 检测到当前帧已有 catch 地址时，会 `njs_mp_alloc` 一个 `njs_exception_t`，把旧的 `{catch}` 存进去挂在 `frame->exception.next` 上（[njs_vmcode.c:2784-2793](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2784-L2793)）；内层 try 结束时 `njs_vmcode_try_end` 再从链表里恢复外层地址（[njs_vmcode.c:2856-2862](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2856-L2862)）。

**练习 2**：`finally` 块既要处理「正常结束」，又要处理 `break`/`continue`/`return`，它是靠哪一个槽区分这四种情况的？

**答案**：靠 `TRY_START` 初始化的 `exit_value` 槽（[njs_vmcode.c:2800-2803](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2800-L2803)）：`TRY_BREAK` 把它设为「无效 + 数字 1」、`TRY_CONTINUE` 设为「无效 + 数字 -1」、`TRY_RETURN` 设为一个有效返回值、正常结束保持「无效 + 数字 0」。`njs_vmcode_finally` 据此分流（[njs_vmcode.c:2896-2923](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2896-L2923)）。

---

### 4.2 exception 存储：vm->exception 与回溯读取

#### 4.2.1 概念说明

指令解决了「往哪跳」，但「被抛出的那个值」需要一个存放处。njs 的设计极简：VM 结构体里有一个 `njs_value_t exception` 字段，**任意时刻最多只有一个正在传播的异常值**。这符合 JS 的单线程语义——异常是串行处理的，不存在两个并发异常。

围绕这个字段有三类操作：

- **写入**：`njs_vm_throw(vm, value)` 把一个值设为当前异常；`njs_throw_error` / `njs_vm_error2` 等则「创建错误对象并直接写进 `vm->exception`」。
- **取出（破坏性）**：`njs_vm_exception(vm)` 读出异常并把字段清空——因为一旦被 catch 接住，这个异常就「消费」掉了。
- **对外读取**：`njs_vm_exception_get(vm, retval)` 是给宿主（如 NGINX 模块）用的包装，在 `njs_vm_start` 返回 `NJS_ERROR` 后取走异常值。

#### 4.2.2 核心流程

异常值的生命周期：

```
throw err  ──►  njs_vm_throw(vm, err)  ──►  vm->exception = *err
                                              │
                  (解释器 error: 回溯，沿调用帧链找 catch)
                                              │
                  到达 CATCH 指令  ──►  *user_var = njs_vm_exception(vm)
                                              │  (取出并清空 vm->exception)
                                              ▼
                                    catch 体里 err 可用，异常已消费
```

关键点：`njs_vm_exception` 是**破坏性读取**——它 `njs_set_invalid(&vm->exception)` 把字段清掉。所以同一个异常不会被消费两次。若 finally 块需要「重新抛出未捕获的异常」，`njs_vmcode_finally` 会先把那个值从 `exception_value` 槽里 `njs_vm_throw` 回去（[njs_vmcode.c:2886-2890](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2886-L2890)）。

异常值挂在 VM 而非调用帧上，这意味着**异常是「跨帧的单值」**：它在某一帧被抛出，可能在外层帧被捕获，中间穿越的帧只负责在回溯时被弹出（`njs_vm_scopes_restore` + 释放帧，见 `error:` 标签循环）。

#### 4.2.3 源码精读

[njs_vm.c:813-817](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L813-L817) — `njs_vm_throw`：实现只有一行 `vm->exception = *value;`，纯赋值（用 `*value` 解引用而非 `njs_value_assign`，因为目标是结构体字段、不存在别名双关问题）。

[njs_vm.c:752-761](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L752-L761) — `njs_vm_exception`：先拷贝 `vm->exception` 到局部变量，再 `njs_set_invalid(&vm->exception)` 清空，最后返回拷贝。这就是「破坏性读取」的实现。

[njs_vm.c:764-768](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L764-L768) — `njs_vm_exception_get`：只是 `*retval = njs_vm_exception(vm)`，给外部 C 宿主用的薄封装。`njs.h` 的文档注释明确指出：当 `njs_vm_start` 等返回 `NJS_ERROR` 时，用 `njs_vm_exception_get` 取异常值（[njs.h:110](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L110)、[njs.h:346](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L346)）。

回溯时对 `vm->exception` 的判定见 [njs_vmcode.c:1844-1848](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1844-L1848)：只有当 `njs_is_error(&vm->exception)`（即抛出的是一个 Error 对象）时才记录 `pc` 并附上调用栈——这解释了为什么 `throw "字符串"` 不会带 stack 信息，而 `throw new TypeError(...)` 会。

最后看「调用帧上的 catch 地址」如何承载回溯：

[njs_function.h:66-71](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.h#L66-L71) — `njs_exception_t` 结构体：`{ next, catch }`。`catch` 是一个 `u_char *`（指向某条 `CATCH` 指令的字节码地址），`next` 串起嵌套 try 的外层 catch。

[njs_function.h:74-80](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.h#L74-L80) — `njs_frame_s`：每个字节码 lambda 帧都内嵌一个 `njs_exception_t exception`。注意它**只存在于字节码帧**（`native` 帧没有 try/catch），所以回溯循环里要先判 `!native->native`（[njs_vmcode.c:1853](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1853)）。

#### 4.2.4 代码实践

**实践目标**：跟踪一次「跨函数」的异常传播，理解 `vm->exception` 是单值、跨帧的。

**操作步骤**（源码阅读型实践）：

1. 读 [njs_vmcode.c:1687-1695](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1687-L1695) 的 `THROW` 分支：确认它只是 `njs_vm_throw` + `goto error`，并没有指明「跳到哪个 catch」。
2. 读 [njs_vmcode.c:1850-1881](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1850-L1881) 的回溯循环：注意它沿 `native->previous` 逐帧向上，对每一帧检查 `frame->exception.catch`。
3. 构造心智模型：函数 A 里有 `try`，A 调用函数 B，B 里 `throw`。此时 B 的帧没有 catch，循环会 `njs_vm_scopes_restore` 弹掉 B 的帧（[njs_vmcode.c:1871-1876](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1871-L1876)），继续爬到 A 的帧，发现 A 有 catch，于是 `pc = catch` 跳回 A 的 `CATCH` 指令。

**需要观察的现象**：`vm->exception` 在整个回溯过程中**值不变**（只是帧在不断弹出），直到被 `CATCH` 里的 `njs_vm_exception` 取走。

**预期结果**：你能用自己的话讲清「为什么 `throw` 不需要知道 catch 在哪——回溯循环会替它找到」。这一步无需运行命令，是纯阅读理解。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `njs_vm_exception` 取出异常后要立刻 `njs_set_invalid` 清空字段？

**答案**：因为异常是「单值、一次性消费」的。一旦被某个 catch 接住，传播就结束了；若不清空，下一次无异常的回溯可能误读到这个旧值。`njs_is_valid` / `njs_is_error` 等检查都依赖该字段在无异常时处于 invalid 状态。

**练习 2**：`throw "oops"` 和 `throw new TypeError("oops")` 在回溯阶段的处理有何不同？

**答案**：进入 `error:` 后，只有当 `njs_is_error(&vm->exception)` 为真（即抛出的是 Error 对象）时，才会执行 `vm->active_frame->native.pc = pc` 与 `njs_error_stack_attach`（[njs_vmcode.c:1844-1848](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1844-L1848)）。所以抛字符串不会附调用栈，抛 Error 对象才会。

---

### 4.3 Error 家族：错误类型编号与构造器

#### 4.3.1 概念说明

JavaScript 有一族标准的错误构造器：`Error`、`TypeError`、`RangeError`、`SyntaxError`、`ReferenceError`、`EvalError`、`URIError`，外加 njs 自己的 `InternalError`（内存错误等）和 `AggregateError`。它们都是「以 `Error.prototype` 为根的原型链上的对象」，区别只在 `name` 和原型。

njs 用一个**连续的枚举段**给它们编号，让「创建一个某类型错误」变成「按编号从 `vm->prototypes[]` 数组里取出原型 → 用它物化一个对象」。这个编号有两套等价的「身份」：

- **绝对编号**：`NJS_OBJ_TYPE_ERROR`、`NJS_OBJ_TYPE_TYPE_ERROR` 等，是 `njs_object_type_t` 枚举值，用来索引 `vm->prototypes[]`。
- **相对编号**：以 `NJS_OBJ_TYPE_ERROR` 为 0 的偏移量（`TypeError` 是 6、`RangeError` 是 3 ……），就是 `njs_vm_error2(vm, error_type, ...)` 里的那个 `error_type`。

这两套编号通过一句 `error_type += NJS_OBJ_TYPE_ERROR` 互相转换。

#### 4.3.2 核心流程

一次「抛出一个 TypeError」的标准路径（从便捷宏到对象物化）：

```
njs_type_error(vm, "...")            ; 或 njs_vm_type_error 宏
        │
        ▼
njs_vm_error2(vm, 6, "...")          ; 6 = 相对编号
        │  error_type += NJS_OBJ_TYPE_ERROR   ; 6 → NJS_OBJ_TYPE_TYPE_ERROR
        ▼
njs_throw_error_va(vm, njs_vm_proto(vm, TYPE_ERROR), fmt, args)
        │  njs_vsprintf 把文案格式化进 buf[NJS_MAX_ERROR_STR]
        ▼
njs_error_new(vm, &vm->exception, proto, buf, len)
        │  njs_string_create → njs_error_alloc(proto) → njs_set_object
        ▼
vm->exception 现在持有一个 TypeError 对象
```

注意最后一步：错误对象被**直接写进 `vm->exception`**（[njs_error.c:40](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_error.c#L40)）。也就是说「抛错」与「造错」在这条路径上是合并的——造完即抛。

原型查询用的是一个宏：`njs_vm_proto(vm, type)` 展开为 `&vm->prototypes[type].object`（[njs_vm.h:146](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L146)），即按编号从 VM 的原型数组里取。回顾 u2-l1 讲过的 `shared` 与克隆：这些原型在 `njs_vm_protos_init` 阶段被物化，克隆 VM 会复用模板的 shared 原型。

#### 4.3.3 源码精读

先看错误类型枚举段：

[njs_vm.h:69-79](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L69-L79) — `NJS_OBJ_TYPE_ERROR` 起的连续枚举：`ERROR, EVAL_ERROR, INTERNAL_ERROR, RANGE_ERROR, REF_ERROR, SYNTAX_ERROR, TYPE_ERROR, URI_ERROR, MEMORY_ERROR, AGGREGATE_ERROR`，并以 `NJS_OBJ_TYPE_ERROR_MAX = NJS_OBJ_TYPE_AGGREGATE_ERROR` 作上界哨兵。注意它们在枚举里是**连续排列**的，这正是「相对编号」能成立的前提。

相对编号 ↔ 绝对编号的转换枢纽：

[njs_vm.c:820-833](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L820-L833) — `njs_vm_error2`：先做越界检查 `error_type > (NJS_OBJ_TYPE_ERROR_MAX - NJS_OBJ_TYPE_ERROR)`（防止传入超范围编号导致数组越界），然后 `error_type += NJS_OBJ_TYPE_ERROR` 转成绝对编号，最后用 `njs_vm_proto(vm, error_type)` 取原型并调 `njs_throw_error_va`。

便捷宏（注意相对编号就是这些宏传给 `njs_vm_error2` 的第二个参数）：

[njs.h:76-87](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L76-L87) — 一族对外宏：`njs_vm_error`→0、`njs_vm_internal_error`→2、`njs_vm_range_error`→3、`njs_vm_ref_error`→4、`njs_vm_syntax_error`→5、`njs_vm_type_error`→6。对照上面的枚举：0 即 `ERROR`、2 即 `INTERNAL_ERROR`、3 即 `RANGE_ERROR`、4 即 `REF_ERROR`、5 即 `SYNTAX_ERROR`、6 即 `TYPE_ERROR`，完全吻合。

内核侧另有一族**直接传绝对编号**的宏（不经 `njs_vm_error2`，直接调 `njs_throw_error(vm, NJS_OBJ_TYPE_*, ...)`）：

[njs_error.h:11-26](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_error.h#L11-L26) — `njs_error` / `njs_eval_error` / `njs_internal_error` / `njs_range_error` / `njs_reference_error` / `njs_syntax_error` / `njs_type_error` / `njs_uri_error`，每个都展开成一次带具体 `NJS_OBJ_TYPE_*` 的 `njs_throw_error`。这族宏覆盖更全（含 `eval_error`、`uri_error`），是内核代码里最常见的抛错入口。

造错与抛错的实现：

[njs_error.c:44-52](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_error.c#L44-L52) — `njs_throw_error`：先用 `njs_vm_proto(vm, type)` 取原型，再委托 `njs_throw_error_va`。

[njs_error.c:32-41](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_error.c#L32-L41) — `njs_throw_error_va`：用 `njs_vsprintf` 把格式化文案写进栈缓冲 `buf[NJS_MAX_ERROR_STR]`，然后调 `njs_error_new(vm, &vm->exception, proto, buf, len)`——注意第二个参数是 `&vm->exception`，即错误对象会被直接装进异常字段。

[njs_error.c:11-30](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_error.c#L11-L30) — `njs_error_new`：把文案建成字符串，调 `njs_error_alloc` 物化错误对象，最后 `njs_set_object(dst, error)` 写回目标。因为传入的 `dst` 是 `&vm->exception`，所以这一步等价于「抛出」。

错误家族作为内建构造器的注册由 `njs_builtin.c` 完成，例如：

[njs_builtin.c:789-812](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_builtin.c#L789-L812)（节选） — 把 `Error`、`EvalError`、`RangeError`、`SyntaxError`、`TypeError` 等构造器按 `NJS_OBJ_TYPE_*` 注册进 shared 层，并建立原型链（`TypeError.prototype.__proto__ === Error.prototype`）。这部分回顾 u5-l3 内建对象注册会更清楚。

最后，`NJS_VMCODE_ERROR` 指令里的 `type` 字段用的也是这套 `NJS_OBJ_TYPE_*` 编号（见 4.1.3 引用的 [njs_vmcode.c:2929-2942](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2929-L2942)）——这就是「编译期已知的错误」与「运行期抛出的错误」共用同一套类型编号的体现。

#### 4.3.4 代码实践

**实践目标**：亲手把 `njs.h` 的便捷宏与枚举编号对应起来，验证「相对编号 + NJS_OBJ_TYPE_ERROR = 绝对编号」。

**操作步骤**（源码阅读型实践）：

1. 打开 [njs.h:76-87](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L76-L87)，记下每个宏传给 `njs_vm_error2` 的数字。
2. 打开 [njs_vm.h:69-79](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L69-L79)，数清每个 `NJS_OBJ_TYPE_*_ERROR` 相对 `NJS_OBJ_TYPE_ERROR` 的偏移。
3. 把两张表并排比对。

**需要观察的现象/预期结果**：你会得到下面这张映射表（请自行核对）：

| 便捷宏（njs.h） | 相对编号 | 绝对枚举（njs_vm.h） |
|---|---|---|
| `njs_vm_error` | 0 | `NJS_OBJ_TYPE_ERROR` |
| `njs_vm_internal_error` | 2 | `NJS_OBJ_TYPE_INTERNAL_ERROR` |
| `njs_vm_range_error` | 3 | `NJS_OBJ_TYPE_RANGE_ERROR` |
| `njs_vm_ref_error` | 4 | `NJS_OBJ_TYPE_REF_ERROR` |
| `njs_vm_syntax_error` | 5 | `NJS_OBJ_TYPE_SYNTAX_ERROR` |
| `njs_vm_type_error` | 6 | `NJS_OBJ_TYPE_TYPE_ERROR` |

注意相对编号 1（`EVAL_ERROR`）在 `njs.h` 里**没有**对应宏——要抛 EvalError 得用 `njs_error.h` 里的 `njs_eval_error`。这是一个容易踩坑的细节。

**进阶（可选，待本地验证）**：构建 CLI 后运行 `./build/njs -c 'try { null.foo } catch(e){ console.log(e.constructor.name) }'`，预期打印 `TypeError`——这验证了「属性访问失败抛出的是 `NJS_OBJ_TYPE_TYPE_ERROR`」。具体错误文案格式待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`njs_vm_error2` 为什么要先做 `error_type > (NJS_OBJ_TYPE_ERROR_MAX - NJS_OBJ_TYPE_ERROR)` 检查？

**答案**：因为 `error_type` 是相对编号，随后要 `+= NJS_OBJ_TYPE_ERROR` 变成绝对编号去索引 `vm->prototypes[]`（经 `njs_vm_proto`）。若传入超范围编号（如 100），加上基址后会越界访问数组。这个检查是边界保护（[njs_vm.c:825-827](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L825-L827)）。

**练习 2**：`njs_type_error(vm, "x")` 调用之后，错误对象在哪里？还要不要再调一次 `njs_vm_throw`？

**答案**：不需要。`njs_type_error` → `njs_throw_error` → `njs_throw_error_va` → `njs_error_new(vm, &vm->exception, ...)`，错误对象被直接写进 `vm->exception`（[njs_error.c:40](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_error.c#L40)），所以「造错」即「抛错」。这与 `njs_vm_throw`（仅赋值，用于抛出已存在的值）是两条不同入口。

**练习 3**：为什么 `throw {}`（普通对象）不会带 `.stack`，而 `throw new Error()` 会？

**答案**：回溯进入 `error:` 后，仅当 `njs_is_error(&vm->exception)` 为真（即它是 Error 家族对象）才会调 `njs_error_stack_attach`（[njs_vmcode.c:1844-1848](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1844-L1848)）。普通对象不满足该判断，故不附栈。

---

## 5. 综合实践

把本讲三个模块串起来：**用反汇编追踪一次「跨函数、带 finally、抛 TypeError」的完整异常路径**。

把下面这段写进 `ex.js`：

```js
function boom() {
    return null.x;        // 触发 TypeError
}

function outer() {
    try {
        boom();
        return "ok";      // 不会执行
    } catch (e) {
        console.log("caught:", e.message);
        return "caught";
    } finally {
        console.log("finally");
    }
}

outer();
```

任务：

1. **反汇编**：`./build/njs -d ex.js`，在输出里找到 `outer` 对应的字节码，确认它包含 `TRY START`、`CATCH`、`TRY FINALLY` 三类指令，并记下 `TRY START` 的 `offset` 指向哪一行（应指向 `CATCH`）。
2. **跑通语义**：直接 `./build/njs ex.js`，确认输出依次是 `caught: ...`、`finally`（注意 finally 在 return 之后仍会执行）。
3. **类型核对**：把 `catch (e)` 体改成 `console.log(e.constructor.name)`，重跑确认打印 `TypeError`，呼应 4.3 的类型编号映射。
4. **源码对应**：对照 [njs_vmcode.c:1842-1886](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1842-L1886) 的回溯循环，说出 `boom()` 帧被弹出、控制权回到 `outer` 的 `CATCH` 的全过程。

> 说明：具体反汇编十六进制偏移与错误文案细节随构建而变，**待本地验证**；本实践的重点是确认「指令种类」与「执行顺序」与源码逻辑一致。

## 6. 本讲小结

- njs 把 `try/catch/finally/throw` 编译成一组带跳转偏移的字节码指令（`TRY_START/THROW/TRY_END/CATCH/FINALLY` 及 break/continue/return 跳板），catch 地址登记在调用帧的 `frame->exception.catch` 上，嵌套 try 用 `njs_exception_t` 链表保存外层。
- 被抛出的值存在 `vm->exception` 这一**单值字段**里；`njs_vm_throw` 写入、`njs_vm_exception` 破坏性读出、`njs_vm_exception_get` 给宿主用。回溯靠 `error:` 标签沿 `top_frame->previous` 链向上找第一个非空 catch。
- `finally` 用 `exit_value` 槽里的「无效 + 数字」编码区分正常结束 / break / continue / return 四种退出方式，统一在 `njs_vmcode_finally` 里分流。
- `Error` 家族用连续枚举段 `NJS_OBJ_TYPE_ERROR … AGGREGATE_ERROR` 编号；`njs_vm_error2` 把「相对编号 + NJS_OBJ_TYPE_ERROR」转成绝对编号，再用 `njs_vm_proto` 取原型物化错误对象，且造完直接写进 `vm->exception`（造错即抛错）。
- 只有抛出真正的 Error 对象时才会附调用栈（`njs_is_error` 判定），抛字符串或普通对象不会。
- 异常回溯完全建立在 u4-l3 讲的调用帧链之上：异常穿越的中间帧会被 `njs_vm_scopes_restore` 弹出，直到落在带 catch 的帧。

## 7. 下一步学习建议

- 下一讲 [u4-l5 Promise 与 async/await 的 VM 支持](u4-l5-promise-and-async.md) 会用到本讲的 `vm->exception` 与 `error:` 回溯——`async/await` 的「拒绝」本质就是把一个 rejected promise 当作异常抛进 await 点，建议带着「异常如何跨函数传播」的认知去读。
- 想加深对回溯与调用帧弹出的理解，可重读 [u4-l3 函数调用与调用帧](u4-l3-function-call-frames.md) 的 `njs_vm_scopes_restore` 部分，对照本讲 `error:` 循环里的同一调用。
- 对错误对象的原型链与注册机制感兴趣的读者，可跳读 [u5-l3 内建对象与构造器的注册](u5-l3-builtin-registration.md)，看 `Error` 家族是如何在 shared 层被物化的。
