# njs_vm_t 生命周期：创建、编译、执行、销毁

## 1. 本讲目标

本讲是进入 njs 内核 `src/` 源码的第一站。学完后你应该能够：

- 说清一个 `njs_vm_t`（njs 虚拟机实例）从无到有、再到被销毁的完整生命周期，以及每个阶段由哪个公共 API 负责。
- 看懂 `njs_vm_opt_t` 选项结构体里 `init`、`interactive`、`sandbox`、`unsafe`、`module`、`disassemble`、`backtrace`、`ast` 等开关分别控制什么。
- 解释为什么 njs 要把「编译」和「执行」拆成两步，以及 `njs_vm_compile` 把字节码产物存到了 `vm` 的哪个字段、`njs_vm_start` 又从哪里取出来跑。
- 理解 `njs_vm_clone` 提供的「模板 VM 编译一次、每个请求克隆一份」隔离模型，并指出 `interactive` 模式为何会拒绝克隆。

承接上一讲 u1-l4：你已经知道 CLI 的 `njs_create_engine` 用函数指针表统一封装了 njs / QuickJS 两套引擎。本讲我们钻进函数指针表指向的「内置 njs 引擎」一侧，看清指针背后真正调用的那组公共 API。

---

## 2. 前置知识

### 2.1 什么是「嵌入一个 JS 引擎」

很多 JS 引擎（如 V8、QuickJS、njs）既可以做成独立程序，也可以作为「库」被一个更大的 C 程序调用。后者就叫**嵌入式使用**：你的 C 程序负责创建虚拟机、喂入源码、取出执行结果，JS 引擎只负责解释执行。

njs 的公共 API 就声明在 [src/njs.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h) 里，函数名几乎都以 `njs_vm_` 开头，`vm` 就是 **virtual machine（虚拟机）** 的缩写。一个 `njs_vm_t *` 句柄就是一台可用的 JS 虚拟机。

### 2.2 njs 的返回码约定

njs 的整数返回值有一套固定含义，本讲会反复出现，先记住：

| 宏 | 值 | 含义 |
|---|---|---|
| `NJS_OK` | `0` | 成功（或：没有更多待处理任务） |
| `NJS_ERROR` | `-1` | 出错/抛了异常 |
| `NJS_AGAIN` | `-2` | 需要再次尝试（异步未就绪） |
| `NJS_DECLINED` | `-3` | 不适用/拒绝（如属性处理器作用于错误对象） |

它们定义在 [src/njs_types.h:11-14](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_types.h#L11-L14)。

> 注意 `njs_vm_execute_pending_job` 返回值有第三种：返回 `1` 表示「成功执行了一个作业」，`NJS_OK`(0) 表示「队列已空」。这一点后面会用到。

### 2.3 内存池与 shared 结构

njs 几乎所有运行时分配都来自一个**内存池**（`njs_mp_t`，下一讲 u2-l3 会专门讲）。每台虚拟机持有一个 `vm->mem_pool`。销毁虚拟机只需销毁这个池，池里所有对象一次性回收。

另一关键是 `vm->shared`（`njs_vm_shared_t *`），它存放**可被多个克隆虚拟机共享**的只读资源：内建构造器、原型、原子表等。新建一台独立 VM 时会自己建 `shared`；克隆时则复用模板的 `shared`，从而省掉重复的内建初始化开销。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/njs.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h) | 公共头文件。声明所有 `njs_vm_*` API、`njs_vm_opt_t` 选项结构、各种值访问宏。**嵌入式用户唯一需要 `#include` 的核心头。** |
| [src/njs_vm.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c) | VM 生命周期的实现：`njs_vm_opt_init / create / compile / start / call / invoke / clone / destroy / enqueue_job / execute_pending_job` 都在这里。 |
| [src/njs_vm.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h) | 内部头文件。定义 `struct njs_vm_s`（即 `njs_vm_t` 的真身）、`njs_vm_shared_s`、`levels`/`hooks` 等枚举与字段。 |
| [external/njs_shell.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c) | CLI 入口。`njs_engine_njs_init/eval/destroy` 三个函数给本讲的 API 做了一个**真实、完整**的调用范例，本讲会反复对照它。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 VM 创建与选项**：`njs_vm_opt_t` 与 `njs_vm_create`。
- **4.2 编译与执行 API**：`njs_vm_compile` → `njs_vm_start`，以及 `njs_vm_call` / `njs_vm_invoke` 与 jobs 队列。
- **4.3 克隆与隔离**：`njs_vm_clone` 与 `njs_vm_destroy`。

