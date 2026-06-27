# 函数调用与调用帧

## 1. 本讲目标

上一讲（u4-l2）我们搞清楚了「值存在哪、怎么取」：每条指令的操作数是一个 `njs_index_t`，被解码成 `vm->levels[level][slot]` 的一次 O(1) 访问。其中 LOCAL 和 CLOSURE 两级指针会在「函数调用边界」上整体替换。本讲就来回答那个被刻意按下没讲的问题：**一次函数调用，到底是怎么把新函数的 LOCAL/CLOSURE 数组建好、把旧的存起来、跑完再恢复的？**

读完本讲你应该能够：

- 区分 njs 里两种函数的内部表示：**原生 C 函数（native）**与**字节码 lambda 函数**，看懂它们共用一个 `njs_function_t` 却在 `u` 联合里分叉。
- 画出**调用帧链** `vm->top_frame` / `vm->active_frame` 的结构，说清楚每次调用是怎么把一个新帧「压」上去、返回时怎么「弹」下来。
- 跟踪一条调用从 **`FUNCTION_FRAME` → `PUT_ARG` → `FUNCTION_CALL` → 函数体 → `RETURN`** 的完整字节码路径，并指出 native 调用与 lambda 调用在执行方式上的根本差别。

本讲是 u4-l1（解释器主循环）、u4-l2（作用域寻址）的直接延续，也为 u4-l4（异常处理）和 u4-l5（Promise/async）铺垫——后两者的「沿调用帧回溯」和「续体恢复」都建立在调用帧链之上。

## 2. 前置知识

- **寄存器式 VM**：njs 不用操作数栈，中间结果都放在带编号的槽位里（见 u4-l2）。本讲里你会看到，函数的局部变量、参数也住在这样的槽位里，而这些槽位的物理内存就**分配在调用帧上**。
- **`njs_value_t`**：16 字节的 JS 值（见 u2-l2）。一个函数在 JS 层是一个值，内部用一个 `njs_function_t` 结构体表示。
- **四级存储 LOCAL/CLOSURE/GLOBAL/STATIC**：见 u4-l2。本讲最关心的是：调用一个函数时，VM 怎么把 `vm->levels[NJS_LEVEL_LOCAL]` 和 `vm->levels[NJS_LEVEL_CLOSURE]` 这两个指针「换」成被调函数自己的数组。
- **字节码取指循环**：见 u4-l1。解释器主循环 `pc` 推进靠 `ret`（本条指令长度），遇到 `FUNCTION_CALL` 这类指令会递归进入新一轮解释器循环。

> 一句话区分两个容易混的词：**「函数」（`njs_function_t`）**是 JS 语义对象，描述「这是一个可调用的东西」；**「调用帧」（`njs_native_frame_t` / `njs_frame_t`）**是运行期对象，描述「这一次具体调用所占的内存与上下文」。同一个函数可以被调用很多次，每次调用对应一个独立的帧。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/njs_value.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h) | 定义函数对象 `struct njs_function_s`（`njs_function_t`），含 `native` 位与 `u` 联合——本讲的起点。 |
| [src/njs_function.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.h) | lambda 描述 `njs_function_lambda_t`、两种调用帧 `njs_native_frame_t` / `njs_frame_t`、帧大小宏、以及分流内联函数 `njs_function_frame`。 |
| [src/njs.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h) | 原生函数指针类型 `njs_function_native_t` 的签名——`u.native` 装的就是它。 |
| [src/njs_vm.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h) | VM 里的调用帧链入口 `top_frame` / `active_frame`，以及栈深度预算 `spare_stack_size`。 |
| [src/njs_function.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c) | 本讲主战场：函数对象创建、native/lambda 帧分配、`njs_function_frame_invoke` 分流、`njs_function_lambda_call` 的指针切换、native 调用的内联执行。 |
| [src/njs_vmcode.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c) | 解释器里 `FUNCTION_FRAME` / `METHOD_FRAME` / `PUT_ARG` / `FUNCTION_CALL` / `RETURN` 五个分支的处理，以及函数值创建 `njs_vmcode_function`。 |
| [src/njs_vmcode.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h) | 上述指令的二进制结构体（`njs_vmcode_function_frame_t` 等）与操作码枚举 `NJS_VMCODE_*`。 |
| [src/njs_vm.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c) | 帧弹出/恢复 `njs_vm_scopes_restore`——调用返回时把 `top_frame`/`active_frame` 退回上一层。 |

---

## 4.1 函数表示：native 与 lambda 的双形态

### 4.1.1 概念说明

在 JS 里，`function f(){}` 和 `Math.max` 看起来都是「函数」，但它们的执行方式天差地别：

- `function f(){...}` 是**你用 JS 写的函数**，引擎先把它编译成字节码，运行时按字节码一步步执行。njs 称这种为 **lambda 函数**（lambda 指编译产物里的「函数体字节码」）。
- `Math.max`、`Array.prototype.push` 这些是**引擎用 C 预先写好的内建函数**，调用时直接执行对应的 C 代码，根本不经过字节码解释器。njs 称这种为 **native 函数**（原生函数）。

njs 用**同一个结构体** `njs_function_t` 表示这两种函数，靠一个 `native` 标志位区分，并在一个 `u` 联合里「分叉」存放各自专属的信息。这样上层代码（比如属性查找、`typeof`）只需要一种「函数值」的概念，只有真正要调用时才去关心它是 native 还是 lambda。

此外还有一个关键标志 `ctor`（constructor）：标记这个函数能否用 `new` 调用。内建函数和你写的 `class`/构造函数会置 `ctor=1`，普通箭头函数为 0。

### 4.1.2 核心流程

一个 `njs_function_t` 的内部布局可以概括为：

```
njs_function_t
├─ object          ← 它首先是一个 JS 对象（有原型链、属性），type = NJS_FUNCTION
├─ native   :1     ← ★ 0 = JS 字节码函数(lambda)，1 = C 原生函数
├─ ctor     :1     ← 是否可作为构造器(new 调用)
├─ args_count      ← 形参个数(给 .length 用)
├─ magic8          ← 给 native 函数用的「子类型」标记(同一个 C 函数服务多个内建)
├─ u ─┬─ lambda   ← native=0 时：指向 njs_function_lambda_t（字节码 + 元数据）
│    └─ native    ← native=1 时：一个 C 函数指针 njs_function_native_t
├─ context         ← 给 bound/特殊函数挂的附加数据
└─ bound           ← Function.prototype.bind 预绑的参数
```

