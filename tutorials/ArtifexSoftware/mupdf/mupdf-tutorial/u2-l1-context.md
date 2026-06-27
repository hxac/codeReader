# fz_context：一切调用的入口

## 1. 本讲目标

在上一讲（u1-l5）里，你已经用 `example.c` 跑通了 MuPDF 的「标准三步」：创建 context → 注册 handler → 打开文档。那时我们只把 `ctx` 当成一个「必须传、但先别管它是什么」的黑盒。本讲要打开这个黑盒。

学完本讲你应该能够：

- 说清楚 `fz_context` 内部到底装了哪些东西（异常栈、各类子上下文、缓存）。
- 解释 `fz_new_context(alloc, locks, max_store)` 三个参数各自的含义，以及「store 上限 256MB」该传什么值。
- 描述一个 context 从被创建、被克隆、到被销毁的完整生命周期，理解 `master` / `context_count` 的作用。
- 写出一个最小且「正确释放」的 context 创建程序。

---

## 2. 前置知识

本讲假设你已经读过 u1-l5，了解以下概念（不展开，只提醒）：

- **context**：全局状态容器，是几乎所有 fitz 函数的第一个参数 `ctx`。
- **fz_try / fz_catch**：MuPDF 基于 `setjmp/longjmp` 的异常机制（下一讲 u2-l3 会深入，本讲只需知道 context 里为它预留了栈空间）。
- **标准三步**：`fz_new_context` → `fz_register_document_handlers` → `fz_open_document`。

再补充两个本讲会用到的 C 语言背景：

- **函数指针表 / 回调**：把「要做什么」抽象成一组函数指针，由调用方在运行时填入具体实现。MuPDF 的内存分配和加锁都用这种方式，这样库本身就不绑定任何具体平台。
- **引用计数（reference counting）**：每个对象带一个计数器，`keep` 加 1、`drop` 减 1，减到 0 才真正释放。context 的「克隆/销毁」计数就是用类似思路管理一族 context 的。

---

## 3. 本讲源码地图

本讲只盯住两个文件，外加两个辅助文件佐证：

| 文件 | 作用 |
| --- | --- |
| [`include/mupdf/fitz/context.h`](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h) | `fz_context` 结构定义、`fz_new_context` 宏、`fz_alloc_context` / `fz_locks_context` / 各类错误与回调的公共声明。本讲的「契约」。 |
| [`source/fitz/context.c`](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c) | `fz_new_context_imp`、`fz_clone_context`、`fz_drop_context` 的实现，以及 style/tuning 子上下文。本讲的「实现」。 |
| [`source/fitz/memory.c`](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c) | 默认分配器 `fz_alloc_default` 与默认锁 `fz_locks_default`，即传 `NULL` 时实际使用的后备实现。 |
| [`docs/examples/example.c`](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c) | 上一讲用过的示例，本讲引用其中 `fz_new_context` / `fz_drop_context` 的真实调用位置。 |

> 定位口诀：找「context 长什么样」看头文件 `.h`，找「context 怎么造、怎么销毁」看实现文件 `.c`。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 context 结构与子上下文** —— 它内部到底装了什么。
2. **4.2 fz_new_context 参数** —— 三个参数怎么填、`256MB` 怎么传。
3. **4.3 context 的生命周期** —— 创建、克隆、销毁的全过程。

---

### 4.1 context 结构与子上下文

#### 4.1.1 概念说明

MuPDF 用纯 C 写成。C 语言没有「全局对象」这一层语言支持，但 MuPDF 又必须在多线程下安全运行、又必须能被多个进程/库共存。它的解决办法是：**把所有「本该是全局」的状态，打包进一个结构体 `fz_context`，然后要求每个函数都把它作为第一个参数传进来**。这也是为什么几乎每个 fitz 函数签名都是 `f(..., fz_context *ctx, ...)` 或 `f(fz_context *ctx, ...)`。

这个结构体里大致装了三类东西：