### 4.1 VM 创建与选项

#### 4.1.1 概念说明

要「嵌入」njs，第一件事是创建一台虚拟机。但创建之前需要先准备好一份**选项结构体** `njs_vm_opt_t`，它告诉引擎：要不要开沙箱、是不是模块模式、源码文件名是什么、最多用多少栈、用哪份 shared 资源……所有这些开关都集中在一个结构体里。

创建 VM 分两步，这是 njs 公共 API 的固定写法：

```c
njs_vm_opt_t  vm_options;
njs_vm_opt_init(&vm_options);   // 第一步：零初始化 + 填默认值
/* ... 按需改写 vm_options 的字段 ... */
njs_vm_t *vm = njs_vm_create(&vm_options);   // 第二步：真正创建
```

为什么要拆出 `njs_vm_opt_init` 这一步？因为 `njs_vm_opt_t` 字段很多，直接声明一个局部变量后里面的随机内存会带来未定义行为。`njs_vm_opt_init` 先 `memzero` 整块清零，再补上必须的默认值（例如默认栈大小 `max_stack_size`），这样调用方只需改自己关心的几个字段，其余保持安全默认。

#### 4.1.2 核心流程

`njs_vm_create` 内部依次完成：

1. 创建内存池 `mp`（所有后续分配的来源）。
2. 从池里分配并清零 `njs_vm_t` 结构体，把 `mp` 记到 `vm->mem_pool`。
3. 初始化正则引擎、`values_hash`。
4. 把传入的 `options` 整体拷给 `vm->options`（按值拷贝，之后改 options 不再影响 vm）。
5. 处理 `shared`：如果调用方传了现成的 `shared` 就复用；否则调用 `njs_builtin_objects_create` 自己创建一份内建资源。
6. 记录 `external` 指针（宿主侧的「请求」或「会话」上下文，回调里可取回）。
7. 如果 `options->init` 为真，调用 `njs_vm_runtime_init` 建立运行时（活跃帧、jobs 队列等）。
8. 对所有内置模块依次调用 `preinit`、再调用 `njs_vm_protos_init` 物化原型/构造器、再调用各模块 `init`；用户传的 `addons` 模块也同样走一遍。
9. 分配 `undefined` 对应的作用域索引。

成功返回 `vm`，任何一步失败都 `return NULL`。

#### 4.1.3 源码精读

先看选项结构体本身。每个字段的作用在结构体上方那段注释里写得很清楚：

[src/njs.h:261-290 — 选项字段含义注释与字段定义](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L261-L290) 给出了每个开关的含义：`interactive` 启用 REPL、允许不克隆直接启动父 VM；`sandbox` 关闭文件访问；`unsafe` 放开 `Function` 构造器等危险特性；`module` 切到 ES6 模块模式；`ast` 打印语法树；等等。

其中几个本讲会反复用到：
- `interactive`（第 274 行）：是否交互式 REPL。**它会让 `njs_vm_clone` 直接返回 NULL**，见 4.3。
- `init`（第 276 行）：是否在 `create` 阶段就初始化运行时（建活跃帧）。
- `shared`（在结构体上方，第 251 行）：传入则复用、为空则新建。
- `external`（第 250 行）：宿主上下文指针，可被 `njs_vm_external_ptr` 取回。
- `addons`（第 253 行）：额外的原生模块数组。

接着看 `njs_vm_opt_init`，它非常短：

[src/njs_vm.c:21-27 — 选项清零并设默认栈大小](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L21-L27)。`njs_memzero` 把整个结构清零，再把 `max_stack_size` 设为 `NJS_MAX_STACK_SIZE`。

再看 `njs_vm_create` 的关键几段（函数整体在 [src/njs_vm.c:30-145](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L30-L145)）：

- 创建内存池并分配 vm：[src/njs_vm.c:39-49](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L39-L49)（`njs_mp_fast_create` 建池，`njs_mp_zalign` 从池里分配清零的 vm，`vm->mem_pool = mp`）。
- 选项拷贝与 shared 复用：[src/njs_vm.c:58-70](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L58-L70)。`vm->options = *options` 是按值拷贝；`shared` 为空时调 `njs_builtin_objects_create` 自建一份。`vm->external = options->external` 记下宿主指针。
- `init` 触发运行时初始化：[src/njs_vm.c:78-83](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L78-L83)。
- 模块 preinit/init 与原型物化：[src/njs_vm.c:85-142](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L85-L142)（先遍历 `njs_modules[]` 调 `preinit`，再 `njs_vm_protos_init`，再调各模块 `init`；`addons` 同样处理；最后分配 undefined 索引）。

