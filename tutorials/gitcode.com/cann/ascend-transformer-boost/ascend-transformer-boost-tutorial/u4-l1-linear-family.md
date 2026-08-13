# Linear 算子族

## 1. 本讲目标

Linear（矩阵乘 + 偏置/反量化）是 Transformer 里被调用次数最多的算子，几乎所有投影层（QKV、FFN、LM Head）都依赖它。ATB 没有为「带 bias 的 matmul」「量化 matmul」「爱因斯坦乘 matmul」「多卡通信 matmul」「稀疏 matmul」各写一个独立算子，而是用一个 **Linear 算子族** 统一覆盖。学完本讲，你应当能够：

- 说清 `LinearParam` 如何用「字段组合」表达出 N 种不同的 matmul 行为；
- 看懂 `LinearOperation::CreateRunner` 这棵「按芯片 + 场景分派后端 Runner」的核心决策树；
- 理解非 950 芯片上 `LinearOpsRunner` 如何用一张 `KernelGraph` 把 `Transdata` + `MatMul` + `ElewiseAdd` 拼出来；
- 区分 `Linear`、`LinearParallel`（通信并行）、`LinearSparse`（稀疏）三个兄弟算子在输入输出与 Runner 选择上的差异；
- 能改着跑通 `example/op_demo/linear/linear_demo.cpp`，并把它的 Param 换成量化/Einsum 场景。

## 2. 前置知识

本讲承接你已经建立的两组认知（见 u3-l1、u3-l2）：

- **OperationBase 是模板方法骨架**：它把 `Operation::InferShape/Setup/Execute` 写成冻结流程，子类只重写 `InferShapeImpl`（推导输出形状）、`CreateRunner`（选后端执行单元）等钩子。`LinearOperation` 就是这样一个子类。
- **Runner 是执行后端**：`OperationBase` 自己从不下发 kernel，它通过 `CreateRunner` 拿到一个 `Runner`，由 Runner 完成实际计算。主流的 `OpsRunner` 内部维护一张 `KernelGraph`，把算子拆成若干 `KernelGraphNode`（如 `MatMulOperation`、`ElewiseOperation`），逐节点下发。完整链路是 `Operation → Runner → KernelGraph → Kernel`。

此外需要一点矩阵乘基础：记 \(C = A \times B\)，其中 \(A\) 形状 \([M, K]\)、\(B\) 形状 \([K, N]\)、\(C\) 形状 \([M, N]\)。`transposeA/transposeB` 决定乘之前是否对 \(A\)、\(B\) 做行列转置。本讲反复出现的 M/K/N 就是这三个维度。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| `include/atb/infer_op_params.h` | 定义 `LinearParam`、`LinearParallelParam`、`LinearSparseParam` 三个参数结构体 |
| `src/ops/ops_infer/linear/linear_operation.cpp` | `LinearOperation` 主实现：参数校验、`InferShapeImpl`、`CreateRunner` 分派 |
| `src/ops/ops_infer/linear/linear_ops_runner.cpp` | 非 950 芯片的后端 Runner，用 `KernelGraph` 拼出各种 matmul 变体 |
| `src/ops/ops_infer/linear/linear_aclnn_runner.h` 等 | 950 芯片走 aclnn 后端的三个变体 Runner（普通/反量化/Einsum） |
| `src/ops/ops_infer/linear_parallel/linear_parallel_operation.cpp` | 通信并行变体 `LinearParallelOperation` |
| `src/ops/ops_infer/linear_sparse/linear_sparse_operation.cpp` | 稀疏变体 `LinearSparseOperation` |
| `example/op_demo/linear/linear_demo.cpp` | C++ 调用 Linear 的最小可运行示例 |

---

## 4. 核心概念与源码讲解

### 4.1 LinearParam：用一个结构表达 N 种 matmul

#### 4.1.1 概念说明

很多加速库会为「Linear」「QuantLinear」「EinsumLinear」各开一个算子类。ATB 反其道而行：**只定义一个 `LinearParam`，靠字段组合 + 一个 `outDataType` 总开关，覆盖所有 matmul 形态**。这样做的好处是上层框架（如 MindIE）只需要认识一个 `Linear` 算子名，换场景只需改 Param 字段。

关键设计有三点：

1. **`outDataType` 是浮点/量化场景的总开关**：默认 `ACL_DT_UNDEFINED` 表示浮点 matmul（输出类型跟随输入）；一旦设成 `ACL_FLOAT16`/`ACL_BF16`，就进入「量化 matmul + 反量化」场景，输入变成 int8、需要额外的 `deqScale`（甚至 `perTokenScale`）张量。
2. **`matmulType` 区分普通乘与爱因斯坦乘**：`MATMUL_UNDEFINED`（普通二维/三维 matmul）与 `MATMUL_EIN_SUM`（带 batch 维的 Einsum，权重要求是三维）。
3. **`enAccum`/`hasBias`/`quantMode` 是行为修饰位**：累加（结果原子加到输入 accum 上）、偏置叠加、逐通道/逐 token 量化。

