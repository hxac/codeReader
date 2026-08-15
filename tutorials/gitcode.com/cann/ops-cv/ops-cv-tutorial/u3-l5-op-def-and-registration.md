# 算子定义与编译注册：def 文件与算子信息生成

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `*_def.cpp` 文件：知道 `OpDef` 类、`Input/Output/Attr` 链式定义、`OpAICoreConfig` 编译配置和 `OP_ADD` 注册宏各自声明了什么。
2. 理解 def 文件是「一份声明、多处消费」的单一事实源：它同时驱动 aclnn 接口代码自动生成、算子信息库（ops-info ini/json）生成和 kernel 二进制编译。
3. 说出 `op_host/config/<芯片型号>/` 目录下 `*_binary.json` 与 `*_simplified_key.ini` 的作用，以及它们与 def 文件中 `AddConfig` 的对应关系。
4. 能完整跟踪一条链路：def 注册 → opbuild 工具生成 ini/aclnn → gen_ops_info.cmake 生成 ops-info.json → opc 编译出 kernel 二进制 → 打包安装。

本讲承接 u3-l3（Tiling 机制）。在 u3-l1 中我们已经知道「框架按 OpDef 路由算子」，本讲就回答：这个 OpDef 到底是什么、写在哪儿、编译系统怎么消费它。

## 2. 前置知识

- **def 文件**：每个 Ascend C 算子在 `op_host/` 目录下的 `算子名_def.cpp`，用 C++ 类的方式向 CANN 框架「自我介绍」：我叫什么、有几个输入输出、支持什么 dtype/format、有哪些属性、能在哪些芯片上跑、kernel 实现文件叫什么。它不包含任何计算逻辑。
- **OP_ADD 宏**：def 文件最后一行的注册入口。C++ 全局对象在动态库加载时会自动执行构造函数，`OP_ADD(AddExample)` 借这一特性把算子定义登记进框架的注册表——这与 u3-l2 见过的 `IMPL_OP_INFERSHAPE`、u3-l3 见过的 `IMPL_OP_OPTILING` 是同一族机制。
- **opbuild 工具**：CANN 提供的可执行程序（CMake 变量 `OP_BUILD_TOOL`）。它把编译好的 def 动态库加载起来，遍历注册表，为每个算子「反推」出算子信息 ini 文件和 aclnn 接口骨架代码。这就是 u1-l3 提到的「opbuild.cmake 调 op_build 从 *_def.cpp 自动生成 aclnn 接口代码到 build/autogen」的具体机制。
- **opc**：另一个 CANN 工具，负责把 Ascend C kernel 源码按算子信息编译成 `.o` 二进制。binary.json 就是在告诉 opc「要编出哪几个二进制、每个二进制对应什么 dtype/format 组合」。
- **op_type**：算子在框架内的正式名字，即 `OP_ADD(AddExample)` 括号里的类名，与目录名（add_example）是两套命名——前者大驼峰，后者小写下划线。

## 3. 本讲源码地图

| 文件 | 作用 |
| ---- | ---- |
| [examples/add_example/op_host/add_example_def.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/add_example_def.cpp) | 最简 def 文件：双输入逐元素加法的完整定义 |
| [image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp) | 复杂 def 文件：带 dtype 映射表、属性、多格式支持 |
| [examples/add_example/op_host/CMakeLists.txt](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/CMakeLists.txt) | 算子 op_host 侧的编译声明，把 def 源码挂入模块 |
| [cmake/opbuild.cmake](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/opbuild.cmake) | 调 opbuild 工具，从 def 生成 aclnn 代码与 ini |
| [cmake/gen_ops_info.cmake](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/gen_ops_info.cmake) | 消费 ini，生成 ops-info.json 并驱动 kernel 二进制编译 |
| [examples/add_example/op_host/config/ascend910b/add_example_binary.json](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/config/ascend910b/add_example_binary.json) | 芯片级二进制清单：ascend910b 上要编出的 kernel 组合 |
| [examples/add_example/op_host/config/ascend910b/add_example_simplified_key.ini](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/config/ascend910b/add_example_simplified_key.ini) | 控制 opc 的 `--simplified_key_mode` 选项 |
| [examples/add_example/README.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/README.md) | 算子对外文档，与 def/config 构成三方契约 |

