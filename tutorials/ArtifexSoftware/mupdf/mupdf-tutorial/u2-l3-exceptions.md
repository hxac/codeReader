# 异常处理：fz_try / fz_catch

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 MuPDF 的 `fz_try` / `fz_always` / `fz_catch` 三个宏是如何用 C 标准库的 `setjmp` / `longjmp` 拼出来的，以及它们展开后的 `if … do … while(0)` 结构。
- 看懂 `throw` / `fz_push_try` / `fz_do_try` / `fz_do_always` / `fz_do_catch` 五个函数共同维护的「状态机」，理解为什么 `fz_catch` 在「正常」与「抛出」两种情况下都能正确地进入或不进入。
- 解释为什么在 `fz_try` 块内被修改、又要在块外（`fz_always` / `fz_catch`）使用的局部变量，必须先用 `fz_var` 声明，否则跳转后值会丢失。
- 学会用 `fz_throw` 抛出异常、用 `fz_report_error` / `fz_caught` / `fz_caught_message` 读取并报告异常，并知道异常未被任何 `fz_try` 捕获时会发生什么。

本讲承接 [u2-l1](u2-l1-context.md)：`fz_context` 里有一个「异常栈」字段（`ctx->error`），本讲就专门拆解这套异常栈如何运作。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，C 语言本身没有异常。** 像 Java/Python 那样的 `try/catch/throw` 在 C 里不是语法特性。C 标准库只提供了两个底层跳转函数：

- `setjmp(buf)`：把「当前的 CPU 寄存器状态、栈指针」快照存进 `buf`，**第一次调用返回 0**。
- `longjmp(buf, val)`：用 `buf` 里的快照把寄存器和栈指针恢复成 `setjmp` 当时记录的样子，**程序仿佛从 `setjmp` 「第二次返回」，这次返回值是 `val`**（必须非 0）。

只要把「第一次返回 = 正常往下走」「第二次返回 = 发生了异常，跳到 catch」对应起来，就能模拟出异常。代价是：`longjmp` 会直接「撕裂」函数调用栈，中间那些还没 `return` 的函数会被强行丢弃——这一点是后面 `fz_var` 问题的根源。

**第二，异常是「状态」而不是「数据流」。** MuPDF 没有「抛出一个对象」的概念，它的异常只携带两样东西：一个**错误码**（`int code`，来自 `enum fz_error_type`）和一段**人可读的文本**（最多 256 字节）。这两样东西都存在 `ctx->error` 这个共享结构里，靠一个「栈顶指针」区分当前是哪一层 `fz_try`。

**第三，异常是「每线程一份」的。** 回顾 [u2-l1](u2-l1-context.md)：`ctx->error` 属于「非共享」字段，`fz_clone_context` 克隆出来的分身各自有独立的异常栈。这意味着你不需要为异常加锁，但也意味着异常**不能跨线程**抛——一个线程里 `fz_throw`，只能在同一个线程的 `fz_try` 里被接住。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `include/mupdf/fitz/context.h` | 定义 `fz_try`/`fz_always`/`fz_catch`/`fz_var` 四个宏、声明 `fz_throw`/`fz_rethrow` 等接口、定义错误码枚举 `enum fz_error_type` 与异常栈结构 `fz_error_context` |
| `source/fitz/error.c` | 异常机制的**全部实现**：`throw`、`fz_push_try`、`fz_do_try`、`fz_do_always`、`fz_do_catch`、`fz_vthrow`、`fz_throw`、`fz_rethrow`、`fz_report_error` 等 |
| `include/mupdf/fitz/system.h` | 把 `fz_setjmp`/`fz_longjmp`/`fz_jmp_buf` 适配到底层 `setjmp` 或更安全的 `sigsetjmp` |
| `docs/examples/example.c` | 官方示例，展示 `fz_try … fz_catch` 的标准用法 |
| `source/fitz/document.c` | `fz_open_accelerated_document` 是「`fz_var` + `fz_try` + `fz_always` + `fz_catch` + `fz_rethrow`」最完整的真实范例 |

## 4. 核心概念与源码讲解

### 4.1 异常宏的实现：用 setjmp/longjmp 模拟 try/catch

#### 4.1.1 概念说明

MuPDF 给使用者的「语法糖」是这三个宏：

```c
fz_try(ctx)
    /* 可能抛异常的代码 */
fz_always(ctx)
    /* 无论是否异常都会执行的清理代码（可选） */
fz_catch(ctx)
    /* 捕获到异常时执行的代码 */
```

