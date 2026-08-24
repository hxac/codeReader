# ops-nn 的 AiInfraMatmul：复用公共算子基建

## 1. 本讲目标

本讲是进阶篇（Unit 4）的收官之讲。前面几讲我们逐个拆解了 transformer 类算子，本讲换一个视角：**AiInfraMatmul 不是"又一个独立算子"，而是一套被多个上层入口共享的"矩阵乘基建"**。学完本讲，你应该能够：

1. 梳理 matmul 公共库的复用层次：`aclnn` 图分发层 → `l0op` 薄封装层 → 共享的 `AiInfraMatmul` 算子，并说清 `matmul.cpp`、`matmul_util.cpp`、`batch_matmul.cpp` 等 7 个公共文件各自的角色。
2. 理解 `simplified_key` 的拼接规则，以及它与 `ai_infra_matmul_binary.json`（编译期预编译二进制选择）、`runtime_kb.json`（运行期调优知识库）在**编译期/运行期**各自的作用。
3. 说明 L2 cache 优化的动机与实现：host 侧 `DoL2CacheTiling` 如何计算 L2 分块、`l2CacheFlag` 如何在 kernel 侧变成 `SetL2CacheHint`。
4. 说明 ND2NZ 格式转换 kernel 的设计动机：为什么 Cube 核偏爱 FRACTAL_NZ 排布、`MatrixAtoNZV2` 的三分支决策树、`KernelND2NZMM` 的双缓冲设计，以及 AIC/AIV 真并行（CVP）路径。
5. 对比 ops-nn 与 ops-transformer 两套 `common` 目录中 `tiling_base` 的差异，理解"同一套七步框架、两套平行实现"的组织方式。

本讲依赖 u2-l2（aclnn 两段式接口）和 u2-l3（Tiling 七步框架）的内容，相关基础不再重复展开。

## 2. 前置知识

- **aclnn 两段式接口**（u2-l2）：`aclnnXxxGetWorkspaceSize`（host 同步段：参数检查、构图、算 workspace）+ `aclnnXxx`（异步执行段，内部走 `CommonOpExecutorRun` 下发）。本讲的 `aclnnAiInfraMatmul` 仍是这个骨架，但 host 段里"构图"变成了一张**可组合的图**。
- **Tiling 七步框架**（u2-l3）：`GetShapeAttrsInfo → GetPlatformInfo → IsCapable → DoOpTiling → DoLibApiTiling → GetWorkspaceSize → PostTiling`，最后 `SetTilingKey`。本讲会看到 ops-nn 版 `TilingBaseClass` 在此骨架上多出的东西（`DumpTilingInfo`、tiling 缓存、调优知识库挂点）。
- **Cube 与 Vector 核**：Ascend AI Core 上有两类核。Cube 核（AIC）做矩阵乘，Vector 核（AIV）做逐元素/搬运类计算。矩阵乘的输入输出是大规模数据搬运，所以经常 AIC 算、AIV 搬，两者需要同步。
- **ND 与 FRACTAL_NZ 格式**：ND（N-Dimensional）是普通行主序排布；FRACTAL_NZ 是 Cube 单元更喜欢的分块排布（按 `c0` 列小块为单位重排，本仓库 fp16 时 `c0 = 16`）。host 收到 ND 输入时，可能需要在 device 上先做一次 ND→NZ 转换，这就是 ND2NZ。
- **L2 cache**：AI Core 与 DDR 之间的大容量片上缓存（数百 MB 量级，具体由平台信息给出）。矩阵乘的 A/B 矩阵会被多个核、多轮迭代反复读取，如果能常驻 L2，就能大幅减少 DDR 带宽压力。
- **哈希与缓存**：tiling 计算代价不低，同样的 (m, k, n, dtype...) 组合没必要每次重算。用哈希（本仓库用 MurmurHash）把输入特征映射成 32 位 key，再查缓存即可复用结果。

## 3. 本讲源码地图

以下路径均相对 `inference/` 目录（下同）。永久链接 base 为 `https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/`。

### 3.1 算子主目录（`ascendc/src/ops-nn/matmul/ai_infra_matmul/`）

