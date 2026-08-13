# 解析器框架:工厂模式与统一入口

## 1. 本讲目标

GE（Graph Engine）能吃进 ONNX、Caffe、TensorFlow 等多种前端格式，但后端只认一种图表示——AscendIR。本讲要回答一个关键问题:**这么多各不相同的前端模型，GE 是如何用同一套机制把它们统一翻译成 AscendIR 的？**

学完本讲，你应当能够:

- 说清 parser 模块用「工厂模式 + 自注册」支持多前端的设计思路。
- 掌握解析器的统一入口:从一条 `aclgrphParseONNX`（在线）或 `ParseGraph`（atc 离线）调用，如何最终走到 `model_parser->Parse()`。
- 了解 `parser/common` 这一公共转换层提供了哪些共享基础设施（基类、上下文、protobuf 读取、算子类型映射），让各前端解析器只关心「自家格式→AscendIR」这一段。

本讲是单元 3（前端接入与模型解析）的第一讲，承接 u2-l1 建立的 AscendIR 四层对象模型，为下一讲 u3-l2（ONNX 解析实战）和 u3-l3（ATC 工具链）打底。

## 2. 前置知识

阅读本讲前，你需要先具备以下概念（均来自前面的讲义）:

- **AscendIR 四层对象模型**:GE 内部的统一图表示是 `ComputeGraph → Node → OpDesc → GeTensorDesc`，连边由 Anchor 表达（见 u2-l1、u2-l2）。所有前端输入最终都要变成它。
- **静态图**:AscendIR 的核心拓扑在编译期固定，parser 的职责就是把这些固定的节点和边从外部格式「搬」进来。
- **在线 vs 离线**:在线场景由框架内部驱动 GE；离线场景用 `atc` 把模型文件编译成 OM（见 u1-l1、u1-l4）。parser 在两种场景下都被复用。
- **工厂模式（Factory Pattern）**:一种创建型设计模式——调用方不直接 `new` 具体类，而是告诉工厂「我要哪种」，由工厂返回对应实例。这样调用方和具体实现解耦。

本讲还会用到两个工程术语:

- **自注册（Self-Registration）**:具体的解析器类不被动等待别人来注册它，而是在自己的源文件里用一个「全局对象 + 宏」在程序启动时把自己登记进工厂。后续新增前端只需新增一个源文件，不必修改工厂代码——这叫对扩展开放、对修改关闭（OCP）。
- **FrameworkType（框架类型）**:GE 用一个枚举值区分不同前端（CAFFE=0、MINDSPORE=1、TENSORFLOW=3、ONNX=5 等），它是工厂选择解析器的「钥匙」。

## 3. 本讲源码地图

本讲涉及的源码按职责可分成四组:

| 文件 | 作用 |
| --- | --- |
| [inc/framework/omg/parser/parser_factory.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/framework/omg/parser/parser_factory.h) | 声明 `ModelParserFactory`、`WeightsParserFactory` 两个工厂，以及注册宏 `REGISTER_MODEL_PARSER_CREATOR` |
| [parser/parser/common/parser_factory.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/parser_factory.cc) | 工厂的实现:`Instance()` 单例、`CreateModelParser()`、`RegisterCreator()` |
| [parser/parser/common/op_parser_factory.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/op_parser_factory.cc) / [.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/op_parser_factory.h) | 第三个工厂 `OpParserFactory`，按**算子类型**（而非框架）创建单算子解析器 |
| [inc/framework/omg/parser/model_parser.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/framework/omg/parser/model_parser.h) | 抽象基类 `ModelParser`，定义统一的 `Parse()` 等接口 |
| [inc/framework/omg/parser/op_parser.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/framework/omg/parser/op_parser.h) | 抽象基类 `OpParser`，定义单算子解析接口 |
| [inc/graph_metadef/common/ge_common/ge_types.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/common/ge_common/ge_types.h) | `FrameworkType` 枚举（工厂的钥匙） |
| [parser/parser/onnx/onnx_parser.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc) | ONNX 解析器的统一入口 `aclgrphParseONNX` 与自注册代码 |
| [api/atc/omg.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/omg.cc) | atc 离线场景的解析入口 `ParseGraph` |
| [parser/parser/tensorflow/ge_parser_api_wrapper.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/tensorflow/ge_parser_api_wrapper.cc) | 在线场景（TF 适配层）的 C 包装入口 |
| [parser/parser/common/acl_graph_parser_util.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/acl_graph_parser_util.cc) / [parser_api.cc](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/parser_api.cc) | 公共转换层:参数解析、上下文、初始化、protobuf 读取 |

parser 模块的目录结构（`parser/parser/` 下）也印证了「一个前端一个目录 + 一个公共层」的组织方式:

```
parser/parser/
├── common/         # 公共转换层（工厂、基类、工具、上下文）
├── onnx/           # ONNX 前端解析器
├── caffe/          # Caffe 前端解析器
├── tensorflow/     # TensorFlow 前端解析器
├── func_to_graph/  # 函数式描述 → 图
└── stub/           # 空实现桩（用于裁剪编译）
```

## 4. 核心概念与源码讲解

本讲拆成三个最小模块:①ParserFactory 工厂；②统一解析入口；③common 转换层。