## 4. 核心概念与源码讲解

### 4.1 最简 def 文件：add_example_def.cpp 的注册结构

#### 4.1.1 概念说明

def 文件回答的是框架在调度一个算子前必须知道的所有「元信息」。以 AddExample（y = x1 + x2）为例，框架至少要知道：输入端口叫 x1、x2，输出端口叫 y，dtype 只能是 FLOAT 或 INT32，格式是 ND，kernel 实现文件名是 add_example，支持 ascend910b / ascend910_93 / ascend950 三种芯片。这些信息全部集中在一个继承自 `OpDef` 的类的构造函数里，用链式调用逐项声明。

#### 4.1.2 核心流程

```text
编写 def 文件
  ├── class XxxOp : public OpDef，构造函数里链式声明
  │     ├── Input("端口名").ParamType(...).DataType(...).Format(...)
  │     ├── Output("端口名")...
  │     ├── Attr("属性名").AttrType(...).Bool/Int(...)
  │     └── OpAICoreConfig + AICore().AddConfig("芯片名", cfg)
  └── OP_ADD(XxxOp)  ← 生成全局静态注册对象
        ↓（动态库被 dlopen 时构造函数执行）
框架注册表中出现该算子的完整描述
```

注意：def 只注册「是什么」，不注册「怎么算」——计算由 op_kernel 实现，shape 推导由 infershape 文件注册，tiling 由 tiling 文件注册。def 里的 `ExtendCfgInfo("opFile.value", ...)` 只负责把算子名与 kernel 实现文件名绑定起来。

#### 4.1.3 源码精读

先看输入端口的定义。x1 是必选输入，dtype 白名单是 FLOAT 和 INT32，格式 ND，并开启了 `AutoContiguous`（框架自动把非连续 tensor 连续化，呼应 u2-l2 讲过的非连续处理）：