| 文件 | 作用 |
| --- | --- |
| `README.md` | 算子说明：产品支持矩阵、公式 `C = op(A) @ op(B) + bias`、各 SoC 的格式/精度约束、对外 aclnn 接口清单 |
| `op_api/aclnn_ai_infra_matmul.cpp` | aclnn 入口：8 个 Graph 分发类 + 工厂 `CreateMatmulGraphImpl` + 两段式接口 |
| `op_host/ai_infra_matmul_def.cpp` | OpDef：输入/输出/属性定义，`AddConfig` 指定支持的 SoC |
| `op_host/ai_infra_matmul_tiling.cpp` | tiling 注册：`REGISTER_TILING_TEMPLATE` + `IMPL_OP_OPTILING`（挂 `GenSimplifiedKey`、`TilingParse`） |
| `op_host/ai_infra_matmul_base_tiling.h/.cpp` | 七步 tiling 主实现（2000+ 行核心逻辑：基本 tiling、L1 fullload、L2 cache、ND2NZ tiling、tiling key） |
| `op_host/ai_infra_matmul_simplifiedkey.h` | `GenSimplifiedKey`：拼接 simplified key 字符串 |
| `op_host/ai_infra_matmul_l2_cache.cpp` | `L2Cache` 类：计算 `l2CacheFlag` 位标志 |
| `op_host/ai_infra_matmul_tuning.h` | 调优参数结构（与 runtime_kb.json 的 `knowledge` 字段一一对应） |
| `op_host/config/ascend910b/ai_infra_matmul_binary.json` | 编译期二进制清单：每个 `simplified_key` 对应一个预编译 bin 文件名 |
| `op_host/config/ascend910b/*_runtime_kb.json` | 运行期调优知识库（JSON-lines，910B1/B2/B2C/B3/B4 共 5 份） |
| `op_kernel/ai_infra_matmul.cpp` | kernel 入口：模板参数分发（`if constexpr` 链） |
| `op_kernel/arch32/ai_infra_matmul_tiling_key.h` | 模板 TilingKey 声明（`ASCENDC_TPL_ARGS_DECL` / `ASCENDC_TPL_SEL`） |
| `op_kernel/arch32/ai_infra_matmul_tiling_data.h` | `AiInfraMatmulTilingData` 结构（4 个子结构 + ND2NZ 对齐参数） |
| `op_kernel/arch32/ai_infra_matmul_base_kernel.h` | 基础 matmul kernel（L2 tile 循环、蛇形 n 序） |
| `op_kernel/arch32/ai_infra_matmul_cvp_base_kernel.h` | AIC/AIV 真并行 kernel（AIV 路径做 ND2NZ） |
| `op_kernel/arch32/ai_infra_matmul_nd2nz.h` | ND2NZ 工具函数：`MatrixAtoNZV2` / `MatrixBtoNZV2` / `MatrixtoNZ` / `CopyPadNd2Nz` |
| `op_kernel/arch32/ai_infra_matmul_nd2nz_kernel.h` | `KernelND2NZMM`：带双缓冲的 ND2NZ 转换 kernel |
| `CMakeLists.txt` | 编译目标划分（op_host_aclnnInner / opsproto / opapi / optiling / opmaster_ct） |
| `tests/st/test_ai_infra_matmul.py` | ST 测试：`torch.ops.custom.npu_ai_infra_matmul` 对拍 `torch.matmul` |

### 3.2 公共复用层

| 文件 | 作用 |
| --- | --- |
| `ascendc/src/ops-nn/matmul/common/op_host/op_api/matmul.cpp` | `l0op` 层：`AiInfraMatmulCommon`（INFER_SHAPE + ADD_TO_LAUNCHER_LIST_AICORE）及 10+ 个薄封装（按输出 dtype/format 区分） |
| `ascendc/src/ops-nn/matmul/common/op_host/op_api/matmul_util.cpp` | `GetMatMulOp`：按输入 dtype/format/bias 选具体 l0op 封装；`CheckStreamKSKTiling` 等 stream-k 启发式 |
| `ascendc/src/ops-nn/matmul/common/op_host/op_tiling/tiling_cache.h` | `TilingCache<HashInput, HashItem>` 模板：上限 500 条、读写锁、哈希冲突校验 |
| `ascendc/src/ops-nn/matmul/common/op_host/op_tiling/hash.cpp` | `MurmurHash`：32 位哈希实现 |
| `ascendc/src/ops-nn/common/inc/op_host/tiling_base.h` | ops-nn 版 `TilingBaseClass`（命名空间 `Ops::NN::Optiling`） |
| `ascendc/src/ops-nn/common/src/op_host/op_cache_tiling.cpp` | `TilingPrepareForOpCache`：dlopen 桥接 legacy 公共库 |
| `ascendc/src/ops-nn/common/src/op_host/runtime_kb_api.cpp` | `QueryBank`：dlopen 桥接 `LegacyQueryBank`（runtime_kb.json 的查询引擎在本仓库之外） |
| `ascendc/src/ops-transformer/common/include/tiling_base/tiling_base.h` | 对照组：transformer 版 `TilingBaseClass`（命名空间 `Ops::Transformer::OpTiling`） |

### 3.3 调用链总览

```text
torch.ops.custom.npu_ai_infra_matmul(a, b)          # tests/st/test_ai_infra_matmul.py
  └─ aclnnAiInfraMatmulGetWorkspaceSize              # op_api/aclnn_ai_infra_matmul.cpp
       ├─ CheckInputParams                           #   4 步参数检查
       ├─ CreateMatmulGraphImpl(dim1, dim2)          #   按维度组合选 Graph 类（8 分支）
       └─ matmulGraph->Execute()                     #   图执行
            └─ MatmulProcess → GetMatMulOp           #   common/op_host/op_api/matmul_util.cpp
                 └─ l0op::AiInfraMatmulNd / ...      #   common/op_host/op_api/matmul.cpp
                      └─ AiInfraMatmulCommon
                           ├─ INFER_SHAPE(AiInfraMatmul, ...)
                           └─ ADD_TO_LAUNCHER_LIST_AICORE(AiInfraMatmul, ...)
                                │  （进入算子下发流程：host tiling → 选 kernel）
                                ├─ GenSimplifiedKey                # 编译期：匹配 binary.json 选预编译二进制
                                ├─ AiInfraMatmulTilingFunc        # host tiling
                                │    └─ AiInfraMatmulBaseTiling（七步）
                                │         ├─ DoBasicTiling / DoSelectTiling
                                │         ├─ DoL2CacheTiling       # L2 分块 + l2CacheFlag
                                │         ├─ DoNd2NzVectorTiling   # baseAN/AD/BN/BD
                                │         └─ DoTilingKey → GET_TPL_TILING_KEY
                                └─ ai_infra_matmul<LOADMODE, SPLITCOREMODE, FIXOPTI,
                                    MIXND2NZ, SPECIALOPT, FP32ADDMM>   # kernel 入口
                                     ├─ AiInfraMatmulBaseKernel            # 基础路径（AIC）
                                     ├─ MatmulBaseKernelAL1/BL1FullLoad    # L1 常驻路径
                                     └─ AiInfraMatmulCvpBaseKernel         # AIC+AIV 真并行
                                          └─ AivProcess → Nd2nzVnchwMM      # ND2NZ 转换
```
