# 自定义 Op 全流程

## 1. 本讲目标

本讲是「扩展与二次开发」单元的第一讲。TensorFlow 内置了数千个 op，但当你需要一个**它没有提供的算子**（比如一种特殊的数值裁剪、一个定制化的硬件指令包装、一段用 CUDA 写的高性能 kernel）时，唯一的办法就是**自己写一个 op 并把它接进 TF 运行时**。

本讲以仓库自带的官方示例 `tensorflow/examples/adding_an_op/` 为主线，把「自定义 op」这件事从一头走到另一头。读完本讲你应当能够：

1. **写**：用 C++ 写出一个最小的自定义 op——声明它的输入输出与形状（`REGISTER_OP`），并实现它的计算（`OpKernel::Compute`）。
2. **编译并加载**：用 Bazel 把它编译成一个动态库 `.so`，再用 `tf.load_op_library` 把它加载进已经 `import tensorflow` 的 Python 进程，理解 dlopen 背后那段「动态注册」机制。
3. **配梯度**：用 `@tf.RegisterGradient` 为自定义 op 提供反向求导函数，让它能参与 `GradientTape` / `tf.gradients` 的训练。

本讲会把前几讲（u4-1 的 Op 注册、u4-2 的 OpKernel、u5-1 的自动微分）学到的一切，**串成一条可复现的工程链路**。

## 2. 前置知识

本讲依赖以下已建立的认知（不重复展开，只承接）：

- **Op 是声明，Kernel 是实现**（u4-1、u4-2）：`REGISTER_OP` 写一张「说明书」（`OpDef`：名字、输入、输出、属性、形状推导），`REGISTER_KERNEL_BUILDER` 给某个 op 在某个设备/类型上配一个「工人」（`OpKernel` 子类，实现 `Compute`）。运行时用 `OpKernelContext` 这条总线取输入、写输出、报错误。
- **自动微分靠 grad_fn**（u5-1）：反向模式 autodiff 对每个 op 查一张全局的「op 名 → 梯度函数」表；梯度函数的契约是 `(op, *上游梯度) → (*下游梯度)`。
- **op 到 Python 的最后一层是代码生成**（u4-5）：C++ 的 `REGISTER_OP` 是唯一真相源，由 `python_op_gen_main` 读出 `OpDef`、生成 `gen_*_ops.py`，之上再叠一层手写包装。

还需要一个底层概念：**动态链接库（`.so` / `.dll` / `.dylib`）**。它是一段编译好的机器码，可以在程序**已经运行起来之后**再加载进来，加载时操作系统会执行库内所有「静态全局变量」的初始化代码——这正是 TF 借以「在运行时把新 op 注入进程」的物理基础。

## 3. 本讲源码地图

本讲涉及两类「op 进入进程」的范例，先建立空间感：

| 文件 | 作用 | 所属模型 |
|------|------|----------|
| `tensorflow/examples/adding_an_op/zero_out_op_kernel_1.cc` | 最小自定义 op「ZeroOut」的 C++ 声明 + kernel 实现 | 动态加载 |
| `tensorflow/examples/adding_an_op/zero_out_op_1.py` | 用 `load_op_library` 加载 `.so` 的 Python 包装 | 动态加载 |
| `tensorflow/examples/adding_an_op/zero_out_grad_2.py` | 为 ZeroOut 提供梯度函数 `_zero_out_grad` | 动态加载 |
| `tensorflow/examples/adding_an_op/cuda_op_kernel.cc` / `.cu.cc` | GPU 版自定义 op「AddOne」 | 动态加载（GPU） |
| `tensorflow/examples/adding_an_op/BUILD` | 把上述 `.cc` 编译成 `.so` 的 Bazel 规则 | 构建 |
| `tensorflow/tensorflow.bzl`（`tf_custom_op_library` 宏） | 自定义 op 动态库的标准构建配方 | 构建 |
| `tensorflow/core/framework/load_library.cc` | C++ 侧 `LoadDynamicLibrary`：dlopen + 抓取 OpList | 运行时加载 |
| `tensorflow/c/c_api.cc`（`TF_LoadLibrary` / `TF_GetOpList`） | C API 对加载能力的封装 | 运行时加载 |
| `tensorflow/python/framework/load_library.py` | Python 侧 `load_op_library`：加载 + 现场生成包装 | 运行时加载 |
| `tensorflow/python/framework/ops.py`（`RegisterGradient`） | Python 侧梯度注册装饰器 | 梯度 |
| `tensorflow/core/user_ops/fact.cc` / `BUILD` | 「Fact」op：**构建期静态链接**进 TF 的对照例 | 静态链接 |
| `tensorflow/python/user_ops/user_ops.py` | Fact 在 Python 侧的手写包装 `my_fact` | 静态链接 |

一句话区分两个模型：`examples/adding_an_op` 的 op 需要**运行时手动 `load_op_library`**；`core/user_ops` 的 op 在**构建 TF 本体时就被链接进去**，`import tensorflow` 后立即可用。理解这条对照，就抓住了本讲的主轴。

## 4. 核心概念与源码讲解

### 4.1 写一个最小的 C++ Op：ZeroOut 与 Fact

#### 4.1.1 概念说明

「自定义 op」最朴素的定义是：**一段你自己写的 C++ 代码，它声明了一个 TF 不认识的新算子，并给出了这个算子的计算实现。**

我们全程围绕一个极简需求展开——**ZeroOut**：输入一个 `int32` 张量，输出一个同形状张量，除了**第一个元素**保留原值，其余全部置 0。例如 `[5,4,3,2,1]` → `[5,0,0,0,0]`。

