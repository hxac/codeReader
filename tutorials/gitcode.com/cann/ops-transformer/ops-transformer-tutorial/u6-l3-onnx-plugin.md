# u6-l3 ONNX 插件框架：让 ONNX 模型跑上 NPU 算子

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解 **onnx plugin（ONNX 算子插件）** 在「ONNX 模型 → NPU 可执行图」这条转换链路中的位置与职责。
2. 读懂一个插件的两个核心回调：`ParseParamsFn`（解析 ONNX 节点属性）与 `ParseOpToGraphFn`（把 ONNX 节点展开为 GE 子图）。
3. 掌握 NPUFlashAttention、NPUMultiHeadAttention 等插件如何把 ONNX 节点映射到本仓库的 `FlashAttentionScore` 等算子上，以及每种插件的适用场景。
4. 知道插件源码如何被 `build.sh --onnxplugin` 收集进 `liboponnx_plugin_transformer.so`，命名约定是什么。

本讲承接 u6-l2 的 op_graph 层知识：插件展开子图时最终落到的算子节点（如 `ge::op::FlashAttentionScore`），其 IR 原型正是 u6-l2 中讲过的 proto 文件。

## 2. 前置知识

### 2.1 ONNX 是什么

ONNX（Open Neural Network Exchange）是一种跨框架的模型交换格式：PyTorch、TensorFlow 等框架训练出的模型可以导出为 `.onnx` 文件，再由推理引擎加载执行。一个 ONNX 模型本质上是一张**计算图**：

- **节点（NodeProto）**：一次算子调用，例如 `MatMul`、`Softmax`，或者厂商自定义的 `NPUFlashAttention`。
- **属性（AttributeProto）**：挂在节点上的超参数，例如 `head_num = 12`，有明确的类型（INT、FLOAT、STRING 等）。
- **输入/输出列表**：按名字引用图里的 tensor。

ONNX 使用 **protobuf**（Google 的结构化序列化库）定义消息结构，所以插件代码里会看到 `node->attribute()`、`attr.i()`、`attr.s()` 这样的 protobuf 访问接口——分别是「取出属性列表」「读 INT 值」「读 STRING 值」。

### 2.2 模型转换器与插件的位置

昇腾的模型转换工具（ATC，Ascend Tensor Compiler）把 ONNX 模型转换成 NPU 可执行的离线模型（om 文件）。转换的第一步是**图解析**：把 ONNX 的节点翻译成 GE（Graph Engine，u2-l4 与 u6-l2 已见过）能理解的 `ge::Operator` 节点。

问题来了：ONNX 标准算子表里没有 `FlashAttentionScore` 这种 NPU 融合大算子。怎么办？两条路：

1. **拆解**：用多个标准 ONNX 算子（MatMul + Softmax + …）拼出等价计算——精度和性能都差。
2. **插件**：提供一个解析器，告诉转换器「遇到 `npu::1::NPUFlashAttention` 这种节点时，直接映射成 NPU 上的 `FlashAttentionScore` 算子」。

本讲讲的就是第二条路。插件运行在**模型转换期（编译期）**，不在运行期——它产出的是图结构，真正计算仍由 op_kernel 里的 AscendC 核函数完成。

### 2.3 domi 与 REGISTER_CUSTOM_OP

仓库里的插件代码都放在 `namespace domi` 中。`domi` 是 GE 图解析框架的命名空间，插件通过一个链式注册宏把自己登记进去：

```cpp
// 示例代码：注册宏的最小骨架（摘自本仓库真实插件，格式略有简化）
REGISTER_CUSTOM_OP("MultiHeadAttention")   // 注册到 GE 的算子类型名
    .FrameworkType(ONNX)                   // 声明这是 ONNX 框架的插件
    .OriginOpType({...})                   // 能识别哪些 ONNX 域::版本::类型名
    .ParseParamsFn(ParseParamsXxx)         // 属性解析回调
    .ImplyType(ImplyType::TVM);            // 声明由 NPU（TVM 通道）执行
```

