# RoIAlign 家族：组合式算子的实现

> 版本说明：本讲义基于 HEAD `394ba763` 更新。上一版本（`2bd9cb7c`）以来，roi_align 家族相关的实际代码变化很小：
> - [objdetect/roi_align/op_host/op_api/aclnn_roi_align_v2.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/aclnn_roi_align_v2.cpp) 中 `CheckFormatValid` 的日志文案由 `"Format error. ..."` 改为 `"Invalid format. ..."`，并补齐文件末尾换行；
> - `experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp` 版权头重写（行号整体前移约 13 行）、一条 `OP_LOGE` 文案改为 `"Failed to set tiling data"`。
> 另请注意：大纲中提到的 `objdetect/roi_align_v2/` 目录在仓库中并不存在，带 Ascend C 实现的 RoiAlignV2 位于 `experimental/objdetect/roi_align_v2/`，本讲义按真实路径讲解。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 RoIAlign 的算子语义：它解决目标检测中「把任意大小的候选框特征池化成固定尺寸输出」的问题。
2. 理解什么是**组合式算子**：仓库里没有它的 Ascend C kernel，aclnn 接口通过编排一组底层公共算子（Contiguous / TransData / Cast / Concat / Fill / ViewCopy）和一个 L0 级内置算子完成任务。
3. 读懂 `op_host/op_api` 目录内嵌 aclnn 实现的组织方式，以及它与「标准三件套（op_host/op_kernel/op_api 分离）」工程的差异。
4. 了解 roi_align 家族的演进关系：V1（torchvision 风格分离 batchIndices）→ V2（boxes 内联 batch 索引、支持 aligned）→ rotated（旋转框）→ grad（反向梯度），以及 `experimental` 目录下正在自研 Ascend C 实现的 RoiAlignV2。

## 2. 前置知识

- **RoI（Region of Interest，感兴趣区域）**：目标检测模型（如 Faster R-CNN）在特征图上给出的候选框，每个框用坐标 \((x_1, y_1, x_2, y_2)\) 表示。不同候选框大小不一，但后续全连接层要求输入尺寸固定，因此需要一种「任意框 → 固定尺寸」的池化操作。
- **RoIAlign 与 RoIPool 的区别**：RoIPool 会把框坐标先量化成整数（两次取整），带来偏差；RoIAlign 用**双线性插值**在浮点坐标上采样，不做粗暴取整，因此对小框的定位精度更高。本算子文档明确声明「默认确定性实现」。
- **组合式算子**：本讲最重要的新概念。一个 aclnn 算子不一定要有自己的核函数——它可以像搭积木一样，把输入整理成底层内置算子要求的形态，再调用内置算子完成核心计算。aclnn 第一段接口里登记的每一个「积木」都是一次 L0 级算子调用（回顾 u2-l2 的「第一段登记、第二段执行」模型）。
- **L0 算子（l0op）**：CANN 算子库内部的底层算子封装层，形如 `l0op::Contiguous`、`l0op::ConcatD`、`l0op::ROIAlign`。`l0op::ROIAlign` 最终通过 `ADD_TO_LAUNCHER_LIST_AICORE` 把任务登记进 `aclOpExecutor`，由框架下发到 **CANN 内置的** RoiAlign 设备核（不在本仓库中）。
- 建议先回顾 u3-l1 的「算子完整解剖」：那里以 resize_bilinear_v2 展示了「自研 kernel」的标准链路；本讲正好是对照组——「不自研 kernel」的链路长什么样。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
| --- | --- |
| [objdetect/roi_align/README.md](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/README.md) | 一句话声明：本目录只含 aclnn 接口，无 Ascend C 实现 |
| [objdetect/roi_align/docs/aclnnRoiAlign.md](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/docs/aclnnRoiAlign.md) | 接口文档：产品支持情况、参数表、错误码、调用示例 |
| [objdetect/roi_align/examples/test_aclnn_roi_align.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/examples/test_aclnn_roi_align.cpp) | aclnn 样例（两段式调用骨架 + 期望输出） |
| [objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp) | V1 接口实现：参数检查 + 组合流水线 |
| [objdetect/roi_align/op_host/op_api/aclnn_roi_align_v2.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/aclnn_roi_align_v2.cpp) | V2 接口实现（本轮日志文案调整处） |
| [objdetect/roi_align/op_host/op_api/roi_align.h](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/roi_align.h) / [roi_align.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/roi_align.cpp) | L0 封装：`l0op::ROIAlign` / `l0op::ROIAlignV2`，通往设备核的桥梁 |
| [objdetect/roi_align/op_host/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/CMakeLists.txt) | 一行 `add_modules_sources`，说明该工程只编 host 侧 aclnn 代码 |
| [experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp) | 家族对照：社区自研的 RoiAlignV2 Ascend C 实现（本轮有修改） |
| objdetect/roi_align_grad、objdetect/roi_align_rotated | 家族成员：反向梯度与旋转框版本（本讲只看目录形态） |