写一个 op 需要两块拼图，它们来自 u4-1/u4-2，这里只是把它们组装起来：

1. **说明书**：`REGISTER_OP("ZeroOut")` —— 声明名字、输入 `to_zero: int32`、输出 `zeroed: int32`、形状推导（输出形状 = 输入形状）。
2. **工人**：一个继承 `OpKernel` 的类，实现 `Compute`，在其中读输入、分配输出、做计算。

与之并行的第二个例子是 `core/user_ops/fact.cc` 的 **Fact**：它**没有输入**，只输出一个标量字符串 `"0! == 1"`。Fact 的价值不在于计算多复杂，而在于展示「op 可以没有输入」以及「它走的是另一条进入进程的路径」（4.3 节展开）。

#### 4.1.2 核心流程

写一个最小 op 的固定四步：

```
1. #include 必要头文件（op.h / op_kernel.h / shape_inference.h）
2. REGISTER_OP("名字").Input(...).Output(...).SetShapeFn(...).Doc(...)
3. class XxxOp : public OpKernel { void Compute(ctx) override { ... } };
4. REGISTER_KERNEL_BUILDER(Name("名字").Device(DEVICE_CPU), XxxOp);
```

其中第 2 步是**声明**（不产生可执行计算），第 3 步是**实现**，第 4 步把实现**挂到**声明上并指定设备。

`Compute` 内部的固定骨架是「取输入 → 分配输出 → 填充输出」：

```
const Tensor& in = ctx->input(0);          // 取输入
Tensor* out = nullptr;
ctx->allocate_output(0, in.shape(), &out); // 分配输出（形状通常等于输入）
auto out_flat = out->flat<int32_t>();      // 取一维视图便于逐元素写
// ... 计算 ...
```

#### 4.1.3 源码精读

先看 ZeroOut 的完整声明（说明书）：

