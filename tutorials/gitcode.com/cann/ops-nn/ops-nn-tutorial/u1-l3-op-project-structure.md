# 算子工程的目录解剖：op_host、op_kernel、op_api 等五层交付件

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出一个标准算子工程包含哪些子目录（`op_host`、`op_kernel`、`op_api`、`op_graph`、`tests`、`examples`、`docs`）以及各自的职责。
2. 区分不同目录产出的「交付物」形态：哪些代码跑在 Host CPU 上、哪些跑在 AI Core 上、哪些是给 aclnn 调用用的、哪些是给图模式用的。
3. 对照官方 `dir_structure.md` 文档，独立定位仓库里任意一个算子的交付件；并能解释教学样例 `add_example` 与生产算子 `activation/gelu` 在目录上的差异。

本讲承接 u1-l1 建立的「四类交付件」概念和 u1-l2 的编译闭环，不涉及具体代码逻辑（tiling、kernel 实现分别在 u4、u5 精读），只解决「东西放在哪、是什么」这个地图问题。

## 2. 前置知识

- **Host 侧与 Device 侧**：在昇腾平台上，CPU 侧叫 Host，NPU 侧叫 Device。一个算子的代码不是写在一个文件里，而是拆成「跑在 Host 上的准备代码」和「跑在 AI Core（Device）上的计算代码」两大块。
- **交付件（交付物）**：算子源码编译后会生成多种产物——算子信息库、Host 实现库、kernel 二进制、aclnn 动态库、图模式原型库等。源码目录的划分方式，本质上就是按这些产物划分的。
- **aclnn 调用与图模式调用**：上层用户有两种方式使用算子——直接调 `aclnnXxx` C++ 接口（eager，两段式 API），或把算子作为节点组成计算图再整体下发（GE 图模式）。两种方式分别依赖 `op_api` 和 `op_graph` 交付件。
- **soc_version**：芯片型号短名，如 `ascend910b`（Atlas A2）、`ascend910_93`（Atlas A3）、`ascend950`。算子的部分配置（如 kernel 二进制描述）按芯片型号分目录存放。
- **Tiling（切分）**：把大张量切成小块分给多个 AI Core 并行处理，Host 侧需要一段「tiling 计算」代码。现在只需知道它属于 `op_host`。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
| --- | --- |
| [docs/zh/install/dir_structure.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/dir_structure.md) | 官方全量目录层级说明，是本讲的「字典」 |
| [examples/add_example/](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/CMakeLists.txt) | 官方 AI Core 算子教学样例，目录最精简 |
| [examples/add_example/CMakeLists.txt](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/CMakeLists.txt) | 样例的编译入口，体现「子目录自治」的组织方式 |
| [examples/add_example/README.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/README.md) | 算子级 README：产品支持、参数说明、调用样例索引 |
| [examples/add_example/op_host/](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp) | Host 侧：算子定义、shape 推导、tiling、二进制配置 |
| [examples/add_example/op_kernel/](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp) | Device 侧：AI Core kernel 入口与实现 |
| [examples/add_example/op_graph/add_example_proto.h](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_graph/add_example_proto.h) | 图模式算子原型（proto）定义 |
| [activation/gelu/](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/README.md) | 生产算子对照样本：目录比样例更全 |
| [matmul/](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/matmul/CMakeLists.txt) | 算子大类目录示例：一个类下挂几十个算子工程 |

## 4. 核心概念与源码讲解

### 4.1 算子工程的标准目录骨架

#### 4.1.1 概念说明

ops-nn 仓库用「算子大类目录 / 算子工程目录」两级组织算子源码：顶层 `activation`、`matmul`、`norm` 等是**算子大类**（对应 `${op_class}`），每个大类下面是若干**算子工程目录**（`${op_name}`，小写下划线形式）。每个算子工程内部再按交付物类型拆成固定的几个子目录。

