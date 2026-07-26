# TFLite 架构与 Interpreter

> 本讲进入「边缘部署 TFLite」单元。前面七个单元我们一直在桌面端/服务器端打转：从张量、计算图、Op/Kernel 注册，一直到 DirectSession 的多设备执行调度。本讲要换一个视角——当一个 TensorFlow 模型被打包成一个 `.tflite` 文件、塞进一部手机或一块嵌入式板子时，它由谁来执行？答案就是 **TensorFlow Lite**。

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说清楚 TFLite **为什么**要被单独设计、它放弃了桌面端的哪些东西、又换来了什么（轻、快、省内存）。
2. 画出 TFLite 推理的「四件套」架构：`FlatBufferModel` → `InterpreterBuilder` + `OpResolver` → `Interpreter` → `AllocateTensors` / `Invoke`。
3. 精读 [interpreter.h](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h)，掌握 Interpreter 的执行流程与生命周期。
4. 看懂 TFLite 的 C 边界：[common.h](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h) 里的 `TfLiteTensor`/`TfLiteNode`/`TfLiteContext`/`TfLiteRegistration` 如何定义运行时与算子之间的契约；以及 [c_api.h](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/c_api.h) 作为「不透明指针」稳定 ABI 边界的意义。
5. 能把 TFLite 的解释执行模型与桌面端 `DirectSession` 的执行模型对照起来，指出二者在图表示、设备、算子分发、内存上的本质差别。

## 2. 前置知识

本讲依赖前面几讲已建立的概念，下面用一句话帮你回忆，不会重复展开：

- **桌面端执行模型（u1-l5、u3-l2）**：用户拿到一张 `GraphDef`，通过 `NewSession()` 创建 `DirectSession`；`Run` 时要做 **Placement（放置）→ Pruning（剪枝）→ Optimize（Grappler 优化）→ Partition（分区）**，为每台设备产出一个 `Executor`，跨设备靠 `_Send`/`_Recv` + `Rendezvous` 传张量。TFLite 几乎把这些全砍掉了——本讲会逐条对比。
- **Op 与 Kernel（u4-l1、u4-l2）**：桌面端 Op 是「说明书」（`OpDef`），Kernel 是「工人」（`OpKernel::Compute(OpKernelContext*)`），靠全局 `OpRegistry`/`KernelRegistry` 注册。TFLite 用一张显式传入的 `OpResolver` 查找表替代全局注册表，用 C 函数指针 `TfLiteRegistration::invoke` 替代 C++ 虚函数。
- **FlatBuffer**：TFLite 的模型文件不是 protobuf，而是 Google 的 FlatBuffer 序列化格式。它的杀手锏是 **零拷贝（zero-copy）**——可以直接 mmap 到内存，无需反序列化即可按偏移读取，这对内存紧张的移动端至关重要。与之对照，protobuf 必须先反序列化成一棵对象树。

如果你对「解释执行 vs 编译执行」这个区分陌生，记住一句话即可：TFLite 默认是 **解释器（interpreter）**——读一条算一条，按 `execution_plan` 数组里的顺序依次调用每个算子的 C 函数；而桌面端 XLA/TFRT 走的是 **编译/调度** 路线。第 7 单元讲过编译器，这里我们看它的「轻量兄弟」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tensorflow/lite/core/interpreter.h` | **本讲主角**。定义 `tflite::Interpreter`（实为 `impl::Interpreter` 的别名），是驱动整张图推理的顶层对象。 |
| `tensorflow/lite/c/common.h` | 公共头，仅 `#include` 下一层实现。真正的类型定义在 `tensorflow/lite/core/c/common.h`。 |
| `tensorflow/lite/c/c_api.h` | 公共头，仅 `#include` 下一层实现。真正的 C API 声明在 `tensorflow/lite/core/c/c_api.h`。 |
| `tensorflow/lite/core/c/common.h` | **运行时↔算子的 C 契约**：`TfLiteStatus`、`TfLiteTensor`、`TfLiteNode`、`TfLiteContext`、`TfLiteRegistration`、`TfLiteDelegate`。 |
| `tensorflow/lite/core/c/c_api.h` | **稳定 ABI 的 C API**：`TfLiteModel`/`TfLiteInterpreter`/`TfLiteTensor` 等不透明类型及其生命周期函数。 |
| `tensorflow/lite/core/c/c_api_types.h` | `TfLiteStatus` 枚举、不透明类型 `TfLiteOpaqueContext` 等的前向声明。 |
| `tensorflow/compiler/mlir/lite/core/model_builder_base.h` | `FlatBufferModelBase`：把 `.tflite` 文件 mmap 成内存表示的 `FlatBufferModel`。 |
| `tensorflow/lite/core/interpreter_builder.h` | `InterpreterBuilder`：吃 `FlatBufferModel` + `OpResolver`，把 FlatBuffer 里的算子解析、装配成一个 `Interpreter`。 |
| `tensorflow/lite/core/api/op_resolver.h` | `OpResolver` 抽象：把 FlatBuffer 里的 op code 映射到 `TfLiteRegistration`（算子实现）。 |
| `tensorflow/lite/examples/minimal/minimal.cc` | 最小可运行示例，串起「加载→构建→分配→推理」全流程，是本讲代码实践的样板。 |