- **基础设施**：内存分配器 `alloc`、锁 `locks` —— 让库可以换底层 malloc、可以适配不同线程库。
- **异常机制**：`error`（异常栈）、`warn`（警告缓冲）—— 支撑 `fz_try / fz_catch`。
- **各类子上下文与缓存**：字体 `font`、颜色空间 `colorspace`、字形缓存 `glyph_cache`、资源存储 `store`、文档处理器表 `handler` 等等。

> 直觉：你可以把 `fz_context` 想象成一个「工具箱」，里面有装内存的格子、装锁的格子、装异常栈的格子、装各种缓存的格子。fitz 函数每次被调用，都先从这个工具箱里取出自己需要的格子。

#### 4.1.2 核心流程

`struct fz_context` 的字段按用途可以分成五组，理解时按这五组记即可：

```
struct fz_context
├─ ① 身份与计数      : user / master / context_count / next_document_id
├─ ② 基础设施        : alloc / locks
├─ ③ 异常与日志      : error / warn / activity
├─ ④ 非共享(每线程独有): aa / seed48 / icc_enabled
└─ ⑤ 子上下文指针
     ├─ 当前未共享   : handler / archive / style / tuning
     └─ 共享(克隆时共用): font / hyph / colorspace / store / glyph_cache
```

「共享 vs 非共享」是本讲最重要的一组区分：

- **非共享**字段在 `fz_clone_context` 时会被各自独立维护（最典型的是 `error` 异常栈——每个线程必须有自己的异常栈，否则一个线程的 `longjmp` 会跳进另一个线程）。
- **共享**字段（`font` / `colorspace` / `store` / `glyph_cache` 等）在一族 context 之间共用同一个指针，从而多个线程能共用同一份缓存，这正是多线程渲染能加速的关键（详见 u9-l2）。

#### 4.1.3 源码精读

先看头文件对各个子上下文的**前向声明**——因为 `fz_context` 只持有它们的指针，所以只需要先声明类型名，不必暴露内部结构：

[include/mupdf/fitz/context.h:35-45](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L35-L45) 用 `typedef struct xxx xxx;` 把九个子上下文类型先声明出来，最后才声明 `fz_context` 自身。这就是「context 持有一组子上下文指针」的源头。

接着是结构体本体。注意源码里用注释把字段分成「unshared」「shared」两段，与我们上面的分组一致：

[include/mupdf/fitz/context.h:885-928](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L885-L928) 这是 `struct fz_context` 的完整定义。重点看几处：

- `master` / `context_count`（[context.h:894-897](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L894-L897)）：`master` 指向「祖先」context；若指向自己，说明它自己就是祖宗。注释明确解释了三种取值的含义。
- `alloc` / `locks`（[context.h:902-903](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L902-L903)）：基础设施，直接内嵌结构体而非指针。
- 异常栈 `error`（[context.h:904](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L904)）：其类型 `fz_error_context` 内含一个固定大小的栈 `stack[256]`，见 [context.h:838-849](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L838-L849)。这就是 context 体积较大的原因——它把异常栈直接内联进去了。
- 共享子上下文段（[context.h:921-927](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L921-L927)）：`font` / `hyph` / `colorspace` / `store` / `glyph_cache` 都是指针，克隆时会被多个 context 指向同一份。

#### 4.1.4 代码实践

**实践目标**：建立对 context 字段分组的直觉。

**操作步骤**：

1. 打开 [include/mupdf/fitz/context.h:885-928](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L885-L928)。
2. 准备一张表，把结构体里每个字段填进下面三类之一：
   - **基础设施**（alloc / locks）
   - **非共享 / 每线程独有**（含异常栈、抗锯齿参数 `aa`、随机种子 `seed48` 等）
   - **共享子上下文**（注释标了 `/* shared contexts */` 的那一段）

**需要观察的现象**：

- `error`、`warn`、`aa` 都是「直接内嵌的结构体」，不是指针——说明它们必然每线程一份。
- `font`、`store`、`glyph_cache` 都是「指针」，且位于 `/* shared contexts */` 注释下——说明它们可以被一族 context 共用。

