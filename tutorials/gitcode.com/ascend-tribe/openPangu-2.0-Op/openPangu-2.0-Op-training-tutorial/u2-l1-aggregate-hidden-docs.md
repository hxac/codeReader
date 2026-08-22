# 从文档读懂一个算子：AggregateHidden 的功能与约束

## 1. 本讲目标

本讲是「单算子四层结构精读」单元的第一讲。我们不急着读 C++ 代码，先学会一件事：**只靠文档，把一个算子的行为完整地描述出来**。

学完本讲，你应该能够：

1. 说出 `ai_infra_aggregate_hidden` 算子的功能：对 hidden 层的 token 之间做一维分组卷积。
2. 独立写出该算子的输入/输出张量清单（名称、类型、shape、必选/可选）。
3. 对照计算公式，解释 mask（掩码）在公式中的位置和作用，并用一个小规模数值例子手工算出输出。
4. 看懂「产品支持情况」表格：A2/A3 支持、950PR/950DT 等不支持，并知道如何与源码中的芯片注册互相印证。
5. 用 numpy 写出该算子的 CPU 参考实现（golden），作为后续验证 NPU 算子正确性的基准。

本讲的方法论比结论更重要：**读算子库的顺序是「文档 → 源码 → 测试」，文档是入口，但要有能力发现文档本身的笔误**。本讲会带你实际发现一处。

## 2. 前置知识

### 2.1 token、序列与 hidden 层

大模型处理文本时，会把输入切成一个个 token（词元）。一个批次的输入在模型内部通常表示为三维张量。本算子使用的记法是 `[S, B, H]`：

- `S`（Sequence Length）：序列长度，即 token 个数；
- `B`（Batch Size）：批大小，即同时处理多少条序列；
- `H`（Head Size / hidden Size）：每个 token 的隐状态向量维度。

注意：本算子把 **S 放在第一维**，与 PyTorch 常见的 `[B, S, H]` 布局不同。这是阅读昇腾算子文档时要格外小心的点——**先看文档声明的维度顺序，再写代码**。

### 2.2 一维卷积与分组卷积的直觉

一维卷积：用一个长度为 `W` 的窗口在序列上滑动，每个输出位置 = 窗口内输入的加权和。本算子 `W = 3`，即每个输出位置只看**当前 token 和前面两个 token**——这是一个只向过去看的「因果」窗口。

「分组」卷积的含义：权重 `weight` 的 shape 是 `[W, H]`，即 **H 维度上每个通道有自己独立的 W 个权重**，通道之间不混合。这与普通卷积「把所有输入通道加在一起」不同，等价于 `groups = H` 的一维卷积。

### 2.3 mask（掩码）

mask 是一个 `bool` 类型张量，shape 为 `[B, S]`。`True` 表示保留该位置的输出，`False` 表示把该位置输出整体置 0。直觉上它用来屏蔽无效 token（例如 padding 部分）对后续计算的影响。

### 2.4 数据类型与两段式接口

- `bfloat16` / `float16`：NPU 训练最常用的两种半精度浮点类型。
- 两段式 aclnn 接口（承接 u1-l2 的心智模型）：先调 `aclnnXxxGetWorkspaceSize` 拿到 workspace 大小和执行器 executor，再调 `aclnnXxx` 下发执行。本讲的 aclnn 文档会再次印证这一约定。

### 2.5 关于公式排版

仓库文档里公式用 `$$...$$` 排版；本讲义统一改用 `\[ ... \]`（独立公式）和 `\( ... \)`（行内公式），两者内容一致。

## 3. 本讲源码地图

本讲的主角是两份文档，其余文件仅用于「交叉验证」，不精读：

