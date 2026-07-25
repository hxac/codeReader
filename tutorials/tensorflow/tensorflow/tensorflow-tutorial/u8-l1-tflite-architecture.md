# TFLite 架构与 Interpreter

> 单元 u8 边缘部署 TFLite · 第 1 讲
> 依赖：u1-l5（版本信息与 C++ public 接口）

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说清 **TensorFlow Lite（TFLite）** 与桌面端 TensorFlow 的定位差异，理解它为何要「轻」、靠什么「轻」。
2. 描述一个 `.tflite` 模型从**加载 → 建图 → 分配张量 → 推理**的完整链路，并能在源码中定位每个阶段对应的函数。
3. 理解 `Interpreter` 的**解释执行模型**——它不像 DirectSession 那样放置/分区/跨设备通信，而是按一张「执行计划」顺序逐个调用 op 的 `invoke`。
4. 认识 TFLite 的 **C 接口边界**：`common.h` 定义的 `TfLiteTensor`/`TfLiteContext`/`TfLiteRegistration` 等核心 C 结构，以及 `c_api.h` 提供的稳定 ABI。

## 2. 前置知识

本讲假定你已经读过 **u1-l5**，知道桌面端有一个 C++ 抽象入口 `Session`（`core/public/session.h`），它用工厂模式 `NewSession()` 创建 `DirectSession`，执行靠「放置（Placement）→ 剪枝 → 优化 → 分区 → 调度执行」。TFLite 是这套执行模型在「移动/嵌入式」场景下的精简对应物，因此我们会不断拿它和 `Session`/`DirectSession` 做对照。

需要先建立的几个直觉：

- **推理（inference）vs 训练（training）**：TFLite 只做推理。模型先在桌面端用完整 TF 训练好，再转换（convert）成 `.tflite` 文件，最后在手机/嵌入式设备上由 TFLite 解释执行。
- **解释执行（interpretation）vs JIT 编译**：TFLite 默认不把图编译成新的设备代码，而是拿到 FlatBuffer 模型后逐个 op 调用预注册的 C kernel（`invoke` 函数指针）。这是它「启动快、二进制小」的根源；加速则交给可选的 **delegate**（下讲 u8-l2 专题）。
- **C ABI 边界**：op 的实现可以用 C++ 写，但 op 与解释器之间的契约是 **纯 C 结构 + 函数指针**（`TfLiteRegistration`）。这让 TFLite 能以一个稳定 `.so` 被多种语言绑定复用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tensorflow/lite/README.md](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/README.md) | TFLite 一句话定位 |
| [tensorflow/lite/core/interpreter.h](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.h) | `Interpreter` 类的 C++ 接口（本讲主线） |
| [tensorflow/lite/core/interpreter.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.cc) | `AllocateTensors`/`Invoke` 等方法的实现（多数转交给主子图） |
| [tensorflow/lite/core/subgraph.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc) | `Subgraph::InvokeImpl` 真正的「按执行计划逐 op 调用」循环 |
| [tensorflow/lite/core/c/common.h](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h) | C 核心类型：`TfLiteTensor`/`TfLiteNode`/`TfLiteContext`/`TfLiteRegistration` |
| [tensorflow/lite/core/c/c_api_types.h](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/c_api_types.h) | `TfLiteStatus` 枚举 |
| [tensorflow/lite/core/c/c_api.h](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/c_api.h) | 稳定 C API：`TfLiteModel*`/`TfLiteInterpreter*` 等 |
| [tensorflow/lite/examples/label_image/label_image.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/examples/label_image/label_image.cc) | 一个完整可参考的 C++ 调用示例 |

> 注意：仓库里 `tensorflow/lite/c/common.h`、`tensorflow/lite/c/c_api.h`、`tensorflow/lite/model.h` 都是**转发 shim**，内部只有一行 `#include "tensorflow/lite/core/c/..."`。官方要求使用者 include shim、实现者 include `core/` 下的真实文件。本讲引用的是真实实现文件。

---

## 4. 核心概念与源码讲解

### 4.1 TFLite 是什么：轻量化推理的设计取舍

#### 4.1.1 概念说明

`README.md` 用一句话点明了 TFLite 的定位：

> TensorFlow Lite is TensorFlow's lightweight solution for mobile and embedded devices. It enables low-latency inference of on-device machine learning models with a small binary size and fast performance supporting hardware acceleration.

提取出三个关键词，正是 TFLite 全部设计的出发点：