**预期结果**：你会得出一张类似下表的归类（节选）：

| 字段 | 类型 | 归类 |
| --- | --- | --- |
| `alloc` | `fz_alloc_context`（内嵌） | 基础设施 |
| `locks` | `fz_locks_context`（内嵌） | 基础设施 |
| `error` | `fz_error_context`（内嵌，含栈） | 非共享 |
| `aa` | `fz_aa_context`（内嵌） | 非共享 |
| `store` | `fz_store *`（指针） | 共享 |
| `glyph_cache` | `fz_glyph_cache *`（指针） | 共享 |

> 本步为「源码阅读型实践」，无需运行程序；它的产出是一张表，目的是让你之后看任何 fitz 函数时，能立刻判断它用到的状态是线程独有还是共享的。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fz_error_context`（异常栈）必须是「非共享」的，而 `fz_store`（缓存）适合「共享」？

> **参考答案**：异常栈与 `setjmp/longjmp` 绑定，跳转目标是当前线程的栈帧；若多个线程共用一个异常栈，一个线程 `longjmp` 会跳进另一个线程的执行流，造成未定义行为。而缓存（字形、图像、字体）是「只读复用」的资源，多线程共享同一份可以显著节省内存并提高命中率，只要访问时用 `locks` 里的锁保护即可。

**练习 2**：`next_document_id` 字段的注释写着 `/* Only the master version of this is used! */`。这句话和 context 的克隆机制有什么关系？

> **参考答案**：文档 id 是一个全局递增计数器，整个「context 家族」应共用一个序列。只有 `master`（祖宗）context 维护的那份才生效，克隆体虽然字段被 `memcpy` 复制了，但运行时实际读写的是 master 的那份（参见 4.3 节 `fz_new_document_id` 的实现）。

---

### 4.2 fz_new_context 参数

#### 4.2.1 概念说明

创建 context 的入口是宏 `fz_new_context(alloc, locks, max_store)`，它有三个参数：

| 参数 | 类型 | 含义 | 传 `NULL` / 特殊值时的行为 |
| --- | --- | --- | --- |
| `alloc` | `const fz_alloc_context *` | 自定义内存分配器（一组 malloc/realloc/free 回调） | 用默认分配器（包装 libc 的 malloc/free） |
| `locks` | `const fz_locks_context *` | 线程锁（一组 lock/unlock 回调） | 用默认锁——**空操作**，意味着单线程 |
| `max_store` | `size_t` | 资源缓存 `store` 的字节数上限，超限会逐出旧缓存 | 传 `FZ_STORE_UNLIMITED` 表示不限 |

关于 `max_store`，头文件里给了两个现成常量（关键！）：

[include/mupdf/fitz/context.h:312-315](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L312-L315) 定义了 `FZ_STORE_UNLIMITED = 0` 与 `FZ_STORE_DEFAULT = 256 << 20`。

注意：

\[ \text{FZ\_STORE\_DEFAULT} = 256 \ll 20 = 256 \times 2^{20} = 268\,435\,456 \text{ 字节} = 256\,\text{MiB} \]

也就是说，**本讲实践任务要的「256MB」正好就是 `FZ_STORE_DEFAULT`**。所以你既可以直接写 `256 << 20`，也可以直接用常量 `FZ_STORE_DEFAULT`，二者等价。

> 还有一个隐藏的「第四参数」——版本号。宏会自动把 `FZ_VERSION`（当前为 `"1.29.0"`，见 `include/mupdf/fitz/version.h`）塞进去，库会在运行时校验「头文件版本」与「库版本」是否一致，不一致则拒绝创建 context。这就是 `fz_new_context_imp` 名字里 `_imp` 的由来——用户调宏，内部调 `_imp`。

#### 4.2.2 核心流程

`fz_new_context_imp` 的构造分**两个阶段**，理解这两阶段对排查「创建失败」很重要：

```
阶段 1（分配与置零，不抛异常）
  ① 校验版本号 → 不一致直接返回 NULL
  ② alloc/locks 为 NULL → 换成默认实现
  ③ malloc 一个 fz_context，memset 清零
  ④ 设置 user / alloc / locks / master=self / context_count=1
  ⑤ 设置默认 error/warn 回调
  ⑥ 初始化 error / aa / random 三组内嵌状态

