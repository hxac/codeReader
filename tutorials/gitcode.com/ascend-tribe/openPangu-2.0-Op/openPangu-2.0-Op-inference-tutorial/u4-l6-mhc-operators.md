# u4-l6 MHC 算子：流形约束超连接的前后处理

## 1. 本讲目标

本讲解读 `ops-transformer/mhc` 目录下的两个推理算子，它们共同服务于 MHC（Manifold-Constrained Hyper Connection，流形约束超连接）这一种改造残差连接的网络结构。读完本讲你应该能够：

1. 说清 `ai_infra_mhc_pre_split_post_res`（系数发生器）与 `ai_infra_mhc_sandwich_norm_post_preonly`（融合执行器）在 MHC 结构中各自的位置，以及前者的输出如何成为后者的输入。
2. 对比两种「分支策略」：pre 算子用 **TilingKey 编译期分派** single_tile / multi_tile 两条 kernel 路径；sandwich 算子只用一个 TilingKey，靠 **TilingData 字段运行期分派** singlecore / dualcore / dualcore_mt 三条路径。
3. 理解双核协同的两种做法：pre 的双核「phi 行对半、冗余算 RMSNorm、零同步」；sandwich 的双核「N 轴 head 对半、GM workspace 交换部分和、自旋屏障对齐」。
4. 解释 v2 接口新增 `return_h_in_f32` 输出的动机：把算子内部本来就有的 fp32 中间值按需导出，替代 v1 的「分配占位张量却不用」。

本讲承接 u2-l4（kernel 入口 / GET_TILING_DATA / TilingKey）的知识，并把它们放到一对真实算子里对照着看。

## 2. 前置知识

- **MHC（超连接）**：传统 Transformer 每层的残差连接只有一条流 `y = x + F(x)`。MHC 把它扩展为 N 条并行流（本项目固定 N=4），每个 token 额外携带两组系数：`h_post`（N 个门控标量）与 `h_res`（N×N 残差混合矩阵），由网络学习得到。推理时要先用输入 x 算出这些系数，再用它们做混合。
- **token / T**：一个序列元素。`T = B*S`（batch × seq），文档里也叫 `totalTokens`。
- **D（embedding 维）**：本仓库 MHC 算子只支持 D ∈ {2560, 5120}，N 固定为 4。
- **UB 容量约束**：向量核的 Unified Buffer 单次能放下的数据有限，本项目取 `D_TILE_MAX = 2560` 作为单个 tile 的上限，D=5120 时必须拆成两块（tile）处理——这就是「tile 切分」的来源。
- **AIV**：AI Vector 核，本讲两个算子都是 `KERNEL_TYPE_AIV_ONLY`，只跑在向量核上。
- **双核协同**：一个 token 的计算交给 2 个核并行完成，两核之间通过 GM（Global Memory）上的 workspace 交换数据，并用「自旋屏障」互等。
- **TilingKey 分派 vs 运行期分派**：前者在 host 侧 `SetTilingKey`、device 侧 `TILING_KEY_IS` 对号入座，编译出多份 kernel 二进制；后者只编译一份，kernel 里读 TilingData 字段走 if 分支。u2-l4 已见过这两种风格，本讲是一组完整对照样本。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/docs/npu_ai_infra_mhc_pre_split_post_res.md` | pre 算子 npu 接口文档：计算公式、参数、规格约束 |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_tiling.h` | pre 算子 TilingData（15 字段）与两个 TilingKey 常量定义 |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_tiling.cpp` | pre 算子 tiling 主逻辑：dimTile/dimLoop/双核判定/分核 |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res.cpp` | pre 算子 kernel 入口：按 TilingKey 分派两个 kernel 类 |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res_base.h` | pre 算子共享基类：GM 绑定、UB 分配、点积/写出辅助函数 |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res_single_tile.h` | SINGLE_TILE 路径：D≤2560，每 token 2 核，phi 行对半 |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res_multi_tile.h` | MULTI_TILE 路径：D=5120，每 token 1 核，Phase A/B 两遍 |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/docs/npu_mhc_sandwich_norm_post_preonly.md` | sandwich 算子 npu 接口文档：五阶段公式、v1/v2 原型 |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_host/ai_infra_mhc_sandwich_norm_post_preonly_tiling.h` | sandwich 算子 TilingData（含 coresPerToken、returnHInF32） |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_host/ai_infra_mhc_sandwich_norm_post_preonly_tiling.cpp` | sandwich 算子 tiling：coresPerToken 判定、workspace 与同步区布局 |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_api/aclnn_ai_infra_mhc_sandwich_norm_post_preonly.cpp` | sandwich 算子 aclnn 两段式接口（V1 与 V2 两套） |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_api/ai_infra_mhc_sandwich_norm_post_preonly.cpp` | L0 封装：登记进 executor 下发列表，h_in_f32 占位逻辑 |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly.cpp` | sandwich 算子 kernel 入口（无 TilingKey 分支） |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h` | 主类：Init 核映射、Process 三路分派 |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h` | 双核同步、workspace 读写、输出写出（含 h_in_f32） |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_compute.h` | 计算 building blocks：加载、RMSNorm、MHC_Post、Gate |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_dualcore.h` | 双核单 tile 路径（D≤2560 且 token 少） |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_dualcore_mt.h` | 双核多 tile 路径（D=5120 且 token 少） |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_singlecore.h` | 单核兜底路径 |
| `ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp` | torch 侧 v1/v2 实现（构造输出、EXEC_NPU_CMD_V1） |
| `ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/tests/st/test_ai_infra_mhc_sandwich_norm_post_preonly.py` | ST 精度测试：NPU vs CPU fp64 golden 对拍 |

两个算子目录都是标准的 docs / op_api / op_host / op_kernel / tests 五件套（见 u1-l3）。

## 4. 核心概念与源码讲解

本讲的三个最小模块（外加一个 v2 接口模块）：

1. **MHC 结构**：两个算子怎么配合，公式如何衔接。
2. **tile 切分**：D 轴分块（dimTile/dimLoop）如何决定走哪条 kernel 路径。
3. **双核协同**：token 少时用两个核合做一个 token，两种同步代价的取舍。
4. **v2 接口的 return_h_in_f32**：一个「把中间值捎带导出」的接口演进样本。

### 4.1 MHC 结构：一个系数发生器 + 一个融合执行器

#### 4.1.1 概念说明

MHC 层的推理计算被拆成两个自定义算子：

- **`ai_infra_mhc_pre_split_post_res`（下称 pre 算子）**：输入 mHC 层的原始输入 x（[B,S,N,D] 或 [T,N,D]），输出每个 token 的两组系数——`h_post`（[T,N]，post 分支门控）与 `h_res`（[T,N,N]，comb/残差分支混合矩阵）。它只负责「算系数」这一部分，文档明确写着它是 mhc_pre 算子的一部分。
- **`ai_infra_mhc_sandwich_norm_post_preonly`（下称 sandwich 算子）**：消费上面产出的 h_post / h_res，把五个阶段的小算子深度融合成一次 kernel 调用：RMSNorm_0 → MHC_Post → RMSNorm_mid（可选）→ MHC_Pre（PreOnly）→ RMSNorm_1，输出 `h_in_prime`、`x_2_out`（v2 另有 `h_in_f32`）。

为什么拆成两个而不是一个大算子？pre 算子的输入 x 是 MHC 层的入口张量，输出系数很小（每 token 仅 20 个 fp32）；而 sandwich 算子的输入 h_out/residual 是上一层输出，形状不同、生命周期不同。分开后各自 tiling 独立、各自复用，也方便 sandwich 在层间被图引擎融合。

#### 4.1.2 核心流程

pre 算子的计算公式（见文档）：

\[
X_{flat} = \mathrm{reshape}(x, [B,S,N \cdot D])
\]

\[
\mathrm{inv\_rms} = \frac{1}{\sqrt{\frac{1}{N \cdot D}\sum_{k} X_{flat,k}^2 + \mathrm{norm\_eps}}}
\]

\[
X_{hat} = (X_{flat} \cdot \phi^T) \odot \mathrm{inv\_rms}, \qquad [X_{post}, X_{comb}] = \mathrm{split}(X_{hat}, [N, N^2])
\]

\[
h_{post} = 2 \cdot \sigma(\alpha_0 \cdot X_{post} + b_{post}), \qquad h_{res} = \alpha_1 \cdot \mathrm{reshape}(X_{comb}, [N,N]) + \mathrm{reshape}(b_{comb}, [N,N])
\]

注意 phi 的形状是 \([N^2+N, N\cdot D]\)：前 N 行投影出 post 门控，后 N² 行投影出 comb 混合矩阵，这就是 `phiRows = N²+N = 20` 的来历。

sandwich 算子的阶段 2（MHC_Post）直接消费上面两个输出：

\[
x_{2,(b,s,n,d)} = \text{h\_post}_{(b,s,n)} \cdot x_{1,(b,s,d)} + \sum_{j=0}^{N-1} \left( \text{h\_res}_{(b,s,j,n)} \cdot \text{residual}_{(b,s,j,d)} \right)
\]

数据流串起来就是：

```text
x [T,N,D] ──pre算子──> h_post [T,N]  ─┐
                      h_res  [T,N,N] ─┼──sandwich算子──> h_in_prime [T,D]