`OriginOpType` 里的字符串如 `"ai.onnx::13::NPUFlashAttention"` 是 ONNX 的**三元组寻址**：`域::opset 版本::算子类型名`。一个插件通常把 opset 11~18 全列一遍，这样不管模型用哪个 opset 导出都能命中。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
| --- | --- |
| [common/include/framework/onnx_common.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/framework/onnx_common.h) | 插件公共头：日志名工具 `GetOpName`、tensor 构造工具 `Vec2Tensor`/`CreateScalar` |
| [common/src/framework/multi_head_attention_onnx_plugin.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/multi_head_attention_onnx_plugin.cpp) | NPUMultiHeadAttention 插件：最简「纯属性映射」样本 |
| [attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp) | NPUFlashAttention 插件：带 `ParseOpToGraphFn` 子图展开的完整样本 |
| [attention/flash_attention_score/op_graph/flash_attention_score_proto.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_graph/flash_attention_score_proto.h) | 插件子图的落点：`REG_OP(FlashAttentionScore)` IR 原型（u6-l2 已讲） |
| [attention/incre_flash_attention/framework/npu_incre_flash_attention_onnx_plugin.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/incre_flash_attention/framework/npu_incre_flash_attention_onnx_plugin.cpp) | 推理侧 NPUIncreFlashAttention 插件：动态输入分组的另一变体 |
| [cmake/obj_func.cmake](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/obj_func.cmake) | `add_onnx_plugin_sources`：按文件名 GLOB 收集插件源码的构建约定 |
| [cmake/variables.cmake](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/variables.cmake) | `ONNX_PLUGIN_NAME = oponnx_plugin_${PKG_NAME}`：产物库命名 |
| [build.sh](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh) | `--onnxplugin` 选项：编译 `liboponnx_plugin_transformer.so` |

仓库中现有插件分布在多个算子的 `framework/` 子目录与 `common/src/framework/` 下，可用一条命令全部找到（见 4.1.4 实践）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **onnx plugin 框架**：注册宏、构建收集与产物形态。
2. **属性映射（ParseParamsFn）**：从 ONNX NodeProto 到 GE 算子属性，含类型转换与默认值。
3. **ONNX 算子映射（ParseOpToGraphFn）**：把一个 ONNX 节点展开为 GE 子图并落到 NPU 算子。

### 4.1 模块一：onnx plugin 框架

#### 4.1.1 概念说明

插件框架要回答三个问题：

- **谁触发**：模型转换器解析 ONNX 图时，遇到无法识别的节点类型，就到已加载的插件注册表里按 `OriginOpType` 查找。
- **怎么登记**：每个 `.cpp` 文件里的 `REGISTER_CUSTOM_OP` 宏在**静态初始化期**（so 被 dlopen 时）自动执行注册，无需手工清单——这与 op_host 的 `OP_ADD`、op_graph 的 `REG_OP` 是同一套「自注册」设计哲学（u2-l2、u6-l2 已建立该认知）。
- **怎么编译**：`build.sh --onnxplugin` 产出单独的动态库 `liboponnx_plugin_transformer.so`，安装后被转换工具加载。

#### 4.1.2 核心流程

```text
ONNX 模型文件 (.onnx)
      │  ATC / 图解析器
      ▼
逐节点反序列化为 NodeProto
      │  按 "域::版本::类型名" 匹配 OriginOpType
      ▼
命中插件 ──► ParseParamsFn(op_src, op_dest)
      │      把 ONNX attribute 拷贝/换算到 ge::Operator 属性
      ▼
（可选）ParseOpToGraphFn(op, graph)
      │      用 Data 占位 + 真实 NPU 算子节点搭一张子图
      ▼
GE 计算图 ──► 图编译（tiling、算子选择）──► om 离线模型 ──► NPU 执行
```