阶段 2（创建共享子上下文，可能抛异常，包在 fz_try 内）
  ⑦ fz_new_store_context         (用 max_store)
  ⑧ fz_new_glyph_cache_context
  ⑨ fz_new_colorspace_context
  ⑩ fz_new_font_context
  ⑪ fz_new_hyph_context
  ⑫ fz_new_document_handler_context
  ⑬ fz_new_archive_handler_context
  ⑭ fz_new_style_context
  ⑮ fz_new_tuning_context
  └─ 任一步失败 → fz_catch 里 fz_drop_context(ctx) 并返回 NULL
```

设计要点：

- **阶段 1 用普通 malloc，失败返回 `NULL`**（因为此时 context 还没建好，不能用 MuPDF 自己的异常机制）。
- **阶段 2 用 `fz_try`**——此刻 context 已可用（有异常栈了），所以子上下文创建失败能走正常的异常路径，并在 catch 里清理掉已分配的东西。

#### 4.2.3 源码精读

先看用户调用的宏，它把版本号作为第四参数注入：

[include/mupdf/fitz/context.h:345](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L345) `#define fz_new_context(alloc, locks, max_store) fz_new_context_imp(alloc, locks, max_store, FZ_VERSION)` —— 看不见的第四参数负责版本校验。

再看实现。版本校验 + 默认值兜底 + 两阶段构造：

[source/fitz/context.c:255-316](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L255-L316) 这是 `fz_new_context_imp` 全文。重点几行：

- [context.c:260-264](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L260-L264)：版本不一致直接 `return NULL`，这就是「头库版本不匹配」的报错来源。
- [context.c:266-270](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L266-L270)：`alloc` / `locks` 为 `NULL` 时换默认值。
- [context.c:272-278](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L272-L278)：阶段 1 的 malloc + 清零。
- [context.c:284-286](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L284-L286)：`master = ctx`（自己是祖宗）、`context_count = 1`。
- [context.c:296-307](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L296-L307)：阶段 2，`fz_try` 内依次创建九个子上下文，第一个就是 `fz_new_store_context(ctx, max_store)`——`max_store` 参数最终落到这里。
- [context.c:308-314](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L308-L314)：阶段 2 失败时，`fz_drop_context(ctx)` 清理后返回 `NULL`。

最后确认「默认实现」长什么样：

