# OpKernel 与 Compute 接口

## 1. 本讲目标

学完本讲，你应该能够：

- 用一句话说清 **OpKernel** 与 u4-l1 讲过的 **Op / OpDef** 的关系——前者是「计算说明书」，后者是「真正干活的实现」，并解释为什么一个 Op 会对应**多个** OpKernel。
- 读懂 `OpKernel` 这个抽象基类的核心契约：子类**必须**实现哪个方法（`Compute`），它接收的 `OpKernelContext*` 又提供了什么能力。
- 区分**同步**与**异步**两种 kernel：什么时候该继承 `OpKernel`、什么时候必须继承 `AsyncOpKernel`，以及 `OP_REQUIRES` 为什么在异步 kernel 里会被强行禁止。
- 看懂 `OpKernelContext` 如何成为 kernel 与运行时之间的**唯一通道**：读输入 `input()`、分配输出 `allocate_output()`、回填结果 `set_output()`、上报错误 `SetStatus()`。
- 解释 `REGISTER_KERNEL_BUILDER(Name(...).Device(...).TypeConstraint<T>("T"), OpImpl)` 这条链式注册语句如何把「某个 op + 某台设备 + 某种类型」绑定到一个具体的 C++ 类，以及运行时如何按 `(op 名, 设备, 属性)` 三元组查到正确的 kernel。

本讲聚焦一个最小模块：`core.framework.op_kernel`（对应 `op_kernel.h` / `op_kernel.cc`）。为讲清「一对多」关系，会配套引用 `core/kernels/cwise_ops_common.h`（一个真实的 OpKernel 子类模板）与 `core/kernels/cwise_op_add_1.cc`（`"Add"` 这个 op 注册了多少个 kernel）。

## 2. 前置知识

进入源码前，先建立三个直觉。

**第一，Op 是「说明书」，OpKernel 才是「干活的工人」。** 回顾 u4-l1：`REGISTER_OP("Add")` 只是把 `"Add"` 这个名字连同它的 `OpDef`（输入几个、什么属性、形状怎么推导）登记进了全局 `OpRegistry`。但 OpDef 里**没有任何一行真正的加法代码**。当运行时真正要执行图里的一个 `"Add"` 节点时，它需要的是一段「给定两个输入张量，怎么算出输出张量」的实现——这段实现就是 **OpKernel**。

打个比方：

- `OpDef`（u4-l1）→ 菜谱上的「菜名 + 配料 + 做法描述」；
- 本讲的 `OpKernel` → 真正下厨的厨师，按菜谱把生料做成菜。

一个菜名（Op）可以有多位厨师（OpKernel）会做：会做素食版的、会做辣版的……对应到 TF，就是「同一个 Op，在 CPU 上用一个 kernel、在 GPU 上用另一个 kernel、对 `float` 用一个 kernel、对 `int32` 用另一个 kernel」。这就是本讲标题里「一对多」的含义。

**第二，kernel 的输入输出不是「返回值」，而是通过一个「上下文对象」搬进搬出。** 很多语言里计算函数长这样 `C = add(A, B)`，输入是参数、输出是返回值。但 OpKernel 的 `Compute` 方法返回 `void`——它既不接收张量参数，也不返回张量。所有的输入张量、输出张量、设备句柄、错误状态，全都塞在一个叫 **`OpKernelContext`** 的对象里，`Compute` 通过它来「取输入、写输出」。这种「总线式」设计是为了让 kernel 实现与运行时的内存分配、设备调度、错误传播彻底解耦，我们会在 4.3 节细讲。

**第三，注册 OpKernel 的手法和注册 Op 几乎一模一样——「静态全局对象 + 全局表」。** u4-l1 讲过 `OpRegistry::Global()` 是一张 `map<名字, OpDef>`，靠 C++ 静态初始化在 `main` 之前自动填充。本讲的 kernel 注册表 `KernelRegistry` 也是同款套路：一张 `multimap<键, 工厂>`，靠 `REGISTER_KERNEL_BUILDER` 宏在启动期自动登记。如果对这套「启动期登记、运行时查找」的模式还不熟，建议先回头看 u4-l1 的 4.4 节，本讲会直接复用这个结论。

> 名词速查：**设备（Device）** 指 CPU / GPU / TPU 等执行单元（u6-l1 会展开）；**Eigen** 是 TF 用来做 CPU/GPU 上张量运算的模板库，kernel 里常见 `ctx->eigen_device<Device>()` 取的就是它；**属性（attr）** 是 op 在建图时绑定的编译期参数，如 `"Add"` 的 `T` 表示元素类型（u4-l1）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `tensorflow/core/framework/op_kernel.h` | `OpKernel` / `AsyncOpKernel` / `OpKernelConstruction` / `OpKernelContext` 四大类的定义，以及 `REGISTER_KERNEL_BUILDER` 宏 | 讲清「计算契约」长什么样 |
| `tensorflow/core/framework/op_kernel.cc` | 上述类的实现、`KernelRegistry` 全局表、`CreateOpKernel` 查找与实例化 | 讲清注册表如何存、运行时如何查到并造出 kernel |
| `tensorflow/core/framework/op_requires.h` | `OP_REQUIRES` / `OP_REQUIRES_OK` 等错误处理宏 | 讲清 kernel 里「校验失败就 return」的标准写法 |
| `tensorflow/core/kernels/cwise_ops_common.h` | `BinaryOp<Device, Functor>` 等真实 OpKernel 子类模板 | 作为「一个 OpKernel 子类长什么样」的实例 |
| `tensorflow/core/kernels/cwise_op_add_1.cc` | 为 `"Add"` 注册 CPU/GPU/DEFAULT 多个 kernel | 作为「一个 Op 为什么有多个 kernel」的实例 |

