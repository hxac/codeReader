# 自定义 Op 全流程

## 1. 本讲目标

本讲是「扩展与二次开发」单元的第一讲。TensorFlow 内置了数千个 op，但当你需要一个**它没有提供的算子**（一种特殊的数值裁剪、一段用 CUDA 写的高性能 kernel、一个定制硬件指令的包装）时，唯一的办法就是**自己写一个 op 并把它接进 TF 运行时**。

本讲以仓库自带的官方示例 `tensorflow/examples/adding_an_op/`（对应 `https://tensorflow.org/guide/create_op`）为主线，把「自定义 op」从一头走到另一头。读完本讲你应当能够：

1. **写**：用 C++ 写出一个最小的自定义 op——声明它的输入输出与形状（`REGISTER_OP`），并实现它的计算（`OpKernel::Compute`）。
2. **编译并加载**：用 Bazel 把它编译成动态库 `.so`，再用 `tf.load_op_library` 把它加载进已经 `import tensorflow` 的 Python 进程，理解 `dlopen` 背后那段「动态注册」机制。
3. **配梯度**：用 `@tf.RegisterGradient` 为自定义 op 提供反向求导函数，让它能参与 `GradientTape` / `tf.gradients` 的训练。
4. **对照两条路线**：明白「运行时动态加载 `.so`」与「构建期静态链入 `core/user_ops`」两种让 op 生效的方式的区别。

本讲会把前几讲（u4-l1 的 Op 注册、u4-l2 的 OpKernel、u4-l3 的形状推导、u4-l5 的代码生成、u5-l1 的自动微分）学到的机制，**串成一条可复现的工程链路**。

## 2. 前置知识

本讲依赖以下已建立的认知（不重复展开，只承接）：

- **Op 是声明，Kernel 是实现**（u4-l1、u4-l2）：`REGISTER_OP` 写一张「说明书」（`OpDef`：名字、输入、输出、属性、形状推导），`REGISTER_KERNEL_BUILDER` 给某个 op 在某个设备/类型上配一个「工人」（`OpKernel` 子类，实现 `Compute`）。运行时用 `OpKernelContext` 这条总线取输入、写输出、报错误。
- **注册靠「启动期登记、惰性求值」**（u4-l1）：`REGISTER_OP` 借 C++ 静态全局变量，在 `main` 之前把一个工厂 lambda 推入全局 `OpRegistry::Global()` 的延迟列表，首次 `LookUp` 时才真正构造 `OpDef`。
- **形状推导独立于 kernel**（u4-l3）：`SetShapeFn` 在不执行 kernel 的前提下，用 `InferenceContext` 推断输出形状，服务于类型检查、图优化、XLA 编译与自动微分。
- **op 到 Python 的最后一层是代码生成**（u4-l5）：C++ 的 `REGISTER_OP` 是唯一真相源，由 `python_op_gen_main` 读出 `OpDef` 生成 `gen_*_ops.py`，之上再叠一层手写包装；op 名 `FooBar` → Python 函数名 `foo_bar`。
- **自动微分靠 grad_fn**（u5-l1）：反向模式 autodiff 对每个 op 查一张全局的「op 名 → 梯度函数」表；梯度函数的契约是 `(op, *上游梯度) → (*下游梯度)`，图模式与 Eager 模式共用同一套 grad_fn。

还需要一个底层概念：**动态链接库（`.so` / `.dll` / `.dylib`）**。它是一段编译好的机器码，可以在程序**已经运行起来之后**再加载进来；加载时操作系统会执行库内所有「静态全局变量」的初始化代码——这正是 TF 借以「在运行时把新 op 注入进程」的物理基础，也是本讲一切的关键。

## 3. 本讲源码地图

本讲涉及两类「op 进入进程」的范例，先建立空间感：

| 文件 | 作用 | 所属模型 |
|------|------|----------|
| `tensorflow/examples/adding_an_op/zero_out_op_kernel_1.cc` | 最小自定义 op「ZeroOut」的 C++ 声明 + kernel 实现 | 动态加载 |
| `tensorflow/examples/adding_an_op/zero_out_op_kernel_2.cc` | 进阶：用属性 `T` 把 op 泛型化到多种数值类型 | 动态加载 |
| `tensorflow/examples/adding_an_op/zero_out_op_kernel_3.cc` | 进阶：带属性 `preserve_index` 与构造期/运行期校验 | 动态加载 |
| `tensorflow/examples/adding_an_op/zero_out_op_1.py` | 用 `load_op_library` 加载 `.so` 的 Python 包装 | 动态加载 |
| `tensorflow/examples/adding_an_op/zero_out_grad_2.py` | 为 ZeroOut 提供梯度函数 `_zero_out_grad` | 动态加载 |
| `tensorflow/examples/adding_an_op/cuda_op_kernel.cc` / `.cu.cc` | GPU 版自定义 op「AddOne」（主机壳 + 设备 kernel） | 动态加载（GPU） |
| `tensorflow/examples/adding_an_op/BUILD` | 把上述 `.cc` 编译成 `.so` 的 Bazel 规则 | 构建 |
| `tensorflow/tensorflow.bzl`（`tf_custom_op_library` 宏） | 自定义 op 动态库的标准构建配方 | 构建 |
| `tensorflow/core/framework/load_library.cc` | C++ 侧 `LoadDynamicLibrary`：dlopen + watcher 抓取 OpList | 运行时加载 |
| `tensorflow/c/c_api.cc`（`TF_LoadLibrary` / `TF_GetOpList`） | C API 对加载能力的封装 | 运行时加载 |
| `tensorflow/python/framework/load_library.py` | Python 侧 `load_op_library`：加载 + 现场生成包装 | 运行时加载 |
| `tensorflow/python/framework/ops.py`（`RegisterGradient`） | Python 侧梯度注册装饰器 | 梯度 |
| `tensorflow/core/user_ops/fact.cc` / `BUILD` | 「Fact」op：**构建期静态链接**进 TF 的对照例 | 静态链接 |
| `tensorflow/python/user_ops/BUILD` / `user_ops.py` | Fact 在 Python 侧的构建期代码生成与手写包装 `my_fact` | 静态链接 |

一句话区分两个模型：`examples/adding_an_op` 的 op 需要**运行时手动 `load_op_library`**；`core/user_ops` 的 op 在**构建 TF 本体时就被链接进去**，`import tensorflow` 后立即可用。理解这条对照，就抓住了本讲的主轴。

## 4. 核心概念与源码讲解

### 4.1 写一个最小的 C++ Op：ZeroOut 与 Fact

#### 4.1.1 概念说明

「自定义 op」最朴素的定义是：**一段你自己写的 C++ 代码，它声明了一个 TF 不认识的新算子，并给出了这个算子的计算实现。**

我们全程围绕一个极简需求展开——**ZeroOut**：输入一个 `int32` 张量，输出一个同形状张量，除**第一个元素**保留原值外，其余全部置 0。例如 `[5,4,3,2,1]` → `[5,0,0,0,0]`。

写一个 op 需要三块拼图，它们来自 u4-l1/u4-l2，这里只是把它们组装起来：