两种函数在 `u` 联合上的差异：

| 维度 | lambda 函数（JS 写的） | native 函数（C 写的） |
|---|---|---|
| `native` 位 | 0 | 1 |
| `u.lambda` | 指向 `njs_function_lambda_t`：字节码起点 `start`、形参数 `nargs`、局部变量数 `nlocal`、闭包索引表 `closures` 等 | （不用） |
| `u.native` | （不用） | 一个 C 函数指针，签名固定为 `(vm, args, nargs, magic8, retval)` |
| 执行方式 | 走解释器，递归进入字节码循环 | 直接 C 函数调用，**不**进解释器 |
| 字节码来源 | 有自己的 `lambda->start` 代码块 | 无字节码 |

lambda 专属的 `njs_function_lambda_t` 才是「编译产物」的载体——它就是 u3-l4/u3-l5 讲的字节码生成器的产物被挂载的地方。

### 4.1.3 源码精读

**函数对象 `njs_function_t`** —— [src/njs_value.h:L224-L249](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L224-L249)（节选关键部分）：

```c
struct njs_function_s {
    njs_object_t                      object;     // 首先是个 JS 对象

    uint8_t                           args_count:4;
    uint8_t                           native:1;   // ★ 区分 native / lambda
    uint8_t                           ctor:1;     // ★ 是否可 new
    ...
    uint8_t                           magic8;     // native 函数的子类型标记

    union {
        njs_function_lambda_t         *lambda;    // native=0：字节码函数
        njs_function_native_t         native;     // native=1：C 函数指针
    } u;

    void                              *context;
    njs_value_t                       *bound;
};
```

注意 `u` 是一个 `union`：`lambda`（指针）和 `native`（函数指针）共用同一段内存，**同一时刻只用其中一个**。到底是哪个，由上面的 `native` 位决定。

**native 函数指针的类型** —— [src/njs.h:L118-L119](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L118-L119)：

```c
typedef njs_int_t (*njs_function_native_t) (njs_vm_t *vm, njs_value_t *args,
    njs_uint_t nargs, njs_index_t magic8, njs_value_t *retval);
```

所有内建函数（`Math.max`、`Array.prototype.push`、`console.log`……）的 C 实现都是这个签名。`magic8` 是个巧思：一组语义相近的内建（比如各种 `Array.prototype` 方法）可以共用同一个 C 函数，靠 `magic8` 区分具体是哪一个——这就是为什么 `njs_function_t` 里要单独留一个 `magic8` 字段。

