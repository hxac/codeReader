# Tensor/属性/序列化与 ASCIR 桥接

## 1. 本讲目标

上一讲（u4-l1）我们把 graph_metadef 的「骨架」立起来了：`ComputeGraph` 装着一堆 `Node`，`Node` 通过 `Anchor` 互相连接，每个 `Node` 背后挂着一个 `OpDesc` 描述符、若干 `Operator` 接口。但骨架还缺两块「肉」：

- 数据在节点之间流动时，靠什么描述「这一路数据的形状、类型、格式」？
- 算子除了输入输出，那些形形色色的「属性」（axis、axis、reuse、size……）存在哪里、怎么读写？

本讲就补上这两块，并顺手回答一个贯穿后续 optimize/att/codegen 全流程的关键问题：**ASCIR 这个被反复提及的「图 IR」，到底和 graph_metadef 是什么关系？**

学完本讲，你应当能够：

1. 说清 graph_metadef 里**张量的两层结构**——外层 `Tensor/TensorDesc` 与内层 `GeTensor/GeTensorDesc`，以及二者通过 `TensorAdapter` 如何互转。
2. 说清 **shape / dtype / format** 三要素是如何被表示的，以及「当前值」与「origin 原始值」为何要分开存。
3. 说清 graph_metadef 的**统一属性存储机制**：`AttrHolder` + `AttrStore`（`ProtoAttrMap`），以及 `OperatorFactory` 这种「自注册工厂」怎么把算子登记进系统。
4. 画出 **ASCIR 与 graph_metadef 的衔接关系**：`ascir::Graph`、`ascir::NodeView`、`ascir::TensorView` 这些名字背后，实际是哪些 `af::Asc*` 类型，而这些类型又如何搭在 `ComputeGraph/Node/Anchor` 之上。

## 2. 前置知识

本讲承接 u4-l1，默认你已经知道：

- **ComputeGraph / Node / OpDesc**：图的容器、节点、算子描述符（见 u4-l1）。
- **Anchor**：节点上的「端口」，数据边连接的端点（`OutDataAnchor` → `InDataAnchor`）。

本讲会用到几个新术语，先用一句话解释：

- **TensorDesc（张量描述）**：描述一路数据的「形状（shape）+ 数据类型（dtype）+ 内存格式（format）」，但不包含数据本身。
- **GeTensor（GE 张量）**：TensorDesc 加上真正的字节缓冲（data），即「描述 + 数据」。
- **属性（attribute）**：挂在算子/张量/图上的「键值对」附加信息，例如 `size`、`reuse_input`、`origin_format`。
- **序列化（serialization）**：把内存里的图对象写成可保存、可传输的字节流（本项目用 protobuf），反之叫反序列化。
- **ASCIR**：Autofuse 内部对「计算图」的一套统一叫法（词汇层），后续 optimize/att/codegen 都用它来指代图、节点、张量。

> 小提示：本讲会出现 `af::` 和 `ge::` 两个命名空间。graph_metadef 自己的实现放在 `af::`，而它兼容一套老的 `ge::` 接口，二者通过 `using` 互相对应。读代码时把 `af::AscGraph` 和 `ge::AscGraph` 当成同一个东西即可。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tensor.cc](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/tensor.cc) | 张量**外层** API：`Shape`、`TensorDesc`、`Tensor`，以及把外层和内层互转的 `TensorAdapter` |
| [ge_tensor.cc](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/ge_tensor.cc) | 张量**内层** IR：`GeShape`、`GeTensorDesc`、`GeTensor`、`TensorData`，以及基于 protobuf 的序列化 `GeTensorSerializeUtils` |
| [operator_factory.cc](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/operator_factory.cc) | 算子工厂门面 `OperatorFactory` 与一组 `*Register` 自注册类 |
| [ascir.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/meta/ascir.h) | ASCIR 词汇层：把 `ascir::` 名字 `using` 到 `af::Asc*` 类型上 |

此外会引用两个「被 include 的头」来佐证衔接关系（它们在 `autofuse/inc/` 公共头目录下）：

| 文件 | 作用 |
|------|------|
| [ascendc_ir.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascendc_ir_core/ascendc_ir.h) | 定义 `af::AscGraph` / `AscNode` / `AscTensor` 这些「桥接类型」 |
| [attributes_holder.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/detail/attributes_holder.h) | 定义统一属性基类 `AttrHolder` 与 `ProtoAttrMap`（= `AttrStore`） |

## 4. 核心概念与源码讲解

本讲的三个最小模块是：**GeTensor/GeTensorDesc**、**属性存储与算子工厂**、**ASCIR 与 AscGraph 桥接**。

### 4.1 GeTensor/GeTensorDesc：张量的两层描述与序列化

#### 4.1.1 概念说明

要描述一路数据，最少需要三样东西：

- **shape（形状）**：每个维度多大，例如 `[2, 3]`。
- **dtype（数据类型）**：每个元素是什么类型，例如 `DT_FLOAT`（float32）、`DT_FLOAT16`。
- **format（格式）**：数据在内存里怎么排布，例如 `FORMAT_ND`（自由维度）、`FORMAT_NCHW`。

光有这三样，只能算一个「张量描述（TensorDesc）」；再加上真正的字节缓冲，才是一个完整的「张量（Tensor）」。