记住一条主线：**注册（`REGISTER_KERNEL_BUILDER` 宏 → `KernelRegistry` 全局表）→ 查找（`CreateOpKernel` 按 op+设备+属性查到工厂）→ 实例化（工厂 `Create` 造出 OpKernel）→ 执行（运行时调 `Compute(context)`，kernel 经 `context` 取输入、写输出）**。下面四节沿这条线展开。

## 4. 核心概念与源码讲解

### 4.1 Op 与 OpKernel：从「说明书」到「实现」

#### 4.1.1 概念说明

u4-l1 的结论是：`OpDef` 是「说明书」，只描述名字、输入输出和属性，不含任何计算代码。本讲要回答的下一个问题是——**真正执行计算的代码住在哪里？** 答案就是 `OpKernel`。

可以这样建立对应关系：

| 概念 | 所在文件 | 内容 | 时机 |
| --- | --- | --- | --- |
| `OpDef`（u4-l1） | `core/framework/op_def.proto` | 名字、`input_arg`、`output_arg`、`attr`、形状推导函数 | 建图期校验节点是否合法 |
| `OpKernel`（本讲） | `core/kernels/*.cc` 等 | `Compute` 方法——真正读写张量、做运算 | 运行期真正执行节点 |

更关键的一点：**`OpDef` 与 `OpKernel` 是「一对多」的关系。** 一个 Op（比如 `"Add"`）只有一份 OpDef，但可以有许许多多个 OpKernel——按设备（CPU/GPU）、按元素类型（`float`/`int32`/`double`）各有一个。运行时拿到一个具体节点（已知它跑在 CPU 上、`T=float`），就会从这一堆 kernel 里挑出**唯一**匹配的那一个来执行。

为什么会这样设计？因为「加法的数学定义」只有一种（OpDef），但「在 GPU 上对 float 做加法」和「在 CPU 上对 int32 做加法」的最优底层实现完全不同——前者要发起 CUDA 核函数，后者是 CPU 上的 SIMD 循环。把「逻辑定义」与「物理实现」拆成两层，就能在不改 OpDef 的前提下任意扩展新的设备或类型。

#### 4.1.2 核心流程

把 Op 与 OpKernel 的生命周期画在一起：

```
            建图 / 追踪期                         运行期
┌───────────────────────────────┐   ┌────────────────────────────────────┐
│ REGISTER_OP("Add")            │   │ 图里有个节点 NodeDef{op:"Add",      │
│   → OpDef 进 OpRegistry       │   │   device:"/cpu:0", T=float}         │
│                               │   │                                    │
│ REGISTER_KERNEL_BUILDER(      │   │ CreateOpKernel(device=CPU, props): │
│   Name("Add").Device(CPU)     │   │   1. FindKernelRegistration        │
│   .TypeConstraint<float>("T"),│   │      按 (Add, CPU, 属性) 查表       │
│   AddOp<CPUDevice,float>)     │   │   2. 命中工厂                       │
│   → (键, 工厂) 进 KernelRegistry│  │   3. factory->Create(construction) │
└───────────────────────────────┘   │      → 造出 AddOp<CPUDevice,float>│
         启动期自动登记              │   4. executor 调 kernel->Compute(ctx)│
                                   └────────────────────────────────────┘
```

两件登记动作（左边 `REGISTER_OP`、`REGISTER_KERNEL_BUILDER`）都发生在程序启动期；真正造出 kernel 对象（右边第 3 步）发生在运行时第一次需要执行这个节点时。

#### 4.1.3 源码精读

`OpKernel` 是一个抽象基类，定义在 [op_kernel.h:107-L229](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L107-L229)——它**没有**默认构造函数，只能用一个 `OpKernelConstruction*` 来构造：

```cpp
// op_kernel.h:107-111
class OpKernel {
 public:
  // OpKernel won't be instantiated by the scheduler, so you may perform
  // expensive initialization in the descendant's constructor.
  explicit OpKernel(OpKernelConstruction* context);
```

注意那句注释：**构造函数允许做「昂贵」的初始化**。因为一个 OpKernel 对象在一个图的生命周期里通常只构造一次、而 `Compute` 会被调用成千上万次，所以「一次性预处理」（如查表、预编译）应该放构造函数里，而不是每次 `Compute` 都重做。

构造函数的实现见 [op_kernel.cc:136-L162](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L136-L162)，它主要把「输入输出名字到下标的映射」、版本号等元数据从 `OpKernelConstruction` 里抄到自己成员里：

```cpp
// op_kernel.cc:136-152（节选）
OpKernel::OpKernel(OpKernelConstruction* context, bool is_deferred)
    : props_(context->props_),
      ...
      graph_def_version_(context->graph_def_version()),
      is_deferred_(is_deferred),
      // Kernels executing on GPU tie very few resources on the CPU ...
      // we consider them as inexpensive.
      expensive_(context->device_type() != DeviceType(DEVICE_GPU) && ...) {
```

这里有一个有意思的细节 `expensive_`：**GPU 上的 kernel 默认被标记为「不昂贵」**，而 CPU 上的默认「昂贵」。运行时会用 `IsExpensive()`（[op_kernel.h:166](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L166)）来调度——廉价 kernel 会被优先「内联」到调度线程，昂贵的才扔到线程池。这个标记会影响图的并发调度顺序。

#### 4.1.4 代码实践