它们看起来像关键字，实际只是几行 `#define`。设计目标是：**让 `fz_try` 必须配 `fz_catch`（像 C 语法那样需要分号收尾），并且 `fz_always` 可有可无**。这在 C 里靠经典的「`if … do { … } while(0)`」惯用法实现——`do { … } while(0)` 是一个「只执行一次、却像一个语句」的块，能保证宏后面跟的分号被正确消化。

#### 4.1.2 核心流程

把三个宏连起来看，宏展开后的骨架是这样（把 `BODY`/`ALWAYS`/`CATCH` 代入你写的代码）：

```c
if (!fz_setjmp(*fz_push_try(ctx)))   // 记录跳转点；正常时 setjmp 返回 0
    if (fz_do_try(ctx)) do BODY while (0);   // fz_try 的本体
if (fz_do_always(ctx)) do ALWAYS while (0);  // fz_always 的本体
if (fz_do_catch(ctx)) CATCH                   // fz_catch 的本体
```

执行流程：

1. `fz_push_try(ctx)`：在异常栈上**压入一帧**，并把该帧的 `state` 置为 `0`，返回这一帧里的 `jmp_buf` 地址。
2. `fz_setjmp(...)` **第一次返回 0** → `!0` 为真 → `fz_do_try` 检查 `state == 0` 也为真 → 执行 `BODY`。
3. `BODY` 正常跑完 → 进入 `fz_do_always`：若 `state < 3`，就把 `state` 加 1 并返回真 → 执行 `ALWAYS`。
4. 最后 `fz_do_catch`：把本帧的错误码保存到 `ctx->error.errcode`，弹出本帧，并返回 `state > 1`——**只有发生过 `longjmp` 时 `state` 才会大于 1**，此时才执行 `CATCH`。

如果 `BODY` 里调用了 `fz_throw`，底层会做 `state += 2` 再 `longjmp`，于是 `setjmp` **第二次返回非 0** → `!非0` 为假 → 直接跳过 `BODY`，落到 `fz_do_always` / `fz_do_catch`。这就是「跳到 catch」的全部秘密。

> 关键点：`fz_try` 不是一个函数调用，而是**编译期文本替换**。所以它对 C 编译器而言就是普通的 `if` 语句，`BODY` 里的局部变量和外面的局部变量处于**同一个函数栈帧**——这点在 4.2 节会变得很重要。

#### 4.1.3 源码精读

先看三个宏本身的定义。[include/mupdf/fitz/context.h:62-65](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L62-L65) 定义了 `fz_var` 与 `fz_try`/`fz_always`/`fz_catch`，这一段被作者自己戏称为「不要管幕布后面是什么」的黑盒。

紧接着 [include/mupdf/fitz/context.h:67-98](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L67-L98) 是给使用者的**权威使用说明**，里面三条规则务必记住：

- `fz_try` 块内用 `fz_throw` 抛异常；
- **不要在 `fz_always` 段里抛异常**（后面会看到原因：会触发 state 跳到 5 的分支）；
- 在 `fz_catch` 里可以用 `fz_rethrow` 把当前异常原样重新抛出——前提是中间没有再嵌套一层 `fz_try/fz_catch`。

