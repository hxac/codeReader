# u2-l4 GE 图模式调用算子

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `test_geir_add_example.cpp`，说出 GE 图模式调用算子的完整流程：初始化 → 构图 → 加图 → 执行 → 落盘 → 去初始化。
2. 理解 `op_graph/add_example_proto.h` 中 `REG_OP` 原型定义的作用，以及它为什么是图模式能"认识"一个算子的前提。
3. 对比图模式（geir）与单算子模式（aclnn）两种调用方式在代码结构、执行模型、适用场景上的差异，能根据业务场景选型。

## 2. 前置知识

本讲默认你已完成 u1-l4（AddExample 编译安装运行）和 u2-l1（aclnn 两段式接口）。在此基础上补充三个新概念：

- **GE（Graph Engine，图引擎）**：CANN 中负责管理计算图的组件。单算子模式下你直接调 `aclnnXxx` 把一个算子丢给设备；图模式下你先把若干算子和它们的连线组织成一张"计算图"（Graph），交给 GE 统一编译、优化、调度执行。
- **IR（Intermediate Representation，中间表示）**：算子在图中的"身份证"。`op_graph/*_proto.h` 里的 `REG_OP` 宏向 GE 注册了算子名、输入输出名、支持的数据类型等信息，GE 靠这份注册信息识别图中每个节点是什么算子、有几个输入几个输出。所以仓库文档里把这种调用方式称为 **geir 调用**（基于 GE IR 的构图调用）。
- **Session**：GE 提供的会话对象，相当于"图的运行容器"。你把构图加入 Session（`AddGraph`），再让 Session 执行它（`RunGraph`）。这一点和单算子模式下的 `aclrtStream`（任务流）是两套不同的执行入口。

一个直观类比：aclnn 模式像"点外卖"——每调用一次接口就下单执行一个算子；GE 图模式像"定制套餐"——先把整桌菜（整张图）配置好，GE 后厨统一排菜、优化上菜顺序，整体执行。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/add_example/examples/test_geir_add_example.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp) | GE 图模式调用 AddExample 的完整样例，本讲主线 |
| [examples/add_example/op_graph/add_example_proto.h](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_graph/add_example_proto.h) | AddExample 的算子 IR 原型定义（`REG_OP`），图模式识别算子的依据 |
| [examples/add_example/op_graph/add_example_graph_infer.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_graph/add_example_graph_infer.cpp) | 图模式下的 dtype 推导实现（`InferDataType`），本讲附带提及 |
| [examples/add_example/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp) | aclnn 单算子模式样例，用于对比 |
| [docs/zh/invocation/quick_op_invocation.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md) | 官方"GE图模式"调用文档（约 L430-L577），含 CMakeLists 与 run.sh 模板 |

## 4. 核心概念与源码讲解

本讲的最小模块有两个：

1. **算子 IR 原型：add_example_proto.h** —— 图模式如何"认识"一个算子。
2. **构图与执行：test_geir_add_example.cpp** —— 从 `main` 到 `RunGraph` 的完整链路，以及与 aclnn 的差异。

### 4.1 算子 IR 原型：add_example_proto.h

#### 4.1.1 概念说明

u1-l2 讲过：算子工程里的 `op_graph` 目录是"图模式交付件"，缺了它算子就不支持图模式调用。这个目录里最核心的就是 `*_proto.h` 原型文件。它回答三个问题：

- 算子叫什么名字（`AddExample`）；
- 有哪些输入/输出、各叫什么名字（`x1`、`x2`、`y`）；
- 每个输入输出支持哪些数据类型（`DT_FLOAT`、`DT_INT32`）。

注意第三点：proto 里声明的类型集合是 `{DT_FLOAT, DT_INT32}`，而样例只用了 `DT_FLOAT`。文件头部注释写的是 float32，与注册代码存在细微出入——这是阅读真实工程时常见的"注释滞后"现象，以 `REG_OP` 代码为准。

#### 4.1.2 核心流程

`REG_OP` 注册的信息在图模式下被消费的路径：

```text
REG_OP(AddExample) 注册原型
        │
        ▼
用户代码 op::AddExample("add1") 在图中创建算子节点
        │
        ▼
set_input_x1 / set_input_x2 连线（数据边）
        │
        ▼
GE 编译图时按原型做校验与推导（dtype 走 graph_infer，shape 走 op_host 的 Infershape）
        │
        ▼
GE 调度执行，实际落到 op_kernel 的实现上
```

#### 4.1.3 源码精读

原型定义本体非常短：

