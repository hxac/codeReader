# aclnn API 调用算子：两段式接口与调用流程

## 1. 本讲目标

上一讲（u1-l4）我们在 `test_aclnn_add_example.cpp` 里「照抄」了一份调用样例，本讲要把这份样例拆开讲透。学完本讲，你应该能够：

1. 说出 aclnn 两段式接口（`aclnnXxxGetWorkspaceSize` + `aclnnXxx`）各自的职责、参数含义和返回码处理方式。
2. 读懂 op_api 适配层源码：以 `activation/gelu` 为例，理解一次 `aclnnGelu` 调用在 Host 侧是如何经过参数校验、连续性转换，最终被挂到执行器（executor）上等Device执行的。
3. 独立仿照 `test_aclnn_add_example.cpp` 编写一个调用其他算子（如 `aclnnGelu`）的最小 C++ 样例，并与 CPU 参考值对账。

## 2. 前置知识

在进入源码之前，先用通俗语言把几个本讲反复出现的概念说清楚：

- **Host 侧与 Device 侧**：Host 指 CPU 及其内存（host 内存），Device 指 NPU（昇腾 AI 处理器）及其设备内存。算子计算在 Device 上完成，但「准备输入、下发任务、取回结果」都发生在 Host 侧。数据不能直接跨侧访问，必须通过 `aclrtMemcpy` 之类的接口搬运。
- **aclTensor**：CANN 对「设备上的一块多维数据」的描述符，包含 shape、数据类型、format、strides 和设备内存地址。注意 aclTensor 本身只是一个「名片」，真正的数据在 Device 内存里。
- **workspace**：算子执行时可能需要的 Device 侧临时工作内存。有的算子不需要（大小为 0），有的需要。到底需要多少，由算子自己计算——这正是两段式接口第一段的职责之一。
- **stream（aclrtStream）**：Device 上的任务队列。向 stream 提交的任务异步执行，`aclrtSynchronizeStream` 用来等待队列中所有任务完成。
- **aclnnStatus / aclError**：CANN 的返回码类型，`ACL_SUCCESS`（值为 0）和 `ACLNN_SUCCESS` 表示成功，其余为各类错误码。几乎每个调用之后都要检查返回码。
- **executor（aclOpExecutor）**：算子执行器，可以理解为「一张已经排好的算子执行清单」。第一段接口把要做的计算（可能包含多个底层算子调用）登记进 executor，第二段接口把这张清单交给运行时去真正执行。

如果这几个词还陌生，建议先回看 u1-l4 的实践再继续。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/zh/invocation/quick_op_invocation.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/invocation/quick_op_invocation.md) | 官方算子调用总文档：快速调用（build.sh 跑样例）与业务集成（自建调用工程）两条路径 |
| [activation/gelu/op_api/aclnn_gelu.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.h) | `aclnnGelu` 两段式接口的声明与接口文档（参数约束、计算公式、计算图） |
| [activation/gelu/op_api/aclnn_gelu.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp) | aclnn 适配层实现：参数校验 + 组装执行流程（本讲主角） |
| [activation/gelu/op_api/gelu.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/gelu.cpp) | l0 层算子函数：把 Gelu 挂到 AI Core 执行列表，是 aclnn 层的下游 |
| [examples/add_example/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp) | 标准调用样例骨架：初始化 → 构造 aclTensor → 两段式调用 → 同步取回 |

## 4. 核心概念与源码讲解

### 4.1 两段式接口设计：为什么拆成两段

#### 4.1.1 概念说明

所有 aclnn 算子 API 都长成同一个模样——两个 C 函数：

1. **第一段 `aclnnXxxGetWorkspaceSize(...)`**：做「准备工作」。它在 Host 侧完成参数校验、把要执行的算子（可能不止一个，比如还伴随格式转换算子）登记进 executor，并计算出本次执行需要的 workspace 大小。它**不下发执行**。
2. **第二段 `aclnnXxx(workspace, workspaceSize, executor, stream)`**：把 executor 里的执行清单连同 workspace 内存提交到 stream 上，异步启动 Device 计算。

