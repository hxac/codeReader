# 算子规格说明书怎么读：以 add 算子 README 为例

## 1. 本讲目标

学完本讲，你应该能够：

- 读懂任意算子 README 中的四大板块：产品支持情况、功能说明、参数说明、调用说明。
- 理解「算子级支持」与「aclnn 接口级支持」的区别，避免掉进「README 说支持、接口文档说不支持」的坑。
- 学会从 aclnn 接口文档中提取函数原型、参数约束、错误码和调用示例。
- 掌握一套「拿到一个陌生算子，5 分钟搞清它能不能用、怎么用」的通用方法。

## 2. 前置知识

在阅读本讲前，你需要了解以下概念（均在单元一讲义中出现过，这里做简要回顾）：

- **算子（Op）**：NPU 上执行的最小计算单元，本仓中一个文件夹就是一个算子（如 `math/add/`）。
- **aclnn API**：Host 侧（CPU 侧）基于 C 语言的算子调用接口，以 `aclnn` 为前缀，例如 `aclnnAdd`。它是最常用的算子调用方式。
- **两段式接口**：aclnn 接口分为两段——第一段 `xxxGetWorkspaceSize` 做入参校验并返回执行器，第二段 `xxx` 真正下发计算任务。在 u1-l4 中你已经用过一次。
- **数据类型**：张量中元素的类型，如 FLOAT（即 float32）、FLOAT16、BFLOAT16、INT32 等。文档中常用 `ACL_FLOAT(f32)`、`ACL_FLOAT16(f16)` 这类简写。
- **产品型号**：昇腾硬件的系列名，如 Atlas A2 训练系列（910b）、Atlas A3 系列、Ascend 950PR 等。同一算子在不同硬件上的支持情况可能不同。

本讲不需要写代码，是一讲「读文档」的方法课——但读文档的能力决定了你后续能否快速使用和维护几百个算子。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [math/add/README.md](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/README.md) | add 算子的规格说明书，本讲的主解剖对象 |
| [math/add/docs/aclnnAdd&aclnnInplaceAdd.md](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/docs/aclnnAdd%26aclnnInplaceAdd.md) | aclnnAdd / aclnnInplaceAdd 两个用户接口的详细文档 |
| [docs/zh/op_api_list.md](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/op_api_list.md) | 全仓 aclnn 接口总索引，查算子接口的入口 |
| [docs/zh/context/deduction_relationship.md](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/context/deduction_relationship.md) | 数据类型互推导规则表，回答「两个输入类型不一致时怎么办」的权威依据 |

> 说明：规格中提到的 `math/add/docs/aclnnAdd.md` 在仓库中的实际文件名是 `math/add/docs/aclnnAdd&aclnnInplaceAdd.md`（一个文档同时描述 aclnnAdd 与 aclnnInplaceAdd 两个接口），本讲按真实文件名引用。

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**算子 README**、**产品支持表**、**aclnn 接口文档**。

### 4.1 算子 README：一个算子的「身份证 + 说明书」

#### 4.1.1 概念说明

本仓中每个算子目录下都有一个 `README.md`，它是算子的第一手资料，结构高度统一，固定包含四个板块：

1. **产品支持情况**——哪些硬件支持这个算子。
2. **功能说明**——算子做什么，计算公式是什么。
3. **参数说明**——输入/输出/属性的名字、数据类型、格式。
4. **调用说明**——有哪些调用方式，每种方式对应的样例代码在哪里。

为什么要有统一结构？因为本仓有 300 多个算子，统一的 README 结构意味着你只要会读一个，就会读所有。它既是使用者的查询入口，也是后续贡献新算子时必须补齐的文档规范。

#### 4.1.2 核心流程

拿到一个陌生算子，推荐的阅读流程：

```text
打开 <算子目录>/README.md
  ├─ 1. 看「产品支持情况」表 → 我的硬件在不在列表里？支持（√）还是不支持（×）？
  ├─ 2. 看「功能说明」的计算公式 → 它算的是什么？
  ├─ 3. 看「参数说明」表 → 输入输出叫什么名字？支持哪些数据类型？什么格式？
  └─ 4. 看「调用说明」表 → 我打算用哪种调用方式？跳到对应样例和接口文档
```

#### 4.1.3 源码精读

**① 功能说明与计算公式**