最直观的真实范例是 CLI 自己的 `njs_engine_njs_init`，它把命令行选项翻译成 `njs_vm_opt_t`，再创建 VM：

[src/njs_shell.c:1322-1356 — CLI 填充选项并创建 VM](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L1322-L1356)。可以看到 `vm_options.init = 1`（CLI 需要运行时）、`sandbox/unsafe/module/interactive/disassemble` 全都来自命令行开关，最后 `vm = njs_vm_create(&vm_options)`。

#### 4.1.4 代码实践

**实践目标**：建立「命令行开关 → `njs_vm_opt_t` 字段 → VM 行为」的对应关系。

**操作步骤**：
1. 打开 [external/njs_shell.c:1322-1346](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L1322-L1346)，逐行看 CLI 给 `vm_options` 填了哪些字段。
2. 对照 [src/njs.h:261-273](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L261-L273) 的注释，判断每个字段会带来什么效果。例如 `vm_options.sandbox = opts->sandbox;` 对应 `-s` 选项，含义是「禁用文件访问」。
3. 注意第 1333 行 `vm_options.unsafe = !opts->safe;`——`unsafe` 是 `safe` 取反，即默认（`safe=0`）时 `unsafe=1`，所以 CLI 默认就放开了 `Function` 构造器等特性。

**需要观察的现象**：CLI 命令行里没有任何一个开关直接叫「init」，但 `init` 在代码里被硬编码成 `1`。这说明 `init` 不是面向用户的开关，而是「这台 VM 要不要立刻能跑代码」的内部标记。

**预期结果**：你应能在一张表里列出 `interactive`/`disassemble`/`backtrace`/`quiet`/`sandbox`/`unsafe`/`module`/`ast` 各自对应的 CLI 选项（`-e`/`-d`/…/`-s`/`-u`/`-m`/…，参见 u1-l4）和它开启后引擎表现出的不同。

> ⚠️ 待本地验证：上表里把字段名和 CLI 短选项一一对应时，请回到 u1-l4 讲义里 `njs_options_parse` 的 switch 分支核对，不要凭记忆。

#### 4.1.5 小练习与答案

**练习 1**：如果不调用 `njs_vm_opt_init` 而是直接 `njs_vm_opt_t vm_options;` 然后立刻 `njs_vm_create`，会发生什么风险？

**参考答案**：`max_stack_size` 字段会是栈上的随机值，可能导致 VM 栈大小异常（过大或为 0）；其它位标志也可能是随机非零，意外开启 sandbox/unsafe 等行为。`njs_vm_opt_init` 用 `memzero` + 默认值规避了这一切。

**练习 2**：CLI 为何把 `init` 写死成 `1`？在什么场景下你可能会设 `init = 0`？

**参考答案**：CLI 创建 VM 就是为了立刻执行代码，所以必须初始化运行时（活跃帧、jobs 队列）。`init=0` 的典型场景是「只想要一个最小结构、稍后再手动调 `njs_vm_runtime_init`」，比如某些克隆或批量构造路径。本讲不展开，记住 `init` 控制运行时是否就绪即可。

---

### 4.2 编译与执行 API

#### 4.2.1 概念说明

VM 创建好之后，它还「空空如也」——既没有源码也没有字节码。让一段 JS 真正能跑，需要两步：

1. **编译** `njs_vm_compile`：把源码字符串（`start` 到 `end` 的字节区间）经过「词法 → 解析（AST）→ 字节码生成」流水线，产出可执行的字节码，挂到 VM 上。
2. **执行** `njs_vm_start`：从 VM 上取出字节码入口，进入解释器主循环执行「全局代码」（即脚本顶层语句）。

为什么要拆开？因为编译是相对昂贵的操作，执行却很频繁。njs 的设计是「**编译一次，克隆多次执行**」：在配置阶段编译一份模板字节码，运行期对每个请求克隆一份去执行。这个模型在 NGINX 集成（u8 单元）里会大量出现。

除了「跑全局代码」，API 还提供了「**调用某个具体函数**」的能力：`njs_vm_call` / `njs_vm_invoke`。NGINX 处理一个请求时，并不是重跑整段脚本，而是取出脚本里 `export` 出来的那个处理函数，用 `njs_vm_call` 调它。

