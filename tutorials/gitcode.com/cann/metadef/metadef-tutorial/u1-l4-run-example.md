# 官方示例解读与运行

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `example/run_example.sh` 的执行流程，知道它依赖哪些前置环境。
2. 读懂 `example/tc_ge_irrun_open_api_0014/Makefile`，说出示例程序包含了哪些头文件目录、链接了哪些库，其中哪些库来自 metadef。
3. 读懂示例 `tc_ge_irrun_open_api_0014.cpp`，区分哪些 API 来自 metadef（`ge::Tensor`、`TensorDesc`、`DataType`、`AscendString`、`gert::Tensor` 等），哪些来自 ge 与 acl。
4. 有昇腾环境时跑通示例；没有环境时也能写出完整的预期执行流程。

## 2. 前置知识

- **ge（Graph Engine）**：CANN 的图引擎，负责把用户构造的计算图编译并调度到昇腾设备上执行。metadef 为 ge 提供基础数据结构，ge 提供建图与执行的完整 API（如 `ge::GEInitialize`、`ge::Session`）。
- **acl（Ascend Computing Language）**：昇腾计算语言的运行时部分，负责设备管理、内存分配（`aclrtMalloc`）、数据搬运（`aclrtMemcpy`）和流同步（`aclrtSynchronizeStream`）。
- **`ge::Tensor` 与 `gert::Tensor`**：两套张量描述。`ge::Tensor`（老 Graph 编译体系，宿主侧）用于建图时描述输入数据；`gert::Tensor`（exe_graph 运行时新体系，设备侧）用于图执行时描述设备上的输入输出。单元三会深入 gert 体系。
- **算子原型头文件**：`nonlinear_fuc_ops.h`、`array_ops.h` 这类头文件由算子仓生成，定义了 `op::Relu`、`op::Data` 等 C++ 算子包装类，让用户能用链式语法建图。
- **`ASCEND_HOME_PATH` / `set_env.sh`**：CANN 安装后通过 `set_env.sh` 导出环境变量，编译和运行时据此找到头文件和动态库。第 u1-l2 讲已接触过这个机制。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [example/run_example.sh](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/run_example.sh) | 示例的一键运行脚本：source 环境、make 编译、运行二进制 |
| [example/tc_ge_irrun_open_api_0014/Makefile](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/tc_ge_irrun_open_api_0014/Makefile) | 示例的编译配置：include 路径与链接库清单 |
| [example/tc_ge_irrun_open_api_0014/tc_ge_irrun_open_api_0014.cpp](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/tc_ge_irrun_open_api_0014/tc_ge_irrun_open_api_0014.cpp) | 示例主程序：构造 ReLU 图 → 编译 → 在 NPU 上执行 → 落盘输出 |
| [inc/external/graph/tensor.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h) | metadef 中 `ge::Tensor` / `TensorDesc` 的声明（示例宿主侧数据结构） |
| [inc/external/exe_graph/runtime/runtime_tensor.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h) | metadef 中 `gert::Tensor` 的声明（示例设备侧数据结构） |
| [inc/external/graph/types.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h) | metadef 中 `DataType` / `Format` 枚举（示例大量使用 `DT_FLOAT16`、`FORMAT_NHWC`） |
| [inc/external/graph/ascend_string.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/ascend_string.h) | metadef 中 `AscendString` 的声明（示例用它构造 ge 的 options 映射） |

注意：示例中还包含了 `graph.h`、`ge_api.h`、`ge_ir_build.h`、`nonlinear_fuc_ops.h`、`nn_calculation_ops.h`、`array_ops.h`、`acl.h` 等头文件，这些**不在 metadef 仓库内**，它们来自 CANN 安装目录（ge 仓库与 acl 组件），编译时从 `$ASCEND_PATH` 下的 include 目录取到。这正是第 u1-l1 讲强调的依赖方向：示例 → ge/acl → metadef。