h_out [T,D] ─────────────────────────┘        x_2_out  [T,N,D]
residual [T,N,D] ────────────────────┘        (h_in_f32 [T,D], 仅v2)
phi/alpha/bias/gamma_* ───────────────┘
```

一个容易混淆的细节：两个算子都有名为 `phi` 的输入，但形状不同。pre 的 phi 是 \([N^2+N, N\cdot D]\)（20 行，post+comb 全量投影）；sandwich 的 phi 是 \([N, N\cdot D]\)（仅 4 行，PreOnly 分支的 gate 投影），tiling 头文件里专门注释了 "pre-branch only"。

#### 4.1.3 源码精读

先看 pre 算子的 OpDef，确认输入输出与 SOC 配置：

```cpp
this->Input("x").ParamType(REQUIRED).DataType({ge::DT_BF16, ge::DT_FLOAT16})...
this->Input("phi").ParamType(REQUIRED).DataType({ge::DT_FLOAT, ge::DT_FLOAT})...
this->Output("h_post_out").ParamType(REQUIRED).DataType({ge::DT_FLOAT, ge::DT_FLOAT})...
this->Output("h_res_out").ParamType(REQUIRED).DataType({ge::DT_FLOAT, ge::DT_FLOAT})...
this->Attr("norm_eps").AttrType(OPTIONAL).Float(1e-6f);
```

[ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_def.cpp:L23-L60](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_def.cpp#L23-L60) 声明了 4 输入（x 是 bf16/fp16，phi/alpha/bias 是 fp32）、2 输出（都是 fp32）和一个可选属性；[同文件:L69-L70](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_def.cpp#L69-L70) 把算子登记到 ascend910b 与 ascend910_93 两个 SOC。

kernel 侧对应公式最后两步的实现（h_post = 2σ(...)）：

```cpp
Muls(hPostOut, xHat, alpha0_, postCount);
Add(hPostOut, hPostOut, biasLocal, postCount);
Sigmoid<float, true>(hPostOut, hPostOut, postCount);
Muls(hPostOut, hPostOut, FLOAT_TWO, postCount);   // 2·σ(...)
```

[ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res_base.h:L246-L265](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res_base.h#L246-L265) 是 `ComputeAndWriteHPost`：乘 α0、加 bias、sigmoid、乘 2，然后 DataCopy 写到 GM。紧接着 [同文件:L269-L285](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res_base.h#L269-L285) 的 `ComputeAndWriteHRes` 实现线性部分 \(h_{res} = \alpha_1 \cdot X_{comb} + b_{comb}\)（Muls + Add 后直写 GM，没有非线性）。

再看 sandwich 算子消费这两个系数的地方：

```cpp
for (uint32_t hi = 0; hi < myN; ++hi)
    Muls(x2Fp32[hi * dimTile_], hOutFp32, postVals[hStart + hi], tD);
for (uint32_t j = 0; j < N; ++j)
    for (uint32_t hi = 0; hi < myN; ++hi)
        Axpy(x2Fp32[hi * dimTile_], resFp32[j * dimTile_], combVals[j * N + (hStart + hi)], tD);
