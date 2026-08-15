# u6-l3 ONNX 插件框架：让 ONNX 模型跑上 NPU 算子

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 onnx plugin（ONNX 算子插件）在整条「ONNX 模型 → NPU」链路中的位置：它是模型转换器认识「本项目特有算子」的翻译层。
2. 读懂一个插件的两大回调：`ParseParamsFn`（把 ONNX 节点属性翻译成 GE 算子属性）和 `ParseOpToGraphFn`（把一个 ONNX 节点展开成一段 GE 子图）。
3. 掌握 NPUFlashAttention / NPUMultiHeadAttention 等插件的适用场景与输入约束，并能写出「ONNX 输入 → NPU 算子」的对应关系表。
4. 理解插件与本仓库其他层的协作：插件落点是 `op_graph` 目录里 `REG_OP` 声明的 IR 算子（承接 u6-l2），而 IR 算子又由 op_host/op_kernel 支撑——插件自己从不写计算。

## 2. 前置知识

本讲是 advanced 层讲义，默认你已修完 u6-l2（op_graph 与图融合）。以下概念用通俗语言再过一遍：

- **ONNX**：Open Neural Network Exchange，一种跨框架的模型交换格式。一个 ONNX 模型就是一张计算图：节点（Node）代表算子，节点上挂**属性**（attribute，如 `head_num=12`）和输入输出张量。protobuf 的 `NodeProto` 就是节点在内存里的表示。
- **ATC（模型转换器）**：CANN 里把 ONNX/TF 等模型编译成 NPU 可执行文件（om）的工具。ATC 遇到不认识的节点类型时，会去加载「插件库」，把未知节点翻译成自己认识的算子——这就是本讲的主角。
- **`ai.onnx::11::Xxx` 与 `npu::1::Xxx`**：ONNX 节点类型的全名格式是「域::算子集版本::类型名」。`ai.onnx` 是标准域（11~18 是算子集版本号），`npu` 是本项目自定义域，专门容纳 NPU 特有的大算子（如 `NPUFlashAttention`）。
- **GE 算子（IR 算子）**：图引擎（Graph Engine）世界的算子，由 u6-l2 讲过的 `REG_OP` 宏声明（proto 文件）。`ge::Operator` 是它的 C++ 句柄，`SetAttr`/`GetAttr` 读写属性，`ge::op::Data` 是图占位输入。
- **插件不调用 aclnn**：这是最容易混淆的一点。aclnn（u3-l1）是 Eager 直调路径；插件服务的是图编译路径——它把 ONNX 节点翻译成 IR 算子节点，后续由 GE 统一调度 op_host 的 tiling 与 op_kernel 的二进制。两条路径最终执行的是**同一份** host/device 实现。
- **ImplyType::TVM**：注册时标记「翻译后的算子由 NPU 上的算子引擎执行（而非 CPU）」。可以粗记为：这个算子有真 kernel，会被调度到 AI Core 上。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
| --- | --- |
| [common/src/framework/](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework) | 公共插件目录，8 个插件：NPUMultiHeadAttention、NPUFusedAttentionScore（含 Fwd）、NPUScaledMaskedSoftmax、EmbeddingBag、TfIdfVectorizer 等 |
| [attention/flash_attention_score/framework/](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework) | NPUFlashAttention 插件，本讲精读主对象（子图展开级） |
| [moe/moe_init_routing/framework/](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_init_routing/framework) | NPUMoeInitRouting 插件（属性翻译级最小样本，仅 58 行） |
| [common/include/framework/onnx_common.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/framework/onnx_common.h) | 插件公共工具箱：GetOpName / Vec2Tensor / CreateScalar / ChangeFormatFromOnnx |
| [attention/flash_attention_score/op_graph/flash_attention_score_proto.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_graph/flash_attention_score_proto.h) | 插件的「落点」：REG_OP 声明的 FlashAttentionScore IR 算子 |
| [moe/moe_init_routing/op_graph/moe_init_routing_proto.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_init_routing/op_graph/moe_init_routing_proto.h) | 属性翻译级插件的落点，可与插件代码形成完整闭环 |
| [cmake/obj_func.cmake](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/obj_func.cmake)、[cmake/symbol.cmake](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/symbol.cmake)、[cmake/variables.cmake](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/variables.cmake) | 插件库的构建装配：GLOB 收集 → OBJECT 库 → 单一动态库 |
| [examples/add_example/op_host/add_example_def.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp) | 对照物：def（算子信息库）注册方式，说明插件是独立于 def/proto 的第三种「注册面」 |