[source/fitz/memory.c:294-317](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/memory.c#L294-L317) `fz_alloc_default` 就是包装 `malloc/realloc/free`；`fz_locks_default` 的 `lock/unlock` 是**空函数体**——所以传 `NULL` 锁等价于「完全不加锁」，只适合单线程。这也是 `fz_clone_context` 之所以「必须先有真实锁」的原因（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：亲手创建一个 store 上限为 256MB 的 context，打印它的指针和 store 上限，然后正确销毁。这正是本讲规格里指定的实践任务。

**操作步骤**：

1. 在能编译 MuPDF 的环境里（参考 u1-l2 的构建方式），新建一个最小源文件 `ctx_demo.c`，内容如下（**示例代码——本讲为讲解自编，非项目原有代码**）：

   ```c
   /* 示例代码：创建 / 打印 / 销毁一个 fz_context */
   #include <mupdf/fitz.h>
   #include <stdio.h>

   int main(void)
   {
       /* FZ_STORE_DEFAULT == 256 << 20 == 256 MiB */
       fz_context *ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
       if (!ctx) {
           fprintf(stderr, "cannot create context\n");
           return 1;
       }

       printf("context created at %p\n", (void *)ctx);
       printf("store limit = %zu bytes (%.0f MiB)\n",
              (size_t)FZ_STORE_DEFAULT,
              (double)FZ_STORE_DEFAULT / (1 << 20));

       /* 用完必须释放，且必须是该 context 上最后一个动作 */
       fz_drop_context(ctx);
       return 0;
   }
   ```

2. 编译（在源码树内，复用 `build/debug` 已编译的库）：

   ```sh
   cc -o ctx_demo ctx_demo.c \
      -Iinclude \
      build/debug/libmupdf.a build/debug/libmupdf-third.a \
      -lm -lpthread
   ```
   > 具体库路径取决于你的 `build=debug/release` 与平台，参考 u1-l2。若不确定链接参数，可先 `make examples` 看它给 `example` 用的链接命令，照抄即可。

3. 运行 `./ctx_demo`。

**需要观察的现象**：

- 程序打印出非空的 `context created at 0x...` 指针。
- 打印 `store limit = 268435456 bytes (256 MiB)`。
- 程序正常退出，返回码 0，无报错、无内存泄漏告警。

**预期结果**：

```
context created at 0x55a1...
store limit = 268435456 bytes (256 MiB)
```

> 若用 `make build=debug` 或 `build=sanitize` 构建，可借助 Memento/ASan 进一步确认无泄漏。本实践的「正确性」主要体现在：**只 new 一次、只 drop 一次、且 drop 在最后**。如果漏掉 `fz_drop_context`，在带 Memento 的构建里会报泄漏。
>
> 待本地验证：指针的具体值与库路径依你的环境而定。

#### 4.2.5 小练习与答案

**练习 1**：把上面程序的 `FZ_STORE_DEFAULT` 分别换成 `FZ_STORE_UNLIMITED` 和一个很小的值（如 `1 << 20`，即 1MB），重新编译运行。观察输出有什么变化？再后续若用它渲染复杂页面，会有什么不同？

> **参考答案**：仅就这个小程序而言，输出只有 `store limit` 那一行不同（`0` 或 `1048576`），context 都能正常创建和销毁——因为还没有真正往 store 里塞东西。差异会在「渲染时」显现：`FZ_STORE_UNLIMITED` 下缓存只增不减，内存占用持续上升；过小的上限会让 store 频繁触发逐出（scavenging），字形/图像反复重新解码，渲染变慢（缓存机制详见 u9-l1）。

**练习 2**：为什么阶段 1 用普通 `malloc` 并在失败时返回 `NULL`，而阶段 2 改用 `fz_try`？

> **参考答案**：阶段 1 执行时 context 尚未初始化完成，异常栈（`error`）还没就绪，无法走 MuPDF 的异常机制，只能用最原始的「返回 `NULL`」报告失败。阶段 2 开始前 `fz_init_error_context` 已把异常栈装好，于是可以安全地用 `fz_try/fz_catch`，让子上下文创建失败时能统一清理（catch 里调 `fz_drop_context`）。

---

### 4.3 context 的生命周期

#### 4.3.1 概念说明

一个 context 从生到死有三条路径需要理解：

1. **创建**：`fz_new_context` 造出的是「master（祖宗）context」，`master` 指向自己，`context_count = 1`。
2. **克隆**：多线程场景下，用 `fz_clone_context(ctx)` 为工作线程造一个「分身」。分身的 `master` 指向祖宗，祖宗的 `context_count` 加 1。分身**共享**祖宗的 `store / font / glyph_cache` 等缓存，但**独有**自己的异常栈。
3. **销毁**：`fz_drop_context(ctx)` 把 `context_count` 减 1。只有当整个家族的 `context_count` 减到 0（最后一个 context 被销毁）时，才会真正释放共享缓存与 master 本身。

> 为什么需要这么复杂的「家族计数」？因为缓存是共享的：不能在第一个分身 drop 时就释放 `store`——否则还在工作的其他线程会访问已释放内存。所以必须等「最后一个」context 离场，才能安全地回收共享资源。

另一个关键约束：**`fz_clone_context` 要求 context 在创建时必须提供真实的锁**（即 `locks` 不能是默认的空操作锁）。这很容易理解——多线程共享缓存，没有锁就一定会数据竞争。

#### 4.3.2 核心流程

```
fz_new_context          →  master=self, context_count=1          （单线程够用）
        │
        │ fz_clone_context (可多次，需真实锁)
        ▼
   分身 ctx' : master=祖宗, 祖宗.context_count++,
               共享 font/store/glyph_cache/colorspace...,
               error 栈被重置为独立的一份

fz_drop_context(任意成员)
   ├─ 若还有未处理异常 → 警告 "UNHANDLED EXCEPTION!"
   ├─ context_count-- (加 FZ_LOCK_ALLOC 保护)
   ├─ 若 context_count==0 且自己不是 master → 先释放 master
   ├─ 逆序 drop 各子上下文:
   │     document_handler → archive → glyph_cache → store
   │     → style → tuning → colorspace → font → hyph
   ├─ flush 警告；断言异常栈已空
   └─ 若自己是 master 且仍有子分身活着 → 只把 master 置 NULL（延后释放）;
      否则 free(ctx)
```

注意 drop 子上下文是**严格逆序**的——这与 new 时的顺序相反，保证「被依赖者」后释放。

#### 4.3.3 源码精读

先看克隆。它体现了「共享缓存 + 独有异常栈」的设计：

[source/fitz/context.c:318-355](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L318-L355) `fz_clone_context`。重点：

- [context.c:325](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L325)：如果锁仍是默认的空操作锁，直接 `return NULL` 拒绝克隆——没有锁就无法安全多线程。
- [context.c:333-335](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L333-L335)：祖宗的 `context_count++`，新分身的 `master` 指向祖宗。
- [context.c:338](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L338)：`memcpy(new_ctx, ctx, ...)` 一字不差地复制，于是所有共享指针都指向同一份缓存。
- [context.c:341](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L341)：`fz_init_error_context(new_ctx)` 把异常栈**重置**——这是「每线程独有异常栈」的保证。
- [context.c:344-352](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L344-L352)：对每个共享子上下文调 `fz_keep_*`，把引用计数加 1，确保共享对象不会被某一方的 drop 提前释放。

再看销毁。它体现了「家族计数 + 逆序清理」：

[source/fitz/context.c:174-240](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L174-L240) `fz_drop_context`。重点：

- [context.c:183-191](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L183-L191)：drop 前若发现还有未处理异常，会警告 `UNHANDLED EXCEPTION!` 并报告错误——这是排查「忘写 fz_catch」的重要信号。
- [context.c:195-203](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L195-L203)：在 `FZ_LOCK_ALLOC` 保护下 `context_count--`；若归零且自己不是 master，则标记要释放 master。
- [context.c:216-224](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L216-L224)：**逆序** drop 九个子上下文——对比 new 时的顺序（store 在前），drop 时 store 在后（`fz_drop_store_context`），正好相反。
- [context.c:231-239](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L231-L239)：若自己就是 master、但还有分身活着（`context_count != 0`），只把 `master` 置 `NULL` 延后释放（让计数得以续命）；否则才真正 `free(ctx)`。

最后看一个「家族计数」的真实用例——`next_document_id` 的取值：

[source/fitz/context.c:388-399](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L388-L399) `fz_new_document_id`。它先沿着 `master` 链回溯到祖宗（`while (ctx->master && ctx->master != ctx) ctx = ctx->master;`），再读写祖宗的 `next_document_id`——这就印证了 4.1 练习里那条注释「Only the master version of this is used」。

#### 4.3.4 代码实践

**实践目标**：体会「drop 必须配对、且顺序敏感」，并学会用带调试的构建发现遗漏。

**操作步骤**：

1. 在 4.2.4 的 `ctx_demo.c` 基础上做两个对照实验：
   - **实验 A（正确）**：保留 `fz_drop_context(ctx);`，正常编译运行。
   - **实验 B（错误）**：把 `fz_drop_context(ctx);` 那一行**注释掉**，重新编译运行。
2. 用带 Memento 的调试构建来放大问题（参考 u1-l2）：

   ```sh
   make build=memento
   cc -g -o ctx_demo_b ctx_demo.c -Iinclude \
      build/memento/libmupdf.a build/memento/libmupdf-third.a -lm -lpthread
   ./ctx_demo_b
   ```

**需要观察的现象**：

- 实验 A：正常退出，无任何告警。
- 实验 B（memento 构建）：退出时 Memento 会报告「未释放的内存块」，并标注其类型为 `fz_context`（因为 `fz_new_context_imp` 用 `Memento_label(..., "fz_context")` 给这块内存打了标签，见 [context.c:272](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L272)）。

**预期结果**：

- 实验 A 干净退出。
- 实验 B 在 memento/sanitize 构建下会看到泄漏报告，明确指向 `fz_context`。这正是「new 与 drop 必须一一配对」的直接证据。

> 待本地验证：是否启用了 Memento/ASan 取决于你的构建模式；release 构建下泄漏不会主动报告。

#### 4.3.5 小练习与答案

**练习 1**：在 `example.c` 里数一下 `fz_drop_context(ctx)` 出现了几次、分别在哪条错误分支。为什么几乎每个错误退出路径都要调用它？

> **参考答案**：`example.c` 在 [第 65、76、88、96、113、137 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/examples/example.c#L65) 共出现 6 次 `fz_drop_context`，覆盖「注册 handler 失败 / 打开文档失败 / 计数页数失败 / 页码越界 / 渲染失败 / 正常结束」所有退出路径。因为 context 一旦创建就必须由创建者负责销毁；任何提前 `return` 的分支若不 drop，就会泄漏整个 context 及其全部子上下文。注意顺序：凡是有 `doc` 的分支，都先 `fz_drop_document(ctx, doc)` 再 `fz_drop_context(ctx)`（资源逆序释放）。

**练习 2**：假设你写了一个多线程程序，用 `fz_clone_context` 给 4 个工作线程各造了一个分身。当主线程先 `fz_drop_context` 自己那份时，共享的 `store` 会被立刻释放吗？

> **参考答案**：不会。`fz_drop_context` 只是让祖宗的 `context_count` 从 5 减到 4（每个 clone 时 `++`，见 [context.c:333](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L333)）；只要 `context_count` 还没归零，共享的 `store/font/glyph_cache` 就不会被回收。只有当最后一个分身也被 drop、计数归零时，`fz_drop_store_context` 等逆序清理才会真正执行（[context.c:216-224](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L216-L224)）。这就是「家族计数」的意义。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「带错误处理的可配置 context 启动器」。

**任务**：写一个程序 `ctx_launch.c`，完成以下功能（**示例代码——本讲为讲解自编，非项目原有代码**）：

```c
/* 示例代码：综合实践 —— 可配置 store 上限的 context 启动器 */
#include <mupdf/fitz.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
    size_t max_store = FZ_STORE_DEFAULT;        /* 默认 256 MiB */
    fz_context *ctx = NULL;

    if (argc > 1)
        max_store = (size_t)strtoull(argv[1], NULL, 0); /* 允许 0x10000000 等写法 */

    /* ① 创建 context，判空 */
    ctx = fz_new_context(NULL, NULL, max_store);
    if (!ctx) {
        fprintf(stderr, "cannot create context\n");
        return 1;
    }
    fprintf(stderr, "ctx=%p, store_limit=%zu bytes\n", (void *)ctx, max_store);

    /* ② 注册 handler（这是第一次真正用到异常机制，包进 fz_try） */
    fz_try(ctx)
        fz_register_document_handlers(ctx);
    fz_catch(ctx)
    {
        fz_report_error(ctx);
        fprintf(stderr, "cannot register handlers\n");
        fz_drop_context(ctx);
        return 1;
    }

    /* ③ 此处可继续打开文档、渲染……（本练习只演示到 context 层） */

    /* ④ 收尾：context 是最后一个 drop 的对象 */
    fz_drop_context(ctx);
    return 0;
}
```

**验收清单**（逐条对照本讲知识点）：

1. 运行 `./ctx_launch`，应打印 `store_limit=268435456`（即默认 256MiB）。 —— 对应 4.2「`FZ_STORE_DEFAULT = 256 << 20`」。
2. 运行 `./ctx_launch 0`，store 上限变为 `0`（即 `FZ_STORE_UNLIMITED`）。 —— 对应 4.2「`max_store` 参数」。
3. 运行 `./ctx_launch 1048576`，store 上限变为 1MiB。 —— 对应 4.2 小练习 1。
4. 用 `build=memento` 重新编译并运行，正常退出应**无泄漏报告**；若删掉最后的 `fz_drop_context`，应看到指向 `fz_context` 的泄漏。 —— 对应 4.3「new/drop 配对」。
5. 故意把 `fz_register_document_handlers` 那段改成触发异常（例如之后调用 `fz_throw`），观察 catch 分支是否在 drop context 后干净退出。 —— 对应 4.3「未处理异常警告」与 4.2「阶段 2 用 fz_try」。

> 待本地验证：库文件路径、是否启用 Memento 视你的构建而定；重点是逻辑与输出符合上面清单。

---

## 6. 本讲小结

- `fz_context` 是 MuPDF 的「全局状态容器」，把内存分配器、锁、异常栈、各类子上下文与缓存全部打包，因此几乎所有 fitz 函数都把它作为第一个参数（[context.h:885-928](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L885-L928)）。
- 它的字段分为**非共享**（异常栈、抗锯齿等，每线程独有）与**共享**（`store/font/colorspace/glyph_cache` 等，一族 context 共用）两组。
- `fz_new_context(alloc, locks, max_store)` 三参数分别控制分配器、线程锁、缓存上限；`NULL` 表示用默认实现，`max_store` 用 `FZ_STORE_DEFAULT`（= `256 << 20` = 256MiB）或 `FZ_STORE_UNLIMITED`（[context.h:312-345](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L312-L345)）。
- 构造分两阶段：阶段 1 用普通 malloc 并对失败返回 `NULL`；阶段 2 在 `fz_try` 内创建九个子上下文，失败则整体回滚（[context.c:255-316](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L255-L316)）。
- 生命周期靠 `master` + `context_count` 管理：`fz_clone_context` 增计数并共享缓存、独有异常栈（[context.c:318-355](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L318-L355)）；`fz_drop_context` 减计数、逆序清理子上下文，只有家族归零才真正回收（[context.c:174-240](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/context.c#L174-L240)）。
- 实践铁律：**每个 `fz_new_context` 必须恰好配一个 `fz_drop_context`，且 drop 必须是 context 上最后一个动作、放在所有其它资源 drop 之后**。

---

## 7. 下一步学习建议

本讲把 context 的「外壳」讲透了，但有几个内部细节是后续讲义的主题，建议按顺序深入：

1. **u2-l2 内存管理与引用计数**：本讲的 `alloc` 参数到底怎么自定义？`fz_keep_imp` / `fz_drop_imp`（[context.h:954-1107](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/context.h#L954-L1107)）那套引用计数宏如何复用？下一讲会拆开 `memory.c` 与 `store.h`。
2. **u2-l3 异常处理 fz_try / fz_catch**：本讲反复出现的 `error` 异常栈、`fz_init_error_context`、`fz_try` 宏的 `setjmp/longjmp` 实现细节，下一讲专题讲解。
3. **u9-l1 store 缓存与清理 / u9-l2 多线程渲染**：本讲多次提到「共享缓存」「`context_count` 家族计数」，其真正的性能意义要到第九单元才完全展开；届时你会看到 `fz_clone_context` 如何让多线程共享 store、各持异常栈。

> 在进入下一讲前，建议你确认：能不看资料默写出 `fz_new_context` 的三参数与 `FZ_STORE_DEFAULT` 的字节数，并能解释「为什么 drop 子上下文要逆序」。