```

[ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_compute.h:L126-L147](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_compute.h#L126-L147) 的 `ComputeX2MyHeads` 逐字实现了 MHC_Post 公式：`x2[h] = h_post[h]·x1 + Σ_j h_res[j,h]·residual[j]`，其中 `combVals[j*N + h]` 正是 pre 算子输出的 h_res 的第 (j, h) 个元素。系数本身由 [同文件:L19-L40](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_compute.h#L19-L40) 的 `LoadTokenWeights` 从 GM 读入 UB 标量数组。

pre 算子的 tiling 还做了严格的规格校验：[ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_tiling.cpp:L156-L166](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_tiling.cpp#L156-L166) 检查 T∈[1,512K]、N==4、D∈{2560,5120}；[同文件:L99-L103](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_tiling.cpp#L99-L103) 限定 SOC 只允许 ASCEND910B / ASCEND910_93，与 OpDef 的 AddConfig 呼应。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：用两份 npu 文档的公式，画出 MHC 层的「张量接线图」，验证 pre 的输出与 sandwich 的输入逐一对得上。

**操作步骤**：

1. 打开 [ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/docs/npu_ai_infra_mhc_pre_split_post_res.md:L16-L34](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/docs/npu_ai_infra_mhc_pre_split_post_res.md#L16-L34)，抄下 6 行公式。
2. 打开 [ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/docs/npu_mhc_sandwich_norm_post_preonly.md:L27-L57](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/docs/npu_mhc_sandwich_norm_post_preonly.md#L27-L57)，找出阶段 2（MHC_Post）公式里 `h_post_(b,s,n)` 与 `h_res_(b,s,j,n)` 两项。
3. 对照 sandwich 的参数表（[同文件:L110-L126](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/docs/npu_mhc_sandwich_norm_post_preonly.md#L110-L126)）与 pre 的返回值说明（[pre 文档:L70-L74](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/docs/npu_ai_infra_mhc_pre_split_post_res.md#L70-L74)），核对 shape 与 dtype：h_post [T,N] fp32、h_res [T,N,N] fp32，两侧一致。
4. 再到 kernel 代码里各找一个消费点交叉验证：`ComputeX2MyHeads` 里的 `combVals[j * N + (hStart + hi)]` 与 pre 侧 `ComputeAndWriteHRes` 写出的 `(gmRowStart - N_)` 偏移（h_res 段从 phi 第 N 行开始）。

**需要观察的现象**：公式里的下标 (j,n) 在两侧代码中一个写成 `combVals[j*N + h]`（行优先），一个对应 phi 的第 `N + j*N + h` 行，语义一致。

**预期结果**：得到一张 8 个张量的接线图（x、phi、alpha、bias → h_post、h_res → 加上 h_out、residual、gamma_* → h_in_prime、x_2_out），并能解释每个箭头对应哪条公式。

**待本地验证**：本实践是纯阅读任务，无需硬件；若想跑通端到端，可参照 sandwich 文档 [L201-L250](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/docs/npu_mhc_sandwich_norm_post_preonly.md#L201-L250) 的调用示例（需昇腾环境）。

#### 4.1.5 小练习与答案

**练习 1**：pre 算子的 phi 为什么是 \(N^2+N\) 行？这 20 行分别对应什么？

答案：前 N=4 行投影出 \(X_{post}\)（post 门控段），后 N²=16 行投影出 \(X_{comb}\)（残差混合矩阵段），公式里 \(\mathrm{split}(X_{hat}, [N, N^2])\) 按这个边界切开；bias 的长度也是 20，两段各自加偏置。

**练习 2**：pre 的 h_post 用 \(2\sigma(\cdot)\)，sandwich 阶段 3 的 gate 用 \(\sigma(\cdot)+\epsilon_{hc}\)，两者为什么不同？

答案：它们是 MHC 结构中两个不同分支的门控：前者是 post 分支系数（乘 2 是超连接定义里的放缩因子，且无 eps）；后者是 pre 分支的加权系数，加 hc_eps 防止 gate 恰为 0 导致信息截断。可以在 [kernel_compute.h:L193-L218](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_compute.h#L193-L218) 的 `ComputeGateValues` 里看到 `Div + Adds(hcEps_)` 的实现。

**练习 3**：sandwich 算子 tilin g 校验 `residual.ndim == h_out.ndim + 1`，这对应什么形状约定？

答案：h_out 是 [B,S,D] 或 [T,D]，residual 是 [B,S,N,D] 或 [T,N,D]——residual 恰好比 h_out 多出 N 轴这一维，校验代码在 [ai_infra_mhc_sandwich_norm_post_preonly_tiling.cpp:L59-L68](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_host/ai_infra_mhc_sandwich_norm_post_preonly_tiling.cpp#L59-L68)。

### 4.2 tile 切分：D 轴分块与两条 kernel 路径

#### 4.2.1 概念说明

「tile 切分」解决的是 UB 放不下整行数据的问题：一个 token 的 x 是 N×D 个元素（fp32 展开），D=5120 时达 20 万个 float，超过单个 tile 上限。于是 host 侧把 D 轴切成 `dimLoop` 块、每块 `dimTile` 个元素，kernel 逐块搬入、逐块累积。

两个算子共享同一组常量：`dimTile = min(D, 2560)` 并按 128 元素对齐；D=2560 → dimLoop=1（单 tile），D=5120 → dimLoop=2（双 tile）。区别在于拿到 dimLoop 之后怎么选 kernel 路径：

- **pre 算子**：dimLoop 映射成 TilingKey（1→key 0，2→key 1），kernel 入口 `TILING_KEY_IS` 分派到两个不同的 C++ 类，编译期各生成一份二进制。
- **sandwich 算子**：永远 `SetTilingKey(0)`，dimLoop 作为 TilingData 字段传进 kernel，Process 里运行期 if 分派。

#### 4.2.2 核心流程

pre 算子 host 侧的切分决策（伪代码）：

```text
dimTile = 对齐128(min(D, 2560))
dimLoop = ceil(D / dimTile)                    // 2560→1, 5120→2
tilingKey = (dimLoop == 1) ? SINGLE_TILE(0) : MULTI_TILE(1)
```

MULTI_TILE 路径的 kernel 内部又是「一个 token 两遍扫描」：

```text
Phase A: for d in 0..dimLoop-1:  搬入 x 的第 d 块 → Cast fp32 → 累加 Σx²
         循环结束算 invRms
Phase B: for d in 0..dimLoop-1:  重新搬入第 d 块 → Cast → 对全部 20 行 phi 做分块点积累加
         （Phase A 时 xFp32 被原地覆盖成 x²，所以 Phase B 必须从 GM 重读 x）