此外，涉及 Promise / async 的代码还需要一个**作业队列**（jobs）：`njs_vm_enqueue_job` 入队、`njs_vm_execute_pending_job` 出队执行一个。本讲先建立 API 印象，深入留到 u4-l5。

#### 4.2.2 核心流程

**编译流水线**（`njs_vm_compile`）：

```
源码 [start, end)
   │  njs_parser_init
   ▼
parser 状态机 ──► njs_parser ──► AST (parser.node / parser.scope)
   │  (若 options.ast 则打印 AST)
   ▼
njs_generator_init + njs_generate_scope ──► 字节码 generator.code_start
   │  扩容全局 levels、绑定 globalThis
   ▼
vm->start = generator.code_start   (若 options.disassemble 则反汇编打印)
```

编译完成后，字节码入口指针被存进 `vm->start`，全局作用域存进 `vm->global_scope`。`*start` 也被推进到「已消费」的位置（用于多段连续编译）。

**执行**：

- `njs_vm_start(vm, retval)`：直接 `njs_vmcode_interpreter(vm, vm->start, retval, NULL, NULL)`，即从 `vm->start` 进入解释器主循环跑全局代码。返回 `NJS_ERROR` 表示抛了异常，否则视为成功。
- `njs_vm_call(vm, function, args, nargs)`：内部转给 `njs_vm_invoke`，后者用 `njs_function_frame` 建帧、`njs_function_frame_invoke` 调用。
- `njs_vm_execute_pending_job(vm)`：从 `vm->jobs` 队列取一个作业，用 `njs_vm_call` 执行它。返回 `1`=跑了一个、`NJS_OK(0)`=队列空、`NJS_ERROR`=出错。

#### 4.2.3 源码精读

先看 `njs_vm_compile` 的核心（函数在 [src/njs_vm.c:211-304](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L211-L304)）：

- 初始化解析器并跑解析：[src/njs_vm.c:228-237](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L228-L237)（`njs_parser_init` 后 `njs_parser` 产出 AST，失败返回 `NJS_ERROR`）。
- 可选打印 AST：[src/njs_vm.c:239-254](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L239-L254)（只有 `vm->options.ast` 为真时才序列化并打印，所以 CLI 的 `--ast` 选项真正生效点就在这里）。
- 生成字节码：[src/njs_vm.c:259-272](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L259-L272)（`njs_generator_init` 后 `njs_generate_scope` 发射指令，产物是 `code`）。
- 扩容全局 levels 并绑定 `globalThis`：[src/njs_vm.c:274-294](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L274-L294)（这是本讲的**关键落点**——编译结果最终通过 `vm->start = generator.code_start` 与 `vm->global_scope = scope` 挂到 VM 上）。
- 可选反汇编：[src/njs_vm.c:296-303](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L296-L303)。`vm->options.disassemble` 为真时调用 `njs_disassembler(vm)` 打印字节码——这正是上一讲 u1-l4 里 `-d` 选项的真正落地处。

再看执行，三处都很短：

- `njs_vm_start`：[src/njs_vm.c:694-702](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L694-L702)，一行 `njs_vmcode_interpreter`，把 `NJS_ERROR` 透传、其余归为 `NJS_OK`。
- `njs_vm_call` / `njs_vm_invoke`：[src/njs_vm.c:612-635](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L612-L635)。`njs_vm_call` 只是 `njs_vm_invoke` 的包装（丢弃返回值）；`njs_vm_invoke` 走「建帧 → 调用」两步。
- jobs 队列入队/出队：[src/njs_vm.c:662-691](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L662-L691)（`enqueue_job` 把 `njs_event_t` 挂到 `vm->jobs` 队尾）与 [src/njs_vm.c:705-731](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L705-L731)（`execute_pending_job` 取队首、`njs_vm_call` 执行，返回 `1`/`NJS_OK`/`NJS_ERROR`）。

最完整的真实串接仍是 CLI 的 eval：

[src/njs_shell.c:1398-1415 — CLI 的 compile → start 串接](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L1398-L1415)。注意第 1409 行的判断 `if (ret == NJS_OK && start == end)`——只有编译成功**且源码被完整消费**（`start` 推进到了 `end`）才调 `njs_vm_start`。这能正确处理「源码末尾有未消化字符」这类编译错误。

#### 4.2.4 代码实践

**实践目标**：画出「一段源码字符串」到「`njs_vm_start` 可执行」的状态流转，并验证 `disassemble` 开关。

**操作步骤**：

