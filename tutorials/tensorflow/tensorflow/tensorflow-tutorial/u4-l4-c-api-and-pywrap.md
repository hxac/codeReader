# C API 与 pywrap：Python↔C++ 桥梁

## 1. 本讲目标

在前面的讲义里，你已经知道 TensorFlow 的「大脑」是用 C++ 写的（`core/`），而用户日常写的是 Python（`python/`）。但这二者中间隔着一条巨大的鸿沟——两套完全不同的语言、不同的内存模型、不同的类型系统。本讲要回答的核心问题是：

> **Python 代码里的一句 `tf.add(a, b)`，到底是怎样「跨过鸿沟」、最终让 C++ 内核真正算出结果的？**

学完本讲，你应当能够：

1. 理解 **C API**（`tensorflow/c/`）作为「语言无关稳定边界」的设计意义，知道它为什么用「不透明指针 + `TF_Status`」这种风格。
2. 区分两套 C API——稳定的 **图模式 `c_api`** 与不稳定的 **即时执行 `eager/c_api`**——并说清它们的分工。
3. 掌握 **pywrap**（pybind11 桥）如何把 C 函数包装成 Python 可直接调用的对象，并理解「Python 做策略、C 做数据」的职责切分。
4. 能完整画出一条「Python op 调用 → pywrap → C API → C++ 内核」的调用链，并标出每一层的职责边界。

本讲是 u4（Op 与 Kernel 注册机制）单元的关键一环：u4-l1 讲了 Op 是「说明书」、u4-l2 讲了 OpKernel 是「干活的工人」，而本讲讲的是「Python 怎么把任务递交到工人手里」。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个直觉。

### 2.1 为什么要单独搞一个 C API？

TensorFlow 想被很多语言调用：Python、C++、Go、Java、Swift、Rust……如果每种语言都直接去 `#include` C++ 的头文件，会遇到两个大麻烦：

- **ABI 脆弱**：C++ 的对象布局、虚表、STL 容器在不同编译器/版本间不通用。你用 gcc 编译的 `std::string`，换个编译器就认不出来。
- **强耦合**：语言的头文件一改，所有调用方都得重编。

解决办法是在 C++ 之上再包一层**纯 C 函数接口**。C 是「通用底层语言」，几乎所有语言都能调用 C 函数（叫 FFI，Foreign Function Interface）。这一层只暴露：

- **不透明指针**（opaque pointer）：调用方拿到的是 `TF_Session*`、`TF_Graph*` 这种指针，但**看不到结构体内部长什么样**，只能通过函数来操作它。这样内部怎么改布局都不影响调用方。
- **`TF_Status`**：C 没有异常，错误信息塞进一个 `TF_Status*` 参数里返回。

这层 C API 就是 `tensorflow/c/c_api.h`。它是 TensorFlow 给**所有非 C++ 语言**的统一入口，也是 Python 与 C++ 之间的「海关」。

### 2.2 Python 怎么调 C 函数？——pywrap 与 pybind11

Python 本身不能直接调任意的 C 函数，需要一座桥。TensorFlow 用的桥叫 **pybind11**：它是一个 C++ 库，让你用几行 `m.def(...)` 就能把一个 C/C++ 函数注册成 Python 模块里的函数。

具体来说，TF 把 `c_api.cc`（C API 实现）和一堆「Python 友好」的包装代码编译成一个 `.so`（Linux 上的动态库，Windows 上是 `.pyd`），名字形如 `_pywrap_tf_session`。Python 里 `from ... import _pywrap_tf_session` 就能拿到这些函数。这套「把 C/C++ 能力包装给 Python」的机制，社区里统称 **pywrap**。

你在前面讲义（u1-l4）里见过的 `pywrap_tensorflow`、`pywrap_tfe`，都是同一思路的产物。

> 关键术语回顾（来自前置讲义）：
> - **稳定 API**：仅 Python 与 C++。`c/c_api.h` 属于 C++/C 侧的稳定边界。
> - **pywrap_tensorflow**：加载承载 C++ 内核的 `.so` 的桥（见 u1-l4）。
> - **Op / OpKernel**：Op 是说明书，OpKernel 是实现（见 u4-l1、u4-l2）。
> - **Session / Eager Context**：图模式的执行入口 / 即时模式的常驻执行器（见 u1-l5、u3-l3）。

## 3. 本讲源码地图

本讲涉及的关键文件与各自职责：

| 文件 | 语言层 | 职责 |
|------|--------|------|
| `tensorflow/c/c_api.h` | C（稳定） | 图模式 C API 的公开声明：`TF_Session`、`TF_Graph`、`TF_SessionRun` 等 |
| `tensorflow/c/c_api.cc` | C++ | C API 的实现，桥接到 `core/` 的 `Session`、`Graph` |
| `tensorflow/c/c_api_internal.h` | C++ | C API 内部结构体（`TF_Session`、`TF_Graph` 的真实布局，对外不公开） |
| `tensorflow/c/eager/c_api.h` | C（**不稳定**） | Eager 扩展 C API：`TFE_Context`、`TFE_NewOp`、`TFE_Execute` |
| `tensorflow/c/eager/c_api.cc` | C++ | Eager C API 的实现 |
| `tensorflow/python/client/pywrap_tf_session.py` | Python | 把 pybind11 模块 `_pywrap_tf_session` 的符号重新导出给 Python 用 |
| `tensorflow/python/client/tf_session_wrapper.cc` | C++ | pybind11 注册文件，用 `m.def(...)` 把 C API 包成 Python 函数 |
| `tensorflow/python/client/tf_session_helper.cc` | C++ | 负责 numpy↔`TF_Tensor` 转换、释放 GIL、调 `TF_SessionRun` |
| `tensorflow/python/client/session.py` | Python | Python 侧 `BaseSession`，调用 pywrap 的入口 |
| `tensorflow/python/eager/pywrap_tfe_src.cc` | C++ | Eager 的 pybind11 桥：`TFE_Py_Execute` |
| `tensorflow/python/eager/execute.py` | Python | Eager 侧执行入口 `quick_execute` |

