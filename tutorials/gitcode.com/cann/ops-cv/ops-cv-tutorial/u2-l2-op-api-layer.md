# op_api 层源码走读：以 aclnnResize 为例

## 1. 本讲目标

上一讲（u2-l1）我们站在**调用者**视角理解了 aclnn 两段式接口的用法；本讲切换到**实现者**视角，走读 `image/resize_bilinear_v2/op_api/aclnn_resize.cpp`，学完本讲你应该能够：

1. 读懂任何一个 aclnn 实现文件的「三段结构」：文件级检查函数、`GetWorkspaceSize`（第一段）、执行函数（第二段）。
2. 说出 `GetWorkspaceSize` 内参数校验的标准顺序（非空 → 语义 → dtype → format → 元素合法性 → shape），以及每步失败时返回的错误码。
3. 理解非连续输入如何通过 `l0op::Contiguous` 归一化，以及 `aclOpExecutor` 如何像「记账本」一样把 Contiguous / TransData / L0 算子 / ViewCopy 串成一条执行链。
4. 理解 `L2_DFX_PHASE_1` / `L2_DFX_PHASE_2` 打点宏的作用，以及 ACLNN 特性级行号定位原理。
5. 知道 `common/inc/op_api` 下 `aclnn_check.h`、`level2_base.h`、`op_api_def.h` 提供了哪些可复用的公共检查能力。

## 2. 前置知识

- **op_api 层是什么**：算子工程中 `op_api/` 目录负责对外暴露 aclnn C 接口。它是用户态入口，运行在 Host 侧（CPU）上，职责是「把用户参数翻译成算子执行框架认识的任务描述」，真正的计算在 Device（NPU）上完成。
- **两段式接口回顾**（u2-l1 已讲）：第一段 `aclnnXxxGetWorkspaceSize` 做校验、生成 `aclOpExecutor` 并告知 workspace 大小；第二段 `aclnnXxx` 拿着 workspace、executor 和 stream 异步下发。本讲就是拆开第一段和第二段的实现。
- **L0 算子**：op_api 层不直接写核函数，而是调用一批更底层的原子算子（本仓库习惯称为 l0op，如 `l0op::Contiguous`、`l0op::ResizeBilinearV2`）。L0 算子实现负责把任务挂到 executor 的执行列表里。
- **NCHW / NC1HWC0（5HD）**：NCHW 是普通的 4 维排布；NC1HWC0 是昇腾 AI Core 更喜欢的分形排布（把 C 通道按 16 对齐切块）。老架构路径上需要先 TransData 转成 5HD 再计算。
- **错误码**：`aclnnStatus` 类型的返回值，常见取值有 `ACLNN_SUCCESS`（成功）、`ACLNN_ERR_PARAM_NULLPTR`（空指针）、`ACLNN_ERR_PARAM_INVALID`（参数非法）、`ACLNN_ERR_INNER_CREATE_EXECUTOR` / `ACLNN_ERR_INNER_NULLPTR`（内部错误）。它们定义在 CANN toolkit 的 `aclnn_base.h` 中，本仓库不重复定义。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [image/resize_bilinear_v2/op_api/aclnn_resize.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp) | 本讲主角：`aclnnResize` 两段式接口的完整实现，包含全部检查函数与执行链编排 |
| [image/resize_bilinear_v2/op_api/resize_bilinear_v2.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/resize_bilinear_v2.cpp) | L0 算子 `l0op::ResizeBilinearV2` 的实现，展示任务如何挂到 AiCore/AiCPU 执行列表 |
| [common/inc/op_api/aclnn_check.h](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_api/aclnn_check.h) | 公共架构判断工具：`IsRegBase()` 判断当前芯片是否为 DAV_3510 架构 |
| [common/inc/op_api/level2_base.h](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_api/level2_base.h) | 公共检查函数库：非空检查、shape/dtype 检查、按架构选择 dtype 支持列表等 |
| [common/inc/op_api/op_api_def.h](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_api/op_api_def.h) | 极小的公共常量头：`MAX_SUPPORT_DIMS_NUMS = 8` |
| [image/resize_bilinear_v2/examples/test_aclnn_resize.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/examples/test_aclnn_resize.cpp) | aclnnResize 的调用样例，可用来观察本讲源码的行为 |

> 说明：`L2_DFX_PHASE_1/2`、`CHECK_RET`、`OP_CHECK_NULL`、`CREATE_EXECUTOR`、`CommonOpExecutorRun` 等宏定义在 CANN toolkit 的头文件中（如 `opdev/op_dfx.h`、`aclnn_kernels/common/op_error_check.h`、`opdev/make_op_executor.h`），不在本仓库源码内，本仓库只负责使用它们。行号无法在仓库内给出，属「外部依赖」。

## 4. 核心概念与源码讲解

### 4.1 aclnn 实现文件的三段结构

#### 4.1.1 概念说明

打开任何一个 `op_api/aclnn_*.cpp`，你都会看到同样的骨架。以 `aclnn_resize.cpp` 为例，文件从上到下分成三段：