为什么这么设计？直观原因有两个：

- workspace 由**用户**申请和持有（用 `aclrtMalloc`），算子库不替你管内存，第一段告诉你「要多少」，你按需分配，避免每次调用都由库内部反复申请释放。
- 第一段与第二段之间允许插入用户自己的逻辑（比如把多个算子的执行编排在同一条 stream 上），这是 eager（立即执行）模式下灵活集成的基础。

#### 4.1.2 核心流程

```text
用户代码
  │
  ├─① aclnnGeluGetWorkspaceSize(self, out, &workspaceSize, &executor)
  │      Host 侧：校验参数 → 登记 Contiguous/Gelu/ViewCopy 到 executor
  │      → *workspaceSize = executor->GetWorkspaceSize()
  │
  ├─② workspaceSize > 0 ? aclrtMalloc(&workspaceAddr, ...)
  │
  ├─③ aclnnGelu(workspaceAddr, workspaceSize, executor, stream)
  │      → CommonOpExecutorRun(...): 把 executor 提交到 stream 异步执行
  │
  ├─④ aclrtSynchronizeStream(stream)   # 等待 Device 计算真正完成
  │
  └─⑤ aclrtMemcpy(..., DEVICE_TO_HOST) # 把结果搬回 host 检查
```

gelu 的计算公式（见头文件注释）为：

\[ out_i = Gelu(self_i) = self_i \times \Phi(self_i) \]

其中标准正态分布的累积分布函数

\[ \Phi(x) = \frac{1}{2}\left(1 + \mathrm{erf}\left(\frac{x}{\sqrt{2}}\right)\right) \]

后面实践环节验证精度时，CPU 参考值就用这个公式计算。

#### 4.1.3 源码精读