```

单 tile 时这些循环退化为一次，x 只读一遍。

#### 4.2.3 源码精读

TilingKey 常量与 TilingData 定义：

```cpp
constexpr uint64_t MHC_PRE_SPLIT_TILING_KEY_SINGLE_TILE = 0;  // D<=2560，每token用2核
constexpr uint64_t MHC_PRE_SPLIT_TILING_KEY_MULTI_TILE  = 1;  // D>2560，每token用1核
```

[ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_tiling.h:L28-L32](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_tiling.h#L28-L32) 用 constexpr 定义两个 key；[同文件:L35-L51](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_tiling.h#L35-L51) 定义 15 个字段的 TilingData（totalLength/N/D/phiRows/dimTile/dimLoop/coresPerToken/phiRowMid/blockDim/baseT/tailT/coreTAct/normEps/reduceWorkSize/invK），dimTile 与 dimLoop 都在其中。

host 侧切分计算：

```cpp
dimTile_ = D_ > MHC_DIM_TILE_MAX ? MHC_DIM_TILE_MAX : D_;   // min(D, 2560)
dimTile_ = (dimTile_ / MHC_ALIGN_ELEM) * MHC_ALIGN_ELEM;    // 128 对齐
dimLoop_ = (D_ + dimTile_ - 1) / dimTile_;
tilingKey_ = (dimLoop_ == 1) ? SINGLE_TILE : MULTI_TILE;
```

[ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_tiling.cpp:L266-L290](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_tiling.cpp#L266-L290) 是 `GenerateTiling` 的前半段；上限常量 `MHC_DIM_TILE_MAX = 2560`、对齐粒度 `MHC_ALIGN_ELEM = 128` 定义在 [同文件:L52-L54](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_tiling.cpp#L52-L54)。算好的 key 最终在 DoTiling 里落账：[同文件:L380-L383](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_tiling.cpp#L380-L383) 依次 `SaveToBuffer` → `SetTilingKey(tilingKey_)` → `SetBlockDim(blockDim_)`。

device 侧入口按 key 分派：

```cpp
if (TILING_KEY_IS(TILING_KEY_SINGLE_TILE)) {
    KERNEL_TASK_TYPE(TILING_KEY_SINGLE_TILE, KERNEL_TYPE_AIV_ONLY);
    KernelMhcSingleTile<DTYPE_X> op;  op.Init(...);  op.Process();
} else if (TILING_KEY_IS(TILING_KEY_MULTI_TILE)) {
    KERNEL_TASK_TYPE(TILING_KEY_MULTI_TILE, KERNEL_TYPE_AIV_ONLY);
    KernelMhcMultiTile<DTYPE_X> op;   op.Init(...);  op.Process();
}
```

[ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res.cpp:L43-L53](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res.cpp#L43-L53) 是标准的三段式：`GET_TILING_DATA_WITH_STRUCT` 解包（[L33](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res.cpp#L33)）→ 按 key 实例化不同的 Kernel 类。文件头注释 [L15-L19](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res.cpp#L15-L19) 直接写明了两条路径的分工。

MULTI_TILE 的 Phase A/B 结构：

```cpp
CopyInXTile(xTokenOff, 0, TileCopyD(0));                 // 预取 tile 0
for (int64_t d = 0; d < dimLoop_ - 1; d++) {
    CopyInXTile(xTokenOff, (d + 1) * dimTile_, copyDNext);  // 先发下一块 MTE2
    ... DeQue → Cast → AccumSumSq<true>(xFp32, sumSq, copyD, pipe_);  // 与搬运重叠
}
```

[ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res_multi_tile.h:L126-L173](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res_multi_tile.h#L126-L173) 是 Phase A：depth=2 的队列让 MTE2 搬入第 d+1 块与向量核算第 d 块重叠；`AccumSumSq<true>` 的 `INPLACE=true` 表示把 x² 原地写回 xFp32（省掉一个 squareBuf），代价是 Phase B（[同文件:L175-L227](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res_multi_tile.h#L175-L227)）必须从 GM 重读 x。尾部 tile 的实际列数由 [同文件:L115-L120](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res_multi_tile.h#L115-L120) 的 `TileCopyD` 处理。

对比 SINGLE_TILE 路径：[ai_infra_mhc_pre_split_post_res_single_tile.h:L122-L140](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res_single_tile.h#L122-L140) 中 `copyD = D_` 一次搬全量、`AccumSumSq<false>` 用 squareBuf 暂存 x² 保留 xFp32 供后续点积复用——同一条公式，两条路径以「UB 预算」为轴做了不同取舍。

sandwich 算子的 D 轴切分完全同构，只是不分 TilingKey：[ai_infra_mhc_sandwich_norm_post_preonly_tiling.cpp:L145-L155](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_host/ai_infra_mhc_sandwich_norm_post_preonly_tiling.cpp#L145-L155) 计算 dimTile/lastDimTile/dimLoop；[同文件:L240-L241](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_host/ai_infra_mhc_sandwich_norm_post_preonly_tiling.cpp#L240-L241) `SetBlockDim(usedCoreNum)` 之后 `SetTilingKey(0)` 写死。kernel 侧的分派在 Process 里：

```cpp
for (uint32_t t = tokenStart_; t < tokenEnd_; ++t) {
    if (coresPerToken_ == NUM_TWO && dimLoop_ == 1)   ProcessTokenDualCore(t);
    else if (coresPerToken_ == NUM_TWO && dimLoop_ > 1) ProcessTokenDualCoreMultiTile(t);
    else                                               ProcessToken(t);
}
```

[ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h:L209-L216](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h#L209-L216) 用两个 TilingData 字段（coresPerToken、dimLoop）做 3 路运行期分派；kernel 入口因此没有任何 TILING_KEY_IS，只有 [ai_infra_mhc_sandwich_norm_post_preonly.cpp:L31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly.cpp#L31) 的 `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)`。

#### 4.2.4 代码实践（参数推演型）

**实践目标**：验证你能手工复现 tiling 的切分与分支决策。

**操作步骤**：

1. 设 D=5120，按 [tiling.cpp:L269-L277](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_tiling.cpp#L269-L277) 手算：dimTile、dimLoop、tilingKey。
2. 再设 D=2560、T=8、coreNumAiv=50，继续手算 needDualCore / coresPerToken / coreGroups / baseT / coreTAct / tailT / blockDim（[L281-L303](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_tiling.cpp#L281-L303)）。
3. 做一个思想实验：把 `MHC_DIM_TILE_MAX` 从 2560 改成 1280（只是假设，不要真改源码），回答 D=2560 时 dimLoop、tilingKey、走哪个 kernel 类、AccumSumSq 用哪个模板实参。
4. 若本机装了 Python，可以用三行脚本验证整除关系（例如 `math.ceil`）。

**需要观察的现象**：D=5120 时 dimLoop=2 → key=1（MULTI_TILE），且 needDualCore 要求 dimLoop==1，所以 D=5120 永远单核每 token；D=2560、T=8 < 50/2=25 → 双核每 token。

**预期结果**：步骤 2 的答案依次是 needDualCore=true、coresPerToken=2、coreGroups=25、baseT=1、coreTAct=8、tailT=1、blockDim=16。

**待本地验证**：UT 框架可以在无硬件环境验证这些数值，可参照 [tests/ut/op_host/test_ai_infra_mhc_pre_split_post_res_tiling.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/tests/ut/op_host/test_ai_infra_mhc_pre_split_post_res_tiling.cpp) 补一个 D=2560/T=8 的用例后用 `bash build.sh -u --ophost` 跑（见 u6-l1）。

#### 4.2.5 小练习与答案

**练习 1**：sandwich 算子的 `lastDimTile` 什么时候非 0？

答案：当 D 不能被 dimTile 整除时记录尾块长度（`lastDimTile = D % dimTile`，[tiling.cpp:L154](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_host/ai_infra_mhc_sandwich_norm_post_preonly_tiling.cpp#L154)）。当前规格 D∈{2560,5120}、dimTile∈{2560} 时恒整除、lastDimTile=0，但代码保留了不整除的通用处理（kernel 侧 `GetTileD`）。

**练习 2**：pre 的 MULTI_TILE 路径为什么要把 x 从 GM 读两遍，而 SINGLE_TILE 只读一遍？

答案：MULTI_TILE 里 Phase A 用 `AccumSumSq<true>` 把 x² 原地写进 xFp32 省掉 squareBuf 的 UB 开销（[multi_tile.h:L156-L158](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res_multi_tile.h#L156-L158) 注释），xFp32 被覆盖后 Phase B 只能重读；SINGLE_TILE 用独立 squareBuf 保住 xFp32，一份输入喂两个用途。这是「UB 预算 vs GM 带宽」的经典交换。

**练习 3**：如果把 sandwich 的 `SetTilingKey(0)` 改成按 dimLoop 设两个 key（假设 kernel 入口也加了 TILING_KEY_IS），相比现在会多付出什么、换来什么？

答案：多付出编译期实例化份数与二进制体积、host/kernel 两侧 key 数值要硬编码对齐；换来的是每份 kernel 的分支被编译期剪枝、dimLoop 等成为编译期常量（可能更激进地展开循环与分配 UB）。pre 算子就是选了这条路，sandwich 选了运行期分派——两条路线在本仓库里是并存的工程取舍。

### 4.3 双核协同：两种「以冗余换同步」的设计

#### 4.3.1 概念说明

当 token 数太少、单核每 token 摊不满所有 AIV 时，两个算子都会让 **2 个核合做一个 token**（`coresPerToken=2`）。但「怎么分、怎么合」完全不同：

| 维度 | pre 算子（SINGLE_TILE 双核） | sandwich 算子（dualcore） |
| --- | --- | --- |
| 切分轴 | phi 行对半（core0 拿 h_post 段+前半 comb 行，core1 拿后半 comb 行） | N 轴 head 对半（core0 管 head 0..N/2-1，core1 管 head N/2..N-1） |
| 交换数据 | 无 | sumSqPre、xHat[4]、x2 的部分和经 GM workspace 交换 |
| 同步原语 | 无（两核输出段天然不重叠） | sense-reversing 自旋屏障 |
| 冗余计算 | 每核都独立算全量 x 的 RMSNorm（invRms） | RMSNorm_mid 里每核冗余算对方 head 的 sumSq；RMSNorm_0 双核重复算 |

共同的设计哲学：**跨核同步很贵，能用「本核重算一遍」代替就不交换**。pre 干脆整个 invRms 都两核各算各的；sandwich 只在不得不合并的地方（phi 点积的部分和、对方 head 的 x2 数据）才走 GM。

#### 4.3.2 核心流程

sandwich 双核路径一个 token 的时序：

```text
core0 (coreRole=0, head 0..1)                core1 (coreRole=1, head 2..3)
─────────────────────────────                ─────────────────────────────
Phase A: RMSNorm_0 (冗余)                     Phase A: RMSNorm_0 (冗余)
Phase B: x2[我的 2 个 head]                   Phase B: x2[我的 2 个 head]
[可选] RMSNorm_mid: 冗余算对方 head 的 sumSq   同左（因此 mid 阶段无需同步）
Phase C: sumSqPre(部分) + phi 点积(部分)       Phase C: 同左
DualCoreSyncExchange: 把部分和写入自己的 slot
                                            SyncBarrier: 写自己的 flag，自旋读对方 flag