1. **文件级辅助函数**：一堆 `static` 检查函数（CheckNotNull / CheckDtypeValid / …）和辅助构造函数（CreateSizesV35 / CreateSizesRegBase），只服务本文件。
2. **第一段接口** `aclnnResizeGetWorkspaceSize`：做校验、创建 executor、编排执行链、写出 workspaceSize。
3. **第二段接口** `aclnnResize`：极薄，只打一个 DFX 点然后调用公共的 `CommonOpExecutorRun` 下发任务。

这个三段结构是全仓库约定：以后你读 `aclnnGridSampler2d`、`aclnnRoiAlign` 等，都可以按同样套路切着读。

#### 4.1.2 核心流程

`aclnnResize(self, scales, mode, out)` 的整体流程：

```text
用户调用 aclnnResizeGetWorkspaceSize(self, scales, mode, out, &wsSize, &executor)
  ├─ OP_CHECK_COMM_INPUT      检查 wsSize/executor 出参指针
  ├─ L2_DFX_PHASE_1           特性级打点（第一段进入）
  ├─ GetExtendPathFlag()      按芯片架构决定走哪条路径
  ├─ CheckParams(...)         六步参数校验（见 4.2）
  ├─ CREATE_EXECUTOR()        创建 aclOpExecutor（智能指针持有）
  ├─ l0op::Contiguous(self/out)   非连续 → 连续
  ├─ [RegBase 路径] CreateSizesRegBase + L0 算子直连 + ViewCopy
  └─ [V35 路径]     CreateSizesV35 + TransData(5HD) + L0 算子 + TransData 回 + ViewCopy
  ├─ *workspaceSize = executor->GetWorkspaceSize()
  └─ uniqueExecutor.ReleaseTo(executor)   所有权交给调用者

用户调用 aclnnResize(workspace, wsSize, executor, stream)
  ├─ L2_DFX_PHASE_2           特性级打点（第二段进入）
  └─ CommonOpExecutorRun(...) 框架统一：按 executor 里的任务列表异步下发到 stream
```

关键理解：**所有「编排」都发生在第一段**。Contiguous、TransData、L0 算子调用并不会立刻计算，而是把一个个任务记进 `aclOpExecutor`；第二段只是把这本「记账本」整体交给执行框架 (`CommonOpExecutorRun`) 去下发。

#### 4.1.3 源码精读

**第二段接口：薄到只有两行。**
[image/resize_bilinear_v2/op_api/aclnn_resize.cpp:323-327](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L323-L327)

```cpp
aclnnStatus aclnnResize(void* workspace, uint64_t workspaceSize, aclOpExecutor* executor, aclrtStream stream)
{
    L2_DFX_PHASE_2(aclnnResize);
    return CommonOpExecutorRun(workspace, workspaceSize, executor, stream);
}
```

这验证了 u2-l1 的结论：第二段签名全局统一（workspace、size、executor、stream 四件套），且不做任何业务逻辑——因为一切信息都已在第一段封装进 executor。

**第一段接口的主体：**
[image/resize_bilinear_v2/op_api/aclnn_resize.cpp:251-270](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L251-L270)

```cpp
aclnnStatus aclnnResizeGetWorkspaceSize(const aclTensor* self, const aclFloatArray* scales, const char* mode,
                                        aclTensor* out, uint64_t* workspaceSize, aclOpExecutor** executor)
{
    OP_CHECK_COMM_INPUT(workspaceSize, executor);

    L2_DFX_PHASE_1(aclnnResize, DFX_IN(self, scales, mode), DFX_OUT(out));
    // 参数检查
    bool extendFlag = GetExtendPathFlag();

    auto ret = CheckParams(self, scales, mode, out, extendFlag);
    CHECK_RET(ret == ACLNN_SUCCESS, ret);
    // 创建OpExecutor
    auto uniqueExecutor = CREATE_EXECUTOR();
    CHECK_RET(uniqueExecutor.get() != nullptr, ACLNN_ERR_INNER_CREATE_EXECUTOR);

    auto selfContiguous = l0op::Contiguous(self, uniqueExecutor.get());
    // （中间的执行链编排见 4.3）
```

这段做了四件事：

1. `OP_CHECK_COMM_INPUT`：先检查两个**出参**指针（workspaceSize、executor）非空——出参本身为空就没法回报结果了。
2. `L2_DFX_PHASE_1(aclnnResize, DFX_IN(...), DFX_OUT(...))`：打点，记录算子名、输入、输出（详见 4.4）。
3. `CREATE_EXECUTOR()`：创建 executor，用 `unique_ptr` 管理，失败返回 `ACLNN_ERR_INNER_CREATE_EXECUTOR`。
4. `l0op::Contiguous(...)`：把可能非连续的 self/out 归一化为连续排布，返回新的 `aclTensor*`，后续都用它。

**收尾三行：交接所有权。**
[image/resize_bilinear_v2/op_api/aclnn_resize.cpp:317-321](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L317-L321)

```cpp
    *workspaceSize = uniqueExecutor->GetWorkspaceSize();
    uniqueExecutor.ReleaseTo(executor);
    return ACLNN_SUCCESS;
```

`ReleaseTo` 把智能指针管理的 executor 裸指针移交给调用者——这正是 u2-l1 强调「executor 由第一段产出、第二段消费」的源码落点。