- **low-latency（低延迟）**：模型要在设备上即时跑，不能依赖云端往返。
- **small binary size（小体积）**：要能塞进 APK/IPA，运行库越小越好。
- **hardware acceleration（硬件加速）**：CPU 跑不动时，能把子图卸载到 GPU/NPU/ DSP。

为了这三点，TFLite 相比桌面端 TF 做了若干「减法」与一处「加法」：

| 维度 | 桌面端 TF（`DirectSession`） | TFLite（`Interpreter`） |
| --- | --- | --- |
| 模型格式 | 内存中的 `Graph`/序列化 `GraphDef`（protobuf） | `.tflite`（**FlatBuffer**，零拷贝 mmap） |
| 是否训练 | 训练 + 推理 | **仅推理** |
| 执行方式 | 放置 → 剪枝 → 优化（Grappler）→ 分区 → 调度 | 直接按「执行计划」**逐 op 解释执行** |
| 设备 | 单机/多机多卡，靠 `_Send`/`_Recv` 跨设备 | 单设备；加速靠 **delegate** 替换子图 |
| 依赖 | 重（protobuf、众多框架代码） | 轻（纯 C 内核 + FlatBuffer） |

那一处「加法」是 **delegate 机制**：当解释执行的纯 CPU 路径不够快时，delegate 可以「接管」一段子图，用 GPU/NNAPI/XNNPACK 替换它。这是 u8-l3 的主题，本讲只在「执行计划」处点到为止。

#### 4.1.2 核心流程：一条贯穿的推理流水线

把一个 `.tflite` 文件变成一次推理结果，分五步。这五步既是本讲的骨架，也是后面所有源码精读的索引：

```
.tflite 文件
   │ ① 加载（FlatBufferModel::BuildFromFile，零拷贝 mmap）
   ▼
FlatBufferModel（只读模型描述：算子表 + 权重 buffer）
   │ ② 建图（InterpreterBuilder + OpResolver，把算子解析成 TfLiteRegistration）
   ▼
Interpreter（持有 Subgraph，里面有 tensors[] 与 execution_plan）
   │ ③ 分配张量（AllocateTensors：依据输入形状做内存规划）
   ▼
已就绪的 Interpreter
   │ ④ 填输入（typed_tensor / TfLiteTensorCopyFromBuffer）
   │ ⑤ 推理（Invoke：按 execution_plan 顺序调每个 op 的 invoke）
   ▼
输出张量
```

注意这五步和桌面端 `Session` 的对应关系：**①加载**≈`Session::Create(GraphDef)`；**③分配**≈`DirectSession` 的放置/分区（但 TFLite 不跨设备，所以只剩内存规划）；**⑤推理**≈`Session::Run`，只是 TFLite 不走 Executor/Rendezvous，而是直接一个 for 循环。

#### 4.1.3 源码精读

官方在 `interpreter.h` 顶部用一段注释给出了这五步的「标准写法」（C++ 视角）：

