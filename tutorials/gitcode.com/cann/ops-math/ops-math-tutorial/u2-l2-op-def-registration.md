# 算子定义与注册：op_def 与 OpDef DSL

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解 `op_host` 目录下 `<算子名>_def.cpp` 的作用——它是算子向 CANN 注册「规格说明书」的地方。
2. 读懂 OpDef 链式 DSL：`Input` / `Output` / `ParamType` / `DataType` / `Format` / `UnknownShapeFormat` 以及 `AICore().AddConfig(...)` 等声明。
3. 理解多个输入/输出之间 DataType 列表的「按位置组合」语义——这正是上一讲 README 中类型互推导规则的源头。
4. 理解 `IMPL_OP_INFERSHAPE` 如何把输出 shape 推导函数注册给算子。
5. 能够对照 add 写出一个新算子的定义骨架，并说出为一个算子新增数据类型支持需要动哪些文件。

## 2. 前置知识

- **算子信息库（op registry）**：CANN 在加载算子时，需要知道每个算子叫什么、有几个输入输出、支持哪些数据类型和格式、跑在哪款芯片上。这些元信息由一段 C++ 代码声明，编译后成为「算子信息库」。GE（图引擎）和 aclnn 调用层在做合法性检查时，查的就是这份注册信息。
- **DSL（Domain Specific Language，领域专用语言）**：CANN 用 C++ 的链式调用（`.Input().ParamType().DataType()...`）来写算子声明，形式上接近「填表」，这种写法就叫链式 DSL。它本质还是普通 C++ 成员函数调用，只是每个函数都返回自身引用，所以能一直用 `.` 连下去。
- **ge::DT_* 与 ge::FORMAT_***：`ge` 命名空间（来自 CANN 的 Graph Engine 头文件）定义了所有数据类型枚举（如 `ge::DT_FLOAT16`、`ge::DT_BF16`）和格式枚举（如 `ge::FORMAT_ND`，ND 表示任意维、连续存储）。本仓的 def 文件通过包含 CANN toolkit 的 `register/op_def_registry.h` 拿到这些定义。
- **Host 侧与 Device 侧**：def 文件运行在 Host（CPU）侧，只描述元信息，不含任何计算逻辑；真正的计算在 op_kernel 的 Device 侧代码里（下一讲详讲 kernel）。

上一讲（u2-l1）我们学了「怎么读算子 README 规格表」；本讲反过来看：**README 里那张规格表，是怎么由 add_def.cpp 这段代码生成/对应的**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [math/add/op_host/add_def.cpp](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/op_host/add_def.cpp) | add 算子的 AICore 版注册文件，本讲主角 |
| [math/add/op_host/add_infershape.cpp](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/op_host/add_infershape.cpp) | add 的输出 shape 推导函数及其注册 |
| [examples/add_example/op_host/add_example_def.cpp](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/examples/add_example/op_host/add_example_def.cpp) | 教学算子 AddExample 的 def，带中文注释的最简版本 |
| [math/add/op_kernel_aicpu/add_aicpu_def.cpp](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/op_kernel_aicpu/add_aicpu_def.cpp) | add 的 AICPU 版注册文件，用于对比「另一条执行通道」的声明差异 |
| [math/reduce_sum/op_host/reduce_sum_def.cpp](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/reduce_sum/op_host/reduce_sum_def.cpp) | 带属性（Attr）声明的算子 def，用于对比 `Attr()` 写法 |
| [math/add/README.md](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/README.md) | add 的规格文档，其类型表与 def 的 DataType 列表对应 |

> 说明：`register/op_def_registry.h`、`infershape_broadcast_util.h` 等头文件来自 CANN toolkit 安装包（`$ASCEND_HOME_PATH` 下），不在本仓库内，本文只描述其接口行为。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1 OpDef 注册机制**、**4.2 数据类型与格式声明**、**4.3 输出 shape 推导注册**。

### 4.1 OpDef 注册：算子的「户籍登记」

#### 4.1.1 概念说明

每个算子在 CANN 里要能被找到，必须有人把它的名字、输入输出、支持芯片等信息登记进「算子信息库」。本仓的做法是在 `op_host/<算子名>_def.cpp` 中写一个继承 `OpDef` 的类，最后用 `OP_ADD` 宏完成登记。这份代码不参与任何数值计算，纯粹是元数据声明——可以理解为「用 C++ 语法填一张算子规格登记表」。

#### 4.1.2 核心流程

一个 def 文件的骨架固定为四步：