一句话定位：**左边（`c/`、`c/eager/`）是「海关」本身，右边（`python/client`、`python/eager`）是「过海关的车辆与报关员」。**

## 4. 核心概念与源码讲解

### 4.1 C API 的设计哲学：语言无关的稳定边界（`c_api`）

#### 4.1.1 概念说明

`tensorflow/c/c_api.h` 是 TensorFlow 对外承诺**向后兼容**的 C 接口。它的设计目标在文件开头一段很长的注释里写得很直白：

> The API leans towards simplicity and uniformity instead of convenience since most usage will be by language specific wrappers.

翻译过来就是：**这套 API 追求简单与统一，而不是好用——因为它真正的用户是「各语言的包装层」（如 pywrap），而不是人类。** 理解这句话非常重要：你会看到 C API 里全是又长又啰嗦的函数名和繁琐的参数，这是为了让它「好包装」「好跨语言」，而不是「好写」。

它解决的核心问题是：让 C++ 内核的能力以一种**与编译器/语言无关**的方式暴露出去。

#### 4.1.2 核心流程

C API 有一套贯穿始终的「约定（conventions）」，可以总结为五条规则：

1. **统一前缀 `TF_`**：所有符号以 `TF_` 开头，避免命名冲突。
2. **对象都是不透明指针**：`TF_Session*`、`TF_Graph*`、`TF_Tensor*`……调用方只持有指针，看不到结构体内部，只能用 `TF_NewXxx` 创建、`TF_DeleteXxx` 销毁。
3. **错误用 `TF_Status*` 返回**：C 没有异常，每个可能出错的函数都接收一个 `TF_Status*`，成功时清空、失败时填入错误信息。
4. **布尔用 `unsigned char`**：避免 C99 与 C++ 的 `bool` 大小不一致（这是真实存在的坑）。
5. **`Delete` 函数对 nullptr 安全**：可以放心对一个已释放的指针再调 `TF_DeleteXxx`。

图模式下，一次完整的「建图 + 执行」流程在 C API 层是这样的：

```
TF_NewGraph()                      // 1. 建一个空图
  └─ TF_NewOperation(graph, "Add") // 2. 描述一个 op（设属性、接输入）
       └─ TF_FinishOperation()     // 3. 把描述提交，得到 TF_Operation 节点
TF_NewSession(graph, opts)         // 4. 基于图建一个会话
TF_SessionRun(session, ...)        // 5. 喂数据、取结果
TF_DeleteSession / TF_DeleteGraph  // 6. 清理
```

注意步骤 2→3 的两段式：先得到一个「描述对象」`TF_OperationDescription*`，可以反复往里塞属性和输入；调 `TF_FinishOperation` 才真正把它「固化」成图里的节点。这对应 C++ 内核里 NodeBuilder 的用法。

#### 4.1.3 源码精读

**(1) 开头的约定注释**——这是理解整个 C API 风格的钥匙：

