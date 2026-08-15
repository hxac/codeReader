# 一个算子的完整解剖：resize_bilinear_v2 全景

## 1. 本讲目标

前两单元我们已经会用算子（aclnn 两段式调用、GE 图模式），也读懂了 op_api 层的「三段结构」。本讲切换到**工程全景视角**：以仓库中交付件最齐全的 `resize_bilinear_v2` 为解剖对象，把一个算子工程从 op_api 入口到 op_host（def / infershape / tiling）再到 op_kernel 的完整链路一次走通。

学完后你应当能够：

1. 画出从 `aclnnResize` 调用到 Ascend C Kernel 执行的**完整调用链图**，并标注每一环对应的源码文件与函数名。
2. 说出 `*_def.cpp`、`*_infershape.cpp`、`*_tiling*.cpp`、`*_apt.cpp` 四类文件各自的分工。
3. 理解 Host 侧与 Device 侧的衔接点：`ADD_TO_LAUNCHER_LIST_AICORE` 如何把「执行一个算子」登记进 `aclOpExecutor`，TilingKey 如何把 Host 侧的切分决策传递给 Kernel 侧的实现选择。
4. 建立一张「地图」，后续 u3-l2（Infershape）、u3-l3（Tiling）、u4-l1（Kernel）逐层深读时知道每层在全景中的位置。

## 2. 前置知识

本讲假设你已学完 u2-l1、u2-l2，这里补充三个新概念：

- **算子注册（OpDef）**：算子要被 CANN 框架识别，必须在 Host 侧「登记」一次自己的名字、输入/输出端口（名字、dtype、format）和属性。这份登记表就是 op_host 目录下的 `*_def.cpp`。可以理解为算子的「身份证 + 说明书」。
- **L0 算子（l0op）**：u2-l2 提过，aclnn 第一段接口里 `l0op::Contiguous`、`l0op::ViewCopy` 这类「框架内置小算子」只在 `aclOpExecutor` 里登记任务、第二段统一下发。本讲的 `resize_bilinear_v2` 工程自己也提供了一个 L0 层封装（`op_api/resize_bilinear_v2.cpp`），把「调用本算子」也封装成可登记的任务，供其他 aclnn 接口复用——这就是 aclnnResize 里 `l0op::ResizeBilinearV2With4d(...)` 的来源。
- **TilingKey**：Host 侧 Tiling 阶段除了计算切分参数，还会产出一个整数 key；Kernel 入口用 `TILING_KEY_IS(key)` 宏比对它，决定实例化哪个模板实现。它相当于 Host 与 Device 之间的一份「实现选择约定」。

另外回顾一个术语：**GM_ADDR** 是 Global Memory 地址的别名，Kernel 入口函数的所有输入输出（x、size、y、workspace、tiling）都通过 GM 地址传入。

## 3. 本讲源码地图

resize_bilinear_v2 工程位于 `image/resize_bilinear_v2/`，本讲涉及的关键文件：

| 文件 | 层 | 作用 |
| --- | --- | --- |
| [op_api/aclnn_resize.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp) | op_api（外层 aclnn） | 对用户的两段式接口 `aclnnResize`，做参数校验并编排执行链 |
| [op_api/resize_bilinear_v2.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/resize_bilinear_v2.cpp) | op_api（内层 L0 算子） | 在 `l0op` 命名空间封装 `ResizeBilinearV2` / `ResizeBilinearV2With4d`，登记 AiCore（或回退 AiCPU）执行任务 |
| [op_host/resize_bilinear_v2_def.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp) | op_host | OpDef 算子注册：端口、dtype/format 白名单、属性、AICore 配置 |
| [op_host/resize_bilinear_v2_infershape.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_infershape.cpp) | op_host | 输出 shape 推导（InferShape）与输出 dtype 推导（InferDataType） |
| [op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp) | op_host（arch35 子架构） | Tiling 实现：解析平台信息与 shape，选择策略、填 TilingData、设 TilingKey |
| [op_kernel/resize_bilinear_v2_apt.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp) | op_kernel | Ascend C 核函数入口 `resize_bilinear_v2`，按 TilingKey 分发到 11 个实现头文件 |

> 提示：`arch35` 指 DAV_3510 一类新架构（RegBase），tiling 与 kernel 都为它单独放了一份子目录实现；`op_host/config/ascend950/` 下的 `binary.json` / `simplified_key.ini` 是该架构的编译配置（u3-l4 详讲）。