[math/add/README.md:14-22](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/README.md#L14-L22) 给出了算子功能的定义和计算公式 \( y = x_1 + x_2 \)。

注意一个细节：README 里的公式是最简形式 \( y = x_1 + x_2 \)，而 aclnn 接口文档里的公式是 \( out_i = self_i + \alpha \times other_i \)（见 4.3 节）。README 描述「算子本质」，接口文档描述「API 层暴露的完整能力」——aclnn 层多了一个 `alpha` 标量系数。读文档时要意识到这两个视角的差别。

**② 参数说明表**

[math/add/README.md:24-63](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/README.md#L24-L63) 是参数表，三行分别是 x1（输入）、x2（输入）、y（输出），共五列：

| 列名 | 含义 | add 的取值 |
|------|------|-----------|
| 参数名 | 张量在公式中的名字 | x1 / x2 / y |
| 输入/输出/属性 | 参数角色 | 输入 / 输入 / 输出 |
| 描述 | 与公式的对应关系 | 「公式中的输入张量x_1」等 |
| 数据类型 | 支持的元素类型 | BOOL, INT8, INT16, INT32, INT64, UINT8, FLOAT64, FLOAT16, BFLOAT16, FLOAT32, COMPLEX128, COMPLEX64, COMPLEX32, STRING |
| 数据格式 | 张量内存布局 | ND（任意维度常规布局） |

「同x1」是常见缩写，表示该参数类型必须与 x1 一致。另外注意：**STRING 出现在算子级类型列表中，但并不等于每个 aclnn 接口都支持 STRING**——接口级支持以 aclnn 文档为准（对比 4.3 节的接口参数表，里面没有 STRING）。这印证了「算子级」和「接口级」是两层规格。

**③ 调用说明表**

[math/add/README.md:65-71](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/README.md#L65-L71) 列出两种调用方式：

- **aclnn 调用**：样例 [math/add/examples/test_aclnn_add.cpp](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/examples/test_aclnn_add.cpp)，接口文档 `docs/aclnnAdd&aclnnInplaceAdd.md`。
- **图模式调用**：样例 [math/add/examples/test_geir_add.cpp](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/examples/test_geir_add.cpp)，通过算子 IR（`op_graph/add_proto.h`）构图调用。

这一栏实际上就是「调用方式 → 样例 + 接口文档」的路由表。回忆 u1-l2 的结论：如果一个算子目录缺 `op_api`，README 里就不会有 aclnn 调用这一行。

#### 4.1.4 代码实践

**实践目标**：独立读懂一个新算子的 README。

**操作步骤**：

1. 在 `conversion/`、`math/`、`random/` 三个目录中各任选一个算子（不要选 add）。
2. 打开各自的 README.md，按 4.1.2 的流程依次找出：产品支持表、计算公式、参数表、调用说明。
3. 用一句话概括每个算子的功能，并抄下每个算子的输入参数支持的数据类型列表。

**需要观察的现象**：三个 README 的章节标题、顺序是否完全一致？参数表的列名是否相同？

**预期结果**：章节结构完全一致（产品支持情况 → 功能说明 → 参数说明 → 调用说明），列名一致。这验证了「会读一个就会读所有」。本实践为纯源码阅读，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：README 参数表中 x2 的数据类型写的是「同x1」，这句话的准确含义是什么？如果 x1 是 FLOAT16，x2 可以是 FLOAT 吗？

**答案**：在算子规格层面，x2 的类型必须与 x1 相同。但通过 aclnn 接口调用时，接口层允许 x1/x2 类型不一致——接口内部会按互推导规则统一成一个类型再计算（见 4.3 节和 deduction_relationship.md）。所以「同x1」约束的是算子定义层，接口层则由类型推导规则接管。

**练习 2**：add 的 README 中 y（输出）的数据类型为什么是「同x1」而不是独立的一列类型？

**答案**：因为 add 是逐元素运算，输出的元素类型由输入推导决定（与输入推导后的类型一致），不存在独立指定输出类型的自由度。对比图模式或某些带 out 参数的接口，输出类型还须满足「可转换」约束（见 aclnn 文档中 out 参数的说明）。

### 4.2 产品支持表：先查表，再动手

#### 4.2.1 概念说明

每个 README 的第一节都是「产品支持情况」表，列出各硬件产品对算子的支持与否（√/×）。这张表存在的原因很现实：**不同代际昇腾芯片的 AI Core 架构不同，算子的 kernel 需要按架构分别实现**（u1-l2 中讲过的 arch35 目录就是架构适配的体现），所以「算子支不支持」永远是「算子 × 芯片」的二维问题。

更关键的一点：**算子级支持表和 aclnn 接口级支持表可能不一致**，下面对比看。

#### 4.2.2 核心流程

使用一个算子前的检查链：

```text
我的硬件型号（如 910b / 310p / 950）
  ├─ 查算子 README 的产品支持表 → 算子在该硬件上是否实现
  └─ 查具体 aclnn 接口文档的支持列表 → 该调用路径是否可用
        └─ 再查接口文档内的「型号特例」注释（如某型号不支持某数据类型）
```

任何一环是 ×，都意味着这条调用路径走不通，需要换调用方式（如改用图模式）或换接口版本（如 AddV3，见 4.3.1）。

#### 4.2.3 源码精读

**① 算子级支持表**

[math/add/README.md:3-12](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/README.md#L3-L12) 显示 add 算子支持：Ascend 950PR/950DT（√）、Atlas A3 系列（√）、Atlas A2 系列（√）、Atlas 200I/500 A2（×）、Atlas 推理系列（√）、Atlas 训练系列（√）。

**② 接口级支持表**

[math/add/docs/aclnnAdd&aclnnInplaceAdd.md:5-24](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/docs/aclnnAdd%26aclnnInplaceAdd.md#L5-L24) 显示 aclnnAdd 接口的支持情况：950PR/DT 支持、A3 支持、A2（910b）支持、200I/500 A2（310b）**不支持**、Atlas 推理系列（310p）**不支持**、Atlas 训练系列（910）支持。

**对比结论**：README 说「Atlas 推理系列产品 √」，而 aclnnAdd 接口文档说 310p「不支持」——两者并不矛盾。算子在 310p 上有实现（可经图模式等路径调用），但 `aclnnAdd` 这条 Host 侧单算子调用路径在 310p 上不可用。这就是为什么 4.2.2 的检查链要查两层表。

**③ 型号特例注释**

[math/add/docs/aclnnAdd&aclnnInplaceAdd.md:183-185](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/docs/aclnnAdd%26aclnnInplaceAdd.md#L183-L185) 是接口文档中嵌在参数表之后的型号特例说明：`Atlas 训练系列产品：不支持BFLOAT16数据类型`。也就是说即便接口整体可用，某些数据类型在特定型号上仍被排除。这类注释在文档中以 `<!-- npu="910" -->` HTML 注释包围，渲染后按读者所用芯片动态显示，源码里则全部可见——**读原始 Markdown 时务必把所有型号的特例注释都看一遍**。

#### 4.2.4 代码实践

**实践目标**：体验「算子级支持 ≠ 接口级支持」。

**操作步骤**：

1. 读 [math/add/README.md:3-12](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/README.md#L3-L12)，记录 6 个产品在算子级的支持情况。
2. 读 [math/add/docs/aclnnAdd&aclnnInplaceAdd.md:5-24](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/docs/aclnnAdd%26aclnnInplaceAdd.md#L5-L24)，记录同一批产品在 aclnnAdd 接口级的支持情况。
3. 把两张表并排画出来，标出所有不一致的行。

**需要观察的现象**：哪些产品在两张表中的结论不同？

**预期结果**：Atlas 推理系列产品（310p）在算子级为 √、在 aclnnAdd 接口级为「不支持」，至少存在这一处差异。本实践无需运行环境，纯文档比对。

#### 4.2.5 小练习与答案

**练习 1**：如果你的设备是 Atlas 推理系列产品（310p），想使用 add 算子，README 的调用说明表里还有哪条路可能走得通？

**答案**：图模式调用。README 调用说明中列出了 `test_geir_add.cpp` 样例与算子 IR（`op_graph/add_proto.h`），图模式路径与 aclnn 路径的支持范围独立。是否真正可用还需以图模式相关文档/实测为准（待本地验证）。

**练习 2**：为什么接口文档中要用 HTML 注释（`<!-- npu="910" -->`）包裹型号特例，而不是普通文字？

**答案**：这些文档会发布到官网并按读者选择的芯片型号动态渲染，注释标记让渲染系统知道「这段特例说明只对某个型号显示」。但在仓库里读原始 Markdown 时所有型号的特例都可见，因此源码阅读者反而能看到最全的约束信息。

### 4.3 aclnn 接口文档：从算子到用户 API 的完整规格

#### 4.3.1 概念说明

算子 README 之下，每个 aclnn 接口还有一份更细的文档（位于 `<算子>/docs/` 目录），它面向 API 使用者，包含 README 没有的内容：

- **函数原型**：C 函数签名，即你在代码里真正要调用的东西。
- **逐参数使用说明**：包括约束（类型推导、broadcast、维度上限、是否支持非连续 Tensor）。
- **错误码表**：入参校验失败时报什么错、什么原因。
- **完整调用示例**：可直接编译运行的样例代码。

查接口文档的入口是 [docs/zh/op_api_list.md](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/op_api_list.md)——全仓 aclnn 接口总表。以 add 为例，一个算子可以派生出**多个接口**：

| 接口 | 特点 |
|------|------|
| aclnnAdd & aclnnInplaceAdd | 标准版；Inplace 变体直接把结果写回输入内存，省一次申请 |
| aclnnAdds | 标量版本（Tensor + Scalar） |
| aclnnAddV3 & aclnnInplaceAddV3 | V3 演进版本 |

[docs/zh/op_api_list.md:16-18](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/op_api_list.md#L16-L18) 明确了 V 版本使用原则：**存在多个 V 版本时选最高 V 版本即可，高版本兼容低版本全部能力**。总表的每一行链接到对应接口文档，如 [docs/zh/op_api_list.md:37](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/op_api_list.md#L37) 的 aclnnAdd 行。

#### 4.3.2 核心流程

aclnn 接口文档的标准阅读顺序：

```text
打开 <算子>/docs/aclnn<Name>.md
  ├─ 1. 产品支持情况 → 接口在我的芯片上是否可用
  ├─ 2. 功能说明 + 计算公式 → API 层的完整语义（可能比 README 多参数）
  ├─ 3. 函数原型 → 两段式接口签名，确认参数个数与类型
  ├─ 4. 第一段接口参数表 → 每个张量的类型/格式/维度/非连续约束
  ├─ 5. 错误码表 → 校验失败时的行为
  └─ 6. 调用示例 → 七步固定骨架（u1-l4 已学）
```

其中最容易被忽略的是第 4 步参数表中的**使用说明列**，它记载了类型推导、broadcast、维度上限等隐含规则：

- 类型推导规则（deduction）：两输入类型不一致时内部如何统一。
- broadcast 关系：两输入 shape 必须可广播，输出 shape 必须等于广播结果。
- 维度上限：add 是「不超过 8 维」。
- 非连续 Tensor：参数表中单独一列，add 的张量参数均标注 √（支持）。

类型推导的权威规则在 [docs/zh/context/deduction_relationship.md:19-43](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/context/deduction_relationship.md#L19-L43) 的推导表中，原理类似 PyTorch 的 Type Promotion：查表行为一个输入类型、列为另一个输入类型，交叉格即推导结果，× 表示不可推导。

#### 4.3.3 源码精读

**① 函数原型与两段式接口**

[math/add/docs/aclnnAdd&aclnnInplaceAdd.md:35-77](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/docs/aclnnAdd%26aclnnInplaceAdd.md#L35-L77) 给出 4 个函数原型，核心是前两个：

```cpp
aclnnStatus aclnnAddGetWorkspaceSize(
  const aclTensor* self, const aclTensor* other, const aclScalar* alpha,
  aclTensor* out, uint64_t* workspaceSize, aclOpExecutor** executor)

aclnnStatus aclnnAdd(
  void* workspace, uint64_t workspaceSize,
  aclOpExecutor* executor, aclrtStream stream)
```

第一段接收业务参数（self/other/alpha/out），完成校验并产出 `workspaceSize` 与 `executor`；第二段只携带执行所需的资源句柄。这正是 u1-l4 中你实践过的七步骨架的文档化描述。文档还说明了 aclnnAdd 与 aclnnInplaceAdd 的区别：前者需要新建输出张量，后者直接写回输入内存。

**② 第一段接口参数表的关键约束**

[math/add/docs/aclnnAdd&aclnnInplaceAdd.md:105-159](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/docs/aclnnAdd%26aclnnInplaceAdd.md#L105-L159) 定义了 self/other/alpha/out 四个参数。要点：

- self/other 类型列表为 FLOAT、FLOAT16、DOUBLE、INT32、INT64、INT16、INT8、UINT8、BOOL、COMPLEX128、COMPLEX64、BFLOAT16（注意没有 README 算子级列表中的 STRING 和 COMPLEX32）。
- shape 需满足 broadcast 关系；维度不超过 8；均支持非连续 Tensor（√）。
- out 的类型须是 self 与 other 推导后**可转换**的类型，shape 须等于 broadcast 之后的 shape。

**③ 错误码表**

[math/add/docs/aclnnAdd&aclnnInplaceAdd.md:187-234](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/docs/aclnnAdd%26aclnnInplaceAdd.md#L187-L234) 列出第一段接口的全部校验失败场景：空指针报 `ACLNN_ERR_PARAM_NULLPTR`（161001）；类型不支持、类型不可推导、推导结果不可转换到 out、shape 不可 broadcast、out shape 不是 broadcast 结果、alpha 不可转换、维度大于 8，统一报 `ACLNN_ERR_PARAM_INVALID`（161002）。排查调用失败时，这张表就是错误对照清单。

**④ 约束说明与调用示例**

[math/add/docs/aclnnAdd&aclnnInplaceAdd.md:464-467](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/docs/aclnnAdd%26aclnnInplaceAdd.md#L464-L467) 的约束说明指出 aclnnAdd 默认确定性实现（多次运行结果一致）；[调用示例](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/docs/aclnnAdd%26aclnnInplaceAdd.md#L469-L649) 则是一份完整可编译的 main 函数，其骨架与 u1-l4 的 AddExample 七步完全一致，此处不再展开。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：总结 add 算子的数据类型支持，并回答混合类型输入的输出类型问题。

**操作步骤**：

1. 读 [math/add/README.md:42-63](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/README.md#L42-L63)，抄录算子级数据类型列表。
2. 读 [math/add/docs/aclnnAdd&aclnnInplaceAdd.md:105-159](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/docs/aclnnAdd%26aclnnInplaceAdd.md#L105-L159)，抄录 aclnnAdd 接口级类型列表，并与步骤 1 对比，找出差集。
3. 回答问题：输入 x1 为 FLOAT16、x2 为 FLOAT 时，输出类型是什么？查 [docs/zh/context/deduction_relationship.md:19-43](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/context/deduction_relationship.md#L19-L43) 的推导表给出依据。
4.（可选，需 NPU 环境）修改 [math/add/examples/test_aclnn_add.cpp](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/examples/test_aclnn_add.cpp)，把 self 构造为 ACL_FLOAT16、other 构造为 ACL_FLOAT、out 构造为 ACL_FLOAT，重新编译运行，核对是否成功执行；若把 out 改为 ACL_FLOAT16 再观察是否报 161002。

**需要观察的现象**（步骤 4）：混合类型输入是否被接口接受；out 类型与推导结果不一致时第一段接口是否返回错误。

**预期结果**：

- 算子级类型列表：BOOL、INT8、INT16、INT32、INT64、UINT8、FLOAT64、FLOAT16、BFLOAT16、FLOAT32、COMPLEX128、COMPLEX64、COMPLEX32、STRING（共 14 种）；接口级列表比它少 STRING 和 COMPLEX32（共 12 种）。
- **问题答案：输出类型为 FLOAT（float32）**。推导表中 f16 行与 f32 列的交叉格是 f32——接口内部会把 FLOAT16 的输入提升为 FLOAT32 再计算，文档 [deduction_relationship.md:42](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/docs/zh/context/deduction_relationship.md#L42) 的示例原文即为「一个为float16，一个为float32，API内部就会将float16的数据类型转换成float32的数据类型然后进行计算」。
- 步骤 4 的运行验证：待本地验证（无 NPU 环境时以文档推导为准）。

#### 4.3.5 小练习与答案

**练习 1**：调用 `aclnnAdd` 时 x1 是 BFLOAT16、x2 是 INT8，输出应是什么类型？如果 x1 是 INT8、x2 是 UINT16 呢？

**答案**：查推导表：bf16 行 × s8 列 = **f32**。第二种情况查 s8 行 × u16 列 = **×**，即 INT8 与 UINT16 不可推导，第一段接口会报 `ACLNN_ERR_PARAM_INVALID`（161002）中「无法做数据类型推导」的场景。

**练习 2**：aclnnAdd 与 aclnnInplaceAdd 功能相同，什么场景应选 Inplace 版本？

**答案**：当输入张量在计算后不再需要保留原值、且想省去一次输出内存申请与写入时，选 aclnnInplaceAdd，结果直接写回 self 的内存。代价是原输入数据被覆盖。注意 Inplace 版本额外要求 broadcast 后的 shape 必须等于 selfRef 的 shape（因为输出要写回 selfRef，见接口文档 selfRef 参数说明）。

**练习 3**：为什么同一个 add 算子在 docs/ 下有 aclnnAdds、aclnnAddV3 等多个接口文档？

**答案**：一个算子的能力可以从不同参数组合（Tensor+Tensor、Tensor+Scalar）和不同版本演进（V3）暴露为多个 API。Adds 对应标量加法（Tensor 与 Scalar 互推导），AddV3 是演进版本。op_api_list.md 明确了选择原则：选最高 V 版本。

## 5. 综合实践

**任务：给「未知算子」做一次完整的规格调研，产出一张调研卡。**

从 `docs/zh/op_api_list.md` 中任选一个你没用过的接口（建议选 math 目录下的），完成以下调研卡：

| 调研项 | 填写内容 |
|--------|---------|
| 算子目录 | 例如 math/xxx |
| 计算公式 | 抄自 README / 接口文档 |
| 算子级支持硬件 | 抄 README 产品支持表 |
| 接口级支持硬件 | 抄接口文档支持列表，标出与算子级的差异 |
| 输入/输出/属性参数表 | 参数名、角色、类型列表、格式 |
| 关键约束 | 类型推导、broadcast、维度上限、型号特例 |
| 两段式接口签名 | 抄函数原型 |
| 主要错误码 | 161001/161002 的具体触发场景 |
| 一个混合类型例子 | 自选两个输入类型，用推导表查出输出类型 |

以 add 为范例：调研卡中「混合类型例子」一栏填「x1=FLOAT16, x2=FLOAT → out=FLOAT(f32)，依据 deduction_relationship.md 表1」。完成后你会发现，无论算子多复杂，这张卡的结构不变——这就是统一文档结构带来的可复制方法论。整个实践无需 NPU 环境。

## 6. 本讲小结

- 算子 README 固定四板块：产品支持情况、功能说明（含计算公式）、参数说明、调用说明；结构全仓统一，会读一个就会读所有。
- **算子级支持 ≠ aclnn 接口级支持**：add 在算子级支持 Atlas 推理系列（310p），但 aclnnAdd 接口在 310p 上不支持——使用前必须两层表都查，还要注意接口文档内按型号动态渲染的特例注释（如 910 不支持 BFLOAT16）。
- aclnn 接口文档在 README 之上补充了函数原型、逐参数约束（类型推导/broadcast/8 维上限/非连续 Tensor）、错误码表（161001 空指针、161002 参数非法）和完整调用示例；README 公式是算子本质，接口文档公式是 API 层完整语义（如 add 的 alpha 系数）。
- 两个输入类型不一致时按互推导规则表（类似 PyTorch Type Promotion）统一：FLOAT16 + FLOAT → FLOAT；出现 × 组合则接口直接报错。
- 查接口的入口是 `docs/zh/op_api_list.md` 总表；一个算子可派生多个接口（Inplace 变体、Scalar 变体、V 版本），V 版本选最高即可。

## 7. 下一步学习建议

本讲你已经会「读规格」了，下一讲 **u2-l2 算子定义与注册：op_def 与 OpDef DSL** 将打开 `math/add/op_host/add_def.cpp`，看 README 中那张参数表是如何用 C++ 代码（OpDef 链式 DSL）注册到 CANN 系统里的——你会发现 README 的每一行规格在 add_def.cpp 中几乎都有一行对应代码。建议提前浏览 [math/add/op_host/add_def.cpp](https://github.com/gitcode.com/cann/ops-math/blob/4332f74d81d2d3ce1d7ea89375e911bce1b3a516/math/add/op_host/add_def.cpp)，试着找找 `Input("x1")` 和 README 参数表的对应关系。