[add_example_def.cpp:44-49](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/add_example_def.cpp#L44-L49) —— 定义输入 x1 的规格：必选参数、FLOAT/INT32 两种数据类型、ND 格式、未知 shape 时仍按 ND、内存自动连续化。x2 与 y 的定义完全对称（L51-63），三个端口的 dtype 白名单一致，这保证了「同 dtype 进、同 dtype 出」的逐元素语义。

接着是 AI Core 编译配置，这是 def 文件里信息密度最高的几行：

[add_example_def.cpp:66-74](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/add_example_def.cpp#L66-L74) —— 构造 `OpAICoreConfig`：开启动态 shape 与动态 rank 支持（所以样例可以用任意维输入）、关闭动态格式、并通过 `ExtendCfgInfo("opFile.value", "add_example")` 把算子绑定到名为 add_example 的 kernel 实现文件。这个字符串与前几讲见过的 `resize_bilinear_v2_apt.cpp`、binary.json 里的 `*_apt` 后缀同源。

最后是芯片绑定与注册：

[add_example_def.cpp:76-81](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/add_example_def.cpp#L76-L81) —— 把同一份 aicoreConfig 用 `AddConfig` 分别登记到 ascend910b、ascend910_93、ascend950 三个芯片（分别对应 Atlas A2、A3、950 系列），最后 `OP_ADD(AddExample)` 完成注册。AddConfig 的芯片字符串列表，就是编译系统判断「某算子在 `--soc` 指定的芯片上要不要编」的直接依据（见 4.3.3）。

#### 4.1.4 代码实践

**实践目标**：验证 def 文件中的 `AddConfig` 列表与编译系统能力范围的对应关系。

**操作步骤**：

1. 打开 [add_example_def.cpp:76-78](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/add_example_def.cpp#L76-L78)，记下三个芯片名。
2. 在编译环境执行一次 `bash build.sh --pkg --soc=ascend910b --ops=add_example`，观察 CMake 配置阶段日志中形如 `[INFO] On [ascend910b], [add_example] compile binary with self config.` 的输出。
3. 再故意改用 `--soc=ascend310p` 编译，观察日志变化。

**需要观察的现象**：ascend910b 下日志显示算子参与编译；ascend310p 下应出现 `[INFO] On [ascend310p], [add_example] not supported.`（来自 4.3.3 将讲到的 `check_op_supported`），且不会生成该芯片的产物。

**预期结果**：def 里没有 AddConfig 的芯片，编译系统直接跳过——「支持哪些芯片」这句话的唯一事实源就是 def 文件。

**待本地验证**：具体日志文案与是否报错终止，需在实际编译环境中确认。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Output("y")` 的 DataType 改成只剩 `{ge::DT_FLOAT}`，会发生什么？
**答案**：框架在算子选择阶段只接受「输出可表达为 FLOAT」的组合；INT32 输入的调用会因为输出 dtype 无法匹配白名单而校验失败。def 中的 dtype 列表本质是按「列位置对齐」的映射表（见 4.2），删掉一项会使所有组合错位，正确做法是同步调整输入侧映射。

**练习 2**：`OP_ADD(AddExample)` 里的 `AddExample` 和算子目录名 `add_example` 是什么关系？
**答案**：`AddExample` 是 op_type（框架内的算子正式名，出现在 binary.json 的 `op_type` 字段、aclnn 生成的注册代码里）；`add_example` 是工程目录名/kernel 文件名（通过 `opFile.value` 绑定）。二者靠命名约定和 def 文件显式关联，而非自动转换。

### 4.2 复杂 def 文件：resize_bilinear_v2 的 dtype 映射表与属性

#### 4.2.1 概念说明

AddExample 的三个端口共用一份 `{DT_FLOAT, DT_INT32}` 列表，含义是「任选一种」。但很多算子的输入输出 dtype 是**联动**的：比如 resize_bilinear_v2 允许「fp16 输入 → fp32 输出」「fp16 输入 → fp16 输出」等 10 种组合，却不允许「fp16 输入 → int32 输出」。这种「组合而非任选」的语义，靠的就是**按列对齐的映射表**：x、y、size 三个端口各有一个长度为 10 的列表，第 i 列合起来就是一种合法组合。这正是 u3-l2 讲过的「dtype 推导由 `dtype` 属性驱动、并校验输入输出组合合法性」在 def 侧的声明基础。

#### 4.2.2 核心流程

映射表的展开逻辑可以理解为：

```text
对第 i 列（i = 0..9）：
  合法组合_i = (x.dtype = valueDataTypeX[i],
               size.dtype = sizeDataType[i],
               y.dtype = valueDataTypeY[i])
框架/编译器把每个组合编译成（或映射到）一个 kernel 二进制
```

即组合总数 = 列数，而不是各端口列表长度的笛卡尔积。每个端口列表必须等长，否则注册失败。

#### 4.2.3 源码精读

文件开头用 `static const std::vector` 定义了四张映射表：

[resize_bilinear_v2_def.cpp:18-35](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp#L18-L35) —— 定义 x/y 的 dtype 映射表（10 列，例如第 1 列是「x:fp16 → y:fp32」、第 2 列「x:float → y:float」）、size 固定 int32，以及 x/y 的格式表（前 5 列 NCHW、后 5 列 NHWC——注意格式表同样按列对齐，dtype 组合与格式组合绑定在一起）。

输入 size 端口有一个 AddExample 没有的声明：

[resize_bilinear_v2_def.cpp:46-51](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp#L46-L51) —— size 输入带 `ValueDepend(OPTIONAL)`，声明框架「可选地」把该输入当作常量值依赖。这正是 u3-l2 讲过的 `IsConstTensor` 判断能成立的前提：def 侧声明 ValueDepend，注册处理才会生成 `InputsDataDependency` 信息，Infershape 才有机会读到 size 的值来推导输出 H/W。三处（def、infershape、aclnn 层）共同构成一条完整链路。

接着是属性声明：

[resize_bilinear_v2_def.cpp:58-61](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp#L58-L61) —— 声明 4 个可选属性并给默认值：align_corners、half_pixel_centers（bool，默认 false）、dtype（int，默认 DT_FLOAT，驱动输出 dtype 推导）、scales（list_float，u3-l2 已说明它在新链路中仅为兼容保留）。属性会原样出现在 binary.json 的 attrs 字段里（见 4.4.3）。

芯片绑定部分出现了新面孔：

[resize_bilinear_v2_def.cpp:63-69](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp#L63-L69) —— `opFile.value` 绑定到 `resize_bilinear_v2_apt`（与 op_kernel 下的 resize_bilinear_v2_apt.cpp 对应），AddConfig 只有 ascend950 和 mc62 两个芯片。结合 u3-l4 讲过的 arch35 子目录（950 系列专属实现），可以看出：def 的 AddConfig 决定「在哪些芯片编」，`op_host/arch35`、`op_host/config/ascend950` 决定「在这些芯片上按什么差异化配置编」。

最后 `OP_ADD(ResizeBilinearV2)`（[L73](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp#L73)）完成注册。

#### 4.2.4 代码实践

**实践目标**：亲手验证「按列对齐」的映射表语义。

**操作步骤**：

1. 对照 [resize_bilinear_v2_def.cpp:18-27](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp#L18-L27)，手工列出全部 10 列的 `(x.dtype, y.dtype)` 组合。
2. 打开 [image/resize_bilinear_v2/op_host/config/ascend950/resize_bilinear_v2_binary.json](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/config/ascend950/resize_bilinear_v2_binary.json)，数一数 `op_list` 里有多少个条目、每个条目的输入输出 dtype 是什么。

**需要观察的现象**：binary.json 中 `op_list` 的条目数（10 个）与 def 映射表的列数一致，且每个条目恰好对应一列 dtype×format 组合（第 0 条即「bfloat16 + NCHW」，对应映射表第 3 列的顺序中的一个）。

**预期结果**：确认「映射表一列 = 一个预编译二进制」的对应关系。二进制文件名是 `ResizeBilinearV2_<hash>`，hash 由该组合的内容决定。

**待本地验证**：10 个条目与 10 列的精确逐列对应顺序，建议本地展开比对一次。

#### 4.2.5 小练习与答案

**练习 1**：为什么 AddExample 不需要 Attr 声明，而 resize_bilinear_v2 需要 4 个？
**答案**：AddExample 语义上没有任何可配置项；resize 的对齐方式、输出 dtype、缩放比例都是调用方可配置的参数。aclnn 接口签名中每一个属性参数，都必须能在 def 文件中找到对应的 `Attr` 声明，否则生成的算子信息缺项、调用无法通过校验。

**练习 2**：`ValueDepend(OPTIONAL)` 如果删掉，最先在哪里暴露问题？
**答案**：在 Infershape 阶段。框架不再把 size 标记为值依赖输入，u3-l2 讲过的 `IsConstTensor` 判断将失效，输出 H/W 无法从 size 的值推导，只能退化为未知维度，进而导致下游校验失败。

### 4.3 从 def 到产物（上）：op_host/CMakeLists 与 opbuild 工具

#### 4.3.1 概念说明

def 文件只是「声明」，编译系统的任务是把这个声明变成三类产物：① 自动生成的 aclnn 接口代码（u2-l2 走读过的那种文件，add_example 的 aclnn 层就是全自动生成的）；② 每个芯片的算子信息库（ini → json）；③ kernel 二进制。前两类由 opbuild 工具完成，第三类由 opc 完成。本模块跟踪前两类。

#### 4.3.2 核心流程

```text
op_host/CMakeLists.txt: add_modules_sources(OPTYPE add_example ACLNNTYPE aclnn)
        ↓ 把 add_example_def.cpp 归入 "aclnn" 前缀的 def 源集合
cmake/opbuild.cmake: gen_aclnn_classify()
        ↓ 把 xxx_def.cpp 编成动态库 gen_op_host_aclnn.so
        ↓ 文件名去 _def 后缀，预定输出 aclnn_xxx.cpp / aclnn_xxx.h
cmake/opbuild.cmake: gen_opbuild_target()
        ↓ 运行 OP_BUILD_TOOL，加载该动态库
        ↓ 导出 aic-<soc>-ops-info.ini（算子信息）+ 生成 aclnn 接口源码
        ↓ 产出统一落在 build/autogen（ASCEND_AUTOGEN_PATH）
```

#### 4.3.3 源码精读

先看算子自己的声明。add_example 的 op_host/CMakeLists.txt 只有一行有效内容：

[examples/add_example/op_host/CMakeLists.txt:11](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/CMakeLists.txt#L11) —— `add_modules_sources` 把本目录下名为 add_example 的算子、以 aclnn 类型登记进模块源集合。公共 CMake 代码随后会自动发现 `add_example_def.cpp` 并纳入后续流程，无需手工列举源文件。

再看 opbuild.cmake 如何组织「def 进、代码出」：

[opbuild.cmake:58-105](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/opbuild.cmake#L58-L105) —— `gen_aclnn_classify` 按 aclnn / aclnnInner / aclnnExc 三种前缀归类 def 源（对应对外发布、仅内部、被排除三类），关键一步在 L86-89：对每个 `*_def.cpp` 做正则替换去掉 `_def` 后缀，预生成 `aclnn_<算子名>.cpp/.h` 的输出路径。`inner`、`exc` 子目录的产物最终会被分开处理（L62-75）。

真正执行生成的是：

[opbuild.cmake:42-49](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/opbuild.cmake#L42-L49) —— 自定义命令以 `OPS_ACLNN_GEN`、`OPS_PRODUCT_NAME`（即全部 `--soc` 芯片列表）等环境变量运行 `${OP_BUILD_TOOL}`（opbuild 工具），输入是 L23 编出来的 def 动态库 `gen_op_host_<prefix>.so`，输出目录在 autogen 路径下。工具加载动态库 → 全局注册对象构造 → 遍历注册表 → 反推 ini 与 aclnn 骨架，这就是「OP_ADD 注册」被消费的时刻。

[opbuild.cmake:128-145](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/opbuild.cmake#L128-L145) —— `gen_aclnn_with_opdef` 是总入口：串起三类前缀的生成，并把所有生成的头文件汇总成一个总头 `aclnn_ops_cv.h`（自定义模式下为 `aclnn_ops_cv_<vendor>.h`），最终安装到算子包的 `op_api/include`。u1-l4 安装 run 包后能直接 `#include "aclnn_add_example.h"`，源头就在这里。

#### 4.3.4 代码实践

**实践目标**：在编译产物中亲眼看到 def 生成的 aclnn 代码。

**操作步骤**：

1. 执行 `bash build.sh --pkg --soc=ascend910b --ops=add_example`。
2. 编译完成后，进入 build 输出目录，查找 autogen 路径（形如 `build_out/.../autogen` 或 `build/autogen`，具体以 `ASCEND_AUTOGEN_PATH` 的展开为准）。
3. 打开生成的 `aclnn_add_example.h` 与同名的 `.cpp`。

**需要观察的现象**：生成的头文件里应有 `ACLNN_API aclnnAddExampleGetWorkspaceSize(...)` 与 `aclnnAddExample(...)` 两段式接口声明，参数顺序为 x1、x2、y、workspaceSize、executor / workspace、executor、stream——与 u2-l1 讲的两段式规范完全一致。

**预期结果**：确认「def 端口声明 → aclnn 接口签名」的自动映射：每个 `Input` 变成一个 `aclTensor*` 参数，每个 `Attr` 变成一个对应类型的属性参数（AddExample 无属性，故无属性参数）。

**待本地验证**：autogen 目录的确切路径与生成文件命名，需在实际构建后确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 def 要先被编成动态库，而不是直接让 opbuild 解析 C++ 源码？
**答案**：def 的声明是「可执行的 C++ 代码」（构造函数 + 链式调用），借助编译器执行它能天然获得类型检查与宏展开的正确性，比写一个 C++ 解析器可靠得多。加载动态库触发全局对象构造，注册表即可枚举所有算子。

**练习 2**：aclnn、aclnnInner、aclnnExc 三个前缀的差别是什么？
**答案**：对应「对外发布的 aclnn 接口」「仅包内部使用的接口」「显式排除不生成接口」三类。aclnnExc 前缀的 def 只贡献算子信息（ini），不生成 aclnn 代码（opbuild.cmake L69-72 中 `need_gen_aclnn` 为 0）。

### 4.4 从 def 到产物（下）：gen_ops_info.cmake 与 config 目录

#### 4.4.1 概念说明

[op 前缀的 ini 生成后，轮到 gen_ops_info.cmake 消费它：把 ini 转成 json 算子信息库、判断每个算子在每款芯片上是否需要编译、准备 opc 编译脚本，并在 `ENABLE_BINARY` 时真正编出 kernel 二进制。本模块同时回答一个关键问题：`op_host/config/<芯片>/` 下的手工配置文件（binary.json、simplified_key.ini）在这条流水线里处于什么位置。

#### 4.4.2 核心流程

```text
opbuild 生成的 aic-<soc>-ops-info.ini
        ↓ merge_ini_files：合并 aclnn/inner/exc 三份 ini
        ↓ add_ops_info_target：ini → aic-<soc>-ops-info.json（安装进算子包）
        ↓ 对每个 (算子, 芯片)：
        │    get_op_type_from_op_name：grep def 文件的 OP_ADD 拿 op_type
        │    check_op_supported：grep def 文件的 AddConfig("芯片名")
        │    若存在 config/<芯片>/<算子>_binary.json → "self config"（用手工清单编）
        │    否则 → generate_bin_scripts 自动生成 binary.json（"auto gen config"）
        ↓ prepare_compile_from_config → compile_from_config：opc 编出二进制
        ↓ gen_binary_info_config_json：生成 binary_info_config.json
```

#### 4.4.3 源码精读

编译系统如何「读懂」def 文件？答案是直接 grep——这正说明 def 是唯一事实源：

[gen_ops_info.cmake:317-333](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/gen_ops_info.cmake#L317-L333) —— `get_op_type_from_op_name` 用 `find ... -name <算子名>_def.cpp -exec grep OP_ADD` 提取 op_type（如 AddExample），没有 OP_ADD 的目录被视为「不需要编译二进制」。

[gen_ops_info.cmake:541-555](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/gen_ops_info.cmake#L541-L555) —— `check_op_supported` 在 def 文件里 grep `.AddConfig("<芯片名>"`，判断该算子是否支持当前 compute_unit。u4.1.4 实践中「ascend310p 下 not supported」的日志即来自调用它的 [L61-113](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/gen_ops_info.cmake#L61-L113) `get_op_type_and_validate`：它优先看 `config/<芯片>/<算子>_binary.json` 是否存在（L81-90，存在则直接从 json 读 op_type 并走 self config 路线），否则回退到 def 推断（L92-107）。

总装流水线在：

[gen_ops_info.cmake:558-599](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/gen_ops_info.cmake#L558-L599) —— `gen_ops_info_and_python` 依次完成：kernel 源码拷贝（`kernel_src_copy`，L565-568，把 op_kernel 拷到 tbe 编译区）、proto 头合并（L570）、对每款芯片生成 ops-info json（L580-584，自定义模式后缀 `ops-info.json`，整包模式 `ops-info-cv.json`）与合并 ini（L587），最后生成动态 shape 的 python 实现入口（L591-598）。

[gen_ops_info.cmake:601-659](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/gen_ops_info.cmake#L601-L659) —— 二进制编译主体：对每个 (算子, 芯片) 再次做 op_type 与 AddConfig 校验（L607-618），然后检查 `config/<芯片>/<算子>_binary.json` 是否存在（L627）：存在则打日志 `compile binary with self config`（用手工清单），不存在则 `compile binary with auto gen config`（L623-626 的 `generate_bin_scripts` 会先用 ini 自动生成一份 binary.json）。无论哪条路，最终都汇入 `prepare_compile_from_config`（L634-657）准备 opc 编译。

现在看 config 目录本身。ascend910b 下 AddExample 的手工清单：

[add_example_binary.json:2-45](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/config/ascend910b/add_example_binary.json#L2-L45) —— `op_type` 为 AddExample；`op_list` 第一个条目声明一个名为 `AddExample_a15328...`（hash 名）的二进制，覆盖「x1:float32/ND + x2:float32/ND → y:float32/ND、shape 全 -2（任意）」的组合。第二个条目（L47-87）是 int32 版本。两个条目正好对应 def 中 `{DT_FLOAT, DT_INT32}` 两个 dtype 选项——binary.json 就是 dtype 白名单的「芯片级物化」。

[add_example_simplified_key.ini:12-13](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/config/ascend910b/add_example_simplified_key.ini#L12-L13) —— `[AddExample] default=0`：控制 opc 编译时 `--simplified_key_mode` 传 0。文件头注释（L1-11）详细说明了 default 与平台差异化配置的组合规则，以及何时应显式配 None 交给 opc 与 FE 框架自行判断——这正是 u3-l4 结尾提到 simplified_key.ini 时的完整上下文。

最后是安装路径的分叉：

[gen_ops_info.cmake:419-447](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/gen_ops_info.cmake#L419-L447) —— 打包时二进制与配置安装到 `BIN_KERNEL_INSTALL_DIR/<芯片>`；`ENABLE_CUSTOM`（--ops 自定义路线）与整包路线的子路径不同（自定义少一层 `ops_cv`）。同时可见 `_apt` 后缀的产物（L437-446）与 `opFile.value` 中 `*_apt` 命名的绑定关系。

#### 4.4.4 代码实践

**实践目标**：跟踪一次「binary.json 缺失时的自动生成」行为。

**操作步骤**：

1. 备份 `examples/add_example/op_host/config/ascend910b/add_example_binary.json`（临时移出目录）。
2. 清理缓存后重新执行 `bash build.sh --pkg --soc=ascend910b --ops=add_example`。
3. 对比两次构建日志：第一次应出现 `compile binary with self config`，第二次应出现 `compile binary with auto gen config`。
4. 在构建目录 `binary/ascend910b/gen/add_example/` 下找到自动生成的 `add_example_binary.json`，与手工版 diff。
5. 完成后把手工版还原。

**需要观察的现象**：自动生成的 binary.json 内容应与手工版语义等价（同样两个 dtype 条目）；说明 config 目录的手工文件是「覆盖/固定」手段，而非必需品——不提供时系统从 def 推导。

**预期结果**：两次构建产物功能一致。这个实验同时解释了 u1-l2 的结论「缺 config 目录意味着没有芯片级差异化配置」。

**待本地验证**：自动生成文件与手工版是否逐字段一致，需本地 diff 确认。

#### 4.4.5 小练习与答案

**练习 1**：同一个算子 op_host/config 下有 ascend910b 子目录但没有 ascend950 子目录，说明什么？
**答案**：说明该算子虽在 def 中 AddConfig 了 ascend950，但只对 ascend910b 提供了芯片级手工差异化配置（binary 清单 / simplified key）；ascend950 上会走 auto gen config 自动推导路线。config 子目录是「按芯片」的可选覆盖层。

**练习 2**：binary.json 里 shape 写 `[-2]` 是什么含义？
**答案**：-2 表示 unknown rank/任意 shape（与 u3-l2 讲的 UnknownRank(-2) 一致），即该二进制对任意维度 shape 通用——这对应 def 中 `DynamicRankSupportFlag(true)` 与 `DynamicShapeSupportFlag(true)` 的开启；动态 shape 由运行期 tiling 分发，无需按 shape 预编多个二进制。

**练习 3**：为什么 `gen_ops_info.cmake` 敢用 grep 解析 def 文件这种「源码文本」？
**答案**：因为 def 文件有严格的仓库级书写约定（一行一个 AddConfig、OP_ADD 固定收尾），grep 在这种受控格式上足够可靠且零依赖；这也提醒贡献者不要随意改变 def 文件的书写格式，否则会破坏构建系统的解析（u8-l3 的 CI 规范会约束这一点）。

## 5. 综合实践：def 文件、README、config 目录的三方契约对照

本讲的综合实践把三个信息载体放在一张表里做逐项对照，以 AddExample 与 ResizeBilinearV2 各做一遍。

**任务**：制作一张对照表，行为信息项、列为两个算子，逐项核对三方一致性：

| 信息项 | def 文件来源 | README 来源 | config 来源 |
| ------ | ------------ | ----------- | ----------- |
| 算子名 | `OP_ADD(AddExample)`（[L81](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/add_example_def.cpp#L81)） | 标题与功能说明 | binary.json 的 `op_type` |
| 支持芯片 | `AddConfig` 列表（[L76-78](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/op_host/add_example_def.cpp#L76-L78)） | 「产品支持情况」表（[README.md:5-9](https://github.com/gitcode.com/cann-ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/README.md#L5-L9)） | config 下的子目录名集合 |
| 端口与 dtype | `Input/Output` 的 DataType | 「参数说明」表 | binary.json 各条目的 inputs/outputs |
| 属性 | `Attr` 声明 | 参数说明表中「属性」行 | binary.json 的 attrs 字段 |
| kernel 绑定 | `opFile.value` | 无（不对外） | 二进制文件名 `*_apt` |

**操作步骤**：

1. 填完 AddExample 一列后，换 [resize_bilinear_v2_def.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp) 与其 README、[config/ascend950](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/config/ascend950/resize_bilinear_v2_binary.json) 再填一列。
2. 重点核对差异：AddExample 的 README 写「FLOAT、INT32」，对应 def 的两元素列表与 binary.json 的 2 个条目；ResizeBilinearV2 的 README 参数表应与 10 列映射表、binary.json 的 10 个条目对上。
3. 记录任何对不上的地方——那要么是文档滞后，要么是你对映射关系理解有偏差，两者都值得深究。

**预期结果**：你会得出本讲最重要的结论：**README 给人看、def 给框架看、config 给编译器看，三者描述同一个算子的同一组事实**。改任何一处（如新增 dtype 支持）必须三方同步，这也是 u8-l3 贡献规范会检查的内容。

## 6. 本讲小结

- def 文件（`*_def.cpp`）是算子的「身份证 + 说明书」：用继承 `OpDef` 的类声明输入输出端口、dtype/format 映射表、属性、编译开关与芯片绑定，最后由 `OP_ADD` 宏注册；它不含任何计算逻辑。
- dtype 支持分两种语义：AddExample 式的「任选」（共用列表）与 ResizeBilinearV2 式的「按列组合」（等长映射表，一列 = 一种合法组合 = 一个预编译二进制）。
- `ValueDepend`、`opFile.value`、`AddConfig` 是 def 里三个最关键的绑定声明：分别连接 Infershape 的常量推导、kernel 实现文件、芯片支持范围。
- def 是「一份声明、多处消费」的单一事实源：opbuild 工具加载 def 动态库自动生成 aclnn 接口代码与算子信息 ini；gen_ops_info.cmake 甚至直接 grep def 文件的 `OP_ADD` 与 `AddConfig` 来决定编译范围。
- `op_host/config/<芯片>/` 是可选的芯片级覆盖层：binary.json 手工指定要编出的二进制清单（缺省时自动生成），simplified_key.ini 控制 opc 的 `--simplified_key_mode` 选项。
- def、算子 README、config 目录构成三方契约：README 给人、def 给框架、config 给编译器，修改算子能力时必须三方同步。

## 7. 下一步学习建议

本讲补全了 op_host 侧最后一块拼图（def 注册）。至此 op_host 的四类文件——def、infershape（u3-l2）、tiling（u3-l3/u3-l4）、公共设施（下一讲 u3-l6）——只剩后者未展开。建议：

1. 下一讲 u3-l6 走读 `common` 目录的公共基础设施（aclnn_check、allocator_utils、tiling_base/tiling_util 等），理解公共层如何服务所有算子。
2. 在进入 u4 之前，回头把 u3-l1 的调用链图补上本讲内容：在「框架按 OpDef 路由」那个环节标注 def 文件路径与 `opFile.value` 绑定。
3. 延伸阅读：挑一个 `objdetect/` 下你感兴趣的算子，找到它的 def 文件，验证本讲的映射表规律是否同样成立（组合式算子如 roi_align 没有 def/ini 产物，正好加深对「缺目录即声明实现方式」的理解）。