## 4. 核心概念与源码讲解

先给全景调用链，后面五个小节逐段精读：

```text
用户代码 (examples/test_aclnn_resize.cpp)
  │
  ▼
aclnnResizeGetWorkspaceSize ────────────── op_api/aclnn_resize.cpp L251
  ├─ CheckParams 六步校验
  ├─ CREATE_EXECUTOR() 创建 aclOpExecutor
  ├─ l0op::Contiguous(x/out)               （登记非连续处理任务）
  ├─ CreateSizesRegBase / CreateSizesV35   （由 out shape 或 scales 构造 size 张量）
  ├─ l0op::ResizeBilinearV2With4d ───────── op_api/resize_bilinear_v2.cpp L114
  │    └─ ADD_TO_LAUNCHER_LIST_AICORE(ResizeBilinearV2, ...)
  │         │  （登记"执行算子 ResizeBilinearV2"任务，触发框架的
  │         │    infershape → tiling → kernel 下发流水）
  │         ▼
  │    [框架根据 OpDef 找到实现] ────────── op_host/resize_bilinear_v2_def.cpp
  │         ├─ InferShape4Resize2DWithConstSize ── op_host/*_infershape.cpp L111
  │         ├─ InferDtype4ResizeBilinearV2 ─────── op_host/*_infershape.cpp L137
  │         └─ Tiling4ResizeBilinearV2 ─────────── op_host/arch35/*_tiling_arch35.cpp L853
  │              ├─ TilingParse（编译期取核数/UB 大小）
  │              ├─ 匹配策略并 SetTilingKey
  │              └─ FillTilingData + SetBlockDim
  │         ▼
  │    resize_bilinear_v2 核函数 ────────── op_kernel/resize_bilinear_v2_apt.cpp L42
  │         ├─ GET_TILING_DATA(tilingData, tiling)
  │         └─ TILING_KEY_IS(key) 分发到
  │            AllCopy / PointCopy / Broadcast / CParallel / Simt* 模板类
  ├─ l0op::ViewCopy(resizeRet, out)        （登记结果回拷任务）
  └─ *workspaceSize = executor->GetWorkspaceSize()
  │
  ▼
aclnnResize ────────────────────────────── op_api/aclnn_resize.cpp L323
  └─ CommonOpExecutorRun：第二段统一下发所有已登记任务（含 kernel 启动）
```

一句话概括：**第一段把「要做什么」全部登记进 aclOpExecutor（含触发 infershape/tiling 的算子任务），第二段统一下发，kernel 依据 Host 侧写好的 TilingKey 选实现。**

### 4.1 op_api 外层：aclnnResize 的执行链编排

#### 4.1.1 概念说明

u2-l2 已拆过「三段结构」（static 检查函数、GetWorkspaceSize、薄执行函数），本节只补一块拼图：aclnn 层**不直接启动 kernel**，而是把算子调用封装成 L0 算子任务登记进 executor。这样 Contiguous、TransData、本算子、ViewCopy 在同一个记账本里排队，第二段按依赖序统一下发。

#### 4.1.2 核心流程

1. `OP_CHECK_COMM_INPUT` 检查 workspaceSize/executor 出参指针。
2. `GetExtendPathFlag()` 按当前芯片架构决定走新架构路径（RegBase/DAV_2201 等）还是 V35 老架构路径。
3. `CheckParams` 六步校验（非空→mode→dtype→format→元素→shape）。
4. `CREATE_EXECUTOR()` 创建执行器，随后 `l0op::Contiguous` 处理非连续输入输出。
5. 构造 `size` 张量：新架构用 `CreateSizesRegBase`（由 x shape × scales 算出目标 H/W），老架构用 `CreateSizesV35`（直接取 out 的 shape 后两维）。
6. 调 `l0op::ResizeBilinearV2With4d`（新架构，免 5HD 转换）或 `l0op::TransDataSpecial` → `l0op::ResizeBilinearV2` → `l0op::TransData`（老架构，NC1HWC0 往返）。
7. `l0op::ViewCopy` 把结果拷回用户输出张量，`GetWorkspaceSize()` 返回临时内存需求。

#### 4.1.3 源码精读

入口与打点、架构分流见 [image/resize_bilinear_v2/op_api/aclnn_resize.cpp:L251-L265](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L251-L265)：`GetExtendPathFlag()` 判断架构后创建 executor。