两段式接口的「合同」写在头文件里。[aclnn_gelu.h:L46-L58](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.h#L46-L58) 声明了两个函数：第一段接收 `self`、`out` 两个 aclTensor，输出 `workspaceSize` 和 `executor`；第二段接收 workspace 地址/大小、executor 和 stream。注意两个函数都包在 `extern "C"` 块中（[aclnn_gelu.h:L16-L19](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.h#L16-L19)），保证 C 链接，便于任何语言绑定。

头文件注释本身就是接口文档：[aclnn_gelu.h:L36-L43](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.h#L36-L43) 说明了每个参数的约束（dtype 支持 FLOAT16/FLOAT32/BFLOAT16 且 self 与 out 一致、shape 一致、支持非连续 Tensor）。**写调用代码前先读这段注释**，是排查「参数不合法」类错误的最快路径。注释里还贴心地画了计算图（[aclnn_gelu.h:L27-L34](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.h#L27-L34)）：输入先经 `Contiguous` 转连续，再过 `Gelu`，最后 `ViewCopy` 写回 out——这正对应 4.2 节将要读的实现。

#### 4.1.4 代码实践（源码阅读型）

1. 实践目标：学会从任意算子的 aclnn 头文件中提取「调用契约」。
2. 操作步骤：打开 [activation/gelu/op_api/aclnn_gelu.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.h)，把支持的数据类型、shape 约束、format 约束抄成一张三行表格。
3. 需要观察的现象：无（纯阅读）。
4. 预期结果：得到「dtype ∈ {FLOAT16, FLOAT32, BFLOAT16}，self/out 同型同形，format 为 ND 类」的结论，与本讲 4.2.3 中 `CheckDtypeValid`/`CheckShape` 的实现互相印证。

#### 4.1.5 小练习与答案

**练习 1**：如果把第一段的 `executor` 输出参数传成 `nullptr`，会发生什么？

答案：无法通过参数入口检查。实现第一行就调用了 `OP_CHECK_COMM_INPUT(workspaceSize, executor)`（[aclnn_gelu.cpp:L92](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L92)），公共宏会检查这两个指针非空并返回参数空指针错误（`ACLNN_ERR_PARAM_NULLPTR` 类），不会走到创建 executor 的逻辑。

**练习 2**：为什么第二段接口不直接返回计算结果？

答案：第二段只是把任务**异步**提交到 stream，函数返回时 Device 可能还没算完。结果数据在 Device 内存中，需要用户先 `aclrtSynchronizeStream`，再自行 `aclrtMemcpy` 拷回 host。所以第二段只返回「提交是否成功」的状态码。

### 4.2 aclnn 适配层源码精读：一次 aclnnGelu 调用在 Host 侧经历了什么

#### 4.2.1 概念说明

`op_api` 目录是算子对外的 aclnn 适配层。它站在「用户的一句 `aclnnGelu(...)`」和「Device 上真正跑的 AI Core kernel」之间，职责是：

- 把 C 风格的 aclTensor 参数翻译成内部执行描述；
- 做合法性校验（空指针、dtype、shape、format），把问题在 Host 侧就拦下来，而不是让 kernel 在 Device 上跑飞；
- 处理「数据不满足 kernel 要求」的情况——最典型的就是非连续 Tensor 要先转连续；
- 把最终的算子调用登记进 executor，形成一张可执行清单。

在 u1-l3 里我们说过「缺 op_api 就不支持 aclnn 调用」，现在可以看到这一层的具体形态。

#### 4.2.2 核心流程

以 [aclnn_gelu.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp) 为例，第一段接口的执行步骤：

1. `OP_CHECK_COMM_INPUT`：检查 `workspaceSize`/`executor` 出参指针非空。
2. `CREATE_EXECUTOR()`：创建 `uniqueExecutor`（RAII 管理，出错自动释放）。
3. `CheckParams`：依次做空指针、dtype、format、shape 四类校验，任一失败即返回对应错误码。
4. 空 tensor 短路：输入元素数为 0 时直接登记完毕、workspace 记 0 返回。
5. `l0op::Contiguous(self)`：若输入非连续，登记一个转连续的算子调用。
6. `l0op::Gelu(...)`：登记真正的 Gelu AI Core 算子调用（见下文 l0 层）。
7. `l0op::ViewCopy(geluResult, out)`：把连续结果写回用户提供的 out（out 可能非连续）。
8. `GetWorkspaceSize()` + `ReleaseTo(executor)`：算出 workspace 大小，把 executor 所有权移交给调用者。

第二段接口则极其简短：交给公共函数 `CommonOpExecutorRun` 提交执行。

#### 4.2.3 源码精读

**参数校验链**。dtype 支持列表定义在 [aclnn_gelu.cpp:L23-L24](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L23-L24)（FLOAT/FLOAT16/BF16），[aclnn_gelu.cpp:L72-L87](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L72-L87) 的 `CheckParams` 把四类检查按「空指针 → dtype → format → shape」的顺序串起来，每个检查失败返回不同的 `ACLNN_ERR_*` 错误码。注意 BF16 有额外的芯片能力判断（[aclnn_gelu.cpp:L26-L29](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L26-L29)）——老架构不支持 BF16，会在 Host 侧直接报参数错误。

**第一段主体**。[aclnn_gelu.cpp:L89-L127](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L89-L127) 是完整的 `aclnnGeluGetWorkspaceSize`。其中核心的三行「组装执行流程」在 [aclnn_gelu.cpp:L112-L121](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L112-L121)：`Contiguous` → `Gelu` → `ViewCopy`，与头文件注释中的 mermaid 计算图一一对应。一次「用户眼里的单个算子调用」，在 executor 里实际可能是三个底层算子的串联——这是 aclnn 适配层最重要的心智模型。

**l0 层**。`l0op::Gelu` 定义在 [gelu.cpp:L22-L31](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/gelu.cpp#L22-L31)：先用 `executor->AllocTensor` 按输入的 shape/dtype 分配输出，再用 `ADD_TO_LAUNCHER_LIST_AICORE(Gelu, OP_INPUT(self), OP_OUTPUT(out))` 把 Gelu 这个 AI Core kernel 连同输入输出登记到执行列表。注意这里同样**没有执行**，只是登记。

**第二段主体**。[aclnn_gelu.cpp:L129-L134](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L129-L134) 的 `aclnnGelu` 只有一行有效逻辑：`CommonOpExecutorRun(workspace, workspaceSize, executor, stream)`，由公共运行时把 executor 中登记的全部算子提交到 stream 执行。gelu 不需要 workspace 时，第二段传入的 workspace 指针可以为 `nullptr`。

#### 4.2.4 代码实践（源码阅读型）

1. 实践目标：走通「aclnn 层 → l0 层 → 执行列表」的静态调用链。
2. 操作步骤：
   - 从 [aclnn_gelu.cpp:L116](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L116) 的 `l0op::Gelu(selfIsContiguous, uniqueExecutor.get())` 出发；
   - 跳到 [gelu.cpp:L27](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/gelu.cpp#L27) 的 `ADD_TO_LAUNCHER_LIST_AICORE`；
   - 再用 Grep 在仓库里搜 `ADD_TO_LAUNCHER_LIST_AICORE`，看它最终关联到 op_kernel 里的哪个 Gelu 实现。
3. 需要观察的现象：纯阅读，观察「登记」与「执行」在代码上是分离的两个阶段。
4. 预期结果：能画出 `aclnnGelu → CheckParams → Contiguous/Gelu/ViewCopy 登记 → CommonOpExecutorRun` 的时序草图。Grep 公共宏定义的位置可能随 CANN 版本变化，若在仓库内搜不到定义体（定义在 CANN toolkit 头文件中），标注「待确认」即可。

#### 4.2.5 小练习与答案

**练习 1**：用户传入一个 shape 为 `{2,3}` 但 strides 与连续布局不符（例如转置视图）的 `self`，aclnnGelu 还能正确工作吗？走哪条代码路径？

答案：能。`l0op::Contiguous(self, ...)`（[aclnn_gelu.cpp:L112-L113](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L112-L113)）会先登记一个转连续的算子调用；计算完成后 `l0op::ViewCopy(geluResult, out, ...)`（[aclnn_gelu.cpp:L120-L121](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L120-L121)）再把连续结果按 out 的布局写回。这也解释了头文件注释中「支持非连续的 Tensor」的底气。

**练习 2**：如果 `self` 的 dtype 是 `ACL_INT32`，调用第一段接口会返回什么？在校验链的哪一步被拦下？

答案：返回 `ACLNN_ERR_PARAM_INVALID`。在 `CheckDtypeValid` 中被 `OP_CHECK_DTYPE_NOT_SUPPORT` 宏拦下（[aclnn_gelu.cpp:L46](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L46)），因为 INT32 不在 `DTYPE_SUPPORT_LIST` 里。此错误发生在 Host 侧、任何 kernel 启动之前。

**练习 3**：空 tensor（shape 含 0，元素数为 0）输入时为什么可以直接返回？

答案：见 [aclnn_gelu.cpp:L104-L109](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L104-L109)：没有任何元素需要计算，登记空流程并把 workspace 记为 0 后即返回成功，避免后续 Contiguous/Gelu/ViewCopy 对空数据做无意义处理。

### 4.3 标准调用样例骨架：test_aclnn_add_example.cpp 逐段拆解

#### 4.3.1 概念说明

调用一个 aclnn 算子的 C++ 代码有一套高度模板化的「七步骨架」。掌握这个骨架后，换成任何算子只需要改两处：算子名（API 函数）和输入输出的构造方式。仓库中每个算子 `examples/` 目录下的 `test_aclnn_*.cpp` 都是这个骨架的实例。

#### 4.3.2 核心流程

七步骨架（与 u1-l4 实践呼应，这里给出精确的代码定位）：

```伪代码
main:
  ① Init: aclInit → aclrtSetDevice → aclrtCreateStream
  ② 构造输入/输出 aclTensor:
     host 数据 → aclrtMalloc(device) → aclrtMemcpy(H2D) → aclCreateTensor
  ③ 声明 workspaceSize 与 executor
  ④ 第一段: aclnnXxxGetWorkspaceSize(...) → 检查返回码
     若 workspaceSize > 0: aclrtMalloc 申请 workspace
  ⑤ 第二段: aclnnXxx(workspace, workspaceSize, executor, stream)
  ⑥ aclrtSynchronizeStream(stream)
  ⑦ aclrtMemcpy(D2H) 取回结果并校验
收尾: aclrtDestroyStream → aclrtResetDevice → aclFinalize
```

#### 4.3.3 源码精读

**初始化**：[test_aclnn_add_example.cpp:L54-L64](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L54-L64) 的 `Init` 依次完成 `aclInit`（ACL 系统初始化，全进程一次）、`aclrtSetDevice`（绑定 0 号设备）、`aclrtCreateStream`（创建任务流）。

**构造 aclTensor**：[test_aclnn_add_example.cpp:L66-L88](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L66-L88) 的 `CreateAclTensor` 是最值得抄走的工具函数：申请 Device 内存 → host 数据拷入 → 按连续布局计算 strides → `aclCreateTensor` 组装描述符。注意 strides 的计算方式（从倒数第二维往前累乘，[test_aclnn_add_example.cpp:L79-L82](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L79-L82)）只对**连续** Tensor 成立，构造非连续输入需要自行修改。

**两段式调用**：[test_aclnn_add_example.cpp:L130-L145](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L130-L145) 是骨架的核心：第一段拿到 `workspaceSize` 和 `executor`；仅当大小非零才 `aclrtMalloc`（[L137-L141](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L137-L141)）；随后第二段下发。之后 [L148-L149](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L148-L149) 同步等待，`PrintOutResult`（[L38-L52](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L38-L52)）里用 `aclrtMemcpy(ACL_MEMCPY_DEVICE_TO_HOST)` 取回前 10 个元素打印。

**资源释放**：[test_aclnn_add_example.cpp:L156-L170](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L156-L170) 的 `main` 收尾按「销毁 stream → 复位设备 → `aclFinalize`」的固定顺序释放；过程中的 aclTensor 与 Device 内存则由 `std::unique_ptr` 自定义删除器自动释放（[L104-L105](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L104-L105)），这是防止提前 return 造成泄漏的推荐写法。

**快速验证路径**：不想自建工程时，可以用 build.sh 直接跑样例（[quick_op_invocation.md:L39-L55](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/invocation/quick_op_invocation.md#L39-L55)）：`bash build.sh --run_example ${op} eager cust --vendor_name=custom`；自建工程的完整 CMake/run.sh 模板见 [quick_op_invocation.md:L326-L394](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/invocation/quick_op_invocation.md#L326-L394)（注意调用内置算子链接 `libopapi_nn.so`、调用自定义算子包链接 `libcust_opapi.so` 的区别）。

#### 4.3.4 代码实践

1. 实践目标：体验「改样例只需改构造、不改骨架」。
2. 操作步骤：在本地配套环境中，把 `test_aclnn_add_example.cpp` 中三处 shape 从 `{32, 4, 4, 4}` 改为 `{8, 8, 8, 8}`（vector 长度相应改为 4096），重新执行 `bash build.sh --run_example add_example eager cust --vendor_name=custom`。
3. 需要观察的现象：输出打印的元素个数、数值以及执行是否成功。
4. 预期结果：打印 10 个 `result[i] = 2.000000`（输入仍为全 1 相加）。若 shape 乘积与 host 数据 vector 长度不一致，会出现越界或数据错误——这正是 u1-l4 强调过的约束。**待本地验证**（本讲义编写环境无 NPU 硬件）。

#### 4.3.5 小练习与答案

**练习 1**：为什么样例中每个 aclTensor 都要配一个 `unique_ptr`？

答案：函数中有大量 `CHECK_RET(..., return ret)` 提前返回路径。裸指针在这些路径上不会被释放；`unique_ptr` 以 `aclDestroyTensor`/`aclrtFree` 作删除器（[test_aclnn_add_example.cpp:L104-L105](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L104-L105)），无论从哪条路径退出都能自动回收，等价于其他语言的 try-with-resources。

**练习 2**：如果删掉第⑥步的 `aclrtSynchronizeStream` 直接 `aclrtMemcpy` 取结果，一定出错吗？

答案：不一定立即出错，但是**未定义行为的竞态**——第二段只是异步提交，Device 可能尚未写完 out 对应的 Device 内存。部分实现里 `aclrtMemcpy` 隐式同步同 stream 任务从而「碰巧正确」，但不能依赖该行为；规范写法必须先同步再取数。

### 4.4 返回码与错误处理约定

#### 4.4.1 概念说明

aclnn 体系的错误处理有两个层面：

- **用户侧**：每个 acl/aclnn 调用后立即检查返回值，非 0 即失败。样例用 `CHECK_RET` 宏把「检查 + 打印 + 提前返回」压成一行。
- **适配层侧**：`CheckParams` 系列函数把不同问题映射为不同的 `ACLNN_ERR_*` 错误码（`ACLNN_ERR_PARAM_NULLPTR`、`ACLNN_ERR_PARAM_INVALID`、`ACLNN_ERR_INNER_NULLPTR` 等），用户拿到错误码后可以定位到是参数问题还是内部问题。

#### 4.4.2 核心流程

```text
调用返回非 0
  ├─ ACLNN_ERR_PARAM_NULLPTR   → 传了空指针，检查自己的入参
  ├─ ACLNN_ERR_PARAM_INVALID   → dtype/shape/format 不支持，对照头文件注释
  ├─ ACLNN_ERR_INNER_*         → 适配层内部错误（executor 创建失败等）
  └─ 其他 aclError             → 运行时/内存类错误，看 Host 日志
```

排错的第一步永远是打开 Host 侧日志（环境变量方式见 docs 的 debug 章节，第 8 单元详讲），适配层的 `OP_LOGE` 会打出具体原因。

#### 4.4.3 源码精读

样例中的检查宏定义在 [test_aclnn_add_example.cpp:L17-L27](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp#L17-L27)：`CHECK_RET` 在条件不成立时执行清理并返回错误码，`LOG_PRINT` 包装 printf。适配层的错误码来源见 4.2.3 分析的 `CheckParams`（[aclnn_gelu.cpp:L72-L87](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L72-L87)）：四种校验分别对应空指针与三类参数非法。

#### 4.4.4 代码实践（源码阅读型）

1. 实践目标：建立「错误码 → 校验点」的映射能力。
2. 操作步骤：在 [aclnn_gelu.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp) 中数一数 `ACLNN_ERR_` 出现的位置，为每个错误码写下它对应的失败条件。
3. 需要观察的现象：纯阅读。
4. 预期结果：至少得到 5 个映射项（COMM_INPUT 空指针、创建 executor 失败、参数非法 ×3 类、内部 nullptr ×3 处）。

#### 4.4.5 小练习与答案

**练习**：用户反馈「调 aclnnGelu 返回 1（非 0）但不知道哪错了」，请给出你的排查顺序。

答案：① 打开 Host 日志看 `OP_LOGE` 的具体报错文案（适配层每条错误路径都有日志）；② 对照 [aclnn_gelu.h:L36-L43](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.h#L36-L43) 的接口约束自查：是否空指针、dtype 是否在 {F16/F32/BF16} 且 self 与 out 一致、shape 是否一致、format 是否为私有格式；③ 若是 BF16，确认当前芯片架构是否支持（老架构不支持，见 4.2.3）。

## 5. 综合实践

**任务：编写并运行一个调用 `aclnnGelu` 的最小样例，与 CPU 参考值对账。** 这是本讲规格中指定的实践任务，把 4.1～4.4 的知识全部串起来。

前置条件：已完成 u1-l2 的环境准备，且已按 u1-l2 编译安装了包含 gelu 的算子包（例如 `bash build.sh --pkg --soc=${soc_version} --ops=gelu`，gelu 依赖的算子会自动解析一并编入）。

**第 1 步：创建调用目录与 cpp。** 在任意目录新建 `test_aclnn_gelu_my.cpp`（以下为**示例代码**，仿照 [test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp) 改写，仅保留关键差异）：

```cpp
// 示例代码：最小 aclnnGelu 调用样例
#include <cmath>
#include <cstdio>
#include <vector>
#include "acl/acl.h"
#include "aclnn_gelu.h"   // 头文件来自安装包的 aclnnop include 目录

// CHECK_RET / LOG_PRINT / GetShapeSize / CreateAclTensor 直接从
// examples/add_example/examples/test_aclnn_add_example.cpp 抄过来，此处省略

double GeluRef(double x) {                       // CPU 参考值：x * 0.5 * (1 + erf(x / sqrt(2)))
    return x * 0.5 * (1.0 + std::erf(x / std::sqrt(2.0)));
}

int main() {
    int32_t deviceId = 0;
    aclrtStream stream = nullptr;
    auto ret = Init(deviceId, &stream);          // ① 初始化
    CHECK_RET(ret == ACL_SUCCESS, return ret);

    std::vector<int64_t> shape = {8, 8};         // ② 构造一份随机 float 输入
    std::vector<float> hostData(64);
    srand(42);
    for (auto& v : hostData) { v = (rand() % 2000 - 1000) / 100.0f; }  // [-10, 10)

    aclTensor* self = nullptr;
    void* selfAddr = nullptr;
    ret = CreateAclTensor(hostData, shape, &selfAddr, aclDataType::ACL_FLOAT, &self);
    aclTensor* out = nullptr;
    void* outAddr = nullptr;
    std::vector<float> outData(64, 0);
    ret = CreateAclTensor(outData, shape, &outAddr, aclDataType::ACL_FLOAT, &out);

    uint64_t workspaceSize = 0;                  // ③ 第一段接口
    aclOpExecutor* executor = nullptr;
    ret = aclnnGeluGetWorkspaceSize(self, out, &workspaceSize, &executor);
    CHECK_RET(ret == ACLNN_SUCCESS, LOG_PRINT("GetWorkspaceSize failed: %d\n", ret); return ret);

    void* workspaceAddr = nullptr;               // gelu 通常为 0，此处稳妥起见仍处理
    if (workspaceSize > 0) {
        ret = aclrtMalloc(&workspaceAddr, workspaceSize, ACL_MEM_MALLOC_HUGE_FIRST);
        CHECK_RET(ret == ACL_SUCCESS, return ret);
    }

    ret = aclnnGelu(workspaceAddr, workspaceSize, executor, stream);  // ④ 第二段接口
    CHECK_RET(ret == ACLNN_SUCCESS, LOG_PRINT("aclnnGelu failed: %d\n", ret); return ret);

    ret = aclrtSynchronizeStream(stream);        // ⑤ 同步
    CHECK_RET(ret == ACL_SUCCESS, return ret);

    std::vector<float> result(64, 0);            // ⑥ 取回并对账
    ret = aclrtMemcpy(result.data(), 64 * sizeof(float), outAddr, 64 * sizeof(float),
                      ACL_MEMCPY_DEVICE_TO_HOST);
    CHECK_RET(ret == ACL_SUCCESS, return ret);
    for (int i = 0; i < 5; i++) {
        LOG_PRINT("in=%f npu=%f cpu=%f\n", hostData[i], result[i], GeluRef(hostData[i]));
    }

    (void)aclrtFree(selfAddr); (void)aclrtFree(outAddr);
    if (workspaceAddr != nullptr) { (void)aclrtFree(workspaceAddr); }
    (void)aclDestroyTensor(self); (void)aclDestroyTensor(out);
    (void)aclrtDestroyStream(stream);
    (void)aclrtResetDevice(deviceId);
    (void)aclFinalize();
    return 0;
}
```

**第 2 步：搭建编译脚本。** 按 [quick_op_invocation.md:L257-L394](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/invocation/quick_op_invocation.md#L257-L394) 创建 CMakeLists.txt 与 run.sh。要点：

- 调用自定义算子包：include 增加 `${TARGET_SUBDIR}/op_api/include`，链接 `libcust_opapi.so` 并设置 rpath（[L313-L320](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/invocation/quick_op_invocation.md#L313-L320)）；
- 调用 ops-nn 整包（内置算子）：include `${ASCEND_PATH}/include/aclnnop`，链接 `libopapi_nn.so`（[L361-L366](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/invocation/quick_op_invocation.md#L361-L366)）。
- 两种方式都需链接 `libascendcl.so`、`libnnopbase.so`。

**第 3 步：运行与验证。** `bash run.sh`，观察 5 组 `in / npu / cpu` 三列数值。预期 NPU 结果与 CPU 参考值在 float 精度内一致（相对误差量级 \(10^{-6}\) 左右，float 下以绝对误差 < 1e-5 为宜）。若第一段返回参数错误，回到 4.4 的排错顺序。

**备选路径**：如果只想快速验证而不自建工程，可以先把上面的对账逻辑写进 `activation/gelu/examples/` 下的样例（若该算子自带 examples），用 `bash build.sh --run_example gelu eager cust --vendor_name=custom` 执行。本环境无 NPU 硬件，上述运行结果**待本地验证**。

## 6. 本讲小结

- aclnn API 是两段式设计：第一段 `aclnnXxxGetWorkspaceSize` 在 Host 侧做校验、把算子调用登记进 executor 并算出 workspace 大小；第二段 `aclnnXxx` 把 executor 异步提交到 stream 执行。
- 适配层（op_api）里一次用户调用可能被展开为多个底层算子：gelu = `Contiguous` → `Gelu` → `ViewCopy`，登记与执行分离。
- l0 层函数（如 `l0op::Gelu`）通过 `ADD_TO_LAUNCHER_LIST_AICORE` 把 AI Core kernel 挂到执行列表，是 aclnn 层与 op_kernel 之间的桥。
- 调用样例的七步骨架：初始化 → 构造 aclTensor → 第一段 → 申请 workspace → 第二段 → 同步 → 拷回验证；换算子只改 API 名与输入构造。
- 所有返回码都要检查；适配层把空指针、dtype、shape、format 问题在 Host 侧拦截并映射为不同 `ACLNN_ERR_*` 错误码，头文件注释是参数约束的第一手文档。

## 7. 下一步学习建议

- 下一讲（u2-l2）将学习 GE 图模式调用：对比「aclnn eager 调用」与「构图调用」的差异，理解 op_graph 交付件的作用。
- 想继续深挖 aclnn 适配层的分层（l0/l2、公共工具 `aclnn_util`、两段式 API 的设计文档），可预习 u6-l1，本讲的 `aclnn_gelu.cpp`/`gelu.cpp` 分层正是那一讲的入口。
- 建议随手阅读 [docs/zh/invocation/quick_op_invocation.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/invocation/quick_op_invocation.md) 的 PyTorch API 一节，了解第三种调用方式的轮廓，为 u2-l3 做铺垫。