1. **说明书（声明）**：`REGISTER_OP("ZeroOut")` —— 声明名字、输入 `to_zero: int32`、输出 `zeroed: int32`、形状推导（输出形状 = 输入形状）。
2. **工人（实现）**：一个继承 `OpKernel` 的类，实现 `Compute`，在其中读输入、分配输出、做计算。
3. **挂接（注册）**：`REGISTER_KERNEL_BUILDER` 把工人挂到说明书上，并指定设备。

与之并行的第二个例子是 `core/user_ops/fact.cc` 的 **Fact**：它**没有输入**，只输出一个标量字符串 `"0! == 1"`。Fact 的价值不在于计算多复杂，而在于展示「op 可以没有输入」以及「它走的是另一条进入进程的路径」（4.3 节展开）。

#### 4.1.2 核心流程

写一个最小 op 的固定四步：

```text
1. #include 必要头文件（op.h / op_kernel.h / shape_inference.h）
2. REGISTER_OP("名字").Input(...).Output(...).SetShapeFn(...).Doc(...)
3. class XxxOp : public OpKernel { void Compute(ctx) override { ... } };
4. REGISTER_KERNEL_BUILDER(Name("名字").Device(DEVICE_CPU), XxxOp);
```

其中第 2 步是**声明**（不产生可执行计算），第 3 步是**实现**，第 4 步把实现**挂到**声明上并指定设备。`Compute` 内部的固定骨架是「取输入 → 分配输出 → 填充输出」：

```text
const Tensor& in = ctx->input(0);          // 取输入
Tensor* out = nullptr;
ctx->allocate_output(0, in.shape(), &out); // 分配输出（形状通常等于输入）
auto out_flat = out->flat<int32_t>();      // 取一维视图便于逐元素写
// ... 计算 ...
```

#### 4.1.3 源码精读

先看 ZeroOut 的完整声明（说明书）：