[interpreter.h:L87-L115](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.h#L87-L115) —— Interpreter 的推荐用法：先 `BuildFromFile` 建模型，再用 `InterpreterBuilder(model, resolver)` 造解释器，接着 `AllocateTensors()`，填输入，最后 `Invoke()`。

这段示例里隐藏了一个关键设计：**几乎从不直接 `new Interpreter`**。注释明确写道：

> Note: For nearly all practical use cases, one should not directly construct an Interpreter object, but rather use the InterpreterBuilder.

因为「把 FlatBuffer 算子表翻译成可执行图」这件事由 `InterpreterBuilder` 配合 `OpResolver` 完成（见 4.2）。真实的端到端范例在示例程序里：

[label_image.cc:L210-L224](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/examples/label_image/label_image.cc#L210-L224) —— `RunInference` 的开头：`FlatBufferModel::BuildFromFile` 加载模型，`BuiltinOpResolver` 提供内置算子注册表，`InterpreterBuilder(*model, resolver)(&interpreter)` 一步建好解释器。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：在真实示例里把「五步流水线」逐行对上号。
2. **步骤**：打开 [label_image.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/examples/label_image/label_image.cc) 的 `RunInference` 函数（第 203 行起），按下表填空：

   | 流水线步骤 | 对应代码行 | 调用的方法 |
   | --- | --- | --- |
   | ① 加载模型 | 第 212 行 | `FlatBufferModel::BuildFromFile(...)` |
   | ② 建解释器 | 第 224 行 | `InterpreterBuilder(*model, resolver)(&interpreter)` |
   | ③ 分配张量 | 第 286 行 | `interpreter->AllocateTensors()` |
   | ④ 填输入 | 第 300~321 行 | `interpreter->typed_tensor<T>(input)` 后写入数据 |
   | ⑤ 推理 | 第 325 / 334 行 | `interpreter->Invoke()` |

3. **观察现象**：注意第 274~284 行在 `AllocateTensors` **之前**先 `ModifyGraphWithDelegate`——这印证了 delegate 必须在分配前应用（因为 delegate 会改写图、进而改变内存规划）。
4. **预期结果**：你能用一句话说出「TFLite 推理 = 加载 + 建图 + 分配 + 填输入 + Invoke」，并指出每一步的源码位置。

#### 4.1.5 小练习与答案

**Q1**：TFLite 为什么默认不做 Grappler 那样的图优化？
**A**：因为优化（常量折叠、布局变换等）在**转换阶段**（桌面端 `TFLiteConverter`）就已经做完并固化进 `.tflite`；运行时只需解释执行，从而换取更小的运行库与更快的启动。

**Q2**：`FlatBufferModel` 为何要求「模型实例必须比 Interpreter 活得更久」？
**A**：TFLite 用 **mmap 零拷贝** 读取权重，Interpreter 里的张量数据直接指向 FlatBufferModel 持有的只读内存（`kTfLiteMmapRo`）。模型一旦先被释放，这些指针就成了悬空指针。

---

### 4.2 Interpreter：解释执行模型（模块 `lite.core.interpreter`）

#### 4.2.1 概念说明

`Interpreter` 是 TFLite 的「主控对象」，地位类似桌面端的 `DirectSession`，但简单得多。它的核心职责只有两个：

1. **持有图**：一张由若干 `Subgraph` 组成的计算图。绝大多数模型只有一个主子图（primary subgraph），`Interpreter` 的多数方法只是把调用**转发**给主子图。
2. **驱动执行**：`Invoke()` 按执行计划逐个调用 op。

它对外暴露的几乎全是「张量索引」语义：输入、输出、张量都用 `int` 索引引用（`inputs()[0]`、`tensor(5)`），而不是桌面端那种带名字的 `Tensor` 对象。这是一种刻意的轻量化——少造对象、少拷贝。

#### 4.2.2 核心流程：Invoke 到底干了什么

`Interpreter::Invoke` 本身非常薄，真正的活全在 `Subgraph::InvokeImpl`：

```
Interpreter::Invoke()                         [interpreter.cc:232]
   ├─ 重置取消标志、抑制非规格化浮点（性能）
   └─ primary_subgraph().Invoke()             [interpreter.cc:246]
         └─ Subgraph::InvokeImpl()            [subgraph.cc:1662]
               ├─ 检查 consistent_ / state_（是否就绪）
               └─ for node_index in execution_plan_:        ← 按计划顺序
                    ├─ (按需) PrepareOpsAndTensors()         ← 懒 prepare
                    ├─ 检查输入张量数据是否就绪
                    ├─ MayAllocateOpOutput()                 ← 分配动态张量
                    ├─ 检查取消标志
                    └─ OpInvoke(registration, &node)         [subgraph.cc:1770]
                          └─ registration->invoke(&context_, node)  [subgraph.cc:1467]
```

两个关键点：

- **「执行计划」**（`execution_plan_`）是一串**节点索引**，按依赖顺序排好。`Invoke` 就是老老实实 `for` 一遍。和桌面端 `Executor` 的异步调度、`Rendezvous` 跨设备传张量相比，这里没有调度器、没有通信——因为 TFLite 假设**单设备、同步**。
- **「懒 prepare」**：op 的 `prepare`（形状推导 + 申请输出）不是一次性全做完，而是在 `Invoke` 循环里**用到时才做**（`next_execution_plan_index_to_prepare_`）。若某个 op 在运行时改变了中间张量形状，会触发下游 op 重新 prepare。

#### 4.2.3 源码精读

**类定义与构造**。`Interpreter` 其实是 `impl::Interpreter` 的别名：

[interpreter.h:L122](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.h#L122) 与 [interpreter.h:L128-L139](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.h#L128-L139) —— `using Interpreter = impl::Interpreter;`，类注释明确「not thread-safe」（客户端需自行串行化调用）。

**关键私有字段**。`Interpreter` 把图的真相藏在两个成员里：

[interpreter.h:L1013](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.h#L1013) —— `TfLiteContext* context_;`：这是与 C 插件通信的**纯 C 结构**，也是张量元数据的「权威存储」（见 4.3）。

[interpreter.h:L1043](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.h#L1043) —— `std::vector<std::unique_ptr<Subgraph>> subgraphs_;`：真正的图住在子图里。`primary_subgraph()`（[L874](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.h#L874)）永远返回 `subgraphs_.front()`。

**转发模式**。看 `AllocateTensors` 的实现就能体会「Interpreter 多数只是转发」：

[interpreter.cc:L190-L198](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.cc#L190-L198) —— `Interpreter::AllocateTensors` 先应用默认 delegate，然后 `return primary_subgraph().AllocateTensors();`。几乎每个公开方法都是这个「转给主子图」的形状。

**Invoke 的薄壳**：

[interpreter.cc:L232-L257](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.cc#L232-L257) —— `Invoke` 重置取消标志、抑制非规格化浮点（x86 上的性能陷阱），核心一行是 `primary_subgraph().Invoke()`；执行后若未允许 buffer handle 输出，还会逐个输出张量调 `EnsureTensorDataIsReadable`（delegate 把数据留在 GPU 时需拷回 CPU）。

**真正的执行循环**在子图里：

[subgraph.cc:L1662-L1693](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L1662-L1693) —— `InvokeImpl` 先做就绪检查（`consistent_`、`state_`、内存规划是否存在），随后一个 `for (execution_plan_index ...)` 循环，逐节点取出 `TfLiteNode` 与 `TfLiteRegistration`。

[subgraph.cc:L1770-L1774](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L1770-L1774) —— 循环体里调用 `OpInvoke(registration, &node)`，失败则报错返回。

**OpInvoke 落到函数指针**：

[subgraph.cc:L1466-L1467](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L1466-L1467) —— 最终 `return op_reg.invoke(&context_, node);`。也就是说，整条推理链路的终点，就是调用某个 op 注册时填进 `TfLiteRegistration` 的那个 C 函数指针。这与桌面端「`OpKernel::Compute(OpKernelContext*)`」是同一思想，只是换成了纯 C 形态。

**访问张量的便捷方法**：

[interpreter.h:L288-L290](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.h#L288-L290) —— `tensor(int)` 返回 `TfLiteTensor*`，注释反复警告「地址不保证稳定，`Invoke`/`AllocateTensors` 等操作可能使其失效」——所以**每次取值前重新拿指针**是正确用法。

[interpreter.h:L325-L333](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.h#L325-L333) —— `typed_tensor<T>(idx)`：先校验 `tensor->type == typeToTfLiteType<T>()`，类型匹配才 `reinterpret_cast` 返回。这是一个「带类型检查的窄化访问」，避免误把 int8 张量当 float 读。

#### 4.2.4 代码实践（源码阅读型 · 对照桌面端 Session）

1. **目标**：把 TFLite 的执行链路逐行对照桌面端 `DirectSession`，找出「减掉了什么」。
2. **步骤**：
   - 重读 [subgraph.cc:L1662](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L1662) 的 `InvokeImpl` 循环。
   - 回忆 u1-l5 / u3-l2 讲的 `DirectSession::Run`：它要做「剪枝 → 放置 → Grappler 优化 → 分区 → 每设备一个 Executor → 跨设备 `_Send`/`_Recv`」。
3. **需要观察的现象**：在 `InvokeImpl` 里搜索 `Placement`/`Partition`/`Rendezvous`/`_Send`——你会发现**一个都找不到**。这正是 TFLite 的「减法」：单设备、同步、不跨进程，所以整个调度基础设施都被删掉，只剩一个 for 循环。
4. **预期结果**：写出下表（答案见小练习 Q3）。

   | 概念 | DirectSession | TFLite Interpreter |
   | --- | --- | --- |
   | 放置（Placement） | 有，`Placer` 选设备 | 无（单设备） |
   | 图优化 | 有，Grappler | 无（转换期已做） |
   | 跨设备通信 | `_Send`/`_Recv` + Rendezvous | 无 |
   | 执行单元 | Executor 异步调度 | for 循环同步逐 op |

#### 4.2.5 小练习与答案

**Q1**：`Interpreter::Invoke` 为什么不直接遍历节点，而要转交给 `primary_subgraph().Invoke()`？
**A**：因为图的真实结构（张量、节点、执行计划、内存规划）住在 `Subgraph` 里，`Interpreter` 只是「门面」。这种分层让一个 `Interpreter` 能挂多个子图（控制流、子函数），且 `SignatureRunner` 能复用同一套子图机制。

**Q2**：注释说 `Interpreter` 非线程安全，那想在多线程并发推理怎么办？
**A**：**每个线程一个 Interpreter 实例**（各自独立加载模型或共享只读 `FlatBufferModel`）。`Invoke` 会改写张量缓冲区，共享一个实例并发调用会数据竞争。

**Q3**：补全 4.2.4 的对照表。
**A**：放置/优化/通信三栏 TFLite 全为「无」，执行单元栏 TFLite 是「for 循环同步逐 op」。原因即 4.1 的「减法」：单设备 + 转换期已优化。

---

### 4.3 C 接口边界：common.h 与 c_api.h（模块 `lite.c.common`）

#### 4.3.1 概念说明

TFLite 的内核与 op 之间隔着一条 **纯 C 边界**。`common.h` 顶部说得直白：

> This file defines common C types and APIs for implementing operations, delegates and other constructs... the interface between the interpreter and the operations are C. The actual operations and delegates can be defined using C++.

为什么一定要 C？因为 C 结构 + 函数指针构成的 ABI 跨编译器/版本更稳定，便于：把 op 实现单独编译进插件 `.so`、把整套运行库以 `libtensorflowlite_c.so` 形态提供给各语言绑定（Python/Swift/Java）。这条边界上有两层 API：

- **`common.h`（内核契约）**：定义「op 长什么样」——`TfLiteRegistration`、`TfLiteTensor`、`TfLiteNode`、`TfLiteContext`。写自定义 op / delegate 时打交道的是它。
- **`c_api.h`（稳定推理 API）**：定义「用户怎么跑模型」——`TfLiteModel`/`TfLiteInterpreter`/`TfLiteInterpreterInvoke`。它面向的是「只要能 `.tflite` 跑起来」的使用者。

#### 4.3.2 核心流程：一张图看清 C 边界上的数据流

```
            ┌─────────────── c_api.h（稳定 ABI，面向用户）───────────────┐
用户代码 ──► TfLiteModelCreateFromFile(".tflite")
            TfLiteInterpreterCreate(model, options)  ──► 内部 new Interpreter
            TfLiteInterpreterAllocateTensors(...)
            TfLiteInterpreterInvoke(...)             ──► Interpreter::Invoke
            └──────────────────────────────────────────────────────────┘
                            │ 内部桥接
            ┌─────────── common.h（内核契约，面向 op/delegate）──────────┐
            TfLiteRegistration { init, prepare, invoke, ... }   ← op 注册
            TfLiteContext      { tensors, ResizeTensor, ... }    ← 运行时能力
            TfLiteTensor / TfLiteNode                            ← 数据与连线
            └──────────────────────────────────────────────────┘
```

两条贯穿全篇的契约：

1. **op = 四个回调**：`init`（一次性初始化）、`prepare`（形状推导/分配输出，可多次）、`invoke`（真正计算）、`free`（释放）。这与桌面端「OpDef 声明 + OpKernel::Compute 实现」是同构的，只是用 C 函数指针表达。
2. **状态码统一为 `TfLiteStatus`**：成功 `kTfLiteOk=0`，失败 `kTfLiteError=1`，另有 delegate 专用错误码。所有跨边界调用都用它。

#### 4.3.3 源码精读

**状态码**（先看它，因为后面到处用）：

[c_api_types.h:L74-L120](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/c_api_types.h#L74-L120) —— `TfLiteStatus` 枚举：`kTfLiteOk`/`kTfLiteError`/`kTfLiteDelegateError`/`kTfLiteApplicationError`/`kTfLiteUnresolvedOps`/`kTfLiteCancelled` 等。注释提醒「未来可能新增，别死依赖具体枚举值」。

**张量 `TfLiteTensor`**——TFLite 的「数据载体」，比桌面端的 `Tensor` 朴素得多，就是一个 C 结构：

[common.h:L548-L619](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h#L548-L619) —— `type`（元素类型）、`data`（`TfLitePtrUnion` 联合体指针）、`dims`（`TfLiteIntArray*` 形状）、`allocation_type`（内存来源）、`bytes`、`quantization` 等。

[common.h:L415-L435](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h#L415-L435) —— `TfLitePtrUnion`：一个 union，`int8_t*`/`float*`/`int64_t*`… 共用同一块缓冲区，建议只访问 `.data`（`void*`）或用 `GetTensorData<T>`。

[common.h:L112-L128](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h#L112-L128) —— `TfLiteIntArray`：定长 int 数组（`size` + 柔性数组 `data[]`），用来存形状和输入输出索引。这种「自带 size 的紧凑数组」是为了避免依赖 STL，契合嵌入式场景。

**节点 `TfLiteNode`**：

[common.h:L624-L661](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h#L624-L661) —— 一个节点的全部连线：`inputs`/`outputs`/`temporaries`/`intermediates`（都是 `TfLiteIntArray*` 张量索引）、`user_data`（init 返回的私有数据）、`builtin_data`（内置 op 参数）。

**op 注册 `TfLiteRegistration`**——本讲最重要的结构：

[common.h:L1184-L1281](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h#L1184-L1281) —— 定义一个 op 的实现。四个回调的签名与职责：

[common.h:L1210](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h#L1210) `init`：一次性初始化，返回 `void*` 存进 `node->user_data`。

[common.h:L1222](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h#L1222) `prepare`：输入尺寸变化时被调用，可在此 `context->ResizeTensor()` 申请输出。

[common.h:L1228](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h#L1228) `invoke`：执行计算，读 `node->inputs`、写 `node->outputs`。

> 和桌面端 `OpKernel` 的对照：`init`≈构造、`prepare`≈`Compute` 前的形状推导、`invoke`≈`Compute`。区别是 TFLite 把「形状推导」和「计算」拆成两个独立回调，且都是 C 函数指针。

**上下文 `TfLiteContext`**——op 访问运行时能力的「总线」：

[common.h:L871-L1104](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h#L871-L1104) —— 里面既有数据（`tensors` 数组、`tensors_size`、`recommended_num_threads`），也有能力（函数指针：`ResizeTensor`、`AddTensors`、`GetTensor`、`AllocatePersistentBuffer`、`RequestScratchBufferInArena`、`ReportError`…）。它的注释（[L858-L870](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h#L858-L870)）点明：由运行时创建、传给 op 的回调，作用相当于桌面端的 `OpKernelContext`。

**稳定推理 C API `c_api.h`**——面向「跑模型」的用户：

[c_api.h:L109-L115](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/c_api.h#L109-L115) —— 三个不透明类型：`TfLiteModel`、`TfLiteInterpreterOptions`、`TfLiteInterpreter`。对外只暴露指针，内部布局可自由演进——这就是「稳定 ABI」的含义（与 u4-l4 的 `c_api.h` 不透明指针风格一致）。

[c_api.h:L47-L77](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/c_api.h#L47-L77) —— 官方给出的 C API 标准用法：`TfLiteModelCreateFromFile` → `TfLiteInterpreterOptionsCreate` → `TfLiteInterpreterCreate` → `AllocateTensors` → `TfLiteTensorCopyFromBuffer` 填输入 → `TfLiteInterpreterInvoke` → `TfLiteTensorCopyToBuffer` 取输出 → 一系列 `Delete`。

关键函数一一对应 C++ 流水线：

[c_api.h:L201-L202](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/c_api.h#L201-L202) `TfLiteModelCreateFromFile`：从文件加载（对应 ①）。

[c_api.h:L312-L313](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/c_api.h#L312-L313) `TfLiteInterpreterCreate`：建解释器（对应 ②）。

[c_api.h:L362-L363](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/c_api.h#L362-L363) `TfLiteInterpreterAllocateTensors`：分配张量（对应 ③）。

[c_api.h:L395-L396](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/c_api.h#L395-L396) `TfLiteInterpreterInvoke`：推理（对应 ⑤）。

[c_api.h:L640-L641](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/c_api.h#L640-L641) `TfLiteTensorCopyFromBuffer`：往输入张量拷数据（对应 ④）。

**C API 的实现只是薄桥**。以 `Invoke` 为例：

[c_api.cc:L205-L209](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/c_api.cc#L205-L209) —— `TfLiteInterpreterAllocateTensors` 与 `TfLiteInterpreterInvoke` 各自只是取出内部 `Interpreter*` 转调 `AllocateTensors()`/`Invoke()`。C API 不做计算，只做「翻译与转发」——这和 u4-l4 讲的「Python→pywrap→C API→C++ kernel，C 层只翻译」如出一辙。

#### 4.3.4 代码实践（可运行 · 待本地验证）

下面这段「最小 C API 推理程序」直接改自 [c_api.h:L47-L77](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/c_api.h#L47-L77) 的官方示例，是上面五步流水线的 C 语言版本。

> 标注为**示例代码**——它不是仓库里现成的文件，你需要自己创建并链接 `libtensorflowlite_c`。

```c
// 示例代码：min_tflite.c —— 最小 TFLite C API 推理
#include <stdio.h>
#include "tensorflow/lite/c/c_api.h"   // 经 shim 转发到 core/c/c_api.h

int main(void) {
  // ① 加载模型（mmap 只读，model_data 必须比 interpreter 活得久）
  TfLiteModel* model = TfLiteModelCreateFromFile("model.tflite");

  // ② 建解释器 + 选项
  TfLiteInterpreterOptions* opt = TfLiteInterpreterOptionsCreate();
  TfLiteInterpreterOptionsSetNumThreads(opt, 2);
  TfLiteInterpreter* interp = TfLiteInterpreterCreate(model, opt);

  // ③ 分配张量（依据输入形状做内存规划）
  TfLiteInterpreterAllocateTensors(interp);

  // ④ 填输入（把一段 float 缓冲拷进第 0 个输入张量）
  TfLiteTensor* in = TfLiteInterpreterGetInputTensor(interp, 0);
  float input_buf[/*输入元素数*/ 1];   // 按模型实际形状填
  TfLiteTensorCopyFromBuffer(in, input_buf, sizeof(input_buf));

  // ⑤ 推理
  TfLiteInterpreterInvoke(interp);

  // 取输出
  const TfLiteTensor* out = TfLiteInterpreterGetOutputTensor(interp, 0);
  float output_buf[1];
  TfLiteTensorCopyToBuffer(out, output_buf, sizeof(output_buf));
  printf("result = %f\n", output_buf[0]);

  // 释放（顺序：先 interpreter/options，最后 model）
  TfLiteInterpreterDelete(interp);
  TfLiteInterpreterOptionsDelete(opt);
  TfLiteModelDelete(model);
  return 0;
}
```

1. **实践目标**：用 C API 复现「加载→建图→分配→填输入→Invoke」五步，验证它与 C++ `Interpreter` 是同一套机制的两个面。
2. **操作步骤**：
   - 用 Bazel 构建 C 运行库（具体 target 与编译选项请参考 `tensorflow/lite/c/BUILD`，**待本地验证**）。
   - 准备一个 `.tflite` 模型（可用 `tf.lite.TFLiteConverter` 转换任意 Keras 模型得到）。
   - 按模型真实输入形状调整 `input_buf` 大小与 `TfLiteInterpreterResizeInputTensor`（若输入维度可变）。
3. **观察现象**：删掉第 ③ 步 `AllocateTensors` 直接 `Invoke`，预期返回非 `kTfLiteOk`（解释器未就绪）；删掉第 ④ 步填输入，`Invoke` 仍可能成功但结果无意义——说明「分配」是硬前置、「填输入」是数据前提。
4. **预期结果**：程序打印出一个浮点结果；若无法本地编译运行，明确标注「待本地验证」，并改为阅读 `c_api.cc` 确认每个 C 函数确实转调了同名 C++ 方法。

> **无法运行时的退路（源码阅读型）**：打开 [c_api.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/c_api.cc) 第 96、168、205、209 行，确认 `TfLiteModelCreateFromFile`/`TfLiteInterpreterCreate`/`AllocateTensors`/`Invoke` 四个 C 函数分别桥接到 `FlatBufferModel` 与 `Interpreter` 的 C++ 方法——这同样能完成「五步流水线」的追踪目标。

#### 4.3.5 小练习与答案

**Q1**：`TfLiteRegistration` 的 `prepare` 和 `invoke` 为什么要分成两个回调，而不是像 `OpKernel::Compute` 那样合一？
**A**：分开后，**形状推导/输出分配**（`prepare`）可以在真正推理前批量、甚至提前完成，便于内存规划（arena 复用）；而 `invoke` 只做纯计算。合并会导致每次推理都重复算形状、无法静态规划内存——这对内存紧张的嵌入式设备是致命的。

**Q2**：`c_api.h` 里的 `TfLiteModel`/`TfLiteInterpreter` 为什么都是「不透明指针」而不是暴露字段的结构？
**A**：为了 **ABI 稳定**。字段隐藏后，TFLite 运行库可以在版本升级时自由调整内部类的内存布局，而只要 C 函数签名不变，旧的调用方代码与旧 `.so` 仍能工作。这正是「libtensorflowlite_c.so 作为稳定分发物」的前提。

**Q3**：`TfLitePtrUnion` 里为什么建议只访问 `.data` 而不是 `.f`/`.i32`？
**A**：直接访问具名成员会绕过类型检查，且部分成员已标记 deprecated。官方推荐用 `GetTensorData<T>(tensor)` 模板，它内部依据 `tensor->type` 做了安全转换，等价于 `Interpreter::typed_tensor<T>` 的 C++ 版本。

---

## 5. 综合实践

把本讲的三条主线串起来，完成下面这个「端到端追踪」任务。

**任务**：选定一个真实示例 [label_image.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/examples/label_image/label_image.cc) 的 `RunInference`，画一张「从 `.tflite` 文件到一次 `Invoke` 返回」的**完整调用链时序图**，要求：

1. 在图上标注 **C++ 层**（`FlatBufferModel` → `InterpreterBuilder` → `Interpreter` → `Subgraph` → `registration->invoke`）与 **C 边界层**（`TfLiteRegistration` 的四个回调、`TfLiteContext` 提供的能力）的衔接点。
2. 用三种颜色/标记区分五步流水线（加载/建图/分配/填输入/推理）。
3. 在图旁写一段「与 `DirectSession::Run` 的差异说明」，至少列出三条 TFLite **没有**的步骤（提示：放置、Grappler、跨设备通信、异步 Executor）。
4. 最后回答一个开放问题：如果某个 op 没有被 `OpResolver` 注册（即「unresolved op」），追踪到 [subgraph.cc:L1406-L1424](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L1406-L1424) 的 `OpPrepare`，运行时会返回哪个 `TfLiteStatus`？这和桌面端「op 未注册」的表现有何不同？

**验收标准**：你的时序图能让一个没读过 TFLite 源码的人，仅凭图与差异说明，就说出「TFLite 推理为何比桌面端 Session 简单、简单在哪」。

---

## 6. 本讲小结

- TFLite 是面向**移动/嵌入式**的轻量推理方案，目标是「低延迟、小体积、可硬件加速」，只做推理、不做训练。
- 推理五步流水线：**加载（`FlatBufferModel`）→ 建图（`InterpreterBuilder`+`OpResolver`）→ 分配（`AllocateTensors`）→ 填输入 → `Invoke`**。
- `Interpreter` 是 `DirectSession` 的精简对应物：多数方法只是转交给主 `Subgraph`；`Invoke` 的本质是 `Subgraph::InvokeImpl` 里**一个 for 循环按 `execution_plan_` 逐个调 `registration->invoke`**，没有放置、没有 Grappler、没有跨设备通信。
- op 的契约是纯 C 的 `TfLiteRegistration`（`init`/`prepare`/`invoke`/`free` 四回调），数据载体是朴素的 `TfLiteTensor`，运行时能力通过 `TfLiteContext` 这条「总线」传给 op——对应桌面端的 `OpKernel`/`Tensor`/`OpKernelContext`。
- `c_api.h` 提供稳定 ABI（不透明指针 + `TfLite*` 函数族），其实现 `c_api.cc` 只是到 `Interpreter` 的薄桥；状态码统一为 `TfLiteStatus`。

## 7. 下一步学习建议

- **u8-l2 FlatBuffer 模型格式与 OpResolver**：本讲把加载当成黑盒，下一讲拆开 `.tflite` 的 FlatBuffer 结构，并讲清 `OpResolver`/`MutableOpResolver` 如何把算子名映射到 4.3 里的 `TfLiteRegistration`，以及 `flatbuffer_conversions` 如何把 FlatBuffer 节点翻译成 `TfLiteNode`。
- **u8-l3 TFLite 委托机制 delegates**：本讲提到 `ModifyGraphWithDelegate` 会改写图。下一讲讲清 delegate 如何**分区**子图、把可加速部分卸载到 GPU/NNAPI/XNNPACK，以及失败时如何回退到 CPU kernel。
- 想加深「C 边界」理解的读者，可先读 u4-l4（C API 与 pywrap）做对照——桌面端 `c_api.h` 与 TFLite `c_api.h` 是同一套「不透明指针 + 稳定 ABI」哲学的两次应用。
