# ASCIR 算子注册框架

## 1. 本讲目标

ASCIR 是 Autofuse 内部用来描述「融合子图里每一个算子」的中间表示（上一讲 u4-l2 已经讲过，ASCIR 只是搭在 `ComputeGraph/Node/Anchor` 之上的带调度语义视图）。但一张图里的节点要能被**调度、做 tiling、最终生成 AscendC 代码**，光有「图节点」是不够的——系统还得知道这个算子叫什么、有几个输入输出、支持哪些数据类型、用哪段 C++ 代码去生成调用。这些「算子身份证信息」从哪里来？答案是：**在程序启动时，由一整套注册框架把它们登记进一张全局表里**。

本讲就来拆解这套「ASCIR 算子注册框架」。学完后你应当能够：

- 说清一个算子是用哪个宏、经过哪些步骤被登记进 Autofuse 的；
- 区分两个容易混淆的概念：`REG_ASC_IR`（C++ 侧完整的算子 IR 注册）与 `REGISTERED_OPS`（Python 绑定侧的算子名清单），并理解它们为什么必须保持一致；
- 解释「generator（代码生成）」这个词在注册语境下的含义，以及一个算子「从注册到被 codegen 可见」要经过哪些环节。

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- **Autofuse 的端到端数据流**（u3-l2）：`graph_metadef → ascir → optimize → att → codegen → compiler`。本讲聚焦其中 `ascir` 这一段的「算子是怎么进系统的」。
- **ASCIR 与 graph_metadef 的关系**（u4-l2）：ASCIR 不是一张独立的图，而是对同一份 `ComputeGraph` 的带语义视图（`ascir::Graph` 就是 `af::AscGraph` 的别名）。本讲讲的「注册」，注册的不是图节点实例，而是**算子类型（type）的元数据定义**。
- 一点点 C++ 知识：**静态对象初始化**（`static` 全局/命名空间作用域对象的构造函数会在 `main` 之前执行）和**链式调用（fluent builder）**（方法返回 `*this` 的引用，从而可以 `.A().B().C()` 连写）。这两个机制是整个注册框架的基石，下面会详细用到。

> 一句话定位：上一讲讲的是「图里有哪几类节点」，本讲讲的是「每一类节点的说明书是谁写、写在哪、怎么被查到」。

## 3. 本讲源码地图

本讲涉及的关键文件按职责分成三层：