1. **画状态流转图**。基于 4.2.2 的流程，用你自己的话把下面这几个状态节点连起来，并在箭头上标注触发的 API/函数：

   ```
   [源码字符串]
        │  (??)
   [parser 中间态 / AST]
        │  (??)
   [generator / scope]
        │  (??)
   [vm->start 指向字节码]
        │  njs_vm_start
   [njs_vmcode_interpreter 执行]
   ```
   把每个 `(??)` 替换成对应函数名（提示：`njs_parser`、`njs_generate_scope`、赋值语句）。

2. **跑一次反汇编**（承接 u1-l4，你应已有 `build/njs`）：

   ```bash
   ./build/njs -d -c 'var a=42; function f(v){return v+1}'
   ```

   `-d` 在 `njs_options_parse` 里把 `opts->disassemble` 置 1，再经 `njs_engine_njs_init` 写进 `vm_options.disassemble`，最终在 [src/njs_vm.c:299-301](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L299-L301) 触发 `njs_disassembler(vm)`。

**需要观察的现象**：加 `-d` 与不加 `-d`，控制台输出有何不同（前者多出一段 `0000 MOVE ...` 之类的字节码列表）。同时注意：因为有 `-d`，`njs_vm_compile` 内部会打印字节码，但**这并不等于执行**——执行仍由随后的 `njs_vm_start` 完成。

**预期结果**：你能解释「`-d` 让 VM 在编译结束时打印字节码，但不会改变执行结果」这件事，并指出该开关的落地代码行。

> ⚠️ 待本地验证：第 2 步的具体字节码内容请以你本机构建产物实际输出为准，不要照抄；字节码格式的细节留到 u3-l5（反汇编）讲义。

#### 4.2.5 小练习与答案

**练习 1**：`njs_vm_start` 返回 `NJS_OK`，是否就代表 JS 代码「全部正确跑完」、没有任何遗留工作？

**参考答案**：不一定。`njs_vm_start` 只表示「全局代码这一轮同步执行完毕、没有抛异常」。但如果脚本里安排了 Promise 的 `then` 回调或 `await` 续体，这些会作为 job 进 `vm->jobs` 队列，需要宿主循环调用 `njs_vm_execute_pending_job` 才会真正执行（详见 CLI 的 [njs_shell.c:1384-1389](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L1384-L1389) 排空循环）。所以「全局代码跑完」≠「所有异步回调跑完」。

**练习 2**：CLI 的 `njs_engine_njs_eval` 为什么要额外判断 `start == end` 才执行？

**参考答案**：`njs_vm_compile` 会把 `*start` 推进到「已解析」的位置。如果源码末尾有无法解析的残渣（语法错误），`*start` 不会等于 `end`，此时即使 `njs_vm_compile` 返回了 `NJS_OK`（部分场景下编译器对前缀成功），也不能 `start`，否则会漏掉未解析代码或行为异常。`start == end` 保证「整段源码被完整消费」。

---

### 4.3 克隆与隔离

#### 4.3.1 概念说明

设想 NGINX 这样的多请求服务器：每秒可能处理成千上万个请求，每个请求都要跑 JS。如果每个请求都从头 `njs_vm_create` + `njs_vm_compile` 一遍，开销会非常恐怖（要重新解析、重新生成字节码、重新物化所有内建对象）。

njs 的解法是 **clone（克隆）**：

- 配置阶段创建一个**模板 VM**，编译好脚本，得到字节码。
- 每来一个请求，对模板 VM 调 `njs_vm_clone`，得到一个**新 VM**：它**复用**模板的 `shared`（内建、原子表等只读资源）和字节码，但拥有**独立**的内存池、运行时帧、`external` 指针、全局变量槽位。

这样多个请求的 VM 之间互不干扰（独立内存、独立全局状态），又共享了昂贵的编译产物。这就是 njs 在 NGINX 里实现「每请求隔离」的核心机制，u8 单元会把它和 NGINX 的请求生命周期对接。

销毁则极简：`njs_vm_destroy(vm)` 就是 `njs_mp_destroy(vm->mem_pool)`——一把销毁内存池，池内所有对象全部回收，不需要逐个 free。这种「池式生命周期管理」是 njs（也是 NGINX 风格）的内存哲学，下一讲 u2-l3 会展开。

#### 4.3.2 核心流程

**克隆** `njs_vm_clone(vm, external)`：

