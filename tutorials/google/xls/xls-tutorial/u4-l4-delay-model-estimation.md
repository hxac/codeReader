# 延迟模型与估算

## 1. 本讲目标

本讲回答一个贯穿优化与代码生成的核心问题：**XLS 怎么知道一个 IR 运算「跑起来要多久」？**

学完本讲后，你应该能够：

- 说清「延迟（delay）」在 XLS 里指什么、单位是什么、为什么 HLS 必须在编译期就估算它；
- 读懂 `DelayEstimator` 这一套抽象接口，以及 `unit`、`sky130`、`asap7` 等具体模型是如何被定义、生成、注册并按名选取的；
- 理解「关键路径延迟」是如何在数据流图上用最长路径算法算出来的，以及它如何反过来决定流水线被切成几级；
- 用 `delay_info_main` 工具实际观察同一段 IR 在不同延迟模型下的关键路径差异。

本讲依赖你已经建立 XLS IR 的基本心智模型（`Package`/`Function`/`Node` 数据流图，见 u3-l1），因为我们估算的「延迟」正是作用在每一个 `Node` 之上。

## 2. 前置知识

### 2.1 为什么 HLS 需要估算延迟

硬件电路有一个软件没有的硬约束——**时钟周期**。如果你把目标频率设为 1 GHz，那么每个时钟周期只有 1000 皮秒（ps）的预算（还要再减去时钟不确定性 margin）。一个周期内串联起来的所有运算，其延迟之和必须落在这个预算里，否则电路「收不了时序（fail timing）」。

XLS 的流水线调度（见 u4-l5）正是依据延迟来把 IR 操作分到各个周期：它需要知道每个 `Node` 大约要多少 ps，才能判断「这两个操作能不能塞进同一个周期」。这份知识由**延迟模型（delay model）**提供。本讲讲的就是这个模型的来龙去脉，它是调度的**输入**。

> 关键区分（承接 u1-l5）：延迟模型喂给**调度 / 代码生成**；它和**优化 Pass（opt）**无关——opt 只做图变换，不读延迟。

### 2.2 几个 CMOS 术语（只需直觉，不必深究）

XLS 的真实延迟模型借鉴了《Logical Effort》一书对 CMOS 门延迟的建模，背后是一组直觉性的概念，后续源码会反复用到：

- **逻辑努力（logical effort, g）**：实现某个逻辑函数所需的晶体管网络规模，本质是「驱动这个门有多费劲」。
- **电气努力（electrical effort, h）**：输出端挂的负载（扇出到更多 / 更大的门），负载越大越慢。
- **寄生延迟（parasitic delay, p）**：电路里 RC 元素白白消耗的延迟，通常较小。
- **τ（tau）**：一个单位反相器（inverter）的延迟，是这套模型的「基本时间单位」。

一个门的延迟可以写成：

\[ d = (g \cdot h + p) \cdot \tau \]

XLS 的处理思路见官方文档 `docs_src/delay_estimation.md`：先通过综合工具（synthesis tool）对每种运算在一系列位宽下扫频测量，观察到延迟通常由「常数项 + 随位宽线性增长项 + 随位宽对数增长项」三类成分构成，于是对测量数据拟合一条形如：

\[ \text{delay} = a \cdot \text{bitwidth} + b \cdot \log_2(\text{bitwidth}) + c \]

的曲线，把得到的系数 \((a,b,c)\) 固化进延迟估算器。这正是本讲后面会反复见到的「回归模型（regression）」。