| 文件 | 作用 |
|------|------|
| [ascir.h:21-52](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/meta/ascir.h#L21-L52) | ASCIR 的「词汇别名层」：把 `af::AscGraph`、`af::AscNodePtr` 等类型 `using` 成 `ascir::Graph`、`ascir::NodeView`，统一命名空间。 |
| [ascir_register.h:25-94](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascir_register.h#L25-L94) | **注册入口**：定义 `AscirRegister` 流式构造器和 `REG_ASC_IR`、`EXPORT_GENERATOR` 等宏。 |
| [ascir_register.cc:15-50](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/ascendc_ir/generator/ascir_register.cc#L15-L50) | `AscirRegister` 的实现，其中**拷贝构造函数**是真正把算子写进全局表的「提交点」。 |
| [ascir_registry.h:115-284](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascir_registry.h#L115-L284) | **数据结构层**：定义 `AscIrCodegen`/`AscIrAtt`（两个抽象接口）、`AscIrImpl`（实现三元组）、`AscIrDef`（一个算子的完整定义）、`AscirRegistry`（全局单例表）。 |
| [ascir_registry.cc:262-285](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/ascendc_ir/generator/ascir_registry.cc#L262-L285) | 全局表的实现：`RegisterAscIr`（写入）、`GetIrAttImpl`/`GetIrCodegenImpl`（按平台+类型查询）。 |
| [ascir_builtin_ops_v1.cpp:42-421](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/generator/ascir_builtin_ops_v1.cpp#L42-L421) | **builtin ops 注册表（v1 平台）**：用 `REG_ASC_IR` 逐个登记 v1（2201）平台的全部内置算子。 |
| [generator.cc:1806-1824](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/ascendc_ir/generator/generator.cc#L1806-L1824) | **generator 消费侧**：遍历全局表 `GetAll()`，为每个已注册算子生成 IR 代码。 |
| [common_utils.cpp:541-553](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/common/common_utils.cpp#L541-L553) | **查询侧**：`GetAscIrAttImpl`/`GetAscIrCodegenImpl`，按平台与算子类型取出实现，供 att/codegen 使用。 |
| [pyascir.h:31-44](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/py_module/pyascir.h#L31-L44) | **Python 侧算子名清单 `REGISTERED_OPS`**：用于生成 Python 绑定，与 builtin ops 并行存在。 |
| [v1_ascir_codegen_impl.h:23-36](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/generator/v1_ascir_codegen_impl.h#L23-L36) | 一个具体的 codegen 实现类示例（`DataAscIrCodegenImpl`）。 |

阅读建议：先看「4.1 注册宏」建立直觉，再看「4.2 builtin ops」看真实算子长什么样，最后看「4.3 generator」理解注册信息如何被下游消费。

## 4. 核心概念与源码讲解

### 4.1 注册宏与算子列表：REG_ASC_IR 与 AscirRegister

#### 4.1.1 概念说明

先说清楚一个核心问题：**Autofuse 怎么知道 `Add` 这个算子长什么样？**

答案不是硬编码在某个 `switch(type)` 里，而是采用「**自注册（self-registration）**」模式：每个算子在自己所在的 `.cpp` 文件里，用一个宏声明「我存在，这是我的说明书」，程序一启动，这些说明书就自动汇总进一张全局表。

这带来三个好处：

1. **可扩展**：新增一个算子，只要在新文件里加一段 `REG_ASC_IR(Xxx)...` 即可，不用改动任何中心化的调度代码。
2. **解耦**：算子的「定义」（有几输入输出、支持哪些 dtype）和它的「实现」（怎么生成代码）被分开登记。
3. **平台可组合**：不同芯片平台（2201、3510 等）的算子实现可以分别登记，再合并到同一张表里（见 4.2）。

这套机制的核心就是宏 `REG_ASC_IR` 和流式构造器 `AscirRegister`。

#### 4.1.2 核心流程

一个算子从「写一行宏」到「进入全局表」的过程：

```text
1. 源码里写：REG_ASC_IR(Add).Input("x1","T").Input("x2","T").Output("y","T")
                                  .ComputeType(kComputeElewise).Impl(...);
2. 宏展开成：static auto g_register_Add =
              af::ascir::AscirRegister("Add", __FILE__, __LINE__)
                .Input("x1","T")... .Impl(...);
3. 程序启动（main 之前），静态对象 g_register_Add 初始化：
   a. 先构造一个临时 AscirRegister 对象，构造函数记下 type="Add"、文件、行号；
   b. 链式调用 .Input()/.Output()/.Impl() 把元数据填进内部 ir_def_；
   c. 用这个临时对象【拷贝构造】静态对象 g_register_Add ——
      拷贝构造函数里调用 RegisterAscIr("Add", ir_def_)，真正写入全局表。
4. 至此，全局表 AscirRegistry 里多了一条 "Add" → AscIrDef。
```

第 3 步的「拷贝构造 = 提交点」是这套设计最精妙的地方，下面源码精读会专门讲。

#### 4.1.3 源码精读

**① 宏定义**：`REG_ASC_IR` 把一个算子名变成一个静态对象。

[ascir_register.h:84](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascir_register.h#L84) 定义了它：

```cpp
#define REG_ASC_IR(type) static auto g_register_##type = af::ascir::AscirRegister(#type, __FILE__, __LINE__)
```

`##type` 把算子名拼进变量名（`g_register_Add`），`#type` 把算子名变成字符串（`"Add"`），`__FILE__/__LINE__` 记下登记位置——这两个信息后面 generator 会用来给生成的代码加注释（见 4.3）。

注意这个宏只产生「构造 + 文件行号」，真正的输入输出等元数据是靠后面的链式调用追加的。

**② 流式构造器 AscirRegister**：一个返回 `*this` 引用的 builder。

[ascir_register.h:25-82](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascir_register.h#L25-L82) 列出了它的全部方法。每一个都返回 `AscirRegister&`，所以可以连写。关键方法分类：

| 方法 | 作用 |
|------|------|
| `Input(name, sym)` / `Inputs({...})` / `DynamicInput(...)` | 声明输入（`sym` 是数据类型符号，如 `"T"`，用于约束 dtype） |
| `Output(name, sym)` / `DynamicOutput(...)` | 声明输出 |
| `Attr<T>(name)` | 声明一个属性（如 `Attr<float>("negative_slope")`） |
| `ComputeType(...)` | 算子计算类别（`kComputeElewise`/`kComputeReduce`/`kComputeCube` 等） |
| `StartNode()` | 标记为图的起始节点（如 `Data`/`Scalar`） |
| `Impl(soc_versions, {...})` | **登记实现**（见 4.2） |
| `UseFirstInputDataType()` | 输出 dtype 跟随第一个输入（一种 dtype 推导策略） |

**③ 构造函数**：只记下身份信息，不改全局表。

[ascir_register.cc:15-17](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/ascendc_ir/generator/ascir_register.cc#L15-L17)：

```cpp
AscirRegister::AscirRegister(const char *type, const char *def_file_path, int64_t line) : ir_def_{} {
  ir_def_.Init(type, def_file_path, line);
}
```

构造函数只调用 `ir_def_.Init(...)` 初始化内部描述符 `ir_def_`（类型为 `AscIrDef`），**并没有触碰全局表**。

**④ 拷贝构造函数 = 提交点（关键）**：

[ascir_register.cc:48-50](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/ascendc_ir/generator/ascir_register.cc#L48-L50)：

```cpp
AscirRegister::AscirRegister(const AscirRegister &other) {
  AscirRegistry::GetInstance().RegisterAscIr(other.ir_def_.GetType(), other.ir_def_);
}
```

为什么提交动作放在拷贝构造里？回到宏展开：`static auto g_register_Add = AscirRegister("Add",...).Input(...)...;`。等号右边是一个**临时对象**（链式调用都在它身上完成），用这个临时对象去初始化静态对象 `g_register_Add`，会触发**拷贝构造**。于是在链式调用把 `ir_def_` 填满之后、拷贝发生的那一刻，完整的定义才被一次性写进全局表。这是一个非常干净的「**先攒齐再提交**」设计——既保证了静态对象本身被保留（防止注册信息随临时对象析构而丢失），又保证了提交时数据已经完整。

> 小贴士：这个类同时 `delete` 了赋值运算符和移动构造（[ascir_register.h:64-67](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascir_register.h#L64-L67)），正是为了强制「只能拷贝构造提交一次」，避免误用。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `REG_ASC_IR` 的展开过程，确认「链式调用 + 拷贝构造提交」这一机制。

**操作步骤**：

1. 打开 [ascir_builtin_ops_v1.cpp:414-421](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/generator/ascir_builtin_ops_v1.cpp#L414-L421)，找到 `REG_ASC_IR(Add)` 这一段。
2. 在脑子里（或纸上）把宏 `REG_ASC_IR(Add)` 按 4.1.3① 的定义展开，写出它等价的 C++ 语句。
3. 对照 [ascir_register.cc:48-50](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/ascendc_ir/generator/ascir_register.cc#L48-L50)，回答：链式调用 `.Input()/.Output()/.Impl()` 修改的是哪个对象的 `ir_def_`？这个 `ir_def_` 又是在哪一行被送进全局表的？

**需要观察的现象**：你会确认链式方法修改的是「等号右边的临时对象」，而写入全局表发生在「用临时对象拷贝构造静态对象」的瞬间。

**预期结果**：展开后形如 `static auto g_register_Add = af::ascir::AscirRegister("Add", "....cpp", 414).Input("x1","T").Input("x2","T").Output("y","T").ComputeType(...).Impl(...);`，临时对象填好 `ir_def_` 后，经拷贝构造触发 `RegisterAscIr("Add", ...)`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `.Impl(...)` 这一整段链式调用删掉，只留 `REG_ASC_IR(Add)`，`Add` 还会被注册进全局表吗？

> **答案**：会。`REG_ASC_IR(Add)` 本身已经会展开成「构造临时对象 + 拷贝构造静态对象」，拷贝构造里就会调用 `RegisterAscIr`。只是这时登记进去的 `AscIrDef` 没有 soc 实现、没有 dtype 约束——下游查它的 ATT/codegen 实现时会拿到空实现（见 4.2）。也就是说「登记」和「登记得完整」是两件事。

**练习 2**：为什么注册动作放在拷贝构造函数里，而不是放在普通构造函数里？

> **答案**：普通构造函数发生在链式调用**之前**，那时 `ir_def_` 只有 type/文件/行号，还没有输入输出和实现信息，提交进去是不完整的。拷贝构造发生在链式调用**全部完成之后**（用填好的临时对象初始化静态对象），此时提交的才是完整定义。

### 4.2 builtin ops 注册：AscIrImpl 三元组与平台组合

#### 4.2.1 概念说明

4.1 讲的是「骨架」——怎么把一个算子名登记进表。本节讲「血肉」：一个算子登记时，`Impl(...)` 那一段到底塞了什么。

一个算子要能在 Autofuse 里跑通融合全流程，至少需要三样东西：

1. **ATT 实现**：告诉自动 tiling 模块（att）这个算子的性能公式、对齐要求等（用于 cost model）。
2. **codegen 实现**：告诉代码生成模块这个算子要调用哪个 AscendC API、需要包含哪些头文件、临时 buffer 多大。
3. **dtype 约束**：这个算子的每个类型符号（如 `T`、`T1`、`T2`）支持哪些数据类型（如 `DT_FLOAT16`、`DT_INT32`）。

这三样被打包成一个 `AscIrImpl` 三元组，按**芯片平台**登记。这就是 `builtin ops`（内置算子）注册表 `ascir_builtin_ops_v1.cpp` 在做的事。

#### 4.2.2 核心流程

```text
对每个内置算子 X：
  REG_ASC_IR(X)
    .Input(...).Output(...)          // 算子签名
    [.Attr<...>(...)]                // 可选属性
    .ComputeType(...)                // 计算类别
    .Impl(v1_soc_versions, {          // 三元组，绑定到 v1 平台
        AscIrImplCreator<XAscIrAttImpl>(),      // ① ATT 实现创建器
        AscIrImplCreator<XAscIrCodegenImpl>(),  // ② codegen 实现创建器
        {{"T", TensorType{DT_FLOAT16, ...}}}    // ③ dtype 约束
    });

其中 v1_soc_versions = {"2201"}，表示该三元组对 2201 平台生效。
不同平台（如 v35 的 3510/5102）有各自的 builtin ops 文件，
按相同 type 名登记，最终在全局表里「同名合并」。
```

#### 4.2.3 源码精读

**① 全局表的数据结构**：先看「一个算子的完整定义」`AscIrDef` 和「实现三元组」`AscIrImpl` 长什么样。

[ascir_registry.h:219-223](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascir_registry.h#L219-L223) 定义了三元组：

```cpp
struct AscIrImpl {
  AscIrAttCreator att;        // 创建 ATT 实现的工厂函数
  AscIrCodegenCreator codegen; // 创建 codegen 实现的工厂函数
  std::vector<std::pair<std::string, OrderedTensorTypeList>> support_dtypes; // dtype 约束
};
```

两个创建器是函数指针类型（[ascir_registry.h:216-217](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascir_registry.h#L216-L217)），由模板函数 `AscIrImplCreator<T>()` 生成（[ascir_registry.h:211-214](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascir_registry.h#L211-L214)）：它返回一个 `[](){ return std::unique_ptr<T>(new T()); }`，即「new 一个实现类出来」。用工厂函数而不是直接存对象，是为了每次查询都能拿到一个全新的、互不干扰的实例。

`AscIrAtt` 和 `AscIrCodegen` 是两个抽象基类：

- `AscIrAtt`（[ascir_registry.h:193-209](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascir_registry.h#L193-L209)）：提供对齐提示（`GetInnerDimPromptAlignSize` 默认 32B）、性能公式（`GetApiPerf`、`GetAscendCApiPerfTable`）等，供 att 建模使用。
- `AscIrCodegen`（[ascir_registry.h:115-191](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascir_registry.h#L115-L191)）：提供 API 调用名（`GetApiCallName`/`GetApiName`）、头文件（`LoadApiHeaderFiles`/`IncludeApiHeaderFiles`）、临时 buffer 大小（`CalcTmpBufSize`）等，供 codegen 生成代码使用。

完整的算子定义 `AscIrDef`（[ascir_registry.h:232-282](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascir_registry.h#L232-L282)）把这些都装在一起，并提供按 soc 版本取实现的 `GetAscIrAttImpl(soc)` / `GetAscIrCodegenImpl(soc)`。

**② 真实算子的登记**：看 v1 平台的两个典型例子。

平台常量在 [ascir_builtin_ops_v1.cpp:44](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/generator/ascir_builtin_ops_v1.cpp#L44)：

```cpp
const std::vector<std::string> v1_soc_versions{"2201"};
```

一个 elementwise 二元算子 `Add`（[ascir_builtin_ops_v1.cpp:414-421](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/generator/ascir_builtin_ops_v1.cpp#L414-L421)）：

```cpp
REG_ASC_IR(Add)
    .Input("x1", "T").Input("x2", "T").Output("y", "T")
    .ComputeType(ComputeType::kComputeElewise)
    .Impl(v1_soc_versions, {af::ascir::AscIrImplCreator<af::ascir::AddAscIrAttImpl>(),
                            af::ascir::AscIrImplCreator<af::ascir::AddAscIrCodegenImpl>(),
                            {{"T", TensorType{DT_INT16, DT_INT32, DT_FLOAT16, DT_FLOAT}}}});
```

读法：`Add` 有两个同类型输入 `x1/x2`、一个输出 `y`；计算类别是逐元素（elewise）；对 2201 平台，ATT 实现是 `AddAscIrAttImpl`、codegen 实现是 `AddAscIrCodegenImpl`，且类型符号 `T` 只允许 `INT16/INT32/FLOAT16/FLOAT` 四种。

需要类型转换的算子 `Cast`（[ascir_builtin_ops_v1.cpp:189-195](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/generator/ascir_builtin_ops_v1.cpp#L189-L195)）则用两个符号 `T1`/`T2`，并通过 `kCastTypePairs`（[ascir_builtin_ops_v1.cpp:178-188](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/generator/ascir_builtin_ops_v1.cpp#L178-L188)）枚举合法的源/目的类型对——这正是上一讲提到的「混合精度」在注册层的体现。

整个 v1 文件用这种方式登记了 **84 个** `REG_ASC_IR`（含 `Data`/`Scalar`/`Cast`/`Abs`/`Add`/`MatMul`/`Conv2D` 等），全部绑定到 `{"2201"}` 这一个平台。

**③ 平台同名合并机制**：v35 平台（910C/910B，soc `3510/5102`）有另一份 builtin 文件 [ascir_builtin_ops_v2.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp#L53-L53)，登记了 **154 个** `REG_ASC_IR`，其中大量算子名（如 `Add`、`Mul`、`Cast`）与 v1 重叠，但实现类带 `V2` 后缀（如 `AddAscIrAttImplV2`）。

为什么同名不会冲突？看全局表的写入逻辑 [ascir_registry.cc:266-273](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/ascendc_ir/generator/ascir_registry.cc#L266-L273)：

```cpp
void AscirRegistry::RegisterAscIr(const std::string &type, const AscIrDef &def) {
  auto iter = types_to_ascir_.find(type);
  if (iter == types_to_ascir_.end()) {
    types_to_ascir_[type] = def;          // 首次出现：直接插入
  } else {
    iter->second.AppendSocImpl(def);      // 已存在：追加该平台的实现
  }
}
```

也就是说，全局表是「**算子类型 → 多平台实现集合**」的映射。v1 先登记 `Add@2201`，v2 再登记 `Add@3510/5102` 时走 `AppendSocImpl` 分支，把新平台的实现挂到同一个 `Add` 名下。这样一张表就能同时服务多平台，查询时再按当前平台挑实现。

#### 4.2.4 代码实践

**实践目标**：通过单元测试直观感受「注册即生效」，并学会跑相关 UT。

**操作步骤**：

1. 阅读 [test_ascir_ops.cpp:24-29](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/tests/ut/ascir/test_ascir_ops.cpp#L24-L29)。这个测试用 `REG_OP(TestOp)...` 注册了一个测试算子，然后直接构造 `ascir::Graph` 并 `SetInputs`。注意它**不需要手动调用任何注册函数**——只要把测试链接进了可执行文件，静态对象就会在 `main` 前自动登记。
2. （可选，待本地验证）按 AGENTS.md 约定的命令跑 Autofuse 框架 UT：
   ```bash
   sh build.sh -u --module=autofuse_framework -j 8
   ```
   > 注意 `-j 8`：Autofuse 是大型 C++ 工程，并行度过高容易 OOM（详见 u1-l3）。

**需要观察的现象**：测试中没有任何「先注册、再使用」的显式调用顺序——`REG_OP` 宏产生的静态对象在程序启动阶段已完成注册，测试函数体运行时算子工厂已经就绪。

**预期结果**：`RegOp_WillCreateAscirOpFactory` 用例通过，说明注册确实在静态初始化期完成。具体运行结果**待本地验证**（依赖 CANN 环境）。

#### 4.2.5 小练习与答案

**练习 1**：`Add` 的 `Impl` 里 `{{"T", TensorType{DT_INT16, DT_INT32, DT_FLOAT16, DT_FLOAT}}}` 这一项，`"T"` 和列表分别代表什么？如果把 `DT_INT32` 从列表里删掉，会发生什么？

> **答案**：`"T"` 是 `Add` 签名里 `Input("x1","T")` 用的**数据类型符号**（一个占位符，把输入输出绑定到同一组允许的 dtype）；列表是该符号允许的具体数据类型。删掉 `DT_INT32` 后，当 `Add` 的输入是 `int32` 时，会因 dtype 不在支持列表里而无法匹配注册项，从而无法融合（fallback）。

**练习 2**：v1 和 v2 都登记了 `Add`，全局表 `types_to_ascir_` 里 `Add` 这一项有几份实现？为什么不会互相覆盖？

> **答案**：两份实现（分别对应 2201 和 3510/5102 平台）。因为 `RegisterAscIr` 对已存在的 type 走 `AppendSocImpl` 分支追加，而不是覆盖。这正是多平台共存的机制。

### 4.3 generator 代码生成与 codegen 可见性：REGISTERED_OPS 的角色

#### 4.3.1 概念说明

本节回答三个问题：

1. **「generator」是什么？** 在 ASCIR 注册语境下，「generator」有两层含义：一是 builtin ops 文件所在目录就叫 `generator/`（因为它登记的是「用来生成代码的算子」）；二是 `graph_metadef/.../generator/generator.cc` 这个文件会**遍历全局表**，为每个已注册算子生成一份 IR 代码（即把 `AscIrDef` 翻译成 C++ 源码）。本节聚焦后者。
2. **一个算子「被 codegen 可见」要经过哪些环节？** 注册只是把定义放进表；真正被 codegen 用到，还需要：(a) 该算子所在的 builtin 文件被编译并链接进共享库；(b) 程序运行时静态初始化把定义写进全局表；(c) codegen 通过查询接口 `GetAscIrCodegenImpl(平台, 类型)` 取出实现类。
3. **`REGISTERED_OPS` 又是什么，和 builtin ops 是什么关系？** 它是**另一份**清单，位于 Python 绑定侧，列出「要给 Python 前端暴露哪些算子名」。它和 builtin ops（C++ 侧完整注册）是两套并行、必须手工保持一致的列表。

#### 4.3.2 核心流程

```text
【注册侧（编译期 + 启动期）】
  builtin ops (.cpp) --编译链接--> 共享库
        |（main 前静态初始化）
        v
  AscirRegistry 全局表 { type -> AscIrDef(含各平台 att/codegen 实现) }

【消费侧（运行期）】
  路径 A（离线 generator.cc）：遍历 GetAll() → 为每个算子生成 IR 代码
  路径 B（在线 codegen/att）：
        codegen 遇到某算子节点
          -> common_utils::GetAscIrCodegenImpl("Add")
               -> AscirRegistry::GetIrCodegenImpl(平台, "Add")
                    -> AscIrDef::GetAscIrCodegenImpl(平台)
          -> 拿到 AddAscIrCodegenImpl，调用其 GetApiName() 等生成 AscendC 调用

【Python 绑定侧】
  REGISTERED_OPS（pyascir.h）--X 宏展开--> 为每个算子名生成 Python 类
  （与 C++ builtin ops 必须手工保持一致，否则 Python 能构图但 C++ 查不到实现）
```

#### 4.3.3 源码精读

**① generator 遍历全局表生成代码**。

[generator.cc:1806-1824](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/ascendc_ir/generator/generator.cc#L1806-L1824) 的 `GenAll`：

```cpp
void GenAll(std::stringstream &ss) {
  ...
  std::map<std::tuple<std::string,int64_t,std::string>, AscIrDef> ordered_keys_to_def;
  for (const auto &type_and_def : AscirRegistry::GetInstance().GetAll()) {   // 遍历全局表
    ordered_keys_to_def[std::make_tuple(type_and_def.second.GetFilePath(),
        type_and_def.second.GetLine(), type_and_def.first)] = type_and_def.second;
  }
  for (const auto &key_and_def : ordered_keys_to_def) {
    ss << "// Defined at " << ... << ':' << key_and_def.second.GetLine() << std::endl;
    ... GenAscIr(key_and_def.second, ss); ...                                 // 生成该算子的 IR 代码
  }
}
```

注意两个细节：一是它用 `GetAll()` 拿到**全部**已注册算子——这印证了「只要登记进表，generator 就看得见」；二是它按 `(文件, 行号, 类型)` 排序后再生成，保证输出**确定性**（同一份注册集合永远生成同一份代码），这也呼应了编码红线里的「图改写/代码生成确定性」要求（u12-l3 会专门讲）。注释 `// Defined at 文件:行号` 正是用了 4.1 里 `__FILE__/__LINE__` 记下的信息。

**② 在线查询：codegen/att 如何取出实现**。

[common_utils.cpp:541-553](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/common/common_utils.cpp#L541-L553) 提供了两个封装好的查询函数：

```cpp
std::unique_ptr<af::ascir::AscIrAtt> GetAscIrAttImpl(const string &ascir_type) {
  std::string platform_name;
  GE_ASSERT_SUCCESS(ge::PlatformContext::GetInstance().GetCurrentPlatformString(platform_name), ...);
  return af::ascir::AscirRegistry::GetInstance().GetIrAttImpl(platform_name, ascir_type);  // 按平台+类型查
}
std::unique_ptr<af::ascir::AscIrCodegen> GetAscIrCodegenImpl(const string &ascir_type) { ... 同理 ... }
```

它们先取**当前运行平台**（如 `"2201"`），再用 `(平台, 算子类型)` 去全局表查。底层 [ascir_registry.cc:278-285](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/ascendc_ir/generator/ascir_registry.cc#L278-L285) 的实现：找不到返回 `nullptr`，找到则由 `AscIrDef` 按平台挑出对应的创建器 new 一个实例。这正是 4.2 注册的 `AddAscIrCodegenImpl` 被 codegen「看见」的入口。

一个具体的 codegen 实现类示例见 [v1_ascir_codegen_impl.h:23-36](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/generator/v1_ascir_codegen_impl.h#L23-L36)：`DataAscIrCodegenImpl` 继承 `AscIrCodegen`，告诉 codegen 「`Data` 算子的 API 名是 `Data`、调用类名是 `ApiCall`、需要包含 `kernel_operator_vec_duplicate_intf.h`」。codegen 拿到这些信息后就能生成正确的 AscendC 调用代码。

**③ `EXPORT_GENERATOR()` 的真相**。

在 [ascir_builtin_ops_v1.cpp:42](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/generator/ascir_builtin_ops_v1.cpp#L42) 每个 builtin 文件开头都有一行 `EXPORT_GENERATOR()`。它的定义在 [ascir_register.h:94](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/graph_metadef/graph/ascendc_ir/ascir_register.h#L94)：

```cpp
#define EXPORT_GENERATOR()
```

**它是一个空宏**——目前不展开成任何代码。它的作用是**语义占位/扩展点**：标记「本文件是一个 generator（算子注册生成单元）」，为将来按需注入导出逻辑留位置。理解这一点很重要：不要误以为 `EXPORT_GENERATOR()` 触发了什么注册动作，真正的注册始终是 `REG_ASC_IR` + 拷贝构造完成的。

**④ `REGISTERED_OPS`：Python 侧的算子名清单**。

[pyascir.h:31-44](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/py_module/pyascir.h#L31-L44) 定义了它（截取开头）：

```cpp
#define REGISTERED_OPS            \
  OP(Data)                        \
  OP(Scalar)                      \
  OP(ScalarData)                  \
  OP(Workspace)                   \
  OP(Output)                      \
  ...
```

这是一种经典的 **X 宏（X-Macro）技巧**：先写一张「名字清单」（共 144 项 `OP(...)`），然后在 [pyascir.cpp:1520](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/py_module/pyascir.cpp#L1520-L1520) 和 [pyascir_types.cpp:34](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/py_module/pyascir_types.cpp#L34-L34) 里通过先 `#define OP(name) <针对每个 name 的绑定代码>`、再展开 `REGISTERED_OPS`，批量生成每个算子的 Python 绑定。

它与 builtin ops 的关系：

| 维度 | `REG_ASC_IR`（builtin ops） | `REGISTERED_OPS`（pyascir.h） |
|------|------------------------------|-------------------------------|
| 所在侧 | C++ 编译器侧 | Python 绑定侧（pyautofuse） |
| 内容 | 完整 IR 定义（签名/属性/dtype/实现） | 仅算子名清单 |
| 作用 | 让 att/codegen 能查到实现 | 让 Python 前端能按名字构造算子 |
| 数据 | 84（v1）+ 154（v2）条 | 144 条（跨平台合并的名字） |
| 一致性要求 | —— | **必须与 builtin ops 名字集合保持一致** |

核心结论：**`REGISTERED_OPS` 是 builtin ops 的「名字投影」**。如果一个算子在 `REGISTERED_OPS` 里有名字、但在 builtin ops（含当前平台）里没有登记实现，Python 能构造出这个算子，但 codegen 时 `GetAscIrCodegenImpl` 会返回 `nullptr`，导致融合失败/fallback（这正是 u3-l3 讲过的「未在 ASCIR 注册」fallback 原因之一）。反之，两边名字必须对齐。所以新增算子时，**C++ 注册（builtin ops）和 Python 名单（REGISTERED_OPS）要同步改**——这一点 `af-reg-ascir` 这类 skill 会专门检查。

#### 4.3.4 代码实践

**实践目标**：把「注册 → 全局表 → codegen 可见」这条链路完整跟一遍，并理解 `REGISTERED_OPS` 与 builtin ops 的一致性约束。

**操作步骤**：

1. 在 [ascir_builtin_ops_v1.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/generator/ascir_builtin_ops_v1.cpp#L414-L421) 中列出 5 个已注册算子（如 `Add`、`Cast`、`Mul`、`Exp`、`Concat`），记下各自的 `ComputeType` 和 `Impl` 三元组里的 ATT/codegen 实现类名。
2. 跟踪 `Add` 从注册到被 codegen 看见的完整路径：`REG_ASC_IR(Add)`（4.1）→ 拷贝构造提交到 `AscirRegistry`（[ascir_register.cc:48-50](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/graph_metadef/graph/ascendc_ir/generator/ascir_register.cc#L48-L50)）→ codegen 调 `GetAscIrCodegenImpl("Add")`（[common_utils.cpp:548-553](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/common/common_utils.cpp#L548-L553)）→ 取出 `AddAscIrCodegenImpl`。
3. 对比 [pyascir.h:31](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/py_module/pyascir.h#L31-L44) 的 `REGISTERED_OPS` 名单，确认你列的 5 个算子名是否都出现在 `REGISTERED_OPS` 中。

**需要观察的现象**：你会发现 C++ builtin（按平台登记，含完整实现）与 Python `REGISTERED_OPS`（只列名字）是两份独立清单；正常情况下两边名字应当对应。

**预期结果**：你列出的常见算子（`Add`/`Cast`/`Mul`/`Exp`/`Concat`）在 `REGISTERED_OPS` 中都能找到对应名字。若发现某名字只在一边存在，那就是潜在的不一致点（属于代码维护事项，不要随意改动源码）。

#### 4.3.5 小练习与答案

**练习 1**：`EXPORT_GENERATOR()` 空宏既然什么都不做，为什么每个 builtin 文件都要写它？

> **答案**：它是语义标记和扩展点。标记「本文件负责登记 generator 算子」，便于阅读和工具识别；同时为将来需要给 generator 文件统一注入某种导出/初始化逻辑时预留位置。当前不影响行为，真正的注册由 `REG_ASC_IR` 完成。

**练习 2**：假设某天有人只改了 `REGISTERED_OPS` 加了 `OP(Foo)`，却忘了在 builtin ops 文件里 `REG_ASC_IR(Foo).Impl(...)`，会出什么问题？

> **答案**：Python 前端能构造出 `Foo` 算子并建图，但运行到 codegen 时 `GetAscIrCodegenImpl("Foo")` 会因全局表里没有 `Foo` 而返回 `nullptr`，该子图无法融合、退回 fallback，甚至报错。这就是「Python 能画出来、C++ 不认识」的不一致故障，也说明两份清单必须同步维护。

**练习 3**：`generator.cc` 的 `GenAll` 为什么要把 `GetAll()` 的结果先放进一个按 `(文件,行号,类型)` 排序的 `std::map` 再生成？

> **答案**：为了让生成结果**确定**。`GetAll()` 返回的是 `unordered_map`，遍历顺序不确定；若直接遍历，每次生成的代码顺序会变，破坏确定性（也会让 diff/caching 失效）。排序后，同一份注册集合永远产出同一份代码——这贴合项目「图改写与代码生成必须确定性」的红线。

## 5. 综合实践

**任务**：为「新增一个 ASCIR 算子」画出完整的注册链路图，并指出每一站对应的源码位置。

假设要新增一个 unary 算子 `Tanh`（实际上已存在，这里用作练习对象），请完成：

1. **登记**：在 [ascir_builtin_ops_v1.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/generator/ascir_builtin_ops_v1.cpp#L290-L296) 找到 `REG_ASC_IR(Tanh)` 的真实写法（[第 290-296 行](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/generator/ascir_builtin_ops_v1.cpp#L290-L296)），列出它的签名、`ComputeType` 和三元组（`TanhAscIrAttImpl` / `TanhAscIrCodegenImpl` / 支持的 dtype）。
2. **提交**：说明这段 `REG_ASC_IR` 是经由哪个拷贝构造函数（给出行号）把 `Tanh` 写进 `AscirRegistry` 的。
3. **存储**：画出 `AscirRegistry` 里 `Tanh` 这一项的结构（`type -> AscIrDef`，`AscIrDef` 内含按 soc `2201` 存放的 `AscIrImpl` 三元组）。
4. **查询**：写出 codegen 要用到 `Tanh` 时调用的查询函数（`GetAscIrCodegenImpl`），并说明它会按什么键去查。
5. **Python 一致性**：检查 `Tanh` 是否出现在 [pyascir.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/py_module/pyascir.h#L31-L44) 的 `REGISTERED_OPS` 中；若某平台（如 v35）也要支持，还需在 [ascir_builtin_ops_v2.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp#L578-L578) 用同名 `REG_ASC_IR(Tanh)` 登记带 `V2` 后缀的实现。

**交付物**：一张包含「源码位置 → 数据结构变化 → 查询路径」三列的链路表。完成后你应当能向别人讲清：一个算子从一行 `REG_ASC_IR` 到最终被 codegen 调用，中间经历了哪些环节、分别在哪几个文件里。

> 进阶（可选）：阅读 `af-reg-ascir` skill 的说明，了解项目里新增/更新 ASCIR 算子时官方推荐的「最小修改面」清单（注册、regbase、Codegen、Python、UT/ST/E2E），把你的链路表与该清单对照。

## 6. 本讲小结

- ASCIR 算子采用**自注册**模式：每个算子在自己的 `.cpp` 里用 `REG_ASC_IR` 宏声明，程序启动时由静态对象的拷贝构造函数统一提交进全局单例表 `AscirRegistry`。
- `AscirRegister` 是一个**流式构造器**，`.Input()/.Output()/.Attr<T>()/.ComputeType()/.Impl()` 链式调用把算子元数据填进 `AscIrDef`；提交点巧妙地放在**拷贝构造函数**里，保证「链式填完后再一次性写入」。
- 一个算子的完整注册项是 `AscIrImpl` **三元组**：ATT 实现创建器、codegen 实现创建器、dtype 约束，并按**芯片平台**（v1=`2201`，v2=`3510/5102`）绑定。
- 全局表是「算子类型 → 多平台实现」的映射，`RegisterAscIr` 对同名算子走 `AppendSocImpl` **合并**而非覆盖，这是多平台共存的机制。
- 「被 codegen 可见」需要三步：builtin 文件被编译链接 → 静态初始化写表 → 下游用 `GetAscIrCodegenImpl(平台, 类型)` 查询取出实现类。
- `REGISTERED_OPS`（pyascir.h）是 Python 绑定侧的**算子名清单**，是 builtin ops 的「名字投影」，两边必须手工保持一致；`EXPORT_GENERATOR()` 目前是空宏，仅作语义占位。

## 7. 下一步学习建议

- **u5-l2 reg_func 注册函数详解（reduce/compare）**：本讲看到 `Impl(...)` 里登记的是实现类（`XxxAscIrAttImpl`/`XxxAscIrCodegenImpl`），但这些类的形状推导、tiling 占位具体怎么写？下一讲以 `reduce.cpp` 和 `compare.cpp` 为例，拆开一个 reg_func 注册函数的内部结构。
- **u5-l3 AscendC API 头文件与算子能力**：codegen 实现类（如 `GetApiName()` 返回的名字）最终指向 `autofuse/ascendc/api/` 下的 AscendC 接口头文件，下一讲讲解这层衔接。
- **u11-l1 v35 平台扩展机制**：本讲提到的 v2（3510/5102）builtin 文件与 v1 的「同名合并」是平台扩展的核心，专家层会系统讲解 v35 子目录如何按平台启用。
- **延伸阅读**：可先浏览 `autofuse/tests/ut/ascir/test_ascir_ops.cpp` 全文，看更多「注册→建图→校验」的测试用例，巩固「注册即静态生效」的直觉。