```
1. 若 vm->options.interactive → 直接返回 NULL（交互式 VM 不允许克隆）
2. 为新 VM 新建独立内存池 nmp
3. *nvm = *vm        // 结构体整体按值拷贝（拿到 levels/shared/字节码指针等）
4. nvm->mem_pool = nmp；nvm->external = external（每个克隆自己的宿主上下文）
5. 重置 nvm 的私有 atom_hash、重建 njs_vm_runtime_init / njs_vm_protos_init
6. 为 nvm 重新分配独立的全局 levels 槽位（njs_scope_make）
7. 绑定 nvm 自己的 globalThis
失败 → 销毁新池，返回 NULL
```

注意第 3 步 `*nvm = *vm` 是**浅拷贝**：指针字段（如 `shared`、`levels`、`atom_hash_shared`）会指向同一处，所以 `shared` 被复用；但第 5-6 步会**覆盖**掉那些本该私有的字段（独立 runtime、独立全局槽），从而实现隔离。

**销毁** `njs_vm_destroy(vm)`：

```
njs_mp_destroy(vm->mem_pool)   // 一把回收整个池，结束
```

#### 4.3.3 源码精读

克隆函数整体在 [src/njs_vm.c:391-473](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L391-L473)。关键点：

- **`interactive` 拒绝克隆**：[src/njs_vm.c:401-403](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L401-L403)——这正是本讲实践题第二问的答案所在。
- 新建池与浅拷贝：[src/njs_vm.c:405-419](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L405-L419)（`njs_mp_fast_create` 建独立池；`*nvm = *vm` 整体拷贝；`nvm->mem_pool = nmp`、`nvm->external = external` 切到克隆自己的资源/上下文）。
- 私有 atom 表与运行时重建：[src/njs_vm.c:421-434](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L421-L434)（重置 `atom_hash`、`atom_hash_current`，调 `njs_vm_runtime_init` 和 `njs_vm_protos_init`）。注意：这里**重建了原型/构造器**，但它们引用的 `shared` 还是模板那份。
- 独立全局 levels 与 globalThis：[src/njs_vm.c:436-464](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L436-L464)（`njs_scope_make` 分配新槽位，`nvm->levels[NJS_LEVEL_GLOBAL] = global`，并把 `nvm->levels[NJS_LEVEL_LOCAL]` 置空——LOCAL 级别是每个调用帧自己的，克隆时本来就该空）。

要理解「哪些私有、哪些共享」，看 `njs_vm_t` 结构体最清楚：

[src/njs_vm.h:118-189 — `struct njs_vm_s` 字段全貌](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L118-L189)。几个关键字段：
- `exception`（第 119 行）：每个 VM 各自的当前异常值。
- `levels[NJS_LEVEL_MAX]`（第 124 行）：各级作用域存储指针，克隆后全局级是独立的。`NJS_LEVEL_*` 枚举见 [src/njs_vm.h:109-115](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L109-L115)（LOCAL/CLOSURE/GLOBAL/STATIC 四级，u4-l2 详讲）。
- `external`（第 126 行）：宿主上下文，每个克隆自己的。
- `top_frame` / `active_frame`（第 128-129 行）：调用帧栈，私有。
- `atom_hash`（第 132 行）私有 vs `atom_hash_shared`（第 131 行）共享。
- `jobs`（第 142 行）：Promise 作业队列，私有。
- `mem_pool`（第 155 行）：私有内存池。
- `start`（第 157 行）：字节码入口指针——克隆后仍指向模板的字节码，**共享**。
- `shared`（第 160 行）：共享只读资源，**复用模板的**。

销毁就一行：

[src/njs_vm.c:204-208 — `njs_vm_destroy` 只销毁内存池](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L204-L208)。

真实范例看 CLI 的销毁函数，它演示了销毁前应做的「善后」：

[src/njs_shell.c:1377-1395 — CLI 销毁前的善后与销毁](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L1377-L1395)。顺序是：① 调 `njs_vm_call_exit_hook`（执行注册的 exit 钩子）；② `for(;;)` 循环排空 `njs_vm_execute_pending_job` 直到返回 `NJS_OK`（保证所有 Promise 回调都跑完）；③ 才 `njs_vm_destroy`。这个顺序很有讲究：必须在销毁内存池**之前**把还挂在池上的 job 跑完，否则 job 里引用的对象会随池一起消失。

> 补充：`njs_vm_destroy` 之外还有一个轻量的 `njs_vm_reuse`（[src/njs_vm.c:381-388](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L381-L388)），它不重建 VM，只清空帧/模块表并把全局对象重新标记为 shared——用于「同一台 VM 在 REPL 里复用」的场景。本讲了解即可。