两类插件的分工：

| 插件风格 | 提供的回调 | 落点 | 例子 |
| --- | --- | --- | --- |
| 纯属性映射 | 仅 `ParseParamsFn` | 一个已存在的同名 NPU 算子 | NPUMultiHeadAttention |
| 子图展开 | `ParseParamsFn` + `ParseOpToGraphFn` | 一张由 Data/Const/真实算子组成的 GE 子图 | NPUFlashAttention |

#### 4.1.3 源码精读

**注册入口**（[common/src/framework/multi_head_attention_onnx_plugin.cpp:72-81](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/multi_head_attention_onnx_plugin.cpp#L72-L81)）：这段代码把 `NPUMultiHeadAttention` 登记为 ONNX 插件——注册名 `MultiHeadAttention`，匹配 opset 11~18 以及 `npu::1::` 域共 9 种写法，解析函数指向 `ParseParamsMultiHeadAttention`，`ImplyType::TVM` 表示该算子由 NPU 硬件（而非 CPU）执行。

**构建收集约定**（[cmake/obj_func.cmake:871-880](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/obj_func.cmake#L871-L880)）：`add_onnx_plugin_sources` 宏对当前目录做 `file(GLOB *_onnx_plugin.cpp)`——**文件名必须以 `_onnx_plugin.cpp` 结尾**才会被编进插件库，这与 op_host 的 `_tiling` 命名约定（u1-l2 讲过）是同一套「按文件名自动发现」机制。若目录里没有匹配文件则打 WARNING。

**插件对象库与 protobuf 依赖**（[cmake/obj_func.cmake:830-843](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/obj_func.cmake#L830-L843)）：插件需要解析 ONNX 的 protobuf 消息，所以 CMake 先用 `protobuf_generate_external` 从 CANN 包里的 `ge_onnx.proto` 生成解析代码，再建 `${ONNX_PLUGIN_NAME}_obj` OBJECT 库（C++14 标准）。

**产物库命名**（[cmake/variables.cmake:17](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/variables.cmake#L17)）：`ONNX_PLUGIN_NAME = oponnx_plugin_${PKG_NAME}`，本仓库 `PKG_NAME` 为 transformer，故产物是 `liboponnx_plugin_transformer.so`。

**build.sh 入口**（[build.sh:1869-1874](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1869-L1874)）：`--onnxplugin` 把 `oponnx_plugin_transformer` 加入 `BUILD_LIBS` 并置位 `ONNX_PLUGIN=TRUE`；文档说明见 [docs/zh/install/build.md:63](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/build.md#L63)。另外每个 `framework/CMakeLists.txt` 都有 `BUILD_OPEN_PROJECT AND NOT BUILD_OPS_RTY_KERNEL` 的守卫（如 [attention/flash_attention_score/framework/CMakeLists.txt:11-13](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/CMakeLists.txt#L11-L13)），即开源工程形态下才编插件。

#### 4.1.4 代码实践

1. **实践目标**：摸清仓库里到底有多少个插件、分布在哪里，并验证 `--onnxplugin` 构建入口。
2. **操作步骤**：
   - 在仓库根目录执行（源码阅读型，任何环境可做）：

     ```bash
     grep -rln "_onnx_plugin.cpp" --include="*.cpp" .
     bash build.sh --help | grep -A1 onnxplugin
     ```

   - 若已完成 u1-l3 的环境准备，可进一步执行（编译态无需 NPU）：

     ```bash
     bash build.sh --onnxplugin
     ls build/output/
     ```

3. **需要观察的现象**：grep 应列出 9 个左右插件文件，分布在 `common/src/framework/`、`attention/*/framework/`、`moe/*/framework/`；build 帮助信息中 `--onnxplugin` 说明为「build oponnx_plugin_transformer.so」。
4. **预期结果**：编译成功后 `build/output/`（或日志中的安装目录）出现 `liboponnx_plugin_transformer.so`。无 CANN 环境时完成前两步 grep 部分即可，编译部分**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么插件库是独立的 `liboponnx_plugin_transformer.so`，而不是并进 `libophost_transformer.so`？

**答案**：两者消费者不同。ophost 库在**图编译/算子选择**阶段被加载；onnxplugin 库在更早的**图解析**阶段被模型转换器加载，它的入口是 `REGISTER_CUSTOM_OP` 注册表而非算子信息库。分开成库让转换工具按需 dlopen，也避免插件对 protobuf 的依赖污染其他库。

**练习 2**：如果我新写了一个插件文件叫 `my_op_onnx_parser.cpp`，会发生什么？

**答案**：它不会被编译进插件库。[cmake/obj_func.cmake:874](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/obj_func.cmake#L874) 的 GLOB 只匹配 `*_onnx_plugin.cpp`，不匹配的文件被静默忽略（目录里一个都没有时才 WARNING）。命名规范是硬约束，这点与 u1-l2 讲过的 tiling 文件命名约定一致。

### 4.2 模块二：属性映射——ParseParamsFn

#### 4.2.1 概念说明

`ParseParamsFn` 的函数签名是 `Status(const Message* op_src, ge::Operator& op_dest)`：入参是 protobuf 反序列化出的 ONNX 节点（以通用 `Message` 基类传递，需 dynamic_cast 成 `NodeProto`），出参是一个 GE 算子对象。它的工作只有一件事——**把 ONNX 属性搬运并翻译成 GE 算子属性**。翻译不只是改名，还包含：

- **类型换算**：ONNX 的 `dropout_prob`（丢弃概率）→ 算子的 `keep_prob`（保留概率），值取 `1 - x`。
- **必选检查**：统计命中了多少个必需属性，不足即报错返回 FAILED。
- **默认值兜底**：可选属性未提供时用本地初始化的默认值。

#### 4.2.2 核心流程

以 NPUMultiHeadAttention 为例：

```text
遍历 node->attribute()
   ├─ name == "attn_head_num"   且类型 INT    → 存入局部变量, attr_num++
   ├─ name == "attn_dim_per_head" 且 INT     → 同上
   ├─ ... 共 6 个必需属性
   └─ 其他属性 → 忽略
attr_num != 6 ? → OP_LOGE 报错, return FAILED
否则 → op_dest.SetAttr(...) 逐个写入（dropout_prob 换算为 keep_prob）
```

#### 4.2.3 源码精读

**属性遍历与计数**（[common/src/framework/multi_head_attention_onnx_plugin.cpp:35-61](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/multi_head_attention_onnx_plugin.cpp#L35-L61)）：这段代码遍历 ONNX 节点的所有属性，按「属性名 + protobuf 类型」双重匹配提取 6 个必需属性（头数、每头维度、源/目标序列长度、dropout 概率、softmax 是否用浮点）；每个属性同时用 `attr.type() == ge::onnx::AttributeProto::INT` 校验类型，防止字符串 "12" 被误当整数；计数不足 `REQUIRED_ATTRS_NUM`（6）时打日志并返回 FAILED。

**属性写出与语义换算**（[common/src/framework/multi_head_attention_onnx_plugin.cpp:62-68](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/multi_head_attention_onnx_plugin.cpp#L62-L68)）：这里把局部变量写入 GE 算子属性。注意第 66 行的语义换算——ONNX 的 `dropout_prob` 被翻译成算子的 `keep_prob = 1 - dropout_prob`，第 67 行还把 INT 的 `softmax_use_float` 强转为 bool。**这正是「映射规则」最典型的样本：插件是两种生态之间的翻译层。**

对比一个更工业化的版本——NPUFlashAttention 的属性解析（[attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp:134-160](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L134-L160)）：只有 `head_num` 和 `input_layout` 两个是必需属性（`required_attr_num != REQUIRED_ATTR` 即失败），`scale`、`keep_prob`、`pre_tockens`、`next_tockens`、`inner_precise`、`sparse_mode` 都有本地默认值兜底（如 `scale = 1.0f`、`sparse_mode = 0`），且日志换用了 u4-l2 见过的结构化宏 `OP_LOGE_FOR_INVALID_ARGUMENT_WITH_REASON`。

#### 4.2.4 代码实践

1. **实践目标**：手工拆解 NPUMultiHeadAttention 的完整属性映射表，体会「翻译层」的角色。
2. **操作步骤**：
   - 阅读 [common/src/framework/multi_head_attention_onnx_plugin.cpp:21-69](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/multi_head_attention_onnx_plugin.cpp#L21-L69)。
   - 画一张三列表格：ONNX 属性名 | protobuf 类型 | GE 算子属性名（含换算）。
3. **需要观察的现象**：6 个属性里有 5 个是直拷贝，只有 1 个做了数值换算、1 个做了类型强转。
4. **预期结果**：表格应包含 `attn_head_num(INT)→attn_head_num`、`dropout_prob(FLOAT)→keep_prob=1-x`、`softmax_use_float(INT)→softmax_use_float(bool)` 等条目；并能回答「如果 ONNX 模型缺了 src_len 属性会怎样」——解析失败，整图转换终止。

#### 4.2.5 小练习与答案

**练习 1**：为什么属性匹配要同时检查 `attr.name()` 和 `attr.type()`，只查名字不行吗？

**答案**：ONNX 允许同名属性在不同导出路径下类型不同（如 INT 被导出成 INTS 或 STRING）。只查名字会把错误类型的值读进来——`attr.i()` 对非 INT 属性是未定义行为。双重检查是最便宜的防御，符合 u4-l2 讲过的「便宜检查在前」的漏斗原则。

**练习 2**：`ParseParamsFn` 里做不做 shape 校验合适？

**答案**：不合适。这个阶段只有节点属性，输入 shape 信息尚不完整；shape/dtype 约束应由算子信息库（op_host def，u2-l2）与图编译期校验承担。插件只负责翻译，职责单一。

### 4.3 模块三：ONNX 算子映射——ParseOpToGraphFn 子图展开

#### 4.3.1 概念说明

纯属性映射（4.2）有个前提：存在一个「一一对应」的同名 NPU 算子。但真实场景常需要**改写计算结构**——例如 ONNX 的 NPUFlashAttention 节点没有 `drop_mask` 输入（推理不需要 dropout 掩码），而 NPU 的 `FlashAttentionScore` 算子原型里 `drop_mask` 是一个 optional 输入。插件的处理方式是：**现场造一个全 1 的 drop_mask 常量子图**补上这个洞。

这就是 `ParseOpToGraphFn(op, graph)` 的职责：接收上一步 `ParseParamsFn` 产出的 GE 算子，把它展开成一张完整的 GE 子图，图里可以有：

- `ge::op::Data`：占位输入节点（u2-l4 讲 GE 图执行时已见过这个角色）。
- `ge::op::Const`：编译期常量。
- 辅助算子（如 `Fill`、`Cast`）补齐目标算子缺的输入。
- 最终的 NPU 算子节点（如 `ge::op::FlashAttentionScore`）——它的类定义来自 u6-l2 讲过的 proto 头文件。

#### 4.3.2 核心流程

以 NPUFlashAttention 为例：

```text
ParseParamsFlashAttention     (ONNX 属性 → 占位算子属性 + 记录输入/输出个数)
        ▼
ParseOpToGraphNpuFlashAttentionScore
   1. 建 9 个 Data 占位节点 data0..data8（对应 query/key/value/real_shift/
      padding_mask/atten_mask/prefix/actual_seq_qlen/actual_seq_kvlen）
   2. 构造全 1 的 drop_mask：
      Const(scalar=1, DT_UINT8) ──► Fill(dims=[L]) ──► Cast(DT_UINT8)
      其中 L 由输入 shape 与 head_num 推出，128 对齐后除以 8，再加 32 冗余
   3. 实例化 ge::op::FlashAttentionScore：
      9 个 Data + cast 后的 drop_mask 填入 10 路输入，8 个属性填入
   4. graph.SetInputs(9 个 Data).SetOutputs({FlashAttentionScore 的第 3 路输出})
        ▼
GE 子图替换原 ONNX 节点，进入正常图编译流程
```

#### 4.3.3 源码精读

**动态输入登记**（[attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp:30-37](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L30-L37)）：`UpdateFlashAttentionByNode` 按 ONNX 节点实际的输入/输出个数调用 `DynamicInputRegister`/`DynamicOutputRegister`——ONNX 侧输入数量是建模方决定的（optional 输入可省略），插件不能用固定个数假设；同时把原始节点名和 `original_type` 存进属性，供后续子图展开时追溯。

**drop_mask 常量子图**（[attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp:203-216](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L203-L216)）：这是本讲最精彩的一段。注释明确写着「为推理创建一个填充标量 1 的 drop_mask 输入」。它先用 `CreateScalar(ONE, ge::DT_UINT8)` 造标量 1，用 `Fill` 算子按 dims 广播成向量，再 `Cast` 成 UINT8（proto 中 drop_mask 的白名单正是 `DT_UINT8`）。掩码长度由 `GetFinalDimsByOperator`（[同文件:89-106](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L89-L106)）根据 BSH/SBH 布局与 head_num 估算：字节数按 128 对齐、除以 8（一个 bit 管一个 token，u2-l4 讲过 drop_mask 是 bitmap 语义），再加 32 字节冗余。

**落到真实 NPU 算子**（[attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp:218-229](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L218-L229)）：实例化 `ge::op::FlashAttentionScore`，把 9 个 Data 占位与 cast 后的 drop_mask 逐一接到 `set_input_query/key/value/...`，8 个属性经 `set_attr_scale_value/keep_prob/...` 填入；最后 `SetOutputs` 只取该算子的第 `OUTPUT_INDEX = 3` 路输出。对照 proto（[attention/flash_attention_score/op_graph/flash_attention_score_proto.h:101-105](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_graph/flash_attention_score_proto.h#L101-L105)）可见输出顺序为 softmax_max(0)、softmax_sum(1)、softmax_out(2)、attention_out(3)——即 ONNX 节点的输出就是最终注意力输出 `attention_out`，中间两路 softmax 统计量被丢弃。**这就是「ONNX 算子 → NPU 算子」映射合同的最终落点。**

**注册：带子图展开的版本**（[attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp:234-247](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L234-L247)）：与 4.1.3 的 MultiHeadAttention 注册相比，这里多了 `.ParseOpToGraphFn(ParseOpToGraphNpuFlashAttentionScore)` 一环；注册名用的是 `PartitionedCall`（转换框架中承载「需子图替换」语义的登记通道），实际匹配仍由 `OriginOpType` 列表（`npu::1::NPUFlashAttention` 及 ai.onnx 11~18）决定。

**公共工具**（[common/include/framework/onnx_common.h:54-62](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/framework/onnx_common.h#L54-L62) 与 [common/include/framework/onnx_common.h:44-52](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/framework/onnx_common.h#L44-L52)）：`CreateScalar` 把一个标量包成零维 ge::Tensor、`Vec2Tensor` 把 vector 包成带 shape/dtype 的 Tensor，二者是构造 Const 节点的标准积木——这印证了 u3-l2 的结论：公共库按「消费者」分层，`framework/` 这层专门服务插件。

**变体样本**：推理侧的 NPUIncreFlashAttention 插件（[attention/incre_flash_attention/framework/npu_incre_flash_attention_onnx_plugin.cpp:31-42](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/incre_flash_attention/framework/npu_incre_flash_attention_onnx_plugin.cpp#L31-L42)）展示了另一种输入组织：固定 4 个必需输入之外，剩余输入按 `GROUP_SIZE = 2` 一组解释为若干组 (key, value)——把 KV Cache 的多组张量编码进单个 ONNX 节点的动态输入列表，组数不整除即报错。

#### 4.3.4 代码实践

1. **实践目标**：写出 NPUFlashAttention 插件的完整「ONNX 输入约束 → NPU 算子对应关系表」。
2. **操作步骤**：
   - 通读 [attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp:118-231](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L118-L231)。
   - 对照 [attention/flash_attention_score/op_graph/flash_attention_score_proto.h:75-127](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_graph/flash_attention_score_proto.h#L75-L127) 的输入/属性声明，逐路确认映射。
   - 整理成两张表：输入表（ONNX 位置 i → dataN → 算子输入名 → proto dtype 白名单）与属性表（ONNX 属性 → 算子属性 → 是否必需/默认值）。
3. **需要观察的现象**：ONNX 侧 9 路输入里没有 drop_mask，它是插件用 Fill+Cast 现造的；输出侧只透传了第 4 路（attention_out）。
4. **预期结果**：输入表 10 行（9 个 Data + 1 个造出的 drop_mask）、属性表 8 行；必需属性只有 head_num 与 input_layout。dtype 白名单以 proto 为准：query/key/value 支持 FP8/FP16/BF16/FP32。完整的逐 dtype 运行验证**待本地验证**（需要有转换工具链的环境）。

#### 4.3.5 小练习与答案

**练习 1**：为什么推理插件要造一个「全 1 的 drop_mask」而不是给算子传个空指针？

**答案**：在 GE 图模式里，算子节点的输入是图上的边，optional 输入不接边与接一个全 1 掩码在语义上不同：keep_prob=1 时全 1 掩码显式表达「所有位置都保留」，与训练期传真实掩码的路径完全一致，kernel 无需为「空输入」增加分支。插件选择在编译期补一个常量子图，把运行期不确定性消灭在转换阶段。

**练习 2**：`GetFinalDimsByOperator` 里 `(numels + ALIGN_NUM - 1) / ALIGN_NUM * ALIGN_NUM / ONE_BYTE_BITS` 这段算式在做什么？

**答案**：先做 128 字节向上对齐（`(x + 127) / 128 * 128`，经典的手写 ceil-div 取整），再除以 8——因为 drop_mask 是 bitmap，1 个字节管 8 个 token；最后再加 32 字节冗余量。这属于典型的「对齐 + 余量」防御式长度估算。

**练习 3**：如果要让 NPUFlashAttention 也输出 softmax_out（第 2 路），最小改动是什么？

**答案**：在 [npu_flash_attention_score_onnx_plugin.cpp:228](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L228) 的 outputs 向量里再 `emplace_back(AttentionScore, std::vector<std::size_t>{2})` 一项，同时 ONNX 模型侧节点需声明两路输出。这是子图展开方式的直接好处——输出选择是显式可编程的。

## 5. 综合实践

**任务：为 NPUMultiHeadAttention 与 NPUFlashAttention 各写一份「插件映射说明书」，并对比两种插件风格。**

具体步骤：

1. **输入约束梳理**：
   - NPUMultiHeadAttention：从 [common/src/framework/multi_head_attention_onnx_plugin.cpp:35-61](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/multi_head_attention_onnx_plugin.cpp#L35-L61) 提取 6 个必需属性及其类型；
   - NPUFlashAttention：从 [npu_flash_attention_score_onnx_plugin.cpp:134-160](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L134-L160) 提取 2 个必需属性 + 6 个带默认值属性，并记录输入个数是如何由 `DynamicInputRegister` 决定的。

2. **落到 NPU 算子的对应关系**：
   - NPUMultiHeadAttention：目标算子是注册名 `MultiHeadAttention` 对应的 NPU 算子，属性一一搬运（含 keep_prob 换算）；
   - NPUFlashAttention：目标算子是本仓库 `FlashAttentionScore`（proto 见 u6-l2），整理「ONNX 输入位置 → dataN → 算子输入名」与「ONNX 属性 → 算子属性」两张映射表，并特别标注 drop_mask 是插件造出的第 10 路输入、输出取第 3 路 attention_out。

3. **风格对比结论**：用一段话回答——什么情况下选纯属性映射（存在语义完全对齐的同名算子），什么情况下必须子图展开（需要补输入、选输出、插辅助算子改写结构）。

4. **（可选，需环境）**：若有 CANN 工具链，用 Python 导出一个仅含 `NPUFlashAttention` 节点的最小 onnx 文件（示例代码，属性按第 1 步约束填写），走一遍模型转换，观察是否命中插件；无环境则标注**待本地验证**。

预期产出：两份映射表 + 一段风格对比结论。这份说明书同时就是你向仓库贡献新 ONNX 插件时的设计模板。

## 6. 本讲小结

- **onnx plugin 是转换期翻译层**：它运行在模型转换（图解析）阶段，把 ONNX 自定义节点映射为 GE 图结构，本身不做任何计算；计算仍由本仓库的 op_kernel 核函数完成。
- **自注册机制贯穿全仓库**：`REGISTER_CUSTOM_OP` 静态注册与 `OP_ADD`（op_host）、`REG_OP`（op_graph）是同一设计哲学；构建侧则以 `*_onnx_plugin.cpp` 文件名 GLOB 自动收集，命名是硬约束。
- **`ParseParamsFn` 负责属性翻译**：名称映射、类型校验、数值换算（dropout_prob → keep_prob）、必需性检查与默认值兜底，是最简形态的插件（NPUMultiHeadAttention 即此风格）。
- **`ParseOpToGraphFn` 负责子图展开**：用 Data 占位 + Const/Fill/Cast 辅助节点 + 真实 NPU 算子重写计算结构，NPUFlashAttention 用它现场造全 1 drop_mask、并只透传 attention_out 一路输出。
- **映射合同的落点是 proto**：插件填的每个输入名/属性名都必须在 `REG_OP(FlashAttentionScore)` 原型中存在且 dtype 命中白名单，proto（u6-l2）与插件互为对方的存在前提。
- **产物与入口**：`bash build.sh --onnxplugin` 编译出独立的 `liboponnx_plugin_transformer.so`，库命名由 `cmake/variables.cmake` 的 `ONNX_PLUGIN_NAME` 决定。

## 7. 下一步学习建议

- **下一讲 u6-l4（调试与调优）**：插件产出的图最终要经图编译落到设备上，学完本讲后正好接上 profiler 与 NPU Simulator，观察「ONNX 模型 → om → NPU」全链路的实际行为。
- **扩展阅读 1**：`common/src/framework/` 下其余插件（`npu_scaled_masked_softmax_onnx_plugin.cpp`、`embedding_bag_onnx_plugin.cpp`、`fillwindowcache_onnx_plugin.cpp` 等），体会非 attention 域算子的映射套路。
- **扩展阅读 2**：`moe/moe_gating_top_k_softmax/framework/` 与 `moe/moe_compute_expert_tokens/framework/` 的插件（README 首页 2025/12 动态提到的 NPUMoeComputeExpertTokens），结合 u5-l1 的 MoE 链路理解为何 MoE 前处理算子也需要 ONNX 入口。
- **动手方向**：参照 4.1 的命名约定与 4.2/4.3 的两段式骨架，为 u6-l1 自己开发的 my_sum 算子写一个最小 ONNX 插件（只需 ParseParamsFn 即可），把「从零开发算子」延伸到「让算子进入 ONNX 生态」。
