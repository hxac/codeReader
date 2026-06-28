# Promise 与 async/await 的 VM 支持

## 1. 本讲目标

本讲是「字节码执行引擎」单元的最后一讲。在前四讲里，我们已经看到 njs 如何把源码编译成字节码、如何在解释器主循环里取指执行、如何用调用帧链支撑函数调用、又如何用 try/catch 处理异常。但这些都是**同步**的——一段代码从头跑到尾，期间不会「暂停」。

真实的 JS 代码大量使用异步：`Promise`、`then`、`async/await`。它们要求引擎能够**在某个点挂起当前执行，等条件满足后再继续**。njs 作为单线程、寄存器式 VM，没有操作系统的线程切换可用，它必须用自己的机制来模拟这种「挂起—恢复」。

学完本讲，你应该能够：

- 说出 `Promise` 在 njs 内部的状态机表示（pending/fulfilled/rejected），以及 resolve/reject 如何在 VM 层被实现。
- 解释 `vm->jobs` 作业队列的作用，以及为什么「`then` 的回调不是立即执行，而是被排进队列」。
- 描述 `await` 在字节码层是如何被「展开」的：挂起当前帧、把续体注册到 Promise、返回 `NJS_AGAIN`、最后在作业里恢复执行。
- 理解 rejection tracker（拒绝追踪器）与「未处理拒绝」（unhandled rejection）的关系，知道宿主（NGINX/CLI）如何借此报警。

## 2. 前置知识

本讲假设你已经读过 u4-l1～u4-l4，具备以下认知：

- **解释器主循环**（u4-l1）：njs 用 `njs_vmcode_interpreter` 取指分发，指令返回值 `ret` 既表示本条指令长度，也可能是跳转偏移；遇到 `STOP`/`RETURN` 正常退出。
- **作用域寻址**（u4-l2）：运行期值存放在 `vm->levels[level][slot]` 四级存储里，由 `njs_scope_value` 解码 index 定位。
- **调用帧链**（u4-l3）：每次函数调用建立 `njs_frame_t`，靠 `previous` 串成调用栈；`vm->top_frame`/`vm->active_frame` 指向当前帧；切换帧时要同步切换 `vm->levels[NJS_LEVEL_LOCAL]` 与 `NJS_LEVEL_CLOSURE` 两个指针。
- **返回码约定**（u2-l1）：`NJS_OK`/`NJS_ERROR`/`NJS_AGAIN`/`NJS_DECLINED`。本讲最关键的是 **`NJS_AGAIN`**——它表示「本帧被挂起，尚未结束，请稍后恢复」。

如果你还不熟悉 JS 里的 Promise 语义（then 链、微任务、resolve/reject），建议先补一下 [MDN Using Promises](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Guide/Using_promises)。本讲聚焦「njs 是怎么实现这些语义的」，不会从零教 JS 的 Promise 用法。

> 术语提醒：JS 规范里把 `then` 回调的执行称为「微任务（microtask）」。njs 没有用这个词，它把这些待执行的回调统一叫 **job**，放在 `vm->jobs` 队列里。两者在概念上等价。

## 3. 本讲源码地图

本讲涉及四个核心源码文件：

| 文件 | 作用 |
|---|---|
| `src/njs_promise.c` | Promise 的全部实现：构造、状态机、resolve/reject、then、reaction job、all/race/any 等。是本讲篇幅最大的文件。 |
| `src/njs_promise.h` | Promise 私有数据结构 `njs_promise_data_s` 与几个对外函数声明。 |
| `src/njs_async.c` | async/await 的「续体」实现：`njs_await_fulfilled`（恢复挂起的函数）与 `njs_async_function_frame_invoke`（async 函数的包装调用）。 |
| `src/njs_vm.c` | 作业队列的三件套：`njs_vm_enqueue_job`（入队）、`njs_vm_execute_pending_job`（出队执行）、`njs_vm_pending`（判空），以及 rejection tracker 的注册入口。 |

辅助文件（提到但不在主线展开）：`src/njs_vmcode.c`（`NJS_VMCODE_AWAIT` 指令的处理函数 `njs_vmcode_await`）、`src/njs_vmcode.h`（指令格式）、`src/njs_async.h`（续体上下文结构）、`src/njs_event.h`（job 的载体 `njs_event_t`）、`src/njs.h`（rejection tracker 类型定义）、`external/njs_shell.c`（CLI 如何排空 job 队列）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 Promise 状态机**、**4.2 jobs 作业队列**、**4.3 async/await 展开**。三者层层递进——状态机决定 Promise 何时结算，作业队列决定结算后回调何时跑，async/await 则把「挂起—恢复」架在这两者之上。

### 4.1 Promise 状态机

#### 4.1.1 概念说明

一个 Promise 对象在任意时刻处于三种状态之一：

- `pending`（待定）：还没结算。
- `fulfilled`（已完成）：已成功，带一个结果值。
- `rejected`（已拒绝）：已失败，带一个拒绝原因。

状态转移是**单向且一次性**的：`pending → fulfilled` 或 `pending → rejected`，一旦离开 `pending` 就再也不能改。这正是 JS 规范「A promise is settled at most once」的要求。

