# aclnn 适配层深入：op_api 的分层封装

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 op_api 目录下两类文件——`gelu.cpp`（l0 层）与 `aclnn_gelu.cpp`（l2 层）——各自的职责与分层关系。
2. 读懂 l0 层 `l0op::Gelu` 如何用 `AllocTensor` 分配输出、用 `ADD_TO_LAUNCHER_LIST_AICORE` 把算子挂到执行清单。
3. 读懂 l2 层 `aclnnGeluGetWorkspaceSize` 的标准六步骨架：公共入参检查 → 创建 executor → 参数校验 → 空 tensor 短路 → 编排 l0 算子链 → 计算 workspace 并移交 executor。
4. 知道 `common/inc/op_api` 提供了哪些公共设施（`aclnn_util.h`、`op_api_def.h`、`op_util.h`、`runtime2_util.h`），以及哪些常用宏其实来自 CANN 包头文件。
5. 能对照 gelu 的分层模板，审计并补全其他算子 GetWorkspaceSize 的返回码处理。

## 2. 前置知识

本讲建立在 u2-l1（aclnn 两段式调用）和 u3-l1（算子原型定义）之上，先把两个关键结论复述一遍：

- **两段式接口**：用户先调 `aclnnXxxGetWorkspaceSize`（第一段），它做参数校验、把要执行的算子登记进 `aclOpExecutor`（一张"执行清单"），并算出临时内存 workspace 的大小；再调 `aclnnXxx`（第二段）把清单异步提交到 stream 执行。官方说明见 [docs/zh/context/two_phase_api.md:L1-L23](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/context/two_phase_api.md#L1-L23)，其中还明确了一条约束：第二段接口不能对同一个 executor 重复调用。
- **登记与执行分离**：第一段只"记账"不计算，真正的 kernel 下发发生在第二段。这个分离是理解 op_api 分层的钥匙。

再补充三个本讲要用的术语：

| 术语 | 含义 |
| --- | --- |
| l0 层 | `op_api/gelu.cpp` 这类文件，一个函数对应一个最小算子动作（如 Gelu、Contiguous、ViewCopy），把单个算子挂进 executor，命名空间 `l0op` |
| l2 层 | `op_api/aclnn_gelu.cpp`，面向用户的 aclnn 两段式接口，负责校验、编排多个 l0 算子、管理 workspace |
| executor | `aclOpExecutor`，执行清单容器；l2 第一段往里登记，第二段统一提交执行 |

一句话概括分层动机：**一个用户级 API 往往需要多个底层算子串联完成**（gelu 就是 Contiguous→Gelu→ViewCopy 三步），如果都写在一个大函数里，校验、内存、错误处理会搅在一起；拆成"l0 提供积木、l2 负责搭积木"后，每个 l0 算子还能被其他 aclnn 接口复用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [activation/gelu/op_api/gelu.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/gelu.cpp) | l0 层实现：`l0op::Gelu`，分配输出 tensor 并把 Gelu 算子挂到 executor |
| [activation/gelu/op_api/gelu.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/gelu.h) | l0 层声明，供其他 op_api 文件 include 复用 |
| [activation/gelu/op_api/aclnn_gelu.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp) | l2 层实现：`aclnnGeluGetWorkspaceSize` + `aclnnGelu` |
| [activation/gelu/op_api/aclnn_gelu.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.h) | 对外头文件，注释里写明了参数约束与内部计算图 |
| [common/inc/op_api/aclnn_util.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_api/aclnn_util.h) | 公共工具：Regbase 架构判断 |
| [common/inc/op_api/op_api_def.h](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_api/op_api_def.h) | 公共常量：最大维度数、dtype 精度策略枚举值等 |
| [common/inc/op_api/op_util.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_api/op_util.h) | 公共工具：常量 tensor 判断、维 legality 校验与错误消息拼接（主要服务 infershape/tiling） |
| [common/inc/op_api/runtime2_util.h](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_api/runtime2_util.h) | 公共工具：tiling parse 辅助、reduce mean 系数计算（服务 op_host 侧） |
| [docs/zh/context/two_phase_api.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/context/two_phase_api.md) | 两段式接口的官方说明文档 |

注意一个容易混淆的点：`CHECK_RET`、`OP_CHECK_NULL`、`CREATE_EXECUTOR`、`ADD_TO_LAUNCHER_LIST_AICORE`、`CommonOpExecutorRun` 这些宏与函数**不在本仓库中**，它们来自 CANN 包的头文件（如 `opdev/make_op_executor.h`、`aclnn_kernels/common/op_error_check.h`，见 aclnn_gelu.cpp 的 include 列表）。读本仓库源码时要能区分"仓库内代码"与"工具包头文件"。

## 4. 核心概念与源码讲解

### 4.1 op_api 的两层代码骨架

#### 4.1.1 概念说明

gelu 的 op_api 目录有四个文件，两两配对：

```text
op_api/
├── gelu.h / gelu.cpp          # l0 层：l0op::Gelu，一块"积木"
└── aclnn_gelu.h / aclnn_gelu.cpp  # l2 层：面向用户的 aclnn 两段式接口
```

l2 层不直接把"用户输入 → kernel"一步到位，而是像搭积木：用户的一个 `aclnnGelu` 调用在第一段里展开成 Contiguous → Gelu → ViewCopy 三个 l0 算子的串联。为什么需要三步？因为：

- **Contiguous**：Gelu 的 kernel 假设输入是连续内存；如果用户传的是切片后的非连续 tensor（u3-l3 讲过的 strides 视图），需要先转连续。
- **Gelu**：真正做激活计算的那块积木。
- **ViewCopy**：计算结果是新分配的连续 tensor，而用户的出参 `out` 可能是非连续的，需要按 out 的视图把数据搬回去。

这张内部计算图直接写在对外头文件的注释里，见 [activation/gelu/op_api/aclnn_gelu.h:L27-L34](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.h#L27-L34)（mermaid 图：Self → Contiguous → Gelu → ViewCopy → out）。**头文件注释是参数约束的第一手文档**，dtype/format/shape 约束都写在 L36-L44。

#### 4.1.2 核心流程

l2 第一段的编排流程（伪代码）：

```text
aclnnGeluGetWorkspaceSize(self, out, workspaceSize, executor):
    检查 workspaceSize/executor 这两个出参指针     # 公共入参检查
    创建 OpExecutor（unique_ptr 托管）            # 失败即返回创建错误码
    CheckParams(self, out)                       # 空指针→dtype→format→shape 四组校验
    若 self 是空 tensor：workspaceSize=0，直接移交 executor 返回成功
    selfContiguous = l0op::Contiguous(self)       # 登记转连续算子
    geluResult   = l0op::Gelu(selfContiguous)     # 登记计算算子
    l0op::ViewCopy(geluResult, out)               # 登记视图回拷算子
    workspaceSize = executor 内所有算子所需临时内存之和
    executor 所有权移交给出参，返回 ACLNN_SUCCESS
```

#### 4.1.3 源码精读

先看 l0 层。整个 `gelu.cpp` 有效代码不到 15 行：

```cpp
OP_TYPE_REGISTER(Gelu);

const aclTensor* Gelu(const aclTensor* self, aclOpExecutor* executor)
{
    L0_DFX(Gelu, self);
    auto out = executor->AllocTensor(self->GetViewShape(), self->GetDataType());
    auto retAicore = ADD_TO_LAUNCHER_LIST_AICORE(Gelu, OP_INPUT(self), OP_OUTPUT(out));
    OP_CHECK_ADD_TO_LAUNCHER_LIST_AICORE(retAicore != ACLNN_SUCCESS, return nullptr,
                                         "Gelu ADD_TO_LAUNCHER_LIST_AICORE failed.");
    return out;
}
```

这段代码（[activation/gelu/op_api/gelu.cpp:L20-L31](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/gelu.cpp#L20-L31)）做了三件事：

1. `OP_TYPE_REGISTER(Gelu)`：注册 l0 算子类型名（L20）。
2. `executor->AllocTensor(...)`：向 executor 申请一个中间输出 tensor，shape 与 dtype 直接继承输入（逐元素算子的典型写法）。
3. `ADD_TO_LAUNCHER_LIST_AICORE(Gelu, OP_INPUT(self), OP_OUTPUT(out))`：把"Gelu 这个 AI Core kernel，输入 self、输出 out"这条记录追加进 executor 的执行清单；失败则打日志并返回 `nullptr` 作为错误信号。

注意 l0 函数的错误约定：**返回 `const aclTensor*`，成功返回新 tensor 指针，失败返回 `nullptr`**——调用方（l2 层）用 `CHECK_RET(x != nullptr, ...)` 接住。l0 层头文件只有一个声明，见 [activation/gelu/op_api/gelu.h:L15-L17](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/gelu.h#L15-L17)，任何别的 op_api 文件想复用这块积木，include 它即可。

#### 4.1.4 代码实践

**实践：验证 l0 积木的复用关系。**

1. 实践目标：确认 l0 层算子是被跨文件复用的"公共积木"，而不是 aclnn_gelu.cpp 的私有函数。
2. 操作步骤：
   - 在仓库根目录执行 `grep -rn "l0op::Contiguous" --include="*.cpp" activation/ | head -20`；
   - 再执行 `grep -rn "l0op::Gelu(" --include="*.cpp" activation/ | head -20`。
3. 需要观察的现象：`l0op::Contiguous` 出现在大量算子的 op_api 文件里；`l0op::Gelu` 除 gelu 自身外，也可能出现在 gelu 的反向或其他复合算子中。
4. 预期结果：Contiguous/ViewCopy 这类通用 l0 算子被几十处复用，说明"积木复用"是 op_api 分层的实际收益。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `l0op::Gelu` 不自己检查输入 dtype？

**答案**：分层职责约定——参数合法性校验统一放在 l2 层的 `CheckParams`（因为它掌握用户调用的完整上下文，能给出准确的错误码）；l0 层只做"分配输出 + 登记算子"，保持足够小才能被多处复用。l0 层唯一处理的是登记失败本身。

**练习 2**：`gelu.h` 的 include guard 是 `OP_API_INC_LEVEL0_GELU_H_`，`aclnn_gelu.h` 是 `OP_API_INC_LEVEL2_ACLNN_GELU_H_`，这两个名字透露了什么设计约定？

**答案**：仓库用命名直接标注层级——LEVEL0 对应 l0 积木层，LEVEL2 对应 l2 用户接口层。看文件名/include guard 就能判断一个 op_api 文件属于哪一层，无需读代码。

### 4.2 l2 层第一段：校验分组与六步骨架

#### 4.2.1 概念说明

`aclnnGeluGetWorkspaceSize` 是适配层最厚的函数。gelu 把校验拆成四个小函数（非空、dtype、format、shape），每个函数只回答"这一类约束是否满足"，再由 `CheckParams` 串起来。这种分组让"支持什么"一目了然，也方便测试与排错——错误码能精确指向某一类问题（`ACLNN_ERR_PARAM_NULLPTR` vs `ACLNN_ERR_PARAM_INVALID`）。

#### 4.2.2 核心流程

```text
CheckParams(self, out):
    CheckNotNull  → 失败: ACLNN_ERR_PARAM_NULLPTR   (空指针)
    CheckDtypeValid → 失败: ACLNN_ERR_PARAM_INVALID (dtype 不在白名单 / soc 不支持 bf16 / in-out 不一致)
    CheckFormat   → 失败: ACLNN_ERR_PARAM_INVALID   (私有格式如 FRACTAL_NZ)
    CheckShape    → 失败: ACLNN_ERR_PARAM_INVALID   (维度 ≤ 8 / out.shape == self.shape)
```

dtype 校验里有一处平台相关逻辑，值得单独看：

\[ \text{允许 BF16} \iff \text{当前架构} \in \{\text{DAV\_2201},\ \text{Regbase(DAV\_3510)}\} \]

即 BF16 不是所有芯片都支持，校验时要查当前平台架构。

#### 4.2.3 源码精读

dtype 白名单与平台检查（[activation/gelu/op_api/aclnn_gelu.cpp:L23-L29](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L23-L29)）：

```cpp
static const std::initializer_list<DataType> DTYPE_SUPPORT_LIST = {DataType::DT_FLOAT, DataType::DT_FLOAT16,
                                                                   DataType::DT_BF16};

static inline bool CheckSocVersionIsSupportBf16(void)
{
    return GetCurrentPlatformInfo().GetCurNpuArch() == NpuArch::DAV_2201 || Ops::NN::AclnnUtil::IsRegbase();
}
```

注意这里的 `Ops::NN::AclnnUtil::IsRegbase()` 正是本仓库公共设施 `common/inc/op_api/aclnn_util.h` 提供的（见 4.4 节）——l2 层代码与公共工具的连接点。

dtype 校验主体（[activation/gelu/op_api/aclnn_gelu.cpp:L38-L51](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L38-L51)）：先做 soc-BF16 特判并 `OP_LOGE` 记录，再用 `OP_CHECK_DTYPE_NOT_SUPPORT` 查白名单，最后用 `OP_CHECK_DTYPE_NOT_MATCH` 强制 out 与 self 同型。

format 与 shape 校验（[activation/gelu/op_api/aclnn_gelu.cpp:L61-L70](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L61-L70) 与 [L53-L59](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L53-L59)）：私有格式（如 FRACTAL_NZ，见 u3-l3）直接报错；维度数上限 8 来自公共常量 `MAX_SUPPORT_DIMS_NUMS`（[common/inc/op_api/op_api_def.h:L14-L16](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_api/op_api_def.h#L14-L16)），出参 shape 必须与入参一致。

第一段主体（[activation/gelu/op_api/aclnn_gelu.cpp:L89-L127](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L89-L127)），六个步骤都有注释标注"固定写法"或专项逻辑：

```cpp
OP_CHECK_COMM_INPUT(workspaceSize, executor);        // 步骤1：出参指针检查（CANN 包宏）
auto uniqueExecutor = CREATE_EXECUTOR();             // 步骤2：创建 executor，unique_ptr 托管
auto ret = CheckParams(self, out);                   // 步骤3：四组参数校验
if (self->IsEmpty()) { *workspaceSize = 0; ... }     // 步骤4：空 tensor 短路，无 kernel 要跑
auto selfIsContiguous = l0op::Contiguous(self, ...); // 步骤5：编排 l0 算子链
auto geluResult = l0op::Gelu(selfIsContiguous, ...);
auto viewCopyResult = l0op::ViewCopy(geluResult, out, ...);
*workspaceSize = uniqueExecutor->GetWorkspaceSize(); // 步骤6：汇总临时内存
uniqueExecutor.ReleaseTo(executor);                  //      所有权移交出参，成功返回
```

两个细节值得咀嚼：

- **空 tensor 短路**（L104-L109）：输入元素数为 0 时没有任何 kernel 需要执行，workspace 记 0、直接移交 executor 返回成功。这是所有算子都要考虑的边界。
- **所有权移交**：executor 先由 `unique_ptr` 托管，任何一步失败都会随栈回溯自动销毁（不会泄漏半张执行清单）；全部成功后才 `ReleaseTo(executor)` 交给调用者。这解释了两段式为什么安全——第一段半途而废时，用户手里不会拿到残缺的 executor。

#### 4.2.4 代码实践

**实践：构造非法参数，观察错误码路径（待本地验证）。**

1. 实践目标：把 4 组校验逐一触发，验证"错误码能精确指向问题类别"。
2. 操作步骤：以 u2-l1 的 aclnnGelu 调用样例为底稿，依次只改一处——
   - 传入 `nullptr` 的 self；
   - 把 self 的 dtype 设为 `ACL_DOUBLE`（不在白名单）；
   - 把 out 的 shape 改成与 self 不同（如 self 是 {8,8}、out 是 {8,7}）。
3. 需要观察的现象：三种情况分别返回 `ACLNN_ERR_PARAM_NULLPTR`、`ACLNN_ERR_PARAM_INVALID`（日志提示 dtype 不支持）、`ACLNN_ERR_PARAM_INVALID`（日志提示 shape 不一致）；可用 `aclGetRecentErrMsg`（u3-l3 讲过）取详情。
4. 预期结果：错误日志内容与 aclnn_gelu.cpp 中 `OP_LOGE` 的格式串逐字对应，能反推命中了哪一行校验。本实践需要真实昇腾环境编译运行，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么空 tensor 分支在 `CheckParams` 之后、`Contiguous` 之前？

**答案**：校验在前保证即使空 tensor 也必须是"合法的空"（指针非空、dtype 合法）；而 Contiguous/Gelu/ViewCopy 对空 tensor 来说无事可做，登记了反而是浪费，所以短路放在校验后、编排前，直接跳过整条算子链。

**练习 2**：如果删掉 `uniqueExecutor.ReleaseTo(executor)` 这一行，用户侧会发生什么？

**答案**：第一段函数返回时 `unique_ptr` 析构，executor 被销毁，出参 `executor` 指向已释放内存；用户随后调第二段 `aclnnGelu` 传入悬空指针，属于未定义行为。`ReleaseTo` 就是"放弃托管、移交给调用者"的动作。

### 4.3 l2 层第二段与 DFX 打点

#### 4.3.1 概念说明

第二段 `aclnnGelu` 只有 4 行，因为它不需要懂算子：所有计算流程都已登记在 executor 里，第二段只是"执行清单"。仓库里所有算子的第二段几乎是同一个模板，差别只在算子名（DFX 打点名）。

DFX（Design for X，可观测性设计）打点是穿插在两层代码里的性能观测锚点：`L0_DFX` 在 l0 层，`L2_DFX_PHASE_1`/`L2_DFX_PHASE_2` 在 l2 层两段各一个。它们平时近乎零开销，打开采集后能按算子名统计每段耗时——这就是 u8 性能调优讲义里 msprof 能看到算子级数据的基础。

#### 4.3.2 核心流程

```text
aclnnGelu(workspace, workspaceSize, executor, stream):
    L2_DFX_PHASE_2(aclnnGelu)               # 打第二段耗时点
    return CommonOpExecutorRun(...)          # CANN 包通用函数：
        按 executor 清单逐项下发 kernel 到 stream（异步）
```

#### 4.3.3 源码精读

第二段实现（[activation/gelu/op_api/aclnn_gelu.cpp:L129-L134](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L129-L134)）：

```cpp
aclnnStatus aclnnGelu(void* workspace, uint64_t workspaceSize, aclOpExecutor* executor, const aclrtStream stream)
{
    L2_DFX_PHASE_2(aclnnGelu);
    return CommonOpExecutorRun(workspace, workspaceSize, executor, stream);
}
```

`CommonOpExecutorRun` 由 CANN 包提供（本仓库 grep 不到它的定义），职责是遍历 executor 清单、逐个下发 AI Core kernel。对应的第一段打点在 L94：`L2_DFX_PHASE_1(aclnnGelu, DFX_IN(self), DFX_OUT(out))`（[activation/gelu/op_api/aclnn_gelu.cpp:L94](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L94)），l0 层打点在 [activation/gelu/op_api/gelu.cpp:L24](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/gelu.cpp#L24)。两段接口的对外声明与 `extern "C"` 包裹见 [activation/gelu/op_api/aclnn_gelu.h:L46-L58](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.h#L46-L58)，`ACLNN_API` 宏（`__attribute__((visibility("default")))`，定义于 [common/inc/op_api/aclnn_util.h:L18](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_api/aclnn_util.h#L18)）保证符号从动态库导出，用户程序才能 dlopen 到它。

#### 4.3.4 代码实践

**实践：统计第二段模板的复用度。**

1. 实践目标：验证"第二段是通用模板"这一结论。
2. 操作步骤：执行 `grep -rn "CommonOpExecutorRun" --include="*.cpp" activation/ | wc -l`，再任选两个结果文件对比其第二段函数体。
3. 需要观察的现象：命中数以百计；任意两个文件的第二段除函数名与 DFX 名外逐行相同。
4. 预期结果：确认第二段无算子个性逻辑——这也解释了为什么 u2-l1 说"换算子只需改 API 名"。

#### 4.3.5 小练习与答案

**练习**：第二段接口为什么不做任何参数校验？

**答案**：校验属于第一段职责，且 executor 由第一段产出、默认可信；第二段可能在性能敏感路径上被高频调用（对比：第一段每批次调一次），重复校验只会增加 launch 开销。这也呼应 two_phase_api.md 的约束——两段必须配对使用、第二段不可重复调用。

### 4.4 公共 op_api 设施：common/inc/op_api 里有什么

#### 4.4.1 概念说明

`common/inc/op_api` 是仓库内 op_api 层的公共工具箱，目标与 op_host 侧的 `common/inc/op_host`（u4-l3 讲过）一致：把跨算子重复的碎活收敛成一处。当前有 4 个头文件，按服务对象分两类：

| 文件 | 服务对象 | 内容 |
| --- | --- | --- |
| `aclnn_util.h` | l2 层 aclnn 适配 | `IsRegbase()` 架构判断、`ACLNN_API` 导出宏 |
| `op_api_def.h` | l2 层 aclnn 适配 | 公共常量（`MAX_SUPPORT_DIMS_NUMS = 8` 等、dtype 精度策略值） |
| `op_util.h` | infershape / tiling | `IsConstTensor`、维度 legality 校验与错误消息拼接 |
| `runtime2_util.h` | op_host tiling | `GetCompileInfoPtr`、reduce mean 系数计算、哈希输入包装 |

要划清一条边界：**仓库内公共设施 ≠ CANN 包公共设施**。gelu 用到的重量级宏——`CREATE_EXECUTOR`、`CHECK_RET`、`OP_CHECK_NULL`、`OP_CHECK_DTYPE_NOT_SUPPORT`、`ADD_TO_LAUNCHER_LIST_AICORE`、`CommonOpExecutorRun`——全部来自 CANN 包头文件（`opdev/make_op_executor.h`、`aclnn_kernels/common/op_error_check.h` 等），本仓库只是使用方。

#### 4.4.2 核心流程

以 gelu 的 BF16 校验为例，公共设施的调用链：

```text
aclnn_gelu.cpp: CheckSocVersionIsSupportBf16()
    ├─ GetCurrentPlatformInfo().GetCurNpuArch()   # CANN 包 opdev/platform.h
    ├─ NpuArch::DAV_2201 直接比较
    └─ Ops::NN::AclnnUtil::IsRegbase()            # 仓库 aclnn_util.h
         └─ 查静态集合 {NpuArch::DAV_3510}
```

#### 4.4.3 源码精读

`IsRegbase` 的实现（[common/inc/op_api/aclnn_util.h:L32-L43](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_api/aclnn_util.h#L32-L43)）：

```cpp
inline static bool IsRegbase()
{
    auto npuArch = GetCurrentPlatformInfo().GetCurNpuArch();
    const static std::set<NpuArch> regbaseNpuArchs = {NpuArch::DAV_3510};
    return regbaseNpuArchs.find(npuArch) != regbaseNpuArchs.end();
}
```

两个工程细节：`static` 局部集合只构造一次（`inline static` 函数被多个编译单元包含也不重复实例化）；提供有无参/带参两个重载，方便已知架构时免去全局查询。这呼应 u4-l3 讲过的 `tiling_util.h::IsRegbaseSocVersion`——op_host 与 op_api 各有一份 Regbase 判断，属于不同层的平行设施。

`op_util.h` 的维度校验（[common/inc/op_api/op_util.h:L31-L38](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/common/inc/op_api/op_util.h#L31-L38)）用模板统一了有符号/无符号比较：合法维度取值是 \([-rank, rank)\)（支持负索引），`GenInvalidDimMsg`（L66-L83）把非法值拼成带取值区间的错误消息，配合 `OP_LOGE` 输出。

#### 4.4.4 代码实践

**实践：盘点一个算子用到的公共件来源。**

1. 实践目标：建立"仓库公共设施 vs CANN 包设施"的判别习惯。
2. 操作步骤：
   - 打开 [activation/gelu/op_api/aclnn_gelu.cpp:L11-L19](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L11-L19) 的 include 列表；
   - 对每个符号（如 `CREATE_EXECUTOR`、`IsRegbase`、`MAX_SUPPORT_DIMS_NUMS`）在 `common/` 下执行 `grep -rn "符号名" common/inc/ | head -3`。
3. 需要观察的现象：`IsRegbase`、`MAX_SUPPORT_DIMS_NUMS` 在 `common/inc/op_api` 命中；`CREATE_EXECUTOR` 类宏、`CommonOpExecutorRun`、`l0op::Contiguous` 在 common 下无定义（它们来自 CANN 包的 `aclnn_kernels/`、`opdev/` 头）。
4. 预期结果：形成一张 gelu 依赖来源清单表——哪些改动需要改本仓库、哪些要等 CANN 包升级。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `op_util.h`、`runtime2_util.h` 放在 `op_api` 目录却主要服务 infershape/tiling？

**答案**：目录按"编译目标/交付件"组织而非严格按调用者组织；这两个头依赖 `register/op_impl_registry.h` 等 op_host 侧接口，与 op_api 一起构成 Host 侧公共件，所以集中在 `common/inc/op_api` 命名空间下（include guard 也混用 `COMMON_INFERSHAPE_UTIL_H_`、`CANN_OPS_BUILT_IN_OP_TILING_...`，说明其真实归属）。读源码时以 include 关系为准，不要只看目录名。

**练习 2**：新写一个 aclnn 适配文件时，`DTYPE_SUPPORT_LIST` 应该抄 gelu 的还是自己定义？

**答案**：自己按算子 def 文件（u3-l1 的 DataType/Format 候选槽位）定义。白名单必须与 def 声明一致——def 是参数校验第一道闸门，aclnn 层是第二道；两道不一致时要么放行导致 kernel 侧出错，要么误拦导致功能缺失。

## 5. 综合实践

**任务：画出 aclnnGelu 的完整调用时序图，并审计一个同类算子的返回码处理。**

第一部分（源码阅读型，无需硬件）：

1. 以本讲四个源码文件为素材，画一张时序图，参与者为：用户代码、`aclnnGeluGetWorkspaceSize`、`CheckParams`、`l0op::Contiguous`、`l0op::Gelu`、`l0op::ViewCopy`、executor。要求标出：
   - 每一步的返回值类型（`aclnnStatus` / `const aclTensor*`）；
   - 失败分支的返回码（`ACLNN_ERR_PARAM_NULLPTR`、`ACLNN_ERR_PARAM_INVALID`、`ACLNN_ERR_INNER_NULLPTR`）；
   - `ReleaseTo` 发生的时机。
2. 补画第二段：`aclnnGelu` → `CommonOpExecutorRun` → stream 异步执行。

第二部分（返回码审计）：

1. 在 `activation/` 下任选一个 op_api 目录（如 `fast_gelu` 或 `silu`），打开其 aclnn 主文件；
2. 对照 gelu 的六步骨架，逐行检查其 GetWorkspaceSize：每一步失败路径是否都有 `CHECK_RET`（或等价处理）？空 tensor 是否短路？`ReleaseTo` 是否在所有成功路径上都被调用？
3. 若发现缺失（例如某 l0 调用后未检查 `nullptr`、或错误码使用了不精确的值），参照 gelu 的写法在纸面（或本地分支）补全，并说明每处补全防止了什么故障。
4. 若能在配套环境编译（`bash build.sh --pkg --soc=${soc_version} --ops=<该算子>`），验证补全后编译通过；无环境则标注**待本地验证**。

## 6. 本讲小结

- op_api 分两层：l0 层（`gelu.cpp`）是可复用积木——`AllocTensor` 分配输出、`ADD_TO_LAUNCHER_LIST_AICORE` 登记算子；l2 层（`aclnn_gelu.cpp`）是用户接口——校验、编排 l0 链、管理 workspace。
- 一个 aclnn 调用可展开为多个 l0 算子：gelu = Contiguous → Gelu → ViewCopy，分别解决非连续输入、纯计算、非连续输出回写。
- 第一段六步骨架：出参指针检查 → 创建 executor（unique_ptr 托管，失败自动销毁）→ 四组参数校验（空指针/dtype/format/shape 分组，错误码分类精确）→ 空 tensor 短路 → 编排 l0 链 → 汇总 workspace 并 `ReleaseTo` 移交所有权。
- 第二段是通用模板：`L2_DFX_PHASE_2` 打点后委托 `CommonOpExecutorRun` 按清单下发，无算子个性逻辑。
- 公共设施要分清来源：仓库内 `common/inc/op_api`（`IsRegbase`、`MAX_SUPPORT_DIMS_NUMS` 等）与 CANN 包头文件（`CREATE_EXECUTOR`、`CHECK_RET`、`CommonOpExecutorRun` 等）。
- 头文件注释（含 mermaid 计算图与参数约束）是 aclnn 接口的第一手文档；dtype 白名单必须与 def 文件的候选槽位保持一致。

## 7. 下一步学习建议

- 下一讲 u6-l2 将进入量化融合算子 `quant_batch_matmul_v4`，看 aclnn/tiling/kernel 如何组织数十种量化组合——本讲的"def 候选槽位 + aclnn 白名单"双重校验在那里会放大到 84 个槽位。
- 想巩固本讲：横向阅读 `activation/fast_gelu`、`activation/silu` 的 op_api，对比它们与 gelu 在编排 l0 链上的差异。
- 想理解 executor 下发之后发生了什么：回看 u4-l1/u4-l2 的 tiling 机制，以及 u5-l1 的 kernel 三段式——executor 清单里的每一项最终对应一次"tiling → kernel 启动"。