#### 4.1.2 核心流程

参数到行为的映射大致如下（伪代码）：

```text
if outDataType == ACL_DT_UNDEFINED:        # 浮点场景
    输入 = [x, weight] + (hasBias ? [bias] : (enAccum ? [accum] : []))
    输出 dtype 跟随输入；若 enAccum 则输出为 ACL_FLOAT
else:                                       # 量化场景
    输入 = [x(int8), weight(int8)] + (hasBias ? [bias(int32)] : []) + [deqScale]
           + (quantMode==PER_TOKEN ? [perTokenScale] : [])
    输出 dtype = outDataType（fp16/bf16），完成「int8 matmul → 反量化」
if matmulType == MATMUL_EIN_SUM:
    额外约束：enAccum/transposeA/hasBias/量化 均不可用，权重要三维
```

#### 4.1.3 源码精读

`LinearParam` 的字段全部集中定义在头文件里，注释里写清了每个字段的默认值与限制：

- `transposeA/transposeB/hasBias/outDataType/enAccum/matmulType/quantMode` 七个行为字段，末尾 `uint8_t rsv[21]` 是版本兼容预留位（见 [u2-l3](u2-l3-op-params.md) 讲过的 rsv 闸门）: [infer_op_params.h:L1391-L1471](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1391-L1471)
- 两个内嵌枚举 `MatmulType`（普通/爱因斯坦乘）与 `QuantMode`（未量化/逐通道/逐 token）就是行为分支码: [infer_op_params.h:L1393-L1402](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1393-L1402)

`outDataType` 同时充当浮点/量化开关这一点，在创建算子时的校验函数 `MatmulUndefindCheck` 里体现得最清楚——它用 `switch (opParam.outDataType)` 把 `ACL_DT_UNDEFINED`（走 `MatmulParamCheck`）与 `ACL_FLOAT16`/`ACL_BF16`（走反量化校验）分成两条完全不同的校验路径: [linear_operation.cpp:L144-L188](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L144-L188)

#### 4.1.4 代码实践

**目标**：在头文件里亲手核对 LinearParam 的字段与默认值。

1. 打开 `include/atb/infer_op_params.h`，定位 `struct LinearParam`。
2. 列出：`transposeA`、`transposeB`、`hasBias` 三者的默认值各是什么。
3. 找到 `outDataType` 的注释，确认「浮点场景支持 `ACL_DT_UNDEFINED`」「量化场景支持哪些值」。

**预期结果**：`transposeA=false`、`transposeB=true`、`hasBias=true`；`outDataType` 量化场景在 Atlas 800I A2 上支持 fp16/bf16，其它芯片仅 fp16。

#### 4.1.5 小练习与答案

**练习 1**：若用户把 `outDataType` 设成 `ACL_FLOAT16`，但又把 `hasBias` 设成 `false`、`quantMode` 设成 `PER_TOKEN`，输入张量个数应该是几个？

**答案**：4 个。量化场景下基础是 `[x, weight, deqScale]` 3 个；`quantMode==PER_TOKEN` 时再加 `perTokenScale` 共 4 个（见 4.2.3 的 `GetInputNum`）。注意此时 `hasBias` 不影响计数。

**练习 2**：为什么 `enAccum` 与 `hasBias` 不能同时为 `true`？

**答案**：因为 `enAccum` 表示「把 matmul 结果原子累加到一个已有的 accum 张量上」，与「叠加偏置」是两种互斥的输出后处理方式，源码 `MatmulParamCheck` 里显式校验拒绝: [linear_operation.cpp:L53-L59](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L53-L59)

---

### 4.2 LinearOperation：InferShape 与 Runner 分派（本讲核心）

#### 4.2.1 概念说明