```text
1. 定义类：class Add : public OpDef，构造函数里完成所有声明
2. 声明输入/输出：this->Input("x1")...  this->Output("y")...
3. 声明芯片配置：构造 OpAICoreConfig，AICore().AddConfig("<soc版本>", config)
4. 注册：OP_ADD(Add);   ← 宏把类实例化并写入算子信息库
```

编译系统（u1-l2 讲过 CMakeLists 规则）会把所有算子的 def 编译链接成算子信息库，随 run 包安装到设备的 `opp` 目录；此后 aclnn/GE 检查「这个算子支不支持 FLOAT16」时，依据就是它。

#### 4.1.3 源码精读

先看文件头与类定义。add 的 def 只包含一个 CANN 头文件，然后定义 `Add` 类：

[math/add/op_host/add_def.cpp:L15-L21](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/op_host/add_def.cpp#L15-L21) —— 包含 `register/op_def_registry.h`（算子注册框架头文件），在 `ops` 命名空间下定义 `Add` 类并继承 `OpDef`，构造函数接收算子名字符串。

文件末尾一行完成注册：

[math/add/op_host/add_def.cpp:L76-L77](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/op_host/add_def.cpp#L76-L77) —— `OP_ADD(Add);` 宏把 `Add` 类的一个实例登记进算子信息库，`} // namespace ops` 结束。注意 `Add` 这个类名同时就是算子类型名（小写 `Add` 会与实际注册名一致）。

类构造的最后一部分是芯片配置：

[math/add/op_host/add_def.cpp:L64-L72](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/op_host/add_def.cpp#L64-L72) —— 构造 `OpAICoreConfig` 并链式设置能力开关，最后 `AICore().AddConfig("ascend950", aicoreConfig)` 声明该算子支持 ascend950 这款芯片。各开关含义：

| 开关 | 含义 |
| --- | --- |
| `DynamicCompileStaticFlag(true)` | 允许动态 shape 算子复用静态编译成果 |
| `DynamicFormatFlag(false)` | 不支持运行时动态选择格式 |
| `DynamicRankSupportFlag(true)` | 支持任意维（rank 可变）输入 |
| `DynamicShapeSupportFlag(true)` | 支持动态 shape |
| `NeedCheckSupportFlag(false)` | 不需要额外的支持性检查 |
| `PrecisionReduceFlag(true)` | 允许精度降级（如隐式类型转换） |
| `ExtendCfgInfo("opFile.value", "add_apt")` | 指定 kernel 入口文件名，对应 `op_kernel/arch35/add_apt.cpp`（u4-l2 会再遇到它） |

对比教学算子 AddExample 可以看得更直白，它带中文注释且声明了三款芯片：

[examples/add_example/op_host/add_example_def.cpp:L48-L51](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/examples/add_example/op_host/add_example_def.cpp#L48-L51) —— `ExtendCfgInfo("opFile.value", "add_example")` 注释明确说明该值对应 kernel 入口文件名；随后对 ascend910b、ascend910_93、ascend950 三款 soc 分别 `AddConfig`。

另外，`OP_ADD(Add)` 在仓库里出现了两处：AICore 版（op_host）和 AICPU 版（op_kernel_aicpu）。二者编译进**不同的信息库**（AICPU 算子库独立打包），所以类名相同并不冲突：

[math/add/op_kernel_aicpu/add_aicpu_def.cpp:L15-L34](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/op_kernel_aicpu/add_aicpu_def.cpp#L15-L34) —— AICPU 版同样定义 `class Add : public OpDef`，但支持类型更宽（含 INT16、UINT16、DOUBLE、COMPLEX128 等），最后用 `this->AICPU()` 而非 `this->AICore()` 注册。这解释了 u2-l1 的现象：README 类型表是 AICore 与 AICPU 两条通道支持范围的并集。

#### 4.1.4 代码实践

**实践：把 add 的 def 与 AddExample 的 def 并排对照，画出注册结构图。**

1. 实践目标：脱离注释也能说出 def 文件的固定四段结构。
2. 操作步骤：
   - 打开上面两个 def 文件，逐行对照。
   - 在纸上或笔记里画出如下结构（补全 `?` 处）：

     ```text
     OP_ADD(Add)
     ├── Input("x1")：ParamType(?) / DataType({?}) / Format({?}) / UnknownShapeFormat({?})
     ├── Input("x2")：同上
     ├── Output("y")：同上
     └── AICore().AddConfig("?", aicoreConfig)
         └── aicoreConfig：DynamicCompileStaticFlag(?) / ... / ExtendCfgInfo("opFile.value", "?")
     ```

3. 需要观察的现象：AddExample 比 add 多了 `.AutoContiguous()`、少了哪些类型；add 只注册 ascend950 一款芯片而 AddExample 注册三款。
4. 预期结果：能不看资料填出全部 `?`，并能说出 `opFile.value` 的值各自对应的 kernel 文件名（add → `add_apt`，add_example → `add_example`）。

#### 4.1.5 小练习与答案

**练习 1**：`OP_ADD(Add)` 中类名 `Add` 与算子名是什么关系？如果把类名改成 `MyAdd` 但不改其他代码，会发生什么？

**答案**：`OpDef` 构造函数收到的 name 参数就是注册到信息库的算子类型名；类名本身只是 C++ 标识符，但 `OP_ADD` 宏要求传入类名来实例化。实际本文件中 `explicit Add(const char* name) : OpDef(name)` 的 name 由宏在注册时填入，惯例上与类名一致。改成 `MyAdd` 后类名与注册名若不一致，可能导致 aclnn/op_kernel 侧按名字找不到算子的连带问题（具体宏内部行为在 CANN toolkit 头文件中，**待确认**）；本仓所有算子均保持类名 = 算子名的惯例。

**练习 2**：为什么 AICore 版和 AICPU 版都定义了 `class Add` 却不冲突？

**答案**：两个文件分别编入不同的目标（AICPU 算子库与 AICore 算子信息库），不链接到同一个二进制里；且 AICPU 版用 `this->AICPU()` 注册到 AI CPU 执行通道，AICore 版用 `this->AICore().AddConfig("ascend950", ...)` 注册到 AI Core 通道。

### 4.2 数据类型与格式声明：DataType 列表的「按位置组合」语义

#### 4.2.1 概念说明

`Input`/`Output` 链式声明里最重要的是 `DataType`。关键点：**多个张量的 DataType 不是各自独立的列表，而是按位置一一对应的组合表**——第 i 个位置上的所有类型构成一组合法的「输入→输出」类型组合。这正是 u2-l1 里类型互推导规则（如 `FLOAT16 + FLOAT → FLOAT`）在源码层的出处。

`ParamType` 有 `REQUIRED`（必选）和 `OPTIONAL`（可选）两种；`Format` 声明支持的内存格式；`UnknownShapeFormat` 声明 shape 未知的图编译场景下使用的格式（本仓绝大多数算子都是 ND）。

#### 4.2.2 核心流程

把 add_def.cpp 中三个 DataType 列表按位置对齐（每列一组）：

| 位置 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| x1 | BF16 | F16 | F32 | I32 | U8 | I8 | I64 | BOOL | C32 | C64 | **F16** | **F32** | **BF16** | **F32** |
| x2 | BF16 | F16 | F32 | I32 | U8 | I8 | I64 | BOOL | C32 | C64 | **F32** | **F16** | **F32** | **F32** |
| y | BF16 | F16 | F32 | I32 | U8 | I8 | I64 | BOOL | C32 | C64 | **F32** | **F32** | **F32** | **F32** |

前 10 列是「同类型进、同类型出」；后 4 列是**混合精度组合**：任意 half/bfloat16 与 float 相加，输出统一提升为 FLOAT。这就是「类型提升（Type Promotion）」在注册层的表达方式。同理，`Format` 和 `UnknownShapeFormat` 的列表长度必须与 `DataType` 一致（每列一个格式）。

如果算子有属性（如 reduce_sum 的 `keep_dims`），则用 `Attr()` 链声明，语法是另一套：`this->Attr("keep_dims").AttrType(OPTIONAL).Bool(false)`（属性名 / 可选性 / 类型与默认值），见 [math/reduce_sum/op_host/reduce_sum_def.cpp:L44-L45](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/reduce_sum/op_host/reduce_sum_def.cpp#L44-L45)。add 没有属性——aclnn 层的 `alpha` 标量是在 op_api 内部乘进输入的，不进入算子定义（u2-l6 会讲）。

#### 4.2.3 源码精读

x1 的完整链式声明：

[math/add/op_host/add_def.cpp:L22-L35](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/op_host/add_def.cpp#L22-L35) —— `Input("x1")` 声明名为 x1 的输入；`ParamType(REQUIRED)` 表示必选；`DataType({...})` 列出 14 个按位置对齐的类型；`Format` 与 `UnknownShapeFormat` 各给出等长的 ND 列表。

x2 与 y 的声明结构完全相同，只是类型列表在混合精度列上不同：

[math/add/op_host/add_def.cpp:L36-L49](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/op_host/add_def.cpp#L36-L49) —— x2 的声明；注意位置 11-14 是 `F32, F16, F32, F32`，与 x1 的 `F16, F32, BF16, F32` 交错对应。

[math/add/op_host/add_def.cpp:L50-L63](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/op_host/add_def.cpp#L50-L63) —— 输出 y 的声明；混合精度列的输出全部是 `DT_FLOAT`，即「低精度与高精度混合时输出提升为 FLOAT」。

对照 AddExample 的带注释最简写法更容易记住每个链式环节的语义：

[examples/add_example/op_host/add_example_def.cpp:L22-L27](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/examples/add_example/op_host/add_example_def.cpp#L22-L27) —— 注释逐项说明：`ParamType(REQUIRED)` 必选输入、`DataType` 支持数据类型、`Format` 支持格式、`UnknownShapeFormat` 未确定 shape 对应格式、`AutoContiguous()` 内存自动连续化（add 的 def 没有这一项）。

最后与 README 对照闭环——def 中声明的类型集合是 README 类型表的子集：

[math/add/README.md:L43-L61](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/README.md#L43-L61) —— README 参数表中 x1 列出 BOOL、INT8、INT16、INT32、INT64、UINT8、FLOAT64、FLOAT16、BFLOAT16、FLOAT32、COMPLEX128、COMPLEX64、COMPLEX32、STRING 共 14 种类型（含 AICPU 通道与 STRING 等特型），比 AICore def 中的 10 种同型组合更宽。读 README 时要知道它是全集，实际走 AICore 通道时以 def 声明为准。

#### 4.2.4 代码实践

**实践：为 add 假想新增 INT16 支持，列出全部修改点（不实际编译）。**

1. 实践目标：体会「改一行声明 ≠ 改完」，建立算子类型支持的全链路视角。
2. 操作步骤（假设要求 x1/x2/y 同为 INT16 时可计算）：
   - **修改点 1**：`math/add/op_host/add_def.cpp` —— 在 x1、x2、y 三个 `DataType({...})` 列表的**相同位置**各追加 `ge::DT_INT16`，并在两个 `Format({...})` 和两个 `UnknownShapeFormat({...})` 列表的相同位置各追加一个 `ge::FORMAT_ND`（五个列表必须等长、按位对齐）。
   - **修改点 2**：`math/add/op_kernel/arch35/` 下的 kernel 代码 —— kernel 按 dtype 编译期分支组织（u2-l5 详讲），INT16 需要有对应的计算分支或 cast 策略；若走 cast，则改 DAG 定义（`add_dag.h`）。
   - **修改点 3**：`math/add/op_host/arch35/add_tiling_arch35.cpp` —— 若不同 dtype 的块切分策略不同（如 16 位与 32 位每块元素数不同），tiling 需要感知新类型。
   - **修改点 4**：`math/add/op_api/aclnn_add.cpp` —— aclnn 层的类型检查/类型推导逻辑需放行 INT16。
   - **修改点 5**：`math/add/README.md` 与 `math/add/docs/` 接口文档 —— 类型表更新；若涉及 AICPU 通道还要同步 `op_kernel_aicpu` 侧（AICPU 版 def 已含 INT16，见上文 4.1.3）。
   - **修改点 6**：`math/add/tests/` 下 ut/st 用例补充 INT16 数据。
3. 需要观察的现象：五个列表（3 个 DataType + 2 个 Format/UnknownShapeFormat 组）长度是否同步变化；位置错位会把 INT16 错配成另一种混合组合。
4. 预期结果：写出一份 6 点修改清单；本实践为源码阅读型，**不需要编译运行，输出以文档形式记录**。

#### 4.2.5 小练习与答案

**练习 1**：add_def.cpp 中 x1 的 DataType 第 11 位是 `ge::DT_FLOAT16`、y 的第 11 位是 `ge::DT_FLOAT`，这说明什么调用是合法的？

**答案**：说明 `x1` 为 FLOAT16、`x2` 为 FLOAT（x2 第 11 位）、输出 `y` 为 FLOAT 的组合是已注册的合法类型组合——即 half 与 float 混合相加输出 float，符合类型提升规则。

**练习 2**：如果把 `DT_BOOL` 加进 x1 的列表却忘了加进 y 的列表，最直接的后果是什么？

**答案**：三个列表长度不一致、按位组合错乱：DataType 组合表本身就坏了——轻则注册校验失败编译报错，重则所有 BOOL 之后的类型组合整体错位（比如本该 F16+F16→F16 变成了别的组合）。写 def 时必须保证各列表等长且语义对位。

**练习 3**：`ParamType(REQUIRED)` 和 `AttrType(OPTIONAL)` 分别修饰什么对象？

**答案**：`ParamType` 修饰**输入/输出张量**（REQUIRED 必选 / OPTIONAL 可选，可选输入常用于如 weight 缺省场景）；`AttrType` 修饰**属性**（标量/列表等非张量参数，OPTIONAL 表示图模式下可省略并使用默认值，例如 reduce_sum 的 `keep_dims` 默认 false）。两者是不同维度的声明，不要混淆。

### 4.3 输出 shape 推导注册：IMPL_OP_INFERSHAPE

#### 4.3.1 概念说明

注册了输入输出规格后，还差一块：给定输入 shape，输出 shape 是什么？这由 `InferShape`（shape 推导）函数负责，写在独立的 `<算子名>_infershape.cpp` 里，用 `IMPL_OP_INFERSHAPE` 宏注册到对应算子上。图编译和 aclnn 调用前都会执行它来决定输出张量的形状（对 add 来说就是 broadcast 结果）。

#### 4.3.2 核心流程

```text
调用方（GE/aclnn）拿到输入 desc
        ↓
按算子名查到已注册的 InferShapeForAdd
        ↓
InferShapeForAdd(context)          ← context 携带输入 shape/attr，并可写入输出 shape
        ↓
转调公共工具 Ops::Base::InferShape4Broadcast(context)   ← 广播规则推导
        ↓
返回 ge::GRAPH_SUCCESS / 失败码，输出 shape 写回 context
```

broadcast 的规则（右对齐、维度为 1 可扩展、否则必须相等）在 u3-l1 会系统讲；本讲只需记住 add 的推导完全复用公共实现，一行核心逻辑。

#### 4.3.3 源码精读

add 的 infershape 全文只有两个关键语句：

[math/add/op_host/add_infershape.cpp:L11-L21](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/op_host/add_infershape.cpp#L11-L21) —— 包含公共工具头 `infershape_broadcast_util.h`（来自 CANN toolkit，本仓无此文件）与 `register/op_impl_registry.h`；定义 `InferShapeForAdd(gert::InferShapeContext* context)`：先 `OP_LOGI` 打日志，然后直接 `return Ops::Base::InferShape4Broadcast(context)`，把广播 shape 推导交给公共实现。

[math/add/op_host/add_infershape.cpp:L23](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/op_host/add_infershape.cpp#L23) —— `IMPL_OP_INFERSHAPE(Add).InferShape(InferShapeForAdd);` 完成注册：宏定位到名为 Add 的算子，把 `InferShapeForAdd` 挂为其 shape 推导实现。这行与 def 文件里的 `OP_ADD(Add)` 通过算子名关联——**def 负责登记存在，infershape 负责登记行为**。

值得体会的工程模式：仓库里几乎所有二元 broadcast 算子（sub、xlogy、xdivy、truncdiv 等数十个）的 infershape 都是这一个「转发到公共工具」的模板，公共逻辑集中在 CANN 侧的 `Ops::Base::InferShape4Broadcast`，避免每个算子抄一遍广播算法。u2-l3 将专门拆解 InferShape 的输入输出与自定义写法。

#### 4.3.4 代码实践

**实践：验证 (2,3,4)+(1,4) 的推导结果，并统计仓库里多少算子复用同一工具。**

1. 实践目标：确认 add 的 infershape 行为符合 broadcast 直觉；感受公共工具的复用面。
2. 操作步骤：
   - 手算：shape (2,3,4) 与 (1,4) 右对齐后，最后一维 4==4、中间 3 vs 1（广播成 3）、最高维 2 vs 缺省（保留 2），输出应为 (2,3,4)。
   - 在仓库根目录执行 `grep -rl "InferShape4Broadcast" --include=*_infershape.cpp | wc -l`，数出复用该工具的算子数量。
   - 再挑一个结果文件（如 `math/sub/op_host/sub_infershape.cpp`）打开，对比它与 add 的 infershape 是否逐行同构。
3. 需要观察的现象：grep 计数（预期为数十个量级）；sub 的 infershape 与 add 的差异应只有函数名与日志文本。
4. 预期结果：手算结果 (2,3,4)；确认「二元 broadcast 算子共用同一个推导实现」的结论。本实践的 grep/阅读部分可直接在本地完成，无需 NPU 环境。

#### 4.3.5 小练习与答案

**练习 1**：`IMPL_OP_INFERSHAPE(Add)` 是靠什么与 add_def.cpp 里的定义关联起来的？

**答案**：靠算子名。`OP_ADD(Add)` 在算子信息库登记了名为 Add 的算子，`IMPL_OP_INFERSHAPE(Add)` 按同一个名字把推导实现挂上去。名字拼错会导致注册落空、运行时找不到推导实现（链接期不一定报错，**待确认**具体报错时机）。

**练习 2**：为什么 add 不在自己文件里写广播算法，而是转调 `Ops::Base::InferShape4Broadcast`？

**答案**：广播推导是几十个二元算子的共同行为，公共实现保证所有算子规则一致、修复一处即全量生效，也把算子侧代码压缩到最小。这是本仓反复出现的「common/公共层 + 算子薄封装」分层思想（u1-l2 的 common 目录同理）。

## 5. 综合实践

**任务：给「假想算子 MyMul」写一份完整的 def + infershape 骨架，并自查。**

要求（纯源码阅读与写作，不编译）：

1. 参照 [add_def.cpp](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/op_host/add_def.cpp) 写出 `my_mul_def.cpp`：两个必选输入 x1/x2，支持 FLOAT16/BFLOAT16/FLOAT 三种同型组合，外加一组 `F16×F32→F32` 混合组合（共 4 列，注意三个 DataType 与两组 Format 列表都要等长）；`ExtendCfgInfo("opFile.value", "my_mul")`；`AddConfig` 芯片自选一款（如 ascend950）。
2. 参照 [add_infershape.cpp](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/op_host/add_infershape.cpp) 写出 `my_mul_infershape.cpp`，转调 `Ops::Base::InferShape4Broadcast`。
3. 自查三问：五个列表是否等长按位对齐？`OP_ADD(MyMul)` 与 `IMPL_OP_INFERSHAPE(MyMul)` 的名字是否一致？`opFile.value` 对应的 kernel 文件名你会命名为什么？
4. 把 4.2.4 的六点修改清单附在后面，作为「如果要把 MyMul 的 INT16 也支持上」的行动表。

预期结果：一份 60 行以内的两个文件骨架 + 自查记录。若想进一步验证，可对照 u5-l1 的 genop 脚手架（`build.sh --genop`）生成的真实文件核对风格。

## 6. 本讲小结

- def 文件是算子的「户籍登记」：`class X : public OpDef` + `OP_ADD(X)` 四段式骨架（类、输入输出、芯片配置、注册）。
- DataType/Format/UnknownShapeFormat 列表按**位置组合**：第 i 列构成一组合法的输入→输出类型组合，混合精度列表达了类型提升规则；各列表必须等长对位。
- `AICore().AddConfig("<soc>", config)` 声明支持的芯片，`ExtendCfgInfo("opFile.value", ...)` 指定 kernel 入口文件名；AICPU 通道另有一套独立 def（`this->AICPU()`）。
- 属性用 `Attr("name").AttrType(OPTIONAL).Bool(default)` 声明，与张量的 `ParamType` 是两套维度。
- 输出 shape 推导在 `_infershape.cpp` 中用 `IMPL_OP_INFERSHAPE` 注册，按算子名与 def 关联；add 等二元 broadcast 算子统一转调公共工具 `Ops::Base::InferShape4Broadcast`。
- 新增一种数据类型至少要同步 def、kernel、tiling、op_api、文档、测试六处——def 只是入口不是全部。

## 7. 下一步学习建议

- 下一讲 **u2-l3 形状推导：infershape 的实现** 将深入 `InferShapeContext` 的输入输出结构、自定义推导的写法以及 broadcast 规则的细节，可先浏览 `common/inc/op_host/infershape_reduce_util.h` 里 reduce 类算子的推导工具做预习。
- 若想先看「规格声明如何被消费」，可跳读 [math/add/op_api/aclnn_add.cpp](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/op_api/aclnn_add.cpp)（u2-l6 的主角），观察 aclnn 层如何再次做类型检查。
- 扩展阅读：`docs/zh/develop/aicore_develop_guide.md` 中关于 `SetInferShape` 迁移到独立 `_infershape.cpp` 的章节（本讲 grep 中已见其示例代码），能帮助你理解本仓为什么把 def 与 infershape 拆成两个文件。