这套目录约定是「合同」式的：构建系统按目录名找文件、按文件名后缀识别产物类型。例如 `op_host` 下的 `*_def.cpp` 一定是算子信息库，`op_kernel` 下的 `*.cpp` 一定是 kernel 入口。

#### 4.1.2 核心流程

一个标准算子工程的目录骨架（以 `add_example` 实际存在的文件为准）：

```text
${op_class}/${op_name}/                  # 如 activation/gelu
├── CMakeLists.txt                       # 算子编译入口
├── README.md                            # 算子介绍（功能/参数/支持产品/调用样例）
├── docs/                                # 可选：aclnn${OpName}.md 接口文档
├── examples/                            # 调用示例（aclnn 与 geir 两类）
├── op_graph/                            # 图模式交付件：proto + 融合规则
├── op_host/                             # Host 侧交付件：def/infershape/tiling/config
├── op_api/                              # 可选：aclnn 适配层（未提供则构建时自动生成）
├── op_kernel/                           # AI Core Device 侧交付件
├── op_kernel_aicpu/                     # 可选：AI CPU 版 kernel（如 add_example_aicpu）
└── tests/                               # UT/ST 测试用例
```

注意文档开头的提示——**目录是可选的，缺失有明确含义**：

- 缺 `op_host` 或 `op_kernel`：可能复用了其他算子的实现（去看它的 `op_api`/`op_graph` 源码），也可能尚无 Ascend C 实现。
- 缺 `op_api`：该算子暂不支持 aclnn 调用。
- 缺 `op_graph`：该算子暂不支持图模式调用。

#### 4.1.3 源码精读