双路径编排见 [image/resize_bilinear_v2/op_api/aclnn_resize.cpp:L272-L315](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L272-L315)：新架构分支直接 `l0op::ResizeBilinearV2With4d(selfContiguous, sizes, false, nullptr, outContiguous, ...)`；老架构分支先 `TransDataSpecial` 转 `FORMAT_NC1HWC0` 再调 `l0op::ResizeBilinearV2`。注意这一段里**看不到任何 kernel 启动代码**——它们都发生在 L0 算子登记后的框架流水里。

第二段极薄，见 [image/resize_bilinear_v2/op_api/aclnn_resize.cpp:L323-L327](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.cpp#L323-L327)：`aclnnResize` 只打 `L2_DFX_PHASE_2` 点并转交 `CommonOpExecutorRun`，与 u2-l2 总结的「第二段轻薄」约定一致。

#### 4.1.4 代码实践

1. **实践目标**：确认 aclnn 层的「登记而非执行」特征。
2. **操作步骤**：打开 `aclnn_resize.cpp`，全文搜索 `aclrtLaunch`、`Launch`、`<<<` 等启动关键字，记录命中情况；再统计 `l0op::` 前缀的调用个数与顺序。
3. **需要观察的现象**：全文没有任何 kernel 启动调用；`l0op::` 调用按 Contiguous → (TransData) → Resize* → (TransData) → ViewCopy 的顺序排列。
4. **预期结果**：得出结论「aclnn 第一段只编排任务链，真正执行在第二段 CommonOpExecutorRun 内部完成」。

#### 4.1.5 小练习与答案

**练习 1**：为什么老架构路径需要 `TransDataSpecial` 转 NC1HWC0，而新架构不需要？
**答案**：V35 老架构的 ResizeBilinearV2 二进制按 5HD（NC1HWC0）格式编译，输入输出需往返转换；RegBase 类新架构支持 NCHW/NHWC 原生排布，`ResizeBilinearV2With4d` 可直接执行，省去两次 TransData 开销。

**练习 2**：`CreateSizesV35` 和 `CreateSizesRegBase` 的数据来源有何不同？
**答案**：`CreateSizesV35` 从 out 的 view shape 取后两维（用户给的输出 shape 就是目标尺寸）；`CreateSizesRegBase` 由 x 的 H/W 乘以 scales 计算目标尺寸，把 scales 参数真正用起来（见 aclnn_resize.cpp L209-L239）。

### 4.2 op_api 内层：l0op 封装与 ADD_TO_LAUNCHER_LIST_AICORE

#### 4.2.1 概念说明

这是全链路的**关键衔接点**：`ADD_TO_LAUNCHER_LIST_AICORE(算子名, 输入, 输出, 属性...)` 宏把「执行名为 ResizeBilinearV2 的算子」登记进 executor 的任务列表。框架拿到算子名后，依据 op_host 的 OpDef 注册表找到它的 infershape/tiling 实现，并在下发阶段生成「启动 op_kernel 中同名核函数」的任务。理解这一环，op_api 与 op_host/op_kernel 就串起来了。

#### 4.2.2 核心流程

1. `OP_TYPE_REGISTER(ResizeBilinearV2)` 声明本文件实现该算子类型的 L0 封装。
2. `IsAiCoreSupport(x)` 按 dtype（及架构）判断能否走 AiCore；不支持则回退 AiCPU 实现。
3. AiCore 路径调 `ADD_TO_LAUNCHER_LIST_AICORE`，携带输入 `(x, size)`、输出 `y`、属性 `(align_corners, half_pixel_centers, [dtype, scales])`。
4. 对外暴露 `ResizeBilinearV2` 与 `ResizeBilinearV2With4d` 两个入口，后者多带 scales、面向 RegBase 4D 原生格式。

#### 4.2.3 源码精读

AiCore 登记见 [image/resize_bilinear_v2/op_api/resize_bilinear_v2.cpp:L72-L83](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/resize_bilinear_v2.cpp#L72-L83)：`ResizeBilinearV2AICORE` 用 `ADD_TO_LAUNCHER_LIST_AICORE(ResizeBilinearV2, OP_INPUT(x, size), OP_OUTPUT(y), OP_ATTR(align_corners, half_pixel_centers))` 登记任务后直接返回 `y`——注意这里宏的第一个参数就是 OpDef 里注册的算子名。

AiCore/AiCPU 分流见 [image/resize_bilinear_v2/op_api/resize_bilinear_v2.cpp:L84-L92](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/resize_bilinear_v2.cpp#L84-L92)：`ResizeBilinearV2` 入口按 `IsAiCoreSupport` 选择载体，这是「同一个算子双载体」的最小示例（u8-l1 会展开 AiCPU 开发）。

带 scales 的 4D 入口见 [image/resize_bilinear_v2/op_api/resize_bilinear_v2.cpp:L114-L122](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/resize_bilinear_v2.cpp#L114-L122)：`ResizeBilinearV2With4d` 正是 4.1 节 aclnnResize 新架构分支调用的函数。

#### 4.2.4 代码实践

1. **实践目标**：验证「L0 封装的属性列表 = OpDef 注册的属性」。
2. **操作步骤**：抄下 L77-L78 与 L106-L107 两处 `OP_ATTR(...)` 里的属性名，再到 4.3 节的 def 文件中找到 `this->Attr(...)` 列表，逐个对照。
3. **需要观察的现象**：属性名集合一致（align_corners、half_pixel_centers、dtype、scales），且顺序也与 def 中 Attr 声明顺序对应。
4. **预期结果**：理解登记宏的属性是按 OpDef 中属性索引位置传参的，两边必须严格对齐，否则属性会错位。

#### 4.2.5 小练习与答案

**练习**：`ResizeBilinearV2AICORE` 为什么能直接 `return y`，它什么时候真正执行？
**答案**：它只是登记任务并返回输出张量描述符；真正执行发生在 aclnn 第二段 `CommonOpExecutorRun` 统一下发时，框架再走 infershape/tiling/启动核函数的流水。

### 4.3 op_host 之一：def 文件——算子的身份证

#### 4.3.1 概念说明

`*_def.cpp` 回答「这个算子叫什么、长什么样」：输入/输出端口名与类型白名单、属性默认值、在哪些芯片上以何种方式运行。框架用它生成算子信息（配合 u1-l3 讲过的 `gen_ops_info.cmake`），也用它把 `ADD_TO_LAUNCHER_LIST_AICORE` 里的算子名路由到正确实现。

#### 4.3.2 核心流程

1. 定义 dtype/format 白名单表（输入 x、输入 size、输出 y 各一套，10 组组合）。
2. 构造 `OpDef` 子类：`Input("x")`、`Input("size")`（声明 `ValueDepend`）、`Output("y")`、四个 `Attr`。
3. `OpAICoreConfig` 声明动态 shape 能力与 `opFile.value = "resize_bilinear_v2_apt"`——这个值指向 op_kernel 的实现文件（去掉 `.cpp` 后缀）。
4. `AddConfig("ascend950"/"mc62", ...)` 声明支持的芯片；`OP_ADD(ResizeBilinearV2)` 完成注册。

#### 4.3.3 源码精读

端口与属性声明见 [image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp:L41-L61](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp#L41-L61)：`Input("size")` 带 `ValueDepend(OPTIONAL)`——这解释了 infershape 为什么能在编译期读到 size 的值（见 4.4 节）；属性 `align_corners`、`half_pixel_centers`、`dtype`、`scales` 与 4.2 节登记宏的属性一一对应。

AICore 配置与注册见 [image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp:L63-L73](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_def.cpp#L63-L73)：`ExtendCfgInfo("opFile.value", "resize_bilinear_v2_apt")` 把本算子绑定到 op_kernel/`resize_bilinear_v2_apt.cpp` 编出的二进制，`OP_ADD(ResizeBilinearV2)` 是注册收尾。

#### 4.3.4 代码实践

1. **实践目标**：建立「README 参数表 ↔ def 注册」的对照能力。
2. **操作步骤**：打开 `image/resize_bilinear_v2/README.md` 的参数说明表，把每个参数（x、size、y、align_corners、half_pixel_centers、dtype、scales）映射到 def 文件 L41-L61 的对应声明行，做一张两列对照表。
3. **需要观察的现象**：README 中标注「必选/可选」与 `ParamType(REQUIRED)` / `AttrType(OPTIONAL)` 对应；README 的 dtype 支持列表与 L18-L23 的白名单对应。
4. **预期结果**：得出结论「README 是 def 注册的用户视角文档，两者必须同步维护」。

#### 4.3.5 小练习与答案

**练习 1**：`opFile.value` 配成 `resize_bilinear_v2_apt`，为什么 op_kernel 里核函数却叫 `resize_bilinear_v2`？
**答案**：`opFile.value` 指定的是实现文件名（op_kernel/resize_bilinear_v2_apt.cpp），框架按「文件名找二进制、按文件内的 `__global__ __aicore__` 入口函数找核函数」，二者不需要同名。

**练习 2**：删掉 `Input("size")` 上的 `ValueDepend(OPTIONAL)` 会影响什么？
**答案**：`ValueDepend` 声明 size 的值需要在 shape 推导/编译阶段可见；去掉后 infershape 里 `GetInputTensor(IN_SIZE)` 拿到的可能不再是常量张量，输出 H/W 将退化为 -1（UNKNOWN_DIM）。

### 4.4 op_host 之二：infershape——输出 shape 与 dtype 怎么来

#### 4.4.1 概念说明

Infershape 回答「输出张量长什么样」。aclnn 单算子路径下用户自报输出 shape（aclnnResize 的 `out` 参数），但框架仍要用 InferShape 校验/推导；GE 图模式（u2-l4）下它更是唯一能推出输出 shape 的地方。InferDataType 则决定输出 dtype——本算子特殊在输出 dtype 可由 `dtype` 属性指定，不完全跟随输入。

#### 4.4.2 核心流程

InferShape 主流程（`InferShape4Resize2DWithConstSize`）：

1. 取输入 x 的 shape、输出 y 的 shape 槽位、size 的常量张量。
2. `GetSizeFor2D` 读 size 值：非 const 则 H/W 置 `UNKNOWN_DIM`(-1)；是 int32 则读出目标 H/W。
3. 校验 format 只能是 NCHW/NHWC；处理 -2（UnknownRank）与 4D 校验，其余维度原样继承 x。
4. 按 format 找到 H/W 在 shape 中的下标（NHWC 为 1/2，NCHW 为 2/3），用目标 H/W 覆盖。

InferDataType 流程：默认输出 float → 读 `dtype` 属性 → 校验只允许 float/float16/bf16/uint8 → 校验输入输出 dtype 组合合法（如 float 输入不允许降精度到 fp16/bf16）→ 设置输出 dtype。

#### 4.4.3 源码精读

读取 size 常量见 [image/resize_bilinear_v2/op_host/resize_bilinear_v2_infershape.cpp:L52-L72](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_infershape.cpp#L52-L72)：`Ops::Cv::IsConstTensor` 判断是否常量，非 const 走 -1 未知维。

覆盖 H/W 维见 [image/resize_bilinear_v2/op_host/resize_bilinear_v2_infershape.cpp:L97-L103](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_infershape.cpp#L97-L103)：先 `*y_shape = *x_shape` 整体继承，再按 format 的维下标 `SetDim` 覆盖 H/W——这是所有 resize 类算子推导的通用套路。

注册见 [image/resize_bilinear_v2/op_host/resize_bilinear_v2_infershape.cpp:L183-L186](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/resize_bilinear_v2_infershape.cpp#L183-L186)：`IMPL_OP_INFERSHAPE(ResizeBilinearV2)` 把 InferShape 与 InferDataType 回调挂到算子名上，并声明 `InputsDataDependency({IN_SIZE})`——与 def 的 ValueDepend 呼应。

#### 4.4.4 代码实践

1. **实践目标**：手动执行一次 shape 推导，验证对规则的理解。
2. **操作步骤**：假设输入 `x` shape 为 `(2, 3, 8, 16)`、format NCHW、`size = (16, 32)`，按 L97-L103 的规则在纸上推导演算；再换成 NHWC 格式 `(2, 8, 16, 3)` 推一遍。
3. **需要观察的现象**：NCHW 结果应为 `(2, 3, 16, 32)`；NHWC 结果应为 `(2, 16, 32, 3)`——只有 H/W 下标位置不同，逻辑完全一致。
4. **预期结果**：与推导一致；如本地有环境，可运行 `examples/test_aclnn_resize.cpp`（把 size 改成上述值）验证输出 shape。**运行结果待本地验证。**

#### 4.4.5 小练习与答案

**练习 1**：如果 size 不是常量张量，输出 shape 是什么？
**答案**：H/W 为 `ge::UNKNOWN_DIM`(-1)，其余维继承 x（见 L54-L59 的告警分支），留给运行时确定。

**练习 2**：为什么 `InferDtype4ResizeBilinearV2` 里禁止「xDtype=float 且 outDtype=fp16/bf16」？
**答案**：float32 是更高精度类型，降精度到 fp16/bf16 会造成不可控精度损失，框架直接报错拦下（L168-L170）；而 fp16 输入允许输出同为 fp16 或升到 float32。

### 4.5 op_host 之三：tiling——切分策略与 TilingKey

#### 4.5.1 概念说明

Tiling 回答「这块数据怎么分给多个核」。它分两阶段：**TilingParse**（编译期，从平台取核数/UB 大小存进 CompileInfo）和 **Tiling**（每次执行，按实际 shape 选策略、填 TilingData、设 TilingKey 与 BlockDim）。本算子有 10 余种策略（all_copy、point_copy、broadcast、c_parallel、simt 系列），全部靠 TilingKey 区分——这正是 u4-l2 要深挖的「多策略 kernel」的 Host 侧源头。

#### 4.5.2 核心流程

1. `TilingPrepare4ResizeBilinearV2`：用 `PlatformAscendC` 取 AIV 核数与 UB 大小，写入 `ResizeBilinearV2CompileInfo`。
2. `Tiling4ResizeBilinearV2`：构造 `ResizeBilinearV2AscendCTilingImpl`，`Init` 读 CompileInfo 与输入信息，`DoTiling` 执行切分。
3. `DoTiling` 内部：`MatchTilingStrategyAndSetTilingKey()` 按 shape 特征匹配策略并设置 key（如输入输出完全相同→ALL_COPY、src H=W=1→POINT_COPY、通道特大→C_PARALLEL、一般场景→SIMT 系列）→ `FillTilingData()` 填充切分参数 → `SetBlockDim(realCoreNum_)` 设核数 → `SetTilingKey(tilingKey_)`。
4. TilingData 与 TilingKey 随任务下发，Kernel 侧据此选择实现。

#### 4.5.3 源码精读

Tiling 主入口见 [image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp:L853-L868](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp#L853-L868)：`Tiling4ResizeBilinearV2` 从 `context->GetCompileInfo()` 取编译期信息，Init + DoTiling 两步走。

DoTiling 收尾见 [image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp:L840-L850](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp#L840-L850)：`FillTilingData` / `PrintTilingData` 之后 `SetBlockDim` 与 `SetTilingKey` 是 Host→Device 传递调度决策的两个关键写操作。

TilingParse 编译期准备见 [image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp:L870-L887](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp#L870-L887)：`GetCoreNumAiv()` 与 `GetCoreMemSize(UB)` 取平台参数存入 CompileInfo。

注册见 [image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp:L890-L892](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp#L890-L892)：`IMPL_OP_OPTILING(ResizeBilinearV2)` 挂载 Tiling 与 TilingParse 回调。

策略 key 常量见 [image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp:L36-L46](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_host/arch35/resize_bilinear_v2_tiling_arch35.cpp#L36-L46)：`TILING_KEY_ALL_COPY=40000`、`TILING_KEY_SIMT_NHWC=30000` 等，与 4.6 节 kernel 侧同名宏一一对应——这就是跨侧「约定」的实体。

#### 4.5.4 代码实践

1. **实践目标**：找到策略匹配逻辑，理解「什么 shape 走什么策略」。
2. **操作步骤**：在 tiling 文件中定位 `MatchTilingStrategyAndSetTilingKey`、`IsMatchAllCopy`、`IsMatchPointCopy`、`IsMatchCParallel` 等函数（文件前部声明的私有方法），阅读它们的判断条件。
3. **需要观察的现象**：每个 IsMatch 函数对应一组 shape/通道特征不等式；命中后设置对应的 tilingKey_ 成员。
4. **预期结果**：能口头回答「输入输出完全一致时选什么策略（ALL_COPY）、C 维很大时选什么（C_PARALLEL）」。完整策略深读留待 u3-l3/u4-l2。

#### 4.5.5 小练习与答案

**练习 1**：为什么核数和 UB 大小放在 TilingParse（编译期）而不是每次 Tiling 时获取？
**答案**：平台参数在编译期就固定了，提前解析进 CompileInfo 可避免每次执行重复查询平台，tiling 时直接读 `GetCompileInfo()` 即可。

**练习 2**：`SetBlockDim(realCoreNum_)` 和 `SetTilingKey(tilingKey_)` 各自传递什么信息？
**答案**：BlockDim 告诉框架启动多少个核（并行度），TilingKey 告诉 Kernel 侧选中了哪套实现策略；两者加 TilingData 一起构成 Host→Device 的全部调度信息。

### 4.6 op_kernel：Ascend C 核函数入口与按 key 分发

#### 4.6.1 概念说明

op_kernel 是真正跑在 AI Core 上的代码。`resize_bilinear_v2_apt.cpp` 是唯一入口文件，它的职责只有两件事：从 GM 取出 tiling 数据，然后按 TilingKey 把工作转交给 `arch35/` 下 11 个实现头文件中的模板类（`ResizeBilinearV2AllCopy`、`ResizeBilinearV2PointCopy`、`ResizeBilinearV2SimtNHWC` 等）。这种「薄入口 + 策略头文件」结构让多策略实现互不干扰。

#### 4.6.2 核心流程

1. 框架启动核函数 `resize_bilinear_v2(x, size, y, workspace, tiling)`——五个参数全是 GM 地址。
2. `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)` 声明本算子用向量核。
3. `GET_TILING_DATA(tilingData, tiling)` 把 Host 填好的 TilingData 反序列化成结构体。
4. 依次 `TILING_KEY_IS(...)` 比对 key：命中则实例化对应模板类，`op.Init(x, size, y, &pipe, &tilingData)` + `op.Process()` 完成计算并 return。
5. SIMT 系列还会二次细分：按 `halfPixelCenters`、源/目标 H/W 关系选择模板参数（如 `mode=1` 表示同尺寸直拷），按数据量选 `uint32_t/uint64_t` 索引宽度。

#### 4.6.3 源码精读

入口签名与 tiling 获取见 [image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp:L42-L49](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp#L42-L49)：`extern "C" __global__ __aicore__ void resize_bilinear_v2(GM_ADDR x, GM_ADDR size, GM_ADDR y, GM_ADDR workspace, GM_ADDR tiling)` 是 Device 侧唯一对外符号。

按 key 分发见 [image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp:L50-L76](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp#L50-L76)：`TILING_KEY_IS(TILING_KEY_ALL_COPY)` 命中后实例化 `ResizeBilinearV2::ResizeBilinearV2AllCopy<DTYPE_X>`，Init + Process 两段式执行；宏常量定义在同文件 [L27-L38](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp#L27-L38)，与 4.5 节 Host 侧常量成对。

key 内二次细分见 [image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp:L89-L115](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_kernel/resize_bilinear_v2_apt.cpp#L89-L115)：`TILING_KEY_SIMT_NCHW` 分支里按 `halfPixelCenters`、`lenSrcH/lenDesH` 关系选模板参数（第 3/4 个模板参数编码 half-pixel 与场景模式，第 5 个是索引类型）。

#### 4.6.4 代码实践

1. **实践目标**：数清分发树，把「key → 实现类」整理成表。
2. **操作步骤**：通读 `resize_bilinear_v2_apt.cpp`，为 12 个 TILING_KEY_* 宏各记一行：key 值、命中的实现类、所在的 arch35 头文件（对照文件头 L15-L25 的 include 列表）。
3. **需要观察的现象**：C_PARALLEL 分支（L77-L87）比较特殊——命中后还要按 `cFactor < lenC` 再分成 `ResizeBilinearV2Nc` 与 `ResizeBilinearV2CParallel` 两个实现。
4. **预期结果**：得到一张 12 行的「TilingKey → 实现类」映射表，这就是 u4-l2 深读各实现变体的索引。

#### 4.6.5 小练习与答案

**练习 1**：为什么核函数要用 `extern "C"` 修饰？
**答案**：禁止 C++ 名称修饰（name mangling），保证框架按 `resize_bilinear_v2` 这个原始符号名查找入口；否则链接时按 C++ 修饰名查找会失败。

**练习 2**：SIMT 系列为什么需要 `uint32_t` 与 `uint64_t` 两套索引实例？
**答案**：数据量大时元素偏移可能超过 32 位表示范围（key 后缀 `_IDX64`），必须换 64 位索引；数据量小时用 32 位索引更省寄存器与带宽。Host 侧 tiling 按数据量决定选哪个 key。

## 5. 综合实践：绘制 resize_bilinear_v2 完整调用链图

把本讲五个环节串成一张图，作为后续课程的「随身地图」。

1. **实践目标**：产出一张从 `aclnnResize` 到核函数 `resize_bilinear_v2` 的调用链图，每个节点标注源码文件与函数名。
2. **操作步骤**：
   - 参照第 4 节开头的全景图骨架，用你熟悉的工具（纸笔、draw.io、Mermaid）重画一遍。
   - 每个节点要求三要素：**层**（op_api 外层 / op_api L0 层 / op_host / 框架 / op_kernel）、**文件路径**、**函数名与行号**（aclnnResizeGetWorkspaceSize L251、ResizeBilinearV2With4d L114、Tiling4ResizeBilinearV2 L853、resize_bilinear_v2 L42 等）。
   - 用两种箭头区分「直接函数调用」（实线）与「登记后由框架调度」（虚线，如 ADD_TO_LAUNCHER_LIST_AICORE 之后的 infershape/tiling/kernel 三步）。
   - 在图旁标注三组「跨侧约定」：登记宏属性 ↔ def 的 Attr 声明；Host 的 TILING_KEY 常量 ↔ Kernel 的同名宏；TilingData 结构 ↔ Kernel 的 GET_TILING_DATA。
3. **需要观察的现象**：画完后自查——图上是否覆盖了 def/infershape/tiling/kernel 四类文件？是否画出了「第一段登记、第二段执行」的分界线？
4. **预期结果**：一张可长期维护的全景图；后续 u3-l2/u3-l3/u4-l1/u4-l2 学习时，把新细节补到对应节点上。
5. 进阶（可选）：若本地有 Atlas 环境，开启算子日志（参考 `docs/zh/debug/op_debug_prof.md`）运行 `examples/test_aclnn_resize.cpp`，在日志中确认 `Tiling4ResizeBilinearV2` 的 `PrintTilingData` 输出与你图中标注的 tiling 环节对应。**运行结果待本地验证。**

## 6. 本讲小结

- 一个标准算子工程的主链路是：**op_api 外层 aclnn（校验+编排）→ op_api 内层 l0op 封装（ADD_TO_LAUNCHER_LIST_AICORE 登记）→ 框架按 OpDef 路由 → op_host 的 infershape/tiling → op_kernel 核函数按 TilingKey 分发**。
- `*_def.cpp` 是算子身份证：端口、dtype/format 白名单、属性、`opFile.value` 绑定 kernel 实现文件、`AddConfig` 声明支持芯片。
- `*_infershape.cpp` 负责输出 shape（继承 x 再覆盖 H/W）与输出 dtype（受 `dtype` 属性与精度组合规则约束），通过 `IMPL_OP_INFERSHAPE` 注册。
- tiling 分 TilingParse（编译期取核数/UB）与 Tiling（运行期选策略、填 TilingData、SetTilingKey/SetBlockDim），通过 `IMPL_OP_OPTILING` 注册。
- kernel 入口是薄分发层：`GET_TILING_DATA` 取参后按 `TILING_KEY_IS` 分发到 11 个策略头文件的模板类，Init + Process 执行。
- 三组「跨侧约定」保证 Host 与 Device 对齐：登记宏属性 ↔ def 属性、两侧同名 TILING_KEY 常量、TilingData 结构 ↔ GET_TILING_DATA。

## 7. 下一步学习建议

本讲建立了全景地图，后续课程按层深读：

- **u3-l2（Infershape 机制）**：对比 add_example 与 resize_bilinear_v2 的推导实现，学习 `common/inc/op_api/infershape_utils.h` 公共工具。
- **u3-l3（Tiling 机制）**：以 add_example_tiling.cpp 为主线拆 TilingFunc 标准步骤，本讲 4.5 节是它的预告片。
- **u4-l1（Ascend C Kernel 基础）**：进入策略头文件内部，看 Init/Process、TPipe 与 LocalTensor 的用法。
- **u4-l2（多策略 Kernel）**：深挖 all_copy/point_copy/broadcast/simt 各变体的适用 shape 与选择逻辑。

建议同时把 `image/resize_bilinear_v2/README.md` 与 `docs/aclnnResize.md` 通读一遍，对照本讲验证「用户文档描述的行为 ↔ 源码实现」的对应关系。