graph_metof 这套张量代码里，你会看到**两套**几乎同名的类，这是初学者最容易绕晕的地方：

| 层 | 主角类 | 所在文件 | 定位 |
|----|--------|----------|------|
| 外层（对用户/算子开发者） | `Shape` / `TensorDesc` / `Tensor` | tensor.cc | 简单、扁平、好用的 API |
| 内层（IR 底座） | `GeShape` / `GeTensorDesc` / `GeTensor` | ge_tensor.cc | 带 protobuf 序列化、带属性容器 |

为什么要分两层？因为**外层追求好用，内层追求可序列化、可挂属性**。外层 `TensorDesc` 把字段平铺成普通成员变量（一个 `shape_`、一个 `format_`、一个 `data_type_`…），用起来直观；但这些字段无法直接被序列化、也不方便统一管理「附加属性」。内层 `GeTensorDesc` 则把高频字段存在成员里、把低频/可扩展字段塞进一个**属性容器（`ProtoAttrMap`）**，并和 protobuf 消息绑定，从而支持落盘与跨进程传递。

> 直觉记忆：**外层是「门面」，内层是「底座」**。外层的 `Tensor` 内部其实就持有一个内层的 `GeTensor`。

另外，每个字段都分成「当前值」和「origin 原始值」两份。比如 `format_` 和 `origin_format_`、`shape_` 和 `origin_shape_`、`dtype_` 和 `origin_dtype_`。原因是：图在优化过程中可能会做**排布转换（format transfer）**，把张量从 `NCHW` 变成 `NC1HWC0` 之类。转换后「当前 format」变了，但「原始 format」必须记下来，以便需要时还原或校验。

#### 4.1.2 核心流程

一个张量从「构造」到「序列化」的大致流程：

```text
用户构造 TensorDesc(shape, format, dtype)
        │  TensorAdapter::TensorDesc2GeTensorDesc（外层 → 内层）
        ▼
GeTensorDesc（GeShape + format + dtype + 属性容器 + ext_meta）
        │  配上字节缓冲 data
        ▼
GeTensor（desc + tensor_data + 可选 protobuf owner）
        │  GeTensorSerializeUtils::GeTensorAsProto
        ▼
proto::TensorDef（可落盘 / 可反序列化还原）
```

张量的字节大小由 shape 与 dtype 共同决定。设某 dtype 每个元素占 `L(dtype)` 字节，n 维 shape 为 \((d_0, d_1, \dots, d_{n-1})\)，则理论字节数为：

\[
\text{bytes} = L(\text{dtype}) \times \prod_{i=0}^{n-1} d_i
\]

当某个维度是「未知维度」（`UNKNOWN_DIM = -1` 或 `UNKNOWN_DIM_NUM = -2`，用于动态 shape）时，乘积无意义，代码统一返回 `-1` 表示「算不出来」。

#### 4.1.3 源码精读

**外层 TensorDesc 的字段就放在这个 Impl 里**，一目了然：