## 4. 核心概念与源码讲解

本讲的两个最小模块是：①`example/run_example.sh`（含 Makefile）——示例如何被构建和运行；②`example/tc_ge_irrun_open_api_0014/tc_ge_irrun_open_api_0014.cpp`——metadef 接口在真实工程中如何被调用。

### 4.1 run_example.sh 与 Makefile：示例的构建与运行入口

#### 4.1.1 概念说明

metadef 是一个基础库，自身没有可执行入口；它的使用方式是「被别的工程包含头文件并链接动态库」。example 目录演示了这个过程：一个独立的小工程，通过 CANN 安装目录中的头文件和库（其中含 metadef 产出的 `libmetadef.so`）编译出一个 `.bin` 可执行文件，在真实的昇腾设备上跑一个 ReLU 图。

#### 4.1.2 核心流程

`run_example.sh` 的执行流程：

```text
1. source /usr/local/Ascend/cann/set_env.sh   # 导出 CANN 环境变量
2. cd tc_ge_irrun_open_api_0014; make clean; make  # 编译出 ../tc_ge_irrun_open_api_0014.bin
3. mkdir tc_ge_irrun_open_api_0014_npu_output_      # 创建输出目录
4. find . -print | sed ...                          # 打印目录树（方便查看产物）
5. cd ..; ./tc_ge_irrun_open_api_0014.bin           # 运行示例
```

#### 4.1.3 源码精读