| 文件 | 角色 |
| --- | --- |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md` | torch 侧规格入口：功能、公式、参数、约束、调用示例 |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/docs/aclnnAiInfraAggregateHidden.md` | aclnn 侧规格：C++ 两段式接口、参数表、错误码、确定性说明、C++ 调用示例 |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp` | 用于验证「约束说明」确实在源码中被强制执行（本讲只引用常量与校验行，u2-l3 精读） |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp` | 用于验证「产品支持情况」与芯片注册一致（u2-l2 精读） |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden_grad/` | 反向（求梯度）算子，前向与反向成对出现（承接 u1-l1） |

阅读建议：先读 README 建立整体印象，再读 aclnn 文档补全 C++ 视角，最后用 tiling/def 源码给文档「验真」。

## 4. 核心概念与源码讲解

### 4.1 模块一：README.md——torch 侧的算子规格卡片

#### 4.1.1 概念说明

算子目录下的 `README.md` 是这个算子的「门面」，面向的是**用 PyTorch（torch_npu）调用算子的算法工程师**。它回答五个问题：

1. 哪些硬件支持？（产品支持情况）
2. 算子做什么、怎么算？（功能说明 + 计算公式）
3. 怎么调用？（函数原型）
4. 每个参数是什么？（参数说明 / 返回值说明）
5. 有什么限制？（约束说明）

最后附一段可直接复制运行的调用示例。这份 README 就是我们要产出「算子规格卡片」的信息来源。

#### 4.1.2 核心流程

拿到一个新算子的 README，推荐的阅读流程：

1. 查**产品支持情况**，确认目标硬件可用；
2. 读**功能说明与计算公式**，用小例子把公式「算通」；
3. 看**函数原型**，确定调用形式（哪些必选、哪些可选）；
4. 对照**参数说明**，整理输入输出清单（类型 / shape / 必选可选）；
5. 记录**约束说明**（这是后续写 tiling 校验和测试用例的依据）；
6. 跑通**调用示例**（需要 NPU 环境，没有则记为待本地验证）。

#### 4.1.3 源码精读

**(1) 产品支持情况表**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md:3-12](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L3-L12)

这张表列出 6 类产品的支持情况：

| 产品 | 是否支持 |
| --- | --- |
| Ascend 950PR / 950DT | × |
| Atlas A3 训练系列 / A3 推理系列 | √ |
| Atlas A2 训练系列 / A2 推理系列 | √ |
| Atlas 200I/500 A2 推理产品 | × |
| Atlas 推理系列产品 | × |
| Atlas 训练系列产品 | × |

读表要点：

- **A2 / A3 支持，其余不支持**。结合 u1-l3 的知识：A2 大致对应 `ascend910b`、A3 对应 `ascend910_93`（即 `build.sh -c` 的参数值）。
- 注意「Atlas 200I/500 A2 推理产品」虽然名字里带 A2，但**不支持**——所以不能只看代号，必须逐行查表。
- 这张表不是「愿望」，它在源码中有落点：`_def.cpp` 里恰好只为 `ascend910b` 与 `ascend910_93` 注册了配置（见 4.3.3 的验证）。

**(2) 功能说明与计算公式**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md:14-25](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L14-L25)

算子功能一句话：**对 hidden 层的 token 之间进行一维分组卷积操作**（第 16 行）。计算公式（第 21-23 行）：

\[ \text{output}[i, j] = \text{mask}[j, i] \times \sum_{k=0}^{W-1} \text{input}[i-k, j] \times \text{weight}[W-1-k] \]

逐项拆解：

- `i` 是 S 轴索引，`j` 是 B 轴索引；`input[i-k, j]` 和 `weight[W-1-k]` 都是长度为 H 的向量，两者的乘法是 **H 维上逐元素相乘**，所以 `output[i, j]` 也是长度为 H 的向量。
- 求和下标 `k` 从 0 到 W-1：输出位置 `i` 只看输入的 `i, i-1, ..., i-W+1` 位置，即**只向序列前方（过去）看**，是因果窗口。
- 权重下标是 `W-1-k`（随 k 递增而递减）：即 `input[i]` 配 `weight[2]`、`input[i-1]` 配 `weight[1]`、`input[i-2]` 配 `weight[0]`。这说明实现按「互相关」方式组织，权重翻转已写进下标里。
- 「无效位置的 padding 为 0 填充」（第 25 行）：当 `i-k < 0`（序列开头不够长）时，该项按 0 参与求和。
- 当前仅支持 `W = 3`。

把 W=3 展开写，公式变得更直观：

\[ \text{output}[i, j] = \text{mask}[j, i] \times \big( \text{input}[i, j] \times \text{weight}[2] + \text{input}[i-1, j] \times \text{weight}[1] + \text{input}[i-2, j] \times \text{weight}[0] \big) \]

**mask 的位置说明两件事**：它乘在求和结果之外，对整个 H 向量整体生效（置 0 或保留）；它的下标是 `mask[j, i]` 而不是 `mask[i, j]`，因为 mask 的 shape 是 `[B, S]`，第一维是 batch、第二维才是序列。

**(3) 函数原型**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md:27-31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L27-L31)

```python
torch.ops.custom.npu_aggregate_hidden(input, weight, *, mask=None) -> (Tensor)
```

这是 `torch_ops_extension`（u6 会精读）包装出来的调用方式：算子挂在 `torch.ops.custom` 命名空间下，名字带 `npu_` 前缀。

**(4) 参数说明**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md:33-45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L33-L45)

第 37 行先解释维度含义：B 是批大小、S 是序列长度、H 是 hidden 层大小、W 是窗口大小。然后逐个参数说明：

| 参数 | 必选 | 类型 | shape | 说明 |
| --- | --- | --- | --- | --- |
| `input` | 必选 | bfloat16 / float16 | `[S, B, H]` | 待卷积的 hidden 层输入（第 39 行） |
| `weight` | 必选 | bfloat16 / float16 | `[W, H]` | 卷积权重，W 只支持 3，dtype 需与 input 一致（第 41 行） |
| `mask` | 可选 | bool | `[B, S]` | 输出掩码，不传（None）表示无掩码操作（第 45 行） |

第 43 行专门解释了原型中的 `*` 号：`*` 之前的参数是**位置参数**（按顺序传），之后的是**键值参数**（必须用 `mask=...` 的形式传，不传则用默认值）。这就是为什么调用示例里写 `mask=mask` 而不是直接当第三个位置参数传。

**(5) 返回值说明（含一处文档疑点）**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md:47-49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L47-L49)

README 第 49 行写道：output「表示分组卷积输入 input 的**梯度**，对应公式中的 **grad_input**」。

这与本算子的定位矛盾：`ai_infra_aggregate_hidden` 是**前向**卷积算子，公式里的输出就是 `output` 本身；而「input 的梯度」是反向算子 `ai_infra_aggregate_hidden_grad`（真实存在于兄弟目录，其文档为 `ai_infra_aggregate_hidden_grad/docs/aclnnAiInfraAggregateHiddenGrad.md`）的职责。对照 aclnn 文档第 109 行：「output……表示**卷积的输出结果**，对应公式中的 **output**」，可以确认 **README 这一句是从反向算子文档复制时留下的笔误，应以下列依据为准：公式（第 21-23 行）+ aclnn 文档**。

这是本讲想传授的读文档习惯：**两份文档 + 公式 + 源码互相印证，单一来源不能全信**。

**(6) 约束说明**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md:51-59](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L51-L59)

- 不支持图模式（第 53 行）；
- input、weight、output 的数据类型必须一致（第 54 行）；
- shape 范围（第 55-59 行）：

| 维度 | 含义 | 允许范围 |
| --- | --- | --- |
| B | Batch Size | 1 ~ 8 |
| S | Sequence Length | 1 ~ 32K（32768） |
| H | hidden Size | 192×2 ~ 192×128（即 384 ~ 24576，且是 192 的整数倍） |
| W | 窗口大小 | 只支持 3 |

注意「H 取值范围 192\*2 ~ 192\*128」这种写法隐含 **H 必须是 192 的整数倍**（从 384 到 24576 共 127 档）。文档没有解释 192 的来由，通常与 NPU 数据搬运的对齐粒度有关——本讲不下结论，等 u2-l3 读 tiling 切分时再验证。

**(7) 调用示例**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md:61-84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/README.md#L61-L84)

示例（第 72-83 行）取 `b=4, s=4*1024, h=768, w=3`：768 = 192×4 落在约束内；构造 bf16 输入并 `.npu()` 搬到设备；`mask = None`；最后 `torch.ops.custom.npu_aggregate_hidden(input, weight, mask=mask)`。示例第 70 行 `import omni_training_custom_ops` 提示：这个调用入口依赖 torch_ops_extension 包（u6 精读），**先安装扩展包才能跑**。

#### 4.1.4 代码实践

**实践目标**：不改任何源码，把 README 示例中的输入构造逻辑抽出来，做成一个「输入约束自检脚本」，在任何有 Python（含 numpy）的机器上都能运行。

**操作步骤**：

1. 新建 `check_agg_hidden_input.py`（示例代码，本讲义编写，非仓库原有文件）：

```python
# 示例代码：检查输入 shape 是否满足 README 约束
import numpy as np