**lambda 函数的编译产物** —— [src/njs_function.h:L11-L26](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.h#L11-L26)：

```c
struct njs_function_lambda_s {
    njs_index_t                    *closures;     // 被捕获变量的 index 表
    uint32_t                       nclosures;     // 捕获的变量数
    uint32_t                       nlocal;        // 局部变量数(含 this 占的 0 号)

    njs_index_t                    self;          // 函数自引用(this function)的槽位

    uint32_t                       nargs;         // 形参数
    uint8_t                        ctor;          // 是否构造器
    uint8_t                        rest_parameters; // 是否有 ...rest 参数

    njs_value_t                    name;          // 函数名

    u_char                         *start;        // ★ 字节码起点(u3-l5 的产物)
};
```

`start` 就是 u3-l5 讲的字节码序列的入口指针——u4-l2 反汇编里看到的 `shell:f` 那段独立代码块，对应的 `lambda->start` 就是它的第一条指令。`nlocal` 决定了这个函数需要多大的 LOCAL 数组（调用时据此分配帧，见 4.2）。

**两种函数的创建入口不同** —— lambda 由 [src/njs_function.c:L14-L67](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L14-L67) 的 `njs_function_alloc` 创建（把 lambda 挂到 `u.lambda`，并按 `nclosures` 在结构体尾部多分配闭包指针数组）；native 由 [src/njs_function.c:L70-L91](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L70-L91) 的 `njs_vm_function_alloc` 创建（置 `native=1`，把 C 指针挂到 `u.native`）。两者的分流就发生在创建这一刻。

### 4.1.4 代码实践

**实践目标**：在源码层确认「同一个 `njs_function_t`，靠 `native` 位 + `u` 联合承载两种函数」，并能在反汇编里看出一个 JS 函数被编译成了独立的字节码块。

**操作步骤**：

1. 打开 [src/njs_value.h:L224-L249](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L224-L249)，对照上面的表，自己用一句话写出 `u.lambda` 与 `u.native` 各在什么条件下有效。
2. 构建 CLI（见 u1-l3）：`./configure && make njs`。
3. 运行反汇编：
   ```bash
   ./build/njs -d
   >> var a = 42; function f(v) { return v + 1 }
   ```
   对照 u4-l2 给出的输出：你会看到 `shell:main` 和 `shell:f` **两段独立的代码块**。`shell:f` 这段就是函数 `f` 的字节码，它在运行期对应一个 `njs_function_t`（`native=0`），其 `u.lambda->start` 指向这段代码的首地址。

**需要观察的现象**：每个用 `function` 声明的函数，在 `-d` 输出里都有自己的命名代码块（如 `shell:f`）；而像 `Math.max` 这样的内建函数不会出现在反汇编里，因为它没有字节码——它是一个 `native=1` 的 `njs_function_t`，`u.native` 是 C 指针。

**预期结果**：你能清楚说出「JS 函数 = lambda + 字节码块」「内建函数 = native + C 指针」，二者共用 `njs_function_t`。具体的 `shell:f` 字节码内容已在 u4-l2 完整解码，这里不重复。

> 待本地验证：不同 njs 版本 `-d` 输出的代码块命名前缀（如 `shell:`）和指令地址可能略有差异，但「每个 JS 函数独占一段字节码」这一结构恒成立。

### 4.1.5 小练习与答案

**练习 1**：为什么 njs 要把 native 和 lambda 两种函数塞进同一个 `njs_function_t`，而不是定义两个结构体？

> **参考答案**：因为在 JS 语义层，二者完全等价——都能被赋值、当参数传、做属性、用 `()` 调用、出现在原型链上。统一表示让属性查找、`typeof`、`Function.prototype.call/apply` 这些公共逻辑只写一份。只有真正「调用」时才需要分叉（见 4.2 的 `njs_function_frame` 分流），这时用 `native` 位一判断即可。这是「公共接口统一、内部实现分叉」的典型设计。

**练习 2**：`magic8` 字段是给谁用的？为什么 native 函数需要它而 lambda 不需要？

> **参考答案**：`magic8` 只对 native 函数有意义。因为多个内建方法常共用同一段 C 实现（比如一堆类型转换方法），引擎在注册时给它们挂同一个 C 函数指针、却用不同的 `magic8` 值区分，C 函数内部 `switch(magic8)` 决定具体行为，省下重复代码。lambda 函数每个都有独立的字节码，天然互不相同，不需要这个标记。

---

## 4.2 调用帧链：top_frame / active_frame 与帧内存布局

### 4.2.1 概念说明

函数被调用时，需要一块内存来放：被调函数是谁、`this` 是什么、各个实参的值、局部变量的槽位、以及「返回后该回到哪」。这块内存就叫**调用帧（call frame）**。多次嵌套调用会形成一串帧，串成**调用帧链**——这就是 njs 版的「调用栈」。

njs 的调用帧链由两个 VM 字段管理（见 [src/njs_vm.h:L128-L129](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L128-L129)）：

- **`vm->top_frame`**：当前**最顶层**的帧，即「正在执行的这个调用」的帧。新调用会把它压成 `previous`，自己顶上来；返回时退回 `previous`。
- **`vm->active_frame`**：当前**活跃的字节码帧**。它和 `top_frame` 在执行 lambda 时指向同一个帧，但在 native 调用期间会短暂「错位」（见 4.3），用于在 native 函数内部仍能定位到「外层正在跑字节码的那一帧」。

帧本身分两种结构体，前者是后者的「子集」：

- **`njs_native_frame_t`**：最基础的帧，native 调用用它就够（只需放 `this` + 实参）。
- **`njs_frame_t`**：在 `njs_native_frame_t` 基础上**多出**异常链表头 `exception` 和 `previous_active_frame` 指针，lambda 调用用它（因为 lambda 需要 try/catch 上下文，见 u4-l4）。

### 4.2.2 核心流程

调用帧链的「压/弹」节奏（以 `g()` 调用 `f()` 为例）：

```
全局代码运行中：
    top_frame     ──► [全局帧]
                        previous = NULL

调用 g()：
    [g 的帧].previous = [全局帧]
    top_frame     ──► [g 的帧] ──► [全局帧]
    g 内 levels[LOCAL] 指向 [g 的帧].local

g 里调用 f()：
    [f 的帧].previous = [g 的帧]
    top_frame     ──► [f 的帧] ──► [g 的帧] ──► [全局帧]
    f 内 levels[LOCAL] 指向 [f 的帧].local

f 返回（RETURN）：
    top_frame = [f 的帧].previous = [g 的帧]   ← scopes_restore 弹帧
    释放 [f 的帧]
```

帧的内存布局有两种，对应两种调用：

**native 帧**（紧凑，只放 `this`+实参）：

```
┌─────────────────────┬──────┬────────────────┐
│ njs_native_frame_t  │ this │ arg0 arg1 ...  │
└─────────────────────┴──────┴────────────────┘
                       arguments 指向这里(arg0)
```

**lambda 帧**（带局部变量槽位，且用「指针数组 + 值数组」双区布局）：

```
┌────────────┬────────────────────────┬────────────────────────┐
│ njs_frame_t│ p0 p1 ... pn(指针数组) │ v0 v1 ... vn(值数组)    │
└────────────┴────────────────────────┴────────────────────────┘
              └─ local 指向 p[args_count]   └─ 每个 pi 正常指向 vi
                                              (被闭包捕获的 pi 指向堆)
```

> 这个双区布局正是 u4-l2 里 `vm->top_frame->local` 的来源：lambda 调用时 `vm->levels[NJS_LEVEL_LOCAL]` 被换成 `top_frame->local`，即上图指针数组从 `args_count` 开始的那一段。每个槽位 `pi` 指向对应的值 `vi`，所以 `levels[LOCAL][slot]` 取到的是一个 `njs_value_t *`。

帧分配还内置了**栈深度保护**：每次分配都从 `vm->spare_stack_size` 这个总预算里扣，扣超了就直接抛 `RangeError("Maximum call stack size exceeded")`——这就是无限递归不会让进程崩溃、而是抛 JS 异常的原因。

### 4.2.3 源码精读

**两个帧结构体** —— [src/njs_function.h:L40-L63](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.h#L40-L63)（`njs_native_frame_t`）与 [src/njs_function.h:L74-L80](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.h#L74-L80)（`njs_frame_t`）：

```c
struct njs_native_frame_s {
    u_char                    *free;          // 本帧空闲区起点(供下一次分配复用)
    u_char                    *pc;            // 保存的返回 pc(供恢复/async 用)
    njs_function_t            *function;      // 本帧对应的函数
    njs_native_frame_t        *previous;      // ★ 上一层帧(串成链)
    njs_value_t               *arguments;     // 指向第一个实参(this 之后)
    njs_value_t               **local;        // ★ LOCAL 槽位数组(lambda 用)
    uint32_t                  nargs;
    uint8_t                   native:1;       // 本帧是 native 还是 lambda 调用
    uint8_t                   ctor:1;         // 是否 new 调用
    ...
};

struct njs_frame_s {
    njs_native_frame_t        native;         // 先嵌一个基础帧
    njs_exception_t           exception;      // try/catch 链表头(见 u4-l4)
    njs_frame_t               *previous_active_frame;  // ★ 返回时恢复 active_frame
};
```

关键点：`njs_frame_t` 的第一个字段就是 `njs_native_frame_t native`，所以两者可以安全地互相强制转换——一个 `njs_native_frame_t *` 实际指向的内存，只要它是 lambda 调用产生的，就其实是个 `njs_frame_t`。`previous` 串起调用栈，`local` 是 u4-l2 里 LOCAL 指针的来源。

**分流：按 native 位选两种帧** —— [src/njs_function.h:L145-L156](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.h#L145-L156)：

```c
njs_inline njs_int_t
njs_function_frame(njs_vm_t *vm, njs_function_t *function, ...)
{
    if (function->native) {
        return njs_function_native_frame(vm, function, this, args, nargs, ctor);
    } else {
        return njs_function_lambda_frame(vm, function, this, args, nargs, ctor);
    }
}
```

这是 4.1 的「分叉」在调用准备阶段的体现：同一个 `njs_function_frame` 入口，按 `native` 位走两条完全不同的分配路径。

**lambda 帧的双区布局** —— [src/njs_function.c:L397-L441](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L397-L441)（`njs_function_lambda_frame`，含官方布局注释，节选）：

```c
/*
 * Lambda frame has the following layout:
 *  njs_frame_t | p0 , p1, ..., pn | v0, v1, ..., vn
 *  p0..pn - pointers to arguments and locals,
 *  v0..vn - values of arguments and locals.
 *  Normally, pi points to vi directly after them,
 *  but if a value was captured as a closure, pi points to heap memory.
 */
args_count  = njs_max(nargs, lambda->nargs);
value_count = args_count + lambda->nlocal;        // 槽位总数
value_size  = value_count * sizeof(njs_value_t *);
frame_size  = value_size + (value_count * sizeof(njs_value_t));

native_frame = njs_function_frame_alloc(vm, NJS_FRAME_SIZE + frame_size);
...
new   = (njs_value_t **) ((u_char *) native_frame + NJS_FRAME_SIZE);
value = (njs_value_t *)  ((u_char *) new + value_size);

n = value_count;
while (n != 0) { n--; new[n] = &value[n]; njs_set_invalid(new[n]); }  // pi → vi

native_frame->arguments = value;
native_frame->local     = new + args_count;   // ★ LOCAL 数组从实参之后开始
...
*native_frame->local[0] = *this;              // 0 号槽放 this
```

注意三件事：(1) 槽位数 = `max(实参数, 形参数) + 局部变量数`，由 lambda 的编译产物决定；(2) `local = new + args_count`，所以 u4-l2 说「`this` 占 LOCAL 0 号槽」在这里落地——`local[0]` 就是 `this`；(3) 默认 `pi` 指向同帧内的 `vi`，但若某变量被内层闭包捕获，它的 `pi` 会被改成指向堆分配的副本（见 `njs_function_capture_closure`，[src/njs_function.c:L765-L803](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L765-L803)），这样外层函数返回后该变量仍存活——这就是闭包的物理实现。

**帧分配与栈深度保护** —— [src/njs_function.c:L470-L511](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L470-L511)（`njs_function_frame_alloc`，节选）：

```c
spare_size = vm->top_frame ? vm->top_frame->free_size : 0;

if (size <= spare_size) {
    frame = (njs_native_frame_t *) vm->top_frame->free;   // 复用上一帧的空闲尾区
    ...
} else {
    spare_size = size + NJS_FRAME_SPARE_SIZE;             // 4KiB 对齐
    if (spare_size > vm->spare_stack_size) {              // ★ 超总预算
        njs_range_error(vm, "Maximum call stack size exceeded");
        return NULL;
    }
    frame = njs_mp_align(vm->mem_pool, sizeof(njs_value_t), spare_size);
    ...
    vm->spare_stack_size -= spare_size;
}

njs_memzero(frame, sizeof(njs_native_frame_t));
frame->previous = vm->top_frame;     // ★ 接到链上
vm->top_frame   = frame;             // ★ 顶上来
```

两个细节值得记：(1) 帧分配有「复用」优化——如果当前帧尾部还剩足够大的空闲区，就直接在那里切出新帧，避免频繁向内存池要内存；(2) 所有帧共享 `vm->spare_stack_size` 这个总预算，深递归会把它扣光从而触发 `RangeError`，而不是让 C 栈溢出崩溃。

### 4.2.4 代码实践

**实践目标**：亲眼看到「栈深度保护」如何把无限递归变成一个可控的 JS 异常，并理解它对应的源码分支。

**操作步骤**：

1. 构建 CLI（见 u1-l3）。
2. 运行一段无限递归：
   ```bash
   ./build/njs -c 'function f(){ return f() } try { f() } catch(e){ console.log(e.name) }'
   ```
3. 对照 [src/njs_function.c:L486-L489](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L486-L489) 的 `RangeError("Maximum call stack size exceeded")` 分支。

**需要观察的现象**：进程不会崩溃，也不会段错误；而是打印出 `RangeError`（被 `try/catch` 捕获）。每一次 `f()` 调用都在 `njs_function_frame_alloc` 里扣 `spare_stack_size`，扣到不够下一帧（`spare_size > vm->spare_stack_size`）时返回 NULL，调用链把 `RangeError` 抛回 JS 层。

**预期结果**：输出 `RangeError`。这印证了「调用帧链的深度受 `spare_stack_size` 限制，溢出转化为 JS 异常而非 C 崩溃」。

> 待本地验证：不同平台/版本的默认 `spare_stack_size` 不同，触发的递归深度也不同，但「抛 RangeError 而非崩溃」这一行为稳定。

**源码阅读型实践（可选）**：在 [src/njs_function.c:L470-L511](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L470-L511) 的 `frame->previous = vm->top_frame; vm->top_frame = frame;` 两行，用自己的话描述「新帧如何挂到链上并成为新的 top_frame」；再在 [src/njs_vm.c:L639-L652](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L639-L652) 的 `njs_vm_scopes_restore` 里找出对应的「弹帧」操作（`vm->top_frame = native->previous;`）。

### 4.2.5 小练习与答案

**练习 1**：`njs_frame_t` 为什么要把 `njs_native_frame_t` 作为第一个字段嵌进来？

> **参考答案**：为了用 C 的「结构体首字段即首地址」规则实现多态。所有帧链节点都按 `njs_native_frame_t *` 串联（`previous` 字段类型就是它），代码遍历链时只认基础帧类型；只有当确认某帧是 lambda 调用产生时，才把它强制转换成 `njs_frame_t *` 去访问多出来的 `exception`/`previous_active_frame`。这相当于用 C 手写了一个「lambda 帧是 native 帧的子类」的继承关系。

**练习 2**：为什么 lambda 帧用「指针数组 + 值数组」的双区布局，而不是直接存值？

> **参考答案**：为了支持闭包的「按需提升到堆」。直接存值的话，被内层函数捕获的局部变量会随外层帧释放而消失；双区布局下，正常 `pi` 指向帧内 `vi`，一旦某变量被捕获，只需把对应的 `pi` 改指向堆分配的副本（`njs_function_capture_closure` 干这件事），其余槽位不受影响。同时这正好和 u4-l2 的 `levels[LOCAL]` 二级指针模型对齐——`levels[LOCAL][slot]` 取到的本就是指针。

---

## 4.3 调用指令处理：从 FUNCTION_FRAME 到 RETURN

### 4.3.1 概念说明

到了字节码层，一次函数调用不是一条指令，而是**一串指令的协作**。njs 把「调用」拆成三个阶段，对应三条指令（外加一条放参数的指令）：

| 指令 | 干什么 |
|---|---|
| `FUNCTION_FRAME` | 为**自由函数调用** `f(a,b)` 准备帧：读出函数值 `f`，调用 `njs_function_frame_create` 建帧（压链）。 |
| `METHOD_FRAME` | 为**方法调用** `obj.m(a,b)` 准备帧：除了函数值，还多读一个 `this`（即 `obj`）。 |
| `PUT_ARG` | 把一个实参的值写进当前帧的 `arguments` 区（每个实参一条）。 |
| `FUNCTION_CALL` | **真正触发调用**：保存调用者 pc，调 `njs_function_frame_invoke` 分流到 native/lambda 执行。 |

另外还有一条容易和上面混淆的指令：

| 指令 | 干什么 |
|---|---|
| `FUNCTION` | **创建函数值**（不是调用！）。运行期遇到函数字面量时执行，调用 `njs_vmcode_function` 把 lambda 包装成 `njs_function_t` 并捕获闭包。 |

`FUNCTION_FRAME` / `METHOD_FRAME` / `FUNCTION_CALL` 是「调用三件套」；`FUNCTION` 是「函数诞生」。本模块聚焦调用三件套，`FUNCTION` 在 4.1 已涉及它的产物。

返回侧只有一条 `RETURN`，但它做的事比看上去多：处理构造器返回值、**弹帧**（`njs_vm_scopes_restore`）、**释放帧内存**（`njs_function_frame_free`）。

### 4.3.2 核心流程

把调用三件套和 RETURN 串起来，一次 `f(5)` 的完整字节码路径（`f` 是 lambda）：

```
[调用者 f(5) 的字节码]
  1. FUNCTION_FRAME   读出 f 值 ──► njs_function_frame_create
                       └─ njs_function_lambda_frame ──► njs_function_frame_alloc
                          新帧入链：top_frame = [f 的帧] →previous→ [调用者帧]

  2. PUT_ARG (可能多条)  把实参写进 [f 的帧].arguments[put_args++]
                          （这里把 5 写进 arg0）

  3. FUNCTION_CALL      保存：active_frame->native.pc = pc（调用者位置）
                       ──► njs_function_frame_invoke
                          └─ lambda 分支：njs_function_lambda_call
                             ├ 保存 levels[LOCAL]/[CLOSURE] 旧值
                             ├ 换成 f 自己的 local / closures
                             ├ vm->active_frame = [f 的帧]
                             └ 递归进入 njs_vmcode_interpreter(lambda->start)  ← 跑 f 的字节码

[f 的字节码，在新的解释器循环里]
  ... 函数体（如 ADD、PROPERTY_GET 等）...
  RETURN               ──► njs_vmcode_return
                          ├ 构造器返回值修正
                          ├ njs_vm_scopes_restore：top_frame = [调用者帧]，active_frame 复原
                          └ njs_function_frame_free：释放 [f 的帧]
                       return NJS_OK  ← 退出这层解释器循环，回到调用者

[回到调用者的 FUNCTION_CALL 之后]
  pc += sizeof(FUNCTION_CALL)，继续取下一条指令……
```

native 调用（如 `Math.max(1,2)`）路径更短，**不递归进解释器**：

```
  FUNCTION_FRAME ──► njs_function_native_frame（紧凑帧，只放 this+实参）
  PUT_ARG × N
  FUNCTION_CALL  ──► njs_function_frame_invoke
                     └─ native 分支：njs_function_native_call
                        ├ 直接 call = function->u.native;  call(vm, args, nargs, magic8, retval);
                        ├ njs_vm_scopes_restore（自己弹帧）
                        └ njs_function_frame_free
                     （没有新的解释器循环，C 函数返回即调用结束）
```

核心差别：**lambda 调用 = 递归进入一轮解释器循环；native 调用 = 一次普通 C 函数调用，不进解释器**。这就是为什么内建函数比 JS 函数快——没有字节码分发开销。

### 4.3.3 源码精读

**`FUNCTION_FRAME`：建自由函数调用帧** —— [src/njs_vmcode.c:L1484-L1501](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1484-L1501)：

```c
CASE (NJS_VMCODE_FUNCTION_FRAME):
    value2 = (njs_value_t *) vmcode->operand1;          // 实参数(编码在操作数里)
    njs_vmcode_operand(vm, vmcode->operand2, value1);   // value1 = 函数值 f
    function_frame = (njs_vmcode_function_frame_t *) pc;

    ret = njs_function_frame_create(vm, value1, &njs_value_undefined,
                                    (uintptr_t) value2, function_frame->ctor);
    ...
    ret = sizeof(njs_vmcode_function_frame_t);
    BREAK;
```

`njs_function_frame_create`（[src/njs_vmcode.c:L2532-L2591](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2532-L2591)）会先校验「这是不是函数」、处理 `new`（ctor）场景下创建新对象作 `this`、处理 `bind` 预绑参数，最后调 `njs_function_frame`（4.2 的分流函数）真正建帧。注意自由函数调用的 `this` 传的是 `undefined`（严格模式下）。

**`METHOD_FRAME`：建方法调用帧** —— [src/njs_vmcode.c:L1503-L1520](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1503-L1520)：

```c
CASE (NJS_VMCODE_METHOD_FRAME):
    njs_vmcode_operand(vm, vmcode->operand3, value2);   // value2 = this(obj)
    njs_vmcode_operand(vm, vmcode->operand2, value1);   // value1 = 方法值 obj.m
    method_frame = (njs_vmcode_method_frame_t *) pc;

    ret = njs_function_frame_create(vm, value1, value2,
                                    method_frame->nargs, method_frame->ctor);
```

和 `FUNCTION_FRAME` 的唯一差别：多取一个操作数作为 `this`（即 `obj`），传给 `njs_function_frame_create`。这就是 `f()` 与 `obj.m()` 在引擎里的根本不同——后者把 `obj` 作为 `this` 带进帧。

**`PUT_ARG`：逐个写实参** —— [src/njs_vmcode.c:L1297-L1309](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1297-L1309)：

```c
CASE (NJS_VMCODE_PUT_ARG):
    put_arg = (njs_vmcode_1addr_t *) pc;
    native  = vm->top_frame;                            // 刚建好的帧
    value1  = &native->arguments[native->put_args++];   // 下一个实参槽
    njs_vmcode_operand(vm, put_arg->index, value2);     // value2 = 实参值
    njs_value_assign(value1, value2);                   // 写进去
```

每个实参对应一条 `PUT_ARG`，按序写入 `top_frame->arguments[]`，`put_args` 是已写计数器。这解释了 4.2 帧布局里 `arguments` 区是如何被填充的。

**`FUNCTION_CALL`：真正触发调用** —— [src/njs_vmcode.c:L1522-L1539](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1522-L1539)：

```c
CASE (NJS_VMCODE_FUNCTION_CALL):
    value2 = (njs_value_t *) vmcode->operand1;          // retval 槽位
    vm->active_frame->native.pc = pc;                   // ★ 保存调用者 pc
    njs_vmcode_operand(vm, (njs_index_t) value2, value2);

    ret = njs_function_frame_invoke(vm, value2);        // ★ 分流执行
    ...
    ret = sizeof(njs_vmcode_function_call_t);
    BREAK;
```

`vm->active_frame->native.pc = pc` 这一步很关键：把「调用者当前指令地址」存进调用者帧的 `pc` 字段。对于普通同步调用，递归的解释器返回后调用者循环的局部 `pc` 自然就指向 FUNCTION_CALL 之后；但对于 **async/await**（u4-l5），函数会在中途挂起、之后跨事件循环「恢复」，这时就需要这个保存的 `pc` 让调用者从正确位置继续。

**分流执行 `njs_function_frame_invoke`** —— [src/njs_function.c:L660-L679](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L660-L679)：

```c
njs_int_t
njs_function_frame_invoke(njs_vm_t *vm, njs_value_t *retval)
{
    njs_native_frame_t  *frame = vm->top_frame;

    if (njs_function_object_type(vm, frame->function) == NJS_OBJ_TYPE_ASYNC_FUNCTION) {
        return njs_async_function_frame_invoke(vm, retval);   // async 特殊路径(u4-l5)
    }

    if (frame->native) {
        return njs_function_native_call(vm, retval);          // C 函数：内联调用
    } else {
        return njs_function_lambda_call(vm, retval, NULL);    // JS 函数：递归进解释器
    }
}
```

这是整个调用机制的「总岔路口」：async 走专门的续体机制，native 直接调 C，lambda 递归进解释器。

**lambda 调用：递归进解释器 + 指针切换** —— [src/njs_function.c:L568-L601](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L568-L601)（`njs_function_lambda_call` 的核心，承接 u4-l2）：

```c
/* Store current level. */
cur_local    = vm->levels[NJS_LEVEL_LOCAL];
cur_closures = vm->levels[NJS_LEVEL_CLOSURE];

/* Replace current level. */
vm->levels[NJS_LEVEL_LOCAL]   = vm->top_frame->local;        // f 的 LOCAL 数组
vm->levels[NJS_LEVEL_CLOSURE] = njs_function_closures(function); // f 的闭包数组
...
vm->active_frame = frame;

ret = njs_vmcode_interpreter(vm, lambda->start, retval, promise_cap, NULL);  // ★ 递归

/* Restore current level. */
vm->levels[NJS_LEVEL_LOCAL]   = cur_local;
vm->levels[NJS_LEVEL_CLOSURE] = cur_closures;
```

这就是 u4-l2 里「调用边界只换 LOCAL/CLOSURE 两个指针」的代码真身。`njs_vmcode_interpreter` 的递归调用意味着：**调用栈深度 = C 函数 `njs_vmcode_interpreter` 的递归深度**——这也解释了为什么栈深度保护（4.2）如此重要，它防止的正是这层 C 递归失控。

**native 调用：直接调 C 函数并自行弹帧** —— [src/njs_function.c:L636-L657](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L636-L657)（`njs_function_native_call` 核心）：

```c
call = function->u.native;

ret = call(vm, &native->arguments[-1], 1 /* this */ + native->nargs,
           function->magic8, retval);                       // ★ 直接 C 调用
...
njs_vm_scopes_restore(vm, native);                          // 自己弹帧
njs_function_frame_free(vm, native);
return NJS_OK;
```

native 调用没有递归进解释器：C 函数 `call(...)` 返回后，这里直接 `njs_vm_scopes_restore` 弹帧、`njs_function_frame_free` 释放，然后把控制权交还给 `FUNCTION_CALL` 分支。

**`RETURN`：弹帧 + 释放 + 退出本层解释器** —— [src/njs_vmcode.c:L1471-L1482](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1471-L1482) 触发 [src/njs_vmcode.c:L2631-L2649](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2631-L2649)（`njs_vmcode_return`）：

```c
frame = (njs_frame_t *) vm->top_frame;

if (frame->native.ctor) {                  // new 调用：返回值不是对象就用 this
    if (!njs_is_object(retval)) {
        retval = frame->native.local[0];
    }
}

njs_vm_scopes_restore(vm, &frame->native); // ★ top_frame 退回上一层
*dst = *retval;                            // 把返回值写到调用者指定的 retval 槽
njs_function_frame_free(vm, &frame->native);
```

随后 `RETURN` 分支 `return NJS_OK;` 退出当前这层 `njs_vmcode_interpreter`，回到 `njs_function_lambda_call`，再回到调用者的 `FUNCTION_CALL` 分支。弹帧逻辑 `njs_vm_scopes_restore` 在 [src/njs_vm.c:L639-L652](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L639-L652)：`vm->top_frame = native->previous;` 并把 `active_frame` 复原为 `previous_active_frame`。

### 4.3.4 代码实践

**实践目标**：在反汇编里亲眼认出「调用三件套」，并跟踪一次调用从建帧到返回的帧链变化。

**操作步骤**：

1. 构建 CLI（见 u1-l3）。
2. 运行反汇编（这里用一个会真正发生调用的脚本，与 u4-l2 不调用 `f` 的例子互补）：
   ```bash
   ./build/njs -d
   >> function f(v) { return v + 1 } f(5)
   ```
3. 在 `shell:main` 代码块里寻找调用三件套。对照指令结构体 [src/njs_vmcode.h:L288-L308](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L288-L308)（`njs_vmcode_function_frame_t` / `njs_vmcode_method_frame_t` / `njs_vmcode_function_call_t`）确认它们的操作数含义。
4. 想象执行到 `FUNCTION_CALL` 时，对照 [src/njs_function.c:L568-L601](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L568-L601) 说出「保存了哪两个 levels 指针、换成了什么、`njs_vmcode_interpreter` 递归后恢复了什么」。
5. 追到 `shell:f` 里的 `RETURN`，对照 [src/njs_vmcode.c:L2631-L2649](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2631-L2649) 说出「弹帧、写返回值、释放帧」三步。

**需要观察的现象**：`shell:main` 里应出现一组 `FUNCTION_FRAME` →（`PUT_ARG`）→ `FUNCTION_CALL` 的指令序列；`shell:f` 里以 `RETURN` 结尾。`FUNCTION` 指令（创建函数值）应出现在 `f` 首次被需要的地方。

**预期结果**：你能把反汇编里的指令序列与 4.3.2 的流程图一一对应，并能解释「为什么 `f(5)` 的执行会让 `njs_vmcode_interpreter` 递归一层、而 `Math.max(1,2)` 不会」。

> 待本地验证：具体指令的十六进制操作数与排列顺序请以你本地 `./build/njs -d` 的实际输出为准——不同版本的字节码生成器可能微调（例如实参可能由不同方式落入帧），但「FRAME → PUT_ARG → CALL」三段式结构稳定。

### 4.3.5 小练习与答案

**练习 1**：为什么 `f(5)`（f 是 lambda）会让进程的 C 调用栈变深，而 `Math.max(1,2)` 不会？

> **参考答案**：lambda 调用走 `njs_function_lambda_call`，它**递归调用** `njs_vmcode_interpreter`（[src/njs_function.c:L597](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L597)），每嵌套一层 JS 调用，C 栈上就多一层 `njs_vmcode_interpreter` 的栈帧。而 native 调用走 `njs_function_native_call`，直接 `call(...)` 调一个普通 C 函数，函数返回后立即弹帧，不产生新的解释器循环，C 栈深度基本不变。这也是内建函数快的原因之一。

**练习 2**：`FUNCTION_CALL` 里那句 `vm->active_frame->native.pc = pc;` 在普通同步调用里似乎「没用上」（调用者循环的局部 pc 自然正确）。它存在的真正意义是什么？

> **参考答案**：为 **async/await 的挂起与恢复** 服务（见 u4-l5）。当一个 async 函数 `await` 一个未决的 Promise 时，它会从解释器里「逃出去」并把当前调用现场（包括调用者的 `pc`）保存下来；等 Promise 决议、续体被 `njs_vm_execute_pending_job` 重新调度时，需要靠这个保存的 `pc` 让调用者从 `FUNCTION_CALL` 之后正确续执行。同步调用里它确实不直接影响控制流，但保存它几乎零成本，统一写更简洁。

**练习 3**：构造器调用 `new F()` 时，`F` 里的 `return` 一个非对象值（如 `return 5`）会发生什么？根据源码说明。

> **参考答案**：会被忽略，`new` 表达式的结果仍是 `this`（新创建的对象）。见 [src/njs_vmcode.c:L2638-L2642](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2638-L2642)：`if (frame->native.ctor) { if (!njs_is_object(retval)) retval = frame->native.local[0]; }`——`ctor` 帧且返回值不是对象时，把返回值改写成 `local[0]`（即 `this`）。这正是 ES 规范「构造器返回对象则用对象、否则用 this」的实现。

---

## 5. 综合实践：把一次调用「从字节码到帧链」完整走一遍

本任务把「函数表示 + 调用帧链 + 调用指令」三块串起来。请准备一段包含自由调用、方法调用和一层递归的最小脚本：

```js
var obj = { m: function(x) { return x * 2 } };
function f(n) { return obj.m(n) }      // 自由调用 f，内部又方法调用 obj.m
f(3)                                    // 顶层调用
```

**步骤 1：反汇编，识别指令种类。** 运行 `./build/njs -d` 后粘贴上面脚本。在 `shell:main` 与 `shell:f` 两段代码里找出：

- `FUNCTION`（创建函数值，对应 `function f` 和 `obj.m` 的字面量）；
- `FUNCTION_FRAME`（自由调用 `f(3)`）；
- `METHOD_FRAME`（方法调用 `obj.m(n)`）；
- `PUT_ARG`（每个实参一条）；
- `FUNCTION_CALL`（两次，分别触发 `f` 和 `obj.m`）；
- `RETURN`（`shell:f` 末尾）。

> 具体助记符与操作数请以本地输出为准；若某版本用等价方式编码调用，以实际为准。

**步骤 2：画出调用帧链的演变。** 假设执行到 `obj.m(n)` 的函数体内部时，画出此刻的 `top_frame` 链：

```
top_frame ──► [obj.m 的帧] ──► [f 的帧] ──► [全局帧] ──► NULL
                 native=0            native=0
                 local → m 的局部     local → f 的局部
```

回答：(a) 此刻 `vm->levels[NJS_LEVEL_LOCAL]` 指向哪个数组？(b) `vm->levels[NJS_LEVEL_GLOBAL]` 和 `[NJS_LEVEL_STATIC]` 自始至终有没有变过？(c) 这条链上共有几层 `njs_vmcode_interpreter` 的 C 递归？

**步骤 3：验证 native vs lambda 的差别。** 把 `obj.m` 换成一个内建方法，例如：

```js
function f(n) { return Math.max(n, 0) }
f(3)
```

再反汇编。注意 `Math.max` 不会作为独立代码块出现在反汇编里（它是 native），但调用它仍会产生 `METHOD_FRAME`/`PUT_ARG`/`FUNCTION_CALL` 序列。回答：这次执行 `Math.max` 时，C 调用栈上的 `njs_vmcode_interpreter` 递归层数比上一个例子（用 lambda `obj.m`）多还是少？为什么？

**预期结果**：

- 步骤 2：(a) 指向 `[obj.m 的帧].local`（`m` 自己的局部数组）；(b) GLOBAL/STATIC **从未改变**（印证 u4-l2 的「调用边界只换 LOCAL/CLOSURE」）；(c) 三层 C 递归（全局 + f + obj.m，都走 lambda 路径）。
- 步骤 3：**更少**。因为 `Math.max` 是 native，`njs_function_native_call` 直接调 C 函数、不递归进解释器，所以 `Math.max` 这一跳不增加 `njs_vmcode_interpreter` 的递归深度（只有「全局 + f」两层 lambda）。

> 待本地验证：步骤 1 的具体字节码、步骤 2 里 `obj`/`m` 等的槽位编号请以本地 `-d` 输出核对；但「帧链结构、GLOBAL/STATIC 不变、native 不增加解释器递归」三条结论稳定。

---

## 6. 本讲小结

- njs 用**同一个 `njs_function_t`** 表示两种函数，靠 `native` 位区分：`native=0` 时 `u.lambda` 指向字节码产物 `njs_function_lambda_t`（含 `start` 字节码入口、`nargs`/`nlocal`/`closures`）；`native=1` 时 `u.native` 是一个 C 函数指针 `njs_function_native_t`（[src/njs_value.h:L224-L249](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_value.h#L224-L249)、[src/njs.h:L118-L119](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L118-L119)）。
- 调用上下文用**调用帧链**管理：`vm->top_frame` 是当前最顶层帧，`previous` 串成调用栈；`njs_frame_t` 以 `njs_native_frame_t` 为首字段嵌套，实现「lambda 帧是 native 帧的子类」的多态（[src/njs_function.h:L40-L80](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.h#L40-L80)）。
- lambda 帧采用**「指针数组 + 值数组」双区布局**，正常槽位 `pi→vi`，被闭包捕获的槽位 `pi` 改指向堆副本；`local = new + args_count`，0 号槽是 `this`（[src/njs_function.c:L397-L441](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L397-L441)）。帧分配受 `spare_stack_size` 总预算保护，深递归触发 `RangeError` 而非崩溃（[src/njs_function.c:L470-L511](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L470-L511)）。
- 一次调用是**三件套协作**：`FUNCTION_FRAME`/`METHOD_FRAME` 建帧（后者多带一个 `this`）→ `PUT_ARG` 逐个写实参 → `FUNCTION_CALL` 保存调用者 pc 并触发 `njs_function_frame_invoke`（[src/njs_vmcode.c:L1484-L1539](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1484-L1539)）。注意 `FUNCTION` 是「创建函数值」而非调用。
- **native 调用直接执行 C 函数、自行弹帧，不进解释器；lambda 调用切换 LOCAL/CLOSURE 指针后递归进入 `njs_vmcode_interpreter`**——调用栈深度即 C 递归深度（[src/njs_function.c:L636-L679](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L636-L679)）。
- `RETURN` 经 `njs_vmcode_return` 完成「构造器返回值修正 → `njs_vm_scopes_restore` 弹帧 → 写返回值 → `njs_function_frame_free` 释放帧」并退出本层解释器（[src/njs_vmcode.c:L2631-L2649](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2631-L2649)、[src/njs_vm.c:L639-L652](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L639-L652)）。

## 7. 下一步学习建议

- **u4-l4 异常处理**：`throw` 抛出的异常如何沿 `top_frame` 链一层层回溯寻找最近的 `try/catch`？`njs_frame_t` 里多出来的 `exception` 链表头和 `njs_exception_t`（[src/njs_function.h:L66-L80](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.h#L66-L80)）正是为此而设。理解了本讲的帧链，u4-l4 的「异常回溯」就是顺水推舟。
- **u4-l5 Promise 与 async/await**：本讲多次提到 `FUNCTION_CALL` 里保存的 `vm->active_frame->native.pc` 和 `njs_async_function_frame_invoke` 分支——它们是 async 函数「挂起—恢复」的基石。下一讲会讲清一个 async 函数如何在不阻塞 C 栈的情况下跨事件循环续体。
- **回看 u4-l2 / u4-l1**：现在再读 u4-l2 的 `njs_function_frame_invoke` 指针切换、u4-l1 的「解释器退出仅有两条路」，你会对「值存在哪、函数怎么调、调用栈怎么进出」形成完整闭环。
- **进阶阅读**：`Function.prototype.call/apply/bind` 的实现在 [src/njs_function.c:L1209-L1418](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L1209-L1418)，它们最终都汇入本讲的 `njs_function_call2` → `njs_function_frame_invoke`，可以作为「公共 API 如何落到调用帧机制上」的练习材料。