#### 4.3.4 代码实践

**实践目标**：理解 `interactive` 如何影响克隆行为，并掌握销毁前的正确善后顺序。

**操作步骤**：

1. **定位 `interactive` 的拦截点**。打开 [src/njs_vm.c:401-403](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L401-L403)。确认逻辑：只要模板 VM 的 `options.interactive` 为真，`njs_vm_clone` 立刻返回 `NULL`，根本不进入克隆流程。回到 [src/njs.h:262-263](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L262-L263) 的注释：`interactive` 模式的设计本意就是「允许不克隆直接启动父 VM」（REPL 场景），所以它和克隆是互斥的。

2. **解释为什么交互式不能克隆**。在 REPL 里，用户在**同一台** VM 上一行行输入、反复编译执行，全局变量要在行与行之间保持。如果每行都克隆一台新 VM，之前定义的变量/函数就丢了。所以 REPL 路径直接用父 VM（配合 `njs_vm_reuse`），不走 clone。

3. **梳理销毁顺序**。对照 [njs_shell.c:1382-1391](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L1382-L1391)，把销毁前的三步写下来：① `njs_vm_call_exit_hook`；② `for(;;)` 排空 jobs；③ `njs_vm_destroy`。思考：如果跳过第 ② 步直接 destroy，会发生什么？

**需要观察的现象**（阅读型，不需运行）：你应能口述「调用 `njs_vm_call` 执行一次函数」的内部路径——`njs_vm_call` → `njs_vm_invoke` → `njs_function_frame`（建帧）→ `njs_function_frame_invoke`（执行）。这条链路在 [src/njs_vm.c:612-635](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L612-L635)。

**预期结果**：你能回答两个问题——(a) REPL 为什么不能用 clone（变量跨行保持的需要）；(b) 销毁前为什么要排空 jobs（避免引用了池内对象的回调随池一起被释放）。

> ⚠️ 待本地验证：若你想亲眼看到 `interactive` 的效果，可在阅读 [njs_shell.c:1328](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L1328) 后，用 `./build/njs`（无参数，交互式）连续输入 `var a=1` 回车、`a+1` 回车，确认第二行能读到第一行的 `a`——这正是「不克隆、复用同一台 VM」的表现。

#### 4.3.5 小练习与答案

**练习 1**：克隆出来的新 VM，下列哪些是「私有」（每个克隆独立）、哪些是「共享」（复用模板）？`mem_pool`、`shared`、`start`（字节码）、`jobs`、`external`、`exception`。

**参考答案**：
- 私有：`mem_pool`（克隆时新建池）、`jobs`（runtime_init 时新建队列）、`external`（每个克隆传入自己的宿主指针）、`exception`（结构体各自一份）。
- 共享：`shared`（指向模板的内建资源，不重建）、`start`（字节码入口指针，`*nvm = *vm` 浅拷贝得到，仍指向模板字节码）。
- 全局 `levels[NJS_LEVEL_GLOBAL]` 虽然结构体浅拷贝时共享了指针，但克隆流程里会重新分配独立槽位覆盖掉，所以最终也是私有的。

**练习 2**：为什么 `njs_vm_destroy` 只有一行 `njs_mp_destroy`，却不会「漏掉」任何资源？

**参考答案**：因为这台 VM 的**所有**运行时分配（对象、字符串、帧、atom 表项……）都来自 `vm->mem_pool` 这一个池。池式管理的本质就是「整体生命周期」：销毁池 = 一次性回收池内全部内存。所以不需要、也不应该逐对象 free。代价是：单台 VM 内分配的对象不能比池活得更久——这也是为什么 jobs 必须在 destroy 之前跑完（job 回调引用的对象就住在池里）。

---

## 5. 综合实践

把三个模块串起来，完成下面这个「**复述 CLI 的 VM 生命周期**」综合任务。

**任务**：对照 `external/njs_shell.c` 里 `njs_engine_njs_init` / `njs_engine_njs_eval` / `njs_engine_njs_destroy` 三个函数，画出一次 `./build/njs -c 'console.log(42)'` 执行所经历的完整 VM 生命周期时序，标注每一步调用了哪个公共 API、该 API 内部又调了哪个关键内部函数。

**要求时序至少包含**：