#### 4.1.4 代码实践

**实践：定位两段式接口并数一数各自的行数。**

1. 实践目标：直观感受「第一段厚、第二段薄」的结构差异。
2. 操作步骤：
   - 打开 [aclnn_resize.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp)，用编辑器搜索 `aclnnStatus aclnn`，会命中且只命中两个函数（L251 与 L323）。
   - 用 `grep -n "^aclnnStatus" image/resize_bilinear_v2/op_api/aclnn_resize.cpp` 在仓库根目录验证。
   - 再对 `objdetect/roi_align_grad/op_api/aclnn_roi_align_v2_backward.cpp` 重复同样操作，对比结构。
3. 需要观察的现象：两个文件的第一段都长达数十行（校验 + 编排），第二段都只有 2~3 行（打点 + `CommonOpExecutorRun`）。
4. 预期结果：确认「三段结构」是仓库级约定而非个例。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `aclnnResize` 第二段不再校验参数？
**答案**：第一段已把校验过的输入全部封装进 `aclOpExecutor`，第二段拿到的 executor 就是「校验通过」的凭证；且第二段签名全局统一，无法针对每个算子写定制校验。重复校验既浪费 Host 时间，也无处存放逐算子的校验逻辑。

**练习 2**：`uniqueExecutor.ReleaseTo(executor)` 如果改成直接 `*executor = uniqueExecutor.get()` 会怎样？
**答案**：`unique_ptr` 出作用域时会 delete 掉 executor，调用者拿到的就是悬空指针，第二段使用时崩溃。`ReleaseTo` 的语义是「放弃所有权、只移交裸指针」，是两段式接口生命周期管理的关键一环。

### 4.2 参数校验体系：CheckParams 六步检查

#### 4.2.1 概念说明

第一段里最长的部分是参数校验。ops-cv 的套路是：每个维度写一个小的 `static bool CheckXxx`，再由一个总的 `CheckParams` 按固定顺序串联，每步失败映射到对应的 `aclnnStatus` 错误码。这样做的好处：

- 顺序固定，日志可预期（先报空指针，再报语义错误，最后报 shape 错误）；
- 检查与错误码映射集中在 `CheckParams` 一处，单个 CheckXxx 只需返回 bool。

#### 4.2.2 核心流程

`CheckParams` 的六步顺序及错误码映射：

| 步骤 | 函数 | 检查内容 | 失败错误码 |
| --- | --- | --- | --- |
| 1 | `CheckNotNull` | self/scales/mode/out 非空 | `ACLNN_ERR_PARAM_NULLPTR` |
| 2 | `CheckModeStr` | mode 是 "nearest" 或 "bilinear" | `ACLNN_ERR_PARAM_INVALID` |
| 3 | `CheckDtypeValid` | dtype 在支持列表内、out 与 self 一致 | `ACLNN_ERR_PARAM_INVALID` |
| 4 | `CheckFormat` | 格式为 NCHW（扩展路径允许 NHWC） | `ACLNN_ERR_PARAM_INVALID` |
| 5 | `CheckInputElement` | N/C 维一致；扩展路径下 H/W 与 scales 匹配 | `ACLNN_ERR_PARAM_INVALID` |
| 6 | `CheckShape` | 输入输出都是 4 维、scales 长度为 4 | `ACLNN_ERR_PARAM_INVALID` |

一个值得注意的细节：`CheckInputElement` 在扩展路径下用容差判断输出尺寸是否与 scales 相符。设输入高为 \( H \)、缩放系数为 \( s \)、容差 \( \varepsilon = 10^{-5} \)，则输出高 \( H_{out} \) 必须落在：

\[ H \cdot (s - \varepsilon) \;\le\; H_{out}\;\le\; H \cdot (s + \varepsilon) \]

之所以用容差而非精确相等，是因为浮点乘法再取整会产生舍入误差。

#### 4.2.3 源码精读

**总装函数 CheckParams：**
[image/resize_bilinear_v2/op_api/aclnn_resize.cpp:183-199](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L183-L199)

```cpp
static aclnnStatus CheckParams(const aclTensor* self, const aclFloatArray* scales, const char* mode,
                               const aclTensor* out, bool extendFlag)
{
    // 1. 检查参数是否为空指针
    CHECK_RET(CheckNotNull(self, scales, mode, out), ACLNN_ERR_PARAM_NULLPTR);
    // 2. 检查mode是否支持
    CHECK_RET(CheckModeStr(mode), ACLNN_ERR_PARAM_INVALID);
    // 3. 检查参数的数据类型是否符合预期
    CHECK_RET(CheckDtypeValid(self, out, extendFlag), ACLNN_ERR_PARAM_INVALID);
    ...
```

`CHECK_RET(cond, err)` 是 toolkit 提供的宏：cond 为假时直接 `return err`。每个子检查的返回码在这一层显式声明。

**空指针检查：**
[image/resize_bilinear_v2/op_api/aclnn_resize.cpp:126-136](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L126-L136)