def check_aggregate_hidden_shapes(input_shape, weight_shape, mask_shape=None):
    S, B, H = input_shape
    W, H_w = weight_shape
    ok = True
    if not (1 <= B <= 8):
        ok = False; print(f"B={B} 超出 1~8")
    if not (1 <= S <= 32 * 1024):
        ok = False; print(f"S={S} 超出 1~32K")
    if not (192 * 2 <= H <= 192 * 128):
        ok = False; print(f"H={H} 超出 192*2~192*128")
    if W != 3:
        ok = False; print(f"W={W} 只支持 3")
    if H_w != H:
        ok = False; print(f"weight 的 H({H_w}) 与 input 的 H({H}) 不一致")
    if mask_shape is not None and tuple(mask_shape) != (B, S):
        ok = False; print(f"mask shape {mask_shape} 应为 (B,S)={(B, S)}")
    return ok

# README 示例的取值：b=4, s=4K, h=768, w=3
print(check_aggregate_hidden_shapes((4 * 1024, 4, 768), (3, 768)))        # 期望 True
# 三组非法输入
print(check_aggregate_hidden_shapes((4 * 1024, 4, 200), (3, 200)))       # H=200 非法
print(check_aggregate_hidden_shapes((4 * 1024, 16, 768), (3, 768)))      # B=16 非法
print(check_aggregate_hidden_shapes((4 * 1024, 4, 768), (3, 768), (768, 4)))  # mask 维度颠倒
```

2. 运行 `python check_agg_hidden_input.py`（只需 numpy，无需 NPU）。

**需要观察的现象**：每行打印的 True/False 与打印出的具体违规原因。

**预期结果**：第 1 行 `True`；后三行依次报 `H=200 超出…`、`B=16 超出…`、`mask shape (768, 4) 应为 (B,S)=(4, 4096)`，并返回 `False`。最后一组特意演示了把 mask 写成 `[S, B]` 的常见错误——公式里 `mask[j, i]` 的下标顺序就是防错提示。

若你在本机没有 Python 环境，可手工对照第 55-59 行的约束表逐条核对，结论相同。

#### 4.1.5 小练习与答案

**练习 1**：为什么公式里 mask 的下标是 `mask[j, i]`，而不是 `mask[i, j]`？

**答案**：mask 的 shape 是 `[B, S]`（README 第 45 行），第一维是 batch 索引 `j`，第二维才是序列索引 `i`。若写成 `mask[i, j]` 就把两个维度弄反了——这正是上一节实践中最后一组非法输入演示的错误。

**练习 2**：调用示例中为什么写 `mask=mask`，不能直接写 `npu_aggregate_hidden(input, weight, mask)` 吗？

**答案**：函数原型 `npu_aggregate_hidden(input, weight, *, mask=None)` 中的 `*` 把参数分成两段（README 第 43 行）：`*` 之后的所有参数必须以键值对形式传递。所以必须写 `mask=mask`；不写则使用默认值 `None`（无掩码）。

**练习 3**：如果传入 `B=16` 的输入，会发生什么？这是编译期错误还是运行期错误？

**答案**：违反「B 取值范围 1~8」的约束（README 第 56 行）。它不是编译期错误——shape 是运行时才知道的信息，因此由 tiling 阶段的校验报错（对应 `ai_infra_aggregate_hidden_tiling.cpp` 第 138-141 行的 `OP_CHECK_IF`，见 4.3.3）。u2-l3 会精读这套校验机制。

### 4.2 模块二：docs/aclnnAiInfraAggregateHidden.md——aclnn 侧规格与两段式接口

#### 4.2.1 概念说明

`docs/` 目录下的 `aclnnXxx.md` 是**C++/框架视角**的算子文档，面向直接使用 ACL（Ascend Computing Language）算子库的开发者。它与 README 的分工：

- README：torch 接口（`torch.ops.custom.npu_aggregate_hidden`），参数是 `torch.Tensor`；
- aclnn 文档：C 接口（`aclnnAiInfraAggregateHidden`），参数是 `aclTensor*`，并多了 workspace、executor、stream 这些框架概念。

两份文档的产品支持表、功能说明、公式、约束完全一致（逐字重复），但 aclnn 文档多了三块内容：**两段式接口签名与参数表、aclnn 错误码、确定性计算说明**。反过来，torch 侧的 `*` 号语义只写在 README 里。读文档时要两份对照，各取所长。

#### 4.2.2 核心流程

aclnn 两段式调用流程（C++ 示例第 359-373 行的抽象）：

```text
1. aclInit / aclrtSetDevice / aclrtCreateStream      # 环境初始化（固定写法）
2. 构造 4 个 aclTensor：input / weight / mask / output
   （host 数据 → aclrtMalloc → aclrtMemcpy → aclCreateTensor）