官方权威说明在 [docs/zh/install/dir_structure.md:20-63](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/dir_structure.md#L20-L63)，这段用 `${op_class}/${op_name}` 占位符罗列了单算子工程的全部标准文件，并逐行注释了每个文件的作用（例如 `${op_name}_def.cpp` 是「算子信息库」、`config/${soc_version}/${op_name}_binary.json` 是二进制配置）。读任何算子前先把这份目录树通读一遍，性价比极高。

`add_example` 的编译入口体现了目录组织的「自治」方式：

```cmake
file(GLOB CURRENT_DIRS RELATIVE ${CMAKE_CURRENT_SOURCE_DIR} ${CMAKE_CURRENT_SOURCE_DIR}/*)
if(NOT ENABLE_TEST AND NOT BENCHMARK)
    list(REMOVE_ITEM CURRENT_DIRS tests)
endif()
foreach(SUB_DIR ${CURRENT_DIRS})
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/${SUB_DIR}/CMakeLists.txt")
        add_subdirectory(${SUB_DIR})
    endif()
endforeach()
```

见 [examples/add_example/CMakeLists.txt:11-19](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/CMakeLists.txt#L11-L19)。这段代码遍历当前目录下所有子目录，凡是有自己 `CMakeLists.txt` 的就 `add_subdirectory` 递归进去；只有开启 `ENABLE_TEST` 或 `BENCHMARK` 时才会把 `tests` 纳入编译。所以「哪些交付件参与编译」由子目录是否存在 + 各自的 CMakeLists 决定，顶层入口完全不用改。这解释了为什么 `op_host`、`op_kernel`、`op_graph` 下都有自己的 `CMakeLists.txt`。

`matmul` 大类目录则展示了上一级的组织方式：`matmul/` 下挂了 `gemm`、`quant_batch_matmul_v4`、`fused_mat_mul` 等数十个算子工程，大类自身只有一个 `CMakeLists.txt` 汇总。

#### 4.1.4 代码实践

**实践：用命令画出 add_example 的真实目录树**

1. 实践目标：不看文档，用文件系统命令确认 `add_example` 实际包含哪些子目录和文件。
2. 操作步骤（在仓库根目录执行）：

   ```bash
   find examples/add_example -type f | sort
   tree examples/add_example    # 若未装 tree，用上一条 find 即可
   ```

3. 需要观察的现象：输出应包含 `op_host`（含 `config/ascend910b`）、`op_kernel`、`op_graph`、`examples`、`tests/ut/{op_host,op_kernel}` 五组子目录；注意 **`op_api` 目录并不存在**。
4. 预期结果：与 [docs/zh/install/dir_structure.md:91-97](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/dir_structure.md#L91-L97) 列出的 `add_example` 目录一致。`op_api` 缺失是正常的——文档 L47 说明该目录「若未配置工程自动生成」，构建系统会为样例自动生成 aclnn 适配代码。

#### 4.1.5 小练习与答案

**练习 1**：`examples/add_example_aicpu` 与 `examples/add_example` 的目录差异是什么？
答案：前者用 `op_kernel_aicpu` 替代了 `op_kernel`，即 kernel 跑在 AI CPU 上而不是 AI Core 上，其余 `op_host`/`op_graph`/`examples`/`tests` 结构相同。

**练习 2**：为什么 `add_example/CMakeLists.txt` 里要 `list(REMOVE_ITEM CURRENT_DIRS tests)`？
答案：默认编译产物（算子包）不需要测试代码，只有开启 `ENABLE_TEST` 或 `BENCHMARK` 时才把 `tests` 子目录加入编译，加快常规构建速度。

**练习 3**：仓库里 `activation/gelu` 的 `op_host` 下没有 `gelu_tiling.cpp`（只有 `arch35/gelu_tiling_arch35.cpp`），这说明什么？
答案：按 `dir_structure.md` L44 的说明，缺少 `*_tiling.cpp` 表示该场景下无独立 Tiling 实现——gelu 很可能复用了公共 tiling 模板，只在 arch35 架构子场景下才有专属 tiling 优化。

### 4.2 op_host：Host 侧交付件

#### 4.2.1 概念说明

`op_host` 存放**运行在 Host CPU 上**的算子代码，一个目录里通常有四类东西：

1. **算子定义（`*_def.cpp`）**：向 CANN 算子库注册算子的「身份证」——名字、输入输出、支持的数据类型/格式、跑在哪些芯片上。
2. **Shape 推导（`*_infershape.cpp`，可选）**：根据输入 shape 推导输出 shape；没有则默认输出 shape 与输入相同。
3. **Tiling 实现（`*_tiling.cpp/.h`，可选；`*_tiling_${sub_case}.cpp` 为子场景优化版）**：计算任务切分参数。
4. **二进制配置（`config/${soc_version}/${op_name}_binary.json`，可选）**：描述预编译 kernel 二进制的元信息，未配置时工程自动生成。

#### 4.2.2 核心流程

Host 侧代码在算子执行前被调用，大致时序：

```text
用户发起调用
  → CANN 查算子信息库（来自 *_def.cpp 注册的信息）做校验
  → 执行 Infershape（来自 *_infershape.cpp）得到输出 shape、分配输出内存
  → 执行 Tiling（来自 *_tiling.cpp）得到切分参数，写入 tiling data
  → 下发 kernel（op_kernel 的二进制）到 AI Core
```

即：`op_host` 是「参谋部」，`op_kernel` 是「作战部队」。

#### 4.2.3 源码精读

算子定义的写法（以 `add_example` 为例）：

```cpp
class AddExample : public OpDef {
public:
    explicit AddExample(const char* name) : OpDef(name)
    {
        this->Input("x1")
            .ParamType(REQUIRED)
            .DataType({ge::DT_FLOAT, ge::DT_INT32})
            .Format({ge::FORMAT_ND, ge::FORMAT_ND})
            ...
```

见 [examples/add_example/op_host/add_example_def.cpp:29-63](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L29-L63)：这段声明了两个必选输入 `x1`/`x2` 和一个输出 `y`，支持 FLOAT/INT32、ND 格式。文件末尾的三行把同一套编译配置挂到不同芯片上：

```cpp
this->AICore().AddConfig("ascend910b", aicoreConfig);
this->AICore().AddConfig("ascend910_93", aicoreConfig);
this->AICore().AddConfig("ascend950", aicoreConfig);
```

见 [examples/add_example/op_host/add_example_def.cpp:76-78](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L76-L78)，这就是 README「产品支持情况」表在代码侧的对应物。整个类最终通过 [add_example_def.cpp:83](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L83) 的 `OP_ADD(AddExample);` 宏注册进算子库——生产算子 gelu 的写法完全相同（[activation/gelu/op_host/gelu_def.cpp:46](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_host/gelu_def.cpp#L46) 的 `OP_ADD(Gelu);`）。

二进制配置是 JSON 格式：

```json
{
  "op_type": "AddExample",
  "op_list": [
    {
      "bin_filename": "AddExample_a1532827238e1555db7b997c7bce2928",
      "inputs": [ { "name": "x1", "dtype": "float32", "format": "ND", "shape": [-2], ... } ]
    }
  ]
}
```

见 [examples/add_example/op_host/config/ascend910b/add_example_binary.json:1-25](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/config/ascend910b/add_example_binary.json#L1-L25)：它按芯片型号（`ascend910b`）描述预编译 kernel 二进制的文件名和输入规格（`shape: [-2]` 表示任意 shape），供运行时挑选二进制用。

#### 4.2.4 代码实践

**实践：从 def 文件反推 README 的产品支持表**

1. 实践目标：验证「README 产品支持表 ↔ def 文件 AddConfig」的对应关系。
2. 操作步骤：
   - 打开 [examples/add_example/README.md:5-12](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/README.md#L5-L12) 的产品支持表；
   - 再打开 [add_example_def.cpp:76-78](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_def.cpp#L76-L78) 的三行 `AddConfig`；
   - 对照 u1-l1 学过的 soc_version 映射（ascend910b=Atlas A2、ascend910_93=Atlas A3、ascend950=950 系列），逐行核对。
3. 需要观察的现象：README 表中打 √ 的三行产品，恰好对应三行 `AddConfig` 的芯片短名。
4. 预期结果：一一对应。再用同样方法核对 `activation/gelu/op_host/gelu_def.cpp` 中的 `AddConfig` 行数与 gelu README 支持表行数是否一致。

#### 4.2.5 小练习与答案

**练习 1**：`*_infershape.cpp` 缺失时输出 shape 怎么定？
答案：按 [dir_structure.md:41](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/dir_structure.md#L41) 的说明，默认与输入 shape 相同。对 elementwise 的加法天然成立，但 `add_example` 仍然显式提供了 infershape（因为要处理广播，详见 u3-l2）。

**练习 2**：`config/ascend910b/` 这个目录层级为什么按芯片型号分？
答案：不同芯片的 kernel 二进制不同，二进制配置 json 必须按 `${soc_version}` 分开存放，运行时按当前芯片加载对应目录下的配置。

### 4.3 op_kernel：Device 侧交付件

#### 4.3.1 概念说明

`op_kernel` 存放**运行在 AI Core 上**的 Ascend C 代码，通常包含：

- `${op_name}.cpp`：kernel 入口文件，接收 GM 地址和 tiling 数据，按 tiling key 分发到具体实现；
- `${op_name}.h`：kernel 类实现（CopyIn-Compute-CopyOut 流水，u5 精读）；
- `${op_name}_tiling_data.h`（可选）：定义 Host 传给 Device 的 tiling 参数结构体；
- `${op_name}_tiling_key.h`（可选）：定义 tiling key，区分不同实现分支；
- `${sub_case}/`（可选）：子场景目录（如 `arch35` 表示特定架构版本的优化实现）。

#### 4.3.2 核心流程

Device 侧入口的执行骨架：

```text
AI Core 启动
  → 执行 extern 入口函数 add_example(...)
  → 从 GM 的 tiling 区取出 AddExampleTilingData 结构体
  → 按编译期 tiling key（schMode）选择分支
  → 实例化 kernel 类 → Init() → Process()
```

#### 4.3.3 源码精读

kernel 入口（Device 侧真正的「main」）：

```cpp
template <uint32_t schMode>
__global__ __aicore__ void add_example(GM_ADDR x, GM_ADDR y, GM_ADDR z, GM_ADDR workspace, GM_ADDR tiling)
{
    REGISTER_TILING_DEFAULT(AddExampleTilingData);
    GET_TILING_DATA_WITH_STRUCT(AddExampleTilingData, tilingData, tiling);
    if constexpr (schMode == static_cast<uint32_t>(AddExampleTilingKey::TILING_KEY_EXAMPLE_FLOAT)) {
        NsAddExample::AddExample<float> op;
        op.Init(x, y, z, &tilingData);
        op.Process();
    }
    ...
}
```

见 [examples/add_example/op_kernel/add_example.cpp:36-57](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L36-L57)：入口函数拿到 5 个 GM 地址（两个输入、一个输出、workspace、tiling），把 tiling 区数据解成结构体，再按 `schMode` 分发到 float 或 int32 的模板实例。`tilingData` 的结构定义在 [examples/add_example/op_kernel/add_example_tiling_data.h:19-22](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_data.h#L19-L22)，只有 `blockFactor`、`ubFactor` 等几个 int64 字段——这就是 Host 侧 tiling 计算结果传到 Device 的「数据契约」。

生产算子 gelu 的差异：它的 `op_kernel` 下只有 `arch35/` 子目录（`gelu_dag.h`、`gelu_struct.h`），主实现复用公共模板，只有架构专属优化才落到自己的目录里。

#### 4.3.4 代码实践

**实践：追踪 tiling data 的定义与使用两端**

1. 实践目标：确认「Host 写、Device 读」的 tiling data 结构在两个目录间如何衔接。
2. 操作步骤：
   - 读 [add_example_tiling_data.h:19-22](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example_tiling_data.h#L19-L22)，记下结构体字段名；
   - 在 `op_host/add_example_tiling.cpp` 中用编辑器搜索这些字段名，观察 Host 侧在哪里给它们赋值。
3. 需要观察的现象：`op_host` 的 tiling 函数填充同一组字段，`op_kernel` 的入口用 `GET_TILING_DATA_WITH_STRUCT` 读出同一结构体。
4. 预期结果：两侧字段一一对应、类型一致；理解「改 tiling 策略 = 同时动这两处」。（赋值逻辑的精读留到 u4。）

#### 4.3.5 小练习与答案

**练习 1**：`add_example` 里 tiling key 区分的是什么？
答案：数据类型——`TILING_KEY_EXAMPLE_FLOAT = 0` 与 `TILING_KEY_EXAMPLE_INT32 = 1`（见 [add_example.cpp:24-29](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.cpp#L24-L29) 的枚举），kernel 据此选择 float 或 int32 模板实例。更复杂的算子会用 tiling key 区分量化模式、架构等多种维度。

**练习 2**：gelu 的 `op_kernel/arch35/` 目录为什么存在？
答案：存放针对 arch35 架构的专属 kernel 实现（子场景），非 arch35 场景走公共实现，这是「子场景目录」约定的实际运用。

### 4.4 op_api 与 op_graph：两种调用入口的交付件

#### 4.4.1 概念说明

- **`op_api`**：aclnn 适配层，把框架风格的调用转成算子下发。内部又分两层：`${op_name}.cpp/.h` 是 l0 接口实现，`aclnn_${op_name}.cpp/.h` 是对外的两段式 aclnn 接口。**该目录可选**——`add_example` 就没有它，由构建工程自动生成。
- **`op_graph`**：图模式交付件。`${op_name}_proto.h` 定义算子在图中的原型（用 `REG_OP` 宏），供图优化和融合阶段识别算子；`fusion_pass/` 存放融合规则。

#### 4.4.2 核心流程

```text
aclnn 路径：用户 C++ 代码 → aclnnXxx（op_api）→ 下发 kernel
图模式路径：用户构图 → GE 识别 proto（op_graph）→ 图优化/融合 → 下发 kernel
```

两条路最终都落到同一套 `op_host` + `op_kernel` 上，只是入口交付件不同。

#### 4.4.3 源码精读

图模式原型的定义：

```cpp
REG_OP(AddExample)
    .INPUT(x1, TensorType({DT_FLOAT, DT_INT32}))
    .INPUT(x2, TensorType({DT_FLOAT, DT_INT32}))
    .OUTPUT(y, TensorType({DT_FLOAT, DT_INT32}))
    .OP_END_FACTORY_REG(AddExample)
```

见 [examples/add_example/op_graph/add_example_proto.h:35-39](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_graph/add_example_proto.h#L35-L39)：`REG_OP` 是 GE（Graph Engine）的算子注册宏，内容与 `op_host` 的 `OpDef` 声明一致，但服务于图编译阶段；注释里还标注了与 TensorFlow `Add` 算子的兼容关系（[add_example_proto.h:33](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_graph/add_example_proto.h#L33)）。

aclnn 侧以 gelu 为对照：`activation/gelu/op_api/` 下同时存在 `gelu.cpp/gelu.h`（l0 接口）和 `aclnn_gelu.cpp/aclnn_gelu.h`（两段式 aclnn 接口），这正是 [dir_structure.md:47-52](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/dir_structure.md#L47-L52) 描述的标准四文件结构；而 `add_example` 没有该目录，却仍能在 `examples/test_aclnn_add_example.cpp` 中调用 `aclnnAddExample`——适配代码由构建工程自动生成。

算子级 README 的「调用说明」表把两条路径与样例文件对应起来，见 [examples/add_example/README.md:69-88](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/README.md#L69-L88)：aclnn 调用指向 `test_aclnn_add_example.cpp`，图模式调用指向 `test_geir_add_example.cpp`。

#### 4.4.4 代码实践

**实践：确认 add_example「无 op_api 也能 aclnn 调用」**

1. 实践目标：理解 op_api 目录的可选性。
2. 操作步骤：
   - `ls examples/add_example` 确认无 `op_api`；
   - 打开 [examples/add_example/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp)，搜索 `aclnnAddExample` 的调用；
   - 对比 `ls activation/gelu/op_api`，确认 gelu 有四件套。
3. 需要观察的现象：样例中调用的是 `aclnnAddExampleGetWorkspaceSize` + `aclnnAddExample` 两段式接口，但仓库里找不到它的手写实现。
4. 预期结果：印证 `dir_structure.md` L47 的说法——`op_api` 未配置时工程自动生成。至于生成代码落到哪个构建目录、包含哪些内容，**待本地验证**（可在编译后到 `build_out` 中搜索生成的源文件）。

#### 4.4.5 小练习与答案

**练习 1**：`op_graph/add_example_proto.h` 里的 `REG_OP` 与 `op_host/add_example_def.cpp` 里的 `OpDef` 都声明了输入输出，为什么要在两处写两遍？
答案：它们面向不同子系统——`OpDef` 供 aclnn/执行框架查算子信息库，`REG_OP` 原型供 GE 图编译、图优化和融合阶段识别算子。声明内容需保持一致，这是算子工程必须遵守的约定。

**练习 2**：如果一个算子目录既没有 `op_api` 也没有自动生成机制，用户还能用它吗？
答案：不能通过 aclnn 调用（`dir_structure.md` L7：缺 `op_api` 说明暂不支持 aclnn 调用），可能只支持图模式，或完全未交付。

### 4.5 教学样例与生产算子对照：add_example vs activation/gelu

#### 4.5.1 概念说明

`add_example` 是刻意保持精简的教学样例；生产算子 `gelu` 展示了同一套目录约定的「完全体」。对照两者能看清哪些目录是核心、哪些是规模化后才需要的。

#### 4.5.2 核心流程

两者目录差异汇总表：

| 交付件 | add_example | activation/gelu | 说明 |
| --- | --- | --- | --- |
| `README.md` | 有（产品表/参数/调用表） | 有 | 算子级文档标配 |
| `docs/` | 无 | 有 `aclnnGelu.md` | aclnn 接口的正式文档 |
| `examples/` | aclnn + geir 两个样例 | aclnn 样例 | 调用示例 |
| `op_graph/` | proto + 空 fusion_pass | proto + fusion_pass | 图模式交付件 |
| `op_host/` | def/infershape/tiling + config | def/infershape + config + `arch35/` 子场景 tiling | gelu 复用公共 tiling，仅保留架构优化版 |
| `op_api/` | 无（自动生成） | 四件套（l0 + aclnn） | gelu 手写完整适配层 |
| `op_kernel/` | 入口 + 实现 + tiling_data/key | `arch35/` 专属实现 | gelu 主实现走公共模板 |
| `framework/` | 无 | `gelu_tf_plugin.cpp` | 第三方框架（TF）插件 |
| `tests/` | ut（op_host + op_kernel） | ut + **st** + assets | 生产算子要求系统级测试 |

#### 4.5.3 源码精读

gelu 独有目录的实证：`activation/gelu/docs/aclnnGelu.md` 存在（符合 [dir_structure.md:24-25](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/dir_structure.md#L24-L25) 的 `docs/aclnn${OpName}.md` 约定，`OpName` 为大驼峰）；`activation/gelu/framework/gelu_tf_plugin.cpp` 是框架适配层；`activation/gelu/tests/st/` 下按 `aclnnGelu`、`arch35` 组织系统测试。而 [examples/add_example/README.md:69-88](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/README.md#L69-L88) 的调用说明表只链接了两个 examples 样例，没有独立 docs 页。

`matmul` 大类则展示了规模另一极：一个 `matmul/CMakeLists.txt` 下管理 `gemm`、`quant_batch_matmul_v4`、`fused_mat_mul` 等几十个算子工程，每个工程内部仍是同一套骨架。

#### 4.5.4 代码实践：目录结构对比图（本讲核心实践）

1. 实践目标：亲手整理两张算子目录结构对比图，把「子目录 → 交付物类型」的映射固化下来。
2. 操作步骤：
   1. 在仓库根目录执行：

      ```bash
      find examples/add_example -type d | sort
      find activation/gelu -maxdepth 2 -type d | sort
      ```

   2. 为每个目录画一棵树（纸笔或 Markdown 代码块均可）；
   3. 在每个子目录旁标注交付物类型，参考标注如下（`add_example` 部分）：

      ```text
      add_example/
      ├── CMakeLists.txt        # 编译入口（目录自治递归）
      ├── README.md             # 算子文档
      ├── op_host/              # [Host 交付件] 算子信息库+infershape+tiling
      │   └── config/ascend910b/  # [Host 交付件] kernel 二进制配置 json
      ├── op_kernel/            # [AI Core 交付件] kernel 入口+实现+tiling 契约
      ├── op_graph/             # [图模式交付件] proto + 融合规则
      ├── examples/             # 调用样例（aclnn / geir）
      └── tests/ut/             # 测试（op_host / op_kernel UT）
      ```

   4. 再为 `activation/gelu` 画第二张，标出它比样例多出的 `docs/`、`op_api/`、`framework/`、`tests/st/`、`op_host/arch35/`、`op_kernel/arch35/`。
3. 需要观察的现象：两棵树的公共部分完全同构（骨架一致），差异全部是「可选交付件」。
4. 预期结果：得到两张可直接贴进笔记的对比图，且每个标注都能在 [dir_structure.md](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/dir_structure.md) 中找到出处。

#### 4.5.5 小练习与答案

**练习 1**：gelu 的 `tests/st` 与 `tests/ut` 有什么分工？
答案：`ut`（单元测试）针对单个交付件做函数级验证（infershape、tiling、kernel 仿真）；`st`（系统测试）走完整调用链做端到端精度验证。教学样例只配了 ut，生产算子两者都要（详见 u7）。

**练习 2**：为什么 `add_example` 的 `op_graph/fusion_pass/` 里只有一个 `.gitkeep`？
答案：样例没有融合规则，用 `.gitkeep` 保留空目录以示意目录约定的存在；生产算子会在其中放置真正的 fusion pass 代码。

## 5. 综合实践

**任务：给「陌生算子」画交付件地图。**

从 `docs/zh/op_list.md` 或 `matmul/` 目录中任选一个你感兴趣、且非 gelu 的算子（如 `matmul/quant_batch_matmul_v4` 或 `norm/` 下任一算子），完成：

1. 用 `find <算子目录> -type f | sort` 列出全部文件；
2. 按 4.5.4 的样式画出目录树，标注每个子目录的交付物类型；
3. 回答三个定位问题：
   - 它的算子定义（身份证）在哪个文件的哪几行？搜 `OP_ADD` 即可定位；
   - 它有没有手写 `op_api`？有没有 `st` 测试？有没有 `${sub_case}` 子场景目录？
   - 对照 4.5.2 的差异表，它更接近 `add_example` 形态还是 `gelu` 形态？
4. 用一句话向同事解释：为什么这个算子的 `op_host` 下没有（或有）`*_tiling.cpp`。

完成这个任务意味着你已能脱离讲义、仅凭目录约定读懂任何 ops-nn 算子工程的组织方式。

## 6. 本讲小结

- 算子源码按「大类目录/算子工程目录」两级组织，工程内部按交付物拆成 `op_host`、`op_kernel`、`op_api`、`op_graph`、`tests`、`examples`、`docs` 等标准子目录，构建系统按目录名和文件名后缀识别产物。
- `op_host` 是 Host 侧交付件：`*_def.cpp` 注册算子信息（`OP_ADD` 宏）、`*_infershape.cpp` 推导输出 shape、`*_tiling.cpp` 计算切分参数、`config/${soc_version}/*_binary.json` 描述预编译二进制。
- `op_kernel` 是 AI Core 侧交付件：入口函数从 GM 取 tiling data 结构体（Host 写、Device 读的数据契约），按 tiling key 分发到模板实现。
- `op_api` 服务 aclnn 两段式调用（可选，未配置则自动生成），`op_graph` 的 `REG_OP` proto 服务 GE 图模式与融合，两条路径共用同一套 host/kernel。
- 目录是「合同」：缺 `op_host`/`op_kernel` 可能是复用他人实现，缺 `op_api` 表示不支持 aclnn，缺 `op_graph` 表示不支持图模式——`dir_structure.md` 开头的四条提示就是判读手册。
- 教学样例 `add_example` 是精简骨架，生产算子 `gelu` 在其上增加了 `docs/`、`op_api/`、`framework/`、`st` 测试和 `arch35` 子场景目录；`matmul` 大类则展示了数十个算子工程共用一套骨架的规模化形态。

## 7. 下一步学习建议

- 下一讲（u1-l4）将在这张目录地图上动手：修改 `op_kernel/add_example.h` 中的计算逻辑（Add 改 Mul），重新编译并验证，完成第一次算子开发闭环。
- 建议提前浏览 `examples/add_example/op_host/add_example_tiling.cpp` 和 `op_kernel/add_example.h`，只看结构不看细节，为 u1-l4 的修改做准备。
- 想查任意算子的调用支持情况，可阅读 `docs/zh/op_list.md`；想看官方目录约定全文，随时回读 [docs/zh/install/dir_structure.md](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/install/dir_structure.md)。