[c/c_api.h:30-75](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/c_api.h#L30-L75) 逐条列出了上面讲的那些约定（前缀、不透明指针、`TF_Status`、`unsigned char` 当布尔、`size_t` 表字节数、`Delete` 对 nullptr 安全）。

其中 `TF_CAPI_EXPORT` 是一个宏，在不同平台上展开成不同的「导出符号」修饰（如 Linux 的可见性属性），保证这些函数能被动态库外部看到。

**(2) 不透明指针的声明方式**——以 `TF_SessionOptions` 和 `TF_Graph` 为例：

```c
// c_api.h
typedef struct TF_SessionOptions TF_SessionOptions;
TF_CAPI_EXPORT extern TF_SessionOptions* TF_NewSessionOptions(void);
```

注意这里只写了 `typedef struct TF_SessionOptions TF_SessionOptions;` 而**没有定义结构体内容**。调用方拿到的永远是指针，内部布局藏在实现里。

**(3) 结构体的真实布局藏在内部头文件**——`c_api_internal.h` 才给出真实定义：

[c/c_api_internal.h:58-60](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/c_api_internal.h#L58-L60) 显示 `TF_SessionOptions` 内部其实就是包了一个 C++ 的 `tensorflow::SessionOptions`：

```cpp
struct TF_SessionOptions {
  tensorflow::SessionOptions options;
};
```

[c/c_api_internal.h:122-136](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/c_api_internal.h#L122-L136) 则给出 `TF_Session` 的内部结构，它持有指向 C++ `Session*` 的指针、所属的 `TF_Graph*`，以及一个 `extend_before_run` 标志：

```cpp
struct TF_Session {
  TF_Session(tensorflow::Session* s, TF_Graph* g);
  tensorflow::Session* session;     // ← 真正干活的 C++ Session
  TF_Graph* const graph;
  tensorflow::mutex mu;
  int last_num_graph_nodes;
  std::atomic<bool> extend_before_run;
};
```

这就揭示了 C API 的本质：**「不透明指针」是一个壳，里面包着 C++ 对象的指针，所有 C API 函数都是在操作这个壳。**

**(4) `TF_NewSession` 如何桥接到 C++ 的 `NewSession`**：

[c/c_api.cc:2241-2257](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/c_api.cc#L2241-L2257) 的实现非常关键，它演示了 C API → C++ core 的标准跳转方式：

```cpp
TF_Session* TF_NewSession(TF_Graph* graph, const TF_SessionOptions* opt,
                          TF_Status* status) {
  Session* session;
  status->status = NewSession(opt->options, &session);   // ← 调 C++ 工厂
  if (status->status.ok()) {
    TF_Session* new_session = new TF_Session(session, graph);  // ← 包成壳
    if (graph != nullptr) {
      mutex_lock l(graph->mu);
      graph->sessions[new_session] = "";  // ← 在图里登记这个会话
    }
    return new_session;
  }
  ...
}
```

读法：先把 C 风格的 `TF_SessionOptions` 拆出里面的 C++ `options`，调 u1-l5 讲过的全局工厂 `NewSession`（经 `SessionFactory` 选出 `DirectSession`），再把返回的 C++ `Session*` 用 `new TF_Session(...)` 包成不透明指针返回。**`status->status = ...` 这一句，就是把 C++ 的 `absl::Status` 塞进 C 的 `TF_Status` 的标准动作。**

**(5) `TF_SessionRun`——图执行的 C 入口**：

[c/c_api.h:1273-1286](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/c_api.h#L1273-L1286) 声明了这个函数，参数很多但分成清晰的几组：输入（`inputs`/`input_values`/`ninputs`）、输出（`outputs`/`output_values`/`noutputs`）、目标 op、`run_metadata`、以及末尾的 `TF_Status*`。

[c/c_api.cc:2341-2380](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/c_api.cc#L2341-L2380) 是它的实现。它做的全是「翻译」工作，没有真正的计算逻辑：

```cpp
void TF_SessionRun(TF_Session* session, ...) {
  // 1. 若图有改动，先把新增节点 Extend 进 C++ Session
  if (session->extend_before_run &&
      !ExtendSessionGraphHelper(session, status)) return;
  // 2. 初始化输出数组
  TF_Run_Setup(noutputs, output_values, status);
  // 3. 把 C 的 TF_Tensor* 翻译成 C++ 的 Tensor，把 TF_Output 翻译成字符串名
  std::vector<std::pair<string, Tensor>> input_pairs(ninputs);
  TF_Run_Inputs(input_values, &input_pairs, status);
  for (...) input_pairs[i].first = OutputName(inputs[i]);
  std::vector<string> output_names(noutputs);
  for (...) output_names[i] = OutputName(outputs[i]);
  // 4. 真正交给 C++ Session::Run 执行
  TF_Run_Helper(session->session, nullptr, run_options, input_pairs,
                output_names, output_values, target_names, run_metadata, status);
}
```

读法：C API 在这里把「指针 + 索引」风格的 `TF_Output`/`TF_Tensor` 翻译成 C++ 内核更习惯的「字符串名 + `Tensor`」，然后交给 u3-l2 讲过的 `DirectSession::Run`（经 `TF_Run_Helper`）。**真正的放置、剪枝、调度都在 C++ 那一侧，C API 只负责搬运与格式转换。**

#### 4.1.4 代码实践

**实践目标**：直观感受 C API「不透明指针 + `TF_Status`」的风格，并理解它是 C++ 内核的一层壳。

**操作步骤（源码阅读型实践）**：

1. 打开 [c/c_api_internal.h:122-136](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/c_api_internal.h#L122-L136)，确认 `TF_Session` 内部就一个 `tensorflow::Session* session` 字段在「干活」。
2. 打开 [c/c_api.cc:2241](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/c_api.cc#L2241) 的 `TF_NewSession`，追踪它如何调 C++ 的 `NewSession(opt->options, &session)`。
3. 阅读 [c/c_api_test.cc](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/c_api_test.cc) 中任意一个用 `TF_NewGraph`/`TF_NewOperation`/`TF_FinishOperation`/`TF_SessionRun` 拼出的小例子（这是 C API 的「人类可读」用法示范）。

**需要观察的现象**：

- C API 的函数全都接收或返回 `TF_Xxx*` 指针；
- 每个「可能出错」的函数末尾都有一个 `TF_Status*`；
- 真正的计算（`Session::Run`）发生在 C++ 一侧，C API 层看不到任何算术。

**预期结果**：你能在脑中画出「C 不透明指针壳 → 内部包着 C++ Session 指针 → 调用 C++ Session::Run」的三层关系。

> 待本地验证：如果你本机能编译 TF 的 C API 测试目标（`bazel test //tensorflow/c:c_api_test`），可以亲手跑一个建图+`SessionRun` 的用例；若不方便编译，上述源码阅读实践同样有效。

#### 4.1.5 小练习与答案

**练习 1**：为什么 C API 用 `unsigned char` 表示布尔值，而不是直接用 C99 的 `bool`？

**参考答案**：因为 C99 的 `bool`（来自 `stdbool.h` 宏）与 C++ 的 `bool` 关键字，二者的大小都没有被标准强制规定，可能在某些编译器组合下不一致。而 C API 同时要被 C 和 C++ 代码包含，为保证两种语言看到的字节大小一致，干脆用大小确定的 `unsigned char`。

**练习 2**：`TF_Session*` 这个指针在头文件里只是 `typedef struct TF_Session TF_Session;`，没有结构体定义。这种「前向声明不透明指针」带来什么好处？

**参考答案**：调用方无法访问结构体内部字段，只能通过 C API 函数操作它。这样 TF 内部可以自由改动 `TF_Session` 的字段布局（甚至完全重写实现），而不会破坏调用方代码的二进制兼容性——这就是「稳定边界」的含义。

---

### 4.2 Eager C API：即时执行的扩展（`eager/c_api`）

#### 4.2.1 概念说明

前面 u3-l3 讲过，TF2 默认是 **Eager（即时）执行**模式：op 一被调用就立即执行，返回真实数值的 `EagerTensor`，而不需要先建图再 `Session.run`。

这套即时执行需要一套**全新的 C 接口**，因为图模式的 `TF_SessionRun` 是「一次性跑整张子图」的批量语义，而 Eager 是「一次执行一个 op」的细粒度语义。于是 TF 在 `tensorflow/c/eager/c_api.h` 里加了一组以 **`TFE_`** 为前缀的扩展 API。

注意文件开头的警告：

[c/eager/c_api.h:19-21](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/eager/c_api.h#L19-L21) 明确写道：「**Unlike tensorflow/c/c_api.h, the API here is not guaranteed to be stable and can change without notice.**」——也就是说，`TF_` 是稳定 API，`TFE_` 是**不稳定**的实验性扩展。这是一个重要的分工与对比。

#### 4.2.2 核心流程

Eager 执行的核心对象有三个：

- **`TFE_Context`**：即时执行上下文，相当于 Eager 模式的「常驻 Session」。它持有设备列表、资源管理器等。它必须比所有用它创建的 `TFE_TensorHandle` 活得长。
- **`TFE_TensorHandle`**：即时模式的张量句柄。它像 `TF_Tensor`，但更灵活——可能指向一个尚未就绪的异步张量。
- **`TFE_Op`**：即时模式的 op 描述对象，对应图模式的 `TF_OperationDescription`。

一次「执行一个 op」的流程是：

```
TFE_NewOp(ctx, "Add")           // 1. 在 ctx 下创建一个 op 描述
  ├─ TFE_OpSetDevice(...)       // 2. （可选）指定设备
  ├─ TFE_OpAddInput(handle_a)   // 3. 接输入张量句柄
  ├─ TFE_OpAddInput(handle_b)
  └─ TFE_OpSetAttrType("T",...) // 4. 设属性
TFE_Execute(op, &retvals, ...)  // 5. 立即执行，拿到输出句柄
```

关键区别在于：图模式是「先描述一整张图，最后 `TF_FinishOperation` 提交，再 `TF_SessionRun` 跑」；而 Eager 是「描述一个 op，立刻 `TFE_Execute` 就出结果」。`TFE_Execute` 内部会把这个 op 路由到对应设备的 kernel 去执行（即 u4-l2 的 OpKernel）。

#### 4.2.3 源码精读

**(1) 三个核心对象的声明**：

[c/eager/c_api.h:79](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/eager/c_api.h#L79) 声明 `TFE_Context`（注释里写明它封装了可用设备、资源管理器，并且要 `TFE_DeleteContext` 在所有 handle 删除之后才能调用）：

```c
typedef struct TFE_Context TFE_Context;
```

[c/eager/c_api.h:120](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/eager/c_api.h#L120) 声明 `TFE_TensorHandle`（注释点明它「像 `TF_Tensor`，但可能指向尚未就绪的异步张量」）。

[c/eager/c_api.h:230](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/eager/c_api.h#L230) 声明 `TFE_NewOp`，创建一个 op 描述。

**(2) `TFE_NewOp` 的实现**——标准的「解包壳 → 调 C++ → 再包壳」：

[c/eager/c_api.cc:668-678](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/eager/c_api.cc#L668-L678)：

```cpp
TFE_Op* TFE_NewOp(TFE_Context* ctx, const char* op_or_function_name,
                  TF_Status* status) {
  tensorflow::ImmediateExecutionOperation* new_op =
      tensorflow::unwrap(ctx)->CreateOperation();   // ← 解包 ctx，调 C++ 方法
  status->status = new_op->Reset(op_or_function_name, nullptr);  // ← 设 op 名
  if (!status->status.ok()) {
    new_op->Release();
    new_op = nullptr;
  }
  return tensorflow::wrap(new_op);   // ← 再包回不透明指针
}
```

这里的 `unwrap`/`wrap` 是 TF 内部的一对模板函数：`unwrap` 把 C 句柄（`TFE_Context*`）转回 C++ 对象（`ImmediateExecutionContext*`），`wrap` 反过来。这是所有 `TFE_` 函数的固定套路。

**(3) `TFE_Execute`——Eager 的真正执行入口**：

[c/eager/c_api.h:378-379](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/eager/c_api.h#L378-L379) 声明它，注释强调：`retvals` 要预分配数组、`*num_retvals` 传入数组大小、调用后被设为实际输出数；异步模式下可能返回「未就绪」的句柄。

[c/eager/c_api.cc:924-935](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/eager/c_api.cc#L924-L935) 的实现同样很短，把执行交给 C++ 的「自定义设备 op 处理器」：

```cpp
void TFE_Execute(TFE_Op* op, TFE_TensorHandle** retvals, int* num_retvals,
                 TF_Status* status) {
  tensorflow::ImmediateExecutionOperation* unwrapped_op = tensorflow::unwrap(op);
  status->status =
      unwrapped_op->GetContext()->GetCustomDeviceOpHandler().Execute(
          unwrapped_op,
          reinterpret_cast<tensorflow::ImmediateExecutionTensorHandle**>(retvals),
          num_retvals);
}
```

读法：C 层的 `TFE_Execute` 几乎只做类型转换（`TFE_TensorHandle**` → C++ 的 `ImmediateExecutionTensorHandle**`），真正的「选 kernel、调 `Compute`」全在 C++ 的 `Execute` 里。这与图模式 `TF_SessionRun` 的「C 层只翻译、C++ 层干活」完全一致。

#### 4.2.4 代码实践

**实践目标**：理解「图模式 C API」与「Eager C API」的分工，并对照 u3-l3 的 Eager 概念。

**操作步骤（源码阅读型实践）**：

1. 对比两个文件的开头注释：[c/c_api.h:30](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/c_api.h#L30)（稳定）与 [c/eager/c_api.h:19-21](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/eager/c_api.h#L19-L21)（不稳定）。
2. 在 [c/eager/c_api.cc:668](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/eager/c_api.cc#L668) 跟踪 `TFE_NewOp` → `unwrap(ctx)->CreateOperation()` → `new_op->Reset(name)`。
3. 在 [c/eager/c_api.cc:924](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/eager/c_api.cc#L924) 跟踪 `TFE_Execute` → `GetCustomDeviceOpHandler().Execute(...)`，确认真正的执行发生在 C++ 一侧。

**需要观察的现象**：所有 `TFE_` 函数体都极短，套路是「`unwrap` 句柄 → 调一个 C++ 方法 → `wrap` 结果」。

**预期结果**：你能说出「Eager C API 是一组薄薄的转发函数，把 Python 的即时 op 调用转发到 C++ 的 `ImmediateExecutionContext`」。

> 待本地验证：[c/eager/c_api_test.cc](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/eager/c_api_test.cc) 里有用 C++ 直接调 `TFE_NewContext`/`TFE_NewOp`/`TFE_Execute` 的完整示例，可作为对照阅读材料。

#### 4.2.5 小练习与答案

**练习 1**：`TFE_` 前缀和 `TF_` 前缀的 API，哪个保证向后兼容？为什么 TF 要这样区分？

**参考答案**：`TF_`（`c_api.h`）保证向后兼容，是稳定边界；`TFE_`（`eager/c_api.h`）不保证，可随时改。原因是 Eager 运行时还在快速演进（比如后来引入 TFRT、自定义设备等），接口需要灵活变动；而图模式的 C API 已经被 Go/Java/Swift 等外部语言依赖，必须稳定。

**练习 2**：`TFE_Execute` 的实现体只有几行，真正的「选 kernel 并执行」发生在哪里？

**参考答案**：发生在 C++ 一侧的 `ImmediateExecutionContext::GetCustomDeviceOpHandler().Execute(...)`。C 层的 `TFE_Execute` 只做了 `unwrap`（解包句柄）和 `reinterpret_cast`（转换指针类型），把活儿交给 C++ 运行时。

---

### 4.3 pywrap 桥：用 pybind11 把 C API 接入 Python（`pywrap_tf_session`）

#### 4.3.1 概念说明

C API 是给「所有语言」用的，但 Python 用户不会、也不应该手写 C 函数调用。需要一座桥：把 C 函数包成「像普通 Python 函数一样可调用」的对象。TensorFlow 用的桥是 **pybind11**，社区里把这套包装统称为 **pywrap**。

pywrap 有两层：

1. **pybind11 注册层**（`.cc` 文件，如 `tf_session_wrapper.cc`）：用 `m.def("名字", 函数或 lambda)` 把 C/C++ 函数注册成一个 Python 模块 `_pywrap_tf_session`（编译产物是一个 `.so`）。
2. **Python 再导出层**（`.py` 文件，如 `pywrap_tf_session.py`）：`from ._pywrap_tf_session import *`，让符号进入正常的 Python 包命名空间，并做一些 Python 侧的便捷包装。

为什么要在中间多一层 `.py`？因为 pybind11 直接吐出来的符号往往不符合 Python 风格（比如参数类型、版本号要转成 `str`），需要在 Python 侧再做点整理。u1-l4 里你已经见过 `pywrap_tf_session.py` 参与版本号处理。

#### 4.3.2 核心流程

以「图模式 `Session.run`」为例，完整调用链如下（这是本讲最重要的图，建议手抄一遍）：

```
Python: session.py: BaseSession._run()
   │  （Python 侧：整理 fetches/feed_dict，转 numpy）
   ▼
Python: pywrap_tf_session.py  （from _pywrap_tf_session import *）
   │  （只是重新导出符号）
   ▼
pybind11: tf_session_wrapper.cc  m.def("TF_SessionRun_wrapper", lambda{...})
   │  （C++ lambda：把 Python dict 拆成 inputs 列表，建 TF_Status）
   ▼
C++ helper: tf_session_helper.cc  TF_SessionRun_wrapper(...)
   │  （numpy → TF_Tensor 转换；Py_BEGIN_ALLOW_THREADS 释放 GIL）
   ▼
C API: c_api.cc  TF_SessionRun(...)
   │  （TF_Output → 字符串名；TF_Tensor → C++ Tensor）
   ▼
C++ core: DirectSession::Run(...)   ← u3-l2 讲过，真正放置/调度/执行
```

Eager 模式的链路结构几乎一样，只是换了一套符号：

```
Python: execute.py  quick_execute(...)
   │  （pywrap_tfe.TFE_Py_Execute(ctx._handle, device, op_name, inputs, attrs, n)）
   ▼
Python: pywrap_tfe.py  （from _pywrap_tfe import *）
   ▼
pybind11: pywrap_tfe_src.cc  TFE_Py_ExecuteCancelable(...)
   │  （GetOp → AddInput → SetOpAttrs → TFE_Execute；Py_BEGIN_ALLOW_THREADS）
   ▼
C API: eager/c_api.cc  TFE_Execute(...)
   ▼
C++ core: ImmediateExecutionContext::Execute(...)   ← u3-l3 讲过
```

两条链路共享同一个设计思想，可用一句话概括职责切分：

> **Python 做「策略」（决定调哪个 op、喂什么数据、怎么组织结果），C 做「数据搬运」（指针转换、释放 GIL、状态翻译），C++ 做「真正的活」（放置、调度、执行 kernel）。**

#### 4.3.3 源码精读

**(1) Python 再导出层 `pywrap_tf_session.py`**：

[python/client/pywrap_tf_session.py:18-19](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/client/pywrap_tf_session.py#L18-L19) 是核心两行：

```python
from tensorflow.python import pywrap_tensorflow
from tensorflow.python.client._pywrap_tf_session import *
```

第一行（u1-l4 讲过）负责拉起承载 C++ 内核的 `.so`；第二行把 pybind11 注册的函数（`TF_SessionRun_wrapper` 等）以 `*` 导入到当前命名空间。后面 [python/client/pywrap_tf_session.py:51-59](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/client/pywrap_tf_session.py#L51-L59) 还提供了一个 Python 风格的便捷包装 `TF_NewSessionOptions(target, config)`，把「设 target / 序列化 config」两个动作合并，这是「Python 侧做便捷化」的典型例子。

**(2) pybind11 注册层 `tf_session_wrapper.cc`**：

[python/client/tf_session_wrapper.cc:1357-1408](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/client/tf_session_wrapper.cc#L1357-L1408) 用一个 lambda 把 C 函数包成 Python 可调用对象：

```cpp
m.def("TF_SessionRun_wrapper", [](TF_Session* session, TF_Buffer* run_options,
                                  const py::handle& input_dict,
                                  const std::vector<TF_Output>& outputs,
                                  ...) {
  // 1. 把 Python dict 拆成 inputs + ndarray 列表
  std::vector<TF_Output> inputs;
  std::vector<PyObject*> input_ndarrays;
  while (PyDict_Next(input_dict.ptr(), &pos, &key, &value)) {
    inputs.push_back(py::cast<TF_Output>(key));
    input_ndarrays.push_back(value);
  }
  // 2. 调 helper（它会转 numpy、释放 GIL、调 TF_SessionRun）
  tensorflow::TF_SessionRun_wrapper(session, run_options, inputs,
                                    input_ndarrays, outputs, targets,
                                    run_metadata, status.get(), &py_outputs);
  // 3. 把 C 的 TF_Status 翻译成 Python 异常
  tensorflow::MaybeRaiseRegisteredFromTFStatus(status.get());
  // 4. 把输出组装成 Python list 返回
  PyObject* result = PyList_New(py_outputs.size());
  ...
  return tensorflow::PyoOrThrow(result);
});
```

读法：这个 lambda 做的全部是「Python 对象 ↔ C 对象」的适配——`py::cast` 把 Python 的 `TF_Output` 转成 C 结构体，`PyDict_Next` 遍历 Python 字典，`MaybeRaiseRegisteredFromTFStatus` 把 C 的 `TF_Status` 翻译成 Python 异常并抛出。注意它**调用了同名 helper** `tensorflow::TF_SessionRun_wrapper`（命名空间 `tensorflow::` 下的 C++ 函数，区别于 Python 里看到的字符串名）。

**(3) C++ helper `tf_session_helper.cc`**——负责「脏活累活」：

[python/client/tf_session_helper.cc:360-420](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/client/tf_session_helper.cc#L360-L420) 是关键。它做两件 pybind11 lambda 不方便做的事：

```cpp
void TF_SessionRun_wrapper_helper(...) {
  // 1. numpy ndarray → TF_Tensor（NdarrayToTensor）
  for (PyObject* ndarray : input_ndarrays) {
    s = NdarrayToTensor(nullptr, ndarray, &input_vals_safe.back());
    ...
    input_vals.push_back(input_vals_safe.back().get());
  }
  // 2. 清缓存 + 释放 GIL 后调真正的 C API
  ClearDecrefCache();
  Py_BEGIN_ALLOW_THREADS;                        // ← 释放全局解释器锁
  TF_SessionRun(session, run_options, inputs.data(), input_vals.data(),
                inputs.size(), ...);
  Py_END_ALLOW_THREADS;                          // ← 重新获取 GIL
  // 3. 输出 TF_Tensor → numpy ...
}
```

两个关键点：

- **`Py_BEGIN_ALLOW_THREADS` / `Py_END_ALLOW_THREADS`**：Python 有全局解释器锁（GIL），同一时刻只有一个线程跑 Python 字节码。但在执行 C++ 内核计算时，TF 希望其他 Python 线程能继续干活，所以在这对宏之间**临时释放 GIL**。这是 pywrap 层最重要的性能职责之一。
- **numpy ↔ `TF_Tensor` 转换**（`NdarrayToTensor` / 反向）：Python 侧的数据是 numpy 数组，C API 要的是 `TF_Tensor`，这层转换只能用 C API 做，所以放在 helper 里。

**(4) Python 侧的调用点 `session.py`**：

[python/client/session.py:27](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/client/session.py#L27) 用别名导入这层桥：

```python
from tensorflow.python.client import pywrap_tf_session as tf_session
```

于是 [session.py:717-721](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/client/session.py#L717-L721) 里 `BaseSession.__init__` 就能这样建会话：

```python
opts = tf_session.TF_NewSessionOptions(target=self._target, config=config)
with self._graph._c_graph.get() as c_graph:
  self._session = tf_session.TF_NewSessionRef(c_graph, opts)
```

而真正执行在 [session.py:1483](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/client/session.py#L1483)：

```python
return tf_session.TF_SessionRun_wrapper(self._session, options, feed_dict,
                                        fetch_list, target_list, run_metadata)
```

这一句就是从 Python 跨进 C 的「海关闸口」。

**(5) Eager 侧的对应链路**——结构完全对称：

[python/eager/pywrap_tfe.py:24-25](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/pywrap_tfe.py#L24-L25) 是 Eager 的再导出层（`from tensorflow.python._pywrap_tfe import *`）。

[python/eager/execute.py:53-54](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/execute.py#L53-L54) 是 Python 侧执行入口 `quick_execute`，它调：

```python
tensors = pywrap_tfe.TFE_Py_Execute(ctx._handle, device_name, op_name,
                                    inputs, attrs, num_outputs)
```

注意它显式传了 `num_outputs`——u3-l3 讲过，这是为了「避免运行时去查注册表推断输出个数」而做的性能优化。

[python/eager/pywrap_tfe_src.cc:946-953](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/pywrap_tfe_src.cc#L946-L953) 的 `TFE_Py_Execute` 只是转调 `TFE_Py_ExecuteCancelable`。而 [pywrap_tfe_src.cc:955-1000](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/pywrap_tfe_src.cc#L955-L1000) 才是 Eager 桥的核心，它把「构造 op」和「执行 op」串起来：

```cpp
void TFE_Py_ExecuteCancelable(TFE_Context* ctx, const char* device_name,
                              const char* op_name, ...) {
  TFE_Op* op = GetOp(ctx, op_name, device_name, out_status);  // ← TFE_NewOp + 设设备
  ...
  for (...) TFE_OpAddInput(op, inputs->at(i), out_status);    // ← 接输入
  SetOpAttrs(ctx, op, attrs, 0, out_status);                  // ← 设属性
  Py_BEGIN_ALLOW_THREADS;                                     // ← 释放 GIL
  TFE_Execute(op, outputs->data(), &num_outputs, out_status); // ← 调 Eager C API
  ...
  Py_END_ALLOW_THREADS;
}
```

读法：Eager 桥把 Python 传来的「op 名 + 输入 + 属性」逐步装配成一个 `TFE_Op`，释放 GIL 后调 `TFE_Execute`（即 4.2 讲的 Eager C API）。这一段正好补全了 u3-l3 里「`execute.py` 通过 `pywrap_tfe.TFE_Py_Execute` 把单个 op 立即派发到 C++ 内核」这句话背后的全部细节。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：完整追踪一个 Python op 调用到 C API 的路径，写出 pywrap 与 c_api 各自承担的职责边界。这正是规格里要求的实践任务。

**操作步骤**：

下面给出一个最小示例（**示例代码**，非项目原文件，可保存为 `trace_op.py` 在装好 TF 的环境运行；不方便运行则按下面的「源码阅读」步骤走）：

```python
# 示例代码：在 Eager 模式下跟踪一次 op 调用
import tensorflow as tf

a = tf.constant([1.0, 2.0])      # EagerTensor
b = tf.constant([3.0, 4.0])
c = tf.add(a, b)                  # ← 本次要跟踪的 op 调用
print(c)                          # 期望: tf.Tensor([4. 6.], shape=(2,), dtype=float32)
```

跟踪 `tf.add(a, b)` 的 Eager 路径，对照源码逐层标注：

| 层 | 文件:行 | 该层职责 |
|----|---------|----------|
| ① Python | [execute.py:53](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/execute.py#L53) `quick_execute` | 决定 op 名 `"Add"`、整理 `inputs=[a,b]`、`attrs`、`num_outputs` |
| ② pywrap 再导出 | [pywrap_tfe.py:25](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/pywrap_tfe.py#L25) | 仅 `import *`，把符号带入命名空间 |
| ③ pybind11 桥 | [pywrap_tfe_src.cc:955](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/pywrap_tfe_src.cc#L955) `TFE_Py_ExecuteCancelable` | 装配 `TFE_Op`（`GetOp`+`AddInput`+`SetOpAttrs`）、释放 GIL、状态翻译 |
| ④ Eager C API | [eager/c_api.cc:924](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/c/eager/c_api.cc#L924) `TFE_Execute` | `unwrap` 句柄、类型转换、转发到 C++ |
| ⑤ C++ core | `ImmediateExecutionContext::Execute` | 选 kernel、调 `OpKernel::Compute`（u4-l2） |

**需要观察的现象 / 需要回答的问题**：

1. pywrap 层（②③）做了哪些 C API 自己做不到的事？——答：Python 对象↔C 对象转换（`py::cast`、numpy→`TF_Tensor`）、释放/重获 GIL、把 `TF_Status` 翻译成 Python 异常。
2. C API 层（④）做了什么？——答：把不透明句柄解包、做指针类型转换、转发到 C++ 运行时；它**不**做 Python 相关的事（不知道 GIL、不知道 numpy）。
3. 真正的计算在哪？——答：C++ core（⑤），C API 与 pywrap 都不碰算术。

**预期结果**：你能用一句话写出职责边界——「pywrap 负责 Python 适配与 GIL/异常翻译，C API 负责句柄解包与语言无关的稳定转发，C++ core 负责真正的计算」。

> 待本地验证：如果你本机装了 `tensorflow`，运行上面的 `trace_op.py`，再在 `tf.add` 前后各加一句 `print`，可以看到它立即返回真实数值（`tf.Tensor([4. 6.], ...)`），这正是「Eager op 经 pywrap→C API→C++ kernel 立即执行」的可见效果。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `TF_SessionRun_wrapper` 在 pybind11 层（`tf_session_wrapper.cc`）之后，还要再调一个 C++ helper（`tf_session_helper.cc` 里的 `TF_SessionRun_wrapper_helper`）？直接在 lambda 里做完不行吗？

**参考答案**：原则上可以，但拆出 helper 有两个好处：一是 `NdarrayToTensor`（numpy↔`TF_Tensor` 转换）涉及较多 C API 细节，独立成函数便于复用与测试；二是 `Py_BEGIN_ALLOW_THREADS/Py_END_ALLOW_THREADS`（释放 GIL）这段代码需要精确控制作用域，放在独立 helper 里更清晰、不易出错。本质上是为了职责分离与可维护性。

**练习 2**：`Py_BEGIN_ALLOW_THREADS` 和 `Py_END_ALLOW_THREADS` 这对宏在调用链里解决了什么问题？

**参考答案**：Python 有 GIL（全局解释器锁），同一时刻只有一个线程执行 Python 字节码。在 `TF_SessionRun` / `TFE_Execute` 真正执行 C++ kernel 计算时，并不需要持有 GIL。这对宏在进入 C++ 计算前临时释放 GIL、计算结束后再重新获取，从而让**其他 Python 线程能在计算期间继续运行**，避免 C++ 重计算把整个 Python 进程卡死。这是 pywrap 层最重要的性能职责。

**练习 3**：图模式的 `tf_session`（即 `pywrap_tf_session.py`）和 Eager 的 `pywrap_tfe.py`，在职责上有什么共同点？

**参考答案**：两者都是「Python 再导出层」——都是用 `from ._pywrap_xxx import *` 把 pybind11 编译出的 `.so` 里的符号导入到正常 Python 包命名空间，并各自做一些 Python 侧的便捷包装（如 `pywrap_tf_session.py` 的 `TF_NewSessionOptions`、版本号转 `str`）。它们的对应 pybind11 注册文件分别是 `tf_session_wrapper.cc` 与 `pywrap_tfe_src.cc`。

## 5. 综合实践

**任务**：画一张「Python → pywrap → C API → C++ core」的全链路调用图，并分别针对**图模式**和 **Eager 模式**填入真实文件名与函数名。

要求：

1. 用方框标出四个层次（Python / pybind11 桥 / C API / C++ core）。
2. 图模式链路至少填入：`session.py:1483` → `pywrap_tf_session.py` → `tf_session_wrapper.cc:1357` → `tf_session_helper.cc:360` → `c_api.cc:2341 TF_SessionRun` → `DirectSession::Run`。
3. Eager 链路至少填入：`execute.py:53 quick_execute` → `pywrap_tfe.py` → `pywrap_tfe_src.cc:955 TFE_Py_ExecuteCancelable` → `eager/c_api.cc:924 TFE_Execute` → `ImmediateExecutionContext::Execute`。
4. 在图上用三种颜色（或三类标注）分别标出：①「Python 策略」、②「GIL/异常/类型转换」、③「真正计算」分别落在哪一层。
5. 在图旁写一句话总结：为什么要把这条链拆成这么多层？（提示：稳定性、跨语言、GIL、职责分离。）

完成后，对照本讲 4.3.2 的两张流程图自检，看是否遗漏了「释放 GIL」「`TF_Status` 翻译成 Python 异常」「numpy↔`TF_Tensor` 转换」这些关键动作。

## 6. 本讲小结

- **C API（`c/c_api.h`）是语言无关的稳定边界**：用「不透明指针 + `TF_Status` + `unsigned char` 布尔」这套风格，让任何能调 C 的语言都能驱动 TF，内部布局可自由演进而不破坏调用方。
- **不透明指针是壳**：`TF_Session` 内部就是包了一个 C++ `Session*`（见 `c_api_internal.h`），C API 函数都是在操作这个壳——`TF_NewSession` 调 C++ 工厂 `NewSession`，`TF_SessionRun` 把 `TF_Output`/`TF_Tensor` 翻译成字符串名/`Tensor` 后交给 `DirectSession::Run`。
- **存在两套 C API**：稳定的 `TF_`（图模式）与不稳定的 `TFE_`（Eager）。Eager 用 `TFE_Context`/`TFE_TensorHandle`/`TFE_Op` 三件套，`TFE_Execute` 把单个 op 立即派发到 C++ 运行时。
- **pywrap = pybind11 桥 + Python 再导出层**：`.cc` 用 `m.def(...)` 把 C 函数注册成 `.so`，`.py` 用 `import *` 接入命名空间并做便捷包装。
- **职责切分的三句口诀**：Python 做「策略」（选 op、喂数据），C 做「数据搬运」（指针转换、释放 GIL、`TF_Status` 翻译异常），C++ 做「真正的活」（放置、调度、执行 kernel）。
- **释放 GIL 是 pywrap 层的关键性能职责**：`Py_BEGIN_ALLOW_THREADS/Py_END_ALLOW_THREADS` 让 C++ 计算期间其他 Python 线程能继续运行。

## 7. 下一步学习建议

本讲打通了「Python 调用到 C++」的桥梁，接下来可以：

1. **u4-l5（Python op 包装与生成代码 gen_\*_ops）**：看 `math_ops.py`/`array_ops.py` 如何在 pywrap 之上再包一层手写包装，并了解 `gen_*_ops.py` 是怎么由 op 声明自动生成的——这将补全「从 `tf.add` 到 pywrap」之间最顶上的那一层。
2. **回看 u3-l2（Session 与 DirectSession）与 u3-l3（Eager 与 Context）**：现在你已经看过了桥梁，再回头看 C++ 那一侧的 `DirectSession::Run` 和 `ImmediateExecutionContext::Execute`，会把「整条链」彻底串通。
3. **进阶阅读**：若对跨语言绑定感兴趣，可对比 `tensorflow/python/client/tf_session_wrapper.cc`（pybind11 风格）与更早期的 SWIG 风格代码，理解 TF 为什么从 SWIG 迁移到 pybind11；也可阅读 `tensorflow/c/c_api_function.cc`，看 C API 如何把子图打包成可复用的 `TF_Function`。