> **关于头文件分层**：你会注意到 `lite/c/common.h`、`lite/c/c_api.h`、`lite/model_builder.h` 这些文件内容都只有一两行 `#include`。这是 TFLite 的约定——`lite/c/`、`lite/` 是面向用户的 **公共 include 路径**，`lite/core/c/`、`lite/core/` 才是 **实现路径**。用户文档里反复出现的 `WARNING: Users of TensorFlow Lite should not include this file directly` 说的就是这个。本讲为了讲清实现，会直接引用 `core/` 下的真实定义。

## 4. 核心概念与源码讲解

### 4.1 TFLite 的轻量化定位与「四件套」推理架构

#### 4.1.1 概念说明

TFLite 解决的问题是：**把一个在云端训练好的模型，塞进一部只有几 MB 内存余量、没有独立 GPU 调度栈、甚至没有完整 C++ 标准库的手机或微控制器里去推理。** 为此它必须做出取舍：

- **去掉多设备**：移动端推理只在单设备（CPU 或某块加速器）上跑，于是桌面端那一整套 Placement、Partition、`_Send`/`_Recv`、`Rendezvous` 全部不需要。
- **去掉图调度器**：不再用异步 `Executor`，而是用一张扁平的 **执行计划（execution_plan）** 数组，按依赖顺序一个一个调用算子。
- **去掉全局注册表**：桌面端依赖进程级 `OpRegistry`，而 TFLite 要求把可用的算子实现 **显式** 地通过 `OpResolver` 传进来——这样可以按需裁剪二进制体积（只链接你用得到的算子）。
- **去掉按需分配**：改用 **arena（内存竞技场）+ 静态 memory planner**，把所有张量预先规划进一两块连续内存里；权重甚至直接 mmap，连拷贝都省了。

代价是：TFLite **默认不训练**（它有「从训练好的模型继续训练」的能力，但主战场是推理），且单进程单设备，不擅长分布式。

#### 4.1.2 核心流程

TFLite 的推理由 **四个角色** 协作完成，这正是 [interpreter.h](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h) 顶部那段 `Usage` 注释所描述的：

```
┌───────────────────────────┐
│  ① FlatBufferModel        │  把 .tflite 文件 mmap 成只读内存模型
│  （只读、可被多 Interpreter│  （权重零拷贝，常量权重直接指向文件映射区）
│     共享）                 │
└─────────────┬─────────────┘
              │ + OpResolver（算子查找表）
              ▼
┌───────────────────────────┐
│  ② InterpreterBuilder     │  解析 FlatBuffer 里的算子/张量，
│  （一次性装配）            │  把 op code 解析成 TfLiteRegistration，
│                           │  装配出一个 Interpreter
└─────────────┬─────────────┘
              ▼
┌───────────────────────────┐
│  ③ Interpreter            │  AllocateTensors()：做内存规划，给所有
│  （常驻、可反复 Invoke）   │  张量分配 arena 缓冲；填输入 →
│                           │  Invoke()：按 execution_plan 顺序跑每个
│                           │  算子的 invoke() → 读输出
└───────────────────────────┘
```

用伪代码表示一次完整推理：

```text
model     = FlatBufferModel::BuildFromFile("model.tflite")
resolver  = BuiltinOpResolver()              # 内置算子实现表
builder   = InterpreterBuilder(*model, resolver)
interpreter = builder()                      # 装配
interpreter.AllocateTensors()                # 内存规划（贵，只做一次）
填输入: typed_input_tensor<float>(0)[i] = ...
interpreter.Invoke()                         # 按计划遍历算子
读输出: typed_output_tensor<float>(0)[i]
```

注意一个与桌面端的重要差异：桌面端的 `Session.run()` 每次都会 **按 fetches 重新剪枝子图**；而 TFLite 的 `AllocateTensors()` 是 **昂贵的一次性操作**，之后 `Invoke()` 就是廉价的纯遍历。如果输入形状变了（动态 shape），才需要重新 `ResizeInputTensor` + `AllocateTensors`。

#### 4.1.3 源码精读

先看主角 [interpreter.h](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h) 顶部的使用示例，它就是上面那张图的文字版：