[zero_out_op_kernel_1.cc:22-34](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_op_kernel_1.cc#L22-L34) —— 注册 `ZeroOut`：声明输入 `to_zero: int32`、输出 `zeroed: int32`，并用 lambda 设形状函数「输出第 0 个的形状 = 输入第 0 个的形状」，最后附一段文档。

再看它的 kernel 实现（工人）：

[zero_out_op_kernel_1.cc:36-60](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_op_kernel_1.cc#L36-L60) —— `ZeroOutOp`：构造函数仅把 `context` 透传给基类 `OpKernel`；`Compute` 里先用 `context->input(0)` 取输入张量，用 `flat<int32_t>()` 取它的一维视图；用 `allocate_output(0, input_tensor.shape(), &output_tensor)` 分配同形状输出；先把除第一个外的元素全置 0，最后把 `input(0)` 拷到 `output(0)`。

注意一个细节：`OP_REQUIRES_OK(context, ...)`（u4-2 讲过）会把 `allocate_output` 可能返回的错误上报到 `OpKernelContext`，一旦失败立即中止本次 `Compute`。

把工人挂到说明书上并指定 CPU：

[zero_out_op_kernel_1.cc:62](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_op_kernel_1.cc#L62) —— `REGISTER_KERNEL_BUILDER(Name("ZeroOut").Device(DEVICE_CPU), ZeroOutOp)`：这一行说「名叫 ZeroOut 的 op，在 CPU 设备上，用 `ZeroOutOp` 这个类来实现」。

这个文件还演示了「带命名空间的 op 名」：

[zero_out_op_kernel_1.cc:64-96](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_op_kernel_1.cc#L64-L96) —— `REGISTER_OP("Namespace>ZeroOut")` 与 `Namespace>Nested>ZeroOut`：用 `>` 分隔的层次化名字，避免与全局 `ZeroOut` 重名。注意它们**复用同一个 `ZeroOutOp` 类**——说明书可以有多个（不同名字），工人只需写一次。

再看对照例 Fact（无输入、字符串输出）：

[fact.cc:27-29](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/core/user_ops/fact.cc#L27-L29) —— 注册 `Fact`：只有 `Output("fact: string")`，无 `Input`；形状函数用现成的 `shape_inference::UnknownShape`（一个标量字符串，形状信息无意义）。

[fact.cc:31-46](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/core/user_ops/fact.cc#L31-L46) —— `FactOp::Compute`：用 `allocate_output(0, TensorShape(), &output_tensor)` 分配一个**标量**（空 `TensorShape` 即 0 维）输出，取其 `scalar<tstring>()` 视图，写入字符串 `"0! == 1"`。

[fact.cc:48](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/core/user_ops/fact.cc#L48) —— 同样用 `REGISTER_KERNEL_BUILDER(Name("Fact").Device(DEVICE_CPU), FactOp)` 收尾。

> 结论：ZeroOut 与 Fact 在「写法」上完全同构——都是 `REGISTER_OP` + `OpKernel::Compute` + `REGISTER_KERNEL_BUILDER` 三件套。它们真正的区别在「如何进入进程」，那是 4.3 节的主题。

#### 4.1.4 代码实践（源码阅读型，无需编译）

**目标**：确认你已经能读懂一个自定义 op 的四件套，并能预测它的行为。

**步骤**：

1. 打开 `tensorflow/examples/adding_an_op/zero_out_op_kernel_1.cc`，定位 `REGISTER_OP("ZeroOut")` 与 `ZeroOutOp::Compute`。
2. 对照官方测试用例 [zero_out_1_test.py:26-28](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_1_test.py#L26-L28) 的断言 `assertAllEqual(result, [5, 0, 0, 0, 0])`。
3. 仅凭 `Compute` 的源码，手算 `zero_out([5,4,3,2,1])` 的结果。

**需要观察的现象 / 预期结果**：输入 `[5,4,3,2,1]`，`N=5`，循环把 `output(1..4)` 置 0，再把 `input(0)=5` 写到 `output(0)`，结果正是 `[5,0,0,0,0]`，与测试断言一致。若把输入换成 `[[6,5,4],[3,2,1]]`（2×3），由于 `flat<int32_t>()` 把它拍平成 6 元素一维视图，结果应是首元素 6 保留、其余 5 个为 0，即 `[[6,0,0],[0,0,0]]`（这正是 `zero_out_2_test.py` 的 `test_2d` 断言）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Compute` 里最后的 `if (N > 0) output(0) = input(0);` 删掉，`zero_out([5,4,3,2,1])` 会变成什么？
**答案**：会变成 `[0,0,0,0,0]`——因为前一个循环已经把所有元素（含下标 0）置 0，而保留首值的语句被删了。

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

```
你的 .cc (REGISTER_OP + OpKernel)
        │  tf_custom_op_library(name="xxx.so", srcs=[...])
        ▼
   ┌─────────────── Bazel 构建 ───────────────┐
   │ 1. 检查依赖：禁止静态链接 framework/lib   │  ← check_deps
   │ 2. 链接头文件库（framework_headers_lib） │
   │ 3. （可选）合并 GPU 源码 (.cu.cc)         │
   │ 4. 产出动态库 xxx.so                      │
   └────────────────────────────────────────────┘
        │
        ▼
   xxx.so （机器码 + 静态注册副作用）
```

这里有一个**反直觉但关键**的设计：`check_deps` 会**禁止**这个 `.so` 静态链接 `//tensorflow/core:framework` 和 `//tensorflow/core:lib`。为什么？因为这些符号（如 `OpRegistry::Global()`、`OpKernel` 的实现）**已经存在于正在运行的 TF 进程里**（那个巨大的 `_pywrap_tensorflow_internal.so`）。自定义 op 的 `.so` 只需要引用头文件去「声明」这些符号，运行时由操作系统把动态库符号解析到 TF 进程已有的实现上——这样你的 `.so` 才能保持很小，且与 TF 主版本二进制兼容。

#### 4.2.3 源码精读

先看调用方——`adding_an_op/BUILD` 如何把 `zero_out_op_kernel_1.cc` 编译成 `.so` 并配一个 Python 库：

[BUILD:32-43](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/BUILD#L32-L43) —— `tf_custom_op_library(name="zero_out_op_kernel_1.so", srcs=["zero_out_op_kernel_1.cc"])` 产出动态库；随后的 `py_library` 把它作为 `data` 打包，使 `.so` 跟着 Python 文件一起部署。

再看宏本身的实现——`tensorflow.bzl` 里的 `tf_custom_op_library`：

[tensorflow.bzl:2316-2354](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/tensorflow.bzl#L2316-L2354) —— 宏签名与依赖装配：把额外的框架头文件依赖（`tf_custom_op_library_additional_deps()`）、可选的 CUDA/ROCm 头文件都加进 `deps`。

[tensorflow.bzl:2368-2391](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/tensorflow.bzl#L2368-L2391) —— 关键收尾：`check_deps` 显式禁止链接 `framework`/`lib`；`tf_cc_shared_object` 把源码编译并链接成最终动态库（Windows 上用 `windows_export_all_symbols` 自动导出符号）。

对照「GPU op」的构建——`AddOne` 同时有 CPU 包装层和 CUDA kernel：

[BUILD:128-132](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/BUILD#L128-L132) —— `tf_custom_op_library(name="cuda_op_kernel.so", srcs=["cuda_op_kernel.cc"], gpu_srcs=["cuda_op_kernel.cu.cc"])`：`srcs` 是 Host（CPU 侧的 op 调度逻辑），`gpu_srcs` 是设备（GPU 侧真正的 CUDA kernel），宏内部用 `cuda_library` 把两者合并。

#### 4.2.4 代码实践（构建型，需 Bazel + ./configure）

**目标**：亲手把 ZeroOut 编译成 `.so` 并确认产物存在。

**步骤**：

1. 在仓库根目录运行 `./configure`（按 u1-3 完成 CPU/CUDA 探测）。
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
**答案**：`srcs`（如 `cuda_op_kernel.cc`）是 Host 侧的 `OpKernel::Compute`，负责取输入、调用 kernel launcher、写输出；`gpu_srcs`（如 `cuda_op_kernel.cu.cc`）是设备侧真正的 CUDA kernel 与它的 launcher 函数。

---

### 4.3 把 .so 加载进 Python：动态注册机制

> 这是本讲最核心、也最容易被忽略的一节。它回答了一个关键问题：**`load_op_library` 到底做了什么，才让一个全新的 op 在已经启动的 Python 进程里「凭空可用」？**

#### 4.3.1 概念说明

动态加载的本质是一条「**副作用 + 现场代码生成**」的流水线。`.so` 里并没有现成的 Python 函数 `zero_out`——它只有 C++ 的注册副作用和 kernel 机器码。`load_op_library` 的工作是：

1. **dlopen**：让操作系统加载 `.so`，触发其中的 C++ 静态全局变量初始化 → `REGISTER_OP`/`REGISTER_KERNEL_BUILDER` 把新 op/kernel 推入全局注册表的「待处理队列」。
2. **抓 OpList**：让 TF 把「本次新注册的 op 说明书」序列化成一个 `OpList` protobuf 缓冲区返回。
3. **现场生成 Python 包装**：调用和构建期 `gen_*_ops.py` **同一个**代码生成器（u4-5 讲过的 `python_op_gen`），把 `OpList` 即时翻译成 Python 源码字符串。
4. **exec 成模块**：把这段源码 `exec` 进一个新建的 Python 模块对象，于是 `module.zero_out` 就成了可调用函数。

注意：用户看到的 Python 名字（`zero_out`）是 op 名（`ZeroOut`）做 **CamelCase → snake_case** 转换得到的——这与 u4-5 完全一致。

#### 4.3.2 核心流程

```
Python: tf.load_op_library("zero_out_op_kernel_1.so")
   │
   ├─(1) py_tf.TF_LoadLibrary(path)
   │       └─ C++ LoadDynamicLibrary(path):
   │            a. ProcessRegistrations()  # 先处理之前积压的注册
   │            b. SetWatcher(λ)           # 装一个监听器，捕获「新注册」的 OpDef
   │            c. DeferRegistrations()    # 进入延迟模式
   │            d. dlopen(.so)             # 触发 .so 内的 REGISTER_OP 宏
   │            e. ProcessRegistrations()  # 处理 .so 带来的新注册 → 经 watcher 落入 library.op_list
   │            f. 序列化 op_list → 返回 buf/len
   │
   ├─(2) TF_GetOpList(handle)              # 取出刚刚序列化的 OpList 缓冲区
   │
   ├─(3) GetPythonWrappers(op_list)        # 现场生成 gen_*_ops 风格的 Python 源码字符串
   │
   └─(4) exec(wrappers, module.__dict__)   # 注入新模块 → module.zero_out 可用
```

其中 d 这一步的 dlopen 是物理基础；b/c/e 的 watcher+defer 机制是为了**精确区分「哪些 op 是这个 .so 新带来的」**（只把这些做成 OpList，避免和内置 op 混淆）。

#### 4.3.3 源码精读

**Python 入口**——`zero_out_op_1.py` 怎么把 `.so` 变成 `zero_out` 函数：

[zero_out_op_1.py:20-25](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_op_1.py#L20-L25) —— 用 `tf.compat.v1.resource_loader.get_data_files_path()` 定位与 `.py` 同目录的 `.so`，调 `tf.load_op_library` 得到模块对象，再把它的三个属性 `zero_out` / `namespace_zero_out` / `namespace_nested_zero_out` 导出到本模块命名空间（注意命名空间 op 名 `Namespace>ZeroOut` 被转成了 `namespace_zero_out`）。

**`load_op_library` 的实现**——加载 + 抓 OpList + 现场生成 + exec：

[load_library.py:54-74](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/python/framework/load_library.py#L54-L74) —— 先 `TF_LoadLibrary` 拿到库句柄；用 `TF_GetOpList(handle)` 取 OpList 缓冲区，交给 `_pywrap_python_op_gen.GetPythonWrappers` 生成 Python 包装源码；释放句柄；最后用 `wrappers` 内容的 sha1 作模块名，若该模块已在 `sys.modules` 则直接复用（这就是「重复加载返回同一对象」的原因），否则 `exec(wrappers, module.__dict__)` 注入并打上 `_IS_TENSORFLOW_PLUGIN` 标记（供 AutoGraph 识别）。

**C API 边界**——把加载能力暴露给 Python：

[c_api.cc:570-580](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/c/c_api.cc#L570-L580) —— `TF_LoadLibrary`：`new` 一个 `TF_Library`，调 `tensorflow::LoadDynamicLibrary` 把库句柄和 OpList 缓冲区填进去。

[c_api.cc:582](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/c/c_api.cc#L582) —— `TF_GetOpList`：直接返回句柄里存的 OpList 缓冲区。

**真正的核心**——`LoadDynamicLibrary` 的 watcher 机制：

[load_library.cc:46-102](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/core/framework/load_library.cc#L46-L102) —— 整个函数。注意开头的缓存表 `loaded_libs`：同一个 `.so` 第二次加载会直接返回缓存结果，不重复初始化（解释了下方练习里的「load twice 返回相同模块」）。

[load_library.cc:58-82](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/core/framework/load_library.cc#L58-L82) —— 关键序列：先 `ProcessRegistrations()` 处理旧积压；`SetWatcher` 装一个 lambda，每当有 OpDef 注册成功就把它 `add_op` 进 `library.op_list` 并记入 `seen_op_names`，若 `AlreadyExists` 但不是本库的 op 则视为「覆盖了同名内置 op、不算错误」；`DeferRegistrations()` 切到延迟模式；`env->LoadDynamicLibrary(...)` 即 dlopen，触发 `.so` 内 `REGISTER_OP` 静态初始化；再 `ProcessRegistrations()` 把新注册经 watcher 收进 `op_list`。

这段机制保证了：**返回给 Python 的 OpList 只含「这个 .so 新带来的 op」**，所以现场生成的 Python 包装也只针对你的自定义 op，不会与内置 op 重复。

**对照：静态链接模型（`core/user_ops`）**

Fact op 走的是另一条路——它在**构建 TF 本体时**就被链接进去：

[user_ops/BUILD:36-47](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/core/user_ops/BUILD#L36-L47) —— `cc_library(name="user_ops_op_lib", srcs=glob(['*.cc']), alwayslink=1, ...)`：用 `glob(['*.cc'])` 把 `user_ops/` 下**所有** `.cc`（含 `fact.cc`）收集进一个库，`alwayslink=1` 强制把其中的静态注册副作用链接进最终 TF 二进制。于是 `import tensorflow` 时这些 `REGISTER_OP` 就已执行，op 早已在全局注册表里——**无需 `load_op_library`**。

它的 Python 包装则在**构建期**用代码生成器一次性产出（u4-5 的同一条流水线，只是提前到编译期）：

[python/user_ops/BUILD:32-36](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/python/user_ops/BUILD#L32-L36) —— `tf_gen_op_wrapper_private_py(out="ops/gen_user_ops.py")`：构建期生成 `gen_user_ops.py`（提供 `fact()`）。

[user_ops.py:25-28](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/python/user_ops/user_ops.py#L25-L28) —— 手写包装 `my_fact()` 调 `_gen_user_ops.fact()`，并用 `@tf_export(v1=['user_ops.my_fact'])` 注册成 `tf.compat.v1.user_ops.my_fact`。这正是 `fact_test.py` 里 `tf.compat.v1.user_ops.my_fact()` 能直接调用的原因。

> 两种模型的本质对照：
>
> | 维度 | 动态加载（adding_an_op） | 静态链接（user_ops） |
> |------|--------------------------|----------------------|
> | 何时注册 | 运行时 `load_op_library` | 构建期链接进 TF 二进制 |
> | 何时生成 Python 包装 | 加载时**现场生成** | 构建期一次性生成 |
> | 是否需手动加载 | 是 | 否，`import tensorflow` 即可用 |
> | 适用场景 | 第三方插件、不想重编 TF | 随 TF 一同发布的内置扩展 |

#### 4.3.4 代码实践（源码阅读型，无需编译）

**目标**：验证「重复加载返回同一对象」这一缓存行为，并理解它由 C++ 缓存表保证。

**步骤**：

1. 阅读 [load_library.cc:48-56](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/core/framework/load_library.cc#L48-L56)，确认 `loaded_libs` 是一个以文件名为键的静态映射，命中即返回缓存的 `library`（含已序列化的 `op_list`）。
2. 阅读 Python 侧缓存 [load_library.py:64-66](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/python/framework/load_library.py#L64-L66)：模块名由 `wrappers` 的 sha1 决定，若已在 `sys.modules` 则直接返回。
3. 对照测试 [zero_out_1_test.py:43-47](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_1_test.py#L43-L47) 的 `test_load_twice`：第二次 `load_op_library` 得到的对象 `assertEqual` 于第一次。

**预期结果**：两次加载拿到完全相同的模块对象——这正是 C++ 与 Python 两层缓存共同保证的。即便不重新执行注册副作用，op 也早已在首次加载时进入全局注册表，故行为不变。

#### 4.3.5 小练习与答案

**练习 1**：如果两个不同的 `.so` 里都 `REGISTER_OP("ZeroOut")`，第二次加载会发生什么？
**答案**：第二个 `.so` 加载时，其 `ZeroOut` 会与全局已有的同名 op 冲突（`AlreadyExists`）。由于该名字不在「本库 seen_op_names」内，`watcher` 会把它当作「覆盖同名内置 op」**静默跳过**而不报错（见 load_library.cc:65-70），即第二次的同名 op 不会被注册、不生效。

**练习 2**：为什么 `load_op_library` 返回的 OpList 只含「本库新增的 op」，而不是全局所有 op？
**答案**：因为 watcher 只在 dlopen 触发的那次 `ProcessRegistrations` 期间捕获新注册的 OpDef；之前已注册的内置 op 不会再次经过 watcher。这样现场生成的 Python 包装才精确对应你的自定义 op。

---

### 4.4 为自定义 Op 提供梯度：RegisterGradient

#### 4.4.1 概念说明

写完 `Compute`，你的 op 已经能**前向计算**了。但若想把它放进一个需要**训练**的模型（用 `GradientTape` 或 `tf.gradients` 求导），它还缺一样东西：**梯度函数**。

回顾 u5-1：反向模式 autodiff 为每个 op 查一张「op 名 → grad_fn」表。内置 op 的 grad_fn 在 C++/Python 里早已注册；而你的自定义 op 是全新的，**默认没有 grad_fn**——一旦反向传播走到它，就会报「没有注册梯度」的错误。

解决办法有两条：

- 提供 grad_fn：用 `@tf.RegisterGradient("ZeroOut")` 注册一个梯度函数（**推荐**，表示这个 op 可微）。
- 声明不可微：用 `tf.no_gradient("ZeroOut")` 告诉 TF「这个 op 故意不参与求导」，反向传播到它会传播 0 而不报错（适用于 `tf.size` 这类本就无梯度的 op）。

注意一个常被混淆的点：**梯度的注册在 Python 侧完成**（`ops.RegisterGradient` → `gradient_registry.register`），而 op/kernel 的注册在 C++ 侧（`REGISTER_OP`/`REGISTER_KERNEL_BUILDER`）。两者是分开的两张表。

#### 4.4.2 核心流程

梯度函数的契约（u5-1 已确立）：

```
grad_fn(op, *grads_wrt_outputs) -> [*grads_wrt_inputs]
```

即：拿到原始 op、以及「损失对每个输出的梯度」，返回「损失对每个输入的梯度」。对 ZeroOut（1 输入 1 输出）而言就是 `_zero_out_grad(op, grad) -> [to_zero_grad]`。

先推导 ZeroOut 的梯度。设输入 `x`，输出 `y = ZeroOut(x)`，满足：

\[
y_0 = x_0,\qquad y_i = 0\ (i>0)
\]

雅可比矩阵 \(J\) 满足 \(J_{j,i} = \partial y_j/\partial x_i\)，只有 \(J_{0,0}=1\)，其余为 0。对标量损失 \(L\)，记上游梯度 \(g_j = \partial L/\partial y_j\)，则输入梯度为：

\[
\frac{\partial L}{\partial x_i} = \sum_j g_j\,J_{j,i} = g_0 \cdot \delta_{i,0}
\]

也就是：**输入梯度只有第 0 个位置等于上游梯度的第 0 个值，其余全为 0**。

#### 4.4.3 源码精读

ZeroOut 的梯度实现：

[zero_out_grad_2.py:23-40](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_grad_2.py#L23-L40) —— `@ops.RegisterGradient("ZeroOut")` 装饰 `_zero_out_grad(op, grad)`：从 `op.inputs[0]` 拿原输入 `to_zero`；算其形状 `shape` 与全零下标 `index = zeros_like(shape)`（即 `[0,0,...]`，指向首元素）；把上游梯度拍平取首值 `first_grad = reshape(grad, [-1])[0]`；用 `sparse_to_dense([index], shape, first_grad, 0)` 造一个「仅在首位置放 `first_grad`、其余为 0」的稠密张量作为输入梯度，包成单元素列表返回（因为有 1 个输入）。

这段代码**完全对应**上面的数学结论：输入梯度仅在首位置为 \(g_0\)，其余为 0。

注册装饰器的实现：

[ops.py:1756-1800](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/python/framework/ops.py#L1756-L1800) —— `RegisterGradient` 类：构造时记下 `op_type` 字符串；`__call__` 时调 `gradient_registry.register(f, self._op_type)` 把函数存进全局梯度注册表。文档清楚说明了 m 输入 n 输出 op 的梯度函数签名约定。

声明「不可微」的接口：

[ops.py:1803-1818](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/python/framework/ops.py#L1803-L1818) —— `tf.no_gradient(op_type)`：把某 op 标记为不参与求导，反向传播到它会传播 0 而非报错。注意它**不应**用于「有梯度但还没写」的 op——后者应留空，让 TF 在被求导时**报错**提醒你补上。

如何验证梯度正确？官方测试用 `tf.test.compute_gradient` 同时算「解析梯度（你的 grad_fn）」与「数值梯度（有限差分）」并比对：

[zero_out_2_test.py:35-39](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_2_test.py#L35-L39) —— `test_grad`：对 `zero_out` 调 `tf.test.compute_gradient`，断言 `theoretical`（来自你的 grad_fn）与 `numerical`（数值近似）`assertAllClose`。这是检验自定义梯度的标准范式。

> 一个易错点：`zero_out_2_test` 测的是 `zero_out_op_2`（带类型参数化的版本，见 4.5），且测试文件顶部 `import zero_out_grad_2`——**梯度注册是导入时的副作用**，必须确保定义梯度的模块被 import 进来，`@RegisterGradient` 才会执行。`# pylint: disable=unused-import` 注释正是为此（看起来没用，实则触发注册）。

#### 4.4.4 代码实践（源码阅读型，无需编译）

**目标**：理解「梯度注册靠 import 副作用」这一关键事实。

**步骤**：

1. 阅读 [zero_out_2_test.py:21](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_2_test.py#L21)，注意 `from ... import zero_out_grad_2  # pylint: disable=unused-import`。
2. 思考：若删掉这一行 import，`test_grad` 会发生什么？

**预期结果**：删掉后，`@ops.RegisterGradient("ZeroOut")` 不会被执行，全局梯度表里没有 `ZeroOut` 的条目。当 `tf.test.compute_gradient` 反向求导到 `zero_out` 时，TF 会抛出「No gradient defined for op: ZeroOut」之类的错误。这正是 import 副用的意义——**保证梯度函数被注册**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 ZeroOut 的输入梯度「只在首位置非零」？
**答案**：因为前向 `y_0=x_0, y_i=0`，雅可比仅 \(J_{0,0}=1\)，故 \(\partial L/\partial x_i = g_0\delta_{i,0}\)，只有 \(i=0\) 处等于 \(g_0\)，其余为 0。

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

```
REGISTER_OP("ZeroOut").Attr("T: realnumbertype").Input("to_zero: T").Output("zeroed: T")  // T 是类型参数
template <typename T> class ZeroOutOp : public OpKernel { ... input.flat<T>() ... }       // 一个模板类
REGISTER_KERNEL_BUILDER(Name("ZeroOut").Device(CPU).TypeConstraint<float>("T"),  ZeroOutOp<float>)  // 每种类型注册一次
REGISTER_KERNEL_BUILDER(Name("ZeroOut").Device(CPU).TypeConstraint<double>("T"), ZeroOutOp<double>)
REGISTER_KERNEL_BUILDER(Name("ZeroOut").Device(CPU).TypeConstraint<int>("T"),    ZeroOutOp<int>)
```

这里 `Attr("T: realnumbertype")` 声明 `T` 是一个**类型属性**（取值受限为「实数类型」集合），输入输出都用 `T` 标注，于是「同一 op、不同 dtype」在运行时由 `TypeConstraint` 选出对应的 kernel 实例化（u4-2 讲过 `KernelAttrsMatch`）。

**属性校验**：

```
REGISTER_OP("ZeroOut").Attr("preserve_index: int = 0")  // 带默认值的属性
// 构造期：
OP_REQUIRES_OK(ctx, ctx->GetAttr("preserve_index", &preserve_index_));  // 读属性
OP_REQUIRES(ctx, preserve_index_ >= 0, ...);                            // 校验
// Compute 里再用 preserve_index_ 决定保留哪个元素
```

`GetAttr` 在 `OpKernelConstruction`（构造期，u4-2 讲过）阶段读取属性；`OP_REQUIRES` 在校验失败时把错误上报并中止。

**GPU kernel**：CPU 侧 `Compute` 不做循环，而是调用一个 `AddOneKernelLauncher` 函数指针；该 launcher 的实现在 `.cu.cc` 里，内部用 `GpuLaunchKernel` 启动 CUDA kernel。

#### 4.5.3 源码精读

**类型参数化**——声明与模板实现：

[zero_out_op_kernel_2.cc:22-34](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_op_kernel_2.cc#L22-L34) —— `Attr("T: realnumbertype")` 把 `T` 声明为实数类型参数，输入输出均用 `T`；形状函数改用现成的 `shape_inference::UnchangedShape`。

[zero_out_op_kernel_2.cc:58-83](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_op_kernel_2.cc#L58-L83) —— `template <typename T> class ZeroOutOp`：把 `flat<int32_t>()` 换成 `flat<T>()`，于是同一份代码适用任意类型 `T`。

[zero_out_op_kernel_2.cc:85-96](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_op_kernel_2.cc#L85-L96) —— 为 `ZeroOut` 注册三种类型的 kernel：每个 `REGISTER_KERNEL_BUILDER` 都带 `TypeConstraint<具体类型>("T")`，运行时按实际 dtype 选其一。

[zero_out_op_kernel_2.cc:98-114](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_op_kernel_2.cc#L98-L114) —— 更省事的写法：用宏 `REGISTER_KERNEL(type)` 展开多条注册；`ZeroOut3` 更进一步用 `TF_CALL_REAL_NUMBER_TYPES(REGISTER_KERNEL)` 一行展开所有实数类型，避免手写。

**属性校验**——构造期读属性 + 校验：

[zero_out_op_kernel_3.cc:22-29](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_op_kernel_3.cc#L22-L29) —— `Attr("preserve_index: int = 0")`：带默认值 0 的整型属性，调用方可不传。

[zero_out_op_kernel_3.cc:33-41](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_op_kernel_3.cc#L33-L41) —— 构造函数里 `GetAttr("preserve_index", &preserve_index_)` 读出属性，`OP_REQUIRES(..., preserve_index_ >= 0, ...)` 校验非负（失败即构造期报错）。注意构造期校验的是「属性本身合法」，`Compute` 里还需校验「属性相对当前输入是否合法」（如下标不越界，见 [zero_out_op_kernel_3.cc:49-50](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_op_kernel_3.cc#L49-L50)）。

**GPU kernel**——CPU 侧调度 + 设备侧计算分离：

[cuda_op_kernel.cc:31-55](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/cuda_op_kernel.cc#L31-L55) —— `AddOneOp`（`AddOne` op 的 GPU kernel）：`Compute` 不写循环，而是声明一个外部 launcher `AddOneKernelLauncher(input.data(), N, output.data())` 并调用它；注意 `REGISTER_KERNEL_BUILDER(Name("AddOne").Device(DEVICE_GPU), AddOneOp)` 指定 GPU 设备。

[cuda_op_kernel.cu.cc:22-32](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/cuda_op_kernel.cu.cc#L22-L32) —— launcher 的真正实现：`AddOneKernel` 是 `__global__` CUDA kernel（每个线程处理若干元素 `out[i]=in[i]+1`），`AddOneKernelLauncher` 用 `GpuLaunchKernel` 以 `32` 个 block、`256` 个线程启动它。整个 `.cu.cc` 包在 `#if GOOGLE_CUDA` 内，仅启用 CUDA 时才编译。

这种「CPU 壳 + 设备 launcher」是 TF 写 GPU op 的标准范式：CPU 侧负责 op 调度与张量搬运，真正的并行计算交给设备代码。

#### 4.5.4 代码实践（源码阅读型，无需编译）

**目标**：理解「属性校验分两段」的设计。

**步骤**：

1. 在 `zero_out_op_kernel_3.cc` 里区分两处 `OP_REQUIRES`：构造期的 `preserve_index_ >= 0`（[L38-40](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_op_kernel_3.cc#L38-L40)）与 `Compute` 期的 `preserve_index_ < input.dimension(0)`（[L49-50](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_op_kernel_3.cc#L49-L50)）。
2. 思考：为什么不能把第二处校验也放进构造函数？

**预期结果**：构造函数在 op 被创建时执行一次，那时**还看不到具体的输入张量**（不知道 `input.dimension(0)` 是多少），所以「下标是否越界」只能在 `Compute` 里、拿到真实输入后才能判断；而「属性本身是否非负」与输入无关，可在构造期一次判定。

#### 4.5.5 小练习与答案

**练习 1**：`Attr("T: realnumbertype")` 中的 `realnumbertype` 起什么作用？
**答案**：它是一个**类型约束**，限定 `T` 只能取「实数类型」集合（int/float/double 等非复数数值类型）内的成员，防止用户传入不支持的类型（如 string）。

**练习 2**：为什么 GPU op 要把 `.cu.cc` 放进 `gpu_srcs` 而不是 `srcs`？
**答案**：`.cu.cc` 含 CUDA 专有语法（`__global__` 等），必须用 `nvcc` 编译，且只在 `GOOGLE_CUDA` 启用时编译；放进 `gpu_srcs` 让 `tf_custom_op_library` 用 `cuda_library` 规则单独处理它，并与 CPU 侧的 `srcs` 合并成同一个 `.so`。无 CUDA 环境时该文件被 `#if GOOGLE_CUDA` 整段跳过。

---

## 5. 综合实践

**任务**：参照 ZeroOut，从零设计并接通一个全新的自定义 op「**Double**」——输入一个 `int32` 张量，输出每个元素翻倍（`[1,2,3] → [2,4,6]`），并让它能参与训练。

请按全流程交付以下五件产物（前两件能在本仓库 `adding_an_op/` 目录里照搬现有模式；若不具备 Bazel 构建环境，前三件写成「设计稿」并标注待本地验证即可）：

1. **C++ kernel**（仿 `zero_out_op_kernel_1.cc`）：
   - `REGISTER_OP("Double").Input("x: int32").Output("y: int32").SetShapeFn(输出=输入)`。
   - `class DoubleOp : public OpKernel`，`Compute` 里 `output(i) = input(i) * 2`。
   - `REGISTER_KERNEL_BUILDER(Name("Double").Device(DEVICE_CPU), DoubleOp)`。

2. **BUILD 目标**（仿 [BUILD:32-35](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/BUILD#L32-L35)）：新增 `tf_custom_op_library(name="double_op_kernel.so", srcs=["double_op_kernel.cc"])` 与对应 `py_library`。

3. **Python 包装**（仿 `zero_out_op_1.py`）：`_double_module = tf.load_op_library(...)`，`double = _double_module.double`。

4. **梯度**（仿 `zero_out_grad_2.py`）：先用数学推导——`y = 2x`，故 \(\partial y/\partial x = 2\)，输入梯度应为 `2 * grad`。写成：
   ```python
   @ops.RegisterGradient("Double")
   def _double_grad(op, grad):
       return [grad * 2]   # 一个输入 → 返回单元素列表
   ```

5. **验证**：构建后写一个测试，仿 [zero_out_2_test.py:35-39](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/examples/adding_an_op/zero_out_2_test.py#L35-L39) 用 `tf.test.compute_gradient(double, (x,))` 比对解析梯度（你的 `2*grad`）与数值梯度。

**串联要点**：这个任务一次性走过了本讲全部五个模块——写 op（4.1）、编译 `.so`（4.2）、加载进 Python（4.3）、配梯度（4.4），并可选地把类型参数化（4.5，把 `int32` 升级成 `Attr("T: realnumbertype")`）。完成后，你就掌握了一条从「C++ 一行代码」到「Python 里可训练的 `tf.*` 函数」的完整工程链路。

**预期结果（待本地验证）**：`double([1,2,3])` 应输出 `[2,4,6]`；`compute_gradient` 的解析梯度与数值梯度应 `assertAllClose`（因为 `y=2x` 处处可微且梯度恒为 2）。若 gradient 步骤报「No gradient defined」，说明第 4 步的 `@RegisterGradient` 模块没被 import（回顾 4.4.4）。

## 6. 本讲小结

- **自定义 op = 说明书 + 工人**：`REGISTER_OP` 声明输入/输出/属性/形状，`OpKernel::Compute` 实现计算，`REGISTER_KERNEL_BUILDER` 把工人挂到说明书并指定设备——三件套缺一不可（4.1）。
- **`.so` 是注册副作用的载体**：`tf_custom_op_library` 把 C++ 编译成动态库，刻意不静态链接 framework，运行时把符号解析到已加载的 TF 进程（4.2）。
- **`load_op_library` 是一条「副作用 + 现场代码生成」流水线**：dlopen 触发注册 → watcher 抓取本库新增的 OpList → `GetPythonWrappers` 即时生成 Python 包装 → `exec` 成模块（4.3）。
- **两种进入进程的路径**：`adding_an_op` 走运行时动态加载；`core/user_ops` 走构建期静态链接（`alwayslink`），`import tensorflow` 后即用（4.3）。
- **梯度要单独注册在 Python 侧**：`@tf.RegisterGradient` 把 grad_fn 存进全局梯度表；它是 import 副作用，必须确保被 import；不可微的 op 用 `tf.no_gradient` 显式声明（4.4）。
- **进阶变化各有套路**：`Attr("T:...")` + `template` + `TypeConstraint` 做类型参数化；`GetAttr` + 两段 `OP_REQUIRES` 做属性校验；CPU 壳 + `.cu.cc` launcher 做_GPU kernel（4.5）。

## 7. 下一步学习建议

- **自动微分深入**：本讲只写了「最简单的线性梯度」。建议回到 u5-1，研究 `gradients_util` 如何用 BFS 把一串 grad_fn 串成完整反向图，并尝试为带控制流的 op 写梯度。
- **AutoGraph 与 op 的关系**：u9-2（AutoGraph）会把 Python 控制流转成图 op；理解自定义 op 在 `tf.function` tracing 时的形状推导如何被消费（u4-3 的 `SetShapeFn`），有助于写出与 `tf.function` 兼容良好的 op。
- **XLA 与自定义 op**：自定义 op 默认**不被 XLA 认识**（u7-2/u7-3），在 JIT 聚类时会被当作「不支持的 op」切断聚类。若希望自定义 op 参与 XLA 融合，需要额外实现 HLO lowering——可作为高阶扩展方向。
- **阅读更多示例**：仓库 README 指向 `../custom_ops_doc` 有更多自定义 op 示例，可对照本讲建立的心智模型逐一拆解。