**运行脚本**：[example/run_example.sh:11-20](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/run_example.sh#L11-L20) —— 先 source CANN 的 `set_env.sh` 导出环境（脚本注释「换成你自己的Ascend安装目录」说明这是唯一需要按机器修改的地方），然后进入示例目录 `make clean; make`，创建输出目录，最后回到 example 根目录执行编译出的 `tc_ge_irrun_open_api_0014.bin`。

**Makefile 的头文件目录**：[example/tc_ge_irrun_open_api_0014/Makefile:14-27](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/tc_ge_irrun_open_api_0014/Makefile#L14-L27) —— include 路径全部来自 CANN 安装目录 `$ASCEND_PATH`：算子原型头文件（`opp/built-in/op_graph/inc`，提供 `op::Relu` 等）、框架头文件（`$(ARCH)-linux/include` 及其 `graph`、`ge` 子目录，提供 `graph.h`、`ge_api.h`）、acl 头文件（`include/acl`）和编译器头文件（`compiler/include`，metadef 发布头文件随 CANN 包安装后也从此处可被找到）。这印证了第 u1-l3 讲的结论：metadef 对外头文件按「安装目录 + 子路径」组织。

**Makefile 的链接库**：[example/tc_ge_irrun_open_api_0014/Makefile:29-40](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/tc_ge_irrun_open_api_0014/Makefile#L29-L40) —— 链接了 `-lascendcl`（acl 运行时）、`-lgraph` / `-lge_runner` / `-lge_compiler`（ge 图引擎）、`-lgraph_base`、`-lmetadef`（**metadef 产出的核心库**，即第 u1-l2 讲中 build.sh 打出的四个产物之一）、`-lruntime`、`-lge_common_base`。注意 `-lmetadef` 出现在链接清单里，说明示例直接用到了 metadef 符号（如 `ge::Tensor`、`AscendStringImpl`）。

**ABI 标志**：[example/tc_ge_irrun_open_api_0014/Makefile:18](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/tc_ge_irrun_open_api_0014/Makefile#L18) —— `CFLAGS` 中 `-D_GLIBCXX_USE_CXX11_ABI=0` 使用旧版 libstdc++ ABI，这是与 CANN 预编译库保持二进制兼容的必要开关：如果宿主编译器默认使用新 ABI 而 CANN 库用旧 ABI 编译，链接期或运行期都会出错。

**编译规则**：[example/tc_ge_irrun_open_api_0014/Makefile:42-45](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/tc_ge_irrun_open_api_0014/Makefile#L42-L45) —— 用 `wildcard` 收集当前目录所有 `.cpp`，一条 `g++` 命令编译并输出到上级目录的 `../tc_ge_irrun_open_api_0014.bin`；`clean` 同时删除二进制和编译期产生的 `kernel_meta` 目录（ge 编译图时缓存的算子元数据）。

#### 4.1.4 代码实践

1. **实践目标**：不运行任何东西，仅通过阅读 Makefile，说出示例依赖的全部链接库和 include 目录，并标注哪个库来自 metadef 仓库。
2. **操作步骤**：
   - 打开 Makefile，把 `INCLUDES` 中 6 个目录抄下来；
   - 把 `LIBS` 中 8 个 `-l` 项分类：acl 侧 / ge 侧 / metadef 侧 / runtime 侧；
   - 对照第 u1-l2 讲的产物清单（`exe_graph`、`opp_registry`、`rt2_registry_static`、`metadef`），确认 `-lmetadef` 对应哪个产物。
3. **需要观察的现象**：这是一次纯阅读实践，观察的是「一个使用 metadef 的最小工程需要哪些外部依赖」。
4. **预期结果**：能得出结论——示例的最小依赖是 `set_env.sh` 导出的 CANN 安装目录 + `-lmetadef` 等动态库；缺了 `set_env.sh`，编译期会找不到 `graph.h` 等头文件。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `run_example.sh` 里的 `source /usr/local/Ascend/cann/set_env.sh` 一行删掉，最先在哪一步失败？

**答案**：在 `make` 步骤失败。头文件搜索路径写的是绝对路径 `/usr/local/Ascend/cann/...`，source 与否不影响头文件查找；真正受影响的是链接和运行——`-L` 路径虽是绝对的，但运行 `./tc_ge_irrun_open_api_0014.bin` 时动态链接器找不到 `libascendcl.so` 等动态库（`LD_LIBRARY_PATH` 未设置），在程序启动阶段报 `cannot open shared object file`。（若头文件路径也依赖环境变量则编译期就会失败；本例 Makefile 用绝对路径，所以推迟到运行期。）

**练习 2**：Makefile 里 `-D_GLIBCXX_USE_CXX11_ABI=0` 去掉后可能发生什么？

**答案**：示例代码若把 `std::map<std::string,...>` 等带 std 类型的对象传给 CANN 库中用旧 ABI 编译的接口，会出现未定义符号或未定义行为；metadef 对外接口刻意用 `AscendString`、`const char_t*` 等类型规避 std 类型跨界（见第 u2-l2 讲），但 ge/acl 侧接口并非全部如此，保持与预编译库一致的 ABI 开关是稳妥做法。

### 4.2 tc_ge_irrun_open_api_0014.cpp：metadef 接口的真实调用

#### 4.2.1 概念说明

这个示例程序完成一件事：用 ge 的 IR（Intermediate Representation）接口在宿主侧构造一个 `Data → Relu` 的计算图，编译后放到 NPU 上执行，把输出落盘成 bin 文件。整个流程里 metadef 提供的「词汇表」随处可见：`DataType`/`Format` 枚举、`TensorDesc`/`ge::Tensor`（建图侧）、`AscendString`（选项映射的键值类型）、`gert::Tensor`（执行侧的设备张量）。理解这个示例等于同时复习了第 u1-l1 讲的「metadef 四类核心功能」中的三类。

#### 4.2.2 核心流程

`main` 的执行序列（对应源码 [tc_ge_irrun_open_api_0014.cpp:400-439](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/tc_ge_irrun_open_api_0014/tc_ge_irrun_open_api_0014.cpp#L400-L439)）：

```text
InitGE()                    # ge::GEInitialize(global_options)   —— ge API，选项用 AscendString 映射
BuildReluGraph()            # 构造 Data→Relu 图，生成 ge::Tensor 宿主输入 —— metadef 数据结构
CreateAndAddGraphToSession()# Session::AddGraph + CompileGraph   —— ge API
InitACL()                   # aclInit / aclrtSetDevice / aclrtCreateStream —— acl API
ExecuteGraphAndSaveOutput() # 加载图 → 搬输入到设备 → 异步执行 → 回拷输出 —— 三方协作
Cleanup()                   # GEFinalize + aclFinalize
```

其中 `ExecuteGraphAndSaveOutput` 内部（[tc_ge_irrun_open_api_0014.cpp:328-378](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/tc_ge_irrun_open_api_0014/tc_ge_irrun_open_api_0014.cpp#L328-L378)）又分为：

```text
GeSessionLoadGraph(sess, graph_id, options, stream)          # ge：把编译好的图加载进设备
PrepareInputTensors(input, input_device, stream)             # ge::Tensor → aclrtMalloc/Memcpy → gert::Tensor
分配输出 gert::Tensor（挂到预先 aclrtMalloc 的设备 buffer）
GeSessionExecuteGraphWithStreamAsync(sess, id, stream, in, out)  # ge：异步执行
aclrtSynchronizeStream → aclrtMemcpy 回拷 → 写文件
```

#### 4.2.3 源码精读

**头文件包含**：[tc_ge_irrun_open_api_0014.cpp:18-31](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/tc_ge_irrun_open_api_0014/tc_ge_irrun_open_api_0014.cpp#L18-L31) —— 14 个业务头文件中，来自 metadef 仓库的有 5 个：`types.h`（DataType/Format）、`tensor.h`（ge::Tensor/TensorDesc）、`error_codes.h`（graph 错误码）、`ge_api_types.h`（Status 等公共类型）、`ascend_string.h`（AscendString）。`graph.h`、`ge_api.h`、`ge_ir_build.h`、算子原型（`nonlinear_fuc_ops.h`、`nn_calculation_ops.h`、`array_ops.h`）与 acl 三件套来自 CANN 其他组件。

**ge 初始化（AscendString 的用法）**：[tc_ge_irrun_open_api_0014.cpp:189-200](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/tc_ge_irrun_open_api_0014/tc_ge_irrun_open_api_0014.cpp#L189-L200) —— `std::map<AscendString, AscendString>` 作为 `ge::GEInitialize` 的选项容器。注意这里没有用 `std::map<std::string, std::string>`：`AscendString` 是 metadef 定义的跨 ABI 安全字符串（声明见 [inc/external/graph/ascend_string.h:22](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/ascend_string.h#L22)，构造函数接收 `const char_t*`，见 [inc/external/graph/ascend_string.h:28](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/ascend_string.h#L28)）。这是第 u2-l2 讲的伏笔。

**建图与宿主侧 Tensor**：[tc_ge_irrun_open_api_0014.cpp:202-243](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/tc_ge_irrun_open_api_0014/tc_ge_irrun_open_api_0014.cpp#L202-L243) —— `BuildReluGraph` 用 `TensorDesc(ge::Shape(...), FORMAT_NHWC, DT_FLOAT16)` 描述输入（`DT_FLOAT16`、`FORMAT_NHWC` 即 metadef [inc/external/graph/types.h:81](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L81) 的 `DataType` 枚举与 [inc/external/graph/types.h:192](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L192) 的 `Format` 枚举），再由辅助函数 `GenOnesData` 构造 `ge::Tensor`。构造用的三参构造函数声明在 metadef 的 [inc/external/graph/tensor.h:154](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h#L154)：`Tensor(const TensorDesc &tensor_desc, const uint8_t *data, size_t size)` —— 描述符 + 裸数据指针 + 字节数，Tensor 不拥有这块内存（示例中内存来自局部 `std::vector` 或 `new[]`）。

**TensorDesc 的查询与回写**：[tc_ge_irrun_open_api_0014.cpp:224-234](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/tc_ge_irrun_open_api_0014/tc_ge_irrun_open_api_0014.cpp#L224-L234) —— 先 `GetInputDescByName("x")` / `GetOutputDescByName("y")` 取出 Relu 算子的输入输出描述，设置 `SetOriginFormat` / `SetFormat` 后再 `UpdateInputDesc` / `UpdateOutputDesc` 写回。这套「取描述 → 改属性 → 写回」是 ge 建图的标准套路，而 `TensorDesc` 本身是 metadef 类型（`SetRealDimCnt`、`GetFormat`、`GetDataType` 等接口见 [inc/external/graph/tensor.h:100-117](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h#L100-L117)）。

**设备侧张量 gert::Tensor**：[tc_ge_irrun_open_api_0014.cpp:300-326](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/tc_ge_irrun_open_api_0014/tc_ge_irrun_open_api_0014.cpp#L300-L326) —— `PrepareInputTensors` 把宿主侧 `ge::Tensor` 翻译成设备侧 `gert::Tensor`：先用 `aclrtMalloc` / `aclrtMemcpy` 把数据搬到设备，再用一行聚合初始化 `input_device[i] = {storage_shape, storage_format, gert::kOnDeviceHbm, input_dtype, inputdevBuffer}`。这五个字段分别来自 metadef 的 `gert::StorageShape`（[inc/external/exe_graph/runtime/storage_shape.h:17](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/storage_shape.h#L17)）、`gert::StorageFormat`（[inc/external/exe_graph/runtime/storage_format.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/storage_format.h)）、`TensorPlacement` 枚举值 `kOnDeviceHbm`（[inc/external/exe_graph/runtime/tensor_data.h:24](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tensor_data.h#L24)，表示数据位于 Device 的 HBM 内存）、`DataType` 和设备 buffer 指针。`gert::Tensor` 的对应构造函数声明在 [inc/external/exe_graph/runtime/runtime_tensor.h:38-41](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L38-L41)。

> 一个小知识点：示例第 20 行包含的是 `"tensor.h"`（经 CANN 安装目录解析到 ge 侧的 tensor.h）。metadef 自己的 `inc/external/exe_graph/runtime/tensor.h` 现在是一个弃用转发头，直接 `#include "runtime_tensor.h"` 并带 `#pragma message` 警告（见 [inc/external/exe_graph/runtime/tensor.h:13-19](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tensor.h#L13-L19)），这正是第 u1-l3 讲提到的「pkg 弃用路径兼容转发」机制的一个实例。

**输出侧与收尾**：[tc_ge_irrun_open_api_0014.cpp:344-377](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/tc_ge_irrun_open_api_0014/tc_ge_irrun_open_api_0014.cpp#L344-L377) —— 输出同样用聚合初始化构造 `gert::Tensor` 挂到预分配的设备 buffer；执行成功后 `aclrtSynchronizeStream` 等待完成，`GetData<uint8_t>()` / `GetSize()`（模板取数接口见 [inc/external/exe_graph/runtime/runtime_tensor.h:69-78](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L69-L78)，字节数接口见 [inc/external/exe_graph/runtime/runtime_tensor.h:114-115](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h#L114-L115)）读回数据写文件。`Cleanup`（[tc_ge_irrun_open_api_0014.cpp:380-398](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/example/tc_ge_irrun_open_api_0014/tc_ge_irrun_open_api_0014.cpp#L380-L398)）按 ge → acl 的逆序做 `GEFinalize` / `aclFinalize`。

#### 4.2.4 代码实践

1. **实践目标**：梳理示例用到的 metadef 头文件与关键 API 清单；有昇腾环境则实际跑通，无环境则写出完整预期执行流程。
2. **操作步骤**：
   - 通读 `tc_ge_irrun_open_api_0014.cpp`，完成下面这张表（示例代码，请自行填写右列）：

     | metadef 头文件 | 示例中用到的类型/接口 | 出现的函数 |
     | --- | --- | --- |
     | types.h | `DataType`、`Format`（`DT_FLOAT16`、`FORMAT_NHWC`） | `GenOnesData`、`BuildReluGraph` |
     | tensor.h | `TensorDesc`、`ge::Tensor`、`ge::Shape` | ... |
     | ascend_string.h | `AscendString` | ... |
     | ge_api_types.h | `Status` / `SUCCESS` | ... |
     | runtime_tensor.h（gert 体系） | `gert::Tensor`、`StorageShape`、`StorageFormat`、`kOnDeviceHbm` | ... |

   - 有昇腾环境时：确认 `/usr/local/Ascend/cann` 存在（或修改 `run_example.sh` 第 12 行与 Makefile 第 10 行为自己的安装路径），执行 `bash example/run_example.sh`，观察终端上 `[IR run log]` / `[ACL run log]` 前缀的日志依次出现，最后在 `example/tc_ge_irrun_open_api_0014/tc_ge_irrun_open_api_0014_npu_output_/` 下得到输出 bin 文件。**待本地验证**（本讲义编写环境无昇腾设备，未实际运行）。
   - 无环境时：写出预期执行流程并对照源码逐条标注行号（见下面「预期结果」）。
3. **需要观察的现象**：日志按 `Initialize ge → Add relu op → AddGraph/CompileGraph → acl init → LoadGraph → memcpy → ExecuteGraph → sync → Finalize` 的顺序推进；任何一步失败都会打印对应的 failed 日志。
4. **预期结果**：
   - 有设备：程序退出码为 0，输出目录生成 `...npu_output_0.bin`，大小为 4×48×48×3 字节的 float16 数据（实际按 `GetSize()` 决定）；
   - 无设备：能独立复述完整流程图（第 4.2.2 节），并能指出 `gert::Tensor` 聚合初始化的 5 个字段各自含义。

#### 4.2.5 小练习与答案

**练习 1**：示例里 `ge::Tensor`（宿主）和 `gert::Tensor`（设备）分别在哪一步被使用？为什么需要两次转换？

**答案**：`ge::Tensor` 在 `BuildReluGraph`/`GenOnesData`（L202-243）中构造，承载建图阶段的宿主输入数据；`gert::Tensor` 在 `PrepareInputTensors`（L300-326）与输出构造（L344-348）中使用，描述已经搬到设备 HBM 上的数据。转换是必要的，因为图执行接口 `GeSessionExecuteGraphWithStreamAsync`（L351）面向的是运行时体系（exe_graph/gert），它需要的张量描述（StorageShape/StorageFormat/placement）与建图体系的 `TensorDesc`（Shape/Format/DataType）是两套设计——这就是第 u1-l1 讲说的「gert/ge 双体系」。

**练习 2**：示例第 306-308 行构造 `StorageShape({4, 50, 50, 3}, {4, 50, 50, 3})` 传了两个相同的 shape，它们分别是什么？和第 204 行的 `{1, 3, 32, 32}` 是什么关系？

**答案**：`StorageShape` 的两个参数分别是 origin shape（逻辑形状）和 storage shape（物理存储形状），一致表示无重排。它与 L204 的 `{1, 3, 32, 32}` **不一致**——这是示例自身的不严谨之处（输入张量按 1×3×32×32 生成，却声明成 4×50×50×3 的设备张量），阅读时应把它当作「演示聚合初始化写法」的代码，而不是可严格对齐的数值样例。这也提醒我们：阅读示例代码时仍要带着怀疑精神对照数据流。

**练习 3**：`GenOnesData` 里 `Tensor(input_tensor_desc, pData.data(), data_len)` 构造的 `ge::Tensor` 会拷贝数据吗？依据是什么？

**答案**：不会拥有/深拷贝数据。metadef 的 `ge::Tensor` 该构造函数接收 `const uint8_t *data, size_t size`（[inc/external/graph/tensor.h:154](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/tensor.h#L154)），语义是引用外部缓冲；示例中 `pData` 是 `GenOnesData` 的局部 `std::vector`，函数返回后内存依然有效是因为 vector 被移动进了返回值……实际上这里 `input_tensor` 是出参，`pData` 在函数结束时销毁，构造后的 Tensor 是否持有有效数据取决于实现内部的浅/深拷贝策略——这是一个**待确认**点，建议到第 u2-l4 讲（Tensor 与 TensorData 的所有权管理）中验证。

## 5. 综合实践

**任务：为示例画一张「三方职责表 + 数据流图」。**

1. 阅读完整示例后，画一张数据流图，节点是数据对象，边是产生它的函数：

   ```text
   常量 1（host）
     └─ GenOnesData ─→ ge::Tensor（host, DT_FLOAT16, NHWC）
                          └─ PrepareInputTensors ─→ 设备buffer ─→ gert::Tensor（device, kOnDeviceHbm）
                                                        └─ GeSessionExecuteGraphWithStreamAsync ─→ 输出 gert::Tensor
                                                                                                      └─ 回拷 ─→ *.bin 文件
   ```

2. 在图的每个边上标注：该步调用的 API 属于 metadef / ge / acl 哪一方，以及源码行号。
3. 回答收尾问题：整个示例一共出现了几处 metadef 类型向 ge/acl 接口的「跨界传递」（如 `AscendString` 映射传给 `GEInitialize`、`gert::Tensor` 向量传给 `GeSessionExecuteGraphWithStreamAsync`）？列出清单。这张表将成为你日后判断「哪些接口定义在 metadef、哪些在 ge」的直觉训练。

（本任务为源码阅读型实践，无需设备即可完成；有设备时可在运行日志上补充每一步的实际时间戳。）

## 6. 本讲小结

- `run_example.sh` 的流程是：source `set_env.sh` → `make clean; make` → 创建输出目录 → 运行 `.bin`；唯一需要按机器适配的是 CANN 安装路径。
- Makefile 揭示了使用 metadef 的工程形态：头文件全部来自 CANN 安装目录，链接库中 `-lmetadef` 就是 metadef 仓库的产物；`-D_GLIBCXX_USE_CXX11_ABI=0` 是与预编译库保持 ABI 一致的关键开关。
- 示例源码中 metadef 提供「词汇表」：`DataType`/`Format`（types.h）、`TensorDesc`/`ge::Tensor`（建图侧）、`AscendString`（跨 ABI 字符串）、`gert::Tensor`/`StorageShape`/`StorageFormat`/`kOnDeviceHbm`（执行侧），而 `GEInitialize`、`Session`、`op::Relu`、`aclrtMalloc` 等分别来自 ge、算子仓和 acl。
- 建图用 `ge::Tensor`（宿主）、执行用 `gert::Tensor`（设备），两者在 `PrepareInputTensors` 中通过 acl 内存搬运完成翻译——这是 ge/gert 双体系在真实工程中的直接体现。
- metadef 的 `exe_graph/runtime/tensor.h` 已是弃用转发头（转发到 `runtime_tensor.h`），是第 u1-l3 讲「兼容转发机制」的活例子。

## 7. 下一步学习建议

本讲之后你已完整看过 metadef 类型被真实工程消费的方式。建议：

1. 进入单元二，从第 u2-l1 讲（DataType 与 Format）开始，系统学习示例中出现的每个 metadef 类型的定义与实现。
2. 对 `gert::Tensor` 的聚合初始化五个字段感到好奇的读者，可提前浏览 [inc/external/exe_graph/runtime/runtime_tensor.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/runtime_tensor.h)，第 u2-l4 讲会展开。
3. 若想加深对构建体系的理解，可回到 `example/tc_ge_irrun_open_api_0014/Makefile` 与第 u1-l2 讲的 `build.sh` 对照阅读：一个编译 metadef 自身，一个消费 metadef 产物。