### 4.1 ParserFactory 工厂:按框架类型创建解析器

#### 4.1.1 概念说明

parser 面对的核心矛盾是:**前端格式很多，但后端处理流程只有一个**。如果每个入口都自己写一套 `if (ONNX) ... else if (CAFFE) ...`，代码会随前端数量膨胀，且每加一个前端就要改一堆老代码。

GE 的解法是经典的工厂模式:

- 定义一个抽象基类 `ModelParser`，约定统一的 `Parse()` 接口——后端只认这个接口，不关心是哪种前端。
- 每个前端写一个具体子类（如 `OnnxModelParser`、`CaffeModelParser`、`TensorFlowModelParser`）。
- 用一个工厂 `ModelParserFactory` 维护一张「框架类型 → 创建函数」的表。调用方只要给出 `FrameworkType`，工厂就返回对应的解析器实例。

更进一步，GE 用**自注册**机制让这张表自动填充:具体解析器在自己的源文件末尾写一行宏 `REGISTER_MODEL_PARSER_CREATOR(ONNX, OnnxModelParser)`，这行宏会展开成一个全局对象的构造——程序一启动，全局对象构造时就把「ONNX → 创建 OnnxModelParser」这条记录写进工厂的表里。工厂本身不知道有哪些前端存在，新前端只要新增文件即可。

GE 实际上有三个并列的工厂，分工不同:

| 工厂 | 钥匙（key） | 产出 | 用途 |
| --- | --- | --- | --- |
| `ModelParserFactory` | `FrameworkType`（框架） | `ModelParser` | 解析**整张模型图**的结构 |
| `WeightsParserFactory` | `FrameworkType`（框架） | `WeightsParser` | 解析**权重文件**（Caffe 把结构和权重分文件存放） |
| `OpParserFactory` | `OpType`（算子类型字符串） | `OpParser` | 解析**单个算子**的参数 |

#### 4.1.2 核心流程

工厂的「注册—查找—创建」流程可以用下面的伪代码描述:

```text
# 启动阶段（自注册，发生在 main 之前）
每个具体解析器源文件末尾:
  REGISTER_MODEL_PARSER_CREATOR(ONNX, OnnxModelParser)
    ├─ 定义创建函数 Creator_ONNX_Model_Parser() { return new OnnxModelParser(); }
    └─ 定义全局对象 g_ONNX_Model_Parser_Creator(ONNX, Creator_ONNX_Model_Parser)
         └─ 构造时调用 ModelParserFactory::Instance()->RegisterCreator(ONNX, fun)
              └─ creator_map_[ONNX] = fun   # 写进表里

# 运行阶段（解析时）
调用方: parser = ModelParserFactory::Instance()->CreateModelParser(ONNX)
  ├─ Instance()        # 取单例（Meyer's singleton）
  ├─ creator_map_.find(ONNX)
  └─ iter->second()    # 调用注册的创建函数，new 出 OnnxModelParser
parser->Parse(file, graph)   # 多态：实际跑的是 OnnxModelParser::Parse
```

两个要点:

1. **单例**:工厂用 `Instance()` 返回一个进程内唯一的实例，注册表 `creator_map_` 只此一份。
2. **存的是函数指针而非实例**:表里存的是「创建函数」`MODEL_PARSER_CREATOR_FUN`（一个返回 `shared_ptr<ModelParser>` 的函数指针），每次 `CreateModelParser` 都调一次函数 `new` 出新对象，避免多张图共享同一个有状态的解析器。

#### 4.1.3 源码精读

先看工厂的「钥匙」——`FrameworkType` 枚举，每种前端对应一个整数:

[inc/graph_metadef/common/ge_common/ge_types.h:31-37](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/common/ge_common/ge_types.h#L31-L37) —— 定义 CAFFE/MINDSPORE/TENSORFLOW/ONNX 等框架类型枚举值，是工厂选择解析器的 key。

再看工厂类本身。`ModelParserFactory` 持有一张 `creator_map_`，对外暴露 `CreateModelParser` 和 `RegisterCreator`:

[inc/framework/omg/parser/parser_factory.h:29-55](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/framework/omg/parser/parser_factory.h#L29-L55) —— `ModelParserFactory` 类声明，核心是私有的 `std::map<domi::FrameworkType, MODEL_PARSER_CREATOR_FUN> creator_map_`，构造函数是 `protected` 的，强制只能通过 `Instance()` 拿单例。

工厂的实现里，`Instance()` 用 C++ 函数内静态变量实现单例（Meyer's Singleton，线程安全初始化）:

[parser/parser/common/parser_factory.cc:47-50](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/parser_factory.cc#L47-L50) —— `ModelParserFactory::Instance()` 返回函数内静态实例，保证全局唯一。

`CreateModelParser` 做的就是查表 + 调用创建函数；查不到就报错返回 `nullptr`:

[parser/parser/common/parser_factory.cc:52-60](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/parser_factory.cc#L52-L60) —— 按 `type` 在 `creator_map_` 查找，命中则 `iter->second()` 创建解析器，未命中则记错误日志并返回 `nullptr`。

`RegisterCreator` 负责往表里登记，已登记过的会打 warning 并跳过（防止重复注册）:

[parser/parser/common/parser_factory.cc:62-71](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/parser_factory.cc#L62-L71) —— `RegisterCreator` 把「框架类型→创建函数」写入 map，重复注册时只告警不覆盖。

那么谁调用 `RegisterCreator`？答案藏在注册宏里。`REGISTER_MODEL_PARSER_CREATOR` 是关键——它一次性定义了创建函数和一个全局的 `ModelParserRegisterar` 对象:

[inc/framework/omg/parser/parser_factory.h:64-74](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/framework/omg/parser/parser_factory.h#L64-L74) —— 注册宏展开后生成「创建函数 + 全局 Registrar 对象」。全局对象在 `main` 前构造，从而完成自注册。

而 `ModelParserRegisterar` 的构造函数正是把创建函数交给工厂:

[parser/parser/common/parser_factory.cc:86-89](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/parser_factory.cc#L86-L89) —— `ModelParserRegisterar` 构造时调用 `ModelParserFactory::Instance()->RegisterCreator(type, fun)`，把这一前端登记进工厂。

最后看各前端是如何使用这个宏的。ONNX 解析器在自己的源文件末尾自注册:

[parser/parser/onnx/onnx_parser.cc:1260-1261](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L1260-L1261) —— ONNX 把 `OnnxModelParser` 和 `OnnxWeightsParser` 分别注册进模型工厂与权重工厂。

Caffe 和 TensorFlow 同理（参见 [parser/parser/caffe/caffe_parser.cc:2370-2371](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/caffe/caffe_parser.cc#L2370-L2371) 与 [parser/parser/tensorflow/tensorflow_parser.cc:4153-4154](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/tensorflow/tensorflow_parser.cc#L4153-L4154)）。三处写法完全一致，差别只在 `type` 和 `clazz` 两个参数——这正是工厂模式带来的「新增无侵入」收益。

补充:`OpParserFactory` 是第三个工厂，它的 key 是**算子类型字符串**而非框架，且每个框架有独立的工厂实例（`Instance(framework)` 内部用 `static std::map` 为每个框架缓存一个实例）。它服务于「单算子解析」，注册宏是 `REGISTER_OP_PARSER_CREATOR`:

[parser/parser/common/op_parser_factory.cc:41-61](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/op_parser_factory.cc#L41-L61) —— `OpParserFactory::Instance(framework)` 按框架惰性创建并缓存工厂实例（注释解释了为何不能用类的静态成员——构造顺序不确定会引发运行期错误）。

[parser/parser/common/op_parser_factory.cc:63-72](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/op_parser_factory.cc#L63-L72) —— `CreateOpParser(op_type)` 按算子类型查表创建 `OpParser`，机制与模型工厂一致。

[parser/parser/common/op_parser_factory.h:158-167](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/op_parser_factory.h#L158-L167) —— `REGISTER_OP_PARSER_CREATOR` 宏，结构与模型解析器注册宏同构。

#### 4.1.4 代码实践

**实践目标**:从源码层面确认「注册—查找—创建」这条链真实存在，并理解多态派发。

**操作步骤**（纯源码阅读型，无需昇腾设备）:

1. 打开 [parser/parser/onnx/onnx_parser.cc:1260](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L1260)，确认 ONNX 通过 `REGISTER_MODEL_PARSER_CREATOR(ONNX, ge::OnnxModelParser)` 自注册。
2. 在同目录找到 `OnnxModelParser` 的类声明 [parser/parser/onnx/onnx_parser_internal.h:39](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser_internal.h#L39)，确认它 `public domi::ModelParser`（继承抽象基类）。
3. 打开测试用例 [tests/parser/ut/parser/testcase/tensorflow_parser_testcase/tensorflow_parser_unittest.cc:1243](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/tests/parser/ut/parser/testcase/tensorflow_parser_testcase/tensorflow_parser_unittest.cc#L1243)，看测试代码如何用 `ModelParserFactory::Instance()->CreateModelParser(domi::TENSORFLOW)` 取得解析器并调用 `Parse`。
4. 同时打开测试桩文件 [tests/ge/ut/ge/session/main_unittest.cc:153-156](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/tests/ge/ut/ge/session/main_unittest.cc#L153-L156)，看测试如何为多种框架注册 `Stub` 解析器，体会「只要登记就能被工厂创建」。

**需要观察的现象**:

- 真实前端、测试桩，都通过同一行宏把「类型→创建函数」写进同一张表。
- `OnnxModelParser`、`CaffeModelParser`、`TensorFlowModelParser` 都继承自 `domi::ModelParser`，因此 `CreateModelParser` 返回的 `shared_ptr<ModelParser>` 可以统一调用 `->Parse()`，由虚函数实现多态派发。

**预期结果**:你能画出这样一张表（即 `creator_map_` 的运行时内容）:

| key（FrameworkType） | value（创建函数返回） |
| --- | --- |
| `CAFFE` (0) | `new CaffeModelParser` |
| `TENSORFLOW` (3) | `new TensorFlowModelParser` |
| `ONNX` (5) | `new OnnxModelParser` |

未注册的类型（如 `ANDROID_NN`）调用 `CreateModelParser` 会得到 `nullptr`。

> 待本地验证:若你想确认自注册真的在 `main` 前发生，可在 `ModelParserFactory::RegisterCreator` 里临时加一行日志打印 `type`，编译后用任一解析测试观察输出顺序（本步骤会改动源码，仅建议在本地实验分支进行，勿提交）。

#### 4.1.5 小练习与答案

**练习 1**:工厂的 `creator_map_` 存的是「创建函数指针」而不是「已构造好的解析器实例」。这样做有什么好处？

**参考答案**:每次 `CreateModelParser` 都调用函数 `new` 一个全新对象，避免多张图共用同一个有状态的解析器实例，消除并发与状态污染风险；同时也支持按需创建、不用不构造，节省内存。

**练习 2**:如果要在 GE 里新增一个「PyTorch 前端」解析器（假设框架类型 `PYTORCH`），需要修改 `parser_factory.cc` 吗？

**参考答案**:不需要。只需:① 在 `FrameworkType` 枚举里加 `PYTORCH`；② 新建 `parser/parser/pytorch/` 目录，实现一个继承 `ModelParser` 的 `PyTorchModelParser`；③ 在其源文件末尾写一行 `REGISTER_MODEL_PARSER_CREATOR(PYTORCH, PyTorchModelParser)`。工厂代码零修改——这正是自注册 + 工厂模式的扩展性收益。

**练习 3**:`OpParserFactory` 与 `ModelParserFactory` 的 key 有何不同？为什么 `OpParserFactory::Instance` 要带 `framework` 参数？

**参考答案**:`ModelParserFactory` 的 key 是 `FrameworkType`（一个进程一张表）；`OpParserFactory` 的 key 是算子类型字符串，但同一算子类型在不同框架下解析方式可能不同，所以它按 `framework` 维度为每个框架各维护一张表（`Instance(framework)` 内部用 `static std::map<FrameworkType, shared_ptr<OpParserFactory>>` 缓存每框架的工厂实例）。

---

### 4.2 统一解析入口:从模型文件到 AscendIR

#### 4.2.1 概念说明

工厂解决了「拿到解析器」的问题，但**谁来调用工厂、按什么顺序做准备工作、在哪里真正发起解析**？这就是「统一入口」要解决的。

GE 的解析入口随**在线/离线场景**不同而不同，但最终都会收束到同一条核心调用:

```text
model_parser = ModelParserFactory::Instance()->CreateModelParser(type);
model_parser->Parse(model_file, graph);   // graph 是输出，类型 ge::Graph
```

关键在于:不管哪种前端，`Parse` 的输出都是一个 `ge::Graph`（其内部即 AscendIR 的 `ComputeGraph`）。也就是说，**统一入口把「N 种输入」归一成了「1 种输出」**。

三个典型入口:

1. **离线 atc 入口**:`ParseGraph`（在 `omg.cc`），由 atc 命令行驱动，`type` 来自 `--framework` 参数。
2. **在线 ACL 入口**:`aclgrphParseONNX` 等（在各前端的 `_parser.cc`），由用户/框架直接调用，把模型文件或内存里的 protobuf 翻译成图。
3. **在线 TF 适配入口**:`GeApiWrapper_ParseProtoWithSubgraph`（C 符号），供 TorchAir/TF Adapter 这类在线适配层通过子图回调方式喂入序列化的图。

它们形态不同，但内部都遵循「前置参数解析 → 工厂取解析器 → `Parse` → 后置处理」的同构流程。

#### 4.2.2 核心流程

以 atc 离线入口为例，解析一条模型的完整步骤:

```text
ParseGraph(graph, params, model_file, weights_file, type, ...)   # omg.cc
  1. domi::GetContext().type = type            # 记下框架类型（供后续默认格式等判断）
  2. 创建空的 ComputeGraph，包装成 ge::Graph     # 输出容器
  3. InitDomiOmgContext(...)                   # 初始化上下文（input_shape/format 等）
  4. ParseOutNodes / ParseOutputFp16Nodes...   # 解析用户选项（输出节点、精度等）
  5. model_parser = ModelParserFactory::Instance()->CreateModelParser(type)  # 工厂派发
  6. model_parser->Parse(model_file, graph)    # 真正解析：外部格式 → AscendIR（多态）
  7. weights_parser = WeightsParserFactory::Instance()->CreateWeightsParser(type)
  8. weights_parser->Parse(weights_file, graph)  # 解析权重（结构/权重分文件时）
  9. compute_graph->Dump()                     # 打印解析后的图结构
```

在线 ACL 入口（`aclgrphParseONNX`）结构几乎一样，只是把步骤 1/3/4 收敛进一个 `PrepareBeforeParse`、把步骤 6 之后收敛进 `HandleAfterParse`，体现「前后包裹、中间统一」的模板。

#### 4.2.3 源码精读

先看抽象基类 `ModelParser`——它定义了所有前端解析器必须实现的统一接口，最核心的就是纯虚 `Parse`:

[inc/framework/omg/parser/model_parser.h:37-51](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/framework/omg/parser/model_parser.h#L37-L51) —— 抽象基类 `ModelParser`，`virtual Status Parse(const char *file, ge::Graph &graph) = 0` 是统一的「文件→图」入口；此外还声明了 `ParseFromMemory`、`ParseProto`、`ParseProtoWithSubgraph` 等多种数据来源的解析接口，以及默认返回 `UNSUPPORTED` 的可选实现。

> 注意:基类提供了大量重载（文件/内存/protobuf/带子图回调），其中 `ParseProto`、`ParseProtoWithSubgraph` 的部分重载有默认实现返回 `UNSUPPORTED`——这表示「不是每种前端都支持所有入口」，具体前端只 override 自己需要的那几个。

入口一:atc 离线。`ParseGraph` 的签名和工厂调用:

[api/atc/omg.cc:758-761](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/omg.cc#L758-L761) —— `ParseGraph` 入口签名，`type` 参数（`domi::FrameworkType`）决定解析哪种前端。

[api/atc/omg.cc:819-826](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/omg.cc#L819-L826) —— 真正的派发核心:`CreateModelParser(type)` 取解析器，再 `model_parser->Parse(model_file, graph)` 把模型解析进 `graph`。这两行就是「外部格式 → AscendIR」的总开关。

入口二:在线 ACL。`aclgrphParseONNX` 把「前置准备 → 取解析器 → Parse → 后置处理」组织得很清晰:

[parser/parser/onnx/onnx_parser.cc:88-90](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L88-L90) —— 在 `PrepareBeforeParse` 内用工厂 `CreateModelParser(domi::ONNX)` 取得解析器。

[parser/parser/onnx/onnx_parser.cc:110-141](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L110-L141) —— `aclgrphParseONNX` 完整流程:`PrepareBeforeParse`（含工厂取解析器）→ `model_parser->Parse(model_file, graph)` → `HandleAfterParse`（设置输出节点等）。与 atc 路径同构。

入口三:在线 TF 适配。TF 适配层走「子图回调」式接口，通过一个 `extern "C"` 的 C 符号暴露给宿主框架:

[parser/parser/tensorflow/ge_parser_api_wrapper.cc:36-43](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/tensorflow/ge_parser_api_wrapper.cc#L36-L43) —— `GeApiWrapper_ParseProtoWithSubgraph` 内部仍是 `CreateModelParser(TENSORFLOW)` 取解析器，再调用 `ParseProtoWithSubgraph`。即使数据来源是「序列化的子图 + 回调」，派发骨架依旧不变。

三条路径对比:

| 入口 | 触发方 | 数据来源 | 解析方法 |
| --- | --- | --- | --- |
| `ParseGraph`（omg.cc） | atc 命令行 | 模型文件 + 权重文件 | `Parse(file, graph)` |
| `aclgrphParseONNX` | 用户/框架 | 模型文件或内存 buffer | `Parse` / `ParseFromMemory` |
| `GeApiWrapper_*` | TF/TorchAir 在线适配 | 序列化子图 + 回调 | `ParseProtoWithSubgraph` |

无论哪条，都经过同一个工厂、输出同一个 `ge::Graph`。

#### 4.2.4 代码实践

**实践目标**:画出「工厂选择解析器」的完整流程图，确认三种入口殊途同归。

**操作步骤**（源码阅读 + 画图）:

1. 打开 [api/atc/omg.cc:758-886](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/api/atc/omg.cc#L758-L886)，从 `ParseGraph` 开始，标出「创建空图 → 初始化上下文 → 解析用户选项 → `CreateModelParser` → `Parse` → `CreateWeightsParser` → `Parse`」这几步在源码中的行号。
2. 打开 [parser/parser/onnx/onnx_parser.cc:110-141](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L110-L141)，把 `aclgrphParseONNX` 的步骤与上面 atc 路径逐条对应。
3. 用纸或工具画出流程图:起点是「用户给一个模型 + framework 类型」，中间是「工厂按 type 取解析器」，终点是「得到一个 `ge::Graph`」。

**需要观察的现象**:两个入口的「中间段」（取解析器 + `Parse`）几乎一字不差，差异只在前置选项解析和后置输出处理。

**预期结果**:得到一张类似下图的流程:

```text
[模型文件 + type] ──▶ [前置:上下文/选项] ──▶ ModelParserFactory::CreateModelParser(type)
                                                       │ (查 creator_map_)
                                                       ▼
                                          model_parser->Parse(file, graph)
                                                       │ (多态:ONNX/Caffe/TF)
                                                       ▼
                                              [后置:输出节点等]
                                                       │
                                                       ▼
                                            ge::Graph (= AscendIR ComputeGraph)
```

**预期结果**:三种入口都汇入同一条「工厂 + `Parse`」主干。

#### 4.2.5 小练习与答案

**练习 1**:`ModelParser::Parse` 的第二个参数是 `ge::Graph &graph`，它是输入还是输出？

**参考答案**:它是输出（out 参数）。调用方先创建一个空图（在 `ParseGraph` 里是 `MakeShared<ComputeGraph>` 再 `CreateGraphFromComputeGraph`），解析器把从外部模型读到的节点、边填进这张图。这也印证了 AscendIR 是 parser 的唯一产出格式。

**练习 2**:atc 路径里，`CreateModelParser` 和 `CreateWeightsParser` 为什么是**两次**分别调用？

**参考答案**:因为部分前端（典型如 Caffe）把「网络结构」和「权重数据」存在不同文件里（`.prototxt` 描述结构、`.caffemodel` 存权重）。结构先用 `ModelParser` 解析成图，权重再用 `WeightsParser` 灌进对应节点。ONNX 这类把权重内嵌进同一个文件的，`WeightsParser::Parse` 通常做轻量处理或直接返回成功。两段式设计兼容了两种文件组织方式。

**练习 3**:为什么在线 TF 适配入口要用 `extern "C"` 暴露 `GeApiWrapper_*` 符号？

**参考答案**:在线场景下，TF Adapter / TorchAir 这些宿主通常以动态库形式与 GE 链接，通过 C 符号（无 name mangling）跨库查找函数最稳妥。`extern "C"` 保证符号名稳定，宿主只需 `dlsym` 取到函数即可调用，不必关心 C++ ABI。

---

### 4.3 common 转换层:共享基础设施

#### 4.3.1 概念说明

如果把每个前端解析器比作一个「翻译员」，那 `parser/common` 目录就是他们共用的「办公桌」:统一的术语表（算子类型映射）、统一的草稿纸（`ParserContext` 上下文）、统一的原文扫描仪（protobuf 读取工具）、统一的岗位说明书（抽象基类）。

设置 common 转换层的目的，是**让各前端解析器只专注于「自家格式→AscendIR」这一段独有逻辑，把所有跨前端复用的能力下沉到公共层**。这样既避免重复造轮子，也保证了「无论哪种前端，解析后的 AscendIR 行为一致」。

common 层提供的能力主要包括:

- **抽象基类**:`ModelParser`、`WeightsParser`、`OpParser`，定义统一接口契约。
- **三个工厂**:`ModelParserFactory`、`WeightsParserFactory`、`OpParserFactory`（4.1 已讲）。
- **解析上下文 `ParserContext`**:在解析过程中暂存「用户选项 + 解析中间态」（如输出节点、输入 shape、格式等），用线程局部或全局实例在前后阶段间传递。
- **参数解析工具**:`AclGraphParserUtil` 提供 `ParseParamsBeforeGraph` / `ParseParamsAfterGraph` / `SetOutputNodeInfo` 等，把字符串形式的用户选项（如 `--input_shape`、`--out_nodes`）翻译成图上的属性。
- **protobuf 读取工具**:`ReadProtoFromBinaryFile`、`ReadProtoFromText`、`ReadProtoFromArray` 等，统一处理「从文件/内存读出 protobuf 结构」这一高频操作。
- **初始化与算子加载**:`ParserInitialize` → `AclParserInitialize` 负责加载算子原型库（`OpsProtoManager`）与自定义算子插件，让解析出的节点能对应到合法算子定义。
- **算子类型映射**:`kOnnxOpMap` 之类的前端算子名 → AscendIR 算子名映射表（如 ONNX 的 `Input` → GE 的 `Data`）。

#### 4.3.2 核心流程

common 层在一次解析任务中的参与时机，可对照入口流程标注:

```text
(进程级，一次性)
ParserInitialize(options)                      # parser_api.cc 公共 API
  └─ AclParserInitialize(options, is_train)    # acl_graph_parser_util.cc
       ├─ LoadOpsProtoLib()                    # 加载算子原型 .so（OpsProtoManager）
       ├─ TBEPluginLoader::LoadPluginSo()      # 加载自定义算子插件
       └─ 按当前 framework 登记算子注册信息      # OpRegistry

(每次解析任务)
入口函数(如 aclgrphParseONNX / ParseGraph)
  ├─ ParseParamsBeforeGraph(params)            # 解析 --out_nodes/--input_shape 等到 ParserContext
  ├─ ModelParserFactory::CreateModelParser()   # ← 工厂
  ├─ model_parser->Parse()                     # ← 前端特有逻辑（产出 AscendIR）
  ├─ ParseParamsAfterGraph(graph, params)      # 把 input_fp16 等落到图节点属性
  └─ SetOutputNodeInfo(graph)                  # 标记输出节点（common 层统一处理）
```

注意 common 层有**前后包夹**的设计:前端解析器只负责 `Parse` 这一段把外部节点搬进 AscendIR 的核心逻辑；而「解析前把用户选项规整进上下文」「解析后把输出节点、精度属性统一标到图上」这些与前端无关的事，都由 common 层的 `ParseParamsBeforeGraph` / `HandleAfterParse` 统一代劳。

#### 4.3.3 源码精读

公共 API 入口 `ParserInitialize` 极其精简，真正干活的是 `AclParserInitialize`:

[inc/framework/omg/parser/parser_api.h:21-25](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/framework/omg/parser/parser_api.h#L21-L25) —— 对外只暴露 `ParserInitialize` / `ParserFinalize` 两个 C++ 接口，隐藏内部细节。

[parser/parser/common/parser_api.cc:54-66](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/parser_api.cc#L54-L66) —— `ParserInitialize` 委托给 `AclParserInitialize`，并用 `parser_initialized` 标志防止重复初始化。

[parser/parser/common/acl_graph_parser_util.cc:216-265](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/acl_graph_parser_util.cc#L216-L265) —— `AclParserInitialize` 的核心:加载算子原型库 `LoadOpsProtoLib()`、加载自定义插件 `LoadPluginSo`、再按当前 `framework` 把算子注册信息登进 `OpRegistry`。这一步保证了之后 `Parse` 产出的每个节点都能在算子原型表里查到定义。

参数解析工具，把字符串选项翻译进上下文与图属性:

[parser/parser/common/acl_graph_parser_util.cc:647-707](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/acl_graph_parser_util.cc#L647-L707) —— `ParseParamsBeforeGraph`:校验选项合法性、设置默认格式、解析 `out_nodes`、`is_output_adjust_hw_layout`、`input_shape` 等，全部写入 `ge::GetParserContext()`。注意它会按框架设置默认数据排布（TF 默认 NHWC，其余 NCHW）。

[parser/parser/common/acl_graph_parser_util.cc:709-736](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/acl_graph_parser_util.cc#L709-L736) —— `ParseParamsAfterGraph`:在图已生成后，处理 `input_fp16_nodes` 等，把这些选项落到具体 `OpDesc` 的属性上。这正体现了「前端只管建节点，属性标注交给 common」。

[parser/parser/common/acl_graph_parser_util.cc:569-623](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/acl_graph_parser_util.cc#L569-L623) —— `SetOutputNodeInfo`:统一确定「整张图的输出节点」（用户指定的 `out_nodes` 或默认叶子节点），调用 `compute_graph->SetGraphOutNodesInfo`。这一步对 ONNX/Caffe/TF 都一样，所以放在 common 层。

protobuf 读取工具，是各前端解析器读取模型文件的共用扫描仪:

[parser/parser/common/acl_graph_parser_util.cc:843-880](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/acl_graph_parser_util.cc#L843-L880) —— `ReadProtoFromBinaryFile`:通用的「二进制 protobuf 文件 → Message」读取函数，带路径校验与大小上限检查（`kMaxFileSizeLimit`，2GB-1）。ONNX/Caffe 解析器都复用它读模型文件。

抽象基类 `OpParser`（与 `ModelParser` 配套，但面向单算子）:

[inc/framework/omg/parser/op_parser.h:28-44](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/framework/omg/parser/op_parser.h#L28-L44) —— `OpParser` 抽象基类，定义 `ParseParams`（解析算子参数）和 `ParseWeights`（解析算子权重）接口，由各前端的具体算子解析器实现，经 `OpParserFactory` 按算子类型派发。

最后看一例前端算子名→AscendIR 算子名映射，它属于 common 层共享的「术语表」:

[parser/parser/onnx/onnx_parser.cc:175-177](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L175-L177) —— `kOnnxOpMap` 把 ONNX 的 `Input`/`Constant` 等名映射为 GE 内部的 `Data`/`Constant`。这类映射是「外部格式 → AscendIR」语义对齐的第一步。

> 说明:不同前端的映射表各自放在对应前端目录里（如 ONNX 的在 `onnx_parser.cc`），但**映射机制**（map 查表替换算子类型）是 common 层约定的统一套路，各前端都遵循。

#### 4.3.4 代码实践

**实践目标**:验证 common 层「前后包夹」前端 `Parse` 的设计，体会职责切分。

**操作步骤**:

1. 打开 [parser/parser/onnx/onnx_parser.cc:64-141](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L64-L141)，定位三处 common 层调用:`ParseParamsBeforeGraph`（L77）、`model_parser->Parse`（L125）、`HandleAfterParse`（含 `ParseParamsAfterGraph` + `SetOutputNodeInfo`，L134）。
2. 跳到 `ParseParamsBeforeGraph` 实现 [acl_graph_parser_util.cc:647](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/acl_graph_parser_util.cc#L647) 与 `SetOutputNodeInfo` [acl_graph_parser_util.cc:569](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/common/acl_graph_parser_util.cc#L569)，确认它们与具体前端无关（没有 ONNX 专属逻辑）。
3. 思考:如果把 `ParseParamsBeforeGraph` 的逻辑挪进 `OnnxModelParser::Parse`，会发生什么？

**需要观察的现象**:`onnx_parser.cc` 里 `aclgrphParseONNX` 函数体很薄——绝大部分是 common 层调用，只有 `model_parser->Parse` 这一行是真正的 ONNX 专属逻辑。

**预期结果**:你会得出结论——前端解析器（`OnnxModelParser`）只负责「读 ONNX protobuf → 建 AscendIR 节点和边」，而「选项解析、输出节点设定、算子加载、protobuf 文件读取」全是 common 层复用的能力。如果把这些公共逻辑塞进各前端，每加一个前端就要重写一遍，且各前端行为难以保证一致。

> 待本地验证:若想直观看到 common 层的作用，可在 `ParseParamsBeforeGraph` 入口与 `SetOutputNodeInfo` 出口各加一行日志，用一个带 `--out_nodes` 的 atc 命令解析模型，观察两次日志之间 `ParserContext` 中 `user_out_nodes` 的变化（本步骤改动源码，仅建议本地实验）。

#### 4.3.5 小练习与答案

**练习 1**:`ParserContext`（通过 `ge::GetParserContext()` 访问）在解析过程中扮演什么角色？

**参考答案**:它是解析过程的「全局草稿本」——暂存用户传入的选项（输出节点、输入 shape、格式、fp16 配置等）和解析中间态，让 `ParseParamsBeforeGraph`（写）、前端 `Parse`（读/写）、`ParseParamsAfterGraph`/`SetOutputNodeInfo`（读）这几个阶段能在不互相传参的情况下共享状态。

**练习 2**:为什么 `SetOutputNodeInfo`（确定图输出节点）要放在 common 层，而不是让每个前端自己在 `Parse` 里处理？

**参考答案**:因为「图的输出节点由用户的 `--out_nodes` 选项或默认叶子节点决定」这套规则与前端无关——无论 ONNX/Caffe/TF，输出节点的确定逻辑都一样。放进 common 层既避免重复，又保证各前端产出的 AscendIR 在「输出语义」上一致。

**练习 3**:`AclParserInitialize` 里调用 `LoadOpsProtoLib()` 加载算子原型库，这与解析器框架有什么关系？

**参考答案**:parser 产出的 AscendIR 节点都有一个算子类型（`OpDesc.type`），这个类型必须能在算子原型注册表里查到合法定义（见 u2-l4）。`LoadOpsProtoLib` 在解析前先把算子定义 `.so` 加载进来，这样前端解析器建出的每个节点都「有名有姓」，后续编译阶段才能正确推导 shape、找到实现。这是 common 层为所有前端提供的、必不可少的运行环境准备。

---

## 5. 综合实践

把本讲三个模块串起来:假设团队要给 GE 增加一个最小化的 **JSON 计算图前端**（假设框架类型 `JSON_FMT` 已加进 `FrameworkType` 枚举）。请完成一份「接入方案」文档，要求:

1. **工厂模块**:说明你将在哪个新建源文件里、用哪一行宏自注册 `JsonModelParser`；指出你**完全不需要修改** `parser_factory.cc`。给出对应的注册代码示意（标注为「示例代码」）:

   ```cpp
   // 示例代码：parser/parser/json/json_parser.cc 末尾
   namespace domi {
   REGISTER_MODEL_PARSER_CREATOR(JSON_FMT, ge::JsonModelParser);
   }
   ```

2. **统一入口模块**:说明你的 JSON 前端会暴露哪个入口函数（参考 `aclgrphParseONNX`），并写出该入口里**必须出现的那两行核心调用**（工厂取解析器 + `Parse`）。
3. **common 转换层模块**:列出你的 `JsonModelParser::Parse` 可以直接复用 common 层的哪些能力（至少写出:protobuf/文件读取、`ParserContext`、`SetOutputNodeInfo`、算子类型映射表机制），以及它**不需要**自己实现的有哪些。

**验收标准**:

- 能准确说出「新增前端 = 新建一个继承 `ModelParser` 的类 + 一行注册宏 + 一个入口函数」，工厂与 common 层零修改。
- 能画出从「JSON 文件」到「`ge::Graph`」的完整数据流，标出 common 层在前/后的包夹位置。
- 能解释为什么 `JsonModelParser` 只需关心「JSON → AscendIR 节点」，其余交给 common。

> 本实践为源码阅读与设计型任务，无需昇腾设备，也不要求真正编译运行。

## 6. 本讲小结

- parser 用**三个工厂**（`ModelParserFactory`、`WeightsParserFactory`、`OpParserFactory`）按 `FrameworkType` 或算子类型创建解析器，工厂持有「key → 创建函数」的 map，单例 + 惰性创建。
- **自注册机制**（`REGISTER_MODEL_PARSER_CREATOR` 宏 + 全局 Registrar 对象）让新前端只需新增文件、零修改工厂，实现「对扩展开放、对修改关闭」。
- 解析有**三种入口**（atc 的 `ParseGraph`、在线的 `aclgrphParseONNX`、TF 适配的 `GeApiWrapper_*`），形态各异但都收束到同一句 `ModelParserFactory::CreateModelParser(type)` + `model_parser->Parse()`，产出统一的 `ge::Graph`（AscendIR）。
- `parser/common` 公共转换层提供基类、工厂、`ParserContext`、参数解析、protobuf 读取、算子加载与输出节点设定，**前后包夹**前端 `Parse`，让前端只关心「自家格式 → AscendIR」这一段。
- 多态是归一的关键:`ModelParser` 抽象基类定义统一 `Parse` 接口，各前端 override，调用方只面向基类编程。
- AscendIR 是 parser 的**唯一产出**:无论输入是 ONNX/Caffe/TF，输出都是同一种 `ComputeGraph`，这是 GE「统一编译枢纽」设计在解析层的直接体现。

## 7. 下一步学习建议

- **u3-l2 ONNX 模型解析实战**:本讲只讲到「工厂如何派发」，下一讲进入 `OnnxModelParser::Parse` 内部，看它具体如何把 ONNX 节点、权重、子图逐个翻译成 AscendIR 的 `Node` 与边，是本讲「前端特有逻辑」那一行的展开。
- **u3-l3 ATC 离线编译工具链**:本讲的 `ParseGraph` 入口在 atc 中如何被命令行触发，`--framework` 如何变成 `FrameworkType`，留待下一讲从 `main_impl.cc` 一路追下来。
- **u3-l4 GE 对外 API 与会话生命周期**:把解析放进 GE 的完整生命周期（`AddGraph → Build → Run`）中理解其位置。
- 建议同时回看 **u2-l4 算子注册与原型体系**:本讲 common 层 `AclParserInitialize` 里加载算子原型库的动作，正是 u2-l4 描述的算子注册体系的运行时触发点，两讲互为印证。