> **实践目标**：亲手确认「一个 Op 对应一份 OpDef、但多个 OpKernel」。
>
> **操作步骤**：
> 1. 在 `tensorflow/core/ops/math_ops.cc` 中搜索 `"Add"`，确认它只有**一处** `REGISTER_OP(Name("Add") ...)` 声明（这是唯一的 OpDef）。
> 2. 在 `tensorflow/core/kernels/` 下搜索 `Name("Add")`，数一下 `REGISTER_KERNEL_BUILDER(... Name("Add") ...)` 或 `REGISTER6(BinaryOp, CPU, "Add", ...)` 这样的语句共有多少处、分布在多少个文件里。
>
> **需要观察的现象**：OpDef 只有一处；而 kernel 注册语句分散在 `cwise_op_add_1.cc`、`cwise_op_add_2.cc` 等多个文件里，每一处对应一种 (设备, 类型) 组合。
>
> **预期结果**：你会在 [cwise_op_add_1.cc](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/kernels/cwise_op_add_1.cc) 这一个文件里就看到 CPU、GPU、DEVICE_DEFAULT 三类设备、多种类型的注册——这正是「一对多」的直观证据。

#### 4.1.5 小练习与答案

**练习 1**：如果某个 op 在 CPU 上有 kernel、但在 GPU 上没有，运行时把图放到 GPU 上执行会发生什么？

**参考答案**：运行时在 `CreateOpKernel` 里查不到匹配 `(op, GPU, 属性)` 的注册，会返回 `NotFoundError`，典型报错形如 `No registered '...' OpKernel for 'GPU' devices compatible with ...`（见 [op_kernel.cc:1806-L1809](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1806-L1809)）。这正是用户看到「OpKernel not found」类报错的根源。

**练习 2**：为什么 TF 不直接把计算代码写进 OpDef，而要单独搞一个 OpKernel？

**参考答案**：为了让「逻辑定义」与「物理实现」解耦。OpDef 只有一份，描述语义；而不同设备/类型需要完全不同的底层实现。拆成两层后，新增一种设备或类型只需多注册一个 OpKernel，完全不用动 OpDef，也方便做选择性编译（只编入需要的 kernel 以减小二进制体积）。

### 4.2 OpKernel 类与 Compute：计算契约

#### 4.2.1 概念说明

`OpKernel` 最核心的契约只有一个：**子类必须实现 `Compute` 方法**。这是整个 TF 运行时与具体计算代码之间唯一的接口——执行器把一个填好输入的 `OpKernelContext` 交给你，你在里面算完、把输出写回同一个 context，就完事了。

这里有一个初学者最容易踩的坑：**`Compute` 返回 `void`，不通过返回值表达「算完了」或「出错了」。** 算完了是隐含的（函数返回即完成）；出错了则要主动调 `context->SetStatus(...)` 把错误塞进 context，执行器随后会检查并中止。这就是 4.1.2 里说的「总线式」设计——输入、输出、错误状态全走 context 这一条通道。

此外，`Compute` 必须是**线程安全**的：同一张图可能被并发执行多次，同一个 OpKernel 对象的 `Compute` 可能被多个线程同时调用。因此 kernel 里**不能**有可写的成员变量做中间状态，中间量只能放 `Compute` 的栈变量里。

#### 4.2.2 核心流程

一次同步 kernel 执行的时序：

```
执行器 Executor:
  1. 准备 OpKernelContext::Params（op_kernel 指针、设备、输入张量数组...）
  2. 构造 OpKernelContext ctx(&params)        ← ctx 持有输入，outputs_ 预留空位
  3. op_kernel->Compute(&ctx)                 ← 进入用户写的计算代码
       │
       ├─ const Tensor& a = ctx->input(0);    ← 取输入
       ├─ ctx->allocate_output(0, shape, &out);← 分配输出缓冲区
       ├─ out->flat<T>() = a.flat<T>() + ...; ← 真正算（用 Eigen）
       └─ （出错时）ctx->SetStatus(...) / OP_REQUIRES(...)
  4. 检查 ctx.status()，非 OK 则上报错误
  5. 从 ctx.outputs_ 回收结果张量
```

#### 4.2.3 源码精读

纯虚的 `Compute` 定义在 [op_kernel.h:155-L158](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L155-L158)：

```cpp
// op_kernel.h:155-158
// Synchronous compute.
// "context" is guaranteed to be alive until Compute() returns.
virtual void Compute(OpKernelContext* context) = 0;
```

`virtual ... = 0` 表示它是纯虚函数——**任何 OpKernel 子类都必须 override 它，否则该子类仍是抽象类、无法实例化**。这就是本讲实践任务「写出一个 OpKernel 子类必须实现的方法」的直接答案。

头文件里那段长注释（[op_kernel.h:128-L154](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L128-L154)）极其重要，它规定了同步 kernel 的两条铁律：

1. **同步 kernel 绝不能阻塞在「等待另一个 op 执行」的条件变量上**，否则会死锁（执行器线程数有限）。
2. 如果你确实需要等别的 op（比如 `RecvOp` 等待跨设备数据、队列 `DequeueOp` 等待入队），**必须**改用异步 kernel `AsyncOpKernel`，override `ComputeAsync`。

异步版本 `AsyncOpKernel` 见 [op_kernel.h:231-L256](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L231-L256)：

```cpp
// op_kernel.h:250-251
typedef std::function<void()> DoneCallback;
virtual void ComputeAsync(OpKernelContext* context, DoneCallback done) = 0;
```

它的契约和同步版不同：`ComputeAsync` **返回时工作未必完成**，实现必须保证最终**恰好调用一次** `done` 回调来通知执行器「我真的完成了」。注意 [op_kernel.cc:255-L259](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L255-L259) 里基类默认会把 `ComputeAsync` 包装成一个「用 `Notification` 阻塞等待 done」的同步 `Compute`，但这只用于在同步执行器里临时跑异步 kernel，正常不该依赖它。