在 `pending` 期间，可以通过 `then(onFulfilled, onRejected)` 注册回调。这些回调不会立刻跑，而是被存进 Promise 内部的两个队列（成功队列、失败队列）；等 Promise 结算时，再把对应队列里的回调「触发」（trigger）出去。如果 `then` 被调用时 Promise 已经结算，则回调也不立即跑，而是直接排成一个作业（见 4.2）。

#### 4.1.2 核心流程

Promise 结算与回调触发的流程可以画成：

```
new Promise(executor)
   │
   ├─ njs_promise_alloc()        分配 promise + data，state=PENDING
   ├─ create_resolving_functions()  生成一对 resolve/reject，共享 resolved_ref
   └─ 调用 executor(resolve, reject)
              │
              └─ executor 内部最终调用 resolve(x) 或 reject(e)
                       │
                       ├─ resolve(x)：x 非 thenable → njs_promise_fulfill()
                       │             x 是 thenable → 入队 resolve_thenable_job
                       └─ reject(e)：njs_promise_reject()

njs_promise_fulfill / njs_promise_reject
   ├─ 设置 data->state = FULFILL / REJECTED
   ├─ 设置 data->result = 值/原因
   ├─ （reject 时若 !is_handled，通知 rejection_tracker）
   └─ njs_promise_trigger_reactions(queue)
            └─ 遍历队列里的每个 reaction，njs_vm_enqueue_job(reaction_job)
```

关键点：**触发回调 = 把回调包装成 job 入队**，而不是直接调用。这就是 Promise 「异步」特性的根源。

#### 4.1.3 源码精读

**状态枚举与私有数据。** Promise 的三态定义在公共头里：