```cpp
static bool CheckNotNull(const aclTensor* self, const aclFloatArray* scales, const char* mode, const aclTensor* out)
{
    OP_CHECK_NULL(self, return false);
    OP_CHECK_NULL(scales, return false);
    OP_CHECK_NULL(out, return false);
    if (mode == nullptr) {
        OP_LOGE(ACLNN_ERR_PARAM_INVALID, "mode is null, please check input arguments");
        return false;
    }
```

tensor/array 用 `OP_CHECK_NULL` 宏，C 字符串 `mode` 用手写判空 + `OP_LOGE` 打错误日志。`OP_LOGE` 的第一个参数就是错误码，日志会随错误一起上报——这就是「错误码检查模式」：**每个失败分支都必须带一条含错误码的日志**，方便用户根据日志反查源码行号。

**dtype 双支持列表（按架构区分）：**
[image/resize_bilinear_v2/op_api/aclnn_resize.cpp:53-57](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L53-L57)

```cpp
static const std::initializer_list<op::DataType> DTYPE_SUPPORT_LIST_DATA = {op::DataType::DT_FLOAT16,
                                                                            op::DataType::DT_FLOAT};

static const std::initializer_list<op::DataType> DTYPE_SUPPORT_LIST_DATA_REGBASE = {
    op::DataType::DT_FLOAT16, op::DataType::DT_FLOAT, op::DataType::DT_BF16};
```

普通架构支持 fp16/fp32；RegBase 架构（DAV_3510）额外支持 bf16。`CheckDtypeValid`（[L138-147](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L138-L147)）按 `extendFlag` 选择列表，并用 `OP_CHECK_DTYPE_NOT_MATCH(out, self->GetDataType(), ...)` 保证输出与输入同型。

**shape 与 scales 检查：**
[image/resize_bilinear_v2/op_api/aclnn_resize.cpp:59-77](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L59-L77) 中 `CheckShape` 要求 self/out 都是 4 维、`aclGetFloatArraySize` 取得的 scales 长度必须等于 4；注意源码注释明确说明 **scales 参数只为接口一致性保留，当前不参与计算**——输出 shape 由用户传入的 `out` 直接决定。

**mode 字符串检查：**
[image/resize_bilinear_v2/op_api/aclnn_resize.cpp:149-156](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L149-L156) 用 `strncmp` 前缀匹配 "nearest" / "bilinear"，非法值打日志报 `ACLNN_ERR_PARAM_INVALID`。

#### 4.2.4 代码实践

**实践：手工梳理校验顺序并验证一条错误路径。**

1. 实践目标：把六步校验顺序写成表格，并实际触发一次校验失败，观察错误码与日志。
2. 操作步骤：
   - 阅读 [aclnn_resize.cpp:183-199](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L183-L199)，按本文 4.2.2 的表格逐行核对并补全第 5、6 步。
   - 参考 [examples/test_aclnn_resize.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/examples/test_aclnn_resize.cpp) 写一个最小调用程序，故意把 `mode` 传成 `"bicubic"`（不支持），编译运行。
   - 再故意把 out 的 dtype 构造成 INT32，重复运行。
3. 需要观察的现象：终端出现 `OP_LOGE` 风格的错误日志，分别包含 `CheckModeStr failed, mode:bicubic` 和 dtype 相关报错；接口返回值非 0（`ACLNN_ERR_PARAM_INVALID`）。
4. 预期结果：mode 错误在第 2 步就拦截，不会走到后面的 executor 创建；dtype 错误在第 3 步拦截。日志中的报错文本与源码中 `OP_LOGE` 的格式串逐字对应——这就是「根据日志反查源码行」的 ACLNN 排障方式。若本地无 NPU 环境，此步骤**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CheckNotNull` 失败映射到 `ACLNN_ERR_PARAM_NULLPTR`，而其余检查都是 `ACLNN_ERR_PARAM_INVALID`？这对调用者有什么价值？
**答案**：两种错误的修复方向不同——空指针通常是调用侧漏传参数或对象创建失败，参数非法是取值不合法。区分错误码让调用者能快速定位是「自己代码的 bug」还是「参数配置问题」。

**练习 2**：如果把 `scales` 传成长度为 3 的数组，会在哪一步、由哪个函数拦下？
**答案**：第 6 步，`CheckShape`（[L59-77](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L59-L77)）中 `aclGetFloatArraySize` 取回 size 后与 `NCHW_DIM_NUM` 比较，不等则打日志返回 false，最终 `CheckParams` 返回 `ACLNN_ERR_PARAM_INVALID`。

**练习 3**：`CheckModeStr` 用 `strncmp(mode, "nearest", strlen("nearest")) == 0` 判断，传 `"nearest2"` 会怎样？
**答案**：`strncmp` 只比较前 7 个字符，`"nearest2"` 前 7 位与 `"nearest"` 相同，会通过检查并走 nearest 分支。这是一个前缀匹配的宽松实现，调用者不应依赖此行为传非标准字符串。

### 4.3 非连续处理与执行链编排：Contiguous / TransData / ViewCopy

#### 4.3.1 概念说明

用户传进来的 tensor 可能是切片、转置等产生的**非连续** tensor，且格式可能是 NCHW 而 AI Core 的老架构路径偏好 5HD 分形格式。op_api 层的职责是在第一段里把这些问题全部消化掉，让 L0 算子拿到「干净」的输入。本算子按芯片架构分两条编排路径：

- **扩展路径（extendFlag = true，RegBase/DAV_2201/DAV_1001/DAV_2002 架构）**：Contiguous → 由 scales 构造 sizes 张量 → 直接调 L0 算子（保留原格式，支持 NHWC）→ ViewCopy 到用户输出。
- **V35 路径（其余架构）**：Contiguous → 由 out 的 shape 构造 sizes → TransData 转 NC1HWC0 → L0 算子 → TransData 转回原格式 → ViewCopy。

`aclOpExecutor` 在这里扮演「任务记账本」：每个 l0op 调用都向 executor 登记一个任务（并可能登记临时内存需求），最后 `GetWorkspaceSize()` 汇总出这次计算总共需要多少临时内存。

#### 4.3.2 核心流程

```text
extendFlag = IsRegBase(arch) || DAV_2201 || DAV_1001 || DAV_2002