3. 第一段：aclnnAiInfraAggregateHiddenGetWorkspaceSize(
              input, weight, mask, output, &workspaceSize, &executor)
   → 得到 workspaceSize 和 executor（执行器，内含算子计算流程）
4. 按 workspaceSize 用 aclrtMalloc 申请 device 内存
5. 第二段：aclnnAiInfraAggregateHidden(workspace, workspaceSize, executor, stream)
   → 在指定 stream 上下发执行
6. aclrtSynchronizeStream 等待完成 → 拷回结果 → 释放资源
```

#### 4.2.3 源码精读

**(1) 两段式函数原型**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/docs/aclnnAiInfraAggregateHidden.md:28-47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/docs/aclnnAiInfraAggregateHidden.md#L28-L47)

第 30 行明确说明：「每个算子分为两段式接口，必须先调用 aclnnAiInfraAggregateHiddenGetWorkspaceSize 接口获取计算所需 workspace 大小以及包含了算子计算流程的执行器，再调用 aclnnAiInfraAggregateHidden 接口执行计算」。

第一段（第 33-40 行）接收 4 个张量（input、weight、maskOptional、output——注意 **output 也要由调用方构造好再传入**），输出 `workspaceSize` 和 `executor`；第二段（第 42-47 行）接收 workspace 地址、大小、executor 和 `aclrtStream`。

**(2) GetWorkspaceSize 参数表**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/docs/aclnnAiInfraAggregateHidden.md:48-137](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/docs/aclnnAiInfraAggregateHidden.md#L48-L137)

参数表用 8 列表格描述每个参数：参数名 / 输入输出 / 描述 / 使用说明 / 数据类型 / 数据格式 / 维度 / 非连续 Tensor。关键行：

- `input`（第 76-85 行）：输入，BFLOAT16/FLOAT16，ND 格式，3 维，shape `[S, B, H]`，**不支持空 Tensor**；
- `weight`（第 86-95 行）：输入，2 维 `[W, H]`，W 只支持 3，dtype 与 input 一致；
- `maskOptional`（第 96-105 行）：输入，BOOL，2 维 `[B, S]`，「可选输入，默认值是 None」；
- `output`（第 106-115 行）：输出，shape 和数据类型与 input 一致——**这句就是 4.1.3 第 (5) 点中纠正 README 笔误的依据**；
- `workspaceSize` / `executor`（第 116-135 行）：两个输出型参数。

细心的读者会发现：README 说 input/weight「不支持非连续」，而这张表的最后一列「非连续 Tensor」对四个张量都打了 √。这两处表述并不矛盾，而是**描述的层次不同**——注册代码中为每个输入输出打开了 `AutoContiguous`（见 4.3.3 第 (3) 点），允许框架在进入 kernel 前自动把非连续张量转为连续，因此 aclnn 层面「支持传入非连续」；而 kernel 本体按连续内存处理。读文档时要把「接口层支持」和「kernel 层要求」分开。

**(3) aclnn 错误码**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/docs/aclnnAiInfraAggregateHidden.md:140-170](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/docs/aclnnAiInfraAggregateHidden.md#L140-L170)

- `ACLNN_ERR_PARAM_NULLPTR`（161001，第 157-160 行）：必选输入/输出/属性传了空指针；
- `ACLNN_ERR_PARAM_INVALID`（161002，第 162-168 行）：dtype 不在支持范围、数据格式不在支持范围。

这两个错误码是调用出错时排查的第一入口：161001 先查是否漏传参数，161002 再查 dtype/format 是否传错（例如把 fp32 输入传进来）。

**(4) 约束说明与确定性计算**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/docs/aclnnAiInfraAggregateHidden.md:217-228](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/docs/aclnnAiInfraAggregateHidden.md#L217-L228)

除与 README 相同的 shape 约束（第 222-228 行）外，多了一条训练算子很关心的性质——第 220-221 行：**「aclnnAiInfraAggregateHidden 默认确定性实现」**。确定性计算（deterministic）指同样输入必然得到逐位相同的输出，不受并行调度顺序、原子加顺序等影响。对分布式训练的意义在于：多卡/多次运行的结果可复现，便于精度比对和问题定位。

**(5) C++ 调用示例**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/docs/aclnnAiInfraAggregateHidden.md:231-402](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/docs/aclnnAiInfraAggregateHidden.md#L231-L402)

示例是一份完整的 `main()`，值得关注的行：

- 第 321-324 行：示例取的是**最小合法 shape**——`S=1, B=2, H=192*2, W=3`，即 H 取下界 384；
- 第 326-329 行：四个张量的 shape 分别为 `[S,B,H]`、`[W,H]`、`[B,S]`、`[S,B,H]`，与参数表一致；
- 第 346-353 行：`CreateAclTensor` 逐个创建张量，input/weight/output 用 `ACL_FLOAT16`，mask 用 `ACL_BOOL`（第 350 行）；
- 第 359-362 行：第一段调用 `aclnnAiInfraAggregateHiddenGetWorkspaceSize`；
- 第 364-369 行：按返回的 workspaceSize 申请 device 内存（为 0 时跳过）；
- 第 371-373 行：第二段调用 `aclnnAiInfraAggregateHidden` 下发执行；
- 第 376-377 行：`aclrtSynchronizeStream` 同步等待结果。

一个「批判性阅读」的观察：第 333 行 mask 的宿主数据用的是 `std::vector<int16_t>`，而第 350 行声明的类型是 `ACL_BOOL`——两者内存宽度并不一致，这是示例模板复制留下的痕迹。阅读示例时**以参数表的类型约束为准**，示例代码当流程骨架看就好。

#### 4.2.4 代码实践

**实践目标**：完成一次「C++ 示例走读」，把两段式调用的每一行映射到流程步骤，检验自己真正看懂了调用约定（源码阅读型实践，无需编译环境）。

**操作步骤**：

1. 打开 C++ 示例（第 235-402 行），用自己的话回答以下四个问题（答案写在笔记里）：
   - a) 第 321-324 行的 S/B/H/W 各是多少？换成约束表检查一遍是否合法；
   - b) 从第 360-362 行到第 372-373 行，两段调用之间发生了什么（提示：第 364-369 行）？
   - c) 如果第一段返回了 `161001`，按错误码表应该首先检查什么？
   - d) 第 376 行的同步调用如果删掉，直接拷回结果会怎样？
2. （可选，需要 NPU + CANN 环境）按文档第 233 行链接的「编译与运行样例」编译执行该示例。

**需要观察的现象**：（可选步骤）示例运行后终端逐元素打印 output（`PrintOutResult`，第 380 行）。

**预期结果**：

- a) S=1、B=2、H=384（=192×2，恰为下界）、W=3，全部合法；
- b) 中间发生的是：按第一段返回的 workspaceSize 在 device 侧 `aclrtMalloc` 申请 workspace 内存；
- c) 161001 = `ACLNN_ERR_PARAM_NULLPTR`：必选参数传了空指针，先检查是否漏传/传空了 input、weight、output（mask 可选传空时需确认接口接受 nullptr）；
- d) 第二段只是把任务异步下发到 stream，不同步就拷回会读到未计算完成的数据（竞态）。

可选步骤 2 在本讲义编写环境中未执行，**待本地验证**（需要 NPU 与 CANN 环境，见 u1-l3）。

#### 4.2.5 小练习与答案

**练习 1**：为什么必须先调 `GetWorkspaceSize` 再调执行接口？两段各自解决什么问题？

**答案**：第一段负责「规划」——校验参数、计算所需的 workspace 大小，并返回包含算子计算流程的 executor；调用方据此申请 device 内存。第二段负责「执行」——拿着 workspace、executor 在指定 stream 上下发计算（文档第 30 行及示例第 359-373 行）。拆成两段让内存申请的主动权留在调用方/框架手里。

**练习 2**：mask 是可选输入，那在 C++ 里「不传」怎么表达？

**答案**：参数表中 `maskOptional` 标注「可选输入，默认值是 None」（第 100 行）。C 接口层面即传入空指针表示不使用掩码；torch 侧则通过 `mask=None`（README 第 45 行）表达。注意文档示例中仍构造了 mask 张量传入（第 350 行），并未演示空指针路径。

**练习 3**：`ACLNN_ERR_PARAM_INVALID`（161002）在本算子上最典型的触发方式是什么？

**答案**：把 input/weight/mask/output 的数据类型或格式传成不支持的值，例如给 input 传 float32（只支持 BFLOAT16/FLOAT16）、给 weight 传与 input 不一致的类型，或使用非 ND 格式（错误码表第 162-168 行）。

### 4.3 模块三：公式与约束的交叉验证——把文档读「实」

#### 4.3.1 概念说明

文档描述「应该怎样」，源码决定「实际怎样」。上一模块我们已经见过一处 README 笔误；这个模块建立一条**交叉验证链**：

\[ \text{文档（README / aclnn）} \longleftrightarrow \text{注册源码（\_def.cpp）} \longleftrightarrow \text{校验源码（\_tiling.cpp）} \]

具体验证三件事：

1. 「产品支持情况」表 ↔ `_def.cpp` 中注册了哪些芯片；
2. 「约束说明」表 ↔ `_tiling.cpp` 中的常量与 `OP_CHECK_IF` 校验；
3. 计算公式 ↔ 手工数值例子 ↔ numpy golden 实现。

这套方法以后可以用到仓库里任何算子上。

#### 4.3.2 核心流程

**第一步：公式数值化。** 取 `S=4, B=1, H=1, W=3`，标量序列便于手算。设：

\[ \text{input} = [1, 2, 3, 4], \quad \text{weight} = [1, 10, 100] \]

（`weight[0]=1, weight[1]=10, weight[2]=100`。）按公式 \( \text{output}[i] = \sum_{k=0}^{2} \text{input}[i-k] \times \text{weight}[2-k] \)：

| i | 计算 | 结果 |
| --- | --- | --- |
| 0 | input[0]×weight[2]（前两窗越界补 0） | 1×100 = **100** |
| 1 | input[1]×100 + input[0]×10 | 200+10 = **210** |
| 2 | input[2]×100 + input[1]×10 + input[0]×1 | 300+20+1 = **321** |
| 3 | input[3]×100 + input[2]×10 + input[1]×1 | 400+30+2 = **432** |

再取 `mask = [True, True, False, True]`（shape `[B,S] = [1,4]`）：i=2 位置输出整体乘 `False` 变为 **0**，其余不变——这就是 mask 的作用：**按 (batch, 序列位置) 粒度把整段 H 向量清零**。

**第二步：约束溯源。** 把 README 约束表与 tiling 源码常量一一对应（见 4.3.3），确认每条约束都有运行期强制，而不只是文档建议。

**第三步：golden 落地。** 把公式翻译成 numpy（见 4.3.4），跑通上面的手算例子。

#### 4.3.3 源码精读

**(1) 约束常量与校验（tiling 侧）**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp:58-62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L58-L62)

```cpp
static constexpr int64_t S_SIZE_LIMIT = 32 * 1024;  // 32K
static constexpr int64_t B_SIZE_LIMIT = 8;
static constexpr int64_t H_SIZE_UP_LIMIT = 24576;   // 192*128
static constexpr int64_t H_SIZE_DOWN_LIMIT = 384;   // 192*2
static constexpr int64_t W_SIZE_LIMIT = 3;          // 卷积的窗口固定是3
```

这五行常量与 README 约束表**逐条相同**（S≤32K、B≤8、H∈[384,24576]、W=3）。它们不是摆设：

- [ai_infra_aggregate_hidden_tiling.cpp:132-148](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L132-L148)：S（132-135 行）、B（138-141 行）、H（144-148 行）超出范围时用 `OP_CHECK_IF` + `OP_LOGE` 报错退出——练习 3（4.1.5）中「B=16 会在运行期报错」的答案就在第 138-141 行；
- [ai_infra_aggregate_hidden_tiling.cpp:180-180](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L180-L180)：weight 第 0 维不等于 `W_SIZE_LIMIT`（3）时报错；
- [ai_infra_aggregate_hidden_tiling.cpp:209-221](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L209-L221)：mask 必须是 2 维、且两维分别等于 B 和 S——正是「mask 是 `[B,S]` 而非 `[S,B]`」的强制执行处。

本讲只需要记住「约束在校验函数里被逐条强制」这个事实，校验与切分如何组织留给 u2-l3。

**(2) 产品支持表的落点（注册侧）**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp:83-84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L83-L84)

```cpp
this->AICore().AddConfig("ascend910b", aicore_config);
this->AICore().AddConfig("ascend910_93", aicore_config);
```

注册代码只为 `ascend910b`（A2 类）和 `ascend910_93`（A3 类）添加配置，与产品支持表中「A2 训练/推理 √、A3 训练/推理 √、950PR/950DT ×」完全对应。文档表格与注册代码互为印证——以后判断「某算子支持哪些芯片」，最快的办法就是看 `_def.cpp` 里的 `AddConfig` 列表（u2-l2 精读）。

**(3) 「非连续」表述差异的落点**

[ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp:24-41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L24-L41)

注册代码中每个输入/输出都链式调用了 `.AutoContiguous()`（input 第 29 行、weight 第 35 行、mask 第 41 行，output 同样）。结合 4.2.3 的分析：这个开关允许框架层在进入 kernel 前自动把非连续张量转为连续，从而解释了两份文档「不支持非连续（kernel 视角）」与「非连续 Tensor 列打 √（aclnn 接口视角）」的差异。其确切转换时机与行为，可在 u2-l2 精读 `_def.cpp` 时进一步确认（本讲不展开）。

**(4) 前向 / 反向成对存在**

反向算子目录 `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden_grad/` 下同样有 `docs/aclnnAiInfraAggregateHiddenGrad.md`。这印证了 u1-l1 的结论：训练算子前向与 `_grad` 反向成对出现；也进一步佐证 4.1.3 第 (5) 点的判断——README 返回值那句话的措辞属于反向算子。

#### 4.3.4 代码实践

**实践目标**：把公式翻译成 numpy 的 CPU 参考实现（golden），并在 4.3.2 的手算例子上验证。

**操作步骤**：

1. 新建 `golden_agg_hidden.py`（示例代码，本讲义编写，非仓库原有文件）：

```python
import numpy as np

