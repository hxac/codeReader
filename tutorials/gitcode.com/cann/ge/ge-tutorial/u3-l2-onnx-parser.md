# ONNX 模型解析实战

## 1. 本讲目标

上一讲（u3-l1）讲清楚了「工厂如何派发解析器」:无论哪种前端，最终都会走到 `model_parser->Parse(file, graph)` 这一行，产出一个 `ge::Graph`（即 AscendIR）。本讲要钻进这一行的内部，以最常见的前端 **ONNX** 为例，回答:

> 一个 `.onnx` 文件里的节点、权重、子图，究竟是怎么一步步变成 AscendIR 的 `Node` 与连边的？

学完本讲，你应当能够:

- 说清 ONNX 用 protobuf 描述的图（`ModelProto → GraphProto → NodeProto`）与 AscendIR（`ComputeGraph → Node → OpDesc + Anchor`）在**连边表达方式**上的根本差异，以及 GE 如何弥合这一差异。
- 掌握 ONNX 权重的三类来源——`initializer`、显式 `Constant` 节点、外置权重文件——分别走哪条解析路径，最终变成 AscendIR 的哪种常量节点。
- 理解 ONNX 控制流算子（如 `If`）嵌套在属性里的子图，是如何被「摘取、独立解析、再挂回父节点」的。

本讲是单元 3 的第二讲，承接 u3-l1 的工厂与统一入口，为 u3-l3（ATC 工具链）理解「atc 如何驱动 `Parse`」打好地基。

## 2. 前置知识

阅读本讲前，你需要先具备以下概念（均来自前面的讲义）:

- **AscendIR 四层对象模型**:`ComputeGraph → Node → OpDesc → GeTensorDesc`，连边由 Anchor 互引表达，不存在独立 Edge 对象（见 u2-l1、u2-l2）。
- **算子注册体系**:GE 不存算子实现，算子定义外置于独立算子仓；`OpDesc.type` 只是字符串，真正语义来自原型注册表，可用 `OperatorFactory::CreateOperator(name, type)` 按名创建算子（见 u2-l4）。
- **parser 工厂与统一入口**:`OnnxModelParser` 经 `REGISTER_MODEL_PARSER_CREATOR(ONNX, ...)` 自注册进 `ModelParserFactory`，被 `aclgrphParseONNX` / atc 的 `ParseGraph` 通过 `CreateModelParser(domi::ONNX)` 取出并调用 `Parse`（见 u3-l1）。
- **在线 vs 离线**:本讲聚焦 `Parse` 内部，与在线/离线无关——两条路径最终都调到这里。

本讲还会用到三个工程/格式术语:

- **protobuf**:Google 的结构化数据序列化格式。ONNX 标准用 protobuf 定义模型（`ge_onnx.proto`），解析的第一步就是把二进制 `.onnx` 文件反序列化成 `ModelProto` 这个 C++ 结构。
- **ONNX 的「张量名连边」**:ONNX 不设独立边对象，节点之间靠**张量名**隐式连接——A 节点的 `output` 列表里有名字 `"x"`，B 节点的 `input` 列表里也有 `"x"`，就认为有一条 A→B 的数据边。这与 AscendIR 的「Anchor 互引」是两种完全不同的表达方式。
- **initializer（初始化器）**:ONNX `GraphProto` 里的 `repeated TensorProto initializer` 字段，存放模型的常量输入（典型即卷积权重、BN 参数）。它和「图输入」并列存在，是 ONNX 表达权重的主要方式。

## 3. 本讲源码地图

本讲涉及的源码按职责可分成四组:

| 文件 | 作用 |
| --- | --- |
| [graph_metadef/proto/onnx/ge_onnx.proto](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/proto/onnx/ge_onnx.proto) | ONNX 的 protobuf 定义:`ModelProto`、`GraphProto`、`NodeProto`、`TensorProto` 等消息结构（解析的「原文格式」） |
| [parser/parser/onnx/onnx_parser_internal.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser_internal.h) | `OnnxModelParser` 类声明，列出解析过程中用到的全部成员 map 与私有方法 |
| [parser/parser/onnx/onnx_parser.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc) | 解析主逻辑:读文件 → 12 步 `ModelParseToGraphImpl` → 节点映射 → 连边回填 → 子图递归，以及模型/权重解析器的自注册 |
| [parser/parser/onnx/onnx_data_parser.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_data_parser.cc) / [.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_data_parser.h) | `OnnxDataParser`:把 ONNX 的图输入（`Input`）解析成 AscendIR 的 `Data` 节点，并合并用户 `--input_shape` |
| [parser/parser/onnx/onnx_constant_parser.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_constant_parser.cc) / [.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_constant_parser.h) | `OnnxConstantParser`:把内联常量（`Constant` / `initializer`）的 `TensorProto` 解析成 `ge::Tensor`，写入 `value` 属性 |
| [parser/parser/onnx/onnx_file_constant_parser.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_file_constant_parser.cc) | `OnnxFileConstantParser`:把外置权重（`external_data`）解析成 `FileConstant` 节点，只记录路径/shape/dtype，不读数据 |
| [parser/parser/onnx/onnx_util.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_util.cc) / [.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_util.h) | `OnnxUtil`:ONNX 数据类型 → GE 数据类型的映射表，以及子图名/节点名去重工具 |
| [parser/parser/onnx/subgraph_adapter/](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/subgraph_adapter/subgraph_adapter.h) | 子图适配器:`SubgraphAdapter` 基类、`SubgraphAdapterFactory` 工厂、`IfSubgraphAdapter` 具体实现，负责把嵌套子图摘取出来 |

ONNX 解析涉及的核心类与文件的协作关系如下图:

```text
aclgrphParseONNX / Parse (入口)
        │
        ▼
OnnxModelParser::Parse ──► ModelParseToGraph ──► ModelParseToGraphImpl (12 步)
                                                      │
            ┌─────────────────────────────────────────┼─────────────────────────────┐
            ▼                                         ▼                             ▼
   ParseInput/ParseInitializer/ParseOutput    ParseAllNodeProto            AdaptAndFindAllOnnxGraph
   (把 input/initializer/output              (逐节点:AdapterOpType         (子图适配:BFS 摘取
    规整成 NodeProto)                          → TransNodeToOperator         嵌套子图 GraphProto)
                                              → OpParser 解析参数)                  │
                                                      │                             ▼
                                                      ▼                  IfSubgraphAdapter / ...
                                        OpParserFactory 按算子类型派发    递归 ModelParseToGraphImpl
                                            ┌───────────┼──────────┐
                                            ▼           ▼          ▼
                                      OnnxDataParser  OnnxConstantParser  OnnxFileConstantParser
                                      (Data 节点)      (Const 节点)         (FileConstant 节点)
```

## 4. 核心概念与源码讲解

本讲拆成三个最小模块:①ONNX 节点映射；②常量与权重解析；③子图适配。

### 4.1 ONNX 节点映射:从 NodeProto 到 AscendIR 的 Node

#### 4.1.1 概念说明

要理解解析，先看「原文」长什么样。ONNX 用 protobuf 描述模型，其核心消息是 `GraphProto`:

[graph_metadef/proto/onnx/ge_onnx.proto:271-310](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/proto/onnx/ge_onnx.proto#L271-L310) —— `GraphProto` 定义了一张图的三类组成:`repeated NodeProto node`（算子节点）、`repeated TensorProto initializer`（常量权重）、`repeated ValueInfoProto input/output`（图的输入输出描述，含 shape/dtype）。

而每个算子节点 `NodeProto` 的结构是:

[graph_metadef/proto/onnx/ge_onnx.proto:175-193](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/proto/onnx/ge_onnx.proto#L175-L193) —— `NodeProto` 只有 `repeated string input/output`（张量名列表）、`string name`、`string op_type`、`repeated AttributeProto attribute`。注意:**它没有任何「指向其他节点的指针」**。

这就引出了 ONNX 与 AscendIR 的根本差异:

| 维度 | ONNX | AscendIR |
| --- | --- | --- |
| 节点对象 | `NodeProto`（protobuf 消息） | `Node`（C++ 对象，Pimpl） |
| 连边表达 | **张量名隐式匹配**:A.output 含 `"x"`，B.input 含 `"x"` ⇒ A→B | **Anchor 显式互引**:`OutDataAnchor.peer_anchors_` 指向 `InDataAnchor` |
| 边对象 | 无 | 无（用 Anchor 表达，见 u2-l2） |
| 算子类型 | 字符串 `op_type`（如 `"Conv"`） | `OpDesc.type` 字符串（如 `"Conv"`），经注册表查询语义 |

所以 ONNX 解析的核心矛盾是:**把 ONNX「按名字匹配」的隐式连边，翻译成 AscendIR「按对象引用」的 Anchor 连边**。GE 的解法分两步走:① 先为每个节点建好孤立的 `Operator`（此时还没有边）；② 用两张临时 map（按张量名索引）回填连接。

另一个差异是**算子类型名**:ONNX 的 `Input`/`Constant` 在 GE 内部叫 `Data`/`Const`。需要一个映射表对齐语义。

#### 4.1.2 核心流程

整个解析由 `OnnxModelParser::ModelParseToGraphImpl` 的 12 个编号步骤驱动（源码注释里就标了 1~12）:

```text
OnnxModelParser::Parse(file, graph)                      # onnx_parser.cc:1126
  ├─ GetModelFromFile(file) → 反序列化成 ModelProto       # 读 .onnx 二进制
  └─ ModelParseToGraph(model, graph)                     # onnx_parser.cc:911
       ├─ AdaptAndFindAllOnnxGraph(...)                  # 先 BFS 把嵌套子图摘出来（4.3 讲）
       └─ 对 [根图 + 每个子图] 依次执行 ModelParseToGraphImpl:
            1. 收集 initializer（权重名 → TensorProto 表）
            2. ParseInput     —— 把 input ValueInfo 转成 Input 节点
            3. ParseInitializer —— 把 initializer 转成 Const/FileConstant 节点
            4. ParseOutput    —— 记录输出张量名
            5. UpdateNodeNameAndOpType —— 补无名节点名、修正常量类型
            6. Prechecker     —— 合法性预检（名字/类型）
            7. ParseAllNodeProto —— 主循环：逐节点 AdapterOpType → TransNodeToOperator
                                    → OpParser 解析参数 → AddOp 加入图（此时尚无连接边）
            8. SetOperatorInputs —— 用 inputs_map_/outputs_map_ 按张量名回填 Anchor 边
            9. GetGraphInputs + 拓扑排序 —— 确定图入口、固定节点顺序
           10. GetGraphOutputs —— 收集输出算子
           11. ExpandOneToManyGraph —— 一对多输出展开
           12. SetOutputsInfo —— 把输出信息写进 ParserContext（供后续编译用）
```

本模块聚焦步骤 1~9（节点与边的构建），步骤里涉及的常量解析细节见 4.2，子图相关见 4.3。

#### 4.1.3 源码精读

**入口与总调度。** `Parse` 极薄，核心是「读文件 → 调 `ModelParseToGraph`」:

[parser/parser/onnx/onnx_parser.cc:1126-1139](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L1126-L1139) —— `OnnxModelParser::Parse`:先用 `GetModelFromFile` 把 `.onnx` 反序列化成 `ModelProto`，再交给 `ModelParseToGraph` 翻译成 `ge::Graph`。

`ModelParseToGraphImpl` 是真正的 12 步主流程:

[parser/parser/onnx/onnx_parser.cc:1001-1124](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L1001-L1124) —— 12 步解析主流程，步骤编号与源码注释一一对应。注意它对**根图和子图是同一套代码**:参数 `is_subgraph` 只在少数几步（如「子图需设 outputs、根图不设」「根图写 OutputsInfo」）起区分作用。

**步骤 2:把图输入转成节点。** ONNX 的 `GraphProto.input` 只是「输入描述」（名字 + shape + dtype），并不是节点。`ParseInput` 会为每个输入**合成一个 `NodeProto`**（op_type=`"Input"`），塞回图的节点列表，让它和普通算子走同一条解析路径:

[parser/parser/onnx/onnx_parser.cc:293-357](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L293-L357) —— `ParseInput`:遍历 `onnx_graph.input`，跳过那些「同时出现在 initializer 里」的输入（`initializer_name_tensor.find(...) != end()` 即 `continue`，L309-312——这正体现了「权重若被声明为输入则视为常量」的 ONNX 语义），把剩余真正的模型输入合成为 `op_type="Input"` 的 `NodeProto`，并把 shape/dtype 放进 `input_tensor` 属性、序号放进 `index` 属性。

**步骤 3:把权重转成常量节点。** `ParseInitializer` 类似地把每个 `initializer`（权重）合成成一个 `NodeProto`:

[parser/parser/onnx/onnx_parser.cc:359-382](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L359-L382) —— `ParseInitializer`:为每个权重 `TensorProto` 合成常量节点，把原始 `TensorProto` 放进 `value` 属性；**按 `data_location` 决定 op_type**——外置（`EXTERNAL`）则 `kFileConstant`，内联则 `kOpTypeConstant`（`"Constant"`）。节点名加上 `_Initializer_<index>` 后缀保证唯一。

**步骤 7:逐节点映射。** `ParseAllNodeProto` 是节点映射的主循环:

[parser/parser/onnx/onnx_parser.cc:611-669](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L611-L669) —— 对每个 `NodeProto`:① `AdapterOpType` 把 ONNX 算子类型翻译成 AscendIR 算子类型；② `TransNodeToOperator` 用 `OperatorFactory` 创建 `Operator`；③ 取 `OpParser` 解析参数；④ `graph.AddOp(op)` 加入图；⑤ `ConstructInputOutputContext` 登记该节点的输入输出张量名。**注意:循环结束时图里每个节点都是孤立的，节点之间还没有 Anchor 边。**

算子类型映射靠 `kOnnxOpMap` 这张表 + `OpRegistry` 查询:

[parser/parser/onnx/onnx_parser.cc:175-177](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L175-L177) —— `kOnnxOpMap` 把 ONNX 的 `Input`/`Constant`/`FileConstant` 映射为 GE 内部的 `Data`/`Const`/`FileConstant`（`parser::DATA`/`parser::CONSTANT`/`parser::FILECONSTANT`）。

[parser/parser/onnx/onnx_parser.cc:433-460](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L433-L460) —— `AdapterOpType`:先查 `kOnnxOpMap`（命中则直接替换，如 `Constant→Const`）；未命中（普通算子如 `Conv`）则拼出带 domain/version 的 `ori_type`（如 `ai.onnx::11::Conv`），再向 `OpRegistry` 查询对应的 AscendIR 算子类型。拼 `domain::version::op_type` 是为了在多 opset 版本下精确匹配算子定义。

创建 `Operator` 用的是 u2-l4 讲过的算子工厂:

[parser/parser/onnx/onnx_parser.cc:462-479](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L462-L479) —— `TransNodeToOperator`:核心就一行 `ge::OperatorFactory::CreateOperator(node_name, op_type)`，按类型名从算子原型注册表创建算子实例；若返回的算子名与节点名不符，说明该算子类型未注册，报错。这一步把「字符串 op_type」变成了「有输入输出端口的 Operator 对象」。

参数解析则交给 `OpParserFactory` 按算子类型派发:

[parser/parser/onnx/onnx_parser.cc:576-609](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L576-L609) —— `ParseOpParam`:优先用算子注册时挂的 `ParseParamByOpFunc`（自定义算子路径）；否则用通用 `OpParser` 的 `ParseParams`。这条「双路」机制既支持标准算子的属性自动映射，也支持像 `Data`/`Const` 这样需要特殊处理的算子（见 4.2）。

**步骤 8:回填连边。** 这是弥合两种连边表达的关键。`ConstructInputOutputContext` 在步骤 7 循环内为每个节点登记「张量名 → (节点名, 端口序号)」:

[parser/parser/onnx/onnx_parser.cc:481-496](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L481-L496) —— 把每个节点的 `input(i)` 记进 `inputs_map_[张量名]`，`output(i)` 记进 `outputs_map_[张量名]`，值为 `(节点名, 端口号)`。这两张 map 就是「张量名 → 生产者/消费者」的索引。

随后 `SetOperatorInputs` 用这两张表把边连上:

[parser/parser/onnx/onnx_parser.cc:498-542](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L498-L542) —— `SetOperatorInputs`:对 `inputs_map_` 里每个张量名，去 `outputs_map_` 查它的生产者；若找到，就用 `dst_op.SetInput(输入名, src_op, 输出名)` 建立连接（这一步在底层会创建 Anchor 边）。**这就是「张量名隐式连边 → Anchor 显式连边」的翻译点。** 若一个输入名在 `outputs_map_` 里查不到（既无生产者），会告警并跳过——它可能是可选输入或由框架补全。

**步骤 6:合法性预检。** 解析前还有一道预检 `Prechecker`，校验节点名、算子类型是否合法，避免把错误带到编译阶段:

[parser/parser/onnx/onnx_parser.cc:544-574](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L544-L574) —— `Prechecker`:对每个节点构造 `ori_type` 后，用 `PreChecker::Instance()` 依次 `AddOp`、`CheckName`、`CheckType`。它支持「只预检不解析」模式（`run_mode == ONLY_PRE_CHECK`，见步骤 6 之后的早退分支 L1050-1053），可用于在真正解析前快速校验模型合法性。

> 设计要点:为什么 GE 要「先建孤立节点、再用 map 回填连边」分两步，而不是边建节点边连边？因为 ONNX 的节点顺序不保证拓扑序——一个节点的输入可能由排在它后面的节点产出。两步法先把所有节点建好（生产者必然已存在），再统一连边，避免「生产者还没创建」的顺序依赖。

#### 4.1.4 代码实践

**实践目标**:跟踪一个普通算子节点（如 `Conv`）从 `NodeProto` 变成 AscendIR `Node`、再到连上边的完整调用链，并验证「两步建图」。

**操作步骤**（源码阅读型，无需昇腾设备）:

1. 打开测试 [tests/parser/ut/parser/testcase/onnx_parser_testcase/onnx_parser_unittest.cc:140-157](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/tests/parser/ut/parser/testcase/onnx_parser_testcase/onnx_parser_unittest.cc#L140-L157)，看 `onnx_parser_user_output_with_name_and_index` 如何用 `aclgrphParseONNX` 解析 `conv2d.onnx`，并通过 `GraphUtilsEx::GetComputeGraph(graph)` 拿到 `ComputeGraphPtr` 后断言输出节点是 `Conv_0:0`。
2. 对照 [parser/parser/onnx/onnx_parser.cc:611-669](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L611-L669) 的 `ParseAllNodeProto`，在脑中（或纸上）走一遍 `Conv_0` 这个节点:它的 `op_type="Conv"`，经 `AdapterOpType` 不在 `kOnnxOpMap` 里，于是走 `ConstructOriType` 拼成 `ai.onnx::<ver>::Conv`，再向 `OpRegistry` 查到 AscendIR 的 `Conv`，`OperatorFactory::CreateOperator` 建出算子。
3. 接着看 [parser/parser/onnx/onnx_parser.cc:481-542](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L481-L542)，理解 `Conv_0` 的输入（如权重张量名）是如何在 `inputs_map_`/`outputs_map_` 里匹配到那个由 `ParseInitializer` 合成的 `Const` 节点的输出，再被 `SetOperatorInputs` 连成 Anchor 边。

**需要观察的现象**:

- 步骤 2 给的测试里 `Conv_0` 能被识别为输出节点，说明 `ParseOutput`（步骤 4）记录的 `output_node_names_` 与 `GetGraphOutputs`（步骤 10）配合正确。
- `inputs_map_` 里 `Conv_0` 的某个输入名，恰好等于某个 `Const` 节点的输出名——这正是「张量名隐式连边」被翻译为 Anchor 边的实证。

**预期结果**:你能画出 `conv2d.onnx` 解析后的 AscendIR 结构:至少有一个 `Data`（模型输入）→ `Conv`，以及一个 `Const`（来自 initializer 的权重）→ `Conv`，两条边都由 `SetOperatorInputs` 建立。

> 待本地验证:若你想直观看到回填过程，可在 `SetOperatorInputs`（onnx_parser.cc:498）入口加一行日志打印正在连接的 `(src 节点, src 端口) → (dst 节点, dst 端口)`，用一个真实 ONNX 模型解析后观察输出（本步骤改动源码，仅建议在本地实验分支进行，勿提交）。

#### 4.1.5 小练习与答案

**练习 1**:`ParseInput` 里为什么要对「同时出现在 initializer 里的输入」执行 `continue` 跳过？

**参考答案**:ONNX 规范允许同一个张量名同时出现在 `input` 列表和 `initializer` 列表里——这表示「该输入有默认常量值」（典型即权重）。这种张量应当被当作常量（由 `ParseInitializer` 合成 `Const` 节点），而不是当作需要用户在运行时喂入的模型输入（`Data` 节点）。跳过它就避免了「同一个权重既建成 `Data` 又建成 `Const`」的重复，与 ONNX 语义对齐。

**练习 2**:为什么 `SetOperatorInputs` 要在「所有节点都建好之后」才执行，而不是在 `ParseAllNodeProto` 循环里建一个连一个？

**参考答案**:ONNX 的节点顺序不保证拓扑序。若边建边连，处理节点 B 时它的输入生产者 A 可能还没被创建，`outputs_map_` 里查不到，就无法连接。两步法先把全部节点与「张量名→生产者」索引建好，再统一连边，消除了顺序依赖，也保证所有生产者都已就绪。

**练习 3**:`AdapterOpType` 对 `Conv` 这类普通算子，为什么要拼出 `ai.onnx::11::Conv` 这种带 domain 和 version 的字符串再去 `OpRegistry` 查？

**参考答案**:同一个算子名（如 `Conv`）在不同 opset 版本下语义、属性可能不同（ONNX 算子是按 domain + version 版本化的）。带上 domain（如 `ai.onnx`）和版本号，能精确匹配到注册表里对应版本的算子定义，避免版本混淆。`Input`/`Constant`/`FileConstant` 这类 GE 内部固定语义的算子则直接走 `kOnnxOpMap` 简单替换，无需拼版本。

---

### 4.2 常量与权重解析:Constant / Initializer / FileConstant

#### 4.2.1 概念说明

ONNX 模型里的「常量/权重」有三类来源，GE 必须分别处理:

| 来源 | ONNX 里的位置 | 数据放哪 | GE 解析成什么节点 | 由谁解析 |
| --- | --- | --- | --- | --- |
| **initializer（权重）** | `GraphProto.initializer`（`repeated TensorProto`） | 内存 `raw_data` 或外置文件 | `Const` 或 `FileConstant` | 先由 `ParseInitializer` 合成节点，再由对应 OpParser 解析 |
| **显式 Constant 节点** | `GraphProto.node` 里 `op_type="Constant"` | 内存 `raw_data` 或外置文件 | `Const` 或 `FileConstant` | `OnnxConstantParser` / `OnnxFileConstantParser` |
| **外置权重** | `TensorProto.data_location == EXTERNAL` | 独立文件（`external_data` 指向） | `FileConstant` | `OnnxFileConstantParser` |

这里有几个关键区分:

1. **`Const` vs `FileConstant`**:数据**内联**在模型里（`raw_data`）的常量 → `Const` 节点（编译期就持有完整 `ge::Tensor`）；数据**外置**在独立文件里的权重 → `FileConstant` 节点（解析期只记录路径/shape/dtype，**不真正读数据**，运行时才按需加载）。后者正是「外置权重」机制在解析层的体现（详见 u7-l4、u9-l4）。
2. **`TensorProto` → `ge::Tensor`**:ONNX 的 `TensorProto` 用多种字段（`float_data`/`int32_data`/`int64_data`/`raw_data` 等）按数据类型分散存放数值；GE 的 `ge::Tensor` 是连续内存。需要一次「按类型取数据 + 拷成连续 buffer」的转换。
3. **数据类型映射**:ONNX 的类型枚举（`FLOAT=1`、`INT64=7`…）与 GE 的 `DataType`（`DT_FLOAT`、`DT_INT64`…）编号不同，需查表转换。

#### 4.2.2 核心流程

常量/权重解析的整体路径:

```text
（权重来自 initializer 时）
GraphProto.initializer[i]  ──ParseInitializer──►  合成 NodeProto(op_type=Constant 或 FileConstant)
                                                      │（与其他算子一起进入步骤 7 主循环）
                                                      ▼
                                        AdapterOpType: Constant→Const, FileConstant→FileConstant
                                                      │
                                                      ▼
                          OpParserFactory::CreateOpParser(op_type) 按类型派发:
                            ├─ "Const"         → OnnxConstantParser::ParseParams
                            │                      ├─ ParseConstFromInput: 取 value 属性里的 TensorProto
                            │                      ├─ ParseConvertDataType: TensorProto.data_type → ge::DataType
                            │                      ├─ ParseConvertTensor:   dims → GeShape；数据 → 连续 buffer
                            │                      └─ op_def.SetAttr("value", tensor)   # 把完整 Tensor 写进属性
                            │
                            └─ "FileConstant"  → OnnxFileConstantParser::ParseParams
                                                   ├─ ParseDataType: 只存 dtype
                                                   ├─ ParsePath:     存 location/offset/length（不读文件）
                                                   └─ ParseShape:    存 shape
                            ├─ "Data"          → OnnxDataParser::ParseParams     # 见 4.1，模型输入
                            └─ 其他算子        → 通用 OpParser / ParseParamByOpFunc

（类型转换公共工具）
OnnxUtil::ConvertOnnxDataType:  onnx_data_type_map 查表（FLOAT→DT_FLOAT …）
```

要点:`Const` 和 `FileConstant` 虽然都是常量节点，但解析时做的事完全不同——前者把权重**搬进内存**，后者只**记一个文件路径**。这决定了后续编译/执行阶段对它们的不同处理方式。

#### 4.2.3 源码精读

**initializer 合成常量节点**已在 4.1 讲过（[onnx_parser.cc:359-382](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L359-L382)）。这里补充:对**显式 `Constant` 节点**，`UpdateConstantOpType` 也会做同样的「外置则改 FileConstant」判定:

[parser/parser/onnx/onnx_parser.cc:384-396](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L384-L396) —— `UpdateConstantOpType`:遍历 `Constant` 节点的 `value` 属性，若其 `TensorProto.data_location == EXTERNAL`，就把 op_type 改成 `FileConstant`。这样无论权重来自 initializer 还是显式 Constant 节点，「外置权重」都会被统一归到 `FileConstant`。

**内联常量解析:`OnnxConstantParser`。** 入口 `ParseParams` 调 `ParseConstFromInput`:

[parser/parser/onnx/onnx_constant_parser.cc:228-239](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_constant_parser.cc#L228-L239) —— `OnnxConstantParser::ParseParams` 直接委托 `ParseConstFromInput`。

[parser/parser/onnx/onnx_constant_parser.cc:201-226](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_constant_parser.cc#L201-L226) —— `ParseConstFromInput`:取出 `value` 属性里的 `TensorProto`，分别用 `ParseConvertDataType`（转 dtype）和 `ParseConvertTensor`（转 shape + 数据）填好一个 `ge::Tensor`，最后 `op_def.SetAttr(kAttrNameValue, tensor)` 把完整张量写进算子属性。**到这一步，权重数据已经被搬进了 AscendIR 节点的属性里。**

`TensorProto` → `ge::Tensor` 的数据转换分两层。先转 shape 与「元素总数」:

[parser/parser/onnx/onnx_constant_parser.cc:159-184](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_constant_parser.cc#L159-L184) —— `ParseConvertTensor`:把 `TensorProto.dims` 设进 `GeShape`，并逐维相乘算出元素总数 `count`（含对负维、溢出的校验），再调 `ParseConvertData` 取实际数据。

再按数据类型取值，这是最繁琐的一步:

[parser/parser/onnx/onnx_constant_parser.cc:35-104](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_constant_parser.cc#L35-L104) —— `ParseConvertData`:用一张 `datatype_val_size_map` 查出「该类型的数据存在 `TensorProto` 的哪个字段」（如 INT64 存在 `int64_data`、FLOAT 存在 `float_data`）。若该字段为空，说明数据在 `raw_data` 里（直接整块拷贝）；否则调 `ParseConvertDataElements` 按 `switch(data_type)` 从对应字段逐元素取出，最终由模板函数 `SetTensorData` 拷成连续 buffer。

> 注:`SetTensorData` 是个模板函数（定义在头文件 [onnx_constant_parser.h:33-91](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_constant_parser.h#L33-L91)），统一处理「按 GE DataType 把数据转成正确内存布局」的逻辑，还特判了 `DT_BOOL`/`DT_FLOAT16` 等需要位宽转换的类型。

**外置权重解析:`OnnxFileConstantParser`。** 它的 `ParseParams` 只记录元信息，不碰数据:

[parser/parser/onnx/onnx_file_constant_parser.cc:39-60](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_file_constant_parser.cc#L39-L60) —— `OnnxFileConstantParser::ParseParams`:依次 `ParseDataType`（存 dtype）、`ParsePath`（存文件路径）、`ParseShape`（存 shape），全程不读取权重文件内容。

[parser/parser/onnx/onnx_file_constant_parser.cc:97-115](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_file_constant_parser.cc#L97-L115) —— `ParsePath`:遍历 `TensorProto.external_data` 的键值对，`location` 存为路径属性，`offset`/`length` 存为整数属性；若没有 `location` 则报错。这些属性在后续编译/运行时由外置权重机制按需读取（见 u7-l4）。

**外置路径的拼接。** 外置权重的 `location` 在 ONNX 里通常是相对路径，GE 需要在解析早期把它拼成绝对路径（相对模型文件所在目录）:

[parser/parser/onnx/onnx_parser.cc:771-837](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L771-L837) —— `SetExternalPath`:取模型文件所在目录（`mmDirName`），遍历所有 initializer 与 Constant 节点的 `external_data`，把相对 `location` 拼成 `目录/文件名` 的绝对路径。这样后续 `FileConstant` 节点拿到的就是一个可直接打开的完整路径。

**数据类型映射工具。** 贯穿常量与输入解析的公共类型表:

[parser/parser/onnx/onnx_util.cc:15-46](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_util.cc#L15-L46) —— `ConvertOnnxDataType`:用静态 `onnx_data_type_map` 查表，把 ONNX 的 `FLOAT=1`、`INT64=7`、`FLOAT16=10`、`BFLOAT16=16`、`FLOAT8E5M2=19` 等枚举值映射为 GE 的 `DT_FLOAT`、`DT_INT64`、`DT_FLOAT16`、`DT_BF16`、`DT_FLOAT8_E5M2` 等；查不到则返回 `DT_UNDEFINED`（调用方据此报错）。

**三个算子解析器的注册。** 它们用同一个 `REGISTER_OP_PARSER_CREATOR` 宏，按算子类型字符串登记进 `OpParserFactory`:

[parser/parser/onnx/onnx_constant_parser.cc:241](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_constant_parser.cc#L241) —— `OnnxConstantParser` 注册到 `"Const"`（即 `kConstant`）。

[parser/parser/onnx/onnx_file_constant_parser.cc:142](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_file_constant_parser.cc#L142) —— `OnnxFileConstantParser` 注册到 `"FileConstant"`（即 `kFileConstant`）。

[parser/parser/onnx/onnx_data_parser.cc:142](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_data_parser.cc#L142) —— `OnnxDataParser` 注册到 `"Data"`（即 `kData`）。

> 链路自洽检查:`AdapterOpType` 把 ONNX 的 `Constant` 映射成 GE 的 `Const`（[kOnnxOpMap](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L175-L177)），随后 `OpParserFactory::CreateOpParser("Const")` 查到 `OnnxConstantParser`——类型字符串在「映射表」和「注册表」两端必须对得上，这正是 u2-l4 算子注册体系与 u3-l1 工厂机制的协作点。

**补充:`Data` 节点的 shape 合并用户输入。** `OnnxDataParser` 有一处常量解析没有的逻辑——把模型里的输入 shape 与用户通过 `--input_shape` 指定的 shape 合并:

[parser/parser/onnx/onnx_data_parser.cc:30-62](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_data_parser.cc#L30-L62) —— `OnnxDataParser::ParseParams`:先 `ParseInputFromModel`（从模型取 shape/dtype），再 `ParseInputFromUser`（用 `--input_shape` 覆盖），最后把合并后的 shape/dtype 写进 `Data` 节点的输入输出 `TensorDesc`。子图的 `Data` 算子（`IsSubgraphDataOp()`）则跳过用户 shape——因为它的 shape 来自父节点输入映射。

[parser/parser/onnx/onnx_data_parser.cc:111-140](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_data_parser.cc#L111-L140) —— `ParseInputFromUser`:从 `GetParserContext().input_dims` 取用户指定的 `--input_shape`，若用户提供则校验维度数与模型一致后覆盖，否则沿用模型 shape。这就是 atc `--input_shape` 选项在解析层生效的地方。

#### 4.2.4 代码实践

**实践目标**:通过单测用例验证 `TensorProto → ge::Tensor` 的类型转换与数据拷贝逻辑。

**操作步骤**（源码阅读型）:

1. 打开 [tests/parser/ut/parser/testcase/onnx_parser_testcase/onnx_parser_unittest.cc:293-301](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/tests/parser/ut/parser/testcase/onnx_parser_testcase/onnx_parser_unittest.cc#L293-L301) 的 `onnx_parser_const_data_type` 测试，看它解析一个含常量的 ONNX 后断言常量数据类型正确——这验证了 `ConvertOnnxDataType` + `ParseConvertDataType` 链路。
2. 看 [tests/parser/ut/parser/testcase/onnx_parser_testcase/onnx_parser_unittest.cc:315-381](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/tests/parser/ut/parser/testcase/onnx_parser_testcase/onnx_parser_unittest.cc#L315-L381) 的 `OnnxModelParser_ParseConvertData_test`、`OnnxConstantParser_ParseConvertTensor_test`、`OnnxConstantParser_ParseConvertDataType_test`，它们直接构造 `TensorProto` 调用 `OnnxConstantParser` 的私有转换方法，验证不同数据类型（含 bool）的取数与拷贝。
3. 对照 [onnx_constant_parser.cc:35-104](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_constant_parser.cc#L35-L104) 的 `ParseConvertData`，挑一个测试里用到的类型（如 INT64），说明它的数据是从 `TensorProto.int64_data()` 还是 `raw_data()` 取的。

**需要观察的现象**:

- 测试既覆盖「数据存在类型化字段（如 `int64_data`）」也覆盖「数据存在 `raw_data`」两种情形，对应 `ParseConvertData` 里 `datatype_val_size == 0` 的分支判断。
- `FileConstantGetTensorProto` / `FileConstantParsePath` 等测试（同文件 L399 之后）验证了外置权重只解析路径不读数据。

**预期结果**:你能说清——对一个 `INT64` 类型的常量，`ParseConvertData` 会先查 `datatype_val_size_map` 发现它存在 `int64_data` 字段，若该字段非空则走 `ParseConvertDataElements` 的 `case INT64` 分支用 `int64_data()` 取值，否则回退到 `raw_data` 整块拷贝。

> 待本地验证:可参照 [tests/parser/ut/parser/testcase/onnx_parser_testcase/onnx_parser_unittest.cc:315](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/tests/parser/ut/parser/testcase/onnx_parser_testcase/onnx_parser_unittest.cc#L315) 的写法，构造一个最小 `TensorProto`，调用 `OnnxConstantParser::ParseConvertTensor`，断言产出的 `ge::Tensor` shape 与字节数符合预期（运行 UT 需按 AGENTS.md 用 `ge-dt-runner` 技能编译执行）。

#### 4.2.5 小练习与答案

**练习 1**:同样是「权重」，`Const` 节点和 `FileConstant` 节点在解析阶段的行为有何本质区别？为什么要分两种？

**参考答案**:`Const` 在解析期就把权重数据完整搬进 `ge::Tensor` 并写入 `value` 属性（数据在内存里）；`FileConstant` 在解析期只记录文件路径/offset/length/shape/dtype，**不读取权重内容**。分两种是为了支持「外置权重」——大模型权重动辄几十 GB，全部内联进 OM 会臃肿且加载慢；外置后权重独立存放，运行时按需加载，部署更灵活（详见 u7-l4 外置权重、u9-l4 SO-in-OM）。

**练习 2**:`OnnxConstantParser::ParseConvertData` 里，如何判断一个 `TensorProto` 的数据该从 `int64_data()` 取还是从 `raw_data()` 取？

**参考答案**:它先用一张 `datatype_val_size_map` 查出「该 data_type 对应的类型化字段有多少个元素」（如 INT64 对应 `tensor_proto.int64_data_size()`）。若该 size 为 0，说明没有走类型化字段，数据在 `raw_data` 里（整块拷贝）；否则走 `ParseConvertDataElements` 从对应类型化字段逐元素取。这是 ONNX protobuf「两种存数方式」的统一处理。

**练习 3**:atc 的 `--input_shape=input_name:1,3,224,224` 选项，是在解析的哪一步、哪个函数里生效的？

**参考答案**:在步骤 7 主循环解析 `Data` 节点时生效。`OpParserFactory` 对 `"Data"` 类型派发出 `OnnxDataParser`，其 `ParseParams` → `ParseInputFromUser`（[onnx_data_parser.cc:111-140](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_data_parser.cc#L111-L140)）从 `GetParserContext().input_dims`（由 u3-l1 讲的 common 层 `ParseParamsBeforeGraph` 解析 `--input_shape` 填入）取出用户指定 shape，校验维度数后覆盖模型 shape，写进 `Data` 节点的 `TensorDesc`。

---

### 4.3 子图适配:If 等控制流算子的嵌套子图

#### 4.3.1 概念说明

ONNX 的控制流算子（`If`、`Loop`、`Scan`）把子图作为**属性**嵌套在节点里——例如 `If` 节点有两个 `GraphProto` 属性 `then_branch` / `else_branch`，分别描述「条件为真/假」时执行的子计算。AscendIR 则用 `ComputeGraph` 的**嵌套子图**表达（父节点的 `OpDesc` 持有子图引用，通过 `NodeUtils::SetSubgraph` 挂载，见 u2-l1）。

两种表达的关键差异:

| 维度 | ONNX 子图 | AscendIR 子图 |
| --- | --- | --- |
| 存放位置 | 父节点的 `attribute`（类型为 `GraphProto`，嵌在 protobuf 树里） | 独立的 `ComputeGraph` 对象，由父节点引用 |
| 输入来源 | 子图可引用父图张量名（隐式跨图引用） | 子图有自己的 `Data` 节点，与父节点输入按 index 映射 |

子图适配要解决两件事:① 把嵌在父节点属性里的 `GraphProto` **「摘出来」**，作为独立图参与解析；② 解析后把它**挂回父节点**，建立父子图引用关系。GE 还要处理一个 ONNX 特有的麻烦——子图引用的父图张量，需要在子图里补上对应的输入节点。

#### 4.3.2 核心流程

子图解析不是一次性的，而是「先发现、后排队、递归解析」:

```text
ModelParseToGraph(model, root_graph)                       # onnx_parser.cc:911
  ├─ AdaptAndFindAllOnnxGraph(root_onnx_graph, name_to_onnx_graph)
  │     └─ BFS 遍历所有节点:
  │          对每个节点: SubgraphAdapterFactory::CreateSubgraphAdapter(op_type)
  │            └─ 命中（如 "If"）→ IfSubgraphAdapter::AdaptAndFindAllSubgraphs
  │                 ├─ 把 then_branch/else_branch 的 GraphProto 摘出，登记进 name_to_onnx_graph
  │                 ├─ 收集子图引用的外部输入，补成子图的 input
  │                 └─ 把这些输入也加到父节点 input，建立跨图连接
  │            └─ 未命中 → 普通算子，跳过
  │     （BFS 把新发现的子图也入队，递归处理嵌套子图）
  │
  └─ 任务队列 tasks（根图 + 各子图）逐个处理:
       while (!tasks.empty()):
         取出一个 ParseArg {onnx_graph, parent_node, graph_name, subgraph_index}
         ├─ ModelParseToGraphImpl(is_subgraph, onnx_graph, tmp_graph)  # 复用 4.1 的 12 步
         ├─ PostOpProcessForSubgraph(arg, tmp_graph)
         │     └─ 调用父算子注册的 ParseSubgraphPostFunc（做 IO index 自动映射等后处理）
         ├─ BuildLinkForChildAndParentGraph(tmp_graph, arg)
         │     └─ NodeUtils::SetSubgraph(parent_node, index, sub_graph)  # 把子图挂回父节点
         └─ GenSubgraphParseTasks(tmp_graph, tasks)
               └─ 扫描刚解析出的父节点，若它还带子图，生成新任务入队（处理多层嵌套）
```

设计要点:**根图和子图共用同一个 `ModelParseToGraphImpl`**，靠 `is_subgraph` 标志区分少数行为；子图的发现与挂载由独立的 adapter 机制负责，与节点映射逻辑解耦。

#### 4.3.3 源码精读

**先发现所有子图。** `AdaptAndFindAllOnnxGraph` 用 BFS 遍历整棵图树，把嵌套子图都「挖」出来:

[parser/parser/onnx/onnx_parser.cc:859-909](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L859-L909) —— `AdaptAndFindAllOnnxGraph`:用队列 BFS。对每个节点，向 `SubgraphAdapterFactory` 按 `op_type` 请求一个 adapter；若拿到（说明该算子带子图），就调 `AdaptAndFindAllSubgraphs` 摘取子图，并把摘出的子图 `GraphProto` 也入队——从而能处理「子图里又套子图」的多层嵌套。摘出的子图按唯一名登记进 `name_to_onnx_graph`。

**子图适配器工厂。** 同样是「按类型查表创建」的工厂模式:

[parser/parser/onnx/subgraph_adapter/subgraph_adapter_factory.cc:20-29](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/subgraph_adapter/subgraph_adapter_factory.cc#L20-L29) —— `SubgraphAdapterFactory::CreateSubgraphAdapter`:在 `subgraph_adapter_creator_map_` 里按 `op_type` 查创建函数，命中则调用返回 adapter，未命中返回 `nullptr`（表示该算子不带子图、无需适配）。机制与 u3-l1 的模型/算子工厂完全同构。

**具体适配器:以 `If` 为例。** `IfSubgraphAdapter` 负责把 `then_branch`/`else_branch` 两个 `GraphProto` 摘出来:

[parser/parser/onnx/subgraph_adapter/if_subgraph_adapter.cc:25-39](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/subgraph_adapter/if_subgraph_adapter.cc#L25-L39) —— `IfSubgraphAdapter::AdaptAndFindAllSubgraphs`:委托 `ParseIfNodeSubgraphs`。

[parser/parser/onnx/subgraph_adapter/if_subgraph_adapter.cc:41-90](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/subgraph_adapter/if_subgraph_adapter.cc#L41-L90) —— `ParseIfNodeSubgraphs`:校验 `If` 必须恰好 2 个属性（`then_branch`/`else_branch`，由 `kAttrNameToIndex` 映射到 index 0/1）；为每个分支用 `OnnxUtil::GenUniqueSubgraphName` 生成唯一子图名，把 `attribute.mutable_g()`（即嵌套的 `GraphProto`）登记进 `name_to_onnx_graph`；然后 `GetSubgraphsAllInputs` + `AddInputNodeForGraph` + `AddInputForParentNode` 处理跨图输入。

**跨图输入补全**是 ONNX 子图适配的核心难点:

[parser/parser/onnx/subgraph_adapter/if_subgraph_adapter.cc:92-117](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/subgraph_adapter/if_subgraph_adapter.cc#L92-L117) —— `GetSubgraphsAllInputs`:扫描子图所有节点的 input/output/initializer，找出「被引用、但既非子图内部产出、也非子图 initializer」的张量名——这些就是子图**引用自父图**的输入。随后 `AddInputNodeForGraph` 给子图补上对应的 `input` 声明，`AddInputForParentNode` 给父 `If` 节点补上同名 input——这样就在父节点输入与子图输入之间建立了按 index 的映射关系，供后续自动 IO 映射使用。

**任务队列逐图解析。** 回到 `ModelParseToGraph`:

[parser/parser/onnx/onnx_parser.cc:911-999](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L911-L999) —— `ModelParseToGraph`:先 `AdaptAndFindAllOnnxGraph` 发现全部子图，再把根图作为首个任务入队；`while` 循环逐个取出，用同一个 `ModelParseToGraphImpl` 解析（`is_subgraph = (parent_node != nullptr)`）。根图解析结果回填 `root_graph`；每张图解析后做 `PostOpProcessForSubgraph` + `BuildLinkForChildAndParentGraph` + `GenSubgraphParseTasks`。

**挂回父节点。** `BuildLinkForChildAndParentGraph` 建立父子图引用:

[parser/parser/onnx/onnx_parser.cc:213-228](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L213-L228) —— `BuildLinkForChildAndParentGraph`:调用 `ge::NodeUtils::SetSubgraph(parent_node, index, sub_graph)`，把解析好的子图 `ComputeGraph` 按序号挂到父节点上。这一步完成了「ONNX 嵌套属性 → AscendIR 嵌套子图」的最后转换。

**子图后处理。** `PostOpProcessForSubgraph` 调用父算子注册的后处理函数（典型做 IO index 自动映射），并刷新子图内节点名保证全局唯一:

[parser/parser/onnx/onnx_parser.cc:230-273](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L230-L273) —— `PostOpProcessForSubgraph`:按父算子 `op_type` 向 `OpRegistry` 取 `ParseSubgraphPostFunc`（或 v2 版本），给子图节点重命名（`OnnxUtil::GenUniqueNodeName`，用 `graph_name/node_name` 形式），再调用后处理函数完成子图 IO 与父节点的自动映射。

**为下一层嵌套生成任务。** `GenSubgraphParseTasks` 保证多层嵌套子图都能被解析:

[parser/parser/onnx/onnx_parser.cc:187-211](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L187-L211) —— `GenSubgraphParseTasks`:扫描刚解析出的父节点的 `GetSubgraphNameIndexes()`，为每个子图（用 `OnnxUtil::GenUniqueSubgraphName` 生成唯一名）生成新任务入队。这就是「父节点解析出来后，它带的子图才有具体 GraphProto 可解析」的递归推进点。

**适配器的注册。** 与算子解析器同理:

[parser/parser/onnx/subgraph_adapter/if_subgraph_adapter.cc:133](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/subgraph_adapter/if_subgraph_adapter.cc#L133) —— `REGISTER_SUBGRAPH_ADAPTER_CREATOR("If", IfSubgraphAdapter)` 把 `If` 类型的子图适配器登记进工厂。新增带子图的算子只需新增一个 adapter 文件 + 一行注册，主解析流程零修改。

> 子图名/节点名去重工具定义在 [onnx_util.cc:48-55](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_util.cc#L48-L55):`GenUniqueSubgraphName` 拼成 `父节点名_序号_原名`，`GenUniqueNodeName` 拼成 `图名/节点名`，保证多层嵌套下子图与节点名全局唯一，避免后续编译阶段命名冲突。

#### 4.3.4 代码实践

**实践目标**:跟踪一个含 `If` 算子的 ONNX 模型，理解它的两个子图如何被摘取、独立解析、再挂回 `If` 节点。

**操作步骤**（源码阅读型）:

1. 打开测试 [tests/parser/ut/parser/testcase/onnx_parser_testcase/onnx_parser_unittest.cc:129-138](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/tests/parser/ut/parser/testcase/onnx_parser_testcase/onnx_parser_unittest.cc#L129-L138) 的 `onnx_parser_if_node`:它解析 `if.onnx`，预期 `FAILED`（因为该测试模型有环结构，拓扑排序失败）——这正好说明子图解析会走到拓扑排序步骤。
2. 对照 [parser/parser/onnx/onnx_parser.cc:859-909](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L859-L909)，描述 `If` 节点在 `AdaptAndFindAllOnnxGraph` 的 BFS 中如何被 `SubgraphAdapterFactory::CreateSubgraphAdapter("If")` 命中，进而由 `IfSubgraphAdapter` 把 `then_branch`/`else_branch` 摘出。
3. 跟踪 [parser/parser/onnx/subgraph_adapter/if_subgraph_adapter.cc:41-90](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/subgraph_adapter/if_subgraph_adapter.cc#L41-L90)，说明两个分支子图各自获得了唯一的子图名（`If节点名_0_then_branch` 与 `If节点名_1_else_branch`），并被登记进 `name_to_onnx_graph` 等待后续入队解析。

**需要观察的现象**:

- 子图在 `AdaptAndFindAllOnnxGraph` 阶段只是被「登记」（记录到 map），真正解析发生在后续任务队列里——这两个阶段是分开的。
- 一个 `If` 节点会产生两个独立的 `ComputeGraph`（then/else），最终都通过 `NodeUtils::SetSubgraph` 挂到同一个父 `If` 节点的 index 0 和 1 上。

**预期结果**:你能画出解析后的结构——父图里有一个 `If` 节点，它挂着两个子 `ComputeGraph`（then_branch、else_branch），父 `If` 节点的输入里额外多了子图引用的外部张量（由 `AddInputForParentNode` 补上的）。

> 待本地验证:`if.onnx` 测试模型因含环会解析失败。若想观察成功路径，可构造一个无环的 `If` ONNX（then/else 分支各自是简单算子），用 `aclgrphParseONNX` 解析后遍历 `compute_graph->GetAllSubgraphs()`，打印每个子图的名字与节点（本步骤需构造测试模型，运行 UT 请用 `ge-dt-runner` 技能）。

#### 4.3.5 小练习与答案

**练习 1**:`AdaptAndFindAllOnnxGraph` 为什么用 BFS（队列）而不是只遍历根图一层？

**参考答案**:因为子图里可能还嵌套着带子图的算子（如 `If` 的 then 分支里又有一个 `Loop`）。BFS 把每次新摘出的子图也入队，下一轮再扫描它内部的节点，从而能递归发现任意深度的嵌套子图。只遍历一层会漏掉深层子图。

**练习 2**:`IfSubgraphAdapter::GetSubgraphsAllInputs` 找出的「子图引用外部张量」，为什么要同时 `AddInputNodeForGraph`（加到子图）和 `AddInputForParentNode`（加到父节点）两边？

**参考答案**:AscendIR 的子图与父节点通过「父节点输入 index ↔ 子图 Data 节点」建立映射。子图引用了一个父图张量，意味着运行时这个张量要作为子图的输入传入——所以在子图里要补一个对应的 `input`（解析后会变成子图的 `Data` 节点），在父 `If` 节点也要补一个同名 `input`（让父图把这个张量喂给 `If`）。两边的 input 按相同顺序排列，后续 `ParseSubgraphPostFunc` 就能按 index 自动映射，实现 ONNX「子图隐式引用父图张量」到 AscendIR「显式 IO 映射」的转换。

**练习 3**:子图解析为什么能复用 4.1 讲的 `ModelParseToGraphImpl`（同一套 12 步）？根图和子图的行为差异是如何控制的？

**参考答案**:因为子图本身也是一张 `GraphProto`（有 node/initializer/input/output），结构与根图完全一样，自然可以用同一套「input→Data、initializer→Const、逐节点映射、回填连边」的流程解析。差异仅由 `is_subgraph` 标志控制少数几步:子图需要 `SetOutputs`（根图不需要）、子图需 `PostOpProcessForSubgraph` + `BuildLinkForChildAndParentGraph`（挂回父节点）、根图需 `SetOutputsInfo`（写进 ParserContext 供编译用）。这种「同一套核心流程 + 少量标志区分」是处理嵌套结构的常见手法。

---

## 5. 综合实践

把本讲三个模块串起来:假设有一个最小 ONNX 模型，结构如下——

```text
模型输入 X (ValueInfo, shape=[1,3,224,224], dtype=float)
initializer W (卷积权重, 内联 raw_data)
initializer B (大型权重, 外置文件 external_data)
节点序列:
  Conv (input: X, W ; output: Y)
  Add (input: Y, B ; output: Z)
  If (input: Z ; then_branch=<子图T>, else_branch=<子图E>)
模型输出: If 的输出
```

请完成以下「解析过程推演」任务（源码阅读 + 画图，无需昇腾设备）:

1. **节点映射（4.1）**:逐一说明 `X`、`W`、`B`、`Conv`、`Add`、`If` 各自会被解析成 AscendIR 的什么算子类型（提示:`X`→`Data`、`W`→`Const`、`B`→`FileConstant`、`Conv`/`Add`→同名、`If`→`If`）。指出 `Conv` 的算子类型经 `AdapterOpType` 时走的是 `kOnnxOpMap` 还是 `OpRegistry` 查询分支。
2. **权重解析（4.2）**:说明 `W` 的 `TensorProto` 在 `OnnxConstantParser` 里经过 `ParseConvertDataType` → `ParseConvertTensor` → `SetAttr("value", tensor)` 的过程，权重数据最终落在 AscendIR 的哪里；而 `B` 走 `OnnxFileConstantParser`，解析期只记录了哪些属性、为什么不读数据。
3. **连边回填（4.1）**:画出 `inputs_map_` / `outputs_map_` 中与张量名 `Y` 相关的条目，说明 `SetOperatorInputs` 如何据此把 `Conv` 的输出连到 `Add` 的输入。
4. **子图适配（4.3）**:说明 `If` 在 `AdaptAndFindAllOnnxGraph` 阶段如何被 `IfSubgraphAdapter` 处理，`then_branch`/`else_branch` 两个 `GraphProto` 如何被摘出、独立用 `ModelParseToGraphImpl` 解析，最后经 `NodeUtils::SetSubgraph` 挂回 `If` 节点的 index 0/1。

**验收标准**:

- 能准确说出三类常量（`Const`/`FileConstant`/`Data`）分别由哪个 OpParser 解析、注册在哪个类型字符串下。
- 能画出完整的 AscendIR 结构图，标注所有 Anchor 边的建立点（`SetOperatorInputs`）与子图挂载点（`SetSubgraph`）。
- 能解释「ONNX 张量名隐式连边」与「AscendIR Anchor 显式连边」的翻译发生在 `SetOperatorInputs` 这一步。

## 6. 本讲小结

- ONNX 用 protobuf（`ModelProto → GraphProto → NodeProto`）描述图，连边靠**张量名隐式匹配**；AscendIR 用 `ComputeGraph → Node + Anchor` 表达，连边是**显式对象引用**。解析的核心就是把前者翻译成后者。
- 解析由 `ModelParseToGraphImpl` 的 **12 步**驱动:收 initializer → 合成 input/const 节点 → 记 output → 补名/修正类型 → 预检 → 逐节点映射（`AdapterOpType` → `OperatorFactory::CreateOperator` → OpParser 解析）→ 用 `inputs_map_`/`outputs_map_` **回填 Anchor 边** → 拓扑排序 → 收集输出。根图与子图共用此流程。
- **算子类型映射**:`kOnnxOpMap` 处理 `Input/Constant/FileConstant` → `Data/Const/FileConstant`；普通算子拼 `domain::version::op_type` 向 `OpRegistry` 查询。
- **权重分三类**:内联常量（`Constant`/initializer 内联）→ `Const`，由 `OnnxConstantParser` 把 `TensorProto` 转 `ge::Tensor` 写进 `value` 属性；外置权重 → `FileConstant`，由 `OnnxFileConstantParser` 只记路径不读数据；模型输入 → `Data`，由 `OnnxDataParser` 合并 `--input_shape`。
- **类型转换**统一走 `OnnxUtil::ConvertOnnxDataType` 查表；**「外置则改 FileConstant」**的判定在 `ParseInitializer` 与 `UpdateConstantOpType` 两处对 initializer 和显式 Constant 节点分别生效。
- **子图适配**用 `SubgraphAdapterFactory`（按 op_type 查 adapter）+ BFS 摘取嵌套 `GraphProto` + 任务队列递归 `ModelParseToGraphImpl` + `NodeUtils::SetSubgraph` 挂回父节点；`IfSubgraphAdapter` 还负责补全子图引用父图张量带来的跨图输入。新增带子图算子只需新增 adapter + 一行注册。

## 7. 下一步学习建议

- **u3-l3 ATC 离线编译工具链**:本讲的 `OnnxModelParser::Parse` 在 atc 中是如何被 `ParseGraph`（`omg.cc`）触发、`--framework=onnx` 如何变成 `domi::ONNX` 进入工厂的，下一讲从 `main_impl.cc` 一路追下来。
- **u3-l4 GE 对外 API 与会话生命周期**:把解析放进 `AddGraph → Build → Run` 的完整生命周期，理解 parser 产出的 `ge::Graph` 如何被后续编译消费。
- **回看 u2-l4 算子注册与原型体系**:本讲反复出现的 `OperatorFactory::CreateOperator`、`OpRegistry::GetOmTypeByOriOpType`、`ParseSubgraphPostFunc`，其注册与查询机制正是 u2-l4 的运行时体现，两讲互为印证。
- **预告 u5-l1 通用图优化（常量折叠）**:本讲产出的 `Const` 节点（内联了权重数据），在编译的图优化阶段会被常量折叠 Pass 在编译期求值——届时你会明白为什么解析期要把权重完整搬进 `value` 属性。