## 4. 核心概念与源码讲解

### 4.1 RoIAlign 的算子语义与参数

#### 4.1.1 概念说明

RoIAlign 是一种池化层，用于「非均匀输入尺寸的特征图 → 固定尺寸输出特征图」。直观流程：

1. 把第 \(i\) 个 RoI 的坐标从原图坐标系映射到特征图坐标系：\( x^{feat} = x^{img} \times spatialScale \)；
2. 把该 RoI 划分成 \( pooledH \times pooledW \) 个小格子；
3. 每个格子在 \( k \times k \) 个采样点上做**双线性插值**取值，再对采样值做 avg 或 max 池化，得到该格子的输出。

采样点数 \(k\) 由 `samplingRatio` 决定；当 `samplingRatio = 0` 时自适应：

\[ k = \left\lceil \frac{roiW}{pooledW} \right\rceil \]

#### 4.1.2 核心流程

```
输入: self (N, C, H, W) 特征图
      rois (numRois, 4) 框坐标 x1,y1,x2,y2
      batchIndices (numRois,) 每个框属于哪张图
输出: out (numRois, C, outputHeight, outputWidth)
for each roi:
    框坐标 × spatialScale → 特征图坐标
    划分 outputHeight × outputWidth 格子
    每格采样 samplingRatio² (或自适应) 个点做双线性插值
    mode == "avg" ? 取均值 : 取最大值
```

#### 4.1.3 源码精读

接口定义与约束在文档中：