[njs.h:141-145](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs.h#L141-L145) — 定义 `NJS_PROMISE_PENDING=0 / FULFILL / REJECTED` 三个状态值，顺序就是 pending→fulfill→rejected。

[njs_promise.h:17-23](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.h#L17-L23) — Promise 的私有数据 `njs_promise_data_s`：`state`（当前状态）、`result`（结算值/原因）、`fulfill_queue`/`reject_queue`（两条等待回调链表）、`is_handled`（是否已有人 attach 过 handler，用于 unhandled rejection 判断）。注意这两条队列就是 `njs_queue_t` 链表。

**分配一个 pending Promise。**

[njs_promise.c:108-147](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L108-L147) — `njs_promise_alloc` 一次性 `njs_mp_alloc` 出「对象头 + 私有数据」连续的一块内存（`sizeof(njs_promise_t) + sizeof(njs_promise_data_t)`），把 `data->state` 置为 `NJS_PROMISE_PENDING`，初始化两条空队列。这块内存由 VM 内存池持有（承接 u2-l3 的池式分配）。

**构造函数与 executor。**

[njs_promise.c:150-177](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L150-L177) — `njs_promise_constructor` 是 `new Promise(executor)` 的入口：先校验必须用 `new`、且 executor 是函数，再交给 `njs_promise_constructor_call`。

[njs_promise.c:202-236](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L202-L236) — `njs_promise_constructor_call` 是构造的核心：先 `njs_promise_create_resolving_functions` 生成 resolve/reject，再用 `njs_function_call` 调用 executor 并把这对函数传进去。特别注意末尾的 `if (ret != NJS_OK)` 分支——**如果 executor 抛了异常，构造器会自动用 reject 把这个异常喂给 Promise**，这正是 `new Promise(() => { throw 'x' })` 得到 rejected promise 的原因。

**一次性结算：resolve/reject 函数。**

[njs_promise.c:278-313](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L278-L313) — `njs_promise_create_resolving_functions` 生成一对 native 函数。它用一个 do-while 循环建两个函数（注释说某些编译器对未初始化的 context 报错，所以没用 for），再分别把它们的 `u.native` 指向 `njs_promise_resolve_function` 与 `njs_promise_reject_function`。两个函数共享同一个 `resolved_ref` 指针——**先到先得**：哪个先被调用就把 `*resolved_ref` 置 1，另一个再调用就直接返回 undefined，从而保证 Promise 只结算一次。

[njs_promise.c:573-661](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L573-L661) — `njs_promise_resolve_function` 是 resolve 的实现，逻辑分三段：

1. 若已结算（`*resolved_ref`）直接返回；
2. 若 `resolution === 这个 promise 自己`，构造一个 TypeError 并 reject（防自引用死循环）；
3. 若 resolution 是对象且 `.then` 是函数（即 thenable），则**不立即结算**，而是入队一个 `njs_promise_resolve_thenable_job`（[njs_promise.c:1146-1179](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L1146-L1179)）异步地调用 thenable 的 then——这就是「thenable 适配」；
4. 否则走 `fulfill:` 标签直接 `njs_promise_fulfill`。

**fulfill / reject：写状态并触发回调。**

[njs_promise.c:479-504](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L479-L504) — `njs_promise_fulfill`：把 `data->state` 改成 `NJS_PROMISE_FULFILL`、存 `result`，再把 fulfill_queue 整条「搬」到局部变量 `queue`（清空原队列，防止二次触发），最后调用 `njs_promise_trigger_reactions`。

[njs_promise.c:507-541](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L507-L541) — `njs_promise_reject`：与 fulfill 对称，但多一步——若 `!data->is_handled` 且宿主注册了 rejection tracker，就调用它（参数 `0` 表示「产生了未处理拒绝」）。这就是 unhandled rejection 报警的源头。

[njs_promise.c:442-476](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L442-L476) — `njs_promise_trigger_reactions`：遍历队列中的每个 reaction，为每个 reaction 新建一个 native 函数（`u.native = njs_promise_reaction_job`），再调用 `njs_vm_enqueue_job` 把「(reaction, value)」作为参数入队。**这一步把「同步触发」翻译成了「异步作业」**。

**then 的注册：perform_then。**

[njs_promise.c:825-916](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L825-L916) — `njs_promise_perform_then` 是 `then` 的真正实现（也是 await 复用的核心）。它建两个 `njs_promise_reaction_t`（fulfilled_reaction / rejected_reaction），然后分两种情况：

- 若 Promise 还 `pending`：把两个 reaction 分别挂到 `fulfill_queue`/`reject_queue` 尾部，等结算时被 trigger_reactions 取出；
- 若已结算：直接为一个 reaction 入队 `njs_promise_reaction_job`（即「已结算的 Promise 调 then，回调仍异步」）。

末尾 `data->is_handled = 1` 表示「这个 promise 已经被处理过了」，会影响后续的 rejection tracker 行为。

**reaction job：真正跑回调。**

[njs_promise.c:1087-1143](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L1087-L1143) — `njs_promise_reaction_job` 是被作业队列调度的那个 native 函数：取出 reaction 与 argument，若 handler 是 undefined 则原样透传（rejected 且无 catch 时把错误继续往下传），否则调用 handler；最后根据是否出错，调用派生 Promise 的 `capability->resolve` 或 `capability->reject`。**这里就是 `then` 链式调用能让结果向下传递的地方**。

#### 4.1.4 代码实践

**实践目标**：用反汇编和单步观察，看清 `Promise` 构造时 executor 同步执行、而 `then` 回调异步执行。

**操作步骤**：

1. 构建 CLI（参考 u1-l3）：`./configure && make njs`。
2. 运行一段构造脚本，观察 executor 是**同步**跑的：
   ```bash
   ./build/njs -c 'var p = new Promise((res)=>{ console.log("executor"); res(42); }); console.log("after"); p.then(v=>console.log("then", v));'
   ```
3. 对照源码 [njs_promise.c:202-236](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L202-L236) 思考：为什么 `executor` 先打印、`after` 其次、`then 42` 最后？

**需要观察的现象**：

- `executor` 与 `after` 在脚本「同步阶段」就打印了——说明 executor 是在 `njs_promise_constructor_call` 里**同步**调用的；
- `then 42` 在所有同步代码之后才打印——说明 `res(42)` 时 `then` 还没注册（reaction 队列空），等 `then` 被调用时 Promise 已结算，走 `perform_then` 的「已结算」分支，reaction 被排成作业，要等作业队列被排空才执行。

**预期结果**：输出顺序固定为 `executor` → `after` → `then 42`。这与 V8/SpiderMonkey 一致，验证了 njs 的 Promise 同样遵守「executor 同步、handler 异步」的规范语义。

> 本实践假设你能成功构建并运行 CLI；若运行结果与预期不符，请标注「待本地验证」并核对 `build/njs` 是否启用了内置 njs 引擎（默认即 njs 引擎，本讲只讨论 njs 引擎的 Promise 实现）。

#### 4.1.5 小练习与答案

**练习 1**：下列代码输出什么？为什么？

```js
let p = new Promise((res, rej) => { res(1); rej(2); });
p.then(v => console.log("ok", v), e => console.log("err", e));
```

**参考答案**：只输出 `ok 1`。因为 `res(1)` 先调用把 `*resolved_ref` 置 1，随后 `rej(2)` 在 [njs_promise.c:725-728](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L725-L728)（reject_function 开头的 `resolved_ref` 判断）里被直接忽略，Promise 状态锁定为 fulfilled。

**练习 2**：`then` 注册的回调是直接调用，还是被入队？依据是哪个函数？

**参考答案**：是被入队。无论 Promise 是否已结算，`njs_promise_perform_then`（[njs_promise.c:825-916](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L825-L916)）最终都经过 `njs_vm_enqueue_job` 把 `njs_promise_reaction_job` 排进作业队列，从不直接同步调用 handler。

### 4.2 jobs 作业队列

#### 4.2.1 概念说明

上一节反复出现 `njs_vm_enqueue_job`——它把「稍后要执行的一段逻辑」排进队列。这个队列就是 `vm->jobs`，它是 njs 实现**所有异步行为**的中枢。

为什么需要它？因为 njs 是单线程同步执行的 VM：当 `njs_vm_start` 跑全局代码时，它必须**一口气跑完同步部分再返回**。如果允许 `then` 回调在注册时立刻执行，就会破坏「微任务在当前同步代码之后才跑」的语义，还会造成不可控的递归。所以 njs 的策略是：

- 任何「本该异步发生」的回调（Promise reaction、thenable 适配、async 续体恢复），都先打包成一个 `njs_event_t`，挂到 `vm->jobs` 队尾；
- 同步代码跑完、控制权交还宿主后，**宿主**（CLI 或 NGINX）负责循环调用 `njs_vm_execute_pending_job` 把队列排空。

这跟浏览器/Node 的 microtask checkpoint 是一回事，只不过 njs 把「排空」的责任显式交给了嵌入者。

#### 4.2.2 核心流程

作业队列的生命周期：

```
VM 创建
  └─ njs_queue_init(&vm->jobs)         （njs_vm_create 里）

某段代码产生异步回调
  └─ njs_vm_enqueue_job(function, args, nargs)
        ├─ njs_mp_zalloc 一个 njs_event_t
        ├─ 拷贝 args
        └─ njs_queue_insert_tail(&vm->jobs, ...)

宿主排空队列
  └─ while (njs_vm_pending(vm))
        njs_vm_execute_pending_job(vm)
           ├─ 取队首 njs_event_t
           ├─ njs_queue_remove
           └─ njs_vm_call(ev->function, ev->args, ev->nargs)
                 （执行可能又会入队新的 job → 队列继续增长）

VM 销毁前
  └─ （CLI 会在 destroy 前再排空一次，见 njs_shell.c:1384-1389）
```

返回码约定值得记住：`njs_vm_execute_pending_job` 返回 `1` 表示「成功执行了一个 job，可能还有更多」、`NJS_OK(0)` 表示「队列已空」、`NJS_ERROR` 表示「执行 job 时抛了异常」。

#### 4.2.3 源码精读

**队列字段在 VM 里的位置。**

[njs_vm.h:141-142](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L141-L142) — `njs_vm_s` 里 `event_id` 紧挨着 `njs_queue_t jobs`。这就是你要找的「jobs 队列字段」。

[njs_vm.c:507](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L507) — `njs_vm_create` 里 `njs_queue_init(&vm->jobs)` 初始化空队列。

**job 的载体。**

[njs_event.h:11-17](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_event.h#L11-L17) — `njs_event_t` 极简：一个 `function` 指针（要调用的 native 函数）、一组 `args`/`nargs`（参数副本）、一个 `link`（链表节点）。一个 job = 一个待调用的 native 函数及其参数。

**入队。**

[njs_vm.c:662-691](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L662-L691) — `njs_vm_enqueue_job`：分配 `njs_event_t`、若有参数则另开一块拷贝（`memcpy`，因为调用者的栈数组在调用返回后会失效）、最后 `njs_queue_insert_tail`。注意它**不执行** function，只排队。

**判空与出队执行。**

[njs_vm.c:655-659](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L655-L659) — `njs_vm_pending` 就是 `!njs_queue_is_empty(&vm->jobs)`，供宿主做循环条件。

[njs_vm.c:705-731](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L705-L731) — `njs_vm_execute_pending_job`：取队首、`njs_queue_remove` 摘下、用 `njs_vm_call` 执行。返回 `1` 表示「跑了一个，可能还有」（因为 `njs_vm_call` 内部可能又入队新 job），返回 `NJS_OK` 表示队列空了，返回 `NJS_ERROR` 表示 job 抛异常。

**CLI 如何排空队列。**

[njs_shell.c:3453-3467](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L3453-L3467) — `njs_process_script` 在 `engine->eval`（即 `njs_vm_start`）返回后，进入一个双层 `for(;;)` 循环反复调用 `engine->execute_pending_job`，直到返回 `<= NJS_OK`。**正是这段循环让 `then` 回调最终得以执行**。如果你写一个嵌入式 njs 却忘了这段循环，`Promise.resolve(1).then(...)` 的回调永远不会跑。

#### 4.2.4 代码实践

**实践目标**：亲手验证「没有 `njs_vm_execute_pending_job`，`then` 回调就不执行」这一论断。

**操作步骤**：

1. 构建 CLI；
2. 运行题目给的命令：
   ```bash
   ./build/njs -c 'Promise.resolve(1).then(v=>console.log(v))'
   ```
3. 在 `src/njs_vm.h` 第 142 行确认 `jobs` 字段存在（[njs_vm.h:141-142](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L141-L142)）；
4. 阅读 [njs_shell.c:3453-3467](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L3453-L3467) 的排空循环。

**需要观察的现象**：

- 命令应当打印 `1`。如果按规范语义，`console.log(v)` 是在 `njs_vm_start` 返回**之后**、由 CLI 的 job 排空循环触发的。

**预期结果与「为什么需要 execute_pending_job」**：

`Promise.resolve(1)` 同步地造出一个已 fulfilled 的 promise；`.then(v=>...)` 调用 `njs_promise_perform_then`，因为已结算，走的是「直接入队 reaction_job」分支（[njs_promise.c:876-904](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L876-L904)），回调被 `njs_vm_enqueue_job` 排进 `vm->jobs`，**但尚未执行**。`njs_vm_start` 只跑同步全局代码，跑完就返回。此时若没有宿主调用 `njs_vm_execute_pending_job`，队列里的 job 永远不会被取出，`console.log` 自然不打印。CLI 的 `njs_process_script` 在 eval 之后那个排空循环（[njs_shell.c:3453-3467](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L3453-L3467)）正是补上这一步，于是看到 `1`。

> 进阶观察（可选）：在 njs_shell.c 的排空循环里临时加一行 `printf` 打印每次取出的 job 序号，重编译后会看到每次 `execute_pending_job` 各跑一个 job——这能直观感受「一个 job 可能再入队新 job」的链式效应。本步骤会改动源码，仅用于学习，**实践完请还原**。

#### 4.2.5 小练习与答案

**练习 1**：如果某个 job 在执行时又 `enqueue` 了 3 个新 job，`njs_vm_execute_pending_job` 本次调用返回什么？队列最终会剩几个 job？

**参考答案**：本次调用返回 `1`（成功跑了一个）。执行期间新入队的 3 个 job 会追加到队尾，所以队列最终还会剩 3 个待跑（除非这 3 个里又产生新的）。宿主只要继续循环直到 `njs_vm_pending` 为假即可全部排空。

**练习 2**：为什么 `njs_vm_enqueue_job` 要把 `args` 用 `memcpy` 拷一份，而不是直接保存调用者传来的指针？

**参考答案**：因为调用者（如 [njs_promise.c:466-467](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L466-L467) 的 `njs_promise_trigger_reactions`）传的是栈上的局部数组 `arguments[2]`，函数一返回栈帧就失效。job 要等到未来的 `execute_pending_job` 才被消费，必须把参数拷到内存池里独立持有。

### 4.3 async/await 展开

#### 4.3.1 概念说明

`async/await` 是 Promise 的语法糖，但它的实现比 `then` 链更微妙，因为它要在**函数执行中途暂停**。

考虑：

```js
async function f() {
  console.log("a");
  const v = await g();   // g() 返回 Promise
  console.log("b", v);   // 这一行要等 Promise 结算后才跑
  return v + 1;
}
```

`await` 之后的代码（`console.log("b", v)`）叫做**续体（continuation）**。难点是：njs 的解释器是用 C 函数递归驱动的（u4-l3），一旦 C 函数返回，局部状态（当前 PC、局部变量槽）就没了。要在「挂起」后还能「恢复」，就必须把续体需要的状态**显式保存**下来。

njs 的做法是：把 `await` 编译成一条 `NJS_VMCODE_AWAIT` 字节码指令。执行到这条指令时，VM 把当前帧的快照（PC、局部变量区、闭包、结果槽位）打包进一个 `njs_async_ctx_t`，向被 await 的 Promise 注册一对 `onFulfilled/onRejected`（其实就是调用 `njs_promise_perform_then`），然后**返回 `NJS_AGAIN`**。`NJS_AGAIN` 一路向上传播，让整条 C 调用栈 unwind（退栈）回到宿主——这就是「挂起」。

等 Promise 结算、作业队列运行 `njs_await_fulfilled` 时，它把保存的快照**恢复**回 `vm->levels`、`vm->top_frame`，然后再次进入 `njs_vmcode_interpreter`，从当初保存的 PC 继续——这就是「恢复」。async 函数的返回值，则是把最终结果 resolve 进一个「函数级 Promise」（capability）里返回给调用者。

所以一句话总结：**`await` = 保存上下文 + 注册续体到 Promise + 返回 `NJS_AGAIN` 挂起；续体 = 在 job 里恢复上下文 + 从断点 PC 继续执行**。

#### 4.3.2 核心流程

async 函数从调用到结束的完整状态流转：

```
调用 async 函数 f()
  └─ njs_function_frame_invoke 检测到 NJS_OBJ_TYPE_ASYNC_FUNCTION
        └─ njs_async_function_frame_invoke()
              ├─ 造一个 Promise capability（函数级 Promise）
              ├─ njs_function_lambda_call()  进入 f 的字节码
              │     │
              │     └─ 执行到 NJS_VMCODE_AWAIT
              │           └─ njs_vmcode_await()
              │                 ├─ njs_promise_resolve 把 await 的值包成 Promise
              │                 ├─ 分配 njs_async_ctx_t，njs_function_frame_save 保存帧
              │                 ├─ 记 ctx->pc = await 之后下一条指令
              │                 ├─ 建 on_fulfilled=njs_await_fulfilled / on_rejected=njs_await_rejected
              │                 ├─ njs_promise_perform_then(promise, on_fulfilled, on_rejected)
              │                 └─ return NJS_AGAIN   ← 挂起，一路退栈
              │
              ├─ lambda_call 返回 NJS_AGAIN → 视作 NJS_OK，函数级 Promise 仍 pending
              └─ 返回 capability->promise 给调用者

（同步代码结束，宿主排空 jobs）

Promise 结算 → 作业队列调度 njs_await_fulfilled
  └─ 恢复 ctx->await 帧 → 切回 vm->levels/top_frame/active_frame
  ├─ 把 awaited 值写入 ctx->index 槽位（或 throw 若 rejected）
  └─ njs_vmcode_interpreter(vm, ctx->pc, ...)  从断点继续
        ├─ 又遇到 await → 再次 NJS_AGAIN（可多次挂起）
        ├─ 函数正常 return → resolve 函数级 Promise，释放 ctx
        └─ 抛异常 → reject 函数级 Promise，释放 ctx
```

一个 async 函数可以**多次挂起**（多个 `await`），每次都走「保存—AGAIN—恢复」这一圈。

#### 4.3.3 源码精读

**续体上下文。**

[njs_async.h:11-17](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_async.h#L11-L17) — `njs_async_ctx_t` 是挂起时保存的全部状态：`capability`（函数级 Promise，最终用它返回结果）、`await`（保存的帧快照，类型是 `njs_frame_t *`）、`index`（await 结果要写入的槽位）、`pc`（恢复时的下一条指令地址）、`throw_flag`（恢复时是正常值还是抛异常）。

**指令格式。**

[njs_vmcode.h:403-406](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L403-L406) — `njs_vmcode_await_t` 只有操作码和一个 `retval`（指明 await 的值存在哪个槽位，恢复时也写回这里）。`NJS_VMCODE_AWAIT` 操作码枚举见 [njs_vmcode.h:51](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L51)。

**解释器里的 AWAIT 分发。**

[njs_vmcode.c:1659-1672](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L1659-L1672) — 解释器遇到 `NJS_VMCODE_AWAIT` 时调用 `njs_vmcode_await`，**然后用 `return ret` 直接退出本层解释器**（不 `BREAK` 继续下一条）。因为 await 一定返回 `NJS_AGAIN`，本帧到此挂起。

**await 指令的实现：保存 + 注册 + 挂起。**

[njs_vmcode.c:2681-2764](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2681-L2764) — `njs_vmcode_await` 是核心，逐段读：

1. 取出 await 的值（`await->retval` 槽位），用 `njs_promise_resolve` 把它统一包成 Promise `val`——这样 `await 42`（非 Promise）和 `await somePromise` 走同一条路；
2. 若 `ctx == NULL`（第一次 await）：分配 `njs_async_ctx_t`，用 `njs_function_frame_save` 把当前活动帧**整体快照**存到 `ctx->await`，并记下 `ctx->capability = pcap`（async 函数的函数级 Promise）；
3. 记下恢复点：`ctx->pc = await 指令之后下一条指令`、`ctx->index = await->retval`（结果写回哪个槽）、`ctx->throw_flag = 0`；
4. 建 `on_fulfilled = njs_await_fulfilled`、`on_rejected = njs_await_rejected` 两个 native 函数，context 都指向 `ctx`；
5. 调用 `njs_promise_perform_then(val, on_fulfilled, on_rejected, NULL)` 把续体注册到 Promise（复用 4.1 的 then 机制！）；
6. `return NJS_AGAIN`——挂起。

注意第 5 步：**await 完全复用了 Promise 的 then 机制**。续体 `njs_await_fulfilled` 就是一个特殊的 `onFulfilled` handler，它会被 reaction_job 调用，进而被作业队列调度。

**续体：恢复并继续。**

[njs_async.c:55-125](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_async.c#L55-L125) — `njs_await_fulfilled` 是「恢复」侧的实现，是本模块最精巧的函数：

1. 取出 `ctx = vm->top_frame->function->context`（reaction_job 调用它时 context 已设为 ctx）；
2. 把保存的帧 `ctx->await` 接回调用链：`async->previous = vm->top_frame`；
3. **切换运行期存储**：保存当前 `vm->levels[LOCAL/CLOSURE]`、`top_frame`、`active_frame`，换上保存帧的（[njs_async.c:73-82](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_async.c#L73-L82)）——这一步等价于 u4-l3 里函数调用时的切帧，只不过这里是「跨时间」地切回旧帧；
4. 根据 `exception` 参数：若为 0（正常），把 awaited 值写入 `ctx->index` 槽位；若为 1（rejected），置 `throw_flag` 并 `njs_vm_throw`——这样续体从 PC 恢复时会「正好」处于异常状态，被外层 try/catch 接住；
5. **关键调用**：`njs_vmcode_interpreter(vm, ctx->pc, &result, ctx->capability, ctx)` —— 从断点 PC 重新进入解释器继续执行（[njs_async.c:92](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_async.c#L92)）；
6. 恢复刚才保存的 levels/top_frame/active_frame；
7. 按返回值分支：`NJS_OK`（函数跑完）→ resolve 函数级 Promise 并释放 ctx；`NJS_AGAIN`（又遇到 await）→ 保持挂起；`NJS_ERROR`（抛异常）→ reject 函数级 Promise 并释放 ctx。

[njs_async.c:128-133](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_async.c#L128-L133) — `njs_await_rejected` 直接转调 `njs_await_fulfilled` 并传 `exception=1`，复用同一段恢复逻辑。

**async 函数的包装调用。**

[njs_async.c:14-52](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_async.c#L14-L52) — `njs_async_function_frame_invoke` 是 async 函数的入口包装：先用 `njs_promise_new_capability` 造一个函数级 Promise，调 `njs_function_lambda_call` 进入函数体；按返回码分支——`NJS_OK`（同步返回，即函数体里没遇到真正挂起的 await，比如 `async function f(){ return 1 }`）→ resolve；`NJS_AGAIN`（遇到了 await 已挂起）→ 视作 `NJS_OK`，Promise 保持 pending 等续体来结算；`NJS_ERROR` → reject。最后总是返回 `capability->promise`——**所以 async 函数永远返回一个 Promise**。

[njs_function.c:660-674](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_function.c#L660-L674) — 普通函数调用 `njs_function_frame_invoke` 在执行 lambda 前先判断函数对象类型，若是 `NJS_OBJ_TYPE_ASYNC_FUNCTION` 就改走 `njs_async_function_frame_invoke`，这就是「async 函数与普通函数走不同执行路径」的分流点。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 `await` 的完整「挂起—恢复」，看清续体是在作业队列里恢复的。

**操作步骤**：

1. 写一段含单个 await 的脚本到 `/tmp/async_demo.js`（示例代码，非项目原有）：
   ```js
   async function f() {
       console.log("before");
       const v = await Promise.resolve(42);
       console.log("after", v);
       return v + 1;
   }

   f().then(r => console.log("done", r));
   console.log("sync end");
   ```
2. 运行：`./build/njs /tmp/async_demo.js`。
3. 对照源码梳理执行时序：`before` → `sync end` → `after 42` → `done 43`。

**需要观察的现象**：

- `before` 与 `sync end` 在同步阶段打印；
- `after 42` 在作业队列运行 `njs_await_fulfilled`（恢复 f 的帧）后才打印；
- `done 43` 是 f 的函数级 Promise 被	resolve 后，外层 `.then` 的 reaction job 跑出来的，更晚一轮。

**预期结果**：输出顺序为 `before` → `sync end` → `after 42` → `done 43`。

**源码阅读型跟踪**（无需运行）：在 [njs_vmcode.c:2681-2764](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2681-L2764) 与 [njs_async.c:55-125](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_async.c#L55-L125) 之间建立对应关系——`njs_vmcode_await` 里保存的 `ctx->pc` 与 `ctx->index`，分别被 `njs_await_fulfilled` 在 [njs_async.c:89-92](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_async.c#L89-L92) 用作「写值槽位」与「重入解释器的 PC」。看懂这一对保存/读取，就理解了 await 的本质。

> 本脚本运行结果取决于 CLI 是否为内置 njs 引擎（默认是）。若用 `-n QuickJS` 运行，async/await 由 QuickJS 自己的实现驱动，与本讲讲述的 `src/njs_async.c` 路径不同——这一点本身就是 u6「双引擎」的体现。运行结果如有出入请标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`await` 之后的代码（续体）是在「调用 async 函数的同一层 C 栈」里执行的，还是在作业队列驱动的、全新的调用栈里执行的？依据是什么？

**参考答案**：在全新的调用栈里。因为 `njs_vmcode_await` 返回 `NJS_AGAIN` 后，原 C 调用栈已经 unwind 回宿主（[njs_vmcode.c:2763](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2763)）。续体 `njs_await_fulfilled` 是后来由 `njs_vm_execute_pending_job` → `njs_vm_call` 重新发起的一次全新 C 调用（[njs_vm.c:725](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L725)）。它靠 `njs_async_ctx_t` 恢复逻辑帧，而非复用原 C 栈帧。

**练习 2**：一个 async 函数里有 3 个串行 `await`，整个函数会被挂起几次、恢复几次？函数级 Promise 在这期间状态如何？

**参考答案**：挂起 3 次、恢复 3 次（每次 await 都走一遍「保存—AGAIN—job 恢复」）。函数级 Promise 在第一次挂起后进入 pending 并一直保持 pending，直到最后一次续体里函数 `return`（或抛异常）才被 resolve（或 reject）。中间任何一次 await 期间，调用者拿到的都是这个 pending Promise。

**练习 3**：`async function f(){ try { await throwP(); } catch(e){ return "caught" } }` 中，`throwP()` reject 后，catch 为什么能接住？跟踪异常是怎么「扔进」续体的。

**参考答案**：`throwP()` reject 触发 `on_rejected = njs_await_rejected`，它转调 `njs_await_fulfilled(..., exception=1)`（[njs_async.c:128-133](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_async.c#L128-L133)）。在 [njs_async.c:84-86](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_async.c#L84-L86) 里置 `throw_flag` 并 `njs_vm_throw`，然后从 `ctx->pc` 重入解释器——此时 VM 状态恰好像「刚刚抛了异常」，于是被 await 外层编译期生成的 try/catch 字节码（u4-l4）接住，走进 catch 块返回 `"caught"`，最终 resolve 函数级 Promise。

## 5. 综合实践

把三个模块串起来：写一段同时用到「Promise 状态机 + jobs 队列 + async/await + 未处理拒绝」的脚本，并对照源码解释每一步落在哪个函数。

**任务**：创建 `/tmp/integrated_demo.js`（示例代码）：

```js
async function load(id) {
    // 制造一个 0.5 概率 reject 的 Promise，模拟可能失败的异步操作
    const ok = (id % 2) === 0;
    return new Promise((res, rej) => {
        if (ok) res("data-" + id);
        else rej(new Error("fail-" + id));
    });
}

async function main() {
    for (let id = 1; id <= 3; id++) {
        try {
            const v = await load(id);
            console.log("ok", v);
        } catch (e) {
            console.log("caught", e.message);
        }
    }
    return "main-done";
}

main().then(r => console.log(r));

// 一个无人 catch 的拒绝，用于触发 rejection tracker
Promise.reject("orphan");
```

运行：`./build/njs /tmp/integrated_demo.js`。

**完成后，用源码为下列每一步找到对应的函数与行号**：

1. `new Promise(executor)` → `njs_promise_constructor_call`（[njs_promise.c:202-236](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L202-L236)）；
2. executor 里 `res`/`rej` → `njs_promise_resolve_function` / `njs_promise_reject_function`（[njs_promise.c:573-661](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L573-L661) / [njs_promise.c:714-741](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L714-L741)）；
3. `await load(id)` → `njs_vmcode_await`（[njs_vmcode.c:2681-2764](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L2681-L2764)）挂起、`njs_await_fulfilled`（[njs_async.c:55-125](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_async.c#L55-L125)）恢复；
4. catch 接住 reject → `njs_await_rejected` 转 `njs_await_fulfilled(..., exception=1)`（[njs_async.c:128-133](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_async.c#L128-L133)）；
5. `main().then` 链 → `njs_promise_reaction_job`（[njs_promise.c:1087-1143](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L1087-L1143)）；
6. `Promise.reject("orphan")` 无人 catch → `njs_promise_reject` 里 `!is_handled` 分支调用 `vm->rejection_tracker`（[njs_promise.c:519-525](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L519-L525)）；
7. 所有上述异步步骤的驱动器 → CLI 的排空循环（[njs_shell.c:3453-3467](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L3453-L3467)）反复调用 `njs_vm_execute_pending_job`（[njs_vm.c:705-731](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L705-L731)）。

**预期输出**（id=1 reject 被吞、id=2 resolve、id=3 reject 被吞）：

```
caught fail-1
ok data-2
caught fail-3
main-done
```

`Promise.reject("orphan")` 不产生 console 输出（CLI 默认不打印未处理拒绝），但你可以通过阅读 [njs_promise.c:519-525](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_promise.c#L519-L525) 确认它确实触发了 rejection tracker 钩子——在 NGINX 集成场景里，正是这个钩子让 NGINX 把 unhandled rejection 写进 error.log。运行结果如有出入请标注「待本地验证」。

## 6. 本讲小结

- **Promise 是一个三态状态机**：`njs_promise_data_s` 用 `state`/`result`/两条 reaction 队列/`is_handled` 表示；状态迁移单向且一次性，靠 resolve/reject 函数共享 `resolved_ref` 实现「先到先得」。
- **结算 = 触发 reaction = 入队 job**：`njs_promise_fulfill`/`njs_promise_reject` 不直接调用回调，而是用 `njs_promise_trigger_reactions` 把每个回调包成 `njs_promise_reaction_job` 经 `njs_vm_enqueue_job` 排进 `vm->jobs`——这正是「then 回调异步」的根源。
- **`vm->jobs` 是所有异步行为的中枢**：它是一个 `njs_queue_t`，元素是 `njs_event_t`（一个待调用的 native 函数 + 参数副本）。`njs_vm_start` 只跑同步代码，job 的真正执行由宿主循环调用 `njs_vm_execute_pending_job` 完成。
- **await = 保存上下文 + 注册续体 + 返回 NJS_AGAIN**：`NJS_VMCODE_AWAIT` 指令把帧快照存进 `njs_async_ctx_t`，复用 `njs_promise_perform_then` 把 `njs_await_fulfilled`/`njs_await_rejected` 注册为 onFulfilled/onRejected，然后挂起退栈。
- **续体恢复 = 在 job 里还原帧 + 从断点 PC 重入解释器**：`njs_await_fulfilled` 切回保存的 `vm->levels`/`top_frame`，把 awaited 值写入槽位（rejected 则 throw），再用 `njs_vmcode_interpreter(vm, ctx->pc, ...)` 继续；async 函数因此可以多次挂起、多次恢复。
- **rejection tracker 由 reject 与 perform_then 协同驱动**：reject 时若 `!is_handled` 通知「产生未处理拒绝」，perform_then 给已 rejected 的 Promise attach handler 时通知「已被处理」，宿主据此报告 unhandled rejection（CLI 默认不打印，NGINX 写 error.log）。

## 7. 下一步学习建议

本讲讲完了 njs **内置引擎**的异步机制。接下来有三个方向：

1. **进入双引擎世界（u6 单元）**：本讲所有源码（`njs_promise.c`、`njs_async.c`、`njs_vm.c` 的 jobs 队列）都是**内置 njs 引擎**的实现。QuickJS 引擎有自己的 Promise/job 机制（由 `JSRuntime` 的 job 队列驱动，见 njs_shell.c 里 `njs_engine_qjs_execute_pending_job` 与 `JS_IsJobPending`）。学完 u6-l1 的 `qjs.c` 包装层后，对比两套 job 循环的差异，能加深对「异步是引擎职责」的理解。

2. **看 Promise 在 NGINX 里怎么真正「异步」起来（u9 单元）**：在 CLI 里，job 队列在脚本结束后立刻被排空；但在 NGINX 集成里，`r.subrequest()`、`ngx.fetch()` 返回的 Promise 是与 NGINX 事件循环绑定的——job 的恢复发生在 NGINX 的事件回调里。学 u9-l1 的 `ngx.fetch` 时，重点看它是如何把网络 IO 完成事件「翻译」成 Promise 结算的。

3. **回看编译前端（u3 单元）**：`async function` 和 `await` 是怎么从语法变成 `NJS_VMCODE_AWAIT` 指令的？可以在 `src/njs_generator.c` 里搜索 await 相关的生成逻辑，把 u3-l4（生成器）与本讲串成一个闭环：源码 → AST → 字节码 `AWAIT` 指令 → 解释器挂起 → job 恢复。

继续阅读建议：先读 `src/njs_promise.c` 顶部的 `njs_promise_alloc`、`njs_promise_fulfill`、`njs_promise_reject` 三个函数建立状态机直觉，再读 `njs_promise_perform_then` 看回调注册，最后对照 `src/njs_async.c` 的 `njs_await_fulfilled` 把 await 的恢复链路走通——这条阅读顺序最省力。