def golden_aggregate_hidden(x, w, mask=None):
    """ai_infra_aggregate_hidden 的 CPU 参考实现。
    x: [S, B, H]；w: [W, H]；mask: [B, S] 的 bool 数组或 None。返回 [S, B, H]。"""
    S, B, H = x.shape
    W = w.shape[0]
    out = np.zeros_like(x)
    for i in range(S):
        for j in range(B):
            acc = np.zeros(H, dtype=x.dtype)
            for k in range(W):
                if i - k >= 0:                      # i-k < 0 的位置 padding 为 0，跳过
                    acc = acc + x[i - k, j] * w[W - 1 - k]
            out[i, j] = acc * mask[j, i] if mask is not None else acc
    return out

# 手算例子：S=4, B=1, H=1, W=3
x = np.array([1.0, 2.0, 3.0, 4.0]).reshape(4, 1, 1)   # [S, B, H]
w = np.array([1.0, 10.0, 100.0]).reshape(3, 1)        # [W, H]，w[0]=1, w[1]=10, w[2]=100
print(golden_aggregate_hidden(x, w).reshape(-1))       # 期望 [100. 210. 321. 432.]

m = np.array([[True, True, False, True]])              # mask: [B, S] = [1, 4]
print(golden_aggregate_hidden(x, w, m).reshape(-1))    # 期望 [100. 210.   0. 432.]
```

2. 运行 `python golden_agg_hidden.py`（只需 numpy）。
3. 对照 4.3.2 的手工计算表逐个核对输出。

**需要观察的现象**：两行输出的 4 个数值，以及加上 mask 后第 3 个值的变化。

**预期结果**：第一行 `[100. 210. 321. 432.]`，第二行 `[100. 210. 0. 432.]`，与手算表完全一致。若不一致，优先检查两处：`w[W-1-k]` 的下标方向（权重是否配反）和 `mask[j, i]` 的下标顺序。

**待本地验证**：以上脚本由本讲义编写，编写环境未执行；三重循环写法直接照抄公式，正确性可与手算表互验。与 NPU 实际输出的对比需要安装好的算子环境（见综合实践）。

#### 4.3.5 小练习与答案

**练习 1**：H=200 的输入为什么非法？

**答案**：约束要求 H ∈ [192×2, 192×128] = [384, 24576] 且为 192 的整数倍（README 第 58 行）。200 < 384，tiling 侧第 147 行的 `OP_CHECK_IF(hSize_ < H_SIZE_DOWN_LIMIT, ...)` 会在运行期报错。

**练习 2**：README 与 aclnn 文档关于「非连续 Tensor」的表述一个说「不支持」、一个打「√」，如何解释？

**答案**：两者描述的层次不同。kernel 本体按连续内存处理（README 视角）；注册代码为所有输入输出打开了 `AutoContiguous()`（`_def.cpp` 第 24-41 行），框架会在进入 kernel 前自动转连续，因此 aclnn 接口层可以接收非连续张量（aclnn 表格视角）。读文档时要区分「接口层支持」与「kernel 层要求」。

**练习 3**：如果不小心把 mask 传成了 `[S, B]` 形状（维度顺序颠倒），错误会在哪里被拦截？

**答案**：在 tiling 阶段的 mask 形状校验（`ai_infra_aggregate_hidden_tiling.cpp` 第 209-221 行）：mask 必须 2 维、第 0 维等于 B、第 1 维等于 S。顺序颠倒时两维长度通常不同（B≠S）而报错；若 B 恰好等于 S 则可能蒙混过关，得到语义错误的静默结果——所以调用方应自行保证顺序，这也是 4.1.4 自检脚本把 mask 检查放进来的原因。

## 5. 综合实践

**任务：为 `ai_infra_aggregate_hidden` 制作一张「算子规格卡片」，并交付一个可复用的 numpy golden。**

这个任务把本讲全部内容串起来：读文档 → 提炼规格 → 公式验证 → 写参考实现。产出的规格卡片和 golden 以后可以直接复用（ST 精度测试就是「NPU 输出 vs CPU golden」的对比，见 u8-l3）。

**步骤 1：编写规格卡片**（新建 `agg_hidden_spec.md`，模板如下，内容必须来自本讲读过的两份文档并注明出处行号）：

```markdown
# ai_infra_aggregate_hidden 规格卡片
- 功能：对 hidden 层 token 间做一维分组卷积（README L16）
- 公式：output[i,j] = mask[j,i] * Σ_{k=0}^{W-1} input[i-k,j] * weight[W-1-k]（README L21-L23）
- 调用：torch.ops.custom.npu_aggregate_hidden(input, weight, *, mask=None)（README L29-L31）
- 输入输出表：
  | 参数 | 方向 | 必选 | dtype | shape | 备注 |
  | input | 输入 | 必选 | bf16/fp16 | [S,B,H] | 不支持空 Tensor |
  | weight | 输入 | 必选 | bf16/fp16 | [W,H] | W=3，dtype 与 input 一致 |
  | mask | 输入 | 可选 | bool | [B,S] | 默认 None |
  | output | 输出 | - | 同 input | [S,B,H] | 卷积输出结果（以 aclnn L109 为准） |