异步 kernel 的一个硬性约束是：**`OP_REQUIRES` / `OP_REQUIRES_OK` 这两个宏在 `ComputeAsync` 里被禁止使用**。原因看宏定义 [op_requires.h:56-L63](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_requires.h#L56-L63)：

```cpp
// op_requires.h:56-63
#define OP_REQUIRES(CTX, EXP, STATUS)                     \
  do {                                                    \
    if (!TF_PREDICT_TRUE(EXP)) {                          \
      CheckNotInComputeAsync((CTX), "OP_REQUIRES_ASYNC"); \
      (CTX)->CtxFailure(__FILE__, __LINE__, (STATUS));    \
      return;                                             \
    }                                                     \
  } while (0)
```

注意它失败时直接 `return;`——这在同步 `Compute` 里没问题（函数结束即完成），但在异步里直接 return 就**永远不会调用 `done`**，执行器会永久挂起。所以宏里先调 `CheckNotInComputeAsync`：它会在发现当前 kernel 是异步时**直接 crash**（[op_kernel.cc:1939-L1943](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1939-L1943)），用崩溃换死锁。异步 kernel 必须改用带回调参数的 `OP_REQUIRES_ASYNC` / `OP_REQUIRES_OK_ASYNC`（[op_requires.h:90-L107](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_requires.h#L90-L107)），它在 return 前会先调 `CALLBACK()`。

#### 4.2.4 代码实践

> **实践目标**：读懂一个真实的同步 OpKernel 子类，确认它只 override 了 `Compute`。
>
> **操作步骤**：打开 [cwise_ops_common.h:232-L260](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/kernels/cwise_ops_common.h#L232-L260) 的 `ApproximateEqualOp`（这是 TF 里最短小完整的 OpKernel 子类之一）。逐行阅读它的：
> 1. 构造函数：`OpKernelConstruction*` + `OP_REQUIRES_OK(ctx, ctx->GetAttr("tolerance", ...))`；
> 2. `Compute` 方法：`context->input(0/1)` 取输入、`OP_REQUIRES` 校验形状、`context->allocate_output(...)` 分配输出、`context->eigen_device<Device>()` 取设备、调 functor 真正算。
>
> **需要观察的现象**：这个类除了构造函数，**只**多实现了 `Compute` 一个方法——这就是一个 OpKernel 子类的最小实现。
>
> **预期结果**：你能用一句话概括 OpKernel 子类的写法模板：「构造期读属性 → Compute 里取输入、校验、分配输出、调算子」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Compute` 是 `void` 而不是返回 `Status`？

**参考答案**：因为输入、输出、错误都统一走 `OpKernelContext` 这条总线。返回 `void` 让 `Compute` 的签名与「输出几个张量、什么类型」完全解耦，不同 op 的 `Compute` 可以有任意数量的输出，无需为「返回一个 / 两个 / 三个张量」设计不同签名；错误则通过 `ctx->SetStatus()` 上报。

**练习 2**：`AsyncOpKernel::ComputeAsync` 的契约要求「`done` 恰好被调用一次」。如果实现里一次都不调用会怎样？调用两次又会怎样？

**参考答案**：不调用 → 执行器永远等不到完成信号，该步（step）永久挂起，这是典型的死锁/泄漏 bug。调用两次 → 第二次会触发执行器内部的状态机非法转移（通常 DCHECK 失败或重复回收 retval 崩溃）。所以异步 kernel 的难点就在于「任何退出路径（包括错误、取消）都要保证恰好一次 done」，这也是它要支持 `cancellation_manager()` 的原因。

### 4.3 OpKernelContext：输入/输出/分配的统一通道

#### 4.3.1 概念说明

`OpKernelContext`（上下文）是 kernel 与运行时之间的**唯一通道**。可以把它理解成一个「工具箱」：执行器在调你的 `Compute` 前，把所有输入张量、设备句柄、各种运行时服务都装进这个工具箱；你算完后，把输出张量和可能的错误也放回这个工具箱。整个过程 kernel 不直接接触执行器的任何内部结构。

围绕这个上下文，还有两个配角：

- **`OpKernelConstruction`（构造上下文）**：只在 kernel **构造时**可用，主要用来读 op 的属性（`GetAttr`）、校验签名（`MatchSignature`）。它和 `OpKernelContext`（执行时可用）是两个不同阶段的两套 API。
- **`OpKernelContext::Params`**：一个纯数据结构，执行器用它来「装填」context 的初始内容（指向 op_kernel、设备、输入张量数组等）。普通 kernel 作者通常不直接碰 `Params`，但了解它能帮你看清「输入张量是怎么从执行器流进 context 的」。

#### 4.3.2 核心流程

`OpKernelContext` 提供的能力可分成四组：

```
读输入        input(i) / input_list(name,...)        → 只读 const Tensor&
分配输出      allocate_output(i, shape, &out)        → 新建缓冲区并登记为输出
回填输出      set_output(i, tensor)                  → 把已有 Tensor 指定为输出
分配临时区    allocate_temp(type, shape, &tmp)       → 临时缓冲，不作为输出
上报错误      SetStatus(s) / OP_REQUIRES(...)        → 失败时记录并 return
取设备/服务   device() / eigen_device<D>() /         → 拿到执行设备与 Eigen 句柄
              function_library() / resource_manager()
```

输入与输出的连接靠 context 内部一个 `outputs_` 数组（[op_kernel.h:1315](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L1315)）：执行器构造 context 时按输出个数预留空位，kernel 用 `allocate_output` / `set_output` 填上对应下标，`Compute` 返回后执行器再从这个数组回收结果。析构时（[op_kernel.cc:367-L381](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L367-L381)）未被回收的非 ref 输出会被 delete 掉，保证不泄漏。

#### 4.3.3 源码精读

`OpKernelContext` 类定义在 [op_kernel.h:572-L1346](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L572-L1346)，内部嵌套的 `Params` 结构在 [op_kernel.h:582-L708](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L582-L708)。注意 `Params` 里有几项关键内容：`op_kernel`（[op_kernel.h:595](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L595)）、`device`（[op_kernel.h:598](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L598)）、以及输入张量数组 `inputs`（[op_kernel.h:667](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L667)）。

**取输入**：最常用的是按下标取，[op_kernel.cc:454-L460](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L454-L460)：

```cpp
// op_kernel.cc:454-460
const Tensor& OpKernelContext::input(int index) const {
  CHECK_GE(index, 0);
  CHECK_LT(index, num_inputs()) << " name: " << op_kernel().name();
  CHECK(!input_is_ref(index));
  const Tensor& tensor = *params_->inputs[index].tensor;
  return tensor;
}
```

注意它有 `CHECK(!input_is_ref(index))`：**普通 `input()` 只能取不可变（非 ref）输入**；可变的 ref 输入（如老式 Variable）要用 `mutable_input()`。这与 u2-l3 讲过的「ResourceVariable 用资源句柄而非 ref」是一致的演进方向。

**分配输出**：核心实现是带 `AllocatorAttributes` 的重载 [op_kernel.cc:804-L850](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L804-L850)，它内部调 `allocate_tensor` 真正向设备的 `Allocator` 要内存（[op_kernel.cc:780-L802](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L780-L802)），再把分配好的 `Tensor` 挂进 `outputs_`：

```cpp
// op_kernel.cc:843-848（节选）
auto output_tensor = std::make_unique<Tensor>();
absl::Status s = allocate_tensor(type, shape, output_tensor.get(), attr);
if (s.ok()) {
  outputs_[index] = TensorValue(output_tensor.release());
  *output = outputs_[index].tensor;
}
```

这里 `allocate_tensor` 若返回 `ResourceExhaustedError`（OOM）就向上传播（[op_kernel.cc:790-L795](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L790-L795)），这就是你跑模型时看到 `OOM when allocating tensor with shape...` 报错的来源。

**回填输出**：如果你已经有一个算好的 `Tensor`（比如就是某个输入略作变换），可以用 `set_output` 不再重新分配，[op_kernel.cc:1013-L1025](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1013-L1025)。

还有一个性能关键点——**输入转发（input forwarding）**。`forward_input_or_allocate_output(...)`（[op_kernel.h:914-L917](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L914-L917)）会**尽量把某个输入的缓冲区原地复用为输出**，避免一次分配 + 一次拷贝。在真实 kernel `BinaryOp::Compute` 里你能看到它的典型用法 [cwise_ops_common.h:110-L111](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/kernels/cwise_ops_common.h#L110-L111)：

```cpp
// cwise_ops_common.h:109-111
Tensor* out;
OP_REQUIRES_OK(ctx, ctx->forward_input_or_allocate_output(
                        {0, 1}, 0, input_0.shape(), &out));
```

含义是「优先把第 0 或第 1 个输入转发为第 0 个输出；转发不了再分配新的」。这能省掉大量逐元素运算里不必要的内存拷贝。

**构造期上下文**：`OpKernelConstruction` 提供读属性的能力，[op_kernel.h:319-L327](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L319-L327)：

```cpp
// op_kernel.h:322-324
template <class T>
absl::Status GetAttr(absl::string_view attr_name, T* value) const;
```

它的实现极其简单——直接转发给一个读 NodeDef 属性的工具函数（[op_kernel.cc:1613-L1616](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1613-L1616)），因为属性本来就存在节点的 `NodeDef` 里（u3-l1）。所以「构造期读一次属性、存到成员变量」是标准范式——比如 4.2.3 里 `ApproximateEqualOp` 就是把 `tolerance` 读出来存成 `tolerance_`，避免每次 `Compute` 都重读。

#### 4.3.4 代码实践

> **实践目标**：对照源码，确认 kernel 与 context 之间的「四件事」各对应哪个 API。
>
> **操作步骤**：在 `ApproximateEqualOp::Compute`（[cwise_ops_common.h:240-L256](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/kernels/cwise_ops_common.h#L240-L256)）里，分别找出：取输入、校验、分配输出、取设备、真正调用算子这五步各用的是哪个 context 方法。
>
> **需要观察的现象**：你能看到一个清晰的模式——`input()` 读、`allocate_output()` 写、`eigen_device<Device>()` 取设备、最后 functor 做运算，全程不和执行器直接打交道。
>
> **预期结果**：列出一张「步骤 → API」对照表，例如：取输入 = `context->input(0)`；校验 = `OP_REQUIRES(...)`；分配输出 = `context->allocate_output(0, shape, &z_output)`。

#### 4.3.5 小练习与答案

**练习 1**：`allocate_output` 和 `set_output` 都能把一个张量挂到输出位，它们的区别是什么？

**参考答案**：`allocate_output` 是**让 context 帮你向设备 Allocator 申请一块新内存**并登记为输出；`set_output` 是**把一个你已经有的 Tensor**（可能是输入转发来的、或 `allocate_temp` 来的）直接指定为输出。后者可能因分配器属性不匹配而产生一次额外拷贝（见 [op_kernel.h:979-L993](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L979-L993) 的注释），所以优先用 `allocate_output` 或 `forward_input_or_allocate_output`。

**练习 2**：为什么读属性要放在构造函数里（用 `OpKernelConstruction`），而不是每次 `Compute` 用 `OpKernelContext` 读？

**参考答案**：因为属性在建图时就固定了，同一个 kernel 对象的所有 `Compute` 调用看到的属性都一样。构造期读一次、缓存到成员，能避免每次执行都去 `NodeDef` 里查一次字符串属性——这对一个被调用上百万次的 kernel 是可观的性能节省。

### 4.4 一对多：TypeConstraint / Device / 多 Kernel 与注册

#### 4.4.1 概念说明

现在回答本讲的最后一个核心问题：**「同一个 Op 为什么有多个 kernel」「运行时怎么选出正确的那个」**。

答案是：每注册一个 OpKernel，都要声明它**适用于哪种设备、哪些类型**——这通过 `REGISTER_KERNEL_BUILDER` 宏的链式描述完成：

```cpp
REGISTER_KERNEL_BUILDER(
    Name("Sub").Device(DEVICE_CPU).TypeConstraint<float>("T"),
    SubOp<float>);
```

这条语句读作：「为 op `"Sub"`、在 CPU 设备上、当属性 `T=float` 时，注册 `SubOp<float>` 这个实现」。三要素 **(op 名, 设备, 类型约束)** 组合起来就是一个 kernel 的「适用范围」。一个 Op 有多少种 (设备, 类型) 组合，就可以有多少个 kernel——这就是「一对多」的根源。

运行时则反过来：拿到一个具体节点（已知 op 名、目标设备、属性值），去全局 `KernelRegistry` 表里查「哪一条注册能匹配」，挑出优先级最高的那个，调用它的工厂造出对象。

#### 4.4.2 核心流程

注册与查找是一对镜像过程：

```
注册（启动期，每个 REGISTER_KERNEL_BUILDER 一条）：
  KernelDef{Name:"Add", Device:"CPU", TypeConstraint:T==float}
     + 工厂 lambda: [](construction){ return new BinaryOp<CPU,add<float>>; }
  → 用 Key("Add","CPU","") 作为键，连同 KernelDef+工厂，emplace 进
    KernelRegistry::registry （一个 unordered_multimap）

查找（运行期，每个节点一次）：
  CreateOpKernel(device=CPU, props={op:"Add", T=float}):
    1. Key = "Add:CPU:" （label 默认空）
    2. registry.equal_range(Key) → 取出所有候选注册
    3. 对每个候选用 KernelAttrsMatch(def, node_attrs) 比对 TypeConstraint
    4. 命中且优先级最高者 → registration->factory->Create(&construction)
       → 得到 BinaryOp<CPUDevice, functor::add<float>> 对象
```

注意键里有个 `label`（默认空串），它是为 JIT 编译的 kernel 准备的「隐藏标签」（[op_kernel.h:100-L105](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L100-L105)），普通 kernel 用不到，可暂时忽略。

#### 4.4.3 源码精读

**注册宏**。`REGISTER_KERNEL_BUILDER` 最终展开成一段「构造静态对象」的代码，把一个工厂塞进全局表，见 [op_kernel.h:1469-L1502](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L1469-L1502)。展开后的核心是构造一个 `OpKernelRegistrar`，它捕获一个 lambda 作为工厂（[op_kernel.h:1476-L1486](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L1476-L1486)）：

```cpp
// op_kernel.h:1476-1482（宏展开后的等价形式）
OpKernelRegistrar registrar(kernel_def, class_name,
    [](OpKernelConstruction* context) -> OpKernel* {
      return new __VA_ARGS__(context);   // __VA_ARGS__ 即你写的 OpImpl
    });
```

工厂本身是个极简的虚类 `OpKernelFactory`（[op_kernel.h:1566-L1570](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L1566-L1570)），`Create` 只是 `new` 出你的子类（[op_kernel.cc:1421-L1424](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1421-L1424)）。

**全局表**。`KernelRegistry` 是个带锁的 `unordered_multimap`（[op_kernel.cc:1180-L1185](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1180-L1185)），存的是 `KernelRegistration`（KernelDef + 类名 + 工厂，[op_kernel.cc:1167-L1175](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1167-L1175)）。键由 `Key(op, device, label)` 拼成字符串（[op_kernel.cc:1279-L1283](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1279-L1283)）。`InitInternal`（[op_kernel.cc:1397-L1419](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1397-L1419))在静态初始化期把条目 emplace 进去——和 u4-l1 的 `REGISTER_OP` 用的是同一套「启动期登记」手法。唯一多出来的是 `GlobalKernelRegistry()` 在首次使用时顺便给 OpRegistry 注册了一个 `ValidateKernelRegistrations` 校验器（[op_kernel.cc:1334-L1340](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1334-L1340))，用于在 op 注册完成后校验所有 kernel 的 `HostMemory` 参数确实存在于 OpDef 中。

**查找与实例化**。`CreateOpKernel`（[op_kernel.cc:1780-L1839](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1780-L1839)）是运行时的总入口，三步走：

```cpp
// op_kernel.cc:1797-1833（节选）
// 1. 用 (op, device, label) 在表里找候选
s = FindKernelRegistration(device_type, node_def, &registration,
                           &was_attr_mismatch, registry);
// 2. 没找到 → NotFound；属性不匹配 → 追加提示
if (registration == nullptr) {
  s.Update(absl::NotFoundError(absl::StrCat(
      "No registered '", node_def.op(), "' OpKernel for '",
      DeviceTypeString(device_type), "' devices compatible with ...")));
  ...
}
// 3. 构造 OpKernelConstruction，调工厂造对象
OpKernelConstruction context(std::move(device_type), device, allocator, ...);
*kernel = registration->factory->Create(&context);
```

`FindKernelRegistration`（[op_kernel.cc:1447-L1525](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1447-L1525)）的细节值得一看：它先用 `equal_range(key)` 取出该 (op, device) 下的**所有**候选（multimap 允许同键多条目），再逐个用 `KernelAttrsMatch` 比对 `TypeConstraint`；若多条都匹配，取 `priority` 最高者；若同优先级有多条，直接报「Multiple OpKernel registrations match」错误（[op_kernel.cc:1469-L1477](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1469-L1477)）。还有一个**回退机制**：若没有任何设备专属 kernel，会去查 `DEVICE_DEFAULT`（[op_kernel.cc:1490-L1522](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1490-L1522)）——这就是为什么有些「数据管理 / 控制流」类 op 能在任意设备上跑。

**真实例子**。[cwise_op_add_1.cc:19-L66](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/kernels/cwise_op_add_1.cc#L19-L66) 把 `"Add"` 这个 op 的多 kernel 注册一次性展示出来：

```cpp
// cwise_op_add_1.cc:19-23（CPU，六种类型）
REGISTER6(BinaryOp, CPU, "Add", functor::add, float, Eigen::half, double,
          int32_t, int64_t, bfloat16);
// cwise_op_add_1.cc:28（GPU，三种类型）
REGISTER3(BinaryOp, GPU, "Add", functor::add, float, Eigen::half, double);
// cwise_op_add_1.cc:38-44（GPU 上对 int32 的特例：强制输入放 host 内存）
REGISTER_KERNEL_BUILDER(Name("Add").Device(DEVICE_GPU)
                            .HostMemory("x").HostMemory("y").HostMemory("z")
                            .TypeConstraint<int32_t>("T"),
                        BinaryOp<CPUDevice, functor::add<int32_t>>);
// cwise_op_add_1.cc:53-59（DEVICE_DEFAULT，给可插拔设备兜底）
REGISTER_KERNEL_BUILDER(Name("Add").Device(DEVICE_DEFAULT)
                            .HostMemory("x").HostMemory("y").HostMemory("z")
                            .TypeConstraint<int32_t>("T"),
                        BinaryOp<CPUDevice, functor::add<int32_t>>);
```

`REGISTER6` / `REGISTER3` 只是 [cwise_ops_common.h:620-L622](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/kernels/cwise_ops_common.h#L620-L622) 的 `REGISTER` 宏展开 N 次的简写，本质还是 `REGISTER_KERNEL_BUILDER(Name("Add").Device(DEVICE_CPU).TypeConstraint<T>("T"), BinaryOp<CPUDevice, functor::add<T>>)`。一个文件里就注册了「CPU×6 类型 + GPU×4 类型 + DEFAULT×1」共十余个 kernel，全部服务于同一个 `"Add"` OpDef。

注意第 38 行那条特殊注册：它在 GPU 设备上却要求 `HostMemory("x"/"y"/"z")`——意思是「在 GPU 上执行、但 int32 数据留在 CPU 内存」，这是为绕开早期 GPU 对 device 内存 int32 运算的限制而设的特例（见注释里的 TODO(b/25387198)）。这正是「同一个 Op 在不同条件下用不同 kernel」的典型体现。

#### 4.4.4 代码实践

> **实践目标**：亲手把「注册语句」与「查找键」对应起来，理解运行时为何能精确命中。
>
> **操作步骤**：
> 1. 打开 [cwise_op_add_1.cc](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/kernels/cwise_op_add_1.cc)，把所有 `Name("Add")` 的注册语句抄成一张表，列三列：`Device` / `TypeConstraint<T>` / `HostMemory?`。
> 2. 对照 [op_kernel.cc:1279-L1283](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1279-L1283) 的 `Key` 函数，写出每条注册会落进 `multimap` 的哪个键。
> 3. 假设运行时要为「device=GPU、T=float」的 `"Add"` 节点选 kernel，用 [op_kernel.cc:1462-L1487](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1462-L1487) 的逻辑推演：`equal_range("Add:GPU:")` 会取出哪几条候选？`KernelAttrsMatch` 会留下哪一条？
>
> **需要观察的现象**：同一个键 `"Add:GPU:"` 下挂了多条注册（float / half / double / bfloat16 / int32-host），查找时靠 `TypeConstraint` 比对进一步筛出与 `T=float` 匹配的那一条。
>
> **预期结果**：你能说清「op 名 + 设备决定取哪一组候选；TypeConstraint 决定组内最终命中哪一个」的两级筛选过程。这一步是纯源码阅读，**待本地验证**（若想实跑，可在装好 TF 的环境里用 `tf.raw_ops.Add` 在不同 dtype/device 上调用，观察 Profiler 里实际命中的 kernel 名）。

#### 4.4.5 小练习与答案

**练习 1**：`DEVICE_CPU`、`DEVICE_GPU`、`DEVICE_DEFAULT` 三者作为注册设备有什么区别？

**参考答案**：前两者是**设备专属** kernel，只在对应设备上被选中。`DEVICE_DEFAULT` 是**兜底** kernel——当某个 op 在请求的设备上没有任何专属注册时，查找逻辑会回退去查 `DEVICE_DEFAULT`（[op_kernel.cc:1490-L1522](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1490-L1522)）。它通常用于与设备无关的控制流、数据管理 op，或给可插拔设备（PluggableDevice）提供支持。注意头文件注释强调：用 `DEVICE_DEFAULT` 的 kernel 必须全部输入输出走 `HostMemory`（[op_kernel.h:1378-L1385](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L1378-L1385)）。

**练习 2**：如果两个注册的 (op, device, TypeConstraint) 完全相同、优先级也相同，会发生什么？

**参考答案**：`FindKernelRegistration` 在发现第二条同等优先级的匹配时会直接返回 `InvalidArgumentError`，报「Multiple OpKernel registrations match NodeDef at the same priority」（[op_kernel.cc:1470-L1477](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1470-L1477)）。也就是说注册表不允许歧义——必须有明确的「唯一最优」kernel，否则启动期就会暴露错误。`priority` 字段正是用来在「通用 kernel」与「针对某设备的特化 kernel」之间做选择的。

## 5. 综合实践

把本讲的四条主线串起来，完成下面这个**源码阅读 + 推理**任务：

**任务背景**：TF 里有一个逐元素 op `"Add"`。请你以「Op 注册者 + 运行时调度器」的双重视角，完整描述它从注册到执行的全过程。

**步骤 1（说明书层）**：在 `core/ops/math_ops.cc` 里找到 `REGISTER_OP(Name("Add") ...)`，写出它的 OpDef 关键字段：几个 `input_arg`、几个 `output_arg`、有哪些 `attr`（预期看到属性 `T`、输入 `x`/`y` 同类型 `T`、输出 `z` 同类型 `T`）。

**步骤 2（实现层）**：在 `core/kernels/cwise_op_add_1.cc` 里统计 `"Add"` 注册了多少个 kernel，按「设备 × 类型 × 是否 HostMemory」分类列成表格。

**步骤 3（注册机制）**：解释每条注册语句经过 [op_kernel.h:1469-L1502](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L1469-L1502) 的宏展开后，最终在 [op_kernel.cc:1397-L1419](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1397-L1419) 里往 `KernelRegistry` 塞了什么（一个 `Key` + 一个 `KernelRegistration{KernelDef, 类名, 工厂 lambda}`）。

**步骤 4（查找与执行）**：假设运行时遇到节点 `NodeDef{op:"Add", device:"/job:localhost/replica:0/task:0/device:GPU:0", attr:{T:DT_FLOAT}}`。推演：
- `CreateOpKernel`（[op_kernel.cc:1780-L1839](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.cc#L1780-L1839)）如何算出查找键、`equal_range` 取出哪些候选、`KernelAttrsMatch` 留下哪一条；
- 工厂 `Create` 造出的是哪个具体类（`BinaryOp<GPUDevice, functor::add<float>>`）；
- 执行器随后调它的 `Compute`（[cwise_ops_common.h:88-L228](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/kernels/cwise_ops_common.h#L88-L228)），在 `Compute` 内部经 `ctx->input()` 取输入、`ctx->forward_input_or_allocate_output(...)` 分配输出、`ctx->eigen_device<GPUDevice>()` 取 GPU 句柄、最后由 Eigen functor 完成加法。

**交付物**：一张「OpDef → 多 Kernel 注册表 → 查找命中 → Compute 执行」的全链路示意图（手绘或文字均可），并在图上标注每一步对应的源码文件与行号。本任务是纯阅读型，**待本地验证**执行结果；如环境允许，可用 `tf.config.list_physical_devices()` + `tf.raw_ops.Add` 在 GPU 上跑一次，再用 Profiler 确认命中的确实是 GPU float kernel。

## 6. 本讲小结

- **OpKernel 是 OpDef 的「计算实现」**：OpDef（u4-l1）是说明书，OpKernel 才是真正在 `Compute` 里读写张量、做运算的代码；二者是「一对多」关系——一个 Op 可有按设备/类型区分的多个 kernel。
- **唯一必须 override 的方法是 `Compute`**：它是纯虚函数（[op_kernel.h:158](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/op_kernel.h#L158)），返回 `void`，输入输出与错误全部经由 `OpKernelContext` 传递。
- **同步 vs 异步**：普通 kernel 继承 `OpKernel`、实现 `Compute`；需要阻塞等待别的 op（Recv、队列）时必须继承 `AsyncOpKernel`、实现 `ComputeAsync` 并保证 `done` 恰好调用一次；`OP_REQUIRES` 在异步里被禁用（会 crash）。
- **`OpKernelContext` 是唯一通道**：`input()` 取输入、`allocate_output()` / `set_output()` 写输出、`allocate_temp()` 要临时区、`SetStatus()` / `OP_REQUIRES` 上报错误、`eigen_device<D>()` 取设备；`OpKernelConstruction` 则只在构造期用于读属性。
- **注册靠 `REGISTER_KERNEL_BUILDER`**：链式描述 `Name(op).Device(dev).TypeConstraint<T>("T").HostMemory(...)` 声明适用范围，启动期经 `OpKernelRegistrar` 把「键 + KernelDef + 工厂」塞进全局 `KernelRegistry`。
- **查找靠两级筛选**：运行时 `CreateOpKernel` 先用 `(op, device, label)` 在 multimap 里取候选组，再用 `KernelAttrsMatch` 按 TypeConstraint 选出唯一最优（按 priority），最后调工厂 `Create` 造出对象。

## 7. 下一步学习建议

- **接着看 op 的「声明侧」实现**：u4-l3（`core/ops` 与形状推导）会讲 OpDef 里那段 `SetShapeFn` 如何在**建图期**就推导出输出形状——它与本讲的 `Compute`（运行期算值）是互补的：一个推形状、一个算数值。
- **看一个从零到一的完整自定义 op**：u9-l1 会以 `tensorflow/examples/adding_an_op/zero_out_op` 为例，带你亲手写一个 OpKernel 子类、注册它、编译进 Python、并补上梯度。那是对本讲「OpKernel 子类模板」「`REGISTER_KERNEL_BUILDER`」的实战检验。
- **深入 kernel 的运行环境**：本讲多次提到 `device()` 与 `eigen_device<D>()`，但「设备到底是什么」「Allocator 如何给输出张量分内存」要等 u6-l1（Device 与 DeviceFactory）和 u6-l2（Allocator 与 BFCAllocator）才完整展开——届时回看 `allocate_output` → `allocate_tensor` → `device->GetAllocator(attr)` 这条链会有更深的理解。
- **回看执行链路**：本讲讲的是「单个 kernel 如何被调用」，而「执行器如何遍历整张图、按拓扑序逐个调 kernel」在 u3-l2（Session 与 DirectSession）里已铺垫过；把两讲对照，你能补全「图 → Executor → OpKernelContext → Compute」的端到端画面。