[tensor.cc:111-138](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/tensor.cc#L111-L138) —— `TensorDescImpl` 把 `shape_`、`format_`/`origin_format_`、`data_type_`、`origin_shape_`、`size_`、`real_dim_cnt_`、`placement_` 等都作为普通成员变量平铺存放。注意它同时保留「当前」与「origin」两套 shape/format。

外层 `Tensor` 其实是内层 `GeTensor` 的一层包装——看 `TensorImpl` 的私有成员：

[tensor.cc:226-230](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/tensor.cc#L226-L230) —— `TensorImpl` 持有一个 `GeTensor ge_tensor;`，所有 `Tensor::SetData/GetData` 最终都转发给它。这正说明了「外层包内层」。

外层和内层之间的互转，由工具类 `TensorAdapter` 完成。看「外层 TensorDesc → 内层 GeTensorDesc」：

[tensor.cc:947-984](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/tensor.cc#L947-L984) —— `TensorDesc2GeTensorDesc` 先用 shape/format/dtype 构造一个 `GeTensorDesc`，再把外层的 origin shape/format、reuse、name、placement、shape range、size、real_dim_cnt 一个个搬过去。反向 `GeTensorDesc2TensorDesc`（[tensor.cc:986-1018](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/tensor.cc#L986-L1018)）是对称的。`TensorAdapter` 还提供 `GeTensor2Tensor`、`AsGeTensor`、`AsGeTensorPtr` 等桥接函数（[tensor.cc:1020-1064](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/tensor.cc#L1020-L1064)）。

> 关键认知：**`TensorAdapter` 是两层之间的「翻译官」**。后续 optimize/codegen 读到的多是内层 `GeTensorDesc`，而算子开发者面对的多是外层 `TensorDesc`，二者通过它对接。

再看内层。`GeShape` 用了一个优化过的 `SmallVector` 存维度，并对未知维度做了专门处理：

[ge_tensor.cc:387-416](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/ge_tensor.cc#L387-L416) —— `GeShapeImpl` 用 `SmallVector<int64_t, kDefaultDimsNum> dims_` 存维度。`IsUnknownDimNum()` 判断是不是「维度个数都未知」（`dims_ == {UNKNOWN_DIM_NUM}`）。`GetShapeSize()` 就是上节那个连乘公式 \(\prod d_i\)，遇到未知维度返回 `-1`（[ge_tensor.cc:490-508](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/ge_tensor.cc#L490-L508)）。

`DataType` 枚举到 protobuf 枚举的映射，是序列化的基础：

[ge_tensor.cc:38-81](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/ge_tensor.cc#L38-L81) —— `kDataTypeMap` 把每个 `DataType`（如 `DT_FLOAT`、`DT_FLOAT16`、`DT_BF16`、`DT_INT8`…）一一映射到 `proto::DataType`。序列化时查表写 proto，反序列化时反向查表还原。

序列化的核心入口（内层 `GeTensorDesc` → protobuf）：

[ge_tensor.cc:132-196](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/ge_tensor.cc#L132-L196) —— `GeTensorSerializeUtils::GeTensorDescAsProto` 把 ext_meta（size、weight_size、reuse_input、device_type…）、属性容器、origin format/shape、dtype、layout、shape 依次写进 `proto::TensorDescriptor`。反向还原见 `AssembleGeTensorDescFromProto`（[ge_tensor.cc:222-227](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/ge_tensor.cc#L222-L227)）。

最后，内层 `GeTensor` 把「描述 + 数据 + 可选 proto owner」三者合一：

[ge_tensor.cc:1327-1348](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/ge_tensor.cc#L1327-L1348) —— 从 protobuf 构造 `GeTensorImpl` 时，会从 proto 恢复出 `GeTensorDesc`，并让 `tensor_data_` 与 `desc_` 共享同一个描述（注释里解释了为什么必须共享：避免改了描述却反映不到数据上）。`protoOwner_` 非空表示「数据来自一块共享的 protobuf 大对象」，此时用零拷贝方式借用指针。

#### 4.1.4 代码实践

> 实践目标：通过**源码阅读**，验证「外层 `Tensor` 包内层 `GeTensor`」这条链，并理解两层互转。

操作步骤：

1. 打开 [tensor.cc](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/tensor.cc)，定位 `TensorImpl`（[L140](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/tensor.cc#L140)），确认它的私有成员是 `GeTensor ge_tensor;`。
2. 跟着 `Tensor::GetData()`（[L644-L656](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/tensor.cc#L644-L656)）看它如何转发到 `impl->ge_tensor.GetData()`。
3. 打开 `TensorAdapter::TensorDesc2GeTensorDesc`（[L947](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/tensor.cc#L947)），数一数它从外层 `TensorDescImpl` 搬运了多少个字段到内层 `GeTensorDesc`。

需要观察的现象：`Tensor` 对外暴露的几乎所有方法（`SetData`、`GetData`、`SetFormat`、`GetDataType`…）的方法体都形如 `impl->ge_tensor.XXX()`，几乎不做额外计算——这印证了外层只是壳。

预期结果：你能画出 `Tensor → TensorImpl → GeTensor(impl) → GeTensorImpl{desc_, tensor_data_, tensor_def_}` 的包含关系，并指出两层互转的唯一通道是 `TensorAdapter`。

> 说明：本实践为源码阅读型，不依赖 NPU 环境，无需运行命令。如果你想「动手跑」，可在 autofuse 的 framework UT 里搜索 `TensorDesc(` 的构造用例对照阅读（待本地验证具体用例路径）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `GeTensorDesc` 既要存 `format_` 又要存 `origin_format_`？

> 参考答案：图优化会做排布转换（format transfer），转换后当前 `format_` 改变，但「原始排布」必须用 `origin_format_` 留底，用于校验、还原或决定 codegen 的真实布局。`shape_`/`origin_shape_`、`dtype_`/`origin_dtype_` 同理。

**练习 2**：一个 `DT_FLOAT16`、shape 为 `[2, 3, 4]` 的张量，理论字节大小是多少？如果某一维是 `UNKNOWN_DIM(-1)` 呢？

> 参考答案：float16 每元素 2 字节，\(2 \times (2 \times 3 \times 4) = 48\) 字节。若任一维为 `-1`，`GetShapeSize()` 直接返回 `-1`，表示大小未知（动态 shape 场景）。

**练习 3**：`TensorAdapter::AsGeTensorShared` 与 `AsGeTensor` 都能从 `Tensor` 取出 `GeTensor`，它们的差别在哪？（提示：看是否共享同一个 impl）

> 参考答案：`AsGeTensor`（[L1044](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/tensor.cc#L1044)）返回内层 `GeTensor` 的拷贝/引用；`AsGeTensorShared`（[L1058-L1064](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/tensor.cc#L1058-L1064)）通过 `GeTensor(tensor.impl->ge_tensor.impl_)` 直接共享底层 impl 指针，避免深拷贝，用于需要零拷贝共享数据的场景。

### 4.2 属性存储（AttrStore/AttrHolder）与算子工厂（OperatorFactory）

#### 4.2.1 概念说明

光有 shape/dtype/format 还不够。一个算子或张量上常常需要挂很多**附加信息**：这块 tensor 的 `size`、能不能 `reuse_input`、它的 `device_type`、它的 `shape_range`……如果每加一种信息就给类加一个成员变量，类会膨胀到不可维护。

graph_metadef 的解法是**统一的属性容器**：

- 所有「能挂属性的类」（`GeTensorDesc`、`OpDesc`、`ComputeGraph`、`NamedAttrs`…）都继承自抽象基类 **`AttrHolder`**。
- `AttrHolder` 规定每个子类必须实现两个纯虚函数 `MutableAttrMap()` / `GetAttrMap()`，返回一个 **`ProtoAttrMap`**——它其实只是 `AttrStore` 的别名。
- 这样，不管具体子类是什么，读写属性的接口（`SetAttr/GetAttr/HasAttr`）和序列化逻辑都由 `AttrHolder` 统一提供。

另一个难点是**算子从哪里来**。Autofuse 要认识的算子（Add、Reduce、Compare……）成百上千，不可能在 `OperatorFactory` 里写一个巨大的 `switch`。本项目用的是经典的**自注册工厂（self-registering factory）**模式：

- 全局静态对象在程序启动时，构造函数里把自己「登记」进工厂。
- 工厂内部维护一张 `算子类型 → 创建函数/推导函数` 的表。
- 用的时候只要给一个类型字符串（如 `"Add"`），工厂就能造出对应的 `Operator`。

#### 4.2.2 核心流程

属性读写的流程：

```text
任意 AttrHolder 子类（如 GeTensorDesc）
        │  实现 MutableAttrMap() 返回自己的 AttrStore
        ▼
AttrHolder::SetAttr(name, AnyValue)  /  GetAttr(name)  /  HasAttr(name)
        │  （或用工具类 AttrUtils 做类型化读写：SetInt/GetBool/SetListInt…）
        ▼
AttrStore（键 → AnyValue 的容器，可序列化为 proto::AttrDef）
```

算子注册与创建的流程：

```text
程序启动
   │  各全局 *Register 对象的构造函数执行
   ▼
OperatorFactoryImpl 内部登记表：
   op_type → { creator, infer_shape, infer_format, infer_value_range, verify }
   │
   ▼  运行期调用
OperatorFactory::CreateOperator(name, "Add")  → 查表 → 调 creator → 返回 Operator
OperatorFactory::IsExistOp("Add")             → 查表是否存在
OperatorFactory::GetOpsTypeList(all_ops)      → 列出所有已登记类型
```

#### 4.2.3 源码精读

先看属性基类。`ProtoAttrMap` 只是 `AttrStore` 的别名：

[attributes_holder.h:53-55](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/detail/attributes_holder.h#L53-L55) —— `using ProtoAttrMap = AttrStore;`、`using ConstProtoAttrMap = const AttrStore;`、`using ProtoMsgOwner = std::shared_ptr<protobuf::Message>;`。

`AttrHolder` 提供统一的属性读写接口，并要求子类实现两个纯虚函数：

[attributes_holder.h:135-156](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/detail/attributes_holder.h#L135-L156) —— `SetAttr`（已存在则刷新值）、`TrySetAttr`（已存在则不刷新）、`GetAttr`、`HasAttr`、`DelAttr`，以及纯虚 `MutableAttrMap()`/`GetAttrMap()`（[L253-L254](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/detail/attributes_holder.h#L253-L254)）。此外还有一套独立的「扩展属性」`SetExtAttr/TryGetExtAttr`，背后是另一个 `AnyMap ext_attrs_`（[L264](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/detail/attributes_holder.h#L264)），用于不想进 proto 序列化的临时附加信息。

那 `GeTensorDesc` 是怎么落实这两个纯虚函数的？它把自己的 `attrs_` 成员交出去：

[ge_tensor.cc:723-729](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/ge_tensor.cc#L723-L729) —— `GeTensorDescImpl::MutableAttrMap()` 返回 `attrs_`，`GetAttrMap()` 返回 `const attrs_`。外层 `GeTensorDesc` 再转调 impl（[ge_tensor.cc:803-809](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/ge_tensor.cc#L803-L809)）。正因如此，前一小节序列化时才能用 `desc.attrs_.GetAllAttrs()` 把属性一次性写进 proto（[ge_tensor.cc:151](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/ge_tensor.cc#L151)）。

实际工程里很少直接调 `SetAttr(name, AnyValue)`，而是用类型化的工具类 `AttrUtils`，例如把 `size` 这种低频字段当属性存：

[ge_tensor.cc:1621-1635](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/ge_tensor.cc#L1621-L1635) —— `TensorUtils::GetSize/SetSize` 其实是通过 `impl_->ext_meta_` 存取；而像 `shape_range`、`placement`、`ref_port_index` 这类，则在 `GeTensorDesc` 上用 `AttrUtils::SetListListInt`/`SetInt` 存进属性容器（见 [ge_tensor.cc:864-880](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/ge_tensor.cc#L864-L880) 与 [ge_tensor.cc:985-993](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/ge_tensor.cc#L985-L993)）。这就是「高频字段进成员、低频字段进属性容器」的工程取舍。

再看算子工厂。`OperatorFactory` 是一个**门面（facade）**，几乎所有方法都一行转给 `OperatorFactoryImpl`：

[operator_factory.cc:15-17](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/operator_factory.cc#L15-L17) —— `CreateOperator(name, type)` 按类型字符串造算子；[operator_factory.cc:30-32](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/operator_factory.cc#L30-L32) —— `GetOpsTypeList` 列出全部已登记类型；[operator_factory.cc:47-49](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/operator_factory.cc#L47-L49) —— `IsExistOp` 判断某类型是否已登记。

注册则靠一组 `*Register` 类——它们的**构造函数**负责登记，把函数对象塞进 `OperatorFactoryImpl` 的表里：

[operator_factory.cc:61-63](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/operator_factory.cc#L61-L63) —— `OperatorCreatorRegister(type, creator)` 登记「创建函数」；[operator_factory.cc:73-76](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/operator_factory.cc#L73-L76) —— `InferShapeFuncRegister` 登记「形状推导函数」；此外还有 `InferFormatFuncRegister`（[L87-L90](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/operator_factory.cc#L87-L90)）、`InferValueRangeFuncRegister`（[L101-L108](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/operator_factory.cc#L101-L108)）、`VerifyFuncRegister`（[L118-L120](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/operator_factory.cc#L118-L120)）。

> 用法：某个 `.cpp` 文件里写一个文件作用域的静态对象，例如
> `static OperatorCreatorRegister g_reg("Add", CreateAdd);`
> 程序一加载，该全局对象构造，`"Add"` 就被登记进工厂。ASCIR 的 `reg_func/*.cpp`（见 u5-l2）正是用这套机制把算子一个个登记进来的。

#### 4.2.4 代码实践

> 实践目标：在 `operator_factory.cc` 中定位算子注册方式，理解「自注册」如何工作；并在 `GeTensorDesc` 上验证「属性容器」的存在。

操作步骤：

1. 在 [operator_factory.cc](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/operator_factory.cc) 中找到所有名字以 `Register` 结尾的类（共 5 类：`OperatorCreatorRegister`、`InferShapeFuncRegister`、`InferFormatFuncRegister`、`InferValueRangeFuncRegister`、`VerifyFuncRegister`），确认它们的构造函数都调了 `OperatorFactoryImpl::RegisterXxx`。
2. 用 `grep` 在 `autofuse/` 下搜索 `OperatorCreatorRegister(` 或 `InferShapeFuncRegister(` 的实际使用点（示例命令，待本地验证）：
   ```bash
   grep -rn "OperatorCreatorRegister\|InferShapeFuncRegister" autofuse --include=*.cpp | head
   ```
3. 在 [ge_tensor.cc:723-729](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/ge_tensor.cc#L723-L729) 确认 `GeTensorDescImpl` 把自己的 `attrs_` 作为 `ProtoAttrMap` 暴露出去，于是它「是一个 `AttrHolder`」。

需要观察的现象：`operator_factory.cc` 本身**不包含任何具体算子的实现**，它只提供「登记表 + 查表接口」。具体算子的登记散落在各 `reg_func/*.cpp` 与算子实现文件里——这就是自注册工厂「开放添加、无需改工厂」的好处。

预期结果：你能用自己的话说明——「新增一个算子，不需要修改 `OperatorFactory`，只要在新文件里放一个静态 `*Register` 全局对象即可被工厂识别」。

> 说明：第 2 步的 grep 结果取决于本地代码版本，若数量很多属正常现象（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`AttrHolder::SetAttr` 和 `AttrHolder::SetExtAttr` 有什么区别？

> 参考答案：`SetAttr` 写入子类的 `ProtoAttrMap`（即 `AttrStore`），会参与 protobuf 序列化、可随图一起落盘；`SetExtAttr` 写入独立的 `AnyMap ext_attrs_`，是「扩展属性」，不进 proto，通常用于运行期临时附加、不需要持久化的信息。

**练习 2**：为什么 `OperatorFactory` 要同时提供 `CreateOperator` 和 `IsExistOp` 两个接口？在 Autofuse 的 fallback 判定里（见 u3-l3）哪个更有用？

> 参考答案：`CreateOperator` 会真正构造算子对象（有开销、可能失败），而 `IsExistOp` 只查表判断「这个类型登记过没有」，开销极小。Autofuse 在决定某算子能否进入融合流程前，通常先用 `IsExistOp` 快速判断是否被支持，不支持就直接 fallback——这样避免了无谓的构造开销。

**练习 3**：`GeTensorDesc` 上的 `shape_range`、`placement` 用 `AttrUtils::SetListListInt`/`SetInt` 存进属性容器，而 `dtype_`/`format_` 却是普通成员变量。这种「区别对待」的依据是什么？

> 参考答案：依据是**访问频率与是否需要直接运算**。`dtype`/`format`/`shape` 在编译期几乎每一步都要被频繁读取和比较，做成成员变量访问最快；`shape_range`/`placement` 等只在特定阶段（如动态 shape 推导、内存规划）用到，频率低，放进通用属性容器既省成员变量、又能被统一序列化，是合理的工程取舍。

### 4.3 ASCIR 与 AscGraph 桥接

#### 4.3.1 概念说明

翻看 optimize、att、codegen 的源码，你会发现它们几乎都在用一个叫 `ascir::` 的命名空间：`ascir::Graph`、`ascir::NodeView`、`ascir::TensorView`、`ascir::SizeExpr`……但本单元一直在讲 graph_metadef 的 `ComputeGraph/Node/GeTensorDesc`。这二者什么关系？

答案是：**ASCIR 是一层「词汇别名」，它背后实际就是 graph_metadef 的 `Asc*` 类型**。换句话说，`ascir::Graph` 不是新造一套图，而是一行 `using` 指向了 `af::AscGraph`；而 `af::AscGraph` 又是搭在 graph_metadef 核心 `ComputeGraph` 之上的一层「调度增强」封装。

为什么多此一举？两个好处：

1. **解耦命名**：optimize/att/codegen 只认 `ascir::` 这套稳定词汇，不必关心底层是 `af::AscGraph` 还是将来换别的实现。
2. **分层职责**：`ComputeGraph/Node/Anchor` 提供「拓扑 + 基本描述」；`AscGraph/AscNode/AscTensor` 在其之上加了 Autofuse 关心的「轴（Axis）、大小变量（SizeVar）、tiling 切分」等调度概念；`ascir::` 再给这层换个稳定名字。

#### 4.3.2 核心流程

ASCIR 三大对象与 graph_metadef 核心类型的对应关系：

```text
ascir::Graph        ──using──▶ af::AscGraph   ──持有──▶ AscGraphImpl ──持有──▶ ComputeGraphPtr（graph_metadef 核心图）
ascir::NodeView     ──using──▶ af::AscNodePtr (shared_ptr<AscNode>)，AscNode : public Node（继承核心 Node）
ascir::TensorView   ──using──▶ af::AscTensor   ──view──▶ OutDataAnchor + AscTensorAttr（看的是核心图的出边端口）
ascir::SizeExpr     ──using──▶ af::Expression（符号化大小表达式，用于动态 shape/tiling）
```

一句话总结：**数据物理上一直住在 graph_metadef 的 `ComputeGraph/Node/Anchor/AttrStore` 里，ASCIR 只是对同一份数据换了一套「带调度语义」的叫法和访问视图。**

#### 4.3.3 源码精读

先看 ASCIR 这层别名本身——`ascir.h` 整个文件几乎全是 `using`：

[ascir.h:21-34](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/meta/ascir.h#L21-L34) —— `Graph`/`HintGraph`/`ImplGraph` 全部 `= af::AscGraph`；`NodeView = af::AscNodePtr`；`TensorAttr`/`TensorView = af::AscTensor`；`TensorPtr = af::AscTensorAttr*`；`SizeExpr = af::Expression`；`SizeVar = af::SizeVar`。

还有一组枚举与 id 类型的别名：

[ascir.h:37-50](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/meta/ascir.h#L37-L50) —— `ComputeType`/`ComputeUnit`/`ApiType`/`AllocType`/`MemHardware`/`Position` 等枚举，以及 `TensorId`/`BufId`/`QueId`/`MergeScopeId`/`ReuseId`（都是 `int64_t` 的 `Identifier`）。这些是 optimize/codegen 在讨论「这块 tensor 放哪个存储（UB/GM）、属于哪个 buffer/queue」时用的词汇。

那么 `af::AscGraph` / `AscNode` / `AscTensor` 到底长什么样？它们定义在公共头 `ascendc_ir.h`（`af` 命名空间）。先看张量视图：

[ascendc_ir.h:435-440](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascendc_ir_core/ascendc_ir.h#L435-L440) —— `struct AscTensor` 只有两个引用成员 `AscTensorAttr &attr` 和 `const OutDataAnchor &anchor`，注释标注「not owner」（不持有所有权）。也就是说，**`AscTensor` 就是「核心图某条出边端口 + 它的附加属性」的一个非持有视图**。

再看节点：

[ascendc_ir.h:471-478](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascendc_ir_core/ascendc_ir.h#L471-L478) —— `class AscNode : public Node`——ASCIR 节点**公有继承**自 graph_metadef 核心 `Node`！它额外加了 `inputs`/`outputs`（一组 `AscTensor`）和 `attr`。`using AscNodePtr = std::shared_ptr<AscNode>;`（[L478](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascendc_ir_core/ascendc_ir.h#L478)）。因为继承自 `Node`，凡是对 `Node` 成立的拓扑操作（u4-l1 讲的 `AddEdge`、`TopologicalSorting` 等）对 `AscNode` 都直接可用。

最后看图：

[ascendc_ir.h:549-611](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascendc_ir_core/ascendc_ir.h#L549-L611) —— `class AscGraph` 持有 `std::shared_ptr<AscGraphImpl> impl_`（[L610](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascendc_ir_core/ascendc_ir.h#L610)），对外提供 `AddNode`、`GetAllNodes`、`CreateAxis`、`BlockSplit`、`TileSplit`、`MergeAxis` 等 Autofuse 调度相关接口。这些接口的真正实现位于 `AscGraphImpl`（`ascendc_ir_impl.h`）。

`AscGraphImpl` 内部就握着一张 graph_metadef 的核心 `ComputeGraph`：

[ascendc_ir_impl.h:32-50](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/ascendc_ir/core/ascendc_ir_impl.h#L32-L50) —— `class AscGraphImpl`，它的 `AddNode(Operator&)`、`GetAllNodes()` 等都建立在核心 `ComputeGraph` 之上（该文件 `#include "graph/compute_graph.h"`、`"graph/node.h"`、`"graph/anchor.h"`，并提供 `const ComputeGraphPtr GetComputeGraph() const;`）。

> 关键认知：`ascir::Graph`（= `af::AscGraph`）是「同一张 `ComputeGraph`」加了调度语义的封装，而不是另一张独立的图。这就解释了为什么 u3-l2 说「optimize/att/codegen 的源码合流进同一共享库」——它们操作的本就是同一份图数据。

补充一个细节：公共头末尾把所有 `af::Asc*` 类型重新 `using` 进了 `ge::` 命名空间：

[ascendc_ir.h:614-634](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascendc_ir_core/ascendc_ir.h#L614-L634) —— `namespace ge { using af::AscGraph; using af::AscNode; ... }`。所以你在代码里看到 `ge::AscGraph` 和 `af::AscGraph`，是同一个类型，这与张量那节 `af::/ge::` 双命名空间的套路完全一致。

#### 4.3.4 代码实践

> 实践目标：在 `ascir.h` 中找出 ASCIR 与 graph_metadef 图衔接的关键类型，并写出二者如何对应。这是本讲规格指定的核心实践。

操作步骤：

1. 打开 [ascir.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/meta/ascir.h)，把每一条 `using` 左边的 `ascir::` 名字与右边的 `af::` 类型抄成一张对照表。
2. 对每个 `af::Asc*` 类型，在 [ascendc_ir.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascendc_ir_core/ascendc_ir.h) 中找到它的定义，确认它「搭在哪个 graph_metadef 核心类型上」。
3. 画出三层关系图：`ascir::*`（词汇）→ `af::Asc*`（调度封装）→ `ComputeGraph/Node/Anchor`（核心 IR）。

需要观察的现象：`ascir.h` 里**没有任何成员变量或函数定义**，全部是 `using`——这证明 ASCIR 确实只是「换名」，没有引入新的运行时表示。

预期结果：你能写出下面这张对应表（答案）：

| ASCIR 词汇 | 背后 graph_metadef 类型 | 搭在哪个核心类型上 |
|------------|------------------------|--------------------|
| `ascir::Graph` / `HintGraph` / `ImplGraph` | `af::AscGraph` | 持有 `AscGraphImpl` → `ComputeGraph` |
| `ascir::NodeView` | `af::AscNodePtr` = `shared_ptr<AscNode>` | `AscNode : public Node` |
| `ascir::NodeViewVisitorConst` | `af::AscNodeVisitor` | 遍历 `ComputeGraph` 的节点 |
| `ascir::TensorView` / `TensorAttr` | `af::AscTensor` | 视图：`OutDataAnchor` + `AscTensorAttr` |
| `ascir::TensorPtr` | `af::AscTensorAttr*` | 指向张量附加属性 |
| `ascir::SizeExpr` | `af::Expression` | 符号化大小表达式（动态 shape） |
| `ascir::SizeVar` | `af::SizeVar` | 大小变量 |

> 说明：本实践为纯源码阅读，无需运行环境。`AxisId`/`Axis`（[ascir.h:27-28](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/meta/ascir.h#L27-L28)）也属于这层别名，与 u6 的 AutoSchedule「轴调度」直接相关，可一并记录。

#### 4.3.5 小练习与答案

**练习 1**：`AscNode` 为什么用**公有继承** `Node`，而不是「持有一个 `Node*`」组合关系？这对 u4-l1 学过的拓扑操作意味着什么？

> 参考答案：公有继承意味着「ASCIR 节点就是一个核心节点」，是 is-a 关系。这样所有接受 `Node&/NodePtr` 的图算法（`AddEdge`、拓扑排序、anchor 查询等）都可以直接作用在 `AscNode` 上，无需改写。若改成组合，则每个图算法都要包一层转发，代价大。

**练习 2**：`AscTensor` 的注释写着「not owner」（不持有所有权）。它「看」的是核心图里的什么东西？如果对应端口被删掉，`AscTensor` 会怎样？

> 参考答案：`AscTensor` 看的是某个 `OutDataAnchor`（核心图的出边端口）及其 `AscTensorAttr`，二者都是引用，不归 `AscTensor` 所有。一旦对应端口/节点被图改写删除，这个 `AscTensor` 视图就变成悬空引用——所以 ASCIR 视图通常是「短期、当次遍历内有效」，不应跨次缓存。

**练习 3**：既然 `ascir::Graph` 就是 `af::AscGraph`，为什么 optimize/codegen 不直接写 `af::AscGraph`，而要绕一层 `ascir::`？

> 参考答案：为了**解耦命名与稳定接口**。`ascir::` 给了 optimize/att/codegen 一套稳定的词汇（Graph/NodeView/TensorView/SizeExpr），即便底层 `af::Asc*` 的实现演进（甚至将来换成别的后端），上层代码也不用改名；同时这层别名也让代码语义更贴近「调度/代码生成」的视角，而不是裸的图拓扑视角。

## 5. 综合实践

把本讲三块知识串起来，做一次「**追踪一路数据从张量描述到 ASCIR 视图**」的源码穿越：

1. **起点：构造一个张量描述。** 在 [tensor.cc](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/tensor.cc) 中跟踪 `TensorDesc(Shape, Format, DataType)` 构造（[L330-L333](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/tensor.cc#L330-L333)），说出它存了哪些字段。
2. **第一跳：外层 → 内层。** 经 `TensorAdapter::TensorDesc2GeTensorDesc`（[L947](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/tensor.cc#L947)）变成 `GeTensorDesc`，确认它的属性容器来自 `GeTensorDescImpl::attrs_`（[ge_tensor.cc:723](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/normal_graph/ge_tensor.cc#L723)），并指出它是 `AttrHolder` 的子类。
3. **第二跳：张量挂到节点上。** 回忆 u4-l1：`OpDesc` 持有若干 `GeTensorDesc` 作为输入输出描述；`Node` 持有 `OpDesc` 与若干 `Anchor`。所以张量描述最终通过 `OutDataAnchor` 暴露给外部。
4. **第三跳：节点进图，ASCIR 出场。** `AscGraph::AddNode`（[ascendc_ir.h:567](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascendc_ir_core/ascendc_ir.h#L567)）返回 `AscNodePtr`，`AscNode` 继承自 `Node`；遍历 `AscNode::outputs` 得到的 `AscTensor`（[ascendc_ir.h:435](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascendc_ir_core/ascendc_ir.h#L435)）正是对那条 `OutDataAnchor` 的视图。
5. **终点：ASCIR 词汇。** 上一步的 `AscGraph`/`AscNodePtr`/`AscTensor`，在 optimize/att/codegen 里以 `ascir::Graph`/`ascir::NodeView`/`ascir::TensorView`（[ascir.h:22-32](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/meta/ascir.h#L22-L32)）的名字被使用。

**交付物**：画一张纵向流程图，从「`TensorDesc` 字段」一路画到「`ascir::TensorView`」，标出三次「跳转」（外→内、张量→节点端口、核心→ASCIR 词汇）分别发生在哪一行代码。完成后，你应当能解释：**为什么 Autofuse 全链路只需要一份图数据，却能同时被 graph_metadef 接口和 ASCIR 接口访问。**

> 说明：本实践为源码阅读型，全程无需运行环境。若想加深印象，可在 autofuse framework UT 中找一个构造 `AscGraph` 并 `AddNode` 的用例对照阅读（待本地验证具体用例路径）。

## 6. 本讲小结

- graph_metadef 的张量分**两层**：外层 `Tensor/TensorDesc`（扁平成员、好用）与内层 `GeTensor/GeTensorDesc`（带属性容器、可序列化），二者经 `TensorAdapter` 互转；外层 `Tensor` 内部就持有一个内层 `GeTensor`。
- 张量三要素 **shape/dtype/format** 各有「当前值」与「origin 原始值」两份，用于支持图优化中的排布转换与还原；字节大小由 \(\text{bytes} = L(\text{dtype}) \times \prod d_i\) 决定，遇未知维度返回 `-1`。
- 内层 `GeTensor` 通过 `GeTensorSerializeUtils` 与 protobuf（`proto::TensorDef`/`TensorDescriptor`）互转，`kDataTypeMap` 负责 `DataType` 枚举的双向映射。
- **统一属性存储**：`AttrHolder` 是所有「可挂属性」类的基类，规定子类提供 `ProtoAttrMap`（= `AttrStore`）；高频字段进成员变量，低频字段进属性容器（用 `AttrUtils` 类型化读写）。
- **算子工厂**用自注册模式：一组 `*Register` 全局对象在启动期把 creator/infer_shape/infer_format/infer_value_range/verify 登记进 `OperatorFactoryImpl`，`OperatorFactory` 只做门面查询（`CreateOperator`/`IsExistOp`/`GetOpsTypeList`）。
- **ASCIR 是词汇别名层**：`ascir::Graph/NodeView/TensorView` 全部 `using` 到 `af::AscGraph/AscNodePtr/AscTensor`，而这些 `Asc*` 类型又搭在 graph_metadef 核心 `ComputeGraph/Node/Anchor` 之上（`AscNode : public Node`、`AscGraph` 持有 `AscGraphImpl` → `ComputeGraph`）。同一份图数据，两种叫法访问。

## 7. 下一步学习建议

本讲把 graph_metadef 的「张量 + 属性 + 算子工厂」和 ASCIR 的衔接关系讲清了，接下来建议：

1. **进入 u5（ASCIR 算子注册机制）**：u5-l1 会讲 ASCIR 的注册框架（`REGISTERED_OPS`、builtin ops、generator），u5-l2 会以 `reduce.cpp`/`compare.cpp` 为例精读 `reg_func`——届时你会真正用到本讲的 `OperatorCreatorRegister`/`InferShapeFuncRegister`，看到「一个算子如何被登记进工厂并被 codegen 看见」。
2. **进入 u6（Optimize 优化与调度）**：本讲确立的 `ascir::Graph/NodeView/TensorView` 正是 u6 里 `Optimizer`、`AutoSchedule`、`ScheduleTaskGenerator` 操作的对象；带着「ASCIR 视图 = 核心 graph_metadef 图 + 调度语义」的认知去读，会顺畅很多。
3. **回头巩固 u4-l1**：如果对本讲里 `OpDesc`、`Anchor`、`ComputeGraph` 的拓扑部分还不够熟，建议结合 u4-l1 再过一遍 `compute_graph.cc`/`node.cc`，把「拓扑（u4-l1）+ 数据/属性（本讲）」拼成完整的 graph_metadef IR 全景。