[tensorflow/examples/adding_an_op/zero_out_op_kernel_1.cc:22-34](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_op_kernel_1.cc#L22-L34) —— 注册 `ZeroOut`：声明输入 `to_zero: int32`、输出 `zeroed: int32`，并用 lambda 设形状函数「输出第 0 个的形状 = 输入第 0 个的形状」（u4-l3 讲过的 `set_output(0, input(0))`），最后 `.Doc(...)` 附一段文档。

再看它的 kernel 实现（工人）：

[tensorflow/examples/adding_an_op/zero_out_op_kernel_1.cc:36-60](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_op_kernel_1.cc#L36-L60) —— `ZeroOutOp`：构造函数仅把 `context` 透传给基类 `OpKernel`；`Compute` 里先用 `context->input(0)` 取输入张量，用 `flat<int32_t>()` 取它的一维视图；用 `allocate_output(0, input_tensor.shape(), &output_tensor)` 分配同形状输出；先把除第一个外的元素全置 0，最后把 `input(0)` 拷到 `output(0)`。注意 `OP_REQUIRES_OK(context, ...)`（u4-l2 讲过）会把 `allocate_output` 可能返回的错误上报到 `OpKernelContext`，一旦失败立即中止本次 `Compute`。

把工人挂到说明书上并指定 CPU——一行：

[tensorflow/examples/adding_an_op/zero_out_op_kernel_1.cc:62](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_op_kernel_1.cc#L62) —— `REGISTER_KERNEL_BUILDER(Name("ZeroOut").Device(DEVICE_CPU), ZeroOutOp)`：这一行说「名叫 ZeroOut 的 op，在 CPU 设备上，用 `ZeroOutOp` 这个类来实现」。

> 对照基类契约：[tensorflow/core/framework/op_kernel.h:158](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/framework/op_kernel.h#L158) —— `virtual void Compute(OpKernelContext* context) = 0;`，这正是 `ZeroOutOp` 必须 override 的纯虚方法（u4-l2）。

这个文件还演示了「带命名空间的 op 名」：

[tensorflow/examples/adding_an_op/zero_out_op_kernel_1.cc:64-96](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_op_kernel_1.cc#L64-L96) —— `REGISTER_OP("Namespace>ZeroOut")` 与 `Namespace>Nested>ZeroOut`：用 `>` 分隔的层次化名字，到 Python 侧会被翻译成下划线（见 4.3.3）。注意它们**复用同一个 `ZeroOutOp` 类**——说明书可以有多个（不同名字），工人只需写一次。

再看对照例 Fact（无输入、字符串输出），三件套完全同构：

[tensorflow/core/user_ops/fact.cc:27-29](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/user_ops/fact.cc#L27-L29) —— 注册 `Fact`：只有 `Output("fact: string")`，无 `Input`；形状函数用现成的 `shape_inference::UnknownShape`。

[tensorflow/core/user_ops/fact.cc:31-46](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/user_ops/fact.cc#L31-L46) —— `FactOp::Compute`：用 `allocate_output(0, TensorShape(), &output_tensor)` 分配一个**标量**（空 `TensorShape` 即 0 维）输出，取其 `scalar<tstring>()` 视图，写入字符串 `"0! == 1"`。

[tensorflow/core/user_ops/fact.cc:48](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/user_ops/fact.cc#L48) —— `REGISTER_KERNEL_BUILDER(Name("Fact").Device(DEVICE_CPU), FactOp)` 收尾。

> 结论：ZeroOut 与 Fact 在「写法」上完全同构——都是 `REGISTER_OP` + `OpKernel::Compute` + `REGISTER_KERNEL_BUILDER` 三件套。它们真正的区别在「如何进入进程」，那是 4.3 节的主题。

#### 4.1.4 代码实践（源码阅读型，无需编译）

**目标**：确认你能读懂一个自定义 op 的三件套，并能预测它的行为。

**步骤**：

1. 打开 `tensorflow/examples/adding_an_op/zero_out_op_kernel_1.cc`，分别定位 `REGISTER_OP("ZeroOut")`、`ZeroOutOp::Compute`、`REGISTER_KERNEL_BUILDER` 三段。
2. 对照官方测试 [tensorflow/examples/adding_an_op/zero_out_1_test.py:26-28](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_1_test.py#L26-L28) 的断言 `assertAllEqual(result, [5, 0, 0, 0, 0])`。
3. 仅凭 `Compute` 的源码手算 `zero_out([5,4,3,2,1])` 的结果，再设想输入为 2×3 的 `[[6,5,4],[3,2,1]]` 会怎样。

**需要观察的现象 / 预期结果**：输入 `[5,4,3,2,1]`，`N=5`，循环把 `output(1..4)` 置 0，再把 `input(0)=5` 写到 `output(0)`，结果正是 `[5,0,0,0,0]`，与测试断言一致。换成 2×3 时，由于 `flat<int32_t>()` 把它按行主序拍平成 6 元素一维视图，结果应是首元素 6 保留、其余 5 个为 0，即 `[[6,0,0],[0,0,0]]`（这正是 `zero_out_2_test.py` 里 `test_2d` 的断言）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Compute` 里最后的 `if (N > 0) output(0) = input(0);` 删掉，`zero_out([5,4,3,2,1])` 会变成什么？

**答案**：会变成 `[0,0,0,0,0]`——因为前一个循环已经把所有元素（含下标 0）置 0，而保留首值的语句被删了。这也说明 `if (N > 0)` 同时承担了「空张量安全」的作用：`N==0` 时跳过访问，避免越界。

**练习 2**：Fact op 为什么 `allocate_output` 的第二个参数传 `TensorShape()`（空形状）而不是 `input_tensor.shape()`？

**答案**：因为 Fact 没有输入，且输出是一个**标量字符串**（0 维张量），空 `TensorShape` 正好表示标量；它不依赖任何输入形状。

---

### 4.2 把 Op 编译成动态库：tf_custom_op_library

#### 4.2.1 概念说明

写完 `.cc` 只是写了源码，TF 运行时并不会去编译它。你必须在**构建期**用 Bazel 把这段 C++ 编译、链接成一个**动态库**（Linux 下是 `.so`），这个 `.so` 里有两样东西：

- **机器码**：你的 `Compute` 函数真正编译后的指令。
- **注册副作用**：`REGISTER_OP` / `REGISTER_KERNEL_BUILDER` 宏展开后是 C++「静态全局变量」，它们在 `.so` 被 `dlopen` 加载时会自动执行，把自己登记进全局注册表。

`tf_custom_op_library` 就是 TF 提供的「自定义 op 动态库标准构建宏」，帮你处理好依赖头文件、GPU 源码分离、符号导出等繁琐细节。

#### 4.2.2 核心流程

```text
你的 .cc (REGISTER_OP + OpKernel)
        │  tf_custom_op_library(name="xxx.so", srcs=[...])
        ▼
   ┌─────────────── Bazel 构建 ───────────────┐
   │ 1. 装配依赖：框架头文件库 + 可选 CUDA 头  │
   │ 2. check_deps：禁止静态链接 framework/lib │
   │ 3. （可选）cuda_library 合并 GPU 源码     │
   │ 4. tf_cc_shared_object 产出动态库 xxx.so  │
   └────────────────────────────────────────────┘
        │
        ▼
   xxx.so （机器码 + 静态注册副作用）
```

这里有一个**反直觉但关键**的设计：`check_deps` 会**禁止**这个 `.so` 静态链接 `//tensorflow/core:framework` 和 `//tensorflow/core:lib`。为什么？因为这些符号（如 `OpRegistry::Global()`、`OpKernel` 的实现）**已经存在于正在运行的 TF 进程里**（那个巨大的 `_pywrap_tensorflow_internal.so`）。自定义 op 的 `.so` 只需要引用头文件去「声明」这些符号，运行时由操作系统把动态库符号解析到 TF 进程已有的实现上——这样你的 `.so` 才能保持很小，且与 TF 主版本二进制兼容。

#### 4.2.3 源码精读

先看调用方——`adding_an_op/BUILD` 如何把 `zero_out_op_kernel_1.cc` 编译成 `.so` 并配一个 Python 库：

[tensorflow/examples/adding_an_op/BUILD:32-43](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/BUILD#L32-L43) —— `tf_custom_op_library(name="zero_out_op_kernel_1.so", srcs=["zero_out_op_kernel_1.cc"])` 产出动态库；随后的 `py_library(name="zero_out_op_1", data=[":zero_out_op_kernel_1.so"], ...)` 把 `.so` 作为 `data` 打包，使 `.so` 跟着 Python 文件一起部署到同一目录（这是 4.3 里 `get_data_files_path()` 能定位它的前提）。

再看宏本身的实现——`tensorflow.bzl` 里的 `tf_custom_op_library`：

[tensorflow/tensorflow.bzl:2316-2349](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/tensorflow.bzl#L2316-L2349) —— 宏签名与依赖装配：把额外的框架头文件依赖（`tf_custom_op_library_additional_deps()`）、可选的 CUDA/ROCm 头文件都加进 `deps`。

[tensorflow/tensorflow.bzl:2356-2366](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/tensorflow.bzl#L2356-L2366) —— GPU 源码分支：若有 `gpu_srcs`，用 `cuda_library` 单独编译设备代码（`basename + "_gpu"`），再并入 `deps`。

[tensorflow/tensorflow.bzl:2368-2391](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/tensorflow.bzl#L2368-L2391) —— 关键收尾：`check_deps` 显式把 `//tensorflow/core:framework` 与 `//tensorflow/core:lib` 列为 `disallowed_deps`；`tf_cc_shared_object` 把源码编译并链接成最终动态库（Windows 上用 `windows_export_all_symbols` 自动导出符号）。

对照「GPU op」的构建——`AddOne` 同时有 CPU 包装层和 CUDA kernel：

[tensorflow/examples/adding_an_op/BUILD:128-132](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/BUILD#L128-L132) —— `tf_custom_op_library(name="cuda_op_kernel.so", srcs=["cuda_op_kernel.cc"], gpu_srcs=["cuda_op_kernel.cu.cc"])`：`srcs` 是 Host（CPU 侧的 op 调度逻辑），`gpu_srcs` 是设备（GPU 侧真正的 CUDA kernel），宏内部用 `cuda_library` 把两者合并进同一个 `.so`。

#### 4.2.4 代码实践（构建型，需 Bazel + ./configure）

**目标**：亲手把 ZeroOut 编译成 `.so` 并确认产物存在。

**步骤**：

1. 在仓库根目录运行 `./configure`（按 u1-l3 完成 CPU/CUDA 探测）。
2. 构建动态库目标：
   ```bash
   bazel build //tensorflow/examples/adding_an_op:zero_out_op_kernel_1.so
   ```
3. 在 `bazel-bin/tensorflow/examples/adding_an_op/` 下查看产物 `zero_out_op_kernel_1.so`。

**需要观察的现象 / 预期结果**：构建成功后应得到一个体积不大的 `.so`（远小于 TF 本体，因为它只含你的 kernel 与注册副作用，未静态包含 framework）。具体产物路径与体积**待本地验证**（取决于本机配置与是否启用 CUDA）。

> 备注：从源码全量构建 TF 耗时较长（数十分钟到数小时）。若不具备构建环境，可跳过本实践，直接进入 4.3.4 的源码阅读实践。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tf_custom_op_library` 要用 `check_deps` 禁止链接 `//tensorflow/core:framework`？

**答案**：因为这些符号在 TF 主进程里已存在。若静态链接进来，会出现「两份 framework 代码」导致符号冲突与注册表不一致；只引用头文件、运行时解析到进程已有符号，才能让 `.so` 小巧且与 TF 版本二进制兼容。

**练习 2**：GPU op 的 `srcs` 和 `gpu_srcs` 分别承担什么职责？

**答案**：`srcs`（如 `cuda_op_kernel.cc`）是 Host 侧的 `OpKernel::Compute`，负责取输入、调用 kernel launcher、写输出；`gpu_srcs`（如 `cuda_op_kernel.cu.cc`）是设备侧真正的 CUDA kernel 与它的 launcher 函数，由 `nvcc` 编译。

---

### 4.3 把 .so 加载进 Python：动态注册机制

> 这是本讲最核心、也最容易被忽略的一节。它回答一个关键问题：**`load_op_library` 到底做了什么，才让一个全新的 op 在已经启动的 Python 进程里「凭空可用」？** 同时它也是动态加载与静态链接两条路线的分水岭。

#### 4.3.1 概念说明

动态加载的本质是一条「**副作用 + 现场代码生成**」的流水线。`.so` 里并没有现成的 Python 函数 `zero_out`——它只有 C++ 的注册副作用和 kernel 机器码。`load_op_library` 的工作是：

1. **dlopen**：让操作系统加载 `.so`，触发其中的 C++ 静态全局变量初始化 → `REGISTER_OP`/`REGISTER_KERNEL_BUILDER` 把新 op/kernel 登记进全局注册表。
2. **抓 OpList**：让 TF 把「**本次新注册**的 op 说明书」序列化成一个 `OpList` protobuf 缓冲区返回（不是全部 op，只本次新增）。
3. **现场生成 Python 包装**：调用与构建期 `gen_*_ops.py` **同一个**代码生成器（u4-l5 讲过的 `python_op_gen`），把 `OpList` 即时翻译成 Python 源码字符串。
4. **exec 成模块**：把这段源码 `exec` 进一个新建的 Python 模块对象，于是 `module.zero_out` 就成了可调用函数。

而 `core/user_ops`（Fact）走的是另一条路：op 在**构建 TF 本体时**就被 `alwayslink` 链接进去，`import tensorflow` 时这些 `REGISTER_OP` 就已执行，op 早已在全局注册表里——**无需 `load_op_library`**，Python 包装也在构建期一次性生成。

#### 4.3.2 核心流程

```text
Python: tf.load_op_library("zero_out_op_kernel_1.so")
   │
   ├─(1) py_tf.TF_LoadLibrary(path)
   │       └─ C++ LoadDynamicLibrary(path):
   │            a. 命中 loaded_libs 缓存？→ 直接返回旧 OpList
   │            b. ProcessRegistrations()   # 先处理之前积压的注册
   │            c. SetWatcher(λ)            # 装监听器，捕获「新注册」的 OpDef
   │            d. DeferRegistrations()     # 进入延迟模式
   │            e. env->LoadDynamicLibrary  # dlopen，触发 .so 内 REGISTER_OP
   │            f. ProcessRegistrations()   # 处理 .so 带来的新注册 → 经 watcher 落入 op_list
   │            g. 序列化 op_list → 返回 buf/len
   │
   ├─(2) TF_GetOpList(handle)              # 取出刚刚序列化的 OpList 缓冲区
   │
   ├─(3) GetPythonWrappers(op_list)        # 现场生成 gen_*_ops 风格的 Python 源码
   │
   └─(4) exec(wrappers, module.__dict__)   # 注入新模块 → module.zero_out 可用
```

其中 e 这一步的 `dlopen` 是物理基础；c/d/f 的 watcher+defer 机制是为了**精确区分「哪些 op 是这个 .so 新带来的」**（只把这些做成 OpList，避免和内置 op 混淆）。

#### 4.3.3 源码精读

**Python 入口**——`zero_out_op_1.py` 怎么把 `.so` 变成 `zero_out` 函数：

[tensorflow/examples/adding_an_op/zero_out_op_1.py:20-25](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_op_1.py#L20-L25) —— 用 `tf.compat.v1.resource_loader.get_data_files_path()` 定位与 `.py` 同目录的 `.so`，调 `tf.load_op_library` 得到模块对象，再把它的三个属性 `zero_out` / `namespace_zero_out` / `namespace_nested_zero_out` 导出到本模块命名空间。注意 op 名到 Python 函数名的翻译：`ZeroOut` → `zero_out`（PascalCase 转 snake_case，u4-l5），`Namespace>ZeroOut` → `namespace_zero_out`（`>` 转 `_`）。

**`load_op_library` 的实现**——加载 + 抓 OpList + 现场生成 + exec：

[tensorflow/python/framework/load_library.py:31-74](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/framework/load_library.py#L31-L74) —— 先 `TF_LoadLibrary` 拿到库句柄；用 `TF_GetOpList(handle)` 取 OpList 缓冲区，交给 `_pywrap_python_op_gen.GetPythonWrappers` 生成 Python 包装源码；释放句柄；最后用 `wrappers` 内容的 sha1 作模块名，若该模块已在 `sys.modules` 则直接复用（这就是「重复加载返回同一对象」的原因），否则 `exec(wrappers, module.__dict__)` 注入新模块。文档里有一句要害：「ops with the same name as an existing op are rejected」——同名 op 会被拒绝注册。

**C API 边界**——把加载能力暴露给 Python：

[tensorflow/c/c_api.cc:570-578](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/c/c_api.cc#L570-L578) —— `TF_LoadLibrary`：`new` 一个 `TF_Library`，调 `tensorflow::LoadDynamicLibrary` 把库句柄和 OpList 缓冲区填进去。

[tensorflow/c/c_api.cc:582](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/c/c_api.cc#L582) —— `TF_GetOpList`：直接返回句柄里存的 OpList 缓冲区。

**真正的核心**——`LoadDynamicLibrary` 的 watcher 机制：

[tensorflow/core/framework/load_library.cc:46-102](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/framework/load_library.cc#L46-L102) —— 整个函数。注意 [第 48-49 行](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/framework/load_library.cc#L48-L49) 的静态缓存表 `loaded_libs`：同一个 `.so` 第二次加载会直接返回缓存结果，不重复初始化。

[tensorflow/core/framework/load_library.cc:58-82](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/framework/load_library.cc#L58-L82) —— 关键序列：先 `ProcessRegistrations()` 处理旧积压；`SetWatcher` 装一个 lambda，每当有 OpDef 注册成功就把它 `add_op` 进 `library.op_list` 并记入 `seen_op_names`，若 `AlreadyExists` 但不在本库 `seen_op_names` 内则视为「覆盖了同名内置 op、不算错误」（[第 65-70 行](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/framework/load_library.cc#L65-L70)）；`DeferRegistrations()` 切到延迟模式；`env->LoadDynamicLibrary(...)` 即 dlopen，触发 `.so` 内 `REGISTER_OP` 静态初始化；再 `ProcessRegistrations()` 把新注册经 watcher 收进 `op_list`。这段机制保证了：**返回给 Python 的 OpList 只含「这个 .so 新带来的 op」**。

**对照：静态链接模型（`core/user_ops`）**——Fact 走构建期链入：

[tensorflow/core/user_ops/BUILD:36-47](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/user_ops/BUILD#L36-L47) —— `cc_library(name="user_ops_op_lib", srcs=glob(['*.cc']), alwayslink=1, ...)`：`glob(['*.cc'])` 把 `user_ops/` 下**所有** `.cc`（含 `fact.cc`）收集进一个库；`alwayslink=1` 是这条路线的灵魂——它强制链接器保留所有 `.o`（即使「无人直接引用 `FactOp`」），使 `REGISTER_OP("Fact")` 的静态全局对象被链接进最终 TF 二进制并在进程启动时构造。于是 `import tensorflow` 时 Fact 已在全局注册表，**无需 `load_op_library`**。这个 target 再被 [tensorflow/core/BUILD:527-537](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/BUILD#L527-L537) 的 `:ops` 依赖，链入 TF 主库。

它的 Python 包装在**构建期**用代码生成器一次性产出（与 u4-l5 同一条流水线，只是提前到编译期）：

[tensorflow/python/user_ops/BUILD:32-36](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/user_ops/BUILD#L32-L36) —— `tf_gen_op_wrapper_private_py(out="ops/gen_user_ops.py")`：构建期生成 `gen_user_ops.py`（提供 `fact()`）。

[tensorflow/python/user_ops/user_ops.py:25-28](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/user_ops/user_ops.py#L25-L28) —— 手写包装 `my_fact()` 调 `_gen_user_ops.fact()`，并用 `@tf_export(v1=['user_ops.my_fact'])` 注册成 `tf.compat.v1.user_ops.my_fact`。注意 C++ op 名 `Fact`、生成函数名 `fact()`、公开名 `my_fact` 三者各不相同——这正是 u4-l5 讲的「公开名由 `@tf_export` 决定」。这也是 [tensorflow/examples/adding_an_op/fact_test.py:26](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/fact_test.py#L26) 里 `tf.compat.v1.user_ops.my_fact()` 能直接调用、**无 `load_op_library`** 的原因。

> 两种模型的本质对照：
>
> | 维度 | 动态加载（adding_an_op） | 静态链接（user_ops） |
> |------|--------------------------|----------------------|
> | 何时注册 | 运行时 `load_op_library`（dlopen 触发静态构造） | 进程启动时（静态全局对象构造，靠 `alwayslink`） |
> | 何时生成 Python 包装 | 加载时**现场生成**（`GetPythonWrappers`） | 构建期一次性生成（`gen_user_ops.py`） |
> | 是否需手动加载 | 是 | 否，`import tensorflow` 即可用 |
> | 是否需重编 TF | 否（插件 `.so`） | 是（链入主库） |
> | 适用场景 | 第三方插件、不想重编 TF | 随 TF 一同发布的内置扩展 |

#### 4.3.4 代码实践（源码阅读型，无需编译）

**目标**：验证「重复加载返回同一对象」这一缓存行为，并理解它由 C++ 与 Python 两层缓存共同保证。

**步骤**：

1. 阅读 [tensorflow/core/framework/load_library.cc:48-56](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/framework/load_library.cc#L48-L56)，确认 `loaded_libs` 是一个以文件名为键的静态映射，命中即返回缓存的 `library`（含已序列化的 `op_list`）。
2. 阅读 Python 侧缓存 [tensorflow/python/framework/load_library.py:64-66](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/framework/load_library.py#L64-L66)：模块名由 `wrappers` 的 sha1 决定，若已在 `sys.modules` 则直接返回。
3. 对照测试 [tensorflow/examples/adding_an_op/zero_out_1_test.py:43-47](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_1_test.py#L43-L47) 的 `test_load_twice`：第二次 `load_op_library` 得到的对象 `assertEqual` 于第一次。

**预期结果**：两次加载拿到完全相同的模块对象——这正是 C++ 与 Python 两层缓存共同保证的。即便不重新执行注册副作用，op 也早已在首次加载时进入全局注册表，故行为不变。

#### 4.3.5 小练习与答案

**练习 1**：如果两个不同的 `.so` 里都 `REGISTER_OP("ZeroOut")`，第二次加载会发生什么？

**答案**：第二个 `.so` 加载时，其 `ZeroOut` 会与全局已有的同名 op 冲突（`AlreadyExists`）。由于该名字不在「本库 `seen_op_names`」内，watcher 会把它当作「覆盖同名内置 op」**静默跳过**而不报错（见 [load_library.cc:65-70](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/framework/load_library.cc#L65-L70)），即第二次的同名 op 不会被注册、不生效。

**练习 2**：为什么 `load_op_library` 返回的 OpList 只含「本库新增的 op」，而不是全局所有 op？

**答案**：因为 watcher 只在 dlopen 触发的那次 `ProcessRegistrations` 期间捕获新注册的 OpDef；之前已注册的内置 op 不会再次经过 watcher。这样现场生成的 Python 包装才精确对应你的自定义 op，不会与内置 op 重复。

---

### 4.4 为自定义 Op 提供梯度：RegisterGradient

#### 4.4.1 概念说明

写完 `Compute`，你的 op 已经能**前向计算**了。但若想把它放进一个需要**训练**的模型（用 `GradientTape` 或 `tf.gradients` 求导），它还缺一样东西：**梯度函数**。

回顾 u5-l1：反向模式 autodiff 为每个 op 查一张「op 名 → grad_fn」表。内置 op 的 grad_fn 在 C++/Python 里早已注册；而你的自定义 op 是全新的，**默认没有 grad_fn**——一旦反向传播走到它，就会报「No gradient defined for op」。

解决办法有两条：

- 提供 grad_fn：用 `@tf.RegisterGradient("ZeroOut")` 注册一个梯度函数（**推荐**，表示这个 op 可微）。
- 声明不可微：用 `tf.no_gradient("ZeroOut")` 告诉 TF「这个 op 故意不参与求导」，反向传播到它会传播 0 而不报错（适用于 `tf.size` 这类本就无梯度的 op）。

注意一个常被混淆的点：**梯度的注册在 Python 侧完成**（`ops.RegisterGradient` → `gradient_registry.register`），而 op/kernel 的注册在 C++ 侧（`REGISTER_OP`/`REGISTER_KERNEL_BUILDER`）。两者是分开的两张表。

#### 4.4.2 核心流程

梯度函数的契约（u5-l1 已确立）：

```text
grad_fn(op, *grads_wrt_outputs) -> [*grads_wrt_inputs]
```

即：拿到原始 op、以及「损失对每个输出的梯度」，返回「损失对每个输入的梯度」。对 ZeroOut（1 输入 1 输出）而言就是 `_zero_out_grad(op, grad) -> [to_zero_grad]`。

先推导 ZeroOut 的梯度。设输入 \(x\)，输出 \(y = \text{ZeroOut}(x)\)，满足：

\[
y_0 = x_0,\qquad y_i = 0\ (i>0)
\]

雅可比矩阵 \(J\) 满足 \(J_{j,i} = \partial y_j/\partial x_i\)，只有 \(J_{0,0}=1\)，其余为 0。对标量损失 \(L\)，记上游梯度 \(g_j = \partial L/\partial y_j\)，则输入梯度为：

\[
\frac{\partial L}{\partial x_i} \;=\; \sum_j g_j\,J_{j,i} \;=\; g_0 \cdot \delta_{i,0}
\]

也就是：**输入梯度只有第 0 个位置等于上游梯度的第 0 个值，其余全为 0**。

#### 4.4.3 源码精读

ZeroOut 的梯度实现，逐行对应上面的数学：

[tensorflow/examples/adding_an_op/zero_out_grad_2.py:23-40](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_grad_2.py#L23-L40) —— `@ops.RegisterGradient("ZeroOut")` 装饰 `_zero_out_grad(op, grad)`：(1) 从 `op.inputs[0]` 拿原输入 `to_zero`（用来拿形状与首位置坐标）；(2) 算其形状 `shape` 与全零下标 `index = zeros_like(shape)`（即 `[0,0,...]`，指向首元素）；(3) 把上游梯度拍平取首值 `first_grad = reshape(grad, [-1])[0]`；(4) 用 `sparse_to_dense([index], shape, first_grad, 0)` 造一个「仅在首位置放 `first_grad`、其余为 0」的稠密张量作为输入梯度，包成单元素列表返回（因为有 1 个输入）。注意这里全程用 `array_ops`/`sparse_ops` 这些**已有 op** 来构造反向计算图，本身不再需要写 C++。

注册装饰器的实现——印证「按 op 名登记进全局表」：

[tensorflow/python/framework/ops.py:1756-1800](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/framework/ops.py#L1756-L1800) —— `RegisterGradient` 类：构造时记下 `op_type` 字符串；`__call__` 时调 `gradient_registry.register(f, self._op_type)` 把函数存进全局梯度注册表。文档里 `Sub` 的例子清楚说明了 m 输入 n 输出 op 的梯度函数签名约定（u5-l1）。

声明「不可微」的接口：

[tensorflow/python/framework/ops.py:1805-1819](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/framework/ops.py#L1805-L1819) —— `tf.no_gradient(op_type)`：把某 op 标记为不参与求导，反向传播到它会传播 0 而非报错。注意它**不应**用于「有梯度但还没写」的 op——后者应留空，让 TF 在被求导时**报错**提醒你补上。

如何验证梯度正确？官方测试用 `tf.test.compute_gradient` 同时算「解析梯度（你的 grad_fn）」与「数值梯度（有限差分）」并比对：

[tensorflow/examples/adding_an_op/zero_out_2_test.py:35-45](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_2_test.py#L35-L45) —— `test_grad` / `test_grad_2d`：对 `zero_out` 调 `tf.test.compute_gradient`，断言 `theoretical`（来自你的 grad_fn）与 `numerical`（数值近似）`assertAllClose`。这是检验自定义梯度的标准范式。

> 一个易错点：测试文件顶部 [tensorflow/examples/adding_an_op/zero_out_2_test.py:21](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_2_test.py#L21) 的 `from ... import zero_out_grad_2  # pylint: disable=unused-import`——**梯度注册是导入时的副作用**，必须确保定义梯度的模块被 import 进来，`@RegisterGradient` 才会执行。`# pylint: disable=unused-import` 注释正是为此（看似没用，实则触发注册）。

#### 4.4.4 代码实践（源码阅读型，无需编译）

**目标**：理解「梯度注册靠 import 副作用」这一关键事实，并手算 2D 输入的梯度。

**步骤**：

1. 取输入 `x = [[6,5,4],[3,2,1]]`（形状 `[2,3]`，float32）。前向输出是 `[[6,0,0],[0,0,0]]`（只保留展平后第 0 个 = `[0,0]` 位置的 6）。
2. 设上游梯度 `grad = [[g00,g01,g02],[g10,g11,g12]]`。按公式，对输入的梯度只在 `[0,0]` 位置等于 `g00`，其余为 0。
3. 对照代码验算：`shape=[2,3]`，`index=[0,0]`，`first_grad=reshape(grad,[-1])[0]=g00`，`sparse_to_dense([[0,0]],[2,3],g00,0)` = `[[g00,0,0],[0,0,0]]`，与手算一致。
4. 思考：若删掉测试文件顶部 `import zero_out_grad_2` 那一行，`test_grad` 会怎样？

**预期结果**：删掉后，`@ops.RegisterGradient("ZeroOut")` 不会被执行，全局梯度表里没有 `ZeroOut` 条目，`tf.test.compute_gradient` 反向求导到 `zero_out` 时会抛「No gradient defined for op: ZeroOut」。这正是 import 副用的意义——**保证梯度函数被注册**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 ZeroOut 的输入梯度「只在首位置非零」？

**答案**：因为前向 \(y_0=x_0,\ y_i=0\)，雅可比仅 \(J_{0,0}=1\)，故 \(\partial L/\partial x_i = g_0\delta_{i,0}\)，只有 \(i=0\) 处等于 \(g_0\)，其余为 0。

**练习 2**：`tf.no_gradient("ZeroOut")` 和「完全不注册梯度」有何区别？

**答案**：`no_gradient` 显式声明「故意不可微」，反向传播到它时**传播 0 不报错**；完全不注册则表示「应有梯度但未实现」，反向传播到它时**会报错**，提醒开发者补上。对一个确实可微的 op 误用 `no_gradient`，会得到错误（全 0）的梯度而不报错，很危险。

---

### 4.5 进阶变化：类型参数化、属性校验与 GPU kernel

#### 4.5.1 概念说明

`zero_out_op_kernel_1.cc` 的 ZeroOut 把类型写死成 `int32`，只能处理一种数据类型。真实场景里，你往往希望一个 op 能**支持多种类型**（int、float、double……），还能**接受配置参数**（比如「保留第几个元素」），甚至跑在 **GPU** 上。本节用三个进阶示例展示这些常用变化：

- **类型参数化**（`zero_out_op_kernel_2.cc`）：用属性 `T` 把类型变成参数，写一个 `template` kernel，再用宏批量注册多个类型。
- **属性校验**（`zero_out_op_kernel_3.cc`）：增加一个 `preserve_index` 属性，在构造期读出并校验取值范围。
- **GPU kernel**（`cuda_op_kernel.cc` + `.cu.cc`）：把真正的计算写成 CUDA kernel，CPU 侧只做调用。

#### 4.5.2 核心流程

**类型参数化**：

```text
REGISTER_OP("ZeroOut").Attr("T: realnumbertype").Input("to_zero: T").Output("zeroed: T")  // T 是类型参数
template <typename T> class ZeroOutOp : public OpKernel { ... input.flat<T>() ... }          // 一个模板类
REGISTER_KERNEL_BUILDER(Name("ZeroOut").Device(CPU).TypeConstraint<float>("T"),  ZeroOutOp<float>)  // 每种类型注册一次
REGISTER_KERNEL_BUILDER(Name("ZeroOut").Device(CPU).TypeConstraint<double>("T"), ZeroOutOp<double>)
REGISTER_KERNEL_BUILDER(Name("ZeroOut").Device(CPU).TypeConstraint<int>("T"),    ZeroOutOp<int>)
```

这里 `Attr("T: realnumbertype")` 声明 `T` 是一个**类型属性**（取值受限为「实数类型」集合），输入输出都用 `T` 标注，于是「同一 op、不同 dtype」在运行时由 `TypeConstraint` 选出对应的 kernel 实例化（u4-l2 讲过 `KernelAttrsMatch`）。

**属性校验**：

```text
REGISTER_OP("ZeroOut").Attr("preserve_index: int = 0")  // 带默认值的属性
// 构造期：
OP_REQUIRES_OK(ctx, ctx->GetAttr("preserve_index", &preserve_index_));  // 读属性
OP_REQUIRES(ctx, preserve_index_ >= 0, ...);                            // 校验属性本身
// Compute 里还需校验「属性相对当前输入是否合法」（下标不越界）
OP_REQUIRES(ctx, preserve_index_ < input.dimension(0), ...);
```

`GetAttr` 在 `OpKernelConstruction`（构造期，u4-l2）阶段读取属性；校验分两段：构造期判「属性本身合法」，`Compute` 期判「属性相对当前输入合法」。

**GPU kernel**：CPU 侧 `Compute` 不做循环，而是调用一个 `AddOneKernelLauncher` 函数；该 launcher 的实现在 `.cu.cc` 里，内部用 `GpuLaunchKernel` 启动 CUDA kernel。

#### 4.5.3 源码精读

**类型参数化**——声明、模板实现与批量注册：

[tensorflow/examples/adding_an_op/zero_out_op_kernel_2.cc:24-34](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_op_kernel_2.cc#L24-L34) —— `Attr("T: realnumbertype")` 把 `T` 声明为实数类型参数，输入输出均用 `T`；形状函数改用现成的 `shape_inference::UnchangedShape`（u4-l3 讲过的可复用形状函数库）。

[tensorflow/examples/adding_an_op/zero_out_op_kernel_2.cc:58-83](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_op_kernel_2.cc#L58-L83) —— `template <typename T> class ZeroOutOp`：把 `flat<int32_t>()` 换成 `flat<T>()`、`output(i) = T(0)`，于是同一份代码适用任意类型 `T`。

[tensorflow/examples/adding_an_op/zero_out_op_kernel_2.cc:85-96](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_op_kernel_2.cc#L85-L96) —— 为 `ZeroOut` 注册三种类型的 kernel：每个 `REGISTER_KERNEL_BUILDER` 都带 `TypeConstraint<具体类型>("T")`，运行时按实际 dtype 选其一。

[tensorflow/examples/adding_an_op/zero_out_op_kernel_2.cc:98-116](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_op_kernel_2.cc#L98-L116) —— 更省事的写法：用宏 `REGISTER_KERNEL(type)` 展开多条注册（`ZeroOut2`）；`ZeroOut3` 更进一步用 `TF_CALL_REAL_NUMBER_TYPES(REGISTER_KERNEL)` 一行展开所有实数类型，避免手写。

**属性校验**——构造期读属性 + 两段校验：

[tensorflow/examples/adding_an_op/zero_out_op_kernel_3.cc:22-29](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_op_kernel_3.cc#L22-L29) —— `Attr("preserve_index: int = 0")`：带默认值 0 的整型属性，调用方可不传。

[tensorflow/examples/adding_an_op/zero_out_op_kernel_3.cc:33-41](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_op_kernel_3.cc#L33-L41) —— 构造函数里 `GetAttr("preserve_index", &preserve_index_)` 读出属性，`OP_REQUIRES(..., preserve_index_ >= 0, ...)` 校验非负（失败即构造期报错）。

[tensorflow/examples/adding_an_op/zero_out_op_kernel_3.cc:49-50](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_op_kernel_3.cc#L49-L50) —— `Compute` 里再用 `OP_REQUIRES(..., preserve_index_ < input.dimension(0), ...)` 校验下标不越界。这是「运行期」校验：此时才看到真实输入尺寸。

**GPU kernel**——CPU 侧调度与设备侧计算分离：

[tensorflow/examples/adding_an_op/cuda_op_kernel.cc:33-55](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/cuda_op_kernel.cc#L33-L55) —— `AddOneOp`（`AddOne` op 的 GPU kernel）：`Compute` 不写循环，而是声明一个外部 launcher `void AddOneKernelLauncher(...)`（[第 31 行](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/cuda_op_kernel.cc#L31)）并调用它，把原始指针 `input.data()`/`output.data()` 交给设备 kernel；`REGISTER_KERNEL_BUILDER(Name("AddOne").Device(DEVICE_GPU), AddOneOp)` 指定 GPU 设备。

[tensorflow/examples/adding_an_op/cuda_op_kernel.cu.cc:16-34](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/cuda_op_kernel.cu.cc#L16-L34) —— launcher 的真正实现，整段包在 `#if GOOGLE_CUDA` 内（无 CUDA 时为空）：`AddOneKernel` 是 `__global__` CUDA kernel，用经典的 grid-stride loop 让每个线程处理若干元素 `out[i] = in[i] + 1`；`AddOneKernelLauncher` 用 `GpuLaunchKernel(AddOneKernel, 32, 256, 0, nullptr, in, N, out)` 以 32 个 block、每 block 256 线程启动它。

这种「CPU 壳 + 设备 launcher」是 TF 写 GPU op 的标准范式：CPU 侧负责 op 调度与张量搬运，真正的并行计算交给设备代码。

#### 4.5.4 代码实践（源码阅读型，无需编译）

**目标**：理解「属性校验分两段」的设计。

**步骤**：

1. 在 `zero_out_op_kernel_3.cc` 里区分两处 `OP_REQUIRES`：构造期的 `preserve_index_ >= 0`（[L38-40](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_op_kernel_3.cc#L38-L40)）与 `Compute` 期的 `preserve_index_ < input.dimension(0)`（[L49-50](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/zero_out_op_kernel_3.cc#L49-L50)）。
2. 思考：为什么不能把第二处校验也放进构造函数？

**预期结果**：构造函数在 op 被创建时执行一次，那时**还看不到具体的输入张量**（不知道 `input.dimension(0)` 是多少），所以「下标是否越界」只能在 `Compute` 里、拿到真实输入后才能判断；而「属性本身是否非负」与输入无关，可在构造期一次判定。

#### 4.5.5 小练习与答案

**练习 1**：`Attr("T: realnumbertype")` 中的 `realnumbertype` 起什么作用？

**答案**：它是一个**类型约束**，限定 `T` 只能取「实数类型」集合（int/float/double 等非复数数值类型）内的成员，防止用户传入不支持的类型（如 string）。

**练习 2**：为什么 GPU op 要把 `.cu.cc` 放进 `gpu_srcs` 而不是 `srcs`？

**答案**：`.cu.cc` 含 CUDA 专有语法（`__global__` 等），必须用 `nvcc` 编译，且只在 `GOOGLE_CUDA` 启用时编译；放进 `gpu_srcs` 让 `tf_custom_op_library` 用 `cuda_library` 规则单独处理它，并与 CPU 侧的 `srcs` 合并成同一个 `.so`。无 CUDA 环境时该文件被 `#if GOOGLE_CUDA` 整段跳过。

---

## 5. 综合实践

**任务**：参照 ZeroOut，从零设计并接通一个全新的自定义 op「**DoubleIt**」——输入一个 `int32` 张量，输出每个元素翻倍（`[1,2,3] → [2,4,6]`），并让它能参与训练。这个任务一次性走过本讲全部模块：写 op（4.1）、编译 `.so`（4.2）、加载进 Python（4.3）、配梯度（4.4）。

> 以下 C++ / Python / BUILD 片段均为**示例代码**（非项目原有文件），需自行创建文件；完整构建需 Bazel 从源码环境编译，**运行结果待本地验证**。

**第一步：C++ 三件套**（新建 `double_it_op_kernel.cc`，仿 `zero_out_op_kernel_1.cc` 骨架）：

```cpp
// 示例代码
#include "tensorflow/core/framework/op.h"
#include "tensorflow/core/framework/op_kernel.h"
#include "tensorflow/core/framework/shape_inference.h"

using namespace tensorflow;

REGISTER_OP("DoubleIt")
    .Input("to_double: int32")
    .Output("doubled: int32")
    .SetShapeFn([](shape_inference::InferenceContext* c) {
      c->set_output(0, c->input(0));   // 输出形状 = 输入形状
      return absl::OkStatus();
    });

class DoubleItOp : public OpKernel {
 public:
  explicit DoubleItOp(OpKernelConstruction* ctx) : OpKernel(ctx) {}
  void Compute(OpKernelContext* ctx) override {
    const Tensor& in = ctx->input(0);
    Tensor* out = nullptr;
    OP_REQUIRES_OK(ctx, ctx->allocate_output(0, in.shape(), &out));
    auto x = in.flat<int32_t>();
    auto y = out->flat<int32_t>();
    const int N = x.size();
    for (int i = 0; i < N; i++) y(i) = x(i) * 2;   // 关键计算
  }
};

REGISTER_KERNEL_BUILDER(Name("DoubleIt").Device(DEVICE_CPU), DoubleItOp);
```

**第二步：BUILD 目标**（新建 `BUILD`，仿 [adding_an_op/BUILD:32-35](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/examples/adding_an_op/BUILD#L32-L35)）：

```python
# 示例代码
load("//tensorflow:tensorflow.bzl", "tf_custom_op_library")
tf_custom_op_library(
    name = "double_it_op_kernel.so",
    srcs = ["double_it_op_kernel.cc"],
)
```

**第三步：Python 包装**（新建 `double_it_op.py`，仿 `zero_out_op_1.py`）：

```python
# 示例代码
import os.path
import tensorflow as tf

_module = tf.load_op_library(
    os.path.join(tf.compat.v1.resource_loader.get_data_files_path(),
                 'double_it_op_kernel.so'))
double_it = _module.double_it   # op 名 DoubleIt → 函数名 double_it
```

**第四步：梯度**（新建 `double_it_grad.py`，仿 `zero_out_grad_2.py`）。`DoubleIt` 的前向是 \(y = 2x\)，梯度为常数 2，故 \(\partial L/\partial x_i = 2 g_i\)：

```python
# 示例代码
from tensorflow.python.framework import ops
from tensorflow.python.ops import math_ops

@ops.RegisterGradient("DoubleIt")
def _double_it_grad(op, grad):
    # y = 2*x  ⇒  dy/dx = 2  ⇒  grad_x = 2 * grad
    return [math_ops.scalar_mul(2, grad)]   # 一个输入 → 返回单元素列表
```

**第五步：构建并验证**（待本地验证）：

```bash
# 在 TF 源码根目录
bazel build //path/to/your/pkg:double_it_op_kernel.so
# 运行（路径以实际 runfiles 为准）
python -c "
from your.pkg import double_it_op, double_it_grad  # import grad 触发梯度注册
import tensorflow as tf
print(double_it_op.double_it([1,2,3]))              # 期望 [2,4,6]
# 梯度校验（需 float）
theoretical, numerical = tf.test.compute_gradient(
    lambda x: double_it_op.double_it(tf.cast(x, tf.int32)),
    [tf.constant([1,2,3], dtype=tf.float32)])
print(theoretical, numerical)                        # 期望二者 assertAllClose
"
```

**串联要点（对照本讲）**：

- 三件套分别用了 `REGISTER_OP` / `OpKernel::Compute` / `REGISTER_KERNEL_BUILDER`（4.1）。
- `.so` 由 `tf_custom_op_library` 产出，`load_op_library` 在 `dlopen` 时触发注册并现场生成 `double_it`（4.2、4.3）。
- `import double_it_grad` 这一行「看似无用」的 import 触发 `@RegisterGradient`，使 op 可导（4.4）。
- 若把 `.cc` 改放进 `core/user_ops/` 并依赖 `alwayslink` 的 `user_ops_op_lib`，就能省掉 `load_op_library`、变成「启动即注册」（4.3 的静态链接路线）。

**预期结果（待本地验证）**：`double_it([1,2,3])` 应输出 `[2,4,6]`；`compute_gradient` 的解析梯度与数值梯度应 `assertAllClose`（因为 \(y=2x\) 处处可微且梯度恒为 2）。若 gradient 步骤报「No gradient defined」，说明第四步的 `@RegisterGradient` 模块没被 import（回顾 4.4.4）。

## 6. 本讲小结

- **自定义 op = 说明书 + 工人 + 挂接**：`REGISTER_OP` 声明输入/输出/属性/形状，`OpKernel::Compute` 实现计算，`REGISTER_KERNEL_BUILDER` 把工人挂到说明书并指定设备——三件套缺一不可（4.1）。
- **`.so` 是注册副作用的载体**：`tf_custom_op_library` 把 C++ 编译成动态库，`check_deps` 刻意禁止静态链接 framework，运行时把符号解析到已加载的 TF 进程，保持 `.so` 小巧且二进制兼容（4.2）。
- **`load_op_library` 是「副作用 + 现场代码生成」流水线**：dlopen 触发注册 → watcher 抓取本库新增的 OpList → `GetPythonWrappers` 即时生成 Python 包装 → `exec` 成模块；C++ 与 Python 两层缓存保证重复加载返回同一对象（4.3）。
- **两种进入进程的路径**：`adding_an_op` 走运行时动态加载（dlopen）；`core/user_ops` 走构建期静态链接（`alwayslink`），`import tensorflow` 后即用，无需 `load_op_library`（4.3）。
- **梯度要单独注册在 Python 侧**：`@tf.RegisterGradient` 把 grad_fn 存进全局梯度表，它是 import 副作用，必须确保被 import；不可微的 op 用 `tf.no_gradient` 显式声明；用 `tf.test.compute_gradient` 比对解析与数值梯度来验证（4.4）。
- **进阶变化各有套路**：`Attr("T:...")` + `template` + `TypeConstraint` 做类型参数化；`GetAttr` + 两段 `OP_REQUIRES` 做属性校验；CPU 壳 + `.cu.cc` launcher + `GpuLaunchKernel` 做 GPU kernel（4.5）。

## 7. 下一步学习建议

- **自动微分深入**：本讲只写了「最简单的 ZeroOut 梯度」。建议回到 u5-l1，研究 `gradients_util` 如何用 BFS 与 `_PendingCount` 把一串 grad_fn 串成完整反向图，并尝试为带控制流的 op 写梯度。
- **下一讲 u9-l2（AutoGraph）**：AutoGraph 会把 Python 的 `if/while/for` 控制流自动转换成等价的图 op；理解自定义 op 在 `tf.function` tracing 时的形状推导如何被消费（u4-l3 的 `SetShapeFn`），有助于写出与 `tf.function` 兼容良好的 op。
- **XLA 与自定义 op**：自定义 op 默认**不被 XLA 认识**（u7-l2/u7-l3），在 JIT 聚类时会被当作「不支持的 op」切断聚类。若希望自定义 op 参与 XLA 融合，需要额外实现 HLO lowering——可作为高阶扩展方向。
- **阅读更多示例**：`adding_an_op/README.md` 指向 `../custom_ops_doc` 有更多自定义 op 示例，可对照本讲建立的心智模型逐一拆解；`attr_examples.cc` 则演示了更丰富的属性类型用法。