- 约束清单：B∈[1,8]；S∈[1,32K]；H∈[192*2,192*128]；W=3；三 dtype 一致；不支持图模式
- 产品支持：A2/A3 √；950PR/950DT、200I/500 A2、Atlas 推理/训练系列 ×（README L3-L12）
- 其他：aclnn 默认确定性实现（aclnn L220-L221）
```

**步骤 2：实现通用 golden 并自测**。在 4.3.4 的逐元素版本之外，再写一个向量化版本并互相验证（示例代码，本讲义编写）：

```python
import numpy as np

def golden_aggregate_hidden_vec(x, w, mask=None):
    """向量化版本：把 input 右移 k 位代替内层 k 循环。"""
    S, B, H = x.shape
    W = w.shape[0]
    out = np.zeros_like(x)
    for k in range(W):
        xk = np.zeros_like(x)
        xk[k:] = x[:S - k]           # xk[i] = x[i-k]：k 位置以左补 0，即 padding
        out += xk * w[W - 1 - k]     # [S,B,H] 与 [H] 广播相乘
    if mask is not None:
        out = out * mask.T[:, :, None]   # mask.T -> [S,B]，扩成 [S,B,1] 广播到 H
    return out

# 随机自测：两个版本必须逐位一致
rng = np.random.default_rng(0)
x = rng.standard_normal((64, 2, 384)).astype(np.float64)     # S=64, B=2, H=384(=192*2)
w = rng.standard_normal((3, 384)).astype(np.float64)
m = rng.random((2, 64)) > 0.5                                # [B, S]
from golden_agg_hidden import golden_aggregate_hidden
assert np.allclose(golden_aggregate_hidden(x, w, m), golden_aggregate_hidden_vec(x, w, m))
print("two golden implementations match")
```

**步骤 3（可选，需要 NPU 环境）**：安装算子包与 torch_ops_extension 后，把 README 第 65-84 行示例的输入换成步骤 2 的随机数据（转 bf16），对比 NPU 输出与 float64 golden 的误差量级。此步在本讲义编写环境中未执行，**待本地验证**；误差评估方法（MARE/MERE/RMSE）在 u8-l3 展开。

**验收标准**：

1. 规格卡片每一条都能指出文档出处（文件 + 行号）；
2. 手算例子（100/210/321/432 与 mask 置 0）与 golden 输出一致；
3. 两个 golden 实现在随机数据上逐位一致；
4. 能口头复述：本算子输出为什么不是「input 的梯度」（README 笔误 + 反向算子成对存在的证据）。

## 6. 本讲小结

- `ai_infra_aggregate_hidden` 对 hidden 层 token 间做一维分组卷积：`[S,B,H]` 输入 × `[W,H]` 权重（W=3 因果窗口，H 维逐通道独立权重），可选 `[B,S]` bool 掩码把整段 H 输出置 0。
- README 是 torch 侧规格（`torch.ops.custom.npu_aggregate_hidden(input, weight, *, mask=None)`），aclnn 文档是 C++ 侧规格（两段式 `GetWorkspaceSize` + 执行接口），两份文档的产品表/公式/约束一致、内容互补。
- 核心约束：B∈[1,8]、S∈[1,32K]、H∈[384,24576]（192 的倍数）、W=3、dtype 三者一致；这些约束在 tiling 源码中有逐条 `OP_CHECK_IF` 强制（tiling.cpp L58-L62、L132-L148、L180、L209-L221）。
- 产品支持表与 `_def.cpp` 的 `AddConfig("ascend910b")` / `AddConfig("ascend910_93")` 互相印证：A2/A3 支持，950PR/950DT 不支持。
- 文档可能有笔误：README 第 49 行把 output 说成「input 的梯度」是从反向算子文档复制的痕迹，应以公式和 aclnn 文档第 109 行为准——**交叉验证是读算子文档的基本功**。
- numpy golden（逐元素版 + 向量化版）是理解公式、也为后续 ST 精度测试准备基准的手段。

## 7. 下一步学习建议

- **u2-l2（op_def 原型注册）**：本讲两次引用了 `_def.cpp`（AddConfig 验证产品表、AutoContiguous 解释非连续差异），下一讲正式精读这个文件，看 `OpDef` 类如何声明输入输出与芯片配置。
- **u2-l3（Tiling 入门）**：本讲看到了约束校验的「果」（`OP_CHECK_IF` 报错），下一讲看「因」：tiling 如何取平台信息、如何按 S/B/H 切分数据、192 与 4096 这些数字在切分中的角色。
- **延伸阅读**：兄弟目录 `ai_infra_aggregate_hidden_grad/docs/aclnnAiInfraAggregateHiddenGrad.md`（反向算子的规格，可与本讲前向规格对照，体会「输出梯度/输入梯度」参数如何组织）；`tests/st/test_ai_infra_aggregate_hidden.py`（仓库自带的精度测试，看看官方 golden 怎么写，可与你的 numpy 实现对比）。