读对方 slot: totalSumSqPre / xHat = 双方相加   同左
ComputeGateValues: 得到完整 gate[4]
Phase D: h_in = Σ_n gate[n]·x2[n]  ← 需要对方 head 的 x2，从 workspace 读
Phase E: RMSNorm_1 → 只有 core0 写 h_in_prime   core1 不写最终输出
```

workspace 布局（每对核一段，单位 float）：

```text
[0..7]   core0 同步块: [sumSqPre, xHat[0..3], flag, pad, pad]
[8..15]  core1 同步块: 同上
[16..16+myN*D-1]       core0 的 x2（myN 个 head × D）
[16+myN*D..16+N*D-1]   core1 的 x2
每 pair 合计 16 + N*D 个 float
```

#### 4.3.3 源码精读

**pre 侧：双核判定与 phi 行对半。**

```cpp
bool needDualCore = (dimLoop_ == NUM_ONE) && (totalLength_ < coreNum_ / NUM_TWO);
coresPerToken_ = needDualCore ? NUM_TWO : NUM_ONE;
phiRowMid_ = (coresPerToken_ == NUM_TWO) ? (phiRows_ / NUM_TWO) : phiRows_;
```

[ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_tiling.cpp:L279-L287](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_host/ai_infra_mhc_pre_split_post_res_tiling.cpp#L279-L287) 给出双核启用条件：只有单 tile（D=2560）且 token 少于核数一半时才值得双核——token 充足时单核已能填满所有核，双核只添开销。`phiRowMid_` 是 phi 行的劈分点（20/2=10）。

kernel 侧的核间分工：

```cpp
int64_t groupIdx = blockIdx / coresPerToken_;   // token 组索引
int64_t localIdx = blockIdx % coresPerToken_;   // 组内核索引（0 或 1）
int64_t phiRowStart = (localIdx == NUM_ONE) ? phiRowMid_ : NUM_ZERO;
int64_t phiRowEnd   = (localIdx == NUM_ZERO) ? phiRowMid_ : phiRows_;
```

[ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res_single_tile.h:L86-L106](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res_single_tile.h#L86-L106)：blockIdx 除以/取余 coresPerToken 得到「哪个 token、哪个半区」，随后每核只遍历自己的 phi 行区间。文件头注释 [L15-L22](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res_single_tile.h#L15-L22) 画出了 core0/core1 的行段分配。注意 `ProcessOneToken` 里两核各自调 `AccumSumSq` 算的是**全量 x** 的平方和（[L139-L140](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_pre_split_post_res/op_kernel/ai_infra_mhc_pre_split_post_res_single_tile.h#L139-L140)）——这就是「冗余计算换零同步」。

**sandwich 侧：双核判定与 N 轴对半。**

```cpp
uint32_t coresPerToken = 1;
if (N >= HOST_DUAL_CORE_COUNT && dimLoop <= HOST_DUAL_CORE_COUNT
    && totalTokens <= (coreNumAiv / HOST_DUAL_CORE_COUNT)) {
    coresPerToken = HOST_DUAL_CORE_COUNT;
}
uint32_t myHeadCount = N / coresPerToken;
```

[ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_host/ai_infra_mhc_sandwich_norm_post_preonly_tiling.cpp:L157-L163](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_host/ai_infra_mhc_sandwich_norm_post_preonly_tiling.cpp#L157-L163) 是 sandwich 的启用条件（N≥2、dimLoop≤2、token 少于核数一半）；[同文件:L166-L184](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_host/ai_infra_mhc_sandwich_norm_post_preonly_tiling.cpp#L166-L184) 把 usedCoreNum 凑成偶数并用 `rowsPerCore/rowsPerCoreSp/blockPivot` 做「前 blockPivot 个单位多分一个 token」的不均分。

kernel 侧的核映射：

```cpp
coreRole_ = blockIdx % DUAL_CORE_COUNT;
uint32_t pairIdx = blockIdx / DUAL_CORE_COUNT;
headStart_ = coreRole_ * myHeadCount_;      // core0 从 head0，core1 从 head2
pairWsBase_ = (uint64_t)pairIdx * pairWsFloats;   // 本对核的 workspace 基址
```

[ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h:L75-L91](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h#L75-L91)：偶数 blockIdx 是 role0、奇数是 role1，一对核共享一段 workspace（`pairWsBase_`）；单核映射在 [L92-L104](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h#L92-L104)。

**sandwich 侧：交换与屏障。**

```cpp
// 写自己的同步块
scBuf.SetValue(0, mySumSqPre);
for (o...) scBuf.SetValue(1 + o, myXHat[o]);
DataCopy(workspaceGm_[pairWsBase_ + mySync1Slot], scBuf, ...);
```

[ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h:L20-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h#L20-L43) 的 `DualCoreSyncExchange` 只写自己的 slot；屏障本体是 sense-reversing 自旋：

```cpp
int32_t syncVal = (syncRound_ & 1) ? 0 : 1;     // 极性每轮翻转
DataCopy(syncGm_[mySlot], syncUb, WS_SYNC_BLOCK); // 写自己的 flag
while (true) {                                     // 自旋读对方
    DataCopy(syncUb, syncGm_[partSlot], WS_SYNC_BLOCK);
    if (syncUb.GetValue(0) == syncVal) break;
}
```

[同文件:L64-L95](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h#L64-L95) 的 `SyncBarrier`，源码里带有中文注释解释三步走（[L81-L91](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h#L81-L91)）。注释 [L57-L60](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h#L57-L60) 说明了动机：每个核**只写自己的 slot**、极性逐轮翻转，免掉清零阶段，也消除了跨核写引发的越界问题。屏障之后 `SyncAndReadPartner`（[L98-L119](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h#L98-L119)）读对方 slot 把 `totalSumSqPre = my + partner`、`xHat[o] = my + partner` 合成全量。

workspace 布局常量定义在 [ai_infra_mhc_sandwich_norm_post_preonly_common.h:L37-L53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_common.h#L37-L53)，注释画出了 `[0..7]/[8..15]/[16..]` 三段；host 侧按 `coreNumAiv * wsPerUnit` 预留数据区、其后紧跟同步区，并把同步区偏移 `syncGmOffsetFloats` 写进 TilingData（[tiling.cpp:L210-L237](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_host/ai_infra_mhc_sandwich_norm_post_preonly_tiling.cpp#L210-L237)），kernel 在 [kernel.h:L155-L160](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h#L155-L160) 用它把 `syncGm_` 定位到 host 预留的那段——保证核写 flag 不会砸进数据区。

**sandwich 侧：双核主流程中的调用点。**

```cpp
DualCoreSyncExchange(mySumSqPre, myXHat, mySumSqPre, xHatVals);
WriteX2ToWorkspace(tD, 0, x2Fp32, myN, myX2WsSlot);       // 把我的 x2 放进交换区
SyncAndReadPartner(mySumSqPre, myXHat, totalSumSqPre, xHatVals);  // 屏障 + 合并
ComputeGateValues(totalSumSqPre, xHatVals, gate);
```

[ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_dualcore.h:L161-L169](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_dualcore.h#L161-L169) 是双核路径唯一的同步点；Phase D 里 `ReadPartnerX2Tile` 逐 head 取回对方算好的 x2 参与 \(h_{in} = \sum_n \text{gate}_n \cdot x_2[n]\)（[同文件:L171-L189](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_dualcore.h#L171-L189)）；最后 RMSNorm_1 与最终写出只由 `coreRole_ == 0` 执行（[L198-L201](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_dualcore.h#L198-L201)），避免两核重复写同一输出。

「冗余换同步」在 sandwich 里的另一处体现是 RMSNorm_mid：[同文件:L66-L112](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_dualcore.h#L66-L112) 中每核用已在手的 x1 与 residual **把对方 head 的 x2 也重算一遍**来凑齐 N*D 全量平方和，注释明写 "redundant computation, no sync needed"。

多 tile 双核（D=5120 且 token 少）走 [ai_infra_mhc_sandwich_norm_post_preonly_dualcore_mt.h:L30-L258](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_dualcore_mt.h#L30-L258) 的 `ProcessTokenDualCoreMultiTile`：同样「每核半数 head + workspace 交换 + 屏障」，额外用 UB 槽位（复用 gamma1/gamma2 缓冲）保存两个 tile 的 x2，避免多 tile 下 x2 落 GM（文件头注释 [L11-L24](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_dualcore_mt.h#L11-L24)）。单核兜底路径在 [ai_infra_mhc_sandwich_norm_post_preonly_singlecore.h:L81-L110](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_singlecore.h#L81-L110)，按 dimLoop 选 PhaseAB_SingleTile/PhaseAB_MultiTile（文件头注释标明它是 dimLoop>2 或 N<2 的 fallback，当前规格下主要是 token 多时的主路径）。

#### 4.3.4 代码实践（调用链追踪型）

**实践目标**：画出 sandwich 双核路径一次 token 处理的跨核时序图，并标出所有 GM 读写点。

**操作步骤**：

1. 从 [kernel.h:L171-L222](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h#L171-L222) 的 `Process` 出发，注意进入 token 循环前双核模式先把 `syncGm_` 自己的 slot 清零（[L177-L187](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h#L177-L187)），并数一数 `syncRound_` 从 0 开始每 token 自增一次。
2. 沿 `ProcessTokenDualCore` 按顺序摘出每个跨 GM 的调用：`LoadTokenWeights`、`DualCoreSyncExchange`、`WriteX2ToWorkspace`、`SyncBarrier`、`SyncAndReadPartner`、`ReadPartnerX2Tile`、`WriteX2OutTile`、`WriteHInPrimeTile`。
3. 把每一步标注为「写自己 slot / 读对方 slot / 写输出 GM」三类，用两列（core0/core1）画时序。
4. 回答：如果 `SyncBarrier` 的 while 自旋被误删，哪一步会读到过期数据？

**需要观察的现象**：每个 token 恰好一对 `DualCoreSyncExchange → SyncBarrier`（在 SyncAndReadPartner 内）；`ReadPartnerX2Tile` 必须发生在屏障之后，因为它读的正是对方在屏障前写入 workspace 的 x2。

**预期结果**：一张两列时序图，屏障处有一条竖线对齐两核；第 4 步答案是——partner 的 sumSqPre/xHat/x2 可能尚未写好，合并出的 totalSumSqPre 与 xHat 错误，gate 与 h_in 全盘错误，且不报错（静默数据错）。

**待本地验证**：时序为静态分析结论，未在硬件上运行；有环境时可用 msprof 看双核时间线佐证。

#### 4.3.5 小练习与答案

**练习 1**：pre 算子双核模式为什么完全不需要屏障？

答案：它按 phi 行对半分，两核的输出段（core0 写 h_post 段 + comb 前半，core1 写 comb 后半）天然不重叠；唯一的全量依赖 invRms 被两核各自冗余计算，因此没有「等对方」的必要。

**练习 2**：sense-reversing barrier 为什么不需要在使用后把 flag 清零？

答案：期望值每轮在 0/1 间翻转（`syncVal = (syncRound_ & 1) ? 0 : 1`），下一轮等待的新值恰好不同于上一轮写入的旧值，读到的旧值不可能提前满足条件，于是省掉了清零阶段，也避免了清零本身的额外同步。

**练习 3**：sandwich 为什么把 `pairWsBase_` 设计成「一对核一段」而不是「一核一段」？

答案：一对核的两个成员（blockIdx 2k 与 2k+1）要交换的数据（两个 sync 块 + 双方的 x2）在逻辑上属于同一次 token 协作，放进同一段（16 + N*D floats）后，双方用固定的相对偏移（WS_SYNC1_CORE0/CORE1、WS_HIN_BASE + role*myN*D）互访，偏移计算无需知道全局核数。

### 4.4 v2 接口：`return_h_in_f32` 的动机

#### 4.4.1 概念说明

sandwich 算子有两套 aclnn 接口：V1 返回 `(h_in_prime, x_2_out)`；V2 增加 `return_h_in_f32` 布尔参数，为 true 时额外返回第三个张量 `h_in_f32`——数据与 h_in_prime 完全一致，但保留 float32 精度。

动机要从 kernel 内部的数据形态说起：`h_in_prime` 是 io dtype（bf16/fp16）输出，但 RMSNorm_1 归一化后的结果在 UB 里**本来就是 fp32**（`outFp32` buffer），写出前才 Cast 成低精度。若下游（例如残差累加、量化前的旁路）需要 fp32 版本，V1 时代只能从 bf16 反推，白白损失精度；而 kernel 手里现成的 fp32 值直接多写一份 GM 即可，边际成本约等于一次 MTE3 搬运。

同时 v2 修正了 v1 的一个浪费：OpDef 里 `h_in_f32` 是 REQUIRED 输出（[def.cpp:L101-L104](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_host/ai_infra_mhc_sandwich_norm_post_preonly_def.cpp#L101-L104)），v1 调用时不想要它也得给个张量占位。v2 用布尔把「要不要」显式传到 kernel，false 时 torch 层直接传空张量（转 aclTensor 后为 nullptr），kernel 侧跳过写出。

#### 4.4.2 核心流程

```text
v1: csrc 构造 2 个输出 → aclnnV1(returnHInF32 恒 false, hInF32 恒 nullptr)
    L0 层见 false → AllocTensor 占位 hInF32（分配但不写）