仓库内插件共 17 个，分布为：common 8 个、attention 3 个（flash/incre/prompt）、moe 5 个、posembedding 1 个。官方入口说明见 [README.md:15](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md#L15)（2025/12 支持 transformer 类 onnx 算子插件）。

## 4. 核心概念与源码讲解

### 4.1 onnx plugin 框架：ONNX 世界的「海关翻译官」

#### 4.1.1 概念说明

ONNX 标准算子集里只有 MatMul、Softmax 这类小粒度算子，没有 FlashAttention、MoE 路由这种融合大算子。当一个模型导出 ONNX 时带了 `npu::1::NPUFlashAttention` 这样的自定义节点，ATC 默认不认识它，转换就会失败。

onnx plugin 解决这个问题：它是一个**注册表 + 回调函数**机制——插件向 GE 注册「我认识哪些 ONNX 节点类型，遇到它们时 call 我」。核心注册链是：

```
REGISTER_CUSTOM_OP("<GE 侧算子类型名>")   // 注册到 GE 的自定义算子登记处
    .FrameworkType(ONNX)                 // 声明服务 ONNX 前端
    .OriginOpType({...})                 // 我能翻译哪些 ONNX 节点类型全名
    .ParseParamsFn(...)                  // 回调1：翻译属性
    .ParseOpToGraphFn(...)               // 回调2（可选）：展开成子图
    .ImplyType(ImplyType::TVM);          // 标记由 NPU 算子引擎执行
```

按翻译能力的深浅，仓库里的插件分两档：

| 档位 | 回调 | 代表插件 | 适用场景 |
| --- | --- | --- | --- |
| 属性翻译级 | 仅 `ParseParamsFn` | NPUMultiHeadAttention、NPUMoeInitRouting、EmbeddingBag、TfIdfVectorizer | ONNX 节点与某个 NPU IR 算子**一一对应**，只需搬属性 |
| 子图展开级 | `ParseParamsFn` + `ParseOpToGraphFn` | NPUFlashAttention、NPUFusedAttentionScore（含 Fwd）、NPUMaskedSoftmaxWithRelPosBias | 一个 ONNX 节点要变成**多个** GE 算子组成的子图（补常量、补 mask、裁剪输出） |

#### 4.1.2 核心流程

一个 ONNX 模型被转换的完整链路：

```
ONNX 模型文件
    │  ATC 逐节点解析（protobuf 反序列化）
    ▼
节点类型全名 = "<domain>::<version>::<op_type>"（如 npu::1::NPUFlashAttention）
    │  在已加载插件库的 OriginOpType 登记表中查名
    ▼
命中插件 → 调用 ParseParamsFn(op_src=NodeProto, op_dest=ge::Operator)
    │      把节点属性逐个搬进 GE 算子（可改名/换类型/换算）
    ▼
（若注册了 ParseOpToGraphFn）→ 以该算子为「中转站」展开成 GE 子图
    │      子图里的节点 = op_graph proto 声明的 IR 算子
    ▼
GE 整图编译 → op_host 的 tiling/infershape + op_kernel 二进制 → om 文件
```

注意两段式的分工：`ParseParamsFn` 把 ONNX 属性**先暂存**到一个 GE 算子上；`ParseOpToGraphFn` 再把这个算人**消费掉**、吐出真正的子图。所以子图展开级插件里你会看到 `SetAttr("name", node->name())`、`SetAttr("original_type", ...)` 这类「行李寄存」式代码——它们不是最终算子的属性，只是给第二段回调取用。

#### 4.1.3 源码精读

**① 最小样本：NPUMoeInitRouting（属性翻译级，全文 58 行）**

先看翻译函数本体，它把 ONNX 节点的 `active_num` 属性搬进 GE 算子：

- [moe/moe_init_routing/framework/npu_moe_init_routing_onnx_plugin.cpp:L21-L42](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_init_routing/framework/npu_moe_init_routing_onnx_plugin.cpp#L21-L42)

这段代码做了三件事：把 `op_src` 安全转型为 `NodeProto`（ONNX 节点）；遍历 `node->attribute()` 找到名为 `active_num` 且类型为 INT 的属性；用 `op_dest.SetAttr("active_num", ...)` 写入 GE 算子。若一个必需属性都没找到，打日志并返回 `FAILED` 让 ATC 拒绝该模型。

再看注册链，声明「GE 侧我叫 MoeInitRouting，我认识 npu::1:: 与 ai.onnx::11~18:: 域下的 NPUMoeInitRouting」：

- [moe/moe_init_routing/framework/npu_moe_init_routing_onnx_plugin.cpp:L45-L58](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_init_routing/framework/npu_moe_init_routing_onnx_plugin.cpp#L45-L58)

关键闭环验证：`REGISTER_CUSTOM_OP("MoeInitRouting")` 里的名字与 u6-l2 讲过的 proto 声明**同名**——

- [moe/moe_init_routing/op_graph/moe_init_routing_proto.h:L39-L48](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_init_routing/op_graph/moe_init_routing_proto.h#L39-L48)

proto 里 `REG_OP(MoeInitRouting)` 声明了 3 输入 3 输出和 `REQUIRED_ATTR(active_num, Int)`——插件搬运的正是这个必需属性。属性翻译级插件的合同因此非常清晰：**REGISTER_CUSTOM_OP 的名字 = 目标 IR 算子名，SetAttr 的名字 = IR 算子属性名**。

**② 公共工具箱 onnx_common.h**

所有插件都 `#include "onnx_common.h"`，它提供四个小工具：

- [common/include/framework/onnx_common.h:L23-L29](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/framework/onnx_common.h#L23-L29) 引入 GE 注册接口、图接口与 ONNX protobuf 定义（`onnx/proto/ge_onnx.pb.h`）。
- [common/include/framework/onnx_common.h:L32-L42](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/framework/onnx_common.h#L32-L42) `GetOpName`：安全取算子名，失败返回 `"None"`——所有日志都靠它定位是哪个节点出错。
- [common/include/framework/onnx_common.h:L44-L52](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/framework/onnx_common.h#L44-L52) `Vec2Tensor`：把 `vector<T>` 包装成带 shape/dtype 的 `ge::Tensor`，用于造常量。
- [common/include/framework/onnx_common.h:L54-L62](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/framework/onnx_common.h#L54-L62) `CreateScalar`：造标量常量张量（如「全 1 的 drop_mask」的种子值）。
- [common/include/framework/onnx_common.h:L64-L86](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/framework/onnx_common.h#L64-L86) `ChangeFormatFromOnnx`：按索引改写输入/输出 TensorDesc 的 format，供需要显式标注排布的插件使用。

这承接了 u3-l2 的结论：common 库按「消费者」分层组织，`include/framework` 这一层服务的正是插件开发者。

**③ 对照物：def 文件是另一张「注册面」**

- [examples/add_example/op_host/add_example_def.cpp:L18-L54](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_def.cpp#L18-L54)

`OP_ADD` 注册的是**算子信息库**（op_host 侧，管 dtype 白名单、tiling 入口），u6-l2 的 `REG_OP` 注册的是**图 IR 原型**，本讲的 `REGISTER_CUSTOM_OP` 注册的是**前端翻译器**。三张注册面相互独立：add_example 没有 framework 目录，所以它支持 aclnn 与图模式，但不能被 ONNX 模型直接引用——想让 ONNX 模型用上一个算子，proto（落点）与插件（翻译）两层都不可少。

#### 4.1.4 代码实践

**实践目标**：亲手把插件库编出来，确认注册字符串真实进入产物。

**操作步骤**：

1. 按 u1-l3 准备好编译态环境（只需 CANN toolkit，无需 NPU）。
2. 执行 `bash build.sh --onnxplugin`（该选项在帮助中的说明见 [build.sh:366](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L366)，参数分发分支见 [build.sh:1869-L1871](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1869-L1871)）。
3. 在 `build/output/` 下找到 `liboponnx_plugin_transformer.so`。
4. 用 `strings liboponnx_plugin_transformer.so | grep -E "NPUFlashAttention|NPUMultiHeadAttention"` 观察注册进去的 OriginOpType 字符串。

**需要观察的现象**：编译日志末尾出现链接 `oponnx_plugin_transformer` 动态库的命令行；`strings` 能命中 `npu::1::NPUFlashAttention` 等类型全名。

**预期结果**：17 个插件的注册字符串全部可见。本讲义编写环境未执行编译，**待本地验证**。若手头没有编译环境，替代实践：`git grep -l "REGISTER_CUSTOM_OP" -- "*.cpp"` 自行列出全部插件文件，与第 3 节的地图核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么插件回调里拿到的是 `const Message *op_src` 而不是直接的 `NodeProto *`？

**参考答案**：`Message` 是 protobuf 的基类，注册接口面向多种前端格式（ONNX 只是其一），只能给最通用的基类指针；插件内部再 `dynamic_cast<const NodeProto *>(op_src)` 下转并判空（如 [multi_head_attention_onnx_plugin.cpp:L23-L27](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/multi_head_attention_onnx_plugin.cpp#L23-L27)），转型失败说明传入的不是 ONNX 节点，返回 FAILED。

**练习 2**：`ImplyType::TVM` 如果换成 CPU 实现的算子应该怎么标？去掉这一行行不行？

**参考答案**：CANN 注册体系里 ImplyType 还有 CPU 等取值，表示算子落在 CPU 上执行。不能去掉：该字段告诉 GE 这个翻译结果由哪类引擎执行，缺失会导致调度信息不完整。

**练习 3**：一个插件文件里 `REGISTER_CUSTOM_OP` 出现两次会怎样？

**参考答案**：两次注册同名 GE 类型会冲突；但不同插件注册**同名** GE 类型是允许且本仓库真实存在的——4 个子图展开级插件都注册为 `"PartitionedCall"`（见 [npu_flash_attention_score_onnx_plugin.cpp:L234](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L234)），ATC 分发时靠 OriginOpType 列表区分，而不是靠 GE 类型名。

### 4.2 ONNX 算子映射·属性翻译级：改名、换类型、换语义

#### 4.2.1 概念说明

属性翻译看似只是「搬砖」，实际要处理三类落差：

1. **改名**：ONNX 社区命名与 NPU 算子命名不一致（如 `dropout_prob` → `keep_prob`）。
2. **换类型**：ONNX 属性是 INT，NPU 算子要 bool/float。
3. **换语义**：ONNX 表达「丢弃概率」，NPU 算子要「保留概率」，数值上要取补。

NPUMultiHeadAttention 是三种落差齐备的教科书样本，本模块精读它。

#### 4.2.2 核心流程

```
遍历 node->attribute()（protobuf 反射式遍历）
    对每个属性：名字匹配 + 类型匹配（AttributeProto::INT / FLOAT）
        命中 → 存入局部变量并计数 ++attr_num
校验 attr_num == 6（六个属性缺一不可）
逐个 SetAttr 写入 GE 算子，其中：
    dropout_prob(float) ──1-p 换算──▶ keep_prob(float)
    softmax_use_float(int 0/1) ──▶ bool
```

属性计数器 `attr_num` 是「必需属性校验」的朴素实现：不匹配名字或类型就不计数，最后数量对不上就整体失败。

#### 4.2.3 源码精读

**① 属性遍历与校验**

- [common/src/framework/multi_head_attention_onnx_plugin.cpp:L35-L61](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/multi_head_attention_onnx_plugin.cpp#L35-L61)

遍历采用 `名字 + 类型` 双重匹配：`attr.name() == "attn_head_num" && attr.type() == ge::onnx::AttributeProto::INT`。INT 属性用 `attr.i()` 取值，FLOAT 属性用 `attr.f()`——这是 protobuf 生成的访问器约定。六个必需属性（`attn_head_num`/`attn_dim_per_head`/`src_len`/`tgt_len`/`dropout_prob`/`softmax_use_float`）任何一个缺失，[L57-L61](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/multi_head_attention_onnx_plugin.cpp#L57-L61) 处 `attr_num != REQUIRED_ATTRS_NUM` 即报错返回。

**② 写入时的语义换算**

- [common/src/framework/multi_head_attention_onnx_plugin.cpp:L62-L67](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/multi_head_attention_onnx_plugin.cpp#L62-L67)

最关键的一行是 `op_dest.SetAttr("keep_prob", static_cast<float>(1 - dropout_prob))`：ONNX 世界用「dropout 概率」表达随机失活强度，NPU 算子用「保留概率」，两者互补。`softmax_use_float` 则从 int 显式转 bool。若不做这两步换算，算子会拿到语义相反/类型不符的参数，且这类错误发生在模型转换期，排查时要优先对照这里。

**③ 注册：认识 9 个版本的类型全名**

- [common/src/framework/multi_head_attention_onnx_plugin.cpp:L72-L81](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/multi_head_attention_onnx_plugin.cpp#L72-L81)

OriginOpType 同时登记了 `ai.onnx::11` ~ `ai.onnx::18` 八个算子集版本与自定义域 `npu::1::` 下的 `NPUMultiHeadAttention`。这意味着导出模型无论声明哪个 opset 版本都能命中。注意本插件没有对应的本仓库 proto——`MultiHeadAttention` 是 CANN 包内建 IR 算子，插件把 ONNX 节点翻译过去后由 CANN 自身的算子实现承接；这是「插件服务的不一定是本仓库算子」的实例。

#### 4.2.4 代码实践

**实践目标**：独立写出 NPUMultiHeadAttention 的属性映射表，并理解失败模式。

**操作步骤**：

1. 精读 [multi_head_attention_onnx_plugin.cpp:L21-L69](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/multi_head_attention_onnx_plugin.cpp#L21-L69)。
2. 画一张 6 列表：ONNX 属性名 / ONNX 类型 / 必需 / GE 属性名 / GE 类型 / 是否换算。
3. 思考：若模型里 `dropout_prob` 写成了 INT 类型（`attr.type()` 不匹配 FLOAT），走到哪一步失败？

**需要观察的现象**：表中只有两行发生「值变换」（keep_prob 取补、softmax_use_float 转 bool），其余四行是纯搬运。

**预期结果**：参考答案表如下——

| ONNX 属性 | ONNX 类型 | 必需 | GE 属性 | GE 类型 | 换算 |
| --- | --- | --- | --- | --- | --- |
| attn_head_num | INT | 是 | attn_head_num | int | 纯搬运 |
| attn_dim_per_head | INT | 是 | attn_dim_per_head | int | 纯搬运 |
| src_len | INT | 是 | src_len | int | 纯搬运 |
| tgt_len | INT | 是 | tgt_len | int | 纯搬运 |
| dropout_prob | FLOAT | 是 | keep_prob | float | \(1 - p\) 取补 |
| softmax_use_float | INT | 是 | softmax_use_float | bool | int→bool |

第 3 步答案：类型不匹配导致计数不增加，最终 `attr_num != 6` 在 [L57-L61](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/multi_head_attention_onnx_plugin.cpp#L57-L61) 报「Node must have attrs ...」并返回 FAILED——错误信息只说缺属性，不会告诉你「类型写错了」，这是排查 ONNX 导出问题时的常见坑。

#### 4.2.5 小练习与答案

**练习 1**：`REQUIRED_ATTRS_NUM` 定为 6，但代码里 `scale` 这样的可选属性如果出现会被怎样处理？

**参考答案**：MHA 插件没有 scale 属性分支，未匹配的属性在遍历中被静默忽略（else 分支都不进），既不报错也不写入。属性级插件普遍采取「只挑认识的，其余忽略」的宽容策略。

**练习 2**：对比 NPUMoeInitRouting（[L29-L40](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/moe/moe_init_routing/framework/npu_moe_init_routing_onnx_plugin.cpp#L29-L40)）与 NPUMultiHeadAttention 的校验风格，有什么异同？

**参考答案**：两者同构：遍历属性 → 名字+类型双匹配 → 计数 → 与必需数量比对 → SetAttr。差异仅在于必需数量（1 vs 6）与是否换算（MoeInitRouting 的 active_num 纯搬运）。这说明属性翻译级插件已形成稳定模板，新写一个此类插件基本是「填表」。

### 4.3 ONNX 算子映射·子图展开级：NPUFlashAttention 两段翻译

#### 4.3.1 概念说明

`NPUFlashAttention`（自定义域大算子）与 NPU 的 `FlashAttentionScore` IR 算子（u4/u6-l2 已精读）并不严格同构，落差有三处：

1. **drop_mask 缺席**：ONNX 节点面向推理导出，不携带 dropout 掩码；而 IR 算子的 drop_mask 是独立输入。插件要**现场伪造**一个全 1 掩码。
2. **输出数量不同**：IR 算子有 4 个输出（softmax_max/softmax_sum/softmax_out/attention_out），ONNX 节点只要主输出。
3. **输入个数不定**：中转算子需要先按节点实际输入数「透传」，再在子图里重新分配角色。

因此该插件注册了完整的两个回调：第一段把属性暂存到名为 `PartitionedCall` 的中转算子上，第二段把中转算子展开成以 `FlashAttentionScore` 为核心的 GE 子图。

#### 4.3.2 核心流程

```
第一段 ParseParamsFlashAttention（[L118-L173]）
    遍历 ONNX 属性 → 暂存 head_num/input_layout/scale/keep_prob/
                     pre_tockens/next_tockens/inner_precise/sparse_mode
    校验：head_num 与 input_layout 必需（计数==2）
    UpdateFlashAttentionByNode：给中转算子注册动态输入 x[N]、动态输出 y[M]，
                                并寄存 name / original_type 供第二段使用

第二段 ParseOpToGraphNpuFlashAttentionScore（[L175-L231]）
    1. 造 9 个 ge::op::Data 占位输入（index 0~8）
    2. 取回 8 个暂存属性
    3. 伪造 drop_mask：CreateScalar(1) → Const → Fill(dims) → Cast(UINT8)
       dims 长度由布局与 head_num 推出（位打包公式）
    4. 实例化 ge::op::FlashAttentionScore：9 个 Data + 伪 drop_mask + 8 个属性
    5. graph.SetInputs(9 个 Data).SetOutputs(仅 attention_out)
```

drop_mask 的字节长度按**位打包**（8 个掩码元素压成 1 字节）推算。以 BSH 布局为例，设 \(B\) 为批大小、\(S\) 为序列长度、\(N\) 为头数（`head_num`），掩码元素总数为 \(B \cdot S \cdot S \cdot N\)，则：

\[
L = \left\lceil \frac{B \cdot S \cdot S \cdot N}{128} \right\rceil \times \frac{128}{8} + 32
\]

即先向上对齐到 128 位（16 字节），除以 8 得字节数，再预留 32 字节余量。全 1 掩码配合 `keep_prob=1.0` 表示「推理时不丢弃任何元素」。

#### 4.3.3 源码精读

**① 第一段：属性暂存与动态输入登记**

- [attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp:L134-L160](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L134-L160)

属性遍历与 4.2 同模板，但只有 `head_num`（INT）和 `input_layout`(STRING) 计入必需计数（`required_attr_num`），其余 6 个属性可选、有默认值（如 `scale=1.0f`、`keep_prob=1.0f`）。STRING 属性用 `attr.s()` 取值。计数不足 2 时用 `OP_LOGE_FOR_INVALID_ARGUMENT_WITH_REASON` 结构化报错（u4-l2 介绍过的 DFX 日志风格）。

- [attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp:L30-L37](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L30-L37)

`UpdateFlashAttentionByNode` 给中转算子 `DynamicInputRegister("x", input_size)` / `DynamicOutputRegister("y", output_size)`——先让任意输入输出个数都能「过海关」，具体角色留给第二段分配；同时寄存 `name` 与 `original_type = "npu::1::NPUFlashAttention"`。

- [attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp:L164-L171](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L164-L171) 把 8 个属性逐一 SetAttr 到中转算子。

**② 第二段：占位输入与伪 drop_mask**

- [attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp:L181-L189](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L181-L189)

9 个 `ge::op::Data` 占位节点（`set_attr_index(0..8)`）代表 ONNX 节点的 9 路输入，命名加 `ori_name` 前缀避免图中重名——这正是 u2-l4 讲过的「Data placeholder 连边组图」手法。

- [attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp:L203-L216](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L203-L216)

伪造 drop_mask 的四步流水：`CreateScalar(ONE, ge::DT_UINT8)` 造标量 1 → Const 固化 → 以 `const_dims` 为形状 `ge::op::Fill` 广播出整块掩码 → `Cast` 到 UINT8。掩码长度由下面的函数推算：

- [attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp:L89-L106](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L89-L106)

`GetFinalDimsByOperator` 从第 0 路输入的 shape 反推：BSH 时 `numels = dims[0] * dims[1] * dims[1] * head_num`（即 \(B \cdot S \cdot S \cdot N\)），SBH 时对偶地取 `dims[1] * dims[0] * dims[0] * head_num`；布局取值不是 BSH/SBH 直接报错。随后做 4.3.2 的位对齐换算。**插件必须自己算掩码大小**，因为 Fill 的 dims 是编译期常量节点，不能引用动态 shape。

**③ 第二段：组装 FlashAttentionScore 并裁剪输出**

- [attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp:L218-L229](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L218-L229)

`ge::op::FlashAttentionScore` 节点把 9 路 Data 接到 query/key/value/real_shift/padding_mask/atten_mask/prefix/actual_seq_qlen/actual_seq_kvlen，伪 drop_mask 插在 real_shift 之后；8 个属性里注意 `set_attr_scale_value(scale)`——ONNX 的 `scale` 在 IR 算子侧叫 `scale_value`，这是本插件唯一的改名映射。输出侧 `outputs.emplace_back(AttentionScore, {OUTPUT_INDEX})` 只保留索引 3（`OUTPUT_INDEX = 3` 定义于 [L27](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L27)）。

对照落点 proto 的输出声明即可验证索引含义：

- [attention/flash_attention_score/op_graph/flash_attention_score_proto.h:L95-L99](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_graph/flash_attention_score_proto.h#L95-L99)

按声明顺序：0=softmax_max、1=softmax_sum、2=softmax_out、**3=attention_out**——插件只把主输出暴露给 ONNX 图，softmax 中间量在图内被丢弃（训练反向才需要它们，这正是 u4-l1 讲过的「训练 Score 与推理产品线分工」在插件层的体现）。

输入侧的 dtype 白名单也完全由 proto 决定（[flash_attention_score_proto.h:L76-L94](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_graph/flash_attention_score_proto.h#L76-L94)）：query/key/value 支持 FP8_E5M2/FP8_E4M3FN/FP16/BF16/FP32，atten_mask 为 BOOL/UINT8，prefix 与 actual_seq_qlen/kvlen 为 INT64——u2-l4 的结论「def/proto 的 dtype 白名单是算子能力边界」在 ONNX 路径同样成立。

**④ 属性默认值的一个陷阱**

插件里 `inner_precise` 默认 1（[L132](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L132)）、`pre_tockens/next_tockens` 默认 0，而 proto 的默认值是 `inner_precise=0`、`pre_tockens/next_tockens=2147483647`（[flash_attention_score_proto.h:L100-L111](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_graph/flash_attention_score_proto.h#L100-L111)）。因为插件**无条件** SetAttr 这 8 个属性，ONNX 侧缺省时实际生效的是插件默认值而非 proto 默认值。写映射表时必须以插件代码为准。

**⑤ 注册**

- [attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp:L234-L247](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L234-L247)

GE 类型名 `PartitionedCall`（沿用 TensorFlow 导出 ONNX 时函数调用节点的习惯名），OriginOpType 覆盖 `npu::1::` 与 `ai.onnx::11~18::` 九种全名，两个回调齐挂。同目录下的兄弟实现可对照阅读：NPUFusedAttentionScore 插件（[common/src/framework/npu_fused_attention_score_onnx_plugin.cpp:L113-L160](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/npu_fused_attention_score_onnx_plugin.cpp#L113-L160)）用同样的「Fill+Cast 造全 1 掩码」手法落到 `ge::op::AttentionScore`（该 IR 算子由 [common/include/op_graph/op_transformer_proto_extend.h:L82](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/include/op_graph/op_transformer_proto_extend.h#L82) 的 `REG_OP(AttentionScore)` 声明）。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：完成规格指定的任务——梳理 NPUFlashAttention 插件「ONNX 节点 → aclnn 参数」的映射规则，写出输入约束与落点对应关系表。

**操作步骤**：

1. 通读 [npu_flash_attention_score_onnx_plugin.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp) 全文 248 行，标出每次 `SetAttr` 与 `set_input_*`/`set_attr_*`。
2. 打开 [flash_attention_score_proto.h:L75-L112](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_graph/flash_attention_score_proto.h#L75-L112)，把 IR 算子的输入/输出/属性三张清单抄下来。
3. 结合 u4-l2 的 aclnn 接口知识，把三层名字对齐成一张表。
4. 回答两个检验问题：(a) ONNX 节点第 4 路输入接到了 IR 算子的哪个输入？(b) 为什么 ONNX 节点没有 drop_mask 输入？

**需要观察的现象**：属性名在三层几乎同名（唯一改名是 scale → scale_value → aclnn 的 scaleValue）；输入索引 4 接的是 padding_mask 而不是 drop_mask。

**预期结果**：参考答案表如下。

**输入约束与落点对应表（NPUFlashAttention，9 路输入）**：

| ONNX 输入索引 | IR 算子输入 | proto dtype 白名单 | 说明 |
| --- | --- | --- | --- |
| 0 | query | FP8_E5M2 / FP8_E4M3FN / FP16 / BF16 / FP32 | 布局由 input_layout 属性决定 |
| 1 | key | 同 query | |
| 2 | value | 同 query | |
| 3 | real_shift（可选） | FP16 / BF16 / FP32 | 位置编码偏置 |
| —（不来自 ONNX） | drop_mask | UINT8 | 插件用 Fill+Cast 伪造全 1 掩码 |
| 4 | padding_mask（可选） | FP16 / BF16 / FP32 | |
| 5 | atten_mask（可选） | BOOL / UINT8 | |
| 6 | prefix（可选） | INT64 | |
| 7 | actual_seq_qlen（可选） | INT64 | 变长场景 |
| 8 | actual_seq_kvlen（可选） | INT64 | 变长场景 |
| 输出 | 仅 attention_out（IR 输出索引 3） | FP16 / BF16 / FP32 | softmax 三个中间输出被裁剪 |

**属性映射表**：

| ONNX 属性 | 类型 | 必需 | ONNX 缺省默认 | IR 算子属性 | aclnn 侧参数（u4-l2） |
| --- | --- | --- | --- | --- | --- |
| head_num | INT | 是 | — | head_num | headNum |
| input_layout | STRING | 是 | — | input_layout | inputLayout（仅 BSH/SBH） |
| scale | FLOAT | 否 | 1.0 | **scale_value**（唯一改名） | scaleValue |
| keep_prob | FLOAT | 否 | 1.0 | keep_prob | keepProb |
| pre_tockens | INT | 否 | 0（注意与 proto 默认 2147483647 不同） | pre_tockens | preTockens |
| next_tockens | INT | 否 | 0（同上） | next_tockens | nextTockens |
| inner_precise | INT | 否 | 1（proto 默认 0） | inner_precise | innerPrecise |
| sparse_mode | INT | 否 | 0 | sparse_mode | sparseMode |

检验问题答案：(a) 索引 4 的 `data4` 接 `set_input_padding_mask`（[L221](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L221)）；(b) 该插件面向推理导出的模型，keep_prob 恒为 1，drop_mask 由插件按公式伪造，ONNX 图因此少一路输入、转换也更轻。

#### 4.3.5 小练习与答案

**练习 1**：如果把一个训练导出的（keep_prob<1 的）模型直接交给本插件转换，会发生什么？

**参考答案**：结果会是**错的但能跑通**。插件无视模型的真实 dropout 意图，一律伪造全 1 掩码、并按 ONNX 属性把 keep_prob（若导出方写了）传下去；若导出方在训练图里带了真实 drop_mask 输入，本插件没有对应输入槽位，掩码会被丢弃。子图展开级插件隐含「推理专用」假设，使用前必须核对场景。

**练习 2**：`GetFinalDimsByOperator` 为什么不能把 drop_mask 长度做成动态 shape，而要编译期算死？

**参考答案**：Fill 的 `dims` 输入是一个 Const 节点（`Vec2Tensor(final_dims, {1}, ge::DT_INT64)`），值在构图时就要确定；虽然 shape 来自 `op.GetInputDesc(0)`，但插件在 ATC 转换期执行，此时输入 shape 已知（静态图），所以可以算死。若模型是动态 batch，此处取到的 dims 含 -1，长度计算会得到无意义值——这是该实现的一个边界（待确认：动态 shape 模型下 ATC 的实际行为）。

**练习 3**：`ParseParamsFlashAttention` 里 `required_attr_num` 只统计 2 个属性，而 MHA 插件统计全部 6 个，哪种设计更合理？

**参考答案**：没有绝对优劣，反映的是目标算子合同的差异。FlashAttentionScore 的 proto 里只有 `head_num`/`input_layout` 是 `REQUIRED_ATTR`，其余带默认值，插件与 proto 合同对齐是正确做法；MHA 的目标算子六个属性全必需。原则：**必需性校验应镜像目标 IR 算子的 proto 声明**，而不是拍脑袋定数量。

### 4.4 插件工程组织：从 framework 目录到单一动态库

#### 4.4.1 概念说明

17 个插件散落在 4 个算子域的 `framework/` 子目录里，最终却只产出**一个** `liboponnx_plugin_transformer.so`。这种「源码分布式、产物集中式」的组织意味着：ATC 只需加载一个库就能认识全部 NPU 特有算子，而插件源码仍跟着各自算子走，便于随算子一起维护。命名约定是硬约束：文件必须叫 `*_onnx_plugin.cpp` 才会被收集，这与 u2-l1 讲过的「文件名含 `_tiling` 才被识别」是同一套哲学。

#### 4.4.2 核心流程

```
各算子 framework/CMakeLists.txt
    └─ add_onnx_plugin_sources()                    # 每目录一次
         └─ file(GLOB *_onnx_plugin.cpp)            # 按命名约定收集
         └─ add_onnx_plugin_modules()               # 首次调用时建全局 OBJECT 库
              oponnx_plugin_transformer_obj
gen_onnx_plugin_symbol()                            # 收尾出动态库
    └─ add_library(oponnx_plugin_transformer SHARED, OBJECT)
    └─ install → ${ONNX_PLUGIN_LIB_INSTALL_DIR}
                = opp/built-in/framework/onnx       # CANN 包内插件检索目录
```

#### 4.4.3 源码精读

**① 算子侧入口：每个 framework 目录三行有效代码**

- [attention/flash_attention_score/framework/CMakeLists.txt:L11-L13](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/CMakeLists.txt#L11-L13)

条件 `BUILD_OPEN_PROJECT AND NOT BUILD_OPS_RTY_KERNEL` 区分开源工程构建与内部流水线构建，满足时调用 `add_onnx_plugin_sources()`。common 侧入口完全相同：[common/src/framework/CMakeLists.txt:L11-L13](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/CMakeLists.txt#L11-L13)。

**② GLOB 收集与 OBJECT 库复用**

- [cmake/obj_func.cmake:L871-L881](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/obj_func.cmake#L871-L881)

`macro(add_onnx_plugin_sources)` 把当前目录下所有 `*_onnx_plugin.cpp` 塞进全局目标 `oponnx_plugin_transformer_obj`；该目标由 [cmake/obj_func.cmake:L830](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/obj_func.cmake#L830) 的 `add_onnx_plugin_modules` 用 `if (NOT TARGET ...)` 守卫——只有第一个被处理的目录真正建库，后续目录只追加源码。库会编入 protobuf 生成头、静态链接 `ascend_protobuf_static`，并定义日志子模块名 `OPS_UTILS_LOG_SUB_MOD_NAME="ONNX_PLUGIN"`。

**③ 出库与安装**

- [cmake/symbol.cmake:L466-L489](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/symbol.cmake#L466-L489)

`gen_onnx_plugin_symbol` 把 OBJECT 店链接成 SHARED 库，并以 `--whole-archive` 包入注册静态库——`REGISTER_CUSTOM_OP` 的登记代码是全局静态对象初始化，不整包链接就会被裁掉（与 u3-l2 fallback 讲过的符号查找、u2-l1 的库组织一脉相承）。安装目录变量见 [cmake/variables.cmake:L69](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/variables.cmake#L69)：`opp/built-in/framework/onnx`，即 CANN 包内 ATC 检索内置 ONNX 插件的标准位置；库名由 [cmake/variables.cmake:L17](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/variables.cmake#L17) 的 `oponnx_plugin_${PKG_NAME}` 拼出。`onnxplugin` 与 ophost/opapi/opgraph 并列为四大发布目标（[build.sh:13](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L13)）。

#### 4.4.4 代码实践

**实践目标**：回答「给 u6-l1 开发的 my_sum 算子补 ONNX 支持，要新增/修改哪些文件」。

**操作步骤**：

1. 走读 4.4.3 的三处 CMake 源码。
2. 对照 add_example 的目录结构（无 framework 目录）。
3. 列出交付件清单与放置位置。

**需要观察的现象**：构建系统侧需要的改动几乎为零——`add_onnx_plugin_sources` 按 GLOB 自动发现。

**预期结果**：参考清单——

| 交付件 | 位置 | 说明 |
| --- | --- | --- |
| IR 原型 | `my_sum/op_graph/my_sum_proto.h`（`REG_OP(MySum)`） | 插件落点，u6-l2 已讲 |
| graph infer | `my_sum/op_graph/my_sum_graph_infer.cpp` | 图模式推导 dtype |
| 插件实现 | `my_sum/framework/my_sum_onnx_plugin.cpp` | 属性翻译级即可 |
| 构建入口 | `my_sum/framework/CMakeLists.txt` | 三行，照抄 flash_attention_score 的写法 |
| 算子根 CMakeLists | 追加 `add_subdirectory(framework)` | 若 GLOB 子目录机制未覆盖 framework，需确认（待本地验证） |

注意：op_host 的 def 与 tiling 复用现有实现，**不需要动**；这正是「插件只是第三张注册面」的含义。仓库内暂无插件编写的专门文档（入口仅 [README.md:15](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/README.md#L15) 与 [docs/zh/install/build.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/build.md) 的构建选项表），仿照现有插件是最可靠的路径。

#### 4.4.5 小练习与答案

**练习 1**：为什么链接 `oponnx_plugin_transformer` 时要 `--whole-archive` 包入 `rt2_registry_static`？

**参考答案**：`REGISTER_CUSTOM_OP` 宏展开后会生成一个靠静态对象构造函数完成登记的代码块，没有任何显式调用方。链接器默认丢弃「无引用」的目标文件，不整包保留就会静默丢注册，表现为「.so 存在但 ATC 仍不认识节点」。

**练习 2**：两个不同算子域的 framework 目录都被处理时，`add_onnx_plugin_modules` 的 `if (NOT TARGET ...)` 守卫防止了什么？

**参考答案**：防止重复创建同名 OBJECT 库导致 CMake 报错；保证 17 个目录的插件源码汇聚到同一个 `oponnx_plugin_transformer_obj`，最终链接成单一 .so。

**练习 3**：插件库为什么安装到 `opp/built-in/framework/onnx` 而不是普通 lib 目录？

**参考答案**：该路径是 CANN 包内 ATC 约定的「内置 ONNX 插件检索目录」，安装到位后 ATC 做模型转换时按域加载其中的注册表，无需用户手工指定插件路径。

## 5. 综合实践

**任务：编写一份《NPUFlashAttention ONNX 接入说明》文档并自查**。

把本讲四个模块串起来，产出一份给模型导出同学看的一页说明，必须包含：

1. **能力声明**：支持的节点类型全名（9 种，抄自 [npu_flash_attention_score_onnx_plugin.cpp:L236-L244](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/framework/npu_flash_attention_score_onnx_plugin.cpp#L236-L244)）与部署前提（安装包含 liboponnx_plugin_transformer.so 的 run 包）。
2. **输入约束表**：4.3.4 的 10 行输入表，注明 drop_mask 不可传入、attention_out 是唯一输出。
3. **属性映射表**：4.3.4 的 8 行属性表，特别标注三个默认值与 proto 不同的属性。
4. **掩码长度公式**：给出 4.3.2 的位打包公式与一个具体数值例子（例：\(B=2, S=1024, N=32\) 时元素数 \(= 2 \times 1024 \times 1024 \times 32 = 67108864\)，\(L = \lceil 67108864/128 \rceil \times 16 + 32 = 8388624\) 字节），说明它只在导出方需要预估中间内存时有意义。
5. **自查**：用 `git grep -n "drop_mask" attention/flash_attention_score/framework/` 核对文档中每个论断都有源码行号支撑。

进阶（有 NPU/ATC 环境者）：用如下示例代码构造一个仅含 `npu::1::NPUFlashAttention` 节点的最小 ONNX 模型，经 ATC 转换并观察日志中的插件命中记录——**待本地验证**。

```python
# 示例代码：构造带自定义域节点的最小 ONNX 模型（非仓库原有代码）
import onnx
node = onnx.helper.make_node(
    "NPUFlashAttention",
    inputs=[f"x{i}" for i in range(9)],
    outputs=["y"],
    name="fa_node",
    domain="npu",                    # 自定义域 -> npu::1::NPUFlashAttention
    head_num=32,
    input_layout="BSH",
    scale=0.125,
    keep_prob=1.0,
)
# 其余：按 9 路输入的 dtype 白名单造 ValueInfo 后 build 图
```

## 6. 本讲小结

- onnx plugin 是 ATC 模型转换期的「翻译官」：通过 `REGISTER_CUSTOM_OP().FrameworkType(ONNX).OriginOpType({...})` 登记「我认识哪些 ONNX 节点」，命中后回调 `ParseParamsFn`（属性翻译）与可选的 `ParseOpToGraphFn`（子图展开）。
- 插件分两档：属性翻译级（如 NPUMoeInitRouting、NPUMultiHeadAttention，插件属性 SetAttr 名与目标 IR 算子 proto 声明严格对齐）与子图展开级（如 NPUFlashAttention，经 `PartitionedCall` 中转算子两段翻译）。
- 属性映射不只是改名：MHA 的 `dropout_prob → keep_prob` 要做 \(1-p\) 语义取补，FA 的 `scale → scale_value` 是唯一改名点；插件层显式 SetAttr 会覆盖 proto 默认值（inner_precise 等 3 处默认值不同）。
- 子图展开级插件的三板斧：`ge::op::Data` 占位输入、Const+Fill+Cast 现场伪造常量（全 1 drop_mask，长度按位打包公式编译期算死）、`SetOutputs` 裁剪输出（FA 仅保留索引 3 的 attention_out）。
- 插件落点是 op_graph 的 `REG_OP` IR 算子，最终与 aclnn Eager 路径共用同一套 op_host/op_kernel；插件自己不写任何计算。
- 工程上 17 个插件按 `*_onnx_plugin.cpp` 命名约定被 GLOB 进单一 `liboponnx_plugin_transformer.so`（`--whole-archive` 保注册符号），安装到 CANN 包 `opp/built-in/framework/onnx` 供 ATC 检索。

## 7. 下一步学习建议

本讲补齐了「第三张注册面」，至此你已看全 def（op_host 信息库）、proto（IR 原型）、plugin（前端翻译）三种声明的分工。建议下一步：

1. **u6-l4（调试与调优）**：当 ONNX 模型转换失败或结果异常时，prof 与 dump 手段是下一道防线；本讲的属性默认值陷阱正是典型的「转换期问题、执行期爆雷」。
2. **横向对比三个子图展开级插件**：把 [npu_fused_attention_score_onnx_plugin.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/npu_fused_attention_score_onnx_plugin.cpp)、`_fwd_` 版本与 [npu_masked_softmax_with_relposbias_onnx_plugin.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/common/src/framework/npu_masked_softmax_with_relposbias_onnx_plugin.cpp) 与 FA 插件对照阅读，体会「Const 造常量 + Data 占位 + 输出裁剪」模板的变体。
3. **补全 your first plugin**：按 4.4.4 的清单给 u6-l1 的 my_sum 真正补一个属性翻译级插件（my_sum 无属性时甚至只需处理输入透传），这是通往 u7-l3 贡献流程的实战练习。