官方文档把这套「粗粒度、保守」的估算哲学讲得很清楚：HLS 阶段不需要、也不可能做到后端 STA（静态时序分析）那种布线后精度，它要的是「**粗糙但保守**」的上界，让用户一次迭代就能接近收时序。参见 [docs_src/delay_estimation.md:170-177](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/delay_estimation.md#L170-L177)。

## 3. 本讲源码地图

本讲涉及的关键文件都集中在 `xls/estimators/delay_model/` 与 `xls/tools/`：

| 文件 | 作用 |
| --- | --- |
| `xls/estimators/delay_model/delay_estimator.h` | 定义延迟估算的核心抽象：`DelayEstimator` 接口、各种装饰器、注册管理器 `DelayEstimatorManager`、`DelayAnnotator`。 |
| `xls/estimators/delay_model/delay_estimator.cc` | 上述抽象的实现，含逻辑努力求值、注册逻辑、关键路径到节点的最长路径计算。 |
| `xls/estimators/delay_model/delay_estimators.h/.cc` | 面向用户的便捷函数 `GetDelayEstimator(name)`、`GetStandardDelayEstimator()`，以及 `FilterNonSynth` 装饰器。 |
| `xls/estimators/delay_model/models/*.textproto` | 具体模型的数据定义：`unit`（测试用）、`sky130`、`asap7`（真实工艺）。 |
| `xls/estimators/estimator_model.proto` | 上述 textproto 的 schema，定义 `fixed`/`regression`/`bounding_box`/`logical_effort` 等估算器种类。 |
| `xls/estimators/delay_model/generate_delay_lookup.tmpl` | 把 textproto 生成成 C++ `DelayEstimator` 子类的代码模板。 |
| `xls/estimators/delay_model/build_defs.bzl` | Bazel 宏 `delay_model`，把「textproto → 生成 C++ → 注册」串成一条构建规则。 |
| `xls/estimators/delay_model/analyze_critical_path.h/.cc` | 关键路径分析：在数据流图上算最长路径。 |
| `xls/tools/delay_info_main.cc` | 命令行工具，打印某 IR 的逐节点延迟与关键路径，是本讲实践的主力工具。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 延迟估算接口**（怎么定义一个估算器）、**4.2 延迟模型选择**（`unit`/`sky130` 这些模型从哪来、怎么选）、**4.3 关键路径延迟**（单点延迟怎么聚合成一条路径的延迟）。

### 4.1 延迟估算接口

#### 4.1.1 概念说明

延迟估算的本质是一份契约：

> 给我一个 IR `Node*`，我告诉你它要多少**皮秒（picosecond, ps）**。

所有调度、关键路径分析都只依赖这一份契约，不关心你内部是用查表、拟合曲线还是逻辑努力来算。XLS 把这份契约抽成抽象基类 `DelayEstimator`，并提供一组「装饰器」来组合出实际可用的估算器（缓存、修饰、择优等）。再往上，一个 `DelayEstimatorManager` 单例按名字集中管理所有模型实例，供全程序按名取用。

这套设计的好处是：**模型可插拔**。换工艺节点（sky130 → asap7）只换模型名，调度代码一行不用改。

#### 4.1.2 核心流程

一次延迟查询的流程：

1. 代码（调度器、工具）调用 `GetDelayEstimator("sky130")` 从单例按名取出 `DelayEstimator*`。
2. 对某个 `Node*` 调用 `delay_estimator->GetOperationDelayInPs(node)`。
3. 估算器据自身策略（查表 / 回归 / 逻辑努力）返回 `int64_t` 皮秒数；若该运算不被支持则返回错误状态。
4. 调用方把单点延迟累加成路径延迟（见 4.3）。

#### 4.1.3 源码精读

**核心接口**——`DelayEstimator` 只有一个纯虚方法 `GetOperationDelayInPs`，注释明确「返回该 node 估算延迟，单位皮秒」：

[ xls/estimators/delay_model/delay_estimator.h:40-49 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/delay_estimator.h#L40-L49)

```cpp
class DelayEstimator {
 public:
  explicit DelayEstimator(std::string_view name) : name_(name) {}
  const std::string& name() const { return name_; }
  // Returns the estimated delay of the given node in picoseconds.
  virtual absl::StatusOr<int64_t> GetOperationDelayInPs(Node* node) const = 0;
```

它还提供一个静态的**逻辑努力**捷径 `GetLogicalEffortDelayInPs`，仅对 `kAnd`/`kOr`/`kOneHotSel` 等简单运算有效，支持的运算清单见 `kLogicalEffortEstimators` 数组：

[ xls/estimators/delay_model/delay_estimator.h:52-60 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/delay_estimator.h#L52-L60)

**三类装饰器**让接口可以灵活组合，而不必为每种组合写新类：

- `FirstMatchDelayEstimator`：依次问一串估算器，返回**第一个成功**的结果，适合「主模型兜不住时回落到逻辑努力」这种策略。其实现就是一个短路循环：

[ xls/estimators/delay_model/delay_estimator.cc:324-334 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/delay_estimator.cc#L324-L334)

```cpp
absl::StatusOr<int64_t> FirstMatchDelayEstimator::GetOperationDelayInPs(
    Node* node) const {
  absl::StatusOr<int64_t> result;
  for (const DelayEstimator* estimator : estimators_) {
    result = estimator->GetOperationDelayInPs(node);
    if (result.ok()) { return result; }
  }
  return result;  // 全失败则返回最后一个的错误
}
```

- `CachingDelayEstimator`：用一张 `Node*→delay` 的哈希表缓存，**带读写锁、并发安全**，避免对同一节点重复求值：

[ xls/estimators/delay_model/delay_estimator.h:101-131 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/delay_estimator.h#L101-L131)

- `DecoratingDelayEstimator`：用一个 `modifier(node, original)` 函数对底层结果做后处理（例如全局乘一个「fudge factor」放松时序）。

**注册管理器**——`DelayEstimatorManager` 是按名存取的字典，每个模型注册时带一个**优先级（precedence）**；当调用 `GetDefaultDelayEstimator()` 时，它返回**优先级最高**的那个：

[ xls/estimators/delay_model/delay_estimator.cc:91-111 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/delay_estimator.cc#L91-L111)

优先级是个简单三档枚举（`kLow=1, kMedium=2, kHigh=3`）：

[ xls/estimators/delay_model/delay_estimator.h:133-137 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/delay_estimator.h#L133-L137)

注册动作本身做了重名校验：

[ xls/estimators/delay_model/delay_estimator.cc:113-126 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/delay_estimator.cc#L113-L126)

整个进程共享一个单例管理器：

[ xls/estimators/delay_model/delay_estimator.cc:46-49 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/delay_estimator.cc#L46-L49)

**面向用户的便捷函数**——`delay_estimators.h` 把「从单例按名取」和「取默认」封装成两个自由函数：

[ xls/estimators/delay_model/delay_estimators.h:28-35 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/delay_estimators.h#L28-L35)

注意源码里有一条 `TODO`：希望将来强制用户显式指定模型名，而不是悄悄回落到「默认」。若指定的名字找不到，`GetDelayEstimator` 会给出非常详细的报错，提示你检查是否调用了 `InitXls`、是否把模型库链接进了二进制——因为模型是靠「模块初始化器」自注册的，没链接进来就不会注册。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：理解装饰器的组合方式。
2. **步骤**：打开 `xls/estimators/delay_model/delay_estimator_test.cc`，搜索 `FirstMatchDelayEstimator` 和 `CachingDelayEstimator` 的测试用例，阅读它们如何把一个返回错误的估算器与一个返回成功的估算器组合。
3. **观察**：`FirstMatch` 是如何「跳过失败、采用第一个 ok」的；`Caching` 是如何对同一 `Node*` 第二次查询直接命中缓存的。
4. **预期结果**：你能用自己的话说出「为什么调度器在反复遍历 IR 时应该包一层 `CachingDelayEstimator`」。
5. 运行结果：待本地验证（可执行 `bazel test //xls/estimators/delay_model:delay_estimator_test`）。

#### 4.1.5 小练习与答案

**练习 1**：`DelayEstimator::GetOperationDelayInPs` 返回的是 `absl::StatusOr<int64_t>` 而不是裸 `int64_t`。为什么？

> **答案**：因为并非所有估算器都支持所有运算。例如逻辑努力只覆盖 `kAnd`/`kOr` 等少数 op（见 `kLogicalEffortEstimators`），遇到不支持的运算应返回错误状态，让上层（如 `FirstMatchDelayEstimator`）能回落到别的估算器，而不是默默返回 0 造成致命的时序乐观。

**练习 2**：`GetDefaultDelayEstimator()` 在没有任何模型被显式指定时挑哪个模型？依据是什么？

> **答案**：挑注册时 `precedence` 值最大的那个（`kHigh > kMedium > kLow`）。若多个模型同优先级则取决于遍历顺序，所以真实工艺模型（`sky130`/`asap7` 用 `kMedium`）会压过测试用的 `unit`（`kLow`）成为默认——前提是它们都被链接进了二进制。

### 4.2 延迟模型选择

#### 4.2.1 概念说明

接口是「怎么问」，模型是「怎么答」。XLS 不把具体延迟写死在 C++ 里，而是用**文本 protobuf（textproto）**描述每个运算的延迟模型，再在构建期用脚本生成 C++ 代码。这样做的好处是：换工艺、调系数只需要改一个 textproto 文件并重新构建，不用碰 C++ 逻辑。

仓库里自带三个模型：

- **`unit`**：测试用的「玩具」模型，几乎所有运算都给 1 ps（少数「永远免费」的运算给 0）。它不反映真实物理，但可让调度逻辑在没有真实模型时也能跑通。
- **`sky130`**：基于 SkyWater 130nm 开源工艺（PDK）表征的真实模型。
- **`asap7`**：基于 ASU ASAP7 7nm 学术工艺的真实模型。

#### 4.2.2 核心流程

一个模型从定义到可用，经过四步：

1. **定义**：在 `models/<name>.textproto` 里为每个 `Op` 写一条 `op_models`，指定估算器种类（`fixed` / `regression` / `bounding_box` / `logical_effort` / `alias_op`）。
2. **生成**：Bazel 宏 `delay_model` 在构建期调用 `generate_delay_lookup` 脚本，依据 `.tmpl` 模板把 textproto 渲染成一段 C++（一个 `DelayEstimator` 子类 + 注册代码），并用 `clang-format` 格式化。
3. **注册**：生成的 C++ 用 `XLS_REGISTER_MODULE_INITIALIZER` 在程序启动时把自己注册进单例管理器，附带优先级。靠 `alwayslink=1` 确保这段「没有显式调用方」的注册代码不被链接器丢弃。
4. **选取**：用户在命令行用 `--delay_model=<name>` 选模型，工具调用 `GetDelayEstimator(name)` 取出。

#### 4.2.3 源码精读

**先看最简单的 `unit` 模型**。它给 `kAdd` 一个固定 1 ps：

[ xls/estimators/delay_model/models/unit.textproto:19 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/models/unit.textproto#L19)

```proto
op_models { op: "kAdd" estimator { fixed: 1 } }
```

而「永远免费」的运算（参数、字面量、寄存器读写、端口、断言等）给 0——它们要么是连线、要么是时序边界，不占组合逻辑延迟预算：

[ xls/estimators/delay_model/models/unit.textproto:84-98 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/models/unit.textproto#L84-L98)

文件末尾的 `metric: DELAY_METRIC` 声明这是个**延迟**模型（同样的 schema 也用来描述**面积**模型 `AREA_METRIC`）。

**再看真实模型 `sky130` 的 `kAdd`**。它不再是固定值，而是一个**回归（regression）**估算器，回归的自变量（factor）是 `OPERAND_BIT_COUNT`（操作数位宽）：

[ xls/estimators/delay_model/models/sky130.textproto:143-153 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/models/sky130.textproto#L143-L153)

```proto
op_models {
  op: "kAdd"
  estimator {
    regression {
      expressions { factor { source: OPERAND_BIT_COUNT } }
    }
  }
}
```

这里只声明了「延迟随位宽变化」，**真正的系数 \((a,b,c)\) 并不在 textproto 里**——它们由 `generate_delay_lookup.py` 依据文件后半部分大量的 `data_points`（实测数据点）做最小二乘拟合，再硬编码进生成的 C++。这一点很重要：textproto 写的是「模型形状（哪些维度影响延迟）」，生成器算的是「模型参数（具体系数）」。

proto schema 把回归模型的数学形式写得很清楚。对一个表达式因子 \(x\)，回归拟合形如：

\[ \text{delay\_est} = P_0 + P_1 \cdot x + P_2 \cdot \log_2 x \]

多个因子时各项叠加。参见带注释的 proto 定义：

[ xls/estimators/estimator_model.proto:201-220 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/estimator_model.proto#L201-L220)

**特化（specialization）**：同一运算在不同情形下延迟特性可能不同。最典型的例子是乘法——两个操作数相同时（即「平方」）综合出的电路通常更快。`sky130` 的 `kUMul` 因此带了一条 `OPERANDS_IDENTICAL` 特化，命中时改用基于操作数位宽的另一条回归：

[ xls/estimators/delay_model/models/sky130.textproto:226-249 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/models/sky130.textproto#L226-L249)

整套估算器种类（`Estimator` 的 `oneof`）枚举如下，共五种：

[ xls/estimators/estimator_model.proto:274-298 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/estimator_model.proto#L274-L298)

- `fixed`：固定常数（`unit` 模型即用此）。
- `alias_op`：复用另一个 op 的模型（如 `kSub` 别名到 `kAdd`）。
- `regression`：曲线拟合（真实模型主力）。
- `bounding_box`：用实测数据点张成的「包围盒」分段查表。
- `logical_effort`：交给 `DelayEstimator::GetLogicalEffortDelayInPs` 用逻辑努力算。

回归/包围盒所用的「因子」种类定义在 `EstimatorFactor.Source`：结果位宽、操作数位宽、操作数个数、数组元素数等，都可作自变量：

[ xls/estimators/estimator_model.proto:113-141 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/estimator_model.proto#L113-L141)

**代码生成**：模板 `generate_delay_lookup.tmpl` 把 textproto 渲染成一个 `DelayEstimator` 子类，核心就是一个 `switch(node->op())` 分派到为每个 op 生成的估算函数，并把负值夹到 0：

[ xls/estimators/delay_model/generate_delay_lookup.tmpl:28-51 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/generate_delay_lookup.tmpl#L28-L51)

生成代码末尾用模块初始化器完成**自注册**，并传入优先级：

[ xls/estimators/delay_model/generate_delay_lookup.tmpl:53-59 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/generate_delay_lookup.tmpl#L53-L59)

**Bazel 宏** `delay_model` 把这条「textproto → 生成 .cc → cc_library」流水线封装好，关键是用 `genrule_wrapper` 调脚本生成源码、用 `alwayslink = 1` 保证注册代码被链接进来：

[ xls/estimators/delay_model/build_defs.bzl:45-58 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/build_defs.bzl#L45-L58)

最后，三个模型各自的目标与优先级（注意 `unit` 是 `kLow`，真实模型是 `kMedium`）：

[ xls/estimators/delay_model/models/BUILD:46-65 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/models/BUILD#L46-L65)

#### 4.2.4 代码实践

1. **目标**：亲手对比「玩具模型」与「真实模型」给出的延迟形状。
2. **步骤**：用任意编辑器打开 `unit.textproto` 与 `sky130.textproto` 并排比较。在 `sky130` 中分别找 `kAnd`、`kUMul` 两条，记录它们各自用了哪种估算器、哪些因子。
3. **观察**：`unit` 里 `kAnd` 是 `fixed: 1`，`kUMul` 也是 `fixed: 1`——所有运算一视同仁；而 `sky130` 里 `kAnd` 用 `OPERAND_COUNT` 回归、`kUMul` 用 `RESULT_BIT_COUNT` 回归并带 `OPERANDS_IDENTICAL` 特化，体现了「按位与几乎不随位宽变慢，而乘法随位宽显著变慢」这一物理事实。
4. **预期结果**：你能解释为什么用 `unit` 模型调度出来的流水线级数不能代表真实时序——它把乘法和按位与当成同等昂贵。
5. 运行结果：纯阅读，无需执行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `kSub` 在 `sky130` 里可以写成 `kAdd` 的 `alias_op`？这种别名在生成 C++ 时会如何体现？

> **答案**：因为减法器与加法器在综合后底层都是「进位链」结构，延迟特性几乎相同，没必要单独表征。生成器会让 `kSub` 的估算函数复用 `kAdd` 的回归曲线（共享同一组拟合系数）。

**练习 2**：如果把一个全新的 `Op` 加进 IR，但忘了在 `sky130.textproto` 里给它写 `op_models`，会发生什么？

> **答案**：生成的 `GetOperationDelayInPs` 里 `switch` 会落到 `default` 分支，返回 `absl::UnimplementedError`（见 `.tmpl` 第 41-44 行）。这个错误会被 `FirstMatchDelayEstimator` 之类的上层尝试回落，若无回落则一路冒泡成调度失败——提示你该补模型了。

### 4.3 关键路径延迟

#### 4.3.1 概念说明

单点延迟回答「这一个运算要多久」，但调度真正关心的是**一条数据依赖链上累积的总延迟**——也就是**关键路径（critical path）**。

由于 XLS IR 是一张有向无环的数据流图（`Node` 之间靠 operands/users 互连，见 u3-l1），从输入到输出存在多条路径，每条路径的延迟等于路径上各节点延迟之和。**关键路径**就是其中延迟最大的那条；它的总延迟决定了这个函数「最快也得多少 ps 才能算完」。

数学上，对每个节点 \(n\) 定义其**路径延迟**（从某个起点到 \(n\) 的最大累积延迟）：

\[ D_{\text{path}}(n) = \text{delay}(n) + \max_{p \,\in\, \text{operands}(n)} D_{\text{path}}(p) \]

关键路径总延迟就是所有节点里最大的 \(D_{\text{path}}(n)\)。这正是经典的「DAG 最长路径」问题，由于图无环，用一次拓扑排序即可在 \(O(V+E)\) 内解决。

#### 4.3.2 核心流程

`AnalyzeCriticalPath` 的执行过程：

1. 对整个 `FunctionBase` 做拓扑排序，保证处理某节点时它的所有操作数都已处理完。
2. 按拓扑序遍历：对节点 \(n\)，取所有操作数路径延迟的最大值 `max_path_delay`，加上 `n` 自身延迟，得到 `n` 的 `critical_path_delay`，并记下贡献最大的那个操作数作为「前驱」。
3. 可选地传入 `clock_period_ps`：若一条依赖跨越了时钟周期边界，则把 `max_path_delay` 向上取整到周期整数倍，模拟「值要等到下个周期时钟沿才可用」——这会让路径延迟变长，并在该节点上打 `delayed_by_cycle_boundary` 标记。
4. 维护「全局最大」节点。遍历结束后，从该节点沿「前驱」指针一路回溯，即得到关键路径序列（返回值的前端是路径终点，即函数返回值或 proc 状态）。

#### 4.3.3 源码精读

**数据结构** `CriticalPathEntry` 描述路径上的一个节点，含三档延迟信息：本节点延迟、从路径起点算起的累积延迟、是否被周期边界推迟：

[ xls/estimators/delay_model/analyze_critical_path.h:37-51 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/analyze_critical_path.h#L37-L51)

**最长路径主循环**——拓扑序遍历，对每个节点取操作数路径延迟的最大值作前驱，再加上自身延迟：

[ xls/estimators/delay_model/analyze_critical_path.cc:77-117 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/analyze_critical_path.cc#L77-L117)

其中关键几行（精简后）：

```cpp
int64_t max_path_delay = 0;
for (Node* operand : node->operands()) {
  int64_t operand_path_delay = node_entries.at(operand).critical_path_delay;
  if (operand_path_delay >= max_path_delay) {
    max_path_delay = operand_path_delay;
    entry.critical_path_predecessor = operand;  // 记前驱
  }
}
entry.node_delay = delay_estimator.GetOperationDelayInPs(node).value();
// ...（可选的周期边界处理，见下）...
entry.critical_path_delay = max_path_delay + entry.node_delay;
```

**周期边界处理**——当传入 `clock_period_ps` 时，若 \(\lfloor(\text{max\_path\_delay}+\text{node\_delay})/\text{period}\rfloor > \lfloor\text{max\_path\_delay}/\text{period}\rfloor\)，说明这条依赖跨越了周期边界，于是把累积起点抬到下一个周期沿：

[ xls/estimators/delay_model/analyze_critical_path.cc:107-116 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/analyze_critical_path.cc#L107-L116)

**回溯重建路径**——从「全局最大」节点出发，沿 `critical_path_predecessor` 链表逐个回溯，组装成有序的 `CriticalPathEntry` 序列：

[ xls/estimators/delay_model/analyze_critical_path.cc:136-148 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/analyze_critical_path.cc#L136-L148)

**两个衍生的便捷封装**：

- `CriticalPathToString` 把路径打印成 `总延迟ps (+本节点ps): 节点描述` 的逐行格式（本讲实践会用到它）。
- `DelayAnnotator::Create` 是「不带周期边界」的简化版最长路径，把每个节点的 `[path_delay (+node_delay)]` 作为后缀标注到 IR 文本上，实现如下（同样是拓扑序 + 取操作数最大值 + 加自身延迟）：

[ xls/estimators/delay_model/delay_estimator.cc:357-375 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/estimators/delay_model/delay_estimator.cc#L357-L375)

这条「单点延迟 → 路径延迟」的公式，正是调度器（u4-l5）判断「这一串运算能否塞进一个时钟周期」的依据：若某条关键路径延迟 > 周期预算，调度器就必须把它们切到不同流水线级。**关键路径延迟越大，需要的流水线级数越多。**

#### 4.3.4 代码实践

1. **目标**：理解周期边界对关键路径延迟的影响。
2. **步骤**：阅读 `xls/estimators/delay_model/analyze_critical_path_test.cc`，找带 `clock_period_ps` 参数的测试用例。
3. **观察**：同一张图，传 `clock_period_ps` 与不传，关键路径总延迟有何不同；哪些节点被打上 `delayed_by_cycle_boundary`。
4. **预期结果**：你能解释「为什么加上周期边界后，路径总延迟可能变大」——因为跨周期的值要等到下个时钟沿。
5. 运行结果：待本地验证（可执行 `bazel test //xls/estimators/delay_model:analyze_critical_path_test`）。

#### 4.3.5 小练习与答案

**练习 1**：`AnalyzeCriticalPath` 返回的 `vector` 里，**第一个元素**是路径的起点还是终点？

> **答案**：是终点（路径延迟最大的那个节点，通常是函数返回值或 proc 的下一状态）。因为算法是先找到「全局最大」节点，再沿前驱指针**反向回溯**压入 `vector`，所以回溯完成时终点在最前。这与官方工具输出「先打印总延迟最大的行」一致。

**练习 2**：若一张 IR 图里所有节点延迟都相等（如 `unit` 模型下大部分都是 1 ps），关键路径总延迟在数值上等于什么？

> **答案**：等于关键路径上的**节点数**（起点算 0 ps 的 param/literal 不计入）。这正是 `unit` 模型便于测试的原因——它把「延迟」退化成「图深度」，让调度逻辑可以脱离真实物理被单独验证。

## 5. 综合实践

把三个模块串起来：用 `delay_info_main` 工具，亲手对比同一段 IR 在 **`unit` 玩具模型**与 **`sky130` 真实模型**下的关键路径延迟，体会「模型选择如何影响对时序的判断」。

### 5.1 准备一段小 IR

新建 `/tmp/delay_demo.ir`，内容是一个「先乘后取反」的小函数（乘法在真实工艺里很慢，便于观察差异）：

```
package delay_demo

top fn delay_demo(x: bits[32], y: bits[32]) -> bits[32] {
  p: bits[32] = umul(x, y)
  ret n: bits[32] = not(p)
}
```

> 这是**示例代码**（为演示手写的 IR 文本，非仓库自带文件）。注意 `delay_info_main` 期望输入是已经过 `opt_main` 的 IR；这段极简 IR 本身已是最简形式，可直接使用。

### 5.2 操作步骤

先构建工具（一次性，耗时较长；见 u1-l2 的构建说明）：

```bash
bazel build -c opt //xls/tools:delay_info_main
```

> 该目标的依赖里包含了 `//xls/estimators/delay_model/models`，因此 `unit`/`sky130`/`asap7` 三个模型都会被链接进来、可按名取用。

然后用 `unit` 模型查看延迟：

```bash
./bazel-bin/xls/tools/delay_info_main --delay_model=unit /tmp/delay_demo.ir
```

再用 `sky130` 真实模型查看：

```bash
./bazel-bin/xls/tools/delay_info_main --delay_model=sky130 /tmp/delay_demo.ir
```

> 工具用法与输出格式见其 usage：[ xls/tools/delay_info_main.cc:32-46 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/delay_info_main.cc#L32-L46)。它的 `RealMain` 只是创建一个 printer 并生成报告：[ xls/tools/delay_info_main.cc:51-56 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/delay_info_main.cc#L51-L56)。

### 5.3 需要观察的现象

- **`unit` 模型**：输出会显示 `p(umul)` 与 `n(not)` 各 1 ps，`x`/`y`(param) 各 0 ps，关键路径形如 `2ps (+  1ps): n ...` 然后 `1ps (+  1ps): p ...`。即「总延迟 = 路径节点数 = 2」。可对照官方测试断言的格式（逐行 `总延迟ps (+本节点ps): 节点`）：

  [ xls/tools/delay_info_main_test.py:64-81 ](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/delay_info_main_test.py#L64-L81)

- **`sky130` 模型**：`umul` 的延迟会远大于 `not`（乘法在 130nm 工艺下是进位 Wallace 树，延迟随位宽显著增长），关键路径总延迟会是几百到上千 ps 量级，且 `umul` 那一行贡献了绝大部分。

### 5.4 预期结论

- 同一段 IR、同一个调度算法，**换模型 = 换对时序的判断**：`unit` 把乘法当 1 ps，会严重低估时序；`sky130` 揭示乘法才是瓶颈。
- 这说明为什么真实设计必须用真实工艺模型调度，而 `unit` 只适合在测试里验证调度算法本身。
- 若你额外用 `--clock_period_ps=<某值>` 配合 `--schedule` 跑一遍，还会看到周期边界对路径延迟的抬升，以及随之改变的流水线级数。

### 5.5 运行结果

具体数值**待本地验证**（取决于实际构建出的 `sky130` 拟合系数）。重点是对比两次输出的**相对差异**，而非绝对数值。

## 6. 本讲小结

- **延迟（delay）是调度与代码生成的输入**：它告诉调度器每个 IR `Node` 大约要多少皮秒，从而决定流水线怎么切；它和优化 Pass（opt）无关。
- **`DelayEstimator` 是统一接口**：核心契约是 `GetOperationDelayInPs(Node*) -> int64_t ps`；`FirstMatch`/`Caching`/`Decorating` 等装饰器负责组合，`DelayEstimatorManager` 单例按名 + 优先级集中管理所有模型实例。
- **具体模型由 textproto 定义、构建期生成 C++**：`unit`（全 1 ps，测试用）、`sky130`/`asap7`（真实工艺，回归拟合）共用 `estimator_model.proto` schema；系数由 `generate_delay_lookup` 依据实测 `data_points` 拟合并硬编码，靠 `alwayslink` + 模块初始化器自注册。
- **回归模型的数学形式**：\(\text{delay} = P_0 + \sum_i (P_{i,1}\cdot x_i + P_{i,2}\cdot\log_2 x_i)\)，自变量 \(x_i\) 是位宽、操作数个数等因子；乘法等运算还支持 `OPERANDS_IDENTICAL` 等特化。
- **关键路径 = DAG 最长路径**：`AnalyzeCriticalPath` 用一次拓扑排序，对每节点取「操作数路径延迟最大值 + 自身延迟」；可选的 `clock_period_ps` 会把跨周期依赖抬到下个时钟沿，从而拉长路径延迟。
- **模型选择直接改变时序判断**：`unit` 把所有运算一视同仁，`sky130` 反映真实物理；可用 `delay_info_main --delay_model=<name>` 直观对比。

## 7. 下一步学习建议

- **进入 u4-l5「流水线调度」**：看调度器如何**消费**本讲产出的延迟——把「关键路径延迟 vs 时钟预算」翻译成「哪些操作分到哪个流水线级」，并理解 ASAP 与最小割两种调度策略。
- **延伸阅读**：官方 `docs_src/delay_estimation.md` 全文，尤其「Sources of pessimism/optimism」与「Iterative refinement」两节，理解 XLS 如何用「综合工具在环」迭代修正模型（即文档里的 `p0, p1, p2...` 预测序列）。
- **源码深挖**：若你对模型系数如何从测量数据拟合而来感兴趣，可读 `xls/estimators/delay_model/generate_delay_lookup.py`；若关心逻辑努力的细节，可读 `delay_estimator.cc` 中的 `GetLogicalEffortDelayInTau` 及其依赖 `xls/netlist/logical_effort.h`。
- **面积模型对照**：`xls/estimators/area_model/` 用的是同一套 proto schema（`AREA_METRIC`），接口设计与本讲高度同构，可作为对照练习。