1. `njs_vm_opt_init`（清零 + 默认栈）。
2. 填充 `vm_options`（指明 `init=1`、`disassemble` 来自 `-d` 等）。
3. `njs_vm_create`（内部：建池、建 shared 或复用、`njs_vm_runtime_init`、`njs_vm_protos_init`、模块 preinit/init）。
4. `njs_vm_compile`（内部：`njs_parser_init` → `njs_parser` → `njs_generate_scope`，字节码落到 `vm->start`）。
5. `njs_vm_start`（内部：`njs_vmcode_interpreter(vm, vm->start, ...)`）。
6. 若有异步：`njs_vm_execute_pending_job` 排空 `vm->jobs`。
7. `njs_vm_destroy`（内部：`njs_mp_destroy(vm->mem_pool)`）。

**进阶思考**：把上面的「`-c` 一行命令」路径，和 NGINX 里「配置期编译模板 VM、每请求 `njs_vm_clone` + `njs_vm_call` 处理函数、请求结束 `njs_vm_destroy`」的路径并排对比，指出二者在「编译次数」「VM 数量」「执行入口（`start` vs `call`）」三个维度上的差异。这个对比将直接为 u8 单元（NGINX 集成）铺路。

> 这个综合实践以源码阅读 + 画图为主，不需要运行；但鼓励你构建出 `build/njs` 后，在时序的每一步旁边补上一句「这一步如果出问题，控制台会看到什么」（例如 `create` 失败打印 `failed to create vm`，见 [njs_shell.c:1353-1356](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L1353-L1356)）。

---

## 6. 本讲小结

- 一个 njs VM 的标准生命周期是：`njs_vm_opt_init`（清零 + 默认值）→ 填 `njs_vm_opt_t` → `njs_vm_create`（建池/建 shared/初始化运行时与内建）→ `njs_vm_compile`（源码→字节码，挂到 `vm->start`）→ `njs_vm_start`（跑全局代码）或 `njs_vm_call`（调指定函数）→ 最后 `njs_vm_destroy`（销毁内存池）。
- `njs_vm_opt_t` 的开关（`interactive`/`sandbox`/`unsafe`/`module`/`disassemble`/`ast`/`init`/`shared`/`external`/`addons`）分别控制交互式、文件访问、危险特性、模块模式、反汇编、AST 打印、运行时初始化、共享资源复用、宿主上下文、原生模块；它们在 CLI 里由命令行选项填充（[njs_shell.c:1322-1346](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L1322-L1346)）。
- 编译与执行分离：`njs_vm_compile` 把字节码入口写进 `vm->start`，`njs_vm_start` 从 `vm->start` 进入解释器主循环；`-d` 反汇编、`--ast` 打印 AST 的真正生效点都在 compile 内部。
- `njs_vm_clone` 实现「模板编译一次、每请求克隆一份」的隔离：浅拷贝结构体后复用 `shared`/字节码，但重建私有 runtime、atom 表、全局 levels，并切换到各自的 `external`；`interactive` 模式会直接拒绝克隆。
- `njs_vm_destroy` 只销毁内存池这一行，体现了 njs 的池式整体生命周期哲学——因此销毁前必须先排空 `jobs` 队列、跑完 exit 钩子（见 CLI [njs_shell.c:1382-1391](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L1382-L1391)）。
- Promise/async 的作业机制由 `njs_vm_enqueue_job` 入队、`njs_vm_execute_pending_job` 出队执行，返回 `1`/`NJS_OK`/`NJS_ERROR` 三态。

---

## 7. 下一步学习建议

本讲你掌握了 VM 的「外壳」——生命周期 API。但要真正理解这些 API 操纵的内部状态，需要钻进 VM 内部的数据结构。建议按以下顺序继续：

1. **u2-l2（16 字节值表示 `njs_value_t`）**：本讲反复出现 `njs_value_t`（异常、参数、返回值），下一讲拆开这 16 字节，讲清类型标签与 payload。这是所有 API 传递「JS 值」的基础。
2. **u2-l3（内存池 `njs_mp` 与 `njs_flathsh` 哈希表）**：本讲的 `mem_pool`、`atom_hash`、`values_hash` 都是这一讲的主角，讲清池式分配和扁平哈希如何支撑 VM 的所有状态。
3. **u2-l4（Atom 表）**：本讲克隆时看到的 `atom_hash`（私有）与 `atom_hash_shared`（共享）的区分，下一讲给出完整解释。
4. 想提前看到这套生命周期 API「在 NGINX 里怎么用」，可以略读 [nginx/ngx_js.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c) 里的 clone/cleanup 调用——但完整理解建议留到 u8 单元。