if extendFlag && IsRegBase():
    selfContig = Contiguous(self)
    outContig  = Contiguous(out)
    sizes      = CreateSizesRegBase(self, scales)   # 由 H*s、W*s 计算目标尺寸
    resizeRet  = ResizeNearestNeighborV2 / ResizeBilinearV2With4d(selfContig, sizes, outContig)
    ViewCopy(resizeRet, out)                        # 拷回用户输出
else:
    sizes    = CreateSizesV35(out)                  # 直接取 out 的 shape
    selfData = TransDataSpecial(selfContig, NC1HWC0)
    outData  = TransDataSpecial(outContig, NC1HWC0)
    resizeRet= ResizeNearestNeighborV2 / ResizeBilinearV2(selfData, sizes, outData)
    outRet   = TransData(resizeRet, self原格式)
    ViewCopy(outRet, out)
```

#### 4.3.3 源码精读

**架构判断两连：GetExtendPathFlag 与 IsRegBase 的关系。**
[image/resize_bilinear_v2/op_api/aclnn_resize.cpp:241-249](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L241-L249)

```cpp
static bool GetExtendPathFlag()
{
    auto curArch = GetCurrentPlatformInfo().GetCurNpuArch();
    if (IsRegBase(curArch) || curArch == NpuArch::DAV_2201 || curArch == NpuArch::DAV_1001 ||
        curArch == NpuArch::DAV_2002) {
        return true;
    }
    return false;
}
```

其中 `IsRegBase` 来自公共头 [common/inc/op_api/aclnn_check.h:23-34](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_api/aclnn_check.h#L23-L34)：

```cpp
static inline bool IsRegBase()
{
    const static std::set<NpuArch> regbaseArch = {NpuArch::DAV_3510};
    auto curArch = GetCurrentPlatformInfo().GetCurNpuArch();
    return regbaseArch.find(curArch) != regbaseArch.end();
}
```

这是 u2-l1 埋下伏笔的正式落点：`IsRegBase` 用一个 `static` 集合判断当前芯片架构是否为 DAV_3510（Atlas A3 等 RegBase 类架构），并提供「传架构参数」的重载版本，避免每个算子各自硬编码芯片型号。`const static` 局部变量保证集合只构造一次。

**sizes 张量的两种构造方式。**
V35 路径直接把 out 的 view shape 转成 int32 张量：[image/resize_bilinear_v2/op_api/aclnn_resize.cpp:201-207](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L201-L207)

```cpp
static const aclTensor* CreateSizesV35(const aclTensor* out, aclOpExecutor* executor)
{
    auto outShape = op::ToShapeVector(out->GetViewShape());
    const aclIntArray* arr = executor->AllocIntArray(outShape.data(), outShape.size());
    auto sizes = executor->ConvertToTensor(arr, op::ToOpDataType(ACL_INT32));
    return sizes;
}
```

注意 `executor->AllocIntArray` / `ConvertToTensor`：临时数组与张量都从 executor 的内存池分配，生命周期随 executor——这就是 executor「记账本+资源池」的双重身份。RegBase 路径则相反：由 self 的 H/W 乘以 scales 算出目标尺寸（[L209-239](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L209-L239)），并按 NCHW/NHWC 取对应的维下标。

**V35 路径的完整编排。**
[image/resize_bilinear_v2/op_api/aclnn_resize.cpp:289-315](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L289-L315)

```cpp
    } else {
        auto sizes = CreateSizesV35(out, uniqueExecutor.get());
        ...
        auto selfData = l0op::TransDataSpecial(selfContiguous, Format::FORMAT_NC1HWC0, 0, uniqueExecutor.get());
        ...
        auto outData = l0op::TransDataSpecial(outContiguous, Format::FORMAT_NC1HWC0, 0, uniqueExecutor.get());
        ...
        const aclTensor* resizeRet = nullptr;
        if (strncmp(mode, "nearest", strlen("nearest")) == 0) {
            resizeRet = l0op::ResizeNearestNeighborV2(selfData, sizes, nullptr, false, false, outData, ...);
        } else if (strncmp(mode, "bilinear", strlen("bilinear")) == 0) {
            resizeRet = l0op::ResizeBilinearV2(selfData, sizes, false, outData, ...);
        }
        ...
        auto outRet = l0op::TransData(resizeRet, self->GetStorageFormat(), 0, uniqueExecutor.get());
        ...
        auto viewCopyResult = l0op::ViewCopy(outRet, out, uniqueExecutor.get());
```

可以看到：`mode` 参数最终在这里分流到不同的 L0 算子；每一步的返回值都判空（`ACLNN_ERR_INNER_NULLPTR`），任何一环失败都会带着错误码提前返回。而 RegBase 分支（[L272-288](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L272-L288)）少了两次 TransData——源码注释写明「不必转到5HD，直接执行L0算子」，这正是新架构省掉格式转换的性能收益。

**L0 算子内部在做什么（顺带一瞥）。**
[image/resize_bilinear_v2/op_api/resize_bilinear_v2.cpp:32-48](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/resize_bilinear_v2.cpp#L32-L48) 中 `OP_TYPE_REGISTER(ResizeBilinearV2)` 注册算子类型，`IsAiCoreSupport` 按 dtype（RegBase 用独立列表）判断能否走 AI Core；[L51-80](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/resize_bilinear_v2.cpp#L51-L80) 展示了 AiCPU / AiCore 两个实现都通过 `ADD_TO_LAUNCHER_LIST_AICPU` / `ADD_TO_LAUNCHER_LIST_AICORE` 把任务追加进 executor 的下发列表——即 op_api 层调 L0 算子时「登记而非执行」的直接证据。

#### 4.3.4 代码实践

**实践：画出 aclnnResize 到算子执行框架的时序草图。**

1. 实践目标：把 4.1.2 与 4.3.2 的文字流程落实为自己的一张时序图。
2. 操作步骤：
   - 以「用户代码 / aclnnResizeGetWorkspaceSize / CheckParams / l0op(L0算子) / aclOpExecutor / CommonOpExecutorRun / stream」为 7 个参与者。
   - 按 [L251-327](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L251-L327) 的执行顺序画消息：校验 → CREATE_EXECUTOR → Contiguous×2 → (选定架构路径) CreateSizes → TransData×2（仅 V35）→ L0 Resize 算子 → TransData 回（仅 V35）→ ViewCopy → GetWorkspaceSize → ReleaseTo；第二段只有 CommonOpExecutorRun → stream 一条消息。
   - 在 TransData / L0 算子调用旁标注「登记任务到 executor，不立即执行」。
3. 需要观察的现象：图中所有计算类消息都汇聚到 executor 这条生命线，第二段才统一触发下发。
4. 预期结果：得到一张能向他人讲解「为什么第一段厚、第二段薄」的时序图；这也是后续学习 op_host/tiling（u3 单元）前最重要的心智模型。

#### 4.3.5 小练习与答案

**练习 1**：RegBase 路径比 V35 路径少了哪些 l0op 调用？为什么可以少？
**答案**：少了两次 `TransDataSpecial`（转 NC1HWC0）和一次 `TransData`（转回）。因为 RegBase（DAV_3510）等新架构的 L0 算子（`ResizeBilinearV2With4d`）直接吃原始 4D 格式（含 NHWC），不需要 5HD 分形排布。

**练习 2**：用户传入的 `out` 是非连续 tensor，最终结果怎么写回去？
**答案**：计算在 `outContiguous`（Contiguous 产物）上完成，最后一步 `l0op::ViewCopy(resizeRet, out, executor)` 把连续结果按视图拷回用户提供的（可能非连续的）out 内存。所以用户只需保证 out 的 shape/dtype/format 正确，不需要自己保证连续性。

**练习 3**：`CreateSizesV35` 里的 `AllocIntArray` 为什么必须从 executor 分配而不是 `new` 一块内存？
**答案**：第一段登记的任务在第二段才执行，普通局部内存在第一段返回后就可能释放，第二段执行时会读到悬空指针。executor 的内存池生命周期覆盖两次调用，且统一在 executor 销毁时释放，避免泄漏。

### 4.4 L2_DFX 打点与 common 公共基础设施

#### 4.4.1 概念说明

- **DFX 打点**：Design for X（可测试、可观测）的日志埋点。`L2_DFX_PHASE_1` 在第一段入口记录算子名、输入（`DFX_IN`）、输出（`DFX_OUT`）；`L2_DFX_PHASE_2` 在第二段入口记录算子名。配合 CANN 的算子日志开关（如 `ASCEND_GLOBAL_LOG_LEVEL`、`ASCEND_SLOG_PRINT_TO_STDOUT`），可以在运行时打印出「哪个算子、什么输入、走没走到第二段」。
- **L0/L1/L2 分层打点**：L2 对应 aclnn 特性级接口（本讲），L0 对应底层原子算子（如 4.3.3 看到的 `L0_DFX(ResizeBilinearV2AICPU, ...)`）。层级化的目的是问题定界：日志里只有 L2 说明第一段就出问题，有 L2 有 L0 说明下发链路正常。
- **ACLNN 特性级行号定位**：aclnn 报错日志会带上特性名（如 `aclnnResize`），配合源码中每个失败分支唯一的 `OP_LOGE` 文本，可以直接把报错定位到 `aclnn_resize.cpp` 的具体行——这也是 4.2 里「每个失败分支必须带含错误码日志」的原因。
- **common/inc/op_api**：仓库级公共层。`aclnn_check.h` 提供架构判断；`level2_base.h` 收敛了各算子重复的检查函数；`op_api_def.h` 放跨算子常量。

#### 4.4.2 核心流程

一个新算子 op_api 的「复用决策树」：

```text
需要判空 N 个 tensor？
  → 能对上签名就用 level2_base.h 的 CheckNotNull3Tensor/4Tensor...
需要 dtype 白名单 + in/out 同型？
  → CheckDtypeValid1In1OutScalar / 1In1OutTensor
需要按架构选 dtype 列表？
  → GetDtypeSupportListV2(l1, l2)   // DAV_2201 或 RegBase 用 l1
维度上限检查？
  → OP_CHECK_MAX_DIM(self, MAX_SUPPORT_DIMS_NUMS)  // 8, 来自 op_api_def.h
判断当前芯片是否 RegBase？
  → IsRegBase() / IsRegBase(arch)    // aclnn_check.h
都不是 → 在本算子文件里写 static CheckXxx（如 aclnn_resize.cpp 的做法）
```

#### 4.4.3 源码精读

**两处 L2_DFX 打点。**
[image/resize_bilinear_v2/op_api/aclnn_resize.cpp:256](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L256) 与 [L325](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L325)：

```cpp
L2_DFX_PHASE_1(aclnnResize, DFX_IN(self, scales, mode), DFX_OUT(out));   // 第一段入口
...
L2_DFX_PHASE_2(aclnnResize);                                            // 第二段入口
```

宏本体在 toolkit 的 `opdev/op_dfx.h` 中（仓库外）。使用约定：`L2_DFX_PHASE_1` 必须是校验前的第一条业务语句（先于 CheckParams），保证即使校验失败也能在日志里看到「进过这个算子」。

**公共检查函数库 level2_base.h 的代表性片段。**
[common/inc/op_api/level2_base.h:36-45](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_api/level2_base.h#L36-L45)

```cpp
static bool CheckNotNull3Tensor(const aclTensor* t0, const aclTensor* t1, const aclTensor* t2)
{
    // 检查输入是否是空指针
    OP_CHECK_NULL(t0, return false);
    OP_CHECK_NULL(t1, return false);
    OP_CHECK_NULL(t2, return false);
    return true;
}
```

[common/inc/op_api/level2_base.h:85-93](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_api/level2_base.h#L85-L93) 的 `CheckSameShape1In1Out` 组合了「shape 相等 + 维度 ≤ 8」两条规则；[L112-129](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_api/level2_base.h#L112-L129) 的 `CheckDtypeValid1In1OutScalar` 一次校验输入/属性/输出三份 dtype 白名单及 in/out 同型。

[common/inc/op_api/level2_base.h:175-184](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_api/level2_base.h#L175-L184)

```cpp
static const std::initializer_list<DataType>& GetDtypeSupportListV2(const std::initializer_list<op::DataType>& l1,
                                                                    const std::initializer_list<op::DataType>& l2)
{
    auto curArch = GetCurrentPlatformInfo().GetCurNpuArch();
    if (curArch == NpuArch::DAV_2201 || IsRegBase(curArch)) {
        return l1;
    } else {
        return l2;
    }
}
```

它把「按架构选 dtype 列表」这一在各算子中反复出现的模式收敛为一个函数——对比 `aclnn_resize.cpp` 里手写的两份 `DTYPE_SUPPORT_LIST_DATA` / `..._REGBASE` 列表 + `extendFlag` 三元选择，可以看出哪些代码适合上提到公共层。还有 [L186-203](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_api/level2_base.h#L186-L203) 的 `GetDtypeSupportListV3`，用 switch 区分 DAV_2201/DAV_3510/DAV_1001 三类架构（源码注释里的 1971/1980 是架构代号的习惯叫法）。

**公共常量。**
[common/inc/op_api/op_api_def.h:19-21](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_api/op_api_def.h#L19-L21)

```cpp
namespace op {
constexpr size_t MAX_SUPPORT_DIMS_NUMS = 8;
} // namespace op
```

整个头文件只有这一个常量——CANN aclTensor 最多支持 8 维，各算子的维度上限检查统一引用它，避免魔法数字散落各处。

#### 4.4.4 代码实践

**实践：打开算子日志观察 L2_DFX 输出。**

1. 实践目标：亲眼看到 `aclnnResize` 的两级打点日志，建立「日志 ↔ 源码」的条件反射。
2. 操作步骤：
   - 按 u1-l4 的方式编译安装 resize_bilinear_v2 算子包并编译运行 [examples/test_aclnn_resize.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/examples/test_aclnn_resize.cpp)。
   - 运行前导出日志环境变量：`export ASCEND_GLOBAL_LOG_LEVEL=0`（DEBUG）与 `export ASCEND_SLOG_PRINT_TO_STDOUT=1`。
   - 观察 stdout 中含 `ACLNN` / `aclnnResize` / `DFX` 字样的行，以及是否出现 `ResizeBilinearV2AICORE` 或 `ResizeBilinearV2AICPU` 等下层算子名。
3. 需要观察的现象：日志按「L2 特性级 → L0 算子级」顺序出现；对照 4.3.3 的 `L0_DFX` 调用点可以判断本次走的是 AiCore 还是 AiCPU 实现。
4. 预期结果：能从日志还原出 4.3.4 时序图中实际执行的那条架构路径。本实践依赖真实 NPU 环境，**待本地验证**（环境变量名与级别取值请以所用 CANN 版本的日志文档为准）。

#### 4.4.5 小练习与答案

**练习 1**：日志里出现了 `aclnnResize`（L2）但没有任何 L0 算子名，最可能发生了什么？
**答案**：第一段在 L0 算子登记之前就返回了——大概率是参数校验失败（CheckParams 六步之一），或 `CREATE_EXECUTOR`/`Contiguous` 失败。应从同一条日志里的 `OP_LOGE` 错误文本反查 `aclnn_resize.cpp` 的具体分支。

**练习 2**：`level2_base.h` 里的函数为什么全部是 `static`？包含它会有什么代价？
**答案**：`static` 函数具有内部链接，每个包含该头文件的编译单元都会得到一份副本，不产生链接冲突，也便于算子按需裁剪；代价是二进制体积略微增大、函数无法在别的翻译单元复用，但对以动态库交付的单算子来说代价可忽略。

**练习 3**：如果要在 `aclnn_resize.cpp` 中新增支持一种 dtype（如 DT_INT16），需要改哪几处？
**答案**：把 `DT_INT16` 加进 `DTYPE_SUPPORT_LIST_DATA`（及需要的话 `..._REGBASE`）即可通过 op_api 层校验；但这只是「放行」，实际能否计算还取决于 L0 算子 `resize_bilinear_v2.cpp` 中 `AICORE_DTYPE_SUPPORT_LIST` 与底层 kernel 的支持情况——这引出下一单元的 op_host/op_kernel 主题。

## 5. 综合实践

**任务：为 aclnnResize 写一份「实现者笔记」。**

结合本讲全部内容，完成一份 markdown 笔记，包含三部分：

1. **校验顺序表**：照着 [aclnn_resize.cpp:183-199](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L183-L199) 补全六步检查表（本文 4.2.2 已给出前 4 步的框架），每步注明：函数名、检查内容、失败错误码、对应源码行号链接。
2. **双路径对比表**：对比 RegBase 扩展路径（[L272-288](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L272-L288)）与 V35 路径（[L289-315](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L289-L315)）的 l0op 调用序列、sizes 构造方式、支持的 format 与 dtype 差异。
3. **时序草图**：完成 4.3.4 的时序图，并用一句话回答——如果把 `l0op::Contiguous(out, ...)` 这一行删掉，什么场景下结果会出错？

参考答案（第 3 问）：当用户传入的 `out` 本身非连续（例如是对更大 tensor 的切片）时，后续 L0 算子按连续排布写入 `outContiguous` 再由 ViewCopy 映射回视图；若跳过 Contiguous 直接把非连续 out 交给按连续假设实现的 L0 算子，数据会写错位置，输出内容错乱。

## 6. 本讲小结

- aclnn 实现文件遵循三段结构：static 检查函数 + 厚重的 `GetWorkspaceSize`（校验、建 executor、编排 L0 任务链、汇报 workspace）+ 极薄的执行函数（打点 + `CommonOpExecutorRun`）。
- 参数校验按固定六步顺序（非空 → mode 语义 → dtype → format → 元素合法性 → shape），每步失败都映射到 `ACLNN_ERR_PARAM_NULLPTR` / `ACLNN_ERR_PARAM_INVALID` 并伴随含错误码的 `OP_LOGE` 日志，支持从日志反查源码行。
- `aclOpExecutor` 是「任务记账本 + 资源池」：Contiguous、TransData、L0 算子、ViewCopy 在第一段只登记不执行，临时内存也从它分配，第二段才统一下发到 stream。
- 同一算子按芯片架构走不同编排路径：RegBase/新架构路径免 5HD 转换并支持 NHWC 与 bf16；V35 老架构路径需要 TransData 往返。
- `L2_DFX_PHASE_1/2` 是特性级 DFX 打点，与 L0 算子的 `L0_DFX` 配合实现问题定界；宏本体来自 CANN toolkit，仓库只负责使用。
- `common/inc/op_api` 提供公共能力：`aclnn_check.h` 的 `IsRegBase` 架构判断、`level2_base.h` 的成套检查函数与按架构选 dtype 列表、`op_api_def.h` 的维度上限常量。

## 7. 下一步学习建议

- 下一讲（u2-l3）转向调用侧的全景：build.sh 快速调用、业务工程集成与 PyTorch 扩展三种方式。
- 想先深入算子本体的话，可直接进入 u3 单元：u3-l1 将以 resize_bilinear_v2 为对象，从 op_api 一路追到 op_host（def/infershape/tiling）与 op_kernel，补全本讲刻意停在「L0 算子登记」处的那半张地图。
- 建议顺带浏览另一个 op_api 实现做横向对照，例如 `objdetect/roi_align_grad/op_api/aclnn_roi_align_v2_backward.cpp`，检验自己能否脱离讲义独立读出三段结构与校验顺序。