`fz_setjmp` 本身只是对 C 标准库的薄封装。看 [include/mupdf/fitz/system.h:153-161](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/system.h#L153-L161)：如果平台有 `sigsetjmp`（信号安全的版本）就用它，否则退回普通 `setjmp`。`fz_jmp_buf` 也随之是 `sigjmp_buf` 或 `jmp_buf`。

真正控制「进入还是跳过」的是三个状态函数，全部在 [source/fitz/error.c:255-282](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/error.c#L255-L282)：

```c
int fz_do_try(fz_context *ctx) {
    return ctx->error.top->state == 0;          // state 仍是初始 0 才执行 BODY
}
int fz_do_always(fz_context *ctx) {
    if (ctx->error.top->state < 3) {            // 还没被「双倍加过」就执行 ALWAYS
        ctx->error.top->state++;
        return 1;
    }
    return 0;
}
int (fz_do_catch)(fz_context *ctx) {
    ctx->error.errcode = ctx->error.top->code;  // 把错误码存到稳定字段
    return (ctx->error.top--)->state > 1;       // 弹栈；只有 state>1（发生过 longjmp）才进 catch
}
```

注意 `fz_do_catch` 两件事一起做：先 `errcode = top->code` 把错误码搬到「不会随弹栈消失」的 `ctx->error.errcode` 字段，**然后才** `top--` 弹出本帧。这样 `fz_catch` 块内调用的 `fz_caught(ctx)` 仍能读到这个错误码（见 4.3 节）。

`fz_push_try` 在压栈前还有一道防护：[source/fitz/error.c:227-253](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/error.c#L227-L253) 检查异常栈（固定 256 帧）是否快溢出，若溢出则**伪造一次 `state=2` 的抛出**，让你直接落到 `fz_catch` 并拿到一个 `FZ_ERROR_LIMIT` 错误，而不是真的栈溢出崩溃。异常栈大小定义在 [include/mupdf/fitz/context.h:831-849](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L831-L849)（`fz_error_stack_slot stack[256]`）。

整套 state 转换规则，源码里有 [source/fitz/error.c:179-206](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/error.c#L179-L206) 这段长注释完整记录。把它整理成下表就一目了然（`+2` = `throw`，`+1` = 进入 `always`）：

| 场景 | BODY 执行 | `fz_do_always` 后的 state | 进 `fz_catch`？ |
|---|---|---|---|
| 正常，无 `always` | 是 | —（无 always） | 否（state=0，`0>1` 假） |
| 正常，有 `always` | 是 | 1 | 否（`1>1` 假） |
| BODY 内抛出，无 `always` | 中断 | — | 是（state=2，`2>1` 真） |
| BODY 内抛出，有 `always` | 中断 | 3 | 是（`3>1` 真） |
| BODY 内抛出，`always` 内又抛 | 中断 | 5（always 不重入） | 是（`5>1` 真） |

最后一行正是「`fz_always` 里别抛异常」的根因：`always` 里再 `throw`，state 会从 3 跳到 5，`fz_do_always` 因 `state >= 3` 而不再重入，逻辑虽然不会无限循环，但行为很容易让人困惑。

#### 4.1.4 代码实践

**实践目标**：用一个故意会失败的 `fz_open_document` 调用，亲眼看到「异常被 `fz_catch` 接住、程序没有崩溃」。

**操作步骤**（基于 [u1-l5](u1-l5-first-render.md) 已经能编译 example.c 的环境）：

1. 新建一个 `except_demo.c`（**示例代码**，不在仓库中），内容如下：

   ```c
   #include <mupdf/fitz.h>
   #include <stdio.h>
   #include <stdlib.h>

   int main(void)
   {
       fz_context *ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
       if (!ctx) { fprintf(stderr, "no ctx\n"); return 1; }

       fz_try(ctx)
           fz_register_document_handlers(ctx);
       fz_catch(ctx)
           fprintf(stderr, "register handlers failed\n");

       fz_document *doc = NULL;
       fz_try(ctx)
           /* 故意打开一个不存在的文件，触发 fz_throw */
           doc = fz_open_document(ctx, "definitely_not_exist.pdf");
       fz_catch(ctx)
       {
           fz_report_error(ctx);                 /* 打印错误码与文本 */
           fprintf(stderr, "caught! program still alive.\n");
       }

       fz_drop_document(ctx, doc);               /* doc 仍是 NULL，drop 容忍 NULL */
       fz_drop_context(ctx);
       return 0;
   }
   ```

2. 用与 example.c 相同的命令编译它（具体链接标志以你本地的 `build/release` 为准）：

   ```bash
   gcc -Iinclude except_demo.c -o except_demo \
       -Lbuild/release -lmupdf -lmupdf-third
   # 若提示缺 freetype 等，按 Makerules 的 SYS_* 库补齐（待本地验证）
   ./except_demo
   ```

**需要观察的现象**：终端先打印一行类似 `format error: cannot open document 'definitely_not_exist.pdf'`，再打印 `caught! program still alive.`，然后进程正常退出码 0。

**预期结果**：异常被 `fz_catch` 捕获，进程没有因为未处理错误而 `exit`。这正是对比 4.3 节「未捕获异常会让进程 `exit(EXIT_FAILURE)`」的反面教材。

> 如果你暂时无法链接成功，可改为**源码阅读型实践**：在 [source/fitz/document.c:542-564](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L542-L564) 里 `fz_open_file` 打不开文件时最终会走到 `fz_throw(ctx, FZ_ERROR_UNSUPPORTED, ...)`，对照本表理解它如何被这一层的 `fz_catch`/`fz_rethrow` 向上传递。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `fz_try` 写成下面这样，能编译通过吗？为什么？

```c
fz_try(ctx)
    do_something(ctx);
```

**答案**：不能。`fz_try` 展开含有 `if (!fz_setjmp(...)) if (fz_do_try(ctx)) do`，它**必须**有一个 `fz_catch(ctx)`（展开为 `while (0); if (fz_do_catch(ctx)) …`）来闭合那个 `do … while(0)`。缺了 `fz_catch`，`do` 就没有配对的 `while(0)`，编译器会报语法错。

**练习 2**：`fz_do_try` 为什么要返回 `state == 0` 而不是直接返回 1？

**答案**：因为发生 `longjmp` 后 `setjmp` 第二次返回非 0，外层 `if (!setjmp(...))` 为假，根本不会执行到 `fz_do_try`；`fz_do_try` 主要是在「正常路径」上再确认一次 `state` 仍是初值 0，属于双重保险，也方便 `__COVERITY__` 等静态分析工具（见源码 `#ifdef __COVERITY__` 分支直接返回 1，让分析器认为 try 块总会执行，避免误报）。

---

### 4.2 fz_var 的必要性：防止局部变量在跳转后丢失

#### 4.2.1 概念说明

这是 `setjmp/longjmp` 最经典的「坑」。C 标准明确规定（译文大意）：

> 在 `setjmp` 与对应 `longjmp` 之间被修改过、且**没有 `volatile` 限定**的自动存储期（即普通局部）变量，在 `longjmp` 返回后其值是**不确定的（indeterminate）**。

为什么会这样？因为优化器可以把一个局部变量完全放在 CPU 寄存器里，而不在内存里留副本。`setjmp` 快照的是寄存器，但「一个会被改写的局部变量」到底是「在 `longjmp` 时取它当时的寄存器值」还是「取它在 `setjmp` 时的值」，C 标准不保证——编译器甚至可能在 `longjmp` 后给你一个被覆盖过的脏寄存器值。

在 MuPDF 里这个坑尤其常见：你常常在 `fz_try` 里给一个指针赋值（比如打开一个文件流），然后在 `fz_always`/`fz_catch` 里要用这个指针去 `fz_drop_*` 清理。如果这个指针「跳转后值丢了」，清理就会用到野指针。

`fz_var` 就是官方给的解药。

#### 4.2.2 核心流程

`fz_var(x)` 的用法是在 `fz_try` **之前**对每一个「块内会改、块外要用」的局部变量各写一行：

```c
fz_stream *file = NULL;   // 先初始化
fz_var(file);             // 告诉编译器：这个变量要跨 setjmp 存活

fz_try(ctx)
    file = fz_open_file(ctx, name);   // 块内赋值
fz_always(ctx)
    fz_drop_stream(ctx, file);        // 块外/清理段仍能读到正确的 file
fz_catch(ctx)
    fz_rethrow(ctx);
```

它的工作原理**不是**给变量加 `volatile` 关键字，而是**强迫变量拥有一个内存地址**：

1. `fz_var(x)` 展开为 `fz_var_imp((void*)&(x))`——取了 `x` 的地址。
2. `fz_var_imp` 是一个**单独编译的外部函数**（不是 inline），编译器无法看到它对指针做了什么，于是必须保证 `x` 真的存在内存里、有一个稳定地址，不能只活在某个寄存器中。
3. 变量既然落在内存里，`longjmp` 恢复寄存器时就不会动到它，`fz_always`/`fz_catch` 读到的就是最新值。

#### 4.2.3 源码精读

宏与「空操作」函数。[include/mupdf/fitz/context.h:62](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L62) 定义 `#define fz_var(var) fz_var_imp((void *)&(var))`，而 [source/fitz/error.c:86-89](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/error.c#L86-L89) 里 `fz_var_imp` 函数体只有一个注释 `/* Do nothing */`——**它的全部价值就在于「被调用」这件事本身**，而不是它做了什么。这正是上一节「强迫变量落地内存」的实现。

真实范例见 [source/fitz/document.c:538-564](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L538-L564)，`fz_open_accelerated_document` 打开文档时的标准写法：

```c
fz_var(afile);
fz_var(file);
fz_var(dir);

fz_try(ctx)
{
    file = fz_open_file(ctx, filename);          // 块内赋值
    if (accel) afile = fz_open_file(ctx, accel); // 可能抛异常
    ...
    doc = handler->open(ctx, handler, file, afile, dir, state); // 也可能抛
}
fz_always(ctx)
{
    ...
    fz_drop_stream(ctx, afile);                  // 必须能拿到正确的 afile
    fz_drop_stream(ctx, file);                   // 和 file
}
fz_catch(ctx)
    fz_rethrow(ctx);
```

注意三个细节：① `fz_var` 写在 `fz_try` **之前**；② 变量在声明处**先初始化为 `NULL`**（这样即便没走到赋值就抛了，`fz_always` 里 `fz_drop_stream(ctx, NULL)` 也是安全的，因为 MuPDF 的 drop 函数都容忍 NULL）；③ `fz_always` 段只做 `fz_drop_*` 这种「不会抛异常」的清理，把可能的异常用 `fz_catch` 里的 `fz_rethrow` 向上抛。

`fz_var` 的规则可以记成一句话：**凡是「在 `fz_try` 里赋值、在 `fz_always`/`fz_catch` 里读」的局部变量，赋值前一律 `fz_var`。** 对照 [u2-l2](u2-l2-memory-refcount.md) 学过的「每个 keep/new 配一个 drop」，你会发现清理代码天然就该放在 `fz_always`，于是 `fz_var` 几乎是写健壮 MuPDF 代码的标配。

#### 4.2.4 代码实践

**实践目标**：复用 4.1.4 的 `except_demo.c`，对比「加了 `fz_var`」与「故意删掉 `fz_var`」时变量值是否丢失。

**操作步骤**：

1. 把 `except_demo.c` 改成在 `fz_try` 里用一个局部指针承载打开结果，并加 `fz_var`：

   ```c
   fz_document *doc = NULL;
   fz_var(doc);                       /* 关键：声明 doc 要跨 setjmp 存活 */
   fz_try(ctx)
       doc = fz_open_document(ctx, "definitely_not_exist.pdf");
   fz_catch(ctx)
   {
       fz_report_error(ctx);
       fprintf(stderr, "doc in catch = %p\n", (void*)doc);  /* 预期 0x0 或 0x... 仍可预测 */
   }
   ```

2. 编译并运行，记录 `doc in catch` 的输出（应当是初始化时的 `NULL`）。
3. **删掉 `fz_var(doc);` 那一行**，重新用高优化等级编译 `gcc -O2 ...`，再运行。

**需要观察的现象**：

- 有 `fz_var` 时，`doc` 的值稳定可预测（通常是 `NULL`，因为在 `fz_open_document` 抛异常前没赋值成功）。
- 无 `fz_var` 且开了 `-O2` 时，`doc` 的值**可能**变成不可预测的垃圾值（也可能碰巧仍为 0，取决于编译器/平台）。

**预期结果 / 待本地验证**：是否真的出现脏值取决于具体编译器版本和优化等级。这一现象在标准上是「未定义」，所以即使你的机器上「看起来没事」，也不代表 `fz_var` 可省——这正是为什么 MuPDF 源码里**只要符合规则就一律加 `fz_var`**，把不确定性在编译期消除。

> 若你无法稳定复现脏值，可改用 `make build=sanitize` 或 `build=memento` 构建（见 [u1-l2](u1-l2-build-system.md)）运行，这类带检测的构建更容易暴露变量被覆盖一类的问题。

#### 4.2.5 小练习与答案

**练习 1**：`fz_var_imp` 函数体什么都没做，为什么去掉 `fz_var` 还可能出问题？

**答案**：因为 `(void*)&(x)` 这个表达式**强迫 `x` 必须有内存地址**。一旦 `x` 被迫落地内存，`longjmp` 恢复寄存器快照时不会触碰内存里的 `x`，值就保住了。去掉 `fz_var` 后，优化器可能把 `x` 只放进寄存器，而 `longjmp` 后该寄存器的内容按 C 标准是「不确定」的。

**练习 2**：下面这段代码缺了什么？

```c
fz_buffer *buf = NULL;
fz_try(ctx)
    buf = fz_new_buffer(ctx, 1024);
fz_always(ctx)
    fz_drop_buffer(ctx, buf);
fz_catch(ctx)
    fz_rethrow(ctx);
```

**答案**：缺 `fz_var(buf);`（应写在 `fz_try` 之前）。虽然 `fz_new_buffer` 不太会抛异常，但只要「块内赋值、`fz_always` 里读」这条规则成立，就应当加 `fz_var`。好在 `buf` 初始化成了 `NULL`，即使值丢失，`fz_drop_buffer(ctx, NULL)` 也不会崩——但「靠 NULL 兜底」不能替代 `fz_var`，因为脏指针不一定是 NULL。

---

### 4.3 抛出与报告错误：fz_throw、throw、fz_report_error

#### 4.3.1 概念说明

一个异常从「产生」到「被看见」要经过三个动作：

- **抛出**：库内部用 `fz_throw(ctx, code, "fmt", ...)`（printf 风格）抛出一个带错误码和文本的异常；`fz_rethrow` 则把当前已捕获的异常原样再抛。
- **传递**：抛出后，异常会顺着调用栈往上跳，直到遇到最近一层 `fz_try`。中间函数不需要写任何「传递」代码——`longjmp` 自动完成。
- **读取/报告**：在 `fz_catch` 里，用 `fz_caught(ctx)` 拿错误码、`fz_caught_message(ctx)` 拿文本，或直接用 `fz_report_error(ctx)` 把它格式化打印到错误回调（默认 `stderr`）。

还有一个**安全网**：如果一个 `fz_throw` 在到达任何 `fz_try` 之前就跳到了「栈底」（即 `ctx->error.top <= ctx->error.stack_base`），MuPDF 会认为这是「未捕获异常」，直接打印 `aborting process from uncaught error!` 并 `exit(EXIT_FAILURE)`。所以**永远不要在没有外层 `fz_try` 的情况下调用可能 `fz_throw` 的函数**。

#### 4.3.2 核心流程

抛出的调用链很短：

```
fz_throw(ctx, code, fmt, ...)
   └─ fz_vthrow(ctx, code, fmt, ap)        // 格式化文本到 ctx->error.message
        └─ throw(ctx, code)                 // 改 state、记录 code、longjmp
```

`fz_vthrow` 的职责（[source/fitz/error.c:333-354](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/error.c#L333-L354)）：

1. 若 `ctx->error.errcode` 已非 0（说明上一个异常还没被处理），先 `fz_warn(ctx, "UNHANDLED EXCEPTION!")` 并 `fz_report_error` 把它冲掉——这是「一个 context 同一时刻只承载一个活动异常」的保证。
2. 若 `code == FZ_ERROR_SYSTEM`，把当前 `errno` 存进 `ctx->error.errnum`（供 `fz_caught_errno` 取用）；否则清零。
3. 用 `fz_vsnprintf` 把格式化文本写进 256 字节的 `ctx->error.message`。
4. 调 `throw(ctx, code)`。

`throw` 的职责（[source/fitz/error.c:208-225](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/error.c#L208-L225)）：

```c
static void throw(fz_context *ctx, int code) {
    if (ctx->error.top > ctx->error.stack_base) {       // 有 try 帧可跳
        ctx->error.top->state += 2;                     // 触发「发生过 longjmp」标记
        ctx->error.top->code = code;                    // 记录错误码到本帧
        fz_longjmp(ctx->error.top->buffer, 1);          // 跳回 setjmp（不再返回）
    } else {                                            // 没有任何 try：安全网
        ...; ctx->error.print(..., "aborting process from uncaught error!");
        exit(EXIT_FAILURE);
    }
}
```

注意 `throw` 是 `FZ_NORETURN`（标了 `static`，文件内可见），它**要么 `longjmp` 出去、要么 `exit`**，绝不正常返回——所以 `fz_throw` 也声明为 `FZ_NORETURN`，让静态分析器知道调用点之后的代码不可达。

报告侧的几个函数：

- `fz_report_error(ctx)`（[error.c:530-542](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/error.c#L530-L542)）：把当前错误格式化成 `"%s error: %s"`（错误类型名 + 文本）发到错误回调，然后把 `errcode` 重置为 `FZ_ERROR_NONE`。它读取的是 `ctx->error.errcode/message`，**不要求**你正处在 catch 块内——但实践中通常在 `fz_catch` 里调。
- `fz_caught(ctx)` / `fz_caught_message(ctx)`（[error.c:284-300](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/error.c#L284-L300)）：分别返回错误码与文本指针。它们必须在「最近一次 `fz_catch` 之后、且没有再嵌套新的 `fz_try/fz_catch`」时调用，否则读到的 `errcode` 可能已被覆盖。
- `fz_rethrow_if(ctx, err)` / `fz_rethrow_unless(ctx, err)`（[error.c:379-391](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/error.c#L379-L391)）：在 catch 里按错误码「选择性再抛」。例如只想兜住 `FZ_ERROR_TRYLATER`、其余继续往上抛，就 `fz_rethrow_unless(ctx, FZ_ERROR_TRYLATER)`。

错误码取值见 [include/mupdf/fitz/context.h:218-235](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L218-L235) 的 `enum fz_error_type`，对应的可读名字见 [source/fitz/error.c:393-412](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/error.c#L393-L412) 的 `fz_error_type_name`。常用的几个：

| 错误码 | 含义 | 典型场景 |
|---|---|---|
| `FZ_ERROR_SYSTEM` | 致命的内存不足或系统调用失败 | malloc 失败、`errno` 类错误 |
| `FZ_ERROR_ARGUMENT` | 参数非法或越界 | 传了空指针、页号越界 |
| `FZ_ERROR_UNSUPPORTED` | 用了不支持的特性 | 找不到对应格式 handler |
| `FZ_ERROR_FORMAT` | 不可恢复的格式/语法错误 | 文件结构损坏 |
| `FZ_ERROR_SYNTAX` | 应被诊断并忽略的语法错误 | 解析时的小毛病 |
| `FZ_ERROR_TRYLATER` | 渐进加载的「稍后再试」信号 | 内部使用 |
| `FZ_ERROR_ABORT` | 用户请求中止 | 内部使用 |

#### 4.3.3 源码精读

`fz_throw`/`fz_rethrow` 的声明在 [include/mupdf/fitz/context.h:105-107](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L105-L107)，三者都标了 `FZ_NORETURN`。`fz_throw` 的实现 [source/fitz/error.c:357-363](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/error.c#L357-L363) 只是用 `va_start` 把变参包成 `va_list` 转给 `fz_vthrow`——连 `va_end` 都省了，因为 `fz_vthrow` 不会返回。

`fz_rethrow`（[error.c:366-370](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/error.c#L366-L370)）极其简单：它不做格式化，直接用当前 `ctx->error.errcode` 调 `throw`。这正对应 4.1.3 节「`fz_do_catch` 把 `code` 存进了 `errcode`」——`fz_rethrow` 读的就是这个值。也所以「`fz_catch` 与 `fz_rethrow` 之间不能再嵌套新的 `fz_try/fz_catch`」：那会覆盖 `errcode`。

`fz_report_error` 把错误类型名通过 `fz_error_type_name` 翻译成可读串，比如 `format error: ...`、`system error: ...`。这套类型名表就是 [error.c:393-412](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/error.c#L393-L412) 的 switch。

官方示例的典型用法见 [docs/examples/example.c:59-67](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L59-L67) 与 [docs/examples/example.c:70-78](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L70-L78)，每一处都是同一个套路：

```c
fz_try(ctx)
    doc = fz_open_document(ctx, input);
fz_catch(ctx)
{
    fz_report_error(ctx);              // 1. 打印错误
    fprintf(stderr, "cannot open document\n");
    fz_drop_document(ctx, doc);        // 2. 安全清理（逆序）
    fz_drop_context(ctx);
    return EXIT_FAILURE;               // 3. 决定如何收场
}
```

这三步——**报告、清理、收场**——就是写好 `fz_catch` 的全部要点。

#### 4.3.4 代码实践

**实践目标**：在 `fz_catch` 里分别用 `fz_caught` / `fz_caught_message` 读出错误码和文本，理解它们与 `fz_report_error` 的关系。

**操作步骤**：把 4.1.4 的 `fz_catch` 块改成下面这样（**示例代码**）：

```c
fz_catch(ctx)
{
    int code = fz_caught(ctx);                 /* 读错误码 */
    const char *msg = fz_caught_message(ctx);  /* 读文本 */
    fprintf(stderr, "code=%d msg=%s\n", code, msg);
    fz_report_error(ctx);                      /* 再让库自己格式化打印一次 */
}
```

**需要观察的现象**：你的 `fprintf` 输出（如 `code=7 msg=...cannot find document handler...`）会先出现，随后是 `fz_report_error` 输出的 `unsupported error: ...`（错误类型名 7 对应 `FZ_ERROR_UNSUPPORTED`，可对照枚举表）。

**预期结果**：`code` 与枚举值一一对应，`msg` 是 `fz_vthrow` 写入的那段格式化文本；`fz_report_error` 输出的类型名与 `code` 吻合。注意调用顺序——先 `fz_caught`/`fz_caught_message`、**最后**才 `fz_report_error`，因为 `fz_report_error` 会把 `errcode` 重置为 `FZ_ERROR_NONE`，先调它会让后面的 `fz_caught` 返回 0。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `fz_throw` 标记为 `FZ_NORETURN`？调用 `fz_throw` 之后的语句会怎样？

**答案**：因为 `fz_throw` → `fz_vthrow` → `throw`，而 `throw` 要么 `longjmp` 跳走、要么 `exit`，绝不会正常返回。标记 `FZ_NORETURN` 一是为了让编译器省略「调用后的代码」、生成更优的代码；二是告诉静态分析器（如 Coverity，源码里大量 `/* coverity[+kill] */` 注释）「这之后的代码不可达」，避免误报「变量未初始化」之类警告。调用 `fz_throw` 之后的语句不会执行。

**练习 2**：在 `fz_catch` 里写 `fz_report_error(ctx); fz_rethrow(ctx);`，会怎样？

**答案**：`fz_report_error` 把 `errcode` 重置成 `FZ_ERROR_NONE`，随后 `fz_rethrow` 读 `ctx->error.errcode` 就拿到了 `FZ_ERROR_NONE`——它会以「无错误」的状态再抛一次，语义错误。正确做法是**要么** `fz_report_error`（处理掉），**要么** `fz_rethrow`（向上抛），不要在同一段里两者都调。

---

## 5. 综合实践

把本讲三个模块串起来，写一个「健壮的打开文档」函数。要求：

1. 接收一个文件名，返回 `fz_document *`，失败时返回 `NULL` 并打印错误。
2. 用 `fz_var` 保护在 `fz_try` 内赋值的指针。
3. 用 `fz_always` 做清理（即便失败也不泄漏）。
4. 在 `fz_catch` 里用 `fz_caught` 区分：若是 `FZ_ERROR_UNSUPPORTED`（不认识的格式）打印「不支持的格式」，其它错误用 `fz_report_error` 打印后 `fz_rethrow` 向上抛。

参考骨架（**示例代码**，请自行补全 include 与上下文创建）：

```c
fz_document *safe_open(fz_context *ctx, const char *name)
{
    fz_document *doc = NULL;
    fz_var(doc);                       /* 模块 4.2：保护 doc */

    fz_try(ctx)
        doc = fz_open_document(ctx, name);   /* 模块 4.1：可能 fz_throw */
    fz_always(ctx)
        ;                                /* 这里通常放 fz_drop_* 清理；doc 要返回，故不 drop */
    fz_catch(ctx)
    {
        int code = fz_caught(ctx);            /* 模块 4.3：读错误码 */
        if (code == FZ_ERROR_UNSUPPORTED)
            fprintf(stderr, "不支持的格式: %s\n", name);
        else
        {
            fz_report_error(ctx);
            fz_rethrow(ctx);                  /* 其它错误继续向上抛 */
        }
        doc = NULL;
    }
    return doc;
}
```

验证方式：用一个不存在的路径调用 `safe_open`（应进入 catch 并按错误码分支打印），再用一个真实 PDF 调用（应成功返回非空指针）。把 `fz_var(doc)` 删掉、用 `-O2` 重编，观察在高优化下是否仍稳定（参 4.2.4）。

> 说明：`fz_always` 段留空只是为了「`doc` 要被返回、不能在这里 drop」。真实库里更常见的写法见 4.2.3 的 `fz_open_accelerated_document`——那里 `fz_always` 释放的是中间资源（`file`/`afile`/`dir`），而不是最终要返回的 `doc`。

## 6. 本讲小结

- `fz_try`/`fz_always`/`fz_catch` 是三行 `#define`，用 `setjmp`（记录返回点）/ `longjmp`（跳回返回点）+ `if … do … while(0)` 拼出 try/catch 语法糖。
- 是否进入 `fz_catch` 由 `fz_error_stack_slot.state` 决定：`throw` 做 `state += 2`，进入 `always` 做 `state += 1`，`fz_do_catch` 用 `state > 1` 判断「是否发生过 longjmp」。
- `fz_var(x)` 展开为对空函数 `fz_var_imp(&x)` 的调用，**强迫 `x` 落地内存**，避免 `longjmp` 后局部变量值不确定；规则是「块内赋值、块外读取」的局部变量都要 `fz_var`，并先初始化为 `NULL`。
- 抛出链 `fz_throw → fz_vthrow → throw`：`fz_vthrow` 格式化文本与 `errno`，`throw` 改 state 并 `longjmp`；无任何 `fz_try` 时 `throw` 触发安全网 `exit(EXIT_FAILURE)`。
- 在 `fz_catch` 里用 `fz_caught`/`fz_caught_message` 读错误码与文本，用 `fz_report_error` 打印；`fz_rethrow`/`fz_rethrow_if`/`fz_rethrow_unless` 选择性向上抛，但中间不能再嵌套新的 `fz_try/fz_catch`，也不能在 `fz_rethrow` 前调 `fz_report_error`。
- 错误码来自 `enum fz_error_type`（SYSTEM/ARGUMENT/UNSUPPORTED/FORMAT/SYNTAX…），`fz_report_error` 通过 `fz_error_type_name` 把码翻译成可读前缀。

## 7. 下一步学习建议

- 本讲之后，你已经具备读懂 MuPDF 任何 `fz_try/fz_catch` 代码块的能力。建议回到 [u3-l1](u3-l1-document-abstraction.md)「文档抽象」，带着异常视角重读 `fz_open_document` 的完整实现，体会「recognize → open → 失败 throw / 成功 return」如何被 `fz_try/fz_always/fz_catch` 包裹。
- 想看异常与内存分配如何联动，可预习 [u2-l2](u2-l2-memory-refcount.md) 提到的 `fz_malloc`：它在 OOM 时会先 `fz_store_scavenge` 回收缓存再重试，仍失败才 `fz_throw(FZ_ERROR_SYSTEM)`——这是「异常 + 缓存」协作的真实例子。
- 若你对「异常栈最多 256 层」「未捕获即 exit」这两道防线感兴趣，可阅读 [source/fitz/error.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/error.c) 顶部的 `fz_default_error_callback` 与 `throw` 函数，思考在 GUI/服务端程序里如何用 `fz_set_error_callback` 把错误重定向到自己的日志系统而不是 `stderr`。