`LinearOperation` 是 `OperationBase` 的子类（见 [linear_operation.h:L21](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.h#L21)）。它只做两件「算子特有」的事：

1. **`InferShapeImpl`**：根据输入 `TensorDesc` 和 Param，推导输出形状与 dtype；
2. **`CreateRunner`**：根据「芯片型号 + Param 场景」，挑选一个真正能跑的后端 Runner。

其余的校验、Tiling、workspace 对齐、异步下发，全部由 `OperationBase` 骨架统一处理。理解本节就理解了「一个 Linear 算子名，为什么能在不同芯片、不同量化模式下都跑得起来」——答案全在 `CreateRunner` 这棵决策树里。

#### 4.2.2 核心流程

`LinearOperation` 的输入输出个数是**动态**的，完全由 Param 决定：

```text
GetInputNum():
  浮点场景(outDataType==UNDEFINED):
      hasBias 或 enAccum → 3 个 (x, weight, bias/accum)
      否则                → 2 个 (x, weight)
  量化场景:
      hasBias            → 4 个 (x, weight, bias, deqScale)
      PER_TOKEN          → 4 个 (x, weight, deqScale, perTokenScale)
      其它               → 3 个 (x, weight, deqScale)
GetOutputNum(): 恒为 1
```

形状推导规则（普通场景，记输入 x 形状最后一维为 K、倒数第二维为 M，weight 给出 N）：

\[
\text{out.shape}[\text{dimNum}-2] = M,\quad \text{out.shape}[\text{dimNum}-1] = N
\]

输出默认继承输入 x 的描述，仅改最后两维与 dtype。

#### 4.2.3 源码精读

**输入个数动态计算**：`GetInputNum` 用嵌套 `if` 按 `outDataType/hasBias/enAccum/quantMode` 返回 2/3/4: [linear_operation.cpp:L365-L382](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L365-L382)

**形状推导**：`InferShapeImpl` 先把输出描述整体复制自输入 x，再按场景改 dtype（`enAccum` 改 `ACL_FLOAT`，量化改 `outDataType`），最后用 `OperationUtil::GetXTensorM/GetYTensorN` 算出 M/N 覆盖倒数两维: [linear_operation.cpp:L389-L412](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L389-L412)

**Runner 分派（本讲最重要的一段代码）**：`CreateRunner` 的全部逻辑就是一棵「950 芯片 vs 其它」的二叉决策树。950（Atlas A3 系列）走 aclnn 后端，按 Einsum/量化/普通三选一；其它芯片统一走 `LinearOpsRunner`（自己用 KernelGraph 拼）:

```cpp
std::shared_ptr<Runner> LinearOperation::CreateRunner(Context &context) const {
    if (Mki::PlatformInfo::Instance().GetPlatformType() == Mki::PlatformType::ASCEND_950) {
        if (param_.matmulType == infer::LinearParam::MATMUL_EIN_SUM) {
            return std::make_shared<LinearEinsumAclnnRunner>(param_);
        }
        if (param_.outDataType != ACL_DT_UNDEFINED) {
            return std::make_shared<LinearDequantAclnnRunner>(param_);
        }
        return std::make_shared<LinearAclnnRunner>(param_);
    }
    return std::make_shared<LinearOpsRunner>(param_);
}
```

见 [linear_operation.cpp:L430-L443](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L430-L443)。这正是 u3-l3 讲过的「同一算子存在 aclnn/ops 多条后端」的典型样例：950 用 CANN 提供的 aclnn 算子，其它芯片用 ATB 自维护的 Kernel。

> 备注：950 进入算子创建时还会先调用三个 `LoadAclnnFuncs()`，把 aclnn 函数符号动态加载进来，加载失败直接返回 `ERROR_CANN_ERROR`: [linear_operation.cpp:L231-L243](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L231-L243)

#### 4.2.4 代码实践

**目标**：跟踪 `CreateRunner` 决策树，预测不同配置会落到哪个 Runner。

1. 读 [linear_operation.cpp:L430-L443](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L430-L443)。
2. 填下面这张表（假设在非 950 芯片上）：

| `outDataType` | `matmulType` | 落到的 Runner |
| :--- | :--- | :--- |
| `ACL_DT_UNDEFINED` | `MATMUL_UNDEFINED` | ? |
| `ACL_BF16` | `MATMUL_UNDEFINED` | ? |
| `ACL_DT_UNDEFINED` | `MATMUL_EIN_SUM` | ? |

**预期结果**：三行分别是 `LinearOpsRunner`、`LinearOpsRunner`、`LinearOpsRunner`（非 950 下全部走 `LinearOpsRunner`，因为 Einsum/量化的 aclnn 变体只在 950 分支里）。这正好说明 `LinearOpsRunner` 自己要能处理全部子场景，引出 4.3 节。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `LinearOperation` 自己不实现 `Setup/Execute`，而只实现 `CreateRunner`？

**答案**：因为 `OperationBase` 用模板方法模式把 `Setup/Execute` 写成了不可改的骨架（负责校验、Tiling 拷贝、workspace 对齐等公共流程），子类只能通过 `CreateRunner` 钩子提供「执行后端」。`LinearOperation` 把计算差异完全委托给 Runner，符合「Operation 管推导与选型，Runner 管执行」的分工。

**练习 2**：在 950 芯片上，`outDataType=ACL_FLOAT16` 且 `matmulType=MATMUL_EIN_SUM` 同时设置会怎样？

**答案**：`CreateRunner` 里 Einsum 判断在量化判断之前，会优先返回 `LinearEinsumAclnnRunner`。但这种组合在创建阶段就会被 `MatmulEinParamCheck` 拦下（Einsum 要求 `outDataType==ACL_DT_UNDEFINED`），所以实际到不了 Runner: [linear_operation.cpp:L70-L85](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L70-L85)

---

### 4.3 LinearOpsRunner：用 KernelGraph 拼出各种 matmul（非 950 后端）

#### 4.3.1 概念说明

`LinearOpsRunner` 是非 950 芯片上 Linear 的唯一后端 Runner，继承自 `OpsRunner`（见 [linear_ops_runner.h:L21](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_ops_runner.h#L21)）。它的核心职责是：**把 `LinearParam` 描述的某种 matmul 形态，翻译成一张由若干基础 Kernel 节点组成的 `KernelGraph`**。

为什么要组图？因为非 950 芯片上没有「带 bias 的融合 matmul」单 kernel 能覆盖所有情况，于是 Runner 用 `TransdataOperation`（ND↔NZ 排布转换）、`MatMulOperation`（纯矩阵乘）、`ElewiseOperation`（元素级加 bias）这三种基础积木拼出完整流程。不同的芯片（910B/310B vs 其它）、不同的权重排布（ND vs NZ）、是否量化、是否带 bias，决定了图的节点数量和连接方式。

#### 4.3.2 核心流程

入口是 `SetupKernelGraph`，它是一棵分派树，按下面的优先级选择具体的组图函数：

```text
SetupKernelGraph(pack):
  填 matmulParam_（transposeA/B、enShuffleK、enDequant = outDataType!=UNDEFINED、outDtype）
  判断 isWeightNz_、xNeedMergeAxis_、weightNeedMergeAxis_
  if enDequant:        → 量化分支（910B+PER_TOKEN / 910B|310B / NZ / ND）
  elif enAccum:        → SetupKernelGraphMatmulAccum
  elif MATMUL_EIN_SUM: → hasBias ? EinElewiseAdd : Ein
  elif hasBias:        → bias 是 fp32 ? WithBias : (910B|310B ? ElewiseAdd910B : NZ/ND)
  else:                → x/weight 都是 fp32 ? MoeGateCorr : (910B|310B ? Matmul910B : NZ/ND)
```

每个 `SetupKernelGraphMatmul*` 函数的套路完全一致，分三步：

1. `InitKernelGraph(inNum, outNum, internalNum, nodeNum)` 预分配输入/输出/中间张量与节点槽位；
2. 取出这些张量与节点的引用；
3. 给每个节点填 `opDesc = {tid, "XxxOperation", param}`、`inTensors`、`outTensors`，必要时挂 `inTensorViewFuncs`（对输入做 reshape 视图，如 NZ 重排、轴合并）。

#### 4.3.3 源码精读

**分派树入口**：注意第 91 行把 `outDataType != ACL_DT_UNDEFINED` 折算成 `matmulParam_.enDequant`，这正是浮点/量化分支的源头: [linear_ops_runner.cpp:L84-L137](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_ops_runner.cpp#L84-L137)

**最简单的图（910B 无 bias）**：只有一个 `MatMulOperation` 节点，输入直接是 x 和 weight。若权重是 NZ 排布，则给 weight 挂 `matmulNzReshape_` 视图函数把逻辑形状映射到 NZ 物理形状: [linear_ops_runner.cpp:L139-L166](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_ops_runner.cpp#L139-L166)

**带 bias 的图（910B）**：两个节点——先 `MatMulOperation` 输出到中间张量，再 `ElewiseOperation`（ADD）把 bias 加上去。注意 bias 挂了 `elewiseAddUnsqueeze_` 视图，把 `[N]` 广播成可加的形状: [linear_ops_runner.cpp:L259-L297](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_ops_runner.cpp#L259-L297)

**非 910B 的图（ND 权重）**：因为非 A2 芯片的 matmul kernel 只吃 NZ 排布，所以要在前后各插一个 `TransdataOperation`（ND→NZ 与 NZ→ND），图变成 `Transdata(x) + Transdata(weight) + MatMul + Transdata(out)` 四个节点: [linear_ops_runner.cpp:L168-L215](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_ops_runner.cpp#L168-L215)

**组图基建**：`InitKernelGraph` 就是四个 `resize`，把 `KernelGraph` 的四个数组开好槽: [linear_ops_runner.cpp:L819-L825](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_ops_runner.cpp#L819-L825)

**注册**：文件末尾 `REG_RUNNER_TYPE(LinearOpsRunner)` 把它登记进 `RunnerPool`（u3-l5 讲过的对象池），让它能被复用，并 `REG_OP_PARAM` 登记三个用到的 OpParam 类型: [linear_ops_runner.cpp:L845-L848](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_ops_runner.cpp#L845-L848)

#### 4.3.4 代码实践

**目标**：跟踪 `SetupKernelGraph` 分派树，回答「在 910B 上、权重 ND、带 bias、浮点场景，会生成几个节点」。

1. 读 [linear_ops_runner.cpp:L84-L137](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_ops_runner.cpp#L84-L137) 的分派逻辑。
2. 沿路径走：`enDequant=false → enAccum=false → 非 EIN_SUM → hasBias=true → bias 不是 fp32 → Is910B()=true`，应进入哪个函数？
3. 打开该函数，数 `nodes.resize(?)` 与节点种类。

**预期结果**：进入 `SetupKernelGraphMatmulElewiseAdd910B`，共 2 个节点（`MatMulOperation` + `ElewiseOperation`），1 个中间张量。这是 Linear 在 A2 上最常见、也是最高效的组图。

#### 4.3.5 小练习与答案

**练习 1**：为什么非 910B 芯片要在 matmul 前后各加一个 `Transdata` 节点，而 910B 不用？

**答案**：因为 Cube 核的 matmul kernel 在不同芯片上对输入排布要求不同。910B（A2）的 matmul kernel 可直接吃 ND 排布；而非 A2 芯片的 matmul kernel 只吃 NZ（Fractional NZ）排布，所以需要先把 x、weight 从 ND 转成 NZ，算完再把结果从 NZ 转回 ND。`isWeightNz_` 标志就是用来跳过对已是 NZ 的权重的转换。

**练习 2**：`inTensorViewFuncs` 里挂的 `matmulMergeAxis_` 是干什么的？

**答案**：当 x 是三维 `[batch, M, K]` 而权重是二维时，matmul kernel 只接受二维输入，于是用 `matmulMergeAxis_` 把 x 的前两轴合并成 `[batch*M, K]` 的「视图」（不拷贝数据，只改描述），让 matmul 能直接处理: [linear_ops_runner.cpp:L39-L46](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_ops_runner.cpp#L39-L46)

---

### 4.4 950 平台的 aclnn 变体 Runner 三件套

#### 4.4.1 概念说明

在 Atlas A3（950）芯片上，`CreateRunner` 不走 `LinearOpsRunner`，而是分派到三个 aclnn Runner 之一（见 4.2.3）。这三个 Runner 都继承自 `AclnnRunner`（u3-l3 讲过的 aclnn 适配基类），把 ATB 的张量适配成 `aclTensor`，调用 CANN 提供的 aclnn 算子：

| Runner | 触发条件 | 调用的 aclnn 算子方向 |
| :--- | :--- | :--- |
| `LinearAclnnRunner` | 950 + 浮点普通 matmul | 普通 matmul |
| `LinearDequantAclnnRunner` | 950 + 量化（`outDataType≠UNDEFINED`） | `aclnnQuantMatmulV5` 等 |
| `LinearEinsumAclnnRunner` | 950 + `MATMUL_EIN_SUM` | Einsum matmul |

它们与 `LinearOpsRunner` 的区别在于：**不再自己用 KernelGraph 拼，而是直接调用 CANN 已经融合好的 aclnn 单算子**。这也是 950 上 Kernel 目录里没有 Linear 四件套的根本原因——计算下沉给了 CANN。

#### 4.4.2 核心流程

aclnn 系 Runner 的执行协议是 u3-l3 讲过的两段式：

```text
Setup 阶段: BuildAclnnVariantPack → SetAclNNWorkspaceExecutor
            （构造 aclTensor、调 GetWorkspaceSize 得到 aclOpExecutor 与 workspaceSize）
Execute 阶段: LaunchAclnnKernel
            （把 executor 与 workspace 交给 stream 异步执行）
```

`AclnnRunner` 基类用模板方法固化了 executor 缓存命中、地址刷新、workspace 同步等公共逻辑，把算子差异下放给三个纯虚钩子：`BuildAclnnVariantPack` / `SetAclNNWorkspaceExecutor` / `LaunchAclnnKernel`。每个子类只重写这三个钩子。

#### 4.4.3 源码精读

**三个 Runner 的类声明**（均 `: public AclnnRunner`，重写同样的三个 protected 钩子）:

- `LinearAclnnRunner`: [linear_aclnn_runner.h:L35](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_aclnn_runner.h#L35)
- `LinearEinsumAclnnRunner`: [linear_einsum_aclnn_runner.h:L23](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_einsum_aclnn_runner.h#L23)
- `LinearDequantAclnnRunner`：它的成员里能看到调用的 aclnn 函数指针类型 `AclnnQuantMatmulV5GetWorkspaceSizeFunc` 等，说明它对接的是 CANN 的量化 matmul 算子: [linear_dequant_aclnn_runner.h:L27-L69](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_dequant_aclnn_runner.h#L27-L69)

**分派点回顾**：三个 Runner 的选用规则全部集中在 `LinearOperation::CreateRunner` 里: [linear_operation.cpp:L433-L441](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L433-L441)

#### 4.4.4 代码实践

**目标**：对比三个 aclnn Runner 的头文件，确认它们的接口形态一致、差异只在成员变量。

1. 打开 `linear_aclnn_runner.h`、`linear_einsum_aclnn_runner.h`、`linear_dequant_aclnn_runner.h`。
2. 对照三个类的 `protected` 区，确认是否都重写了 `BuildAclnnVariantPack / SetAclNNWorkspaceExecutor / LaunchAclnnKernel`。
3. 看 `LinearDequantAclnnRunner` 的 `private` 成员，数它比另外两个多了哪些 aclnn 函数指针和「张量下标」成员。

**预期结果**：三者接口骨架相同；`LinearDequantAclnnRunner` 因为要处理 bias/deqScale/perTokenScale 等更多输入张量，多了 `xAclTensorIndex_/weightAclTensorIndex_/descaleAclTensorIndex_/...` 一组下标成员，以及 `aclnnQuantMatmulV5*` 与 `aclnnQuantMatmulWeightNz*` 两套函数指针。

#### 4.4.5 小练习与答案

**练习 1**：为什么 950 上不需要 `LinearOpsRunner` 那种 `Transdata` 节点？

**答案**：因为 950 调用的是 CANN 已融合好的 aclnn 算子，排布转换、bias 叠加、反量化都已经在 aclnn 内部完成，不需要 ATB 再用 KernelGraph 拆成多个基础节点。

**练习 2**：`LinearDequantAclnnRunner` 里 `isWeightNz_` 成员的作用是什么？

**答案**：用来区分权重的物理排布（NZ vs ND），从而在 `SetAclNNWorkspaceExecutor` 里选择 `aclnnQuantMatmulV5` 还是 `aclnnQuantMatmulWeightNz` 这两条不同的 aclnn 函数路径（见其 `SetAclnnQuantMatmulWorkspaceExecutor` 与 `SetAclnnQuantMatmulWeightNzWorkspaceExecutor` 两个私有方法）: [linear_dequant_aclnn_runner.h:L47-L48](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_dequant_aclnn_runner.h#L47-L48)

---

### 4.5 通信并行变体 LinearParallel 与稀疏变体 LinearSparse

#### 4.5.1 概念说明

除了 `LinearOperation`，ATB 还有两个「兄弟算子」，它们解决的是单卡 matmul 之外的问题：

- **`LinearParallelOperation`**：把 Linear 与集合通信（AllReduce/ReduceScatter/AllGather/AllToAll）**融合成一个算子**。在大模型张量并行（TP）里，一个投影层往往是「matmul → 通信」或「通信 → matmul」的固定搭配，融合后可以让计算与通信重叠，显著降低时延。它用一个 `type` 字段（`ParallelType`）描述 7 种并行拓扑，用 `backend` 字段选择 4 种通信后端。
- **`LinearSparseOperation`**：稀疏量化 Linear。功能与量化 Linear 类似，区别是先用压缩工具把 weight 压缩（`tilingK/tilingN` 由压缩算法决定，目前固定为 8），以提升性能。它目前只支持 Atlas 推理系列产品（310P）。

#### 4.5.2 核心流程

**LinearParallel 的并行拓扑**由 `ParallelType` 决定，典型的几种：

| `type` | 语义 | 输出形状变化 |
| :--- | :--- | :--- |
| `LINEAR_ALL_REDUCE` | matmul + AllReduce | 与普通 matmul 相同 |
| `LINEAR_REDUCE_SCATTER` | matmul + ReduceScatter | N 维按 `rankSize` 缩小 |
| `ALL_GATHER_LINEAR` | AllGather + matmul | M 维先放大 `rankSize` 再 matmul |

它的 `InferShapeImpl` 按 `type` 分派到不同子函数；`CreateRunner` 按 `backend` 分派到三种 Runner。

**LinearParallel 的 Runner 分派**：

```text
backend == "hccl" or "lccl" → LinearParallelGraphRunner   # 用 KernelGraph 组「matmul+通信」
backend == "lcoc"           → LinearParallelLcocRunner     # LCOC 通信计算重叠后端
backend == "mc2"            → LinearParallelAclnnRunner    # mc2 后端，走 aclnn
```

**LinearSparse** 则简单得多：固定 5 输入 1 输出，`CreateRunner` 直接返回 `LinearSparseOpsRunner`。

#### 4.5.3 源码精读

**LinearParallelParam 的并行与通信字段**：`ParallelType`（7 种拓扑）、`QuantType`（量化粒度）、`backend`（通信后端字符串）、`rank/rankSize/rankRoot`（通信域）、`hasResidual`（是否带残差）、`MoeInfo`/`TwoDimTPInfo`（MoE 与二维 TP 子结构）: [infer_op_params.h:L1482-L1579](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1482-L1579)

**LinearParallel 的动态输入输出**：`GetInputNum` 由 `hasResidual + 是否 MoE 拓扑 + 是否量化 + 是否 PER_TOKEN` 累加得到；`GetOutputNum` 在 `keepIntermediate=true`（仅 `ALL_GATHER_LINEAR` 支持）时返回 2（多一个中间结果）: [linear_parallel_operation.cpp:L212-L238](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear_parallel/linear_parallel_operation.cpp#L212-L238)

**LinearParallel 形状推导分派**：按 `type` switch 到 5 个子函数。以 `LINEAR_REDUCE_SCATTER` 为例，先做普通 matmul 推导，再把输出最后一维（或倒数第二维）除以 `rankSize`: [linear_parallel_operation.cpp:L266-L285](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear_parallel/linear_parallel_operation.cpp#L266-L285)

**LinearParallel 的 Runner 分派**：按 `backend` 字符串三选一，`hccl`/`lccl` 走 Graph Runner，`lcoc`/`mc2` 各走一个专用 Runner: [linear_parallel_operation.cpp:L617-L636](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear_parallel/linear_parallel_operation.cpp#L617-L636)

**LinearSparse 的强约束**：创建时强制 `Is310P()`、`transposeA=false`、`transposeB=true`、`tilingK=tilingN=8`，否则返回 `ERROR_INVALID_PARAM`: [linear_sparse_operation.cpp:L32-L51](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear_sparse/linear_sparse_operation.cpp#L32-L51)

**LinearSparse 的 Runner**：固定返回 `LinearSparseOpsRunner`，形状推导复用通用的 `OperationUtil::MatmulInferShape`: [linear_sparse_operation.cpp:L79-L109](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear_sparse/linear_sparse_operation.cpp#L79-L109)

#### 4.5.4 代码实践

**目标**：对比 `LinearOperation` 与 `LinearParallelOperation`，找出两者在「输入输出确定方式」「形状推导」「Runner 分派依据」上的根本差异。

1. 打开 [linear_operation.cpp:L365-L382](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L365-L382) 与 [linear_parallel_operation.cpp:L212-L238](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear_parallel/linear_parallel_operation.cpp#L212-L238)，对比 `GetInputNum/GetOutputNum`。
2. 对比两者的 `InferShapeImpl`：Linear 是单一规则，LinearParallel 是按 `type` 分派。
3. 对比两者的 `CreateRunner`：Linear 按「芯片 + 场景」分派，LinearParallel 按 `backend` 通信后端分派。

**预期结果**：见下一节「综合实践」的对照表。

#### 4.5.5 小练习与答案

**练习 1**：`LinearParallelOperation` 的 `CreateRunner` 里，为什么要把 `context` 先 `dynamic_cast<ContextBase*>`？

**答案**：因为 `LinearParallelGraphRunner` 和 `LinearParallelLcocRunner` 的构造需要访问 Context 内部的通信资源（执行流、通信域等），这些资源只在具体子类 `ContextBase` 里，抽象接口 `Context` 没有暴露。所以需要向下转型拿到 `ContextBase`: [linear_parallel_operation.cpp:L617-L627](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear_parallel/linear_parallel_operation.cpp#L617-L627)

**练习 2**：`LinearSparseParam` 为什么把 `tilingK/tilingN` 写死成 8？

**答案**：因为稀疏量化 Linear 的 weight 是用专门的压缩工具预先压缩的，压缩块大小固定为 8×8，算子内部按这个块大小做 Tiling，所以 Param 里只接受 8，创建时强制校验: [infer_op_params.h:L1595-L1598](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L1595-L1598)

---

## 5. 综合实践

本实践把全讲内容串起来，分「源码对比」与「运行 demo」两部分。

### 5.1 源码对比：Linear vs LinearParallel（必做）

对照阅读 [linear_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp) 与 [linear_parallel_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear_parallel/linear_parallel_operation.cpp)，填写下表：

| 维度 | LinearOperation | LinearParallelOperation |
| :--- | :--- | :--- |
| Param 主开关 | `outDataType`（浮点/量化） | `type`（并行拓扑）+ `backend`（通信后端） |
| 输入个数依据 | `outDataType/hasBias/enAccum/quantMode` | `hasResidual/是否MoE/是否量化/quantType` |
| 输出个数 | 恒为 1 | `keepIntermediate ? 2 : 1` |
| 形状推导 | 单一规则（改 M/N 两维） | 按 `type` 分派 5 条规则 |
| Runner 分派依据 | 芯片（950 vs 其它）+ 场景 | 通信后端字符串（hccl/lccl/lcoc/mc2） |
| 是否需要通信域 | 否 | 是（rank/rankSize/hcclComm） |

**关键结论**：`LinearOperation` 是「单卡 matmul」，分派维度是「芯片与量化场景」；`LinearParallelOperation` 是「多卡 matmul + 通信融合」，分派维度是「通信后端与并行拓扑」。两者都继承 `OperationBase`，都只重写 `InferShapeImpl/CreateRunner` 等钩子，执行链路完全一致（Operation → Runner → KernelGraph → Kernel），只是 Runner 子类不同。

### 5.2 运行 linear_demo（可选，需 NPU 环境）

`example/op_demo/linear/linear_demo.cpp` 是一个最小可运行示例，它构造了一个最朴素的 Linear（`transposeA=false, transposeB=false, hasBias=true, outDataType=ACL_DT_UNDEFINED`）: [linear_demo.cpp:L50-L62](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/linear/linear_demo.cpp#L50-L62)

操作步骤（参照 [example/op_demo/linear/README.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/linear/README.md)）：

1. 先 source CANN 与 ATB 的环境：`source /usr/local/Ascend/ascend-toolkit/set_env.sh` 与 `source ./ascend-transformer-boost/output/atb/set_env.sh`。
2. 进入 `example/op_demo/linear/`，执行 `bash build.sh`。
3. 运行 `./linear_demo`，应输出 `Linear demo success!`。

> 演示数据为 `x=[2,3] f16`、`weight=[3,2] f16`、`bias=[1,2] f16`、`output=[2,2] f16`，仅用于跑通流程，不代表真实业务。若无 NPU 环境，本步骤标注「待本地验证」，改为阅读 `main` 函数理解五段式骨架即可: [linear_demo.cpp:L64-L114](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/linear/linear_demo.cpp#L64-L114)

**进阶修改**：把 `CreateLinearOperation` 里的 `param.matmulType` 改成 `MATMUL_EIN_SUM`、`hasBias=false`，再参考 README 切到 `linear_einsum_demo.cpp` 的数据规格（x/weight 改三维），观察 `CreateRunner` 在 950 上会落到 `LinearEinsumAclnnRunner`、在非 950 上会进入 `LinearOpsRunner::SetupKernelGraphMatmulEin`。

## 6. 本讲小结

- Linear 算子族用一个 `LinearParam` + 字段组合覆盖了浮点/量化/Einsum/累加等多种 matmul，`outDataType` 是浮点与量化场景的总开关。
- `LinearOperation` 只重写 `InferShapeImpl` 与 `CreateRunner` 两个钩子；`CreateRunner` 是核心决策树，950 走 aclnn 三件套，其它芯片走 `LinearOpsRunner`。
- 非 950 后端 `LinearOpsRunner` 用 `KernelGraph` 把 `Transdata + MatMul + ElewiseAdd` 拼成完整流程，组图函数的分派依据是「芯片/权重排布/量化/bias」。
- 950 上的 `LinearAclnnRunner`/`LinearDequantAclnnRunner`/`LinearEinsumAclnnRunner` 直接调用 CANN 的 aclnn 融合算子，不再自组图。
- `LinearParallelOperation` 是「matmul + 集合通信」融合算子，按 `type` 分拓扑、按 `backend` 分 Runner；`LinearSparseOperation` 是压缩 weight 的稀疏量化 Linear，限定 310P 与 `tilingK/N=8`。
- 全族共享 `OperationBase` 骨架与 `Operation → Runner → KernelGraph → Kernel` 执行链路，差异只在 Param、形状推导规则与 Runner 子类。

## 7. 下一步学习建议

- 想看「带 bias 的归一化」如何与 Linear 串联，继续读 [u4-l2 Normalization 算子](u4-l2-normalization.md)。
- 想理解 Linear 在多卡 TP/EP 里如何与通信算子协作，结合 [u5-l1 通信算子与 HCCL 通信域](u5-l1-comm-hccl.md) 一起读 `LinearParallel` 的 Graph Runner。
- 想自己加一个 matmul 变体（新 Param 字段或新 Runner），参考 [u6 自定义算子开发](u6-l1-plugin-infra.md) 系列中的 Operation + Runner + 注册流程。
- 建议顺带阅读 `src/ops/ops_infer/linear_parallel/linear_parallel_graph_runner.cpp`，看它如何把 matmul 节点与通信节点拼在同一张 `KernelGraph` 里，这是「计算通信重叠」的实现关键。