v2: csrc 按 return_h_in_f32 决定是否构造 h_in_f32
    → aclnnV2(returnHInF32 透传, hInF32 可为 nullptr)
    → kernel TilingData.returnHInF32 → WriteHInPrimeTile 里按需多写一份 fp32
```

#### 4.4.3 源码精读

torch 侧 v2 的按需构造：

```cpp
if (return_h_in_f32) {
    h_in_f32 = at::empty_symint({T, D}, h_out.options().dtype(c10::ScalarType::Float));
}
```

[ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp:L53-L83](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp#L53-L83) 的 `construct_output_tensors_v2`：false 时 `h_in_f32` 保持未定义（注释说明转成 aclTensor* 后就是 nullptr）；随后 [同文件:L99-L111](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp#L99-L111) 用 `EXEC_NPU_CMD_V1(aclnnAiInfraMhcSandwichNormPostPreonlyV2, ...)` 走 v2 的 aclnn 符号（EXEC_NPU_CMD_V1 机制见 u3-l2）。

aclnn 层两套 CommonProcess 结构相同，差异只在参数：V1 在 [aclnn_ai_infra_mhc_sandwich_norm_post_preonly.cpp:L71-L75](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_api/aclnn_ai_infra_mhc_sandwich_norm_post_preonly.cpp#L71-L75) 调 l0op 时硬编码 `false, ..., nullptr`；V2 在 [同文件:L124-L126](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_api/aclnn_ai_infra_mhc_sandwich_norm_post_preonly.cpp#L124-L126) 透传 `returnHInF32` 与真实的 `hInF32`。两段的其余部分（十来个 `l0op::Contiguous` 连续化 + `CREATE_EXECUTOR`）是 u2-l2 讲过的标准套路。

L0 封装层的占位逻辑：

```cpp
if (returnHInF32 == false) {
    hInF32 = executor->AllocTensor(hPost->GetDataType(), Format::FORMAT_ND, Format::FORMAT_ND);
}
```

[ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_api/ai_infra_mhc_sandwich_norm_post_preonly.cpp:L32-L34](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_api/ai_infra_mhc_sandwich_norm_post_preonly.cpp#L32-L34)：v1（returnHInF32 恒 false）每次都分配一个不会被写的占位张量——这正是 v2 要消除的浪费。随后 [同文件:L38-L42](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_api/ai_infra_mhc_sandwich_norm_post_preonly.cpp#L38-L42) 用 `ADD_TO_LAUNCHER_LIST_AICORE` 把 13 张量 + 3 属性登记进 executor 下发列表（含 `return_h_in_f32` 布尔属性）。

tiling 把布尔塞进 TilingData（`TILING_DATA_FIELD_DEF` 不支持 bool，用 int8_t），kernel 读字段后在写出处按需多写一份：

```cpp
DataCopy(hInPrimeGm_[gmOffset], hIpBf16, tD);   // 常规：cast 后的低精度输出
if (returnHInF32_) {
    DataCopy(hInF32Gm_[gmOffset], src, tD);      // 捎带：归一化后的 fp32 原值
}
```

[ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h:L160-L181](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h#L160-L181) 的 `WriteHInPrimeTile`：`src` 就是 RMSNorm_1 之后、Cast 之前的 fp32 buffer，两份输出同源，所以文档说「数据与 h_in_prime 一致，数据类型为 float」。字段定义与透传链在 [tiling.h:L61-L62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_host/ai_infra_mhc_sandwich_norm_post_preonly_tiling.h#L61-L62)、[tiling.cpp:L108-L128](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_host/ai_infra_mhc_sandwich_norm_post_preonly_tiling.cpp#L108-L128)（读属性）、[tiling.cpp:L208](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_host/ai_infra_mhc_sandwich_norm_post_preonly_tiling.cpp#L208)（写 TilingData）、[kernel.h:L65](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h#L65)（kernel 读回）。

文档侧的接口对照见 [docs/npu_mhc_sandwich_norm_post_preonly.md:L82-L98](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/docs/npu_mhc_sandwich_norm_post_preonly.md#L82-L98)（v2 原型，`*` 后关键字传参）、[L151](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/docs/npu_mhc_sandwich_norm_post_preonly.md#L151)（return_h_in_f32 参数说明「仅v2接口支持」）与 [L153-L159](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/docs/npu_mhc_sandwich_norm_post_preonly.md#L153-L159)（三返回值表）。

#### 4.4.4 代码实践（阅读 + 数值对照型）

**实践目标**：用 ST 测试理解 v2 的精度语义，并验证「h_in_f32 ≈ h_in_prime 但更准」。

**操作步骤**：

1. 阅读 [tests/st/test_ai_infra_mhc_sandwich_norm_post_preonly.py:L451-L463](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/tests/st/test_ai_infra_mhc_sandwich_norm_post_preonly.py#L451-L463) 的 `_run_and_verify_v2`：它同时取 golden（CPU fp64）、bench（NPU fp32 逐算子拼）与自定义算子三个版本，对 `h_in_f32` 单独做精度断言。
2. 找到 v2 的两个具体用例 [同文件:L531-L537](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/tests/st/test_ai_infra_mhc_sandwich_norm_post_preonly.py#L531-L537)，注意它们特意覆盖 D=2560（bf16、无 gamma_2）与 D=5120（fp16、有 gamma_2）两组，正好横跨单/多 tile 路径。
3. 有硬件时执行：`pytest tests/st/test_ai_infra_mhc_sandwich_norm_post_preonly.py -k v2 -v`；无硬件时阅读 `_golden_cpu_fp64` 与精度指标（MARE/MERE/RMSE，[L58-L96](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/tests/st/test_ai_infra_mhc_sandwich_norm_post_preonly.py#L58-L96)）理解判据。
4. 思考：为什么 h_in_f32 的精度断言阈值可以和 h_in_prime 用同一套（提示：二者数据同源，差别只在一个 Cast）。

**需要观察的现象**：v2 用例通过时，三个输出（h_in_prime、x_2_out、h_in_f32）的 NPU/CPU 误差比都在 L1 档（MARE≤5.0 等）以内。

**预期结果**：理解 v2 并非新计算，只是把既有 fp32 中间值按需导出；接口差异全部落在「参数透传 + 输出构造 + kernel 一个 if」三处。

**待本地验证**：pytest 运行需昇腾环境与已安装的 run 包/wheel 包；纯阅读部分无环境依赖。

#### 4.4.5 小练习与答案

**练习 1**：v1 调用链里 h_in_f32 占位张量发生了什么？

答案：L0 封装在 `returnHInF32 == false` 时 `executor->AllocTensor` 分配一个张量满足 OpDef 的 REQUIRED 输出，但它既不被 kernel 写、也不返回给调用方——纯粹的协议占位，v2 通过传 nullptr + TilingData 标志消除了它。

**练习 2**：为什么 TilingData 里 `returnHInF32` 用 `int8_t` 而不是 bool？

答案：`TILING_DATA_FIELD_DEF` 宏不支持 bool 类型，头文件注释明确写了这一点（[tiling.h:L62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_host/ai_infra_mhc_sandwich_norm_post_preonly_tiling.h#L62)），kernel 侧再 `!= 0` 转回布尔。这也是 host/device 序列化契约的一个小细节（u2-l3）。

**练习 3**：`WriteHInPrimeTile` 里写 h_in_f32 前后为什么要加 `SetFlag/WaitFlag` 事件对？

答案：`src` 是 TBuf 分配的 local tensor，TBuf 不像 TQue 自带队列同步，搬入搬出必须手动加 MTE3/V 流水线屏障（源码注释 [kernel_io.h:L172-L173](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel_io.h#L172-L173)）：先等 V 侧 Cast 写完 tmp 才能发 MTE3，再等 MTE3 读完 src 才允许后续 V 指令覆盖它——u2-l4 讲过的 Alloc/EnQue/DeQue/Free 之外的手工同步补充。

## 5. 综合实践

完成本讲规格中指定的对比实践，产出一张「MHC 双算子分支地图」：

1. **分支文件与选择条件对照表**。逐个打开两个算子的 op_kernel 目录，填写下表（答案框架已给出，请自行到源码里核实每个条件表达式并抄录行号）：

   | 算子 | 实现文件 | 选择条件（host 侧判定处） | 分派机制 |
   | --- | --- | --- | --- |
   | pre | `_single_tile.h`（KernelMhcSingleTile） | dimLoop==1（D=2560）；tiling.cpp L290 | TilingKey 0，入口 TILING_KEY_IS |
   | pre | `_multi_tile.h`（KernelMhcMultiTile） | dimLoop>1（D=5120） | TilingKey 1 |
   | sandwich | `_dualcore.h` | coresPerToken==2 且 dimLoop==1；tiling.cpp L157-L162 | 运行期 if（kernel.h L209-L216） |
   | sandwich | `_dualcore_mt.h` | coresPerToken==2 且 dimLoop>1 | 运行期 if |
   | sandwich | `_singlecore.h` | 其余（token 多，或 N<2/dimLoop>2） | 运行期 if |

2. **公式对应关系说明**。用 4.1 的公式完成映射：pre 输出的 \(h_{post}\) 对应 sandwich 阶段 2 的系数项 \(\text{h\_post}_{(b,s,n)}\)；\(h_{res}\) 的 \([N,N]\) 展开对应 \(\text{h\_res}_{(b,s,j,n)}\)；写出 `x2[n,d] = h_post[n]·x1[d] + Σ_j h_res[j,n]·residual[j,d]`，并注明 kernel 实现函数是 `ComputeX2MyHeads`。
3. **双核同步清单**。为两个算子分别列出「双核时交换了什么、冗余算了什么、用了什么同步原语」，各写三行。
4. **（有硬件时）端到端验证**：先装 run 包与 wheel 包（顺序见 u1-l2/u1-l4），跑 `pytest ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/tests/st/test_ai_infra_mhc_sandwich_norm_post_preonly.py -k 'l0 or v2' -v`，观察 D=2560 与 D=5120 用例分别命中 dualcore 与 dualcore_mt 路径（可通过 tiling 日志或 msprof 核对 blockDim/核数）；无硬件时以本表 + 时序图为交付物，标注「待本地验证」。

## 6. 本讲小结

- MHC 推理由一对算子构成：pre 算子从 x 算出每 token 的 20 个系数（h_post 4 个 + h_res 16 个），sandwich 算子把它们与 h_out/residual 一起完成 RMSNorm_0 → MHC_Post → RMSNorm_mid（可选）→ MHC_Pre → RMSNorm_1 五阶段融合。
- tile 切分的统一规则是 `dimTile = 对齐128(min(D,2560))`、`dimLoop = ceil(D/dimTile)`；pre 把 dimLoop 映射成 TilingKey 走编译期分派（single_tile/multi_tile 两份二进制），sandwich 写死 key 0、靠 TilingData 字段运行期分派（singlecore/dualcore/dualcore_mt 三路）。
- 双核协同（token 少于核数一半时启用）有两种代价模型：pre 按 phi 行对半、invRms 两核冗余算、零同步；sandwich 按 N 轴 head 对半、必须经 GM workspace 交换 sumSqPre/xHat/x2，并用「只写自己 slot + 极性翻转」的 sense-reversing 自旋屏障对齐。
- sandwich 的 RMSNorm_mid 延续了同一哲学：每核冗余重算对方 head 的 x2 平方和，用重复计算避免一次跨核同步。
- v2 接口的 `return_h_in_f32` 是一次低成本的接口演进：把 kernel 内现成的 fp32 归一化结果按需多写一份 GM，同时用「布尔透传 + nullptr 输出」替代 v1 在 L0 层分配占位张量的浪费。
- 阅读这对算子的通用方法论：先在 docs 找公式，再在 tiling.cpp 找「分支判定」，最后沿 kernel 类的 Process 找分支实现——三个位置对得上，分支地图就完整了。

## 7. 下一步学习建议

- 下一讲 u4-l7 转向 posembedding 族（KV RMSNorm+RoPE Cache 与旋转位置编码），那里的 kernel 变体（b16/pa/nz/mtp/quant/arch35）比本讲的 3 条分支多一个数量级，是「模式组合 → kernel 变体」更极端的样本。
- 想继续深挖双核协同，可预习 u5-l2（AIV/AIC 协同与 FlashDecode）：本讲的同步是同构 AIV 之间的 GM 自旋屏障，u5-l2 会看到 Cube 核与 Vector 核之间的 CrossCoreSetFlag 等跨核原语。
- 若对「冗余计算 vs 同步」的取舍感兴趣，建议回到 [ai_infra_mhc_sandwich_norm_post_preonly_dualcore.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_dualcore.h#L66-L112) 的 RMSNorm_mid 段，数一数它比「交换方案」多做了多少次 Mul/Axpy，再对照 GM 一次往返的延迟量级做估算。
- 测试角度可提前浏览 u6-l2：本讲的 ST 用例（l0_eager/l0_graph/v2）就是按那里讲的「resources marker + NPU/CPU 对拍」框架写的。
