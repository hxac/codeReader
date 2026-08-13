# OpDesc 算子描述:输入输出与属性

## 1. 本讲目标

在前一讲里,我们已经知道 AscendIR 用 `ComputeGraph → Node` 两层容器表达图的拓扑,而 `Node` 自己并不存储算子的"内容",它只是把"算子描述"和"锚点(连边)"组合在一起。本讲我们就打开这个"算子描述",回答三个问题:

1. **一个算子到底由什么描述?** —— `OpDesc` 承载了算子的名字、类型、输入/输出张量描述,以及一袋可任意扩展的属性。
2. **属性(Attribute)怎么存、怎么取?** —— GE 提供了两层 API:底层的 `AttrHolder::SetAttr/GetAttr`(类型擦除)和工程上更常用的 `AttrUtils::SetInt/GetInt/...`(带类型、好写)。
3. **Tensor 的元信息(shape/dtype/format)放在哪?** —— `GeTensorDesc` 是"只有元信息没有数据"的描述,`GeTensor` 才是"元信息 + 真实数据字节"的完整张量。

学完本讲,你应该能:从任意一个 `Node` 出发,读出它的算子类型、读写它的属性、并拿到它的输入/输出张量的 shape/dtype/format。

## 2. 前置知识

- **静态图对象模型**:本讲建立在 [u2-l1](u2-l1-ascendir-object-model.md) 讲过的四层模型 `ComputeGraph → Node → OpDesc → GeTensorDesc` 之上。`Node` 通过 `GetOpDesc()` 拿到它持有的 `OpDesc`。
- **Pimpl 模式**:"指针实现"惯用法。`OpDesc`、`GeTensorDesc` 等类对外只暴露一个 `impl_` 智能指针,真正的成员变量藏在 `*Impl` 类里。这样改动实现不会破坏二进制兼容,是 AscendIR 全栈统一采用的设计。
- **属性(Attribute)**:可以理解为挂在对象上的"键值对袋子"。算子类型只规定了算子"是什么",而很多编译期需要的细节(比如 `axis=1`、`keep_dims=true`、`strides=[1,2,2,1]`)都以属性形式附加在 `OpDesc` 上。
- **DataType / Format 枚举**:GE 用枚举表示数据类型和内存排布格式,例如 `DT_FLOAT=0`、`DT_INT64=9`、`FORMAT_NCHW=0`、`FORMAT_NHWC=1`、`FORMAT_ND=2`(取自 CANN 标准 `graph/types.h`,本仓 [inc/framework/executor_c/types.h:96-163](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/framework/executor_c/types.h#L96-L163) 有一份等价定义)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [inc/graph_metadef/graph/op_desc.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/op_desc.h) | `OpDesc` 类声明,是本讲的主战场:算子名/类型、输入输出描述、属性的对外接口都在这里。 |
| [inc/graph_metadef/graph/ge_tensor.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h) | `GeShape` / `GeTensorDesc` / `GeTensor` 三个类的声明,定义张量元信息与数据载体。 |
| [inc/graph_metadef/graph/detail/attributes_holder.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/detail/attributes_holder.h) | `AttrHolder` 抽象基类,属性体系的"地基"。`OpDesc` 和 `GeTensorDesc` 都继承自它。 |
| [inc/graph_metadef/graph/utils/attr_utils.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/utils/attr_utils.h) | `AttrUtils` 工具类,带类型的属性存取 API(`SetInt/GetInt/SetStr/GetStr...`),工程实践中最常用。 |
| [inc/graph_metadef/graph/node.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h) | `Node` 类,提供 `GetOpDesc()` 把"图节点"和"算子描述"衔接起来。 |

> 提示:本讲引用了 `normal_graph/op_desc.cc`(实现)的若干行号来佐证"Pimpl 委托"的行为,但**实践任务只需要读头文件**。

## 4. 核心概念与源码讲解

### 4.1 OpDesc 结构:算子的"身份证"

#### 4.1.1 概念说明

如果说 `Node` 是图上的一个"顶点",那么 `OpDesc`(Operator Description)就是这个顶点的"身份证 + 履历表"。它回答四件事:

- **我是谁**:`name`(节点实例名,图内唯一)和 `type`(算子类型,如 `"Add"`、`"Conv2D"`)。
- **我吃进什么**:`inputs`,一组 `GeTensorDesc`,描述每个输入张量的元信息。
- **我吐出什么**:`outputs`,同样是一组 `GeTensorDesc`。
- **我有哪些可调参数**:`attributes`,一袋键值对,承载算子特有的编译信息。

需要特别强调:**`OpDesc` 只描述算子的"规格",不包含算子的计算实现**。算子的实际语义(kernel、tiling、shape 推导函数)位于 GE 仓之外的**独立算子仓**,GE 在编译时通过注册表按 `type` 查询它们(详见 [u2-l4](u2-l4-op-registry.md))。这一点和 [u1-l2](u1-l2-directory-structure.md) 里"GE 与算子仓解耦"的结论是一致的。

#### 4.1.2 核心流程

`OpDesc` 在源码层面有三个关键设计点:

1. **继承 `AttrHolder`**:`OpDesc` 通过继承获得"挂属性"的能力,属性体系的实现完全由基类统一处理。
2. **Pimpl 隔离**:对外只有一个 `OpDescImplPtr impl_`,真正的成员(name/type/inputs/outputs)藏在 `OpDescImpl` 里,所有 getter 都委托给 `impl_`。
3. **构造即定型**:`OpDesc(name, type)` 构造时就确定了算子的名字和类型;输入输出描述则通过 `AddInputDesc/AddOutputDesc` 逐个追加。

一个 `OpDesc` 的"一生"大致如下:

```
OpDesc("add_node1", "Add")        // 1. 构造:定 name + type
   .AddInputDesc(x_desc)           // 2. 追加输入 0
   .AddInputDesc(y_desc)           //    追加输入 1
   .AddOutputDesc(z_desc)          // 3. 追加输出 0
SetAttr("axis", ...)               // 4. 挂属性(编译期随时可加)
                                   // 5. 被 Node 持有,进入图中参与编译
```

#### 4.1.3 源码精读

**① OpDesc 的继承关系与 Pimpl 成员。**
[inc/graph_metadef/graph/op_desc.h:35](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/op_desc.h#L35) 声明了 `OpDesc` 同时继承 `std::enable_shared_from_this<OpDesc>`(以便安全地在成员函数里获取自身的 `shared_ptr`)和 `AttrHolder`(获得属性能力):

```cpp
class OpDesc : public std::enable_shared_from_this<OpDesc>, public AttrHolder {
```

唯一的私有数据成员是 [inc/graph_metadef/graph/op_desc.h:331](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/op_desc.h#L331):

```cpp
OpDescImplPtr impl_;   // Pimpl:真正的内容在 OpDescImpl 里
```

构造函数把 name/type 透传给 `OpDescImpl`,见 [graph_metadef/graph/normal_graph/op_desc.cc:1471-L1472](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/op_desc.cc#L1471-L1472):

```cpp
OpDesc::OpDesc(const std::string &name, const std::string &type)
    : enable_shared_from_this(), AttrHolder(), impl_(ComGraphMakeSharedAndThrow<OpDescImpl>(name, type)) {}
```

**② 名字与类型的读写。** [op_desc.h:63-L73](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/op_desc.h#L63-L73) 定义了 `GetName/SetName/GetType/SetType`。以 `GetType` 为例,实现就是把活儿全转给 `impl_`(见 [op_desc.cc:1502-L1504](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/op_desc.cc#L1502-L1504)):

```cpp
std::string OpDesc::GetType() const { return impl_->GetType(); }
```

> 注意:`GetName()` 返回的是**节点实例名**(图内唯一标识,如 `"add_node1"`),`GetType()` 返回的是**算子类型**(如 `"Add"`,可重复)。两者不要混淆。

**③ 输入/输出描述的追加与查询。** 输入侧的核心接口在 [op_desc.h:77-L107](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/op_desc.h#L77-L107),输出侧在 [op_desc.h:125-L147](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/op_desc.h#L125-L147)。常用方法见下表:

| 方法 | 作用 |
| --- | --- |
| `AddInputDesc(desc)` | 按下标顺序追加一个输入描述 |
| `UpdateInputDesc(index, desc)` | 更新指定下标的输入描述(shape 推导后回写常用) |
| `GetInputDesc(index)` | 返回输入描述的 const 引用(只读) |
| `MutableInputDesc(index)` | 返回可写引用的智能指针(需要改 shape 时用) |
| `GetInputsSize()` | 输入个数 |
| `GetOutputDesc / MutableOutputDesc / GetOutputsSize` | 输出侧的对应方法 |

`GetInputDesc(index)` 实现同样委托给 `impl_`,见 [op_desc.cc:1577-L1579](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/op_desc.cc#L1577-L1579)。

**④ `OpDescBuilder`:链式构造算子。** 头文件还提供了一个 Builder([op_desc.h:347-L431](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/op_desc.h#L347-L431)),可以链式地把输入输出拼起来,最后 `Build()` 出一个 `OpDescPtr`,比逐个 `AddInputDesc` 更紧凑,在测试和示例代码里很常见。

#### 4.1.4 代码实践

> **实践目标**:在不运行编译的前提下,通过阅读头文件,从 `Node` 一路拿到 `OpDesc` 的类型与输入描述,验证"`Node` 持有 `OpDesc`"这一关系。

**操作步骤**(纯源码阅读):

1. 打开 [inc/graph_metadef/graph/node.h:192-L193](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h#L192-L193),确认 `Node` 提供了 `GetOpDesc()`(返回智能指针)和 `GetOpDescBarePtr()`(返回裸指针,只读场景更轻)。
2. 打开 [op_desc.h:70](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/op_desc.h#L70) 和 [op_desc.h:97](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/op_desc.h#L97),找到 `GetType()` 与 `GetInputDesc(index)`。
3. 据此写出下面的伪代码(示例代码,非项目原有):

```cpp
// 给定一个 NodePtr node,读取它的算子类型与第 0 个输入的 dtype
OpDesc *op_desc = node->GetOpDescBarePtr();   // 只读,用裸指针版本
std::string op_type = op_desc->GetType();      // 如 "Add"
const GeTensorDesc &in0 = op_desc->GetInputDesc(0);
DataType dt = in0.GetDataType();               // 如 DT_FLOAT
```

**需要观察的现象**:你会看到几乎所有 `OpDesc` 公有方法都是单行委托(返回 `impl_->XXX()`),这正是 Pimpl 模式的典型痕迹。

**预期结果**:能够清晰说出 `Node → OpDesc → (name/type/inputs/outputs)` 的包含关系,并理解"读算子信息要从 `GetOpDesc()` 开始"。

#### 4.1.5 小练习与答案

**练习 1**:`OpDesc` 为什么不直接把 name、type、inputs 写成公有成员,而要塞进 `impl_`?

> **参考答案**:为了二进制兼容与封装。采用 Pimpl 后,修改 `OpDescImpl` 的成员布局不会改变 `OpDesc` 本身的内存结构,对外头文件保持稳定;同时所有读写都收敛到受控的 getter/setter,便于加校验和日志。

**练习 2**:`GetName()` 和 `GetType()` 有何区别?

> **参考答案**:`GetName()` 是节点实例名,在同一张图里唯一,用于区分"两个 Add 节点";`GetType()` 是算子类型,可重复,用于到注册表里查算子定义。两者一对:图内定位靠 name,语义查询靠 type。

---

### 4.2 属性(Attribute)存取:AttrHolder 与 AttrUtils 两层 API

#### 4.2.1 概念说明

算子的"规格"(输入几个、输出几个、什么类型)由算子原型规定,是相对固定的;但每个算子实例还带着一堆"可调旋钮"——卷积的 `strides`、Reduce 的 `axis`、是否 `keep_dims` 等等。这些旋钮在 GE 里统一叫**属性(Attribute)**。

属性体系的核心是基类 `AttrHolder`,它提供"挂一个键值对"的能力。`OpDesc` 继承自 `AttrHolder`,所以每个算子实例都自带一个属性袋。同一个 `AttrHolder` 基类也被 `GeTensorDesc`、`ComputeGraph` 等继承——这意味着**属性是 AscendIR 里通用的扩展机制**,这也是上一讲提到的"静态图 + 属性扩展"原则的落点。

GE 给属性准备了**两种**用法,理解它们的分工是本模块的关键:

- **底层 `AttrHolder::SetAttr/GetAttr`**:操作类型擦除的 `AnyValue`,通用但写起来啰嗦(要手动构造/解析 `AnyValue`)。
- **高层 `AttrUtils::SetInt/GetInt/SetStr/GetStr/...`**:带类型的便捷函数,内部帮你完成 `AnyValue` 与具体 C++ 类型之间的转换。**工程实践中几乎都用这一层。**

此外还有第三类:`SetExtAttr/TryGetExtAttr`,它是**只在内存里存活、不参与序列化**的"扩展属性",用于在编译过程中临时标注一些 Host 侧信息(比如已经算过的中间结果),不会进入最终的 OM。

#### 4.2.2 核心流程

属性在 `AttrHolder` 内部被分成两个存储区:

```
              AttrHolder
        ┌──────────┴──────────┐
   ProtoAttrMap              ext_attrs_ (AnyMap)
  (MutableAttrMap/            (SetExtAttr/TryGetExtAttr)
   GetAttrMap)                 └─ 仅内存,不序列化
   └─ 会被序列化进 IR/OM
      (SetAttr/GetAttr/AttrUtils::*)
```

读写流程:

1. **写**:`AttrUtils::SetInt(op_desc, "axis", 1)` → 适配器把 `op_desc` 转成 `AttrHolder*` → 调 `SetAttr("axis", AnyValue(1))` → 落入 `MutableAttrMap()` 返回的 `AttrStore`。
2. **读**:`AttrUtils::GetInt(op_desc, "axis", val)` → 从 `GetAttrMap()` 取出 `AnyValue` → 反序列化回 `int64_t` 写入 `val`,返回 bool 表示是否存在且类型匹配。
3. **判存在**:`HasAttr("axis")` 不关心类型,只问"有没有这个键"。
4. **删**:`DelAttr("axis")` 移除该键。

关键约定:**`AttrUtils` 的 Get 系列方法返回 `bool`**,失败(属性不存在或类型不符)时返回 `false` 且不改动输出参数。因此调用时**必须判断返回值**,不要假设一定能取到。这正是真实代码里的标准写法。

#### 4.2.3 源码精读

**① `OpDesc` 把属性接口"请"进自己的作用域。**
[op_desc.h:230-L236](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/op_desc.h#L230-L236) 用一组 `using` 把 `AttrHolder` 的属性方法暴露出来:

```cpp
using AttrHolder::AddRequiredAttr;
using AttrHolder::DelAttr;
using AttrHolder::GetAllAttrNames;
using AttrHolder::GetAllAttrs;
using AttrHolder::GetAttr;
using AttrHolder::HasAttr;
using AttrHolder::SetAttr;
```

**② `AttrHolder` 的底层接口。** [attributes_holder.h:141-L157](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/detail/attributes_holder.h#L141-L157) 定义了 `SetAttr/TrySetAttr/GetAttr/HasAttr/DelAttr`。注意它们操作的是 `AnyValue`(类型擦除):

```cpp
graphStatus SetAttr(const std::string &name, const AnyValue &value);
graphStatus TrySetAttr(const std::string &name, const AnyValue &value);  // 已存在则不覆盖
graphStatus GetAttr(const std::string &name, AnyValue &value) const;
bool HasAttr(const std::string &name) const;
graphStatus DelAttr(const std::string &name);
```

而真正决定"存到哪"的是两个纯虚函数 [attributes_holder.h:248-L249](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/detail/attributes_holder.h#L248-L249),`OpDesc` 在 [op_desc.h:323-L324](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/op_desc.h#L323-L324) 重写了它们,返回 `impl_` 里那块会被序列化的属性表。

**③ 不序列化的扩展属性。** [attributes_holder.h:175-L191](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/detail/attributes_holder.h#L175-L191) 的 `SetExtAttr/TryGetExtAttr` 是模板方法,直接写入成员 `AnyMap ext_attrs_;`([attributes_holder.h:259](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/detail/attributes_holder.h#L259)),与序列化属性表隔离。

**④ 工程上最常用的 `AttrUtils`。** [attr_utils.h:28-L58](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/utils/attr_utils.h#L28-L58) 是一堆带类型的 Set,对应 [attr_utils.h:61-L90](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/utils/attr_utils.h#L61-L90) 的 Get,覆盖 `Int/Float/Bool/Str/TensorDesc/Tensor/Graph/Bytes/...` 以及它们的 List 版本。它的"魔法"在于 [attr_utils.h:128-L154](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/utils/attr_utils.h#L128-L154) 的 `AttrHolderAdapter`:既能从 `AttrHolder*` 构造,也能从 `shared_ptr<T>`(如 `OpDescPtr`)隐式构造,所以你可以直接把 `op_desc` 当第一个参数传进去。

**⑤ 真实代码里的标准写法。** 看 GE 运行时如何读属性:[runtime/v1/hybrid/model/node_item.cc:91](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/runtime/v1/hybrid/model/node_item.cc#L91)

```cpp
if (!AttrUtils::GetInt(op_desc, ATTR_NAME_PARENT_NODE_INDEX, parent_index)) { ... }
```

注意三件事:第一参数直接传 `op_desc`(`OpDescPtr`),返回值被 `if` 判断,取不到就走分支处理——这就是属性读取的安全范式。

#### 4.2.4 代码实践

> **实践目标**:掌握"用 `AttrUtils` 读写一个 int 属性"的标准写法,并理解返回值必须判断。

**操作步骤**(源码阅读 + 伪代码):

1. 在 [attr_utils.h:61-L67](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/utils/attr_utils.h#L61-L67) 找到 `GetInt` 的几个重载,确认第二个参数是属性名、第三个是输出引用、返回 `bool`。
2. 在 [node_item.cc:91](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/runtime/v1/hybrid/model/node_item.cc#L91) 观察真实调用如何判断返回值。
3. 写出"读一个 Node 的类型 + 一个名为 `axis` 的 int 属性"的伪代码(示例代码):

```cpp
OpDesc *op_desc = node->GetOpDescBarePtr();
std::string op_type = op_desc->GetType();        // 读类型

int64_t axis = 0;
if (!AttrUtils::GetInt(op_desc, "axis", axis)) { // 必须判断返回值
  // 属性不存在或类型不是 int,做兜底处理
}
```

**需要观察的现象**:`AttrUtils::GetInt` 的第一参数位置,真实代码既有传 `op_desc`(智能指针)也有传裸 `OpDesc*`,两种都能编译——体会 `AttrHolderAdapter` 的隐式构造在起作用。

**预期结果**:能区分 `SetAttr/GetAttr`(底层,`AnyValue`)与 `AttrUtils::SetInt/GetInt`(高层,带类型)两套 API,并知道**生产代码统一用 `AttrUtils` 且必判返回值**。

#### 4.2.5 小练习与答案

**练习 1**:`AttrUtils::GetInt(op_desc, "axis", val)` 返回 `false` 可能是哪两种原因?

> **参考答案**:(1) `op_desc` 上根本没有名为 `"axis"` 的属性;(2) 属性存在,但它存的类型不是整型(比如存的是 `string`),类型不匹配导致反序列化失败。两种情况都应被当作"取不到"处理。

**练习 2**:`SetExtAttr` 存的属性和 `AttrUtils::SetInt` 存的属性,最关键的差异是什么?

> **参考答案**:`AttrUtils::SetInt` 写入的是 `MutableAttrMap()`(ProtoAttrMap),**会随图一起序列化**进 IR/OM,在设备侧仍然可见;`SetExtAttr` 写入的是 `ext_attrs_`(AnyMap),**只在 Host 内存里存活、不参与序列化**,适合存编译过程中的临时标注。

**练习 3**:为什么 `OpDesc` 要用 `using AttrHolder::SetAttr;` 把基类方法"再声明"一遍?

> **参考答案**:因为 `AttrHolder` 还有同名的模板方法 `SetExtAttr` 等,基类方法在派生类里可能被名字隐藏;用 `using` 显式引入基类名字,既消除歧义,也让属性 API 出现在 `OpDesc` 的公有文档里,方便使用者发现。

---

### 4.3 TensorDesc 元信息:Shape/DataType/Format

#### 4.3.1 概念说明

`OpDesc` 的输入输出都是 `GeTensorDesc`。这里的"Desc"很关键:**它只是张量的"元信息描述",不带任何数据字节**。把 shape、dtype、format 这些"长什么样"的信息单独抽出来,是因为图编译期(做 shape 推导、内存规划、算子选择时)只需要元信息,还拿不到运行时的真实数据。

`GeTensorDesc` 主要承载三类元信息(每类都区分"当前值"和"原始值"):

- **Shape**(`GeShape`):形状,如 `[2,3]`。
- **DataType**:元素类型,如 `DT_FLOAT`。
- **Format**:内存排布格式,如 `FORMAT_NCHW`、`FORMAT_ND`。

此外还有"原始(origin)"版本:`GetOriginShape/GetOriginDataType/GetOriginFormat`。它的存在是为了**追踪用户最初指定的信息**:编译过程中 shape/format 可能被多次改写(插入转算子、重排布),但"用户当初给的是什么"需要留底,便于回溯和精度对齐。

> **`GeTensorDesc` vs `GeTensor`**:`GeTensorDesc` = 元信息(无数据);`GeTensor` = `GeTensorDesc` + `TensorData`(真实数据字节)。`OpDesc` 的输入输出是 `GeTensorDesc`(编译期只要元信息);而权重常量、DataDump 落盘数据这些"真有数据"的场景才用 `GeTensor`。这个区分是本模块要建立的核心认知。

#### 4.3.2 核心流程

`GeTensorDesc` 的构造与读写流程:

```
GeShape shape({2, 3});                      // 1. 先建一个形状
GeTensorDesc desc(shape, FORMAT_ND, DT_FLOAT); // 2. 形状 + 格式 + 类型
desc.SetOriginShape(shape);                 // 3. (可选)记录原始形状
// 编译期:
desc.MutableShape().SetDim(0, 4);           // 4. 改写当前 shape(原始 shape 不变)
auto dt = desc.GetDataType();               // 5. 读元信息
```

动态 shape 场景下,维度可能未知,GE 用两种特殊值表达:

- **维度值 `< 0`** 表示"该维度大小未知",`GeShape::IsUnknownShape()` 据此判断([ge_tensor.h:81](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h#L81))。
- **`-2`** 表示"连维度个数(秩)都未知",`SetUnknownDimNumShape()` 用来设置它,`IsUnknownDimNum()` 用来判断。
- 对动态维度,还可以用 `SetShapeRange/GetShapeRange` 给出每个维度的取值范围 `[min, max]`,这是动态 shape 编译(见 [u5-l2](u5-l2-infer-shape.md)、[u5-l4](u5-l4-dynamic-gear.md))的关键输入。

#### 4.3.3 源码精读

**① `GeShape`:形状的载体。** [ge_tensor.h:41-L113](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h#L41-L113) 定义了 `GeShape`。注意头文件注释专门提醒的两个"不等价"接口 [ge_tensor.h:47-L69](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h#L47-L69):

```cpp
size_t GetDimNum() const;            // "有效"维度个数;dim 为 [-2] 时返回 0
std::vector<int64_t> GetDims() const; // dim 列表的长度;dim 为 [-2] 时返回 1
```

也就是说,当形状是 `[-2]`(秩未知)时,`GetDimNum()==0` 而 `GetDims().size()==1`,二者不等价;判断标量应优先用 `IsScalar()`([ge_tensor.h:89](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h#L89))。其他常用方法:`GetDim(idx)` 取某一维、`GetShapeSize()` 算元素总数、`IsUnknownShape()`/`IsEmptyTensor()` 做形态判断。

**② `GeTensorDesc`:元信息描述,本身也是 `AttrHolder`。** 它同样继承 `AttrHolder`([ge_tensor.h:115](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h#L115)),构造函数 [ge_tensor.h:121](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h#L121) 接收 shape + format + dtype:

```cpp
explicit GeTensorDesc(const GeShape &shape, const Format format = FORMAT_ND, const DataType dt = DT_FLOAT);
```

shape 的读写见 [ge_tensor.h:130-L133](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h#L130-L133);动态 shape 的 range 接口见 [ge_tensor.h:138-L143](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h#L138-L143);"原始"信息(origin)见 [ge_tensor.h:145-L167](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h#L145-L167):

| 类别 | 当前值 | 原始值(origin) |
| --- | --- | --- |
| 形状 | `GetShape / MutableShape / SetShape` | `GetOriginShape / SetOriginShape` |
| 类型 | `GetDataType / SetDataType` | `GetOriginDataType / SetOriginDataType` |
| 格式 | `GetFormat / SetFormat` | `GetOriginFormat / SetOriginFormat` |

**③ `GeTensor`:元信息 + 数据。** [ge_tensor.h:263-L326](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h#L263-L326) 是完整的张量。它内部含一个 `GeTensorDesc`(通过 [ge_tensor.h:275-L277](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h#L275-L277) 的 `GetTensorDesc/MutableTensorDesc` 访问)和一个 `TensorData`(真实数据字节,通过 `GetData/MutableData` 访问)。换句话说:

```cpp
GeTensor = GeTensorDesc (元信息) + TensorData (数据字节)
```

而数据本身的载体 `TensorData`([ge_tensor.h:214-L261](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h#L214-L261))支持多种 `SetData` 重载,包括零拷贝版本(直接持有外部 `AlignedPtr`),这与后续 [u7-l4](u7-l4-zero-copy-variable.md) 讲的零拷贝机制呼应。

#### 4.3.4 代码实践

> **实践目标**:从 `OpDesc` 取出某个输入的 `GeTensorDesc`,读出它的 shape / dtype / format,并区分"当前 shape"与"原始 shape"。

**操作步骤**(源码阅读 + 伪代码):

1. 在 [op_desc.h:97](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/op_desc.h#L97) 找到 `GetInputDesc(index)`,在 [ge_tensor.h:130](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h#L130) 找到 `GetShape()`。
2. 写出伪代码(示例代码):

```cpp
const GeTensorDesc &in0 = op_desc->GetInputDesc(0);
const GeShape &shape = in0.GetShape();          // 当前 shape,如 [2,3]
const GeShape &origin = in0.GetOriginShape();   // 原始 shape(用户最初给的)
DataType dt = in0.GetDataType();                // 如 DT_FLOAT
Format fmt = in0.GetFormat();                   // 如 FORMAT_NCHW
bool unknown = shape.IsUnknownShape();          // 是否含未知维度(<0)
```

**需要观察的现象**:`GetShape()` 与 `GetOriginShape()` 可能不同——这正是编译期 shape 被改写(如分档、重排布)后"当前值变了、原始值留底"的体现。

**预期结果**:能说清 `OpDesc` 持有的是 `GeTensorDesc`(只有元信息),而真正带数据的 `GeTensor` 用于权重/常量等场景;能正确读出 shape/dtype/format 三个元信息。

#### 4.3.5 小练习与答案

**练习 1**:为什么 `OpDesc` 的输入输出用 `GeTensorDesc` 而不是 `GeTensor`?

> **参考答案**:因为图编译期只需要张量的"元信息"(shape/dtype/format)来做 shape 推导、内存规划和算子选择,此时还没有运行时数据。`GeTensor` 额外携带数据字节,既无必要又会带来巨大拷贝开销。只有权重常量、DataDump 数据等"真有数据"的场景才用 `GeTensor`。

**练习 2**:某 `GeShape` 的 `GetDims()` 返回 `{-1, 3}`,`GetDimNum()` 返回多少?`IsUnknownShape()` 返回什么?

> **参考答案**:`GetDimNum()` 返回 `2`(两个有效维度,虽然第一个未知但只要不是 `[-2]` 这种"秩未知"就计入);`IsUnknownShape()` 返回 `true`(存在值为 `-1 < 0` 的维度)。注意:只有形状形如 `[-2]`(秩未知)时 `GetDimNum()` 才会返回 `0`,本例不是这种情况。

**练习 3**:`GetShape()` 和 `GetOriginShape()` 分别在什么场景下会不一致?

> **参考答案**:当编译过程改写了 shape 时两者就不一致。例如动态分档(`multi_batch_copy_graph`)把 `-1` 维度拷贝成具体档位值、或插入转排布算子后 shape 被重排,此时 `GetShape()` 是编译后的当前 shape,而 `GetOriginShape()` 保留用户模型里最初写的 shape,供精度对齐和回溯使用。

---

## 5. 综合实践

把本讲三个模块串起来,完成一个"算子信息速查表"任务。

**任务**:给定图中的一个 `NodePtr node`,写一段伪代码,生成它的"算子信息卡片",要求包含:

1. 算子实例名 `name` 与类型 `type`;
2. 输入个数与每个输入的 `(shape, dtype, format)`,标注当前 shape 与 origin shape 是否一致;
3. 读取一个名为 `"axis"` 的 `int` 属性(若不存在则标注"无此属性");
4. 判断该算子是否处于动态 shape(任一输入含未知维度)。

**参考实现**(示例代码,综合本讲所有要点):

```cpp
void PrintOpCard(const NodePtr &node) {
  OpDesc *op = node->GetOpDescBarePtr();

  // (1) 名字与类型 —— 模块 4.1
  std::cout << "name=" << op->GetName() << " type=" << op->GetType() << "\n";

  // (2) 输入元信息 —— 模块 4.3
  for (size_t i = 0; i < op->GetInputsSize(); ++i) {
    const GeTensorDesc &desc = op->GetInputDesc(i);
    const GeShape &s = desc.GetShape();
    const GeShape &os = desc.GetOriginShape();
    std::cout << "  in[" << i << "] shape=" << s.ToString()
              << " dtype=" << desc.GetDataType()
              << " format=" << desc.GetFormat()
              << " origin_eq=" << (s.ToString() == os.ToString()) << "\n";
  }

  // (3) 读属性 —— 模块 4.2(必判返回值)
  int64_t axis = 0;
  if (AttrUtils::GetInt(op, "axis", axis)) {
    std::cout << "  attr axis=" << axis << "\n";
  } else {
    std::cout << "  attr axis=<无此属性或类型不符>\n";
  }

  // (4) 动态 shape 判定 —— 模块 4.3
  bool dyn = false;
  for (size_t i = 0; i < op->GetInputsSize(); ++i) {
    if (op->GetInputDesc(i).GetShape().IsUnknownShape()) { dyn = true; break; }
  }
  std::cout << "  dynamic_shape=" << dyn << "\n";
}
```

> 说明:本实践为**源码阅读型 + 伪代码型**,无需编译运行。若你想在真实环境验证,可在 `tests/` 下找一个构造 `OpDesc` 的 UT 用例作为模板(参考 [u9-l5](u9-l5-testing-and-contribution.md) 介绍的 UT 体系),把上面的逻辑改写成一个可运行的测试。预期输出是一张清晰的算子信息卡片,体现 `Node → OpDesc → (type / inputs / attrs)` 的完整链路。

## 6. 本讲小结

- **`OpDesc` 是算子的"身份证"**:承载 `name`(实例名)、`type`(算子类型)、输入输出 `GeTensorDesc` 列表;采用 Pimpl(`impl_`),所有 getter 委托给 `OpDescImpl`;它只描述规格,不含计算实现(实现在外部算子仓)。
- **`Node` 通过 `GetOpDesc()` 持有 `OpDesc`**:读算子信息的入口永远是 `node->GetOpDesc()`(或裸指针版 `GetOpDescBarePtr()`)。
- **属性有两层 API**:底层 `AttrHolder::SetAttr/GetAttr` 操作类型擦除的 `AnyValue`;工程上统一用 `AttrUtils::SetInt/GetInt/SetStr/GetStr/...` 带类型 API,且**Get 系列必判返回值**。
- **属性分两种存储**:经 `SetAttr/AttrUtils` 写入的会被序列化进 IR/OM;经 `SetExtAttr` 写入的只在内存存活、不序列化。
- **`GeTensorDesc` 是"只有元信息"的张量描述**,核心是 shape(`GeShape`)/dtype/format,且每类都有 origin 版本留底;动态 shape 用 `<0` 维度值与 `SetShapeRange` 表达。
- **`GeTensor = GeTensorDesc + TensorData`**:`OpDesc` 用前者(编译期只要元信息),权重/常量/落盘数据等"真有数据"的场景才用后者。

## 7. 下一步学习建议

- **下一讲 [u2-l4 算子注册与原型体系](u2-l4-op-registry.md)**:本讲反复提到"算子类型 `type` 用于到注册表查算子定义"。下一讲将正式讲解 `OpRegistry` 与 `OpProto` 如何把算子的类型、输入输出、属性登记进系统,以及 GE 仓与算子仓的协作边界。
- **进阶阅读方向**:
  - 想看 shape 如何在图上传播推导 → [u5-l2 Shape 推导与符号化](u5-l2-infer-shape.md)。
  - 想看 `GeTensor` 的零拷贝如何用于变量管理 → [u7-l4 零拷贝与变量管理](u7-l4-zero-copy-variable.md)。
  - 想亲手构造 `OpDesc`/`GeTensorDesc` 写测试 → 参考 `tests/` 下既有用例,并在 [u9-l5 测试体系](u9-l5-testing-and-contribution.md) 里学习 UT 开发规范。