[examples/add_example/op_graph/add_example_proto.h:L35-L39](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_graph/add_example_proto.h#L35-L39)

```cpp
REG_OP(AddExample)
    .INPUT(x1, TensorType({DT_FLOAT, DT_INT32}))
    .INPUT(x2, TensorType({DT_FLOAT, DT_INT32}))
    .OUTPUT(y, TensorType({DT_FLOAT, DT_INT32}))
    .OP_END_FACTORY_REG(AddExample)
```

这段代码用链式宏向 GE 注册了 AddExample：两个输入 `x1`/`x2`、一个输出 `y`，类型均支持 float32 与 int32。`REG_OP`/`OP_END_FACTORY_REG` 是配对的开始/结束宏，中间用 `.INPUT()`/`.OUTPUT()` 声明端口。之后 `op::AddExample` 这个 C++ 类即可用于构图——样例代码 `#include "../op_graph/add_example_proto.h"` 后就能写 `op::AddExample("add1")`。

与之配套的 dtype 推导在 graph_infer 文件中：

[examples/add_example/op_graph/add_example_graph_infer.cpp:L24-L36](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_graph/add_example_graph_infer.cpp#L24-L36)

```cpp
static ge::graphStatus InferDataTypeAddExample(gert::InferDataTypeContext* context)
{
    ge::DataType sizeDtype = context->GetInputDataType(IDX_0);
    context->SetOutputDataType(IDX_0, sizeDtype);
    return GRAPH_SUCCESS;
}
IMPL_OP(AddExample).InferDataType(InferDataTypeAddExample);
```

规则很简单：输出 `y` 的 dtype 跟随第 0 个输入 `x1`。`IMPL_OP` 把这个推导函数绑定到算子上，GE 编译图时会回调它。u6-l1 将详细展开 op_graph 三件套，本讲只需知道"图模式下算子能被识别和推导，靠的就是这两个文件"。

#### 4.1.4 代码实践

1. **实践目标**：确认 proto 原型与 aclnn 接口参数的对应关系。
2. **操作步骤**：
   - 打开 [examples/add_example/README.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/README.md) 中的接口参数表；
   - 对照 proto.h 的 `INPUT(x1)`、`INPUT(x2)`、`OUTPUT(y)`；
   - 再对照 aclnn 样例里 `aclnnAddExampleGetWorkspaceSize(selfX, selfY, out, ...)` 的参数顺序。
3. **需要观察的现象**：三处对"两个输入、一个输出"的描述在名字和顺序上一一对应。
4. **预期结果**：proto 的端口声明、README 参数表、aclnn 接口形参三者构成同一份契约，这正是 u1-l4 结尾提到的"三者对应关系"的具体体现。
5. 本实践为纯源码阅读，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `REG_OP` 中 `.INPUT(x2, ...)` 的端口名改成 `.INPUT(y2, ...)`，样例代码里哪一行会编译失败？

答案：`test_geir_add_example.cpp` 中宏 `ADD_INPUT` 展开出的 `add1.set_input_x2(placeholder2)` 一行（宏定义见 [test_geir_add_example.cpp:L55](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L55)，`set_input_##intputName` 中的名字来自调用处传入的 `x2`）。`op::AddExample` 的 setter 由 proto 生成，端口改名后 `set_input_x2` 不复存在。

**练习 2**：为什么 `add_example_proto.h` 放在 `op_graph/` 目录而不是 `op_api/`？

答案：proto 是给 GE 图引擎看的注册信息，属于图模式交付件；`op_api` 目录放的是 aclnn 接口实现。u1-l2 讲过"缺 op_graph 不支持图模式"——没有这份 proto，`op::AddExample` 类根本不存在，图模式无从构图。

### 4.2 构图与执行：test_geir_add_example.cpp

#### 4.2.1 概念说明

这个样例演示了 GE 图模式调用的"十步骨架"：创建图 → GE 初始化 → 构造算子节点与数据节点 → 连线 → 设置图输入输出 → 创建 Session → AddGraph → RunGraph → 结果落盘 → GEFinalize。与 aclnn 样例的"九步骨架"（Init → 构造 Tensor → 两段调用 → 同步 → 拷回 → 清理）相比，最本质的区别是：

- aclnn 模式下**没有图的概念**，每个算子是一次独立的函数调用，靠 `aclOpExecutor` 记账、靠 stream 保序；
- 图模式下**一次 `RunGraph` 执行整张图**，GE 内部完成算子调度、内存规划与执行保序，用户拿到的是一组输出 `ge::Tensor`。

另外注意数据摆放位置的区别：aclnn 模式需要手动 `aclrtMalloc` + `aclrtMemcpy` 把数据搬到 device；这个样例把输入 `TensorDesc` 设为 `kPlacementHost`（Host 侧放置，见宏里 [test_geir_add_example.cpp:L43](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L43)），数据搬运由 GE 托管，省去了显式的 D2H/H2D 代码。

#### 4.2.2 核心流程

`main` 函数（[test_geir_add_example.cpp:L187-L293](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L187-L293)）的执行流程：

```text
1. Graph graph("tc_ge_irrun_test")            创建图对象
2. GEInitialize(global_options)               初始化 GE（指定 deviceId、graphRunMode=1）
3. CreateOppInGraph(...)                       构图：
   ├─ op::AddExample("add1")                   创建算子节点
   ├─ ADD_INPUT(1, x1, ...)                    创建 op::Data 数据节点 + 全 1 输入数据
   ├─ ADD_INPUT(2, x2, ...)                    同上，连到第二个输入
   └─ ADD_OUTPUT(1, y, ...)                    设置输出 desc
4. graph.SetInputs(inputs).SetOutputs(outputs) 声明图的边界
5. new Session(build_options)                  创建会话
6. session->AddGraph(graph_id, graph, opts)    把图加入会话
7. aclgrphDumpGraph(graph, "./dump", ...)      （可选）把图导出为 txt 便于调试
8. session->RunGraph(graph_id, input, output)  同步执行整图，输出填入 output
9. 输入/输出数据逐个写成 .bin 文件
10. GEGetErrorMsgV2/GEGetWarningMsgV2          取回错误/警告信息
11. GEFinalize()                               去初始化
```

其中构图核心 `CreateOppInGraph` 在 [test_geir_add_example.cpp:L170-L185](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L170-L185)：

```cpp
auto add1 = op::AddExample("add1");
std::vector<int64_t> xShape = {32, 4, 4, 4};
ADD_INPUT(1, x1, inDtype, xShape);
ADD_INPUT(2, x2, inDtype, xShape);
ADD_OUTPUT(1, y, inDtype, xShape);
outputs.push_back(add1);
```

这张图只有三个节点：两个 `op::Data`（占位输入）+ 一个 `AddExample` 算子节点，是"单算子图"。但骨架对多算子图完全通用——再 `ADD_INPUT`、再 `set_input_xxx` 连线即可扩展成真正的网络子图。

#### 4.2.3 源码精读

**（1）输入构造宏 `ADD_INPUT`**

[test_geir_add_example.cpp:L38-L56](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L38-L56)

```cpp
#define ADD_INPUT(intputIndex, intputName, intputDtype, inputShape) ...
    auto placeholder##intputIndex = op::Data("placeholder" + intputIndex).set_attr_index(0);
    TensorDesc placeholder##intputIndex##_desc = TensorDesc(ge::Shape(...), FORMAT_ND, intputDtype);
    placeholder##intputIndex##_desc.SetPlacement(ge::kPlacementHost);
    ret = GenOnesData(..., tensor_placeholder##intputIndex, ..., 2);   // 生成全 2 的数据
    ...
    placeholder##intputIndex.update_input_desc_x(placeholder##intputIndex##_desc);
    input.push_back(tensor_placeholder##intputIndex);   // 作为 RunGraph 的输入数据
    graph.AddOp(placeholder##intputIndex);              // 节点加入图
    add1.set_input_##intputName(placeholder##intputIndex);  // 连线：Data -> AddExample 输入
```

这个宏做了四件事：创建 `op::Data` 节点、生成填充值的数据（`GenOnesData` 最后一参为 2，即每个元素都是整数 2）、把数据挂进 `input` 向量（供 `RunGraph` 使用）、用 `set_input_x1`/`set_input_x2` 建立数据边。与之平行的 `ADD_CONST_INPUT`（[L58-L77](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L58-L77)）则创建 `op::Const` 常量节点——常量在编译期固化进图，不占图的输入口。本样例只用了 `ADD_INPUT`。

**（2）GE 初始化**

[test_geir_add_example.cpp:L194-L195](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L194-L195)

```cpp
std::map<AscendString, AscendString> global_options = {{"ge.exec.deviceId", "0"}, {"ge.graphRunMode", "1"}};
Status ret = ge::GEInitialize(global_options);
```

`ge.exec.deviceId` 指定设备号，`ge.graphRunMode=1` 表示图在 NPU 上运行（0 为仅 CPU 预演）。GE 的初始化/去初始化（`GEInitialize`/`GEFinalize`）对应 aclnn 模式的 `aclInit`/`aclFinalize`，但两者是不同组件的入口，不可混用。

**（3）Session 三连：创建、加图、执行**

[test_geir_add_example.cpp:L226-L247](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L226-L247)

```cpp
ge::Session* session = new Session(build_options);
uint32_t graph_id = 0;
ret = session->AddGraph(graph_id, graph, graph_options);
ret = session->RunGraph(graph_id, input, output);
```

这是图模式的执行核心。`AddGraph` 把构图交给 Session（GE 此后可对其做编译优化），`RunGraph` 同步执行：传入第一步准备好的 `input` 张量，返回时输出已填入 `output`。注意这里是**同步语义**，不需要像 aclnn 模式那样显式 `aclrtSynchronizeStream`。执行前还有一行值得记住的调试技巧：

[test_geir_add_example.cpp:L243-L244](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L243-L244)

```cpp
std::string file_path = "./dump";
aclgrphDumpGraph(graph, file_path.c_str(), file_path.length());
```

`aclgrphDumpGraph` 把当前图导出为文本，用于确认"我构的图和我想的一样"——排查图模式问题的第一手段。

**（4）结果落盘**

[test_geir_add_example.cpp:L267-L276](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L267-L276)

```cpp
string output_file = "./tc_ge_irrun_test_0008_npu_output_" + std::to_string(i) + ".bin";
uint8_t* output_data_i = output[i].GetData();
uint32_t data_size = output_shape * GetDataTypeSize(output[i].GetTensorDesc().GetDataType());
WriteDataToFile((const char*)output_file.c_str(), data_size, output_data_i);
```

与 aclnn 样例直接 `printf` 打印数值不同，这里把输出原样写成 `.bin` 文件（输入也各写一份）。好处是便于用 numpy/Python 离线比对精度——这也是 ST 精度验证常用的产物形态。

**（5）一个值得注意的细节**

[test_geir_add_example.cpp:L205](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L205) 直接 `std::cout << argv[1]`，但 `main` 并未检查 `argc`。若不带参数运行，`argv[1]` 为空指针，行为未定义。实际运行时建议带一个参数（如 `./test_geir_add_example 0`）。官方文档 run.sh 模板中未传参，此差异「待本地验证」。

#### 4.2.4 代码实践

**实践：运行 geir 样例，与 aclnn 样例做量化对比**

1. **实践目标**：跑通图模式调用 AddExample，并用真实数据对比 geir 与 aclnn 两种样例的代码规模和调用步骤。

2. **操作步骤**（在已完成 u1-l4 的算子包编译安装的前提下）：

   ```bash
   # 方式一：仓库脚本一键编译运行（graph 模式会自动选择 test_geir_add_example.cpp）
   bash build.sh --run_example add_example graph cust --vendor_name=custom --soc=${soc_version}

   # 方式二：按 docs/zh/invocation/quick_op_invocation.md「GE图模式」一节
   # 自建 cpp + CMakeLists.txt + run.sh 工程运行（文档约 L430-L577 有完整模板）
   ```

   运行结束后在可执行文件所在目录检查生成的 `./dump` 图描述文件与 `tc_ge_irrun_test_0008_npu_input_*.bin`、`tc_ge_irrun_test_0008_npu_output_*.bin`。

3. **需要观察的现象**：
   - 日志依次输出 `Initialize ge ... success`、`Session add ir compute graph`、`Run graph`、`Finalize ir graph session success`；
   - 生成的 output bin 文件大小 = 32×4×4×4×4 字节（float32）；
   - 用 Python 读取 output bin：`np.fromfile("tc_ge_irrun_test_0008_npu_output_0.bin", dtype=np.float32)`，应为全 4.0（输入是全 2，加法结果 2+2=4；注意 `GenOnesData` 按整型生成再按 dtype 解释，float 下恰为 2.0，「具体值待本地验证」）。

4. **对比与记录**（本实践的量化部分）：

   | 对比项 | test_aclnn_add_example.cpp | test_geir_add_example.cpp |
   | --- | --- | --- |
   | 总行数 | 约 162 行 | 约 293 行 |
   | 执行模型 | 一次调用一个算子，stream 异步 | 一张图一次 RunGraph，同步 |
   | 手动内存管理 | aclrtMalloc/Memcpy/Free 全显式 | 输入 Host 放置，GE 托管搬运 |
   | 结果获取 | D2H 拷贝后 printf | 写 .bin 文件 |
   | 额外能力 | 无 | aclgrphDumpGraph 导出图、Const 节点 |

   把两种方式各自的调用步骤数一遍并记入笔记。

5. **预期结果**：geir 样例运行成功、bin 产物正确；对比表得出结论——单算子场景 aclnn 更简洁，多算子组网场景 geir 更省事且可被 GE 整图优化。若无法在本地环境运行，以上运行现象标注为「待本地验证」，量化对比部分（行数、步骤）纯靠源码统计即可完成。

#### 4.2.5 小练习与答案

**练习 1**：geir 样例里没有出现 `aclrtSynchronizeStream`，为什么结果是"就绪"的？

答案：`Session::RunGraph` 本身是同步接口——调用返回时整张图已执行完毕，输出数据已填入 `output` 向量。而 aclnn 第二段接口是异步下发到 stream，必须显式同步。这是两种执行模型的核心差异之一。

**练习 2**：想在图中再加一个 AddExample 节点（`add2`），让 `add1` 的输出作为 `add2` 的一个输入，至少要写哪几行？

答案（示例代码，基于现有骨架扩展）：

```cpp
auto add2 = op::AddExample("add2");
// add1 的输出直接连到 add2 的 x1（示例代码）
add2.set_input_x1(add1.out_y);
// add2 的另一个输入仍用 ADD_INPUT 宏生成一个 Data 节点
graph.AddOp(add2);
outputs.push_back(add2);   // 图的输出改为 add2
```

关键点：算子间连线用 `out_y`（proto 中 `OUTPUT(y)` 生成的取出口），不需要中间 Data 节点；`outputs` 改为收集 `add2` 才能把新末梢暴露为图输出。

**练习 3**：aclnn 样例支持运行时根据 `--soc` 校验芯片，geir 样例中与之对应的"运行在哪"是由什么决定的？

答案：由 `GEInitialize` 的全局选项 `ge.exec.deviceId`（设备号）与 `ge.graphRunMode=1`（NPU 上运行）共同决定，见 [test_geir_add_example.cpp:L194](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_geir_add_example.cpp#L194)。具体算子跑在哪个芯片版本上，由已安装算子包的编译目标与当前设备共同决定（编译与运行环境须一致，同 u1-l4 的结论）。

## 5. 综合实践

**任务：给 AddExample 的 geir 样例做一次"图模式排障演练"。**

在不修改算子源码的前提下，完成三件事：

1. 复制 `test_geir_add_example.cpp` 为 `test_geir_add_example_my.cpp`（放在自建工程目录，不动仓库文件），把 `GenOnesData` 的填充值从 2 改为 5，运行后用 Python 读 output bin，确认结果为全 10.0。
2. 运行时打开 `./dump` 目录下的图描述文本，找到 `AddExample` 节点，记下它的输入输出端口名，与 `add_example_proto.h` 的 `REG_OP` 声明逐项核对。
3. 把 `main` 中 `session->RunGraph(...)` 的返回值刻意改判为失败分支（例如临时把 `graph_id` 改成 1），观察 `GEGetErrorMsgV2` 输出的错误信息格式，体会图模式错误如何回传。

预期产出：一份包含 output bin 读取脚本、dump 图节点摘录、错误信息截图/文本的排障记录。若本机无 NPU 环境，第 1、3 步「待本地验证」，第 2 步可通过阅读 dump 相关逻辑完成静态分析。

## 6. 本讲小结

- 图模式（geir）调用基于 `op_graph/*_proto.h` 的 `REG_OP` 原型注册，GE 靠它识别算子的名字、端口与类型；dtype 推导由 `*_graph_infer.cpp` 的 `InferDataType` 回调完成。
- geir 样例骨架：Graph → GEInitialize → 构图（`op::Data`/`op::Const` + 算子节点连线）→ SetInputs/SetOutputs → Session → AddGraph → RunGraph（同步）→ 结果写 bin → GEFinalize。
- 与 aclnn 的核心差异：图模式一次执行整张图、同步语义、数据可 Host 放置由 GE 托管；单算子模式逐个调用、异步 stream、显式内存管理。
- `aclgrphDumpGraph` 是图模式排障第一工具；结果落盘为 bin 便于离线精度比对。
- 单算子验证场景优选 aclnn；多算子组网、需要整图优化或框架图执行的场景优选 geir。

## 7. 下一步学习建议

本讲完成了"算子调用方式"单元的最后一讲。接下来进入第三单元 Host 侧主链路：

- 下一讲 **u3-l1（一个算子的完整解剖：resize_bilinear_v2 全景）**：把 aclnn → op_host（def/infershape/tiling）→ op_kernel 的完整调用链画成地图，本讲的 geir/aclnn 入口正是这张地图的起点。
- 若想先深挖图模式交付件，可提前阅读 **u6-l1（op_graph：算子原型定义与图模式识别）**，本讲的 proto/graph_infer 只是它的预热。
- 建议同步源码阅读：[examples/add_example/op_graph/](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_graph/add_example_proto.h) 目录下三个文件的配合关系。