- [objdetect/roi_align/docs/aclnnRoiAlign.md:35-55](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/docs/aclnnRoiAlign.md#L35-L55) — 两段式函数原型：第一段 `aclnnRoiAlignGetWorkspaceSize` 产出 workspaceSize 与 executor，第二段 `aclnnRoiAlign` 执行。
- [objdetect/roi_align/docs/aclnnRoiAlign.md:96-111](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/docs/aclnnRoiAlign.md#L96-L111) — rois 的约束：坐标格式 \((x_1, y_1, x_2, y_2)\) 且 \(0 \le x_1 \le x_2 \le W/spatialScale\)；batchIndices 为 INT32 一维张量。注意 V1 的 rois 是 `(numRois, 4)`，batch 索引单独传入——这是 torchvision 风格。
- [objdetect/roi_align/docs/aclnnRoiAlign.md:144-162](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/docs/aclnnRoiAlign.md#L144-L162) — `samplingRatio`（采样频率，建议 0）与 `spatialScale`（空间尺度因子，建议 1.0）的语义说明。
- [objdetect/roi_align/docs/aclnnRoiAlign.md:164-172](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/docs/aclnnRoiAlign.md#L164-L172) — 输出 shape 契约：`(numRois, C, outputHeight, outputWidth)`，dtype 与 self/rois 一致。

样例构造了一份可以手算验证的数据：

- [objdetect/roi_align/examples/test_aclnn_roi_align.cpp:108-126](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/examples/test_aclnn_roi_align.cpp#L108-L126) — 输入 `self` 为 1×1×6×6、值 1..36 的特征图；`rois = {-2, -2, 22, 22}`（一个超出特征图边界的框，用于展示坐标裁剪行为）；期望输出 `outHostData = {4.5, 6.5, ...}` 是 3×3 平均池化的参考答案（写进 out 只是占位，真实结果以拷回数据为准）。

#### 4.1.4 代码实践

1. **实践目标**：不跑代码，先在纸上算出样例的期望输出，建立对语义的直觉。
2. **操作步骤**：特征图 6×6 值为 1..36（第 \(r\) 行第 \(c\) 列的值为 \(6r + c + 1\)）。ROI 为 \((-2, -2, 22, 22)\)，`spatialScale = 1.0`，输出 3×3，avg 模式，`samplingRatio = 0`。把 ROI 宽 24 等分成 3 份，每格宽 8，采样 8×8 个点，坐标越界处按边界裁剪（双线性插值在边界外取边界值）。计算左上格子的均值。
3. **需要观察的现象**：左上格子覆盖原图坐标 \([-2, 6) \times [-2, 6)\)，裁剪到 \([0, 6) \times [0, 6)\) 后即整幅 6×6 图的左上区域，均值应接近样例期望值 4.5（具体数值取决于采样点网格，精确对齐以样例输出为准，**待本地验证**）。
4. **预期结果**：手算值与样例 `outHostData` 首元素 4.5 同量级；若运行样例，`result[0]` 应精确等于 4.5。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `spatialScale` 从 1.0 改为 0.5，同样的 rois 在特征图上的框会变大还是变小？

**答案**：变小。映射公式为 \( x^{feat} = x^{img} \times spatialScale \)，0.5 × 24 = 12，框宽从 24 缩到 12（特征图坐标系）。spatialScale 表示「输入特征图相对输入图像的空间尺度」，原图 24 像素的框只对应特征图上 12 个像素。

**练习 2**：为什么输出 shape 的第 0 维是 `numRois` 而不是 `N`（批大小）？

**答案**：RoIAlign 对每个 RoI 独立产出一份 \( C \times outputH \times outputW \) 的特征，批内所有图的框被拉平在 rois 的第 0 维上，通过 batchIndices 区分每个框属于哪张图，所以输出第 0 维等于框数 numRois。

### 4.2 组合式算子的目录学：没有 op_kernel 意味着什么

#### 4.2.1 概念说明

回顾 u1-l2 的结论：「缺目录是实现方式声明」。roi_align 是其中最典型的一类：**没有 op_kernel、没有 op_host 的 def/infershape/tiling，只有嵌在 `op_host/op_api/` 里的 aclnn 实现文件**。这意味着：

- 输出 shape 不由本仓库的 InferShape 推导，而是由调用方直接传入合规的 `out`（第一段接口做校验，见 4.1.3 的 shape 契约）；
- 核心计算不在本仓库，而在 CANN 内置算子库的 RoiAlign 设备核上；
- 本仓库的角色是「适配层」：把 aclnn 入参整理成内置核要求的输入形态。

#### 4.2.2 核心流程

```
objdetect/roi_align/
├── README.md            ← 声明"仅含 aclnn 接口"
├── docs/                ← 接口文档（V1/V2 各一份）
├── examples/            ← V1/V2 两份样例
├── op_host/
│   ├── CMakeLists.txt   ← 一行 add_modules_sources，只编 host 侧
│   └── op_api/          ← aclnn_roi_align{,_v2}.cpp + L0 封装 roi_align{.h,.cpp}
├── framework/           ← ONNX/TF 插件（u6-l3 展开）
├── op_graph/            ← roi_align_proto.h（图模式原型）
└── tests/               ← ut/st
（没有 op_kernel/，没有 *_def.cpp，没有 tiling）
```

#### 4.2.3 源码精读

- [objdetect/roi_align/README.md:1-3](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/README.md#L1-L3) — 官方声明：「本目录仅包含 RoiAlign 算子对应的 aclnn 接口；如您想要贡献该算子的 Ascend C 实现，请参考贡献流程」。这句 README 直接定义了本讲的「组合式」主题。
- [objdetect/roi_align/op_host/CMakeLists.txt:13](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/CMakeLists.txt#L13) — 整个 op_host 只有一行 `add_modules_sources(OPTYPE roi_align ACLNNTYPE aclnn)`：以 aclnn 类型注册源码，没有 `KERNEL` 类目标，印证「无设备侧产物」。
- 对照组：`ls objdetect/roi_align_grad/` 可以看到标准八件套（op_host、op_kernel、op_api、op_graph、tests 等齐全）——反向梯度算子反而有完整自研实现（其 op_host 下有 def、infershape、arch35、config）。家族内不同成员的实现深度不同，这是读目录就能获得的信息。

#### 4.2.4 代码实践

1. **实践目标**：用目录形态预判算子实现方式。
2. **操作步骤**：在仓库根目录依次执行 `ls objdetect/roi_align/`、`ls objdetect/roi_align_grad/`、`ls objdetect/roi_align_rotated/`、`ls experimental/objdetect/roi_align_v2/`（源码阅读型实践，不运行）。
3. **需要观察的现象**：记录每个目录的子目录清单，标注「有无 op_kernel」「有无 *_def.cpp」「有无 tiling 文件」。
4. **预期结果**：roi_align 无 op_kernel；roi_align_grad、roi_align_rotated、experimental/roi_align_v2 均有 op_kernel 与 tiling。这说明「同一语义家族中，旧接口走组合复用，新实现逐步自研替代」。

#### 4.2.5 小练习与答案

**练习 1**：`add_modules_sources(OPTYPE roi_align ACLNNTYPE aclnn)` 中没有出现 kernel 相关参数，这对编译产物意味着什么？

**答案**：该工程只把 `op_host/op_api` 下的 C++ 源编进 host 侧的 aclnn 动态库（如 libopapi_cv.so 相关目标），不会调用 opc 编译任何设备侧二进制；运行时设备计算由 CANN 内置 RoiAlign 核承担。

**练习 2**：如果社区按 README 邀请贡献了 Ascend C 实现，目录上需要新增什么？

**答案**：至少新增 `op_kernel/`（核函数与 tiling data/key 头文件）、`op_host/*_def.cpp`（OpDef 注册并经 `opFile.value` 绑定 kernel）、tiling 实现与 `op_host/config/<芯片>/` 配置，并更新 README 声明——即从「组合式」升级为 u3-l1 讲解的标准链路。

### 4.3 aclnnRoiAlign 的组装流水线（V1）

#### 4.3.1 概念说明

V1 接口的本质是一条「预处理流水线 + 内置核调用 + 后处理流水线」。内置 RoiAlign 核的输入约定与 aclnn 对外接口并不一致：

- 核要求特征图为私有格式 **NC1HWC0**（5HD，见 u2-l2 的回顾）；
- 核的 rois 输入是 `(numRois, 5)`——第 0 列为 batch 索引，而后 4 列才是坐标；对外接口却是分离的 `rois (numRois,4)` + `batchIndices (numRois,)`，且 batchIndices 是 INT32、与 rois dtype 不同。

于是第一段接口要完成：连续化 → 转 5HD → batchIndices 变形/转型 → 与 rois 拼接成 5 列 → 调核 → 转回 NCHW → 拷贝到用户 out。

#### 4.3.2 核心流程

```
aclnnRoiAlignGetWorkspaceSize
├── CheckParams（五步校验：非空/dtype/format/shape/attr）
├── 空 tensor 分支：Fill(0) + ViewCopy 直接产出全零输出，返回
├── self/rois/batchIndices → Contiguous
├── self → TransDataSpecial(NCHW → NC1HWC0)
├── batchIndices → Reshape 成 (numRois,1) → Cast 成 rois 的 dtype
├── [batchIndices', rois] → ConcatD(axis=1) ⇒ roisConcat (numRois,5)
├── l0op::ROIAlign(selfTransData, roisConcat, ...) ⇒ roiAlignOut（5HD）
├── roiAlignOut → TransDataSpecial(回 NCHW)
└── ViewCopy 到用户 out（out 可能非连续）
```

每一步都只是把任务**登记**进 `aclOpExecutor`，真正的执行发生在第二段 `CommonOpExecutorRun`。

#### 4.3.3 源码精读

- [objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp:139-159](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp#L139-L159) — `CheckParams` 五步校验：非空指针 → dtype（self/rois/out 为 FLOAT/FLOAT16 且一致、batchIndices 为 INT32，见 [第 33-57 行](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp#L33-L57)）→ format（self/out 为 NCHW、rois/batchIndices 为 ND）→ shape（rois dim1 必须为 4、out 各维契约）→ attr（mode 只能 avg/max）。
- [objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp:161-169](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp#L161-L169) — `GetOutTensorWithValueZero`：把标量 0 经 `ConvertToTensor` 变成值张量、把输出 shape 变成 dim 张量，再用 `l0op::Fill` 生成全零张量。这是组合式算子处理空输入的惯用法——「空 tensor 不进核，直接 Fill 零」。
- [objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp:188-199](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp#L188-L199) — 空 tensor 分支：`Fill` 出全零后 `ViewCopy` 到 out，提前返回。
- [objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp:213-233](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp#L213-L233) — 流水线的「变形」段：`TransDataSpecial` 把 self 转成 NC1HWC0；batchIndices 先 `Reshape` 成 `(numRois, 1)`，再 `Cast` 成 rois 的 dtype，最后 `ConcatD` 在 axis=1 上拼成 `(numRois, 5)` 的新 rois。
- [objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp:236-246](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp#L236-L246) — 核心一行 `l0op::ROIAlign(...)` 得到 5HD 的中间输出，随后 `TransDataSpecial` 转回 NCHW、`ViewCopy` 拷到用户 out。
- [objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp:248-259](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp#L248-L259) — 固定收尾：取 workspaceSize、`ReleaseTo(executor)`；第二段只有一句 `CommonOpExecutorRun`，与 u2-l2 总结的「第一段厚重、第二段轻薄」完全一致。

#### 4.3.4 代码实践

1. **实践目标**：验证「流水线登记」模型——第一段不产生计算结果，只决定要跑哪些底层算子。
2. **操作步骤**：按 [docs/zh/context/compile_and_run_sample.md](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/docs/zh/context/compile_and_run_sample.md) 的流程（或 u1-l4 的 QUICKSTART 方式）编译安装 roi_align 算子包并运行 `test_aclnn_roi_align` 样例；随后在样例 main 中，于第一段调用之后、第二段调用之前插入一次 D2H 拷贝读取 out。
3. **需要观察的现象**：插入的读取拿到的仍是初始占位值（或未定义数据），只有 `aclnnRoiAlign` + `aclrtSynchronizeStream` 之后结果才有效。
4. **预期结果**：9 个输出值依次为 4.5, 6.5, 8.5, 16.5, 18.5, 20.5, 28.5, 30.5, 32.5（与样例 `outHostData` 一致）。若环境不可用，此观察点**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 batchIndices 必须先 `Reshape` 再 `Cast`，顺序反过来行不行？

**答案**：reshape `(numRois,) → (numRois,1)` 只改变视图不改数据，cast 把 INT32 转成 FLOAT/FLOAT16 以便与 rois 同 dtype 参与 `ConcatD`。理论上先 Cast 再 Reshape 也能得到同 dtype 的 `(numRois,1)` 张量；但仓库选定的顺序是「先定型再转型」，两种顺序在此处语义等价，工程上保持与社区贡献模板一致即可。

**练习 2**：流水线里 `ViewCopy(outTransData, out, ...)` 为什么不能省？

**答案**：`l0op::ROIAlign` 的输出是框架分配的内部张量，而用户的 `out` 可能是非连续 tensor（文档参数表标注 out 支持非连续√）；ViewCopy 负责把连续的中间结果按 out 的视图语义写入用户内存。

### 4.4 L0 封装：通往内置设备核的桥梁

#### 4.4.1 概念说明

`roi_align.cpp` 定义了 `l0op::ROIAlign` 与 `l0op::ROIAlignV2` 两个 L0 封装。它们做的事非常薄：**推导输出 shape → 向 executor 申请输出张量 → 用 `ADD_TO_LAUNCHER_LIST_AICORE` 把算子任务挂到 AICore 启动列表**。注意 `OP_TYPE_REGISTER(ROIAlign)` 与宏里的算子名 `ROIAlign` 指向的是 CANN 内置的设备算子（本仓库无其 kernel 源码），这正是「组合复用底层算子能力」的落点。

#### 4.4.2 核心流程

```
l0op::ROIAlign(self, rois, batchIndices, ...)
├── L0_DFX 打点
├── 输出 shape = self 的 shape，但 dim0 ← numRois，dim2/dim3 ← outputHeight/Width
├── executor->AllocTensor(...) 申请输出
└── ADD_TO_LAUNCHER_LIST_AICORE(ROIAlign, 输入, 输出, 属性)  ← 登记内置核任务
```

#### 4.4.3 源码精读

- [objdetect/roi_align/op_host/op_api/roi_align.h:16-26](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/roi_align.h#L16-L26) — `l0op` 命名空间下两个封装的声明：V1 带 batchIndices、mode；V2 只收 boxes（5 列）、多出 `roiEndMode` 参数。
- [objdetect/roi_align/op_host/op_api/roi_align.cpp:26](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/roi_align.cpp#L26) — `OP_TYPE_REGISTER(ROIAlign)`：注册 L0 算子类型。
- [objdetect/roi_align/op_host/op_api/roi_align.cpp:34-50](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/roi_align.cpp#L34-L50) — 输出 shape 的「拷贝再改」推导：以 self 的 storage/original shape 为底，仅覆盖 dim0（numRois）与 dim2/dim3（outputHeight/Width），再 `AllocTensor` 申请输出。这就是组合式算子的「隐形 Infershape」。
- [objdetect/roi_align/op_host/op_api/roi_align.cpp:52-54](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/roi_align.cpp#L52-L54) — 关键宏 `ADD_TO_LAUNCHER_LIST_AICORE(ROIAlign, OP_INPUT(...), OP_OUTPUT(...), OP_ATTR(...))`：把内置 RoiAlign 核的输入、输出与属性（spatialScale、outputHeight、outputWidth、samplingRatio、mode 等）登记进 AICore 启动列表。注意属性里有一个字面量 `0`——正是 V2 中 `roiEndMode` 的位置，V1 固定取 0（不做端点对齐偏移）。
- [objdetect/roi_align/op_host/op_api/roi_align.cpp:84-88](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/roi_align.cpp#L84-L88) — V2 版本同样登记名为 `ROIAlign` 的内置核，但属性里传入变量 `roiEndMode`，并对宏返回值做了 `OP_CHECK` 错误检查（比 V1 的写法更严谨）。

#### 4.4.4 代码实践

1. **实践目标**：对比 V1 与 V2 的 aclnn 组装差异，理解「同名内核、不同外壳」。
2. **操作步骤**（源码阅读型）：并排打开 [aclnn_roi_align.cpp:218-238](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp#L218-L238) 与 [aclnn_roi_align_v2.cpp:187-205](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/aclnn_roi_align_v2.cpp#L187-L205)，逐行标注两者在「batch 索引的来路」上的差别。
3. **需要观察的现象**：V1 用约 16 行做 Reshape/Cast/ConcatD 拼出 5 列 rois；V2 完全没有这段——它直接要求调用方传入 `(numRois, 5)` 的 boxes（校验见 [aclnn_roi_align_v2.cpp:86-89](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/aclnn_roi_align_v2.cpp#L86-L89)「boxes shape dim1 should be 5」）。
4. **预期结果**：得到结论「V2 把拼接成本转移给了调用方/框架侧，host 侧流水线更短」；另可注意到 V2 在 [第 199-204 行](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/aclnn_roi_align_v2.cpp#L199-L204) 由 `aligned` 布尔参数推导 `roiEndMode`（aligned 为 true 时取 2，与 torch 的 aligned 语义对齐），并固定使用 `"avg"` 模式。

#### 4.4.5 小练习与答案

**练习 1**：`l0op::ROIAlign` 里为什么不写任何计算循环？

**答案**：它是登记层而非计算层。计算发生在第二段 `CommonOpExecutorRun` 统一下发之后，由 CANN 内置的 RoiAlign 设备核执行；L0 封装只负责 shape 推导、输出申请与任务登记（`ADD_TO_LAUNCHER_LIST_AICORE`）。

**练习 2**：本轮（394ba763）对 `aclnn_roi_align_v2.cpp` 改了什么？会影响行为吗？

**答案**：仅两处——`CheckFormatValid` 的日志文案从 `"Format error. ..."` 改为 `"Invalid format. ..."`（见 [第 63 行](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/aclnn_roi_align_v2.cpp#L63)），以及补齐文件末尾换行。属于日志质量治理，不改变任何校验逻辑与返回码。

### 4.5 roi_align 家族演进与自研对照：experimental 里的 RoiAlignV2

#### 4.5.1 概念说明

roi_align 家族在同一仓库里有四种形态，正好构成一条演进线：

| 成员 | 位置 | 实现方式 | 输入形态差异 |
| --- | --- | --- | --- |
| RoiAlign（V1） | objdetect/roi_align | 组合式（调内置核） | rois (n,4) + batchIndices 分离 |
| RoiAlignV2 | objdetect/roi_align（aclnn）+ experimental/objdetect/roi_align_v2（自研 Ascend C） | 双轨：组合式与自研并存 | boxes (n,5) 内联 batch 索引，支持 aligned |
| RoiAlignRotated | objdetect/roi_align_rotated | 自研 Ascend C（有 def/tiling/kernel） | 旋转框（带角度） |
| RoiAlignGrad | objdetect/roi_align_grad | 自研 Ascend C | 反向梯度 |

`experimental/` 目录（回顾 u1-l2：社区贡献预留区）下的 roi_align_v2 是「组合式 → 自研」的现场：它有自己的 `roi_align_v2_def.cpp`、`roi_align_v2_infershape.cpp`、`roi_align_v2_tiling.cpp` 和 `op_kernel/`，tiling 思路是**按 ROI 在核间切分**——每个核分到一段 ROI，与 u3-l3 里 add_example 的「按元素切分」不同。

#### 4.5.2 核心流程

```
RoiAlignV2TilingFunc
├── GetPlatformInfo：取 AIV 核数 coreNum 与 UB 容量
├── 读 features (N,C,H,W) 与 rois (numRois,5) 的 shape、读 4 个属性
├── nowCoreNum = min(coreNum, numRois)          ← 核数不超过框数
├── baseRoisPerCore = numRois / nowCoreNum      ← 每核基准框数
├── tailRoiNum = numRois % nowCoreNum           ← 尾部余数
├── 填充 TilingData（框分布 + 特征图尺寸 + 属性）
└── SetBlockDim(nowCoreNum)
```

#### 4.5.3 源码精读

- [experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp:31-42](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp#L31-L42) — `GetPlatformInfo`：经 `PlatformAscendC` 取 AIV 核数与 UB 大小，是 u3-l3 讲过的标准第一步。
- [experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp:63-64](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp#L63-L64) — 本轮修改点之一：`memset_s` 清零 TilingData 失败时的日志文案改为 `"Failed to set tiling data"`；同时整个文件版权头在本轮被重写（由 HIT 开源项目头改为华为标准头），使行号相较上一版整体前移约 13 行——引用旧讲义行号时需注意。
- [experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp:75-108](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp#L75-L108) — 逐个读取 4 个属性（pooledHeight/pooledWidth/spatialScale/samplingRatio），每个都做了空指针防御；pooled 尺寸非正直接报错返回。
- [experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp:110-115](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp#L110-L115) — 按 ROI 切核的核心三行：`nowCoreNum = min(coreNum, numRois)`，`baseRoisPerCore` 与 `tailRoiNum` 组成「基准 + 余数」分配，前 `tailRoiNum` 个核每核多处理 1 个框（`bigTotalRois = baseRoisPerCore + 1`）。
- [experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp:122-138](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp#L122-L138) — 把框分布、特征图各维尺寸与属性全量填入 `RoiAlignV2TilingData`，最后 `SetBlockDim(nowCoreNum)`。
- [experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp:148](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp#L148) — `IMPL_OP_OPTILING(RoiAlignV2)` 注册 TilingFunc 与 TilingParse，接回 u3-l3 讲解的标准注册链路。

#### 4.5.4 代码实践

1. **实践目标**：用具体数字演算「按 ROI 切核」的负载分配。
2. **操作步骤**（纸面推演，无需环境）：假设 AIV 核数 `coreNum = 20`，分别取 `numRois = 47` 与 `numRois = 8`，代入 [第 110-115 行](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp#L110-L115) 的公式计算 `nowCoreNum / baseRoisPerCore / tailRoiNum / bigTotalRois`。
3. **需要观察的现象**：框数少于核数时 BlockDim 会被收缩到框数，避免空核。
4. **预期结果**：numRois=47 时：nowCoreNum=20，baseRoisPerCore=2，tailRoiNum=7，即 7 个核各处理 3 个框、13 个核各处理 2 个框（3×7+2×13=47）；numRois=8 时：nowCoreNum=8，baseRoisPerCore=1，tailRoiNum=0，每核恰好 1 个框。若在真机验证，可打印 TilingData 字段对照，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 RoiAlignV2 的 tiling 选择「按 ROI 切分」而不是像 add_example 那样按元素切分？

**答案**：RoIAlign 的计算以框为单位、框间完全独立，且单个框内部的采样点数量不大、核内可以一次装下；按 ROI 整体分配可以让每个核独立完成若干框的全部计算，无需核间同步。而 add_example 是纯逐元素映射，元素总数大且无框概念，按元素切分（blockFactor）更自然。

**练习 2**：`tailRoiNum` 个「大核」每核多处理 1 个框，这种分配叫什么？有没有更均匀的替代方案？

**答案**：即「基准 + 余数」的不均衡分配（某些调度器里称 bucket 分配）。替代方案是把余数框继续按更细粒度（如按采样行）二次切分做到完全均衡，但会增加 TilingData 复杂度与 kernel 内的分支；对 ROI 数量级不大的场景，±1 个框的偏差通常可接受，工程上取简单方案。

## 5. 综合实践

**任务：给「组合式 vs 自研」写一份对比分析报告。**

1. 运行 roi_align 的 V1 样例 `test_aclnn_roi_align.cpp`（按 [docs/zh/context/compile_and_run_sample.md](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/docs/zh/context/compile_and_run_sample.md) 或 u1-l4 的 QUICKSTART 流程编译安装后执行），记录 9 个输出值并与期望 `4.5, 6.5, ... 32.5` 比对；再运行 V2 样例 `test_aclnn_roi_align_v2.cpp`，观察两者入参构造差异（V2 需要自己拼 `(numRois,5)` 的 boxes）。若无硬件环境，此步替换为通读两份样例的 main 函数并写出差异清单。
2. 通读 [aclnn_roi_align.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/aclnn_roi_align.cpp)、[roi_align.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/op_host/op_api/roi_align.cpp) 与 [experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/experimental/objdetect/roi_align_v2/op_host/roi_align_v2_tiling.cpp)，画出两条链路的调用图：组合式（aclnn → Contiguous/TransData/Cast/ConcatD → l0op::ROIAlign → ADD_TO_LAUNCHER_LIST_AICORE → 内置核）与自研式（aclnn → OpDef 路由 → TilingFunc → 自研 kernel）。
3. 在报告结尾回答：如果一个新算子语义能用现有内置核 + 少量变形凑出来，选组合式有什么收益与代价？（提示：收益——无需写 kernel/def/tiling，交付件极少，见 4.2 的一行 CMakeLists；代价——host 侧多算子链带来额外 launch 与格式转换开销、受内置核能力约束、难以做 shape 特化优化。）

## 6. 本讲小结

- RoIAlign 把任意大小的候选框池化成固定尺寸特征：框坐标乘 `spatialScale` 映射到特征图坐标系，每个输出格子内做双线性插值采样后取 avg/max。
- roi_align 是「组合式算子」的教科书案例：仓库无 op_kernel/def/tiling，README 明言「仅含 aclnn 接口」，op_host 的 CMakeLists 只有一行 `add_modules_sources`。
- V1 的第一段接口是一条登记式流水线：Contiguous → TransData(NC1HWC0) → Reshape/Cast/ConcatD 拼 5 列 rois → `l0op::ROIAlign` → TransData 回 NCHW → ViewCopy；空 tensor 走 Fill(0) 捷径。
- `l0op::ROIAlign` 经 `ADD_TO_LAUNCHER_LIST_AICORE` 把任务交给 CANN 内置设备核——「组合复用底层算子能力」的最终落点；输出 shape 在 L0 封装里以「拷贝 self 再覆盖 dim0/dim2/dim3」的方式隐形推导。
- V2 把 batch 索引并入 boxes(n,5)、新增 aligned→roiEndMode 映射并固定 avg 模式；本轮版本对 V2 只改了日志文案与文件末尾换行，行为不变。
- 家族演进清晰：组合式 V1 → 双轨 V2（experimental 下自研 Ascend C，tiling 按 ROI 在核间分配）→ rotated/grad 全自研；`experimental` 目录是观察「组合式被自研替代」的现场。

## 7. 下一步学习建议

- 下一讲 u5-l2 将进入 NMS 家族（non_max_suppression_v3、combined_non_max_suppression、sorted_nms），那里会出现另一种实现载体——AiCPU 算子，可与本讲的「组合式」再成对照。
- 想吃透 L0 封装与 `ADD_TO_LAUNCHER_LIST_AICORE`，建议回读 u3-l1 的 resize_bilinear_v2 全景与 u2-l2 的 op_api 层走读。
- 对自研 tiling 感兴趣的读者，可以对照阅读 u3-l3（Tiling 机制）与本讲 4.5 的按 ROI 切分，体会切分维度如何由算子语义决定。
- 延伸阅读源码：`objdetect/roi_align/op_graph/roi_align_proto.h`（图模式原型，u6-l1 展开）与 `objdetect/roi_align/framework/` 下的 ONNX/TF 插件（u6-l3 展开）。