- [tensorflow/lite/core/interpreter.h:87-122](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h#L87-L122) —— 这段文档注释给出标准用法：先 `FlatBufferModel::BuildFromFile`，再 `InterpreterBuilder(*model, resolver)(&interpreter)`，再 `AllocateTensors()`，填输入，最后 `Invoke()`。这是全篇的纲领。

模型的加载由 `FlatBufferModelBase` 负责，它本质是 FlatBuffer 的一个 RAII 只读包装：

- [tensorflow/compiler/mlir/lite/core/model_builder_base.h:60-68](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/lite/core/model_builder_base.h#L60-L68) —— 注释说明 `FlatBufferModelBase` 是「从磁盘拷贝或 mmap 的只读 tflite 模型」，并强调它 **不可变（immutable）**，因此一个模型可被多个 Interpreter 共享（甚至跨线程）。

- [tensorflow/compiler/mlir/lite/core/model_builder_base.h:94-105](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/lite/core/model_builder_base.h#L94-L105) —— `BuildFromFile` 把文件经 `GetAllocationFromFile` 转成 `Allocation`（mmap 或读入内存），再 `BuildFromAllocation`。注意大端机器还要 `ByteConvertModel` 做字节序转换（移动端几乎都是小端，故直接返回）。

装配过程在 `InterpreterBuilder` 里：

- [tensorflow/lite/core/interpreter_builder.h:98-102](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter_builder.h#L98-L102) —— `operator()` 是构建入口：成功返回 `kTfLiteOk` 并把 `*interpreter` 置为有效对象，失败置为 `nullptr`。这就是示例里 `builder(&interpreter)` 那一行的真身。

- [tensorflow/lite/core/interpreter_builder.h:133-139](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter_builder.h#L133-L139) —— `ParseNodes` / `ParseTensors` 是构建期的两个核心私有方法，分别把 FlatBuffer 里的算子和张量翻译成 `Subgraph` 里的节点与 `TfLiteTensor`。

算子的查找表定义在 `OpResolver`：

- [tensorflow/lite/core/api/op_resolver.h:55-60](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/api/op_resolver.h#L55-L60) —— `FindOp` 有两个重载：一个按 builtin op 枚举码（如 `ADD`）、一个按自定义 op 名字字符串查；二者都要带 `version`（算子版本）。这取代了桌面端的进程级全局 `OpRegistry`。

- [tensorflow/lite/core/api/op_resolver.h:218-221](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/api/op_resolver.h#L218-L221) —— `GetRegistrationFromOpCode` 是把 FlatBuffer 里的 `OperatorCode` 翻译成 `TfLiteRegistration`（算子实现）的桥梁函数，构建期每个节点都要查一次。

#### 4.1.4 代码实践

**实践目标**：用最小可运行示例 [minimal.cc](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/examples/minimal/minimal.cc) 把「四件套」流程在源码里逐行对上号。

**操作步骤**（源码阅读型实践，无需编译）：

1. 打开 `tensorflow/lite/examples/minimal/minimal.cc`。
2. 在 `main` 里找到这四步，并把每一行与 4.1.2 的流程图对应：
   - `FlatBufferModel::BuildFromFile(filename)` —— ①
   - `BuiltinOpResolver resolver; InterpreterBuilder builder(*model, resolver);` —— ②
   - `builder(&interpreter)` —— 得到 ③
   - `interpreter->AllocateTensors()` 与 `interpreter->Invoke()` —— ③ 的运行期
3. 参考源码：[tensorflow/lite/examples/minimal/minimal.cc:49-74](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/examples/minimal/minimal.cc#L49-L74)。

**需要观察的现象**：你会看到 `AllocateTensors()` 之后示例调用了 `PrintInterpreterState` 打印状态，`Invoke()` 之后再打印一次——这正是观察「分配前/分配后/推理后」三态的官方手段。

**预期结果**：你能写出一个表格，左列是 minimal.cc 的代码行，右列对应四件套中的哪一步。

**待本地验证**：若你想真正运行，minimal 示例自带极简 Makefile（`tensorflow/lite/tools/make`），可 `make` 出一个 `minimal` 二进制并用任意 `.tflite` 文件运行；但本实践以源码阅读为主。

#### 4.1.5 小练习与答案

**练习 1**：为什么 TFLite 推荐用 `InterpreterBuilder` 而不是直接 `new Interpreter()`？

**参考答案**：直接构造的 `Interpreter` 是一张空图——没有节点、没有张量、没有算子实现。`InterpreterBuilder` 才负责把 FlatBuffer 里的算子用 `OpResolver` 解析成 `TfLiteRegistration`、把张量描述装配进 `Subgraph`。`interpreter.h` 的构造函数注释就明确写了 `Use of this constructor outside of an InterpreterBuilder is not recommended`。

**练习 2**：一个 `.tflite` 模型能否被多个 `Interpreter` 共享？为什么？

**参考答案**：能。因为 `FlatBufferModel` 是不可变的只读对象（权重经 mmap 直接指向文件映射区），`InterpreterBuilder` 的文档（见 [interpreter_builder.h:50-70](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter_builder.h#L50-L70)）明确说「a single model instance may safely be used with multiple interpreters」。这与桌面端「一份 GraphDef 可喂给多个 Session」是类似的设计，只是 TFLite 凭借 mmap 连拷贝都省了。

---

### 4.2 lite.core.interpreter —— Interpreter 的解释执行与生命周期

#### 4.2.1 概念说明

本模块精读 `lite.core.interpreter`，也就是 [interpreter.h](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h) 里的 `Interpreter` 类。它是 TFLite 的「主控对象」，但需要先纠正一个直觉：**Interpreter 自己并不真正存张量、也不真正遍历算子**。

真正干活的是一个叫 **`Subgraph`（子图）** 的对象。一个 Interpreter 持有一个 `std::vector<unique_ptr<Subgraph>>`，第 0 个就是 **主子图（primary subgraph）**。我们平时调用的 `AllocateTensors()`、`Invoke()`、`inputs()`、`tensor(i)`，Interpreter 几乎都是转手交给 `primary_subgraph()` 去做。引入多子图是为了支持控制流（`while`/`if`）和「签名（signature）」——每个子图可以有自己的输入输出。

Interpreter 真正自己持有的核心状态只有一个 C 结构体指针 `TfLiteContext* context_`。这个 `TfLiteContext` 是运行时和算子之间通信的「总线」——算子实现（一个 C 函数指针）就是通过它来读写张量、请求分配内存、上报错误的。我们会在 4.3 专门讲它。

#### 4.2.2 核心流程

Interpreter 的生命周期可以分成 **构建期** 和 **运行期** 两段：

```text
【构建期】（由 InterpreterBuilder 驱动，Interpreter 被动接收）
  构造空 Interpreter  →  Builder 调 AddTensors/AddNodeWithParameters/SetInputs/SetOutputs
                       把模型装配进 Subgraph  →  Builder 返回

【运行期】（用户主动调用）
  (可选) ResizeInputTensor  —— 改输入形状（会令图"不一致"，需重新分配）
        AllocateTensors     —— 内存规划：按 execution_plan 跑一遍每个算子的 prepare()，
                                算出输出形状，再用 memory planner 把所有张量布局进 arena
  填输入张量缓冲
        Invoke              —— 按 execution_plan 顺序，对每个节点调用其 invoke() 函数指针
  读输出张量缓冲
        (可反复 Invoke，只要输入形状不变)
```

关键点：

1. **`AllocateTensors()` 是分水岭**。它内部会触发每个算子的 `prepare` 回调来推导形状、规划内存。只有它返回 `kTfLiteOk` 之后，张量缓冲才真正可用。文档反复强调：访问张量数据前 **必须** 先 `AllocateTensors()`；改了输入形状后 **必须** 再次调用。
2. **`Invoke()` 是廉价且可重复的**。它只是遍历一遍已经规划好的执行计划，逐个调用算子的 `invoke`。
3. **执行顺序由 `execution_plan()` 决定**，它是一个 `vector<int>`——节点索引的有序列表，已经做过拓扑排序，直接按数组下标遍历即可，无需桌面端那种复杂的图调度。

#### 4.2.3 源码精读

先确认类型别名与类的真实身份：

- [tensorflow/lite/core/interpreter.h:122-128](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h#L122-L128) —— `using Interpreter = impl::Interpreter;` 这一行说明用户用的 `tflite::Interpreter` 其实是命名空间 `impl` 里的那个类。前面的 `#include` 注释也提醒：**不要直接 include 这个文件**，应 include `lite/interpreter.h`。

注意它的线程安全声明，这是与桌面端 `Executor` 异步模型的鲜明对比：

- [tensorflow/lite/core/interpreter.h:120-121](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h#L120-L121) —— 明确写着 `This class is *not* thread-safe`，客户端要自己串行化对同一个 Interpreter 的访问。桌面端是异步多线程图调度，TFLite 默认是同步单线程（并行交给 delegate，见下一讲 u8-l3）。

两个最关键的运行期方法：

- [tensorflow/lite/core/interpreter.h:574-582](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h#L574-L582) —— `AllocateTensors()` 的声明。注释说明它「相对昂贵」，**必须在创建后、推理前调用**，且**当且仅当**改了输入张量形状时才需再次调用；若模型里有算子不被 `OpResolver` 支持（且未被 delegate 改写），它会失败。

- [tensorflow/lite/core/interpreter.h:584-590](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h#L584-L590) —— `Invoke()` 的声明，「按依赖顺序跑完整张图」。注释提醒：若之前做了 `ResizeTensor` 却没 `AllocateTensors`，Interpreter 可能不在就绪态。

现在看「Interpreter 只是转交给主子图」的证据：

- [tensorflow/lite/core/interpreter.h:248-264](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h#L248-L264) —— `inputs()`/`outputs()`/`variables()` 全都直接 `return primary_subgraph().inputs()` 等，自身不存数据。

- [tensorflow/lite/core/interpreter.h:874-881](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h#L874-L881) —— `primary_subgraph()` 返回 `*subgraphs_.front()`，注释还贴心地注明 `subgraphs_ always has 1 entry`，保证取首元素安全。

最后看 Interpreter 真正持有的核心状态：

- [tensorflow/lite/core/interpreter.h:1009-1013](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h#L1009-L1013) —— `TfLiteContext* context_`，注释写得很清楚：这是「与纯 C 插件接口通信的纯 C 数据结构」，而且为了避免拷贝张量元数据，**它也是张量的权威存储**。注意它存的是「主子图的 context」。

- [tensorflow/lite/core/interpreter.h:1043-1043](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h#L1043-L1043) —— `std::vector<std::unique_ptr<Subgraph>> subgraphs_`，这就是多子图机制的存储。

填输入、读输出最常用的便捷方法：

- [tensorflow/lite/core/interpreter.h:325-345](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h#L325-L345) —— 模板方法 `typed_tensor<T>(idx)`：先取张量、再校验 `tensor->type` 与 `T` 匹配，匹配才把 `data.raw` reinterpret 成 `T*`，否则返回 `nullptr`。这是一个带类型检查的安全转型，`typed_input_tensor`/`typed_output_tensor` 都建立在它之上。

> 本讲的目的是建立架构认知，所以不深入 `Subgraph::AllocateTensors`/`Invoke` 的实现细节（那涉及 `memory_planner` 与算子 `prepare`/`invoke` 的交互）。你只需记住：Interpreter 是门面，Subgraph 是引擎，`TfLiteContext` 是总线。

#### 4.2.4 代码实践

**实践目标**：通过阅读 [interpreter.h](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h) 的公有方法，复原一个最小推理程序的 API 序列，并验证「Interpreter 转交主子图」这一论断。

**操作步骤**：

1. 在 `interpreter.h` 中找到这些方法并抄下它们的签名与文档前一句：构造函数、`AllocateTensors`、`Invoke`、`inputs`、`typed_input_tensor`、`ResizeInputTensor`、`ResetVariableTensors`。
2. 对 `inputs()`、`tensor(int)`、`node_and_registration(int)` 三个方法，确认它们的方法体是否都形如 `return primary_subgraph().xxx(...)`，统计有多少个公有方法是「纯转发」。
3. 找到私有成员 `context_`，阅读它的注释，回答：为什么 TFLite 选择用纯 C 的 `TfLiteContext` 而不是 C++ 类来存张量？

**需要观察的现象**：你会看到大量「函数体只有一行、调用 `primary_subgraph()`」的公有方法，这印证了「Interpreter 是门面，Subgraph 是引擎」。

**预期结果**：你能用 5~8 行 C++ 代码写出（伪代码即可）一个不含错误处理的完整推理骨架：构造 → `AllocateTensors` → `typed_input_tensor` 填值 → `Invoke` → `typed_output_tensor` 读值。

**待本地验证**：纯源码阅读即可完成；若要编译验证，需 Bazel 构建 `//tensorflow/lite/examples/minimal:minimal`。

#### 4.2.5 小练习与答案

**练习 1**：以下两段代码，哪段会在运行期出错？为什么？
```cpp
// A
builder(&interpreter);
interpreter->Invoke();
```
```cpp
// B
builder(&interpreter);
interpreter->AllocateTensors();
interpreter->Invoke();
```

**参考答案**：A 会出问题（很可能段错误或返回 `kTfLiteError`）。因为 `AllocateTensors()` 还没调用，张量缓冲尚未分配，`Invoke` 时算子读到的输入/输出指针无效。`Invoke` 的文档（[interpreter.h:584-590](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h#L584-L590)）就提示「interpreter 可能不在就绪态」。

**练习 2**：一个模型用了 `while` 控制流，Interpreter 里会有几个 `Subgraph`？

**参考答案**：至少 2 个：主子图（`subgraphs_[0]`）加上为循环体单独建的子图。控制流的每个分支/循环体在 TFLite 里会被编译成独立子图，运行时由控制流算子（`While`/`If`）在子图之间切换调用。这也是 `subgraphs_size()`、`subgraph(int)` 这些方法存在的理由。

**练习 3**：`AllocateTensors()` 内部为什么需要调用每个算子的 `prepare` 回调？

**参考答案**：因为输出形状往往依赖输入形状，只有当输入形状确定后，才能逐个算子推导（`prepare` 里算子声明自己的输出形状、申请持久缓冲），memory planner 才知道每个张量有多大、该在 arena 里怎么排布。这与桌面端「形状推导在建图期做」不同——TFLite 把它推迟到了 `AllocateTensors`，以支持动态形状。

---

### 4.3 lite.c.common 与 C API —— 运行时与算子之间的 C 边界

#### 4.3.1 概念说明

本模块覆盖两个「C 边界」，它们位于不同层次，容易混淆，先分清楚：

1. **内部 C 契约（`common.h`）**：定义 **运行时（Interpreter/Subgraph）** 与 **算子实现/委托** 之间怎么对话。核心是 `TfLiteContext`（运行时给算子的工具箱）、`TfLiteTensor`（张量）、`TfLiteNode`（一次算子调用）、`TfLiteRegistration`（一个算子的实现 = 一组 C 函数指针）。这些结构体 **不是** 不透明的——算子作者会直接读写它们的字段。`common.h` 的开头注释就说得很直白：「the interface between the interpreter and the operations are C」。

2. **外部稳定 ABI（`c_api.h`）**：定义 **应用程序** 与 **运行时库** 之间的边界。这里全部是 **不透明指针（opaque pointer）**——`TfLiteModel`、`TfLiteInterpreter`、`TfLiteTensor` 都只是 `typedef struct` 的前向声明，用户只能通过 `TfLiteXxxCreate/Delete/...` 一族函数操作，看不到内部字段。好处是 **ABI 稳定**：运行时 `.so` 内部怎么改，只要这些函数签名不变，调用方就不用重编。这正是「TF Lite in Play Services」能独立升级运行时而无需重新打包 App 的基础。

> 联想 u4-l4 讲过的桌面端 C API（`TF_Graph`/`TF_Session`/`TF_SessionRun` 也是不透明指针风格）。TFLite 的 `c_api.h` 是同一哲学在移动端的翻版，但更精简。

此外，`common.h` 还定义了贯穿全系统的 `TfLiteStatus`（不过这个枚举实际声明在被 `#include` 的 `c_api_types.h` 里）。

#### 4.3.2 核心流程

**算子如何被调用（内部契约视角）**：

```text
Invoke() 遍历 execution_plan:
  for node_index in plan:
      node, registration = context->GetNodeAndRegistration(node_index)
      # node 是 TfLiteNode（含 inputs/outputs 的张量索引数组、user_data 等）
      # registration 是 TfLiteRegistration（含 init/prepare/invoke 函数指针）
      registration->invoke(context, node)   # ← 算子的真正计算
```

- 运行时通过 `TfLiteContext` 把「张量数组」「执行计划」「内存分配能力」暴露给算子；
- 算子通过 `TfLiteNode->inputs/outputs`（`TfLiteIntArray*`，张量索引）拿到自己要读写哪些张量；
- 算子再用 `context->GetTensor(ctx, idx)` 取到具体的 `TfLiteTensor`，对其 `data` 联合体读写真实数值。

**外部 C API 的对象生命周期（ABI 视角）**，对应 [c_api.h](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/c_api.h) 顶部那段 Usage：

```text
TfLiteModel* model = TfLiteModelCreateFromFile(path);    // 创建不透明模型
options = TfLiteInterpreterOptionsCreate();
TfLiteInterpreter* interp = TfLiteInterpreterCreate(model, options);  // 创建解释器
TfLiteInterpreterAllocateTensors(interp);
input = TfLiteInterpreterGetInputTensor(interp, 0);
TfLiteTensorCopyFromBuffer(input, data, bytes);          // 填输入
TfLiteInterpreterInvoke(interp);                         // 推理
output = TfLiteInterpreterGetOutputTensor(interp, 0);
TfLiteTensorCopyToBuffer(output, buf, bytes);            // 读输出
TfLiteInterpreterDelete(interp);                         // 全部 Delete 释放
```

它与 C++ API 一一对应，只是把对象换成不透明指针、把方法换成 `TfLiteXxxYyy` 函数。

#### 4.3.3 源码精读

**先看公共 shim 头如何转发到实现**——理解了这层就理解了 TFLite 的头文件分层：

- [tensorflow/lite/c/common.h:28-33](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/c/common.h#L28-L33) —— 公共 `lite/c/common.h` 全部内容就是 `#include "tensorflow/lite/core/c/common.h"`。

- [tensorflow/lite/c/c_api.h:24-26](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/c/c_api.h#L24-L26) —— 公共 `lite/c/c_api.h` 同样只 `#include "tensorflow/lite/core/c/c_api.h"`，末尾还附了一段 `TfLiteRegistrationExternal → TfLiteOperator` 的向后兼容别名。

**状态码 `TfLiteStatus`**（实际在 `c_api_types.h`）：

- [tensorflow/lite/core/c/c_api_types.h:74-120](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/c_api_types.h#L74-L120) —— `kTfLiteOk=0` 之外，还有 `kTfLiteDelegateError`、`kTfLiteApplicationError`、`kTfLiteUnresolvedOps`、`kTfLiteCancelled` 等。注意注释提醒「未来可能新增更细的状态值，应用不要依赖枚举值是固定集合」——这是 ABI 谨慎设计的体现。

**张量 `TfLiteTensor`**（运行时存储张量的权威结构）：

- [tensorflow/lite/core/c/common.h:548-619](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L548-L619) —— 字段包括 `type`（`TfLiteType`）、`data`（`TfLitePtrUnion` 指针联合体）、`dims`（`TfLiteIntArray*` 形状）、`allocation_type`（内存来源）、`bytes`、`name`、`quantization`、`is_variable` 等。对照桌面端的 `Tensor`（C++ 类、由 `TensorBuffer` 持数据），这里是一个 **扁平的 C struct**，直接持有裸指针——为的是省内存、省间接跳转。
  - `bytes` 字段的注释给出计算公式：\( \text{bytes} = \text{sizeof}(\text{元素类型}) \times \prod_i \text{dims}[i] \)。

- [tensorflow/lite/core/c/common.h:410-435](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L410-L435) —— `TfLitePtrUnion`：一个指针联合体，按 `type` 取 `.f`/`.i32`/`.int8`/`.data`(void*) 等。注释提醒优先用 `GetTensorData<T>(tensor)` 而非直接访问成员。

**形状数组 `TfLiteIntArray`**——一个 C 风格的「柔性数组」技巧：

- [tensorflow/lite/core/c/common.h:110-128](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L110-L128) —— `int size; int data[];`，即「长度 + 内联数据」一次 `malloc`，避免二次间接寻址。`dims`、节点的 `inputs/outputs` 都用它。这是移动端省内存、省指针跳转的典型手法。

**节点 `TfLiteNode`**（一次算子调用的上下文）：

- [tensorflow/lite/core/c/common.h:621-661](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L621-L661) —— 含 `inputs/outputs/intermediates/temporaries`（都是 `TfLiteIntArray*`，即张量索引数组）、`user_data`（算子在 `init` 里返回的私有状态）、`builtin_data`（内置算子的参数，如卷积的 stride）、`delegate`、`might_have_side_effect`。注意它 **只描述连通关系与私有数据，不含算子类型**——算子类型在 `TfLiteRegistration` 里。

**算子实现 `TfLiteRegistration`**（一个算子 = 一组 C 函数指针）：

- [tensorflow/lite/core/c/common.h:1184-1281](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L1184-L1281) —— 这是本模块最重要的一段。结构体里挂了四个关键函数指针：
  - [common.h:1210-1210](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L1210-L1210) `init`：每个算子节点 **只调用一次**，反序列化参数、做一次性分配，返回 `user_data`；
  - [common.h:1222-1222](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L1222-L1222) `prepare`：输入形状变化时调用，算子据此声明输出形状、申请持久缓冲（对应 `AllocateTensors`）；
  - [common.h:1228-1228](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L1228-L1228) `invoke`：真正的计算，读 `node->inputs` 写 `node->outputs`（对应 `Invoke`）；
  - 还有 `free`、`profiling_string`、`builtin_code`、`custom_name`、`version` 等。

  这就是桌面端 `OpKernel::Compute` 的 TFLite 等价物，只不过从 C++ 虚函数换成了 C 函数指针，从而可以纯 C 编译、跨语言、跨 ABI。

**运行时总线 `TfLiteContext`**：

- [tensorflow/lite/core/c/common.h:858-871](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L858-L871) —— 文档说它是「由 TF Lite 运行时创建、传给算子函数指针的结构体」，给算子提供张量访问、内存分配、错误上报等能力。

- [tensorflow/lite/core/c/common.h:871-923](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L871-L923) —— 字段 `tensors_size`、`tensors`（张量数组首地址）、`impl_`（指向 C++ `Subgraph` 的不透明指针）、以及一堆函数指针：`ResizeTensor`、`ReportError`、`AddTensors`、`GetNodeAndRegistration`、`AllocatePersistentBuffer`、`RequestScratchBufferInArena`、`GetTensor`、`GetEvalTensor` 等。换句话说，算子要动运行时的任何东西，都得通过 `context` 上的函数指针「走正门」。

**错误检查宏**——算子里最常见的写法：

- [tensorflow/lite/core/c/common.h:228-235](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L228-L235) —— `TF_LITE_ENSURE(context, a)`：条件不成立就记日志并 `return kTfLiteError`。它取代了桌面端的 `OP_REQUIRES`，是 TFLite 算子里「自给自足的错误检查」（见文件顶部摘要）。

**外部 C API 的不透明类型**（[c_api.h](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/c_api.h)）：

- [tensorflow/lite/core/c/c_api.h:108-119](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/c_api.h#L108-L119) —— `TfLiteModel`、`TfLiteInterpreterOptions`、`TfLiteInterpreter`、`TfLiteTensor` 全是 `typedef struct Xxx Xxx;` 的前向声明——字段不可见，这就是「不透明」。

- [tensorflow/lite/core/c/c_api.h:47-77](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/c_api.h#L47-L77) —— 顶部 Usage 给出完整生命周期，与 4.3.2 的伪代码一致。

- [tensorflow/lite/core/c/c_api.h:201-202](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/c_api.h#L201-L202) —— `TfLiteModelCreateFromFile`：从文件路径创建不透明模型。

- [tensorflow/lite/core/c/c_api.h:312-313](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/c_api.h#L312-L313) —— `TfLiteInterpreterCreate(model, options)`：创建解释器，对应 C++ 的 `InterpreterBuilder(...)(&interpreter)`。

- [tensorflow/lite/core/c/c_api.h:362-363](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/c_api.h#L362-L363) 与 [395-396](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/c_api.h#L395-L396) —— `TfLiteInterpreterAllocateTensors` 与 `TfLiteInterpreterInvoke`，与 C++ 方法同名同义。

- [tensorflow/lite/core/c/c_api.h:640-647](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/c_api.h#L640-L647) —— `TfLiteTensorCopyFromBuffer` / `TfLiteTensorCopyToBuffer`：填输入/读输出的标准手段，要求 `size == TfLiteTensorByteSize(tensor)`。注意 C API 提倡 **拷贝**（而非像 C++ API 那样直接拿 `typed_input_tensor` 裸指针），是因为不透明边界下用户拿不到稳定裸指针。

#### 4.3.4 代码实践

**实践目标**：通过对照「C++ API」与「C API」两套等价调用，体会「不透明指针」边界的设计。

**操作步骤**：

1. 在 [c_api.h](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/c_api.h) 里找出与下列 C++ 调用一一对应的 C 函数，填入下表（左列 C++，右列写 C 函数名）：

   | C++ API | 对应 C API 函数 |
   |---|---|
   | `FlatBufferModel::BuildFromFile` | ? |
   | `InterpreterBuilder(...)(&interp)` | ? |
   | `interpreter->AllocateTensors()` | ? |
   | `interpreter->typed_input_tensor<float>(0)` 写入 | ?（提示：填输入用什么） |
   | `interpreter->Invoke()` | ? |
   | `interpreter->typed_output_tensor<float>(0)` 读取 | ? |

2. 阅读 [common.h:1184-1281](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L1184-L1281) 的 `TfLiteRegistration`，回答：桌面端 `OpKernel` 子类要 override 的 `Compute`，在 TFLite 里对应哪个函数指针？这个改变带来什么好处？

**需要观察的现象**：C API 几乎是 C++ API 的「一一翻译」，但所有对象都换成了不透明指针，所有方法都换成了全局函数。

**预期结果**：你能给出填好的表格，并说出「不透明指针 + 全局函数」让运行时库 `.so` 可以独立升级而不破坏调用方。

**待本地验证**：纯源码阅读即可完成，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`TfLiteTensor` 和桌面端的 `tensorflow::Tensor` 在「如何持有数据」上有什么本质区别？

**参考答案**：桌面端 `Tensor` 是 C++ 对象，内部通过引用计数的 `TensorBuffer` 间接持有数据，有分配器抽象；`TfLiteTensor` 是一个 **扁平 C struct**，直接用 `TfLitePtrUnion data` 这个裸指针联合体指向缓冲（`allocation_type` 标明来源：mmap 的权重、arena 分配的临时区、动态分配等）。扁平结构省去了虚函数与间接跳转，是面向移动端体积与缓存友好的设计。

**练习 2**：`TfLiteRegistration` 里的 `init` 和 `prepare` 都能分配内存，它们的区别是什么？

**参考答案**：`init` 在节点生命周期内 **只调一次**，适合与张量尺寸无关的一次性分配（解析参数）；它分配的是「持久缓冲」，可用 `context->AllocatePersistentBuffer`。`prepare` 在 **输入形状变化时** 被调用（即每次 `AllocateTensors` 相关流程），用于声明输出形状、按当前形状做布局相关准备。简单说：`init` 管「不变的」，`prepare` 管「随形状变的」。

**练习 3**：为什么 `c_api.h` 提倡用 `TfLiteTensorCopyFromBuffer` 拷贝数据，而 C++ API 直接给 `typed_input_tensor` 裸指针？

**参考答案**：因为 C API 把 `TfLiteTensor` 做成不透明，用户拿不到稳定的内部缓冲地址（且文档多处警告 `Invoke`/`AllocateTensors` 等操作可能使指针失效），所以提供「按字节拷贝」的函数作为唯一安全的数据通路。C++ API 用户在同一编译单元内，可以接受「指针不稳、用完即弃」的契约，故直接给裸指针更高效。

## 5. 综合实践

**任务**：把本讲三节串起来——对照 [interpreter.h](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter.h)，说明一个 TFLite 模型从加载到推理经历的关键步骤，并把它与桌面端 `Session` 执行（u3-l2）逐项对比。

**步骤**：

1. **画出 TFLite 推理时序**。以 [minimal.cc:49-74](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/examples/minimal/minimal.cc#L49-L74) 为准，列出 6 个阶段（加载模型、装配 Interpreter、分配张量、填输入、Invoke、读输出），并为每个阶段注明调用的方法名、发生在「构建期」还是「运行期」。

2. **填对比表**。按下表，对比 TFLite Interpreter 与桌面端 `DirectSession`：

   | 维度 | 桌面端 DirectSession（u3-l2） | TFLite Interpreter（本讲） |
   |---|---|---|
   | 图表示 | ? | 扁平 `execution_plan`（节点索引数组） |
   | 设备 | 多设备，需 Placement/Partition | ? |
   | 每次执行的剪枝 | 按 fetches 重新剪枝 | ? |
   | 算子分发 | `OpKernel::Compute`（C++ 虚函数） | ? |
   | 跨设备数据传递 | ? | 无（单设备） |
   | 内存 | BFCAllocator 等按需分配 | ? |
   | 模型格式 | ? | FlatBuffer（mmap 零拷贝） |
   | 算子注册 | 全局 OpRegistry/KernelRegistry | ? |
   | 线程模型 | 异步 Executor | ? |

3. **回答一个理解题**：为什么 TFLite 把「形状推导 + 内存规划」推迟到 `AllocateTensors()`，而不是像桌面端那样在建图期就完成？请结合「移动端需要支持动态输入形状」与「`prepare` 回调」来回答（参考 4.2.5 练习 3）。

**预期结果**：你得到一份填满的时序图与对比表。表的关键答案（自测用）：图表示=扁平执行计划；设备=单设备无放置；剪枝=一次性规划好整图、Invoke 不再剪枝；算子分发=`TfLiteRegistration::invoke` C 函数指针；跨设备=桌面端 `_Send`/`_Recv`+Rendezvous；内存=arena+静态 memory planner；模型格式=桌面端 GraphDef/SavedModel(protobuf)；注册=`OpResolver` 显式查找表；线程=同步、单 Interpreter 非线程安全（并行靠 delegate）。

**待本地验证**：本实践为源码阅读型，全部可在阅读源码后完成；若要实跑，需用 Bazel 构建 minimal 示例并准备一个 `.tflite` 文件。

## 6. 本讲小结

- TFLite 是为移动/嵌入式 **单设备推理** 设计的轻量运行时，主动放弃了桌面端的多设备放置、图分区、异步 Executor、全局注册表，换来小体积、低内存、mmap 零拷贝。
- 推理由「四件套」协作：`FlatBufferModel`（mmap 只读模型）→ `InterpreterBuilder` + `OpResolver`（解析算子、装配）→ `Interpreter`（`AllocateTensors` 规划内存 → `Invoke` 按计划遍历算子）→ 读输出。
- `Interpreter` 是 **门面**，真正存张量、遍历算子的是它持有的 `Subgraph`；大量公有方法只是转发给 `primary_subgraph()`。Interpreter 自身的核心状态是纯 C 的 `TfLiteContext* context_`。
- `AllocateTensors()` 是分水岭（昂贵、按需重做），`Invoke()` 是廉价可重复的纯遍历；二者之间填输入、之后读输出。
- `common.h` 定义运行时↔算子的 **内部 C 契约**：`TfLiteTensor`（扁平张量）、`TfLiteNode`（一次调用）、`TfLiteContext`（运行时总线）、`TfLiteRegistration`（算子=一组 C 函数指针 init/prepare/invoke）；`c_api.h` 定义应用↔运行时的 **外部不透明 ABI 边界**。两者都是「C 当边界」，但层次不同。
- 与桌面端 `DirectSession` 的本质差别：单设备无放置、扁平执行计划、C 函数指针分发、arena 静态内存规划、FlatBuffer 零拷贝、`OpResolver` 显式注册、同步单线程。

## 7. 下一步学习建议

- **下一讲 u8-l2「FlatBuffer 模型格式与 OpResolver」** 会钻进本讲只是「点名」的两个对象：深入 FlatBuffer 的 schema 与零拷贝读取、以及 `MutableOpResolver` 如何把 op code 注册到具体 kernel。建议先重读本讲的 4.1.3 与 4.3.3 中关于 `OpResolver`/`TfLiteRegistration` 的部分作为铺垫。
- **u8-l3「TFLite 委托机制 delegates」** 会解释本讲提到的「并行交给 delegate」是怎么回事——届时你会理解 `Interpreter::ModifyGraphWithDelegate` 如何把子图分区卸载到 GPU/NNAPI/XNNPACK，以及失败时如何回退到 CPU kernel。
- 若你想横向对照，建议回看 **u3-l2（DirectSession 执行链路）** 与 **u6-l2（BFCAllocator）**，把本讲对比表里的每一行都在桌面端那一侧找到对应的源码依据。
- 进阶可阅读 `tensorflow/lite/core/subgraph.h`（Interpreter 的真正引擎）与 `tensorflow/lite/memory_planner.h`（arena 静态规划），它们是本讲有意留到后续的「引擎内部」。
