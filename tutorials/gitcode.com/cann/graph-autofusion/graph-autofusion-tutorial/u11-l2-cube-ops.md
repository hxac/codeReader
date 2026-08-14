# Cube 类算子（matmul/conv2d）与 cv 融合

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 cube（矩阵乘/卷积）类算子与 vector（逐元素）类算子在设备端 API 形态上的本质差异：模板参数化入口、tiling data 结构驱动、L1/L0 多级存储参与调度。
2. 读懂 `batch_matmul.h`、`matmul.h`、`conv2d.h` 三份设备端入口源码，理解「tiling key 模板参数 + `if constexpr` 分支」如何把一份源码展开成多个 kernel 变体。
3. 掌握 CV（Cube-Vector）融合的关键改动：cv tiling wrapper 复用编译（编译期内容寻址缓存 + 运行期 tiling 结果缓存）与 dtype 感知的对齐机制。
4. 了解 v35 平台专属的 optimize/codegen 适配方式：`REG_ASC_IR(MatMul)` 的 v2 注册、`CallCubeTiling` host 函数的生成链路。

## 2. 前置知识

在阅读本讲之前，你需要先建立以下几个直觉（已在前置讲义中铺垫，这里做最小回顾）：

- **Cube 与 Vector 的分工**：昇腾 AI Core 上，矩阵乘（matmul）和卷积（conv2d）由 **Cube 单元**（矩阵乘法器，数据流经 L1 → L0A/L0B → L0C 多级片上缓冲）执行；Add/Exp/Reduce 等逐元素算子由 **Vector 单元**（数据在 UB 上进出）执行。两套单元的存储层次和调度模型完全不同，因此 cube 算子不能直接复用 vector 算子那套「UB tiling + repeat」的代码生成路径。
- **CV 融合（Cube-Vector Fusion）**：把一个 cube 算子（如 MatMul）与紧邻的 vector 算子（如 Cast、elementwise）编进同一个 kernel，让 MatMul 的 L0C 输出经 Fixpipe 直接进 UB，vector 段在 UB 上继续算，省掉一次全局内存往返。设备端以 `CV_UB_FUSION` 编译宏区分融合与非融合两种入口签名。
- **tiling data**：host 侧算好、device 侧只读的切分参数块（block 维度、baseM/baseN、循环次数等）。cube 算子的 tiling 由 CANN 原生 op_host 库（如 `MatMulV3` 的 tiling 函数）计算，Autofuse 不重写这套算法，而是「借用」它——这是本讲 4.3 节的主线。
- **tiling key**：同一个算子按「API 等级 / 转置 / full-load / L0C2OUT 模式」等维度组合出的整型编号，用于区分同一份模板源码展开出的不同 kernel 变体。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `autofuse/v35/ascendc/api_cube/matmul.h` | MatMulV3 设备端入口：模板参数化的 `mat_mul_v3` kernel 与十余个 `if constexpr` 变体分支 |
| `autofuse/v35/ascendc/api_cube/batch_matmul.h` | BatchMatMulV3 设备端入口：批处理版 `batch_mat_mul_v3` kernel，含 batch 专属迭代模型分支 |
| `autofuse/v35/ascendc/api_cube/conv2d.h` | Conv2D v2 设备端入口：NCHW→CI1KHKWCOCI0 布局的卷积 kernel 封装 |
| `autofuse/v35/ascendc/api_cube/matmul/`、`conv2d/` | tiling key 头与 include 汇总头（如 `mat_mul_tiling_key.h`、`conv2d_v2_tilingkey_cv.h`） |
| `autofuse/codegen/codegen_tiling_cube_wrapper.h` | CV tiling wrapper 的「接口 + 实现」源码字符串常量（本次重写的核心文件） |
| `autofuse/codegen/codegen_tiling_cube.cpp` | 生成 `CallCubeTiling` host 函数，并把 wrapper 源码拼进 tiling 产物 |
| `autofuse/compiler/python/ascendc_compile.py` | Python 编译编排：cv wrapper 共享 so 的内容寻址缓存、文件锁与链接注入 |
| `autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp` | v35 平台 ASCIR 算子注册：MatMul/MatMulBias 等的 v2 实现绑定 |

## 4. 核心概念与源码讲解

### 4.1 cube 算子 API：模板参数化的设备端入口

#### 4.1.1 概念说明

vector 算子的设备端封装（见 u5-l3）是「一个 `template + inline __aicore__` 函数，按 dtype 与 (m,k) 分块」；cube 算子则完全不同：它本身就是一个完整的 `__global__ __aicore__` **kernel 入口**（而不是被别的 kernel 调用的内联函数），并且把所有编译期决策维度编码成 **int8_t 模板参数**。这样一份源码经过不同模板实参实例化，就能展开出一族 kernel，每个实例对应一个 tiling key，host 侧按 tiling 结果挑选加载哪个实例。

为什么这样设计？因为 cube kernel 的差异维度（是否转置、API 高低级、full-load 模式、L0C2OUT 搬运方式、batch 迭代模型）是**编译期布局差异**——它们改变 `MatmulType` 的模板实参和 L1/L0 的 ping-pong 结构，无法像 vector 算子那样用运行期参数兜住。

#### 4.1.2 核心流程

以 `batch_mat_mul_v3` 为例，设备端执行的骨架是：

```text
kernel 入口(aGM, bGM, biasGM, offsetWGM, cGM, workspaceGM, tilingGM [, param])
  ├─ GetUserWorkspace(workspaceGM)          // 取用户 workspace
  ├─ 由模板参数 BATCH_A_TRANS/B_TRANS 推导 aLayout/bLayout（编译期）
  ├─ REGISTER_TILING_DEFAULT(BatchMatMulV3TilingData)
  ├─ 按 7 个 int8_t 模板参数的组合，if constexpr 选中一个分支：
  │     ├─ 取出对应结构的 tiling data（GET_TILING_DATA_WITH_STRUCT）
  │     ├─ 组装 MatmulType<GM, format, DTYPE, isTranspose> 类型
  │     └─ 构造 kernel 对象 → op.Init(...) → op.Process()
  └─ （CV_UB_FUSION 时额外传入 AutoFusionVector::Params*，衔接后续 vector 段）
```

#### 4.1.3 源码精读

先看入口签名与模板参数。[batch_matmul.h:L135-L143](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_cube/batch_matmul.h#L135-L143) 中，`batch_mat_mul_v3` 接收 7 个 int8_t 模板参数（API 等级、A/B 转置、迭代模型、模式、full-load、L0C2OUT 模式），并且在 `CV_UB_FUSION` 宏打开时多收一个 `AutoFusionVector::Params *param`——这正是 CV 融合时 vector 段与 cube 段共享参数的通道：

```cpp
template <int8_t BATCH_API_LEVEL, int8_t BATCH_A_TRANS, int8_t BATCH_B_TRANS, int8_t BATCH_ITER_MODEL, int8_t BMODEL,
          int8_t BATCH_FULL_LOAD, int8_t BATCH_L0C2OUT_MODEL>
__global__ __aicore__ void batch_mat_mul_v3(
#ifdef CV_UB_FUSION
    GM_ADDR aGM, GM_ADDR bGM, GM_ADDR biasGM, GM_ADDR offsetWGM, GM_ADDR cGM, GM_ADDR workspaceGM, GM_ADDR tilingGM,
    AutoFusionVector::Params *param)
#else
    GM_ADDR aGM, GM_ADDR bGM, ..., GM_ADDR tilingGM)
#endif
```

接着 [batch_matmul.h:L156-L160](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_cube/batch_matmul.h#L156-L160) 注册默认 tiling 结构，并在**非** CV 融合时把 kernel 标记为 `KERNEL_TYPE_AIC_ONLY`（纯 Cube kernel）；CV 融合时不打这个标记，因为融合 kernel 同时含 AIC 与 AIV 两段：

```cpp
REGISTER_TILING_DEFAULT(BatchMatMulV3TilingData);
#if (defined(CV_UB_FUSION) || defined(CV_SAFETY_FUSION))
#else
KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIC_ONLY);
#endif
```

分支选择主体在 [batch_matmul.h:L162-L251](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_cube/batch_matmul.h#L162-L251)。每个 `if constexpr` 用模板参数组合匹配一种实现，例如最普通的 batch 场景落到 `MatMulActKernel`（[L197-L207](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_cube/batch_matmul.h#L197-L207)），注意它在 `CV_UB_FUSION` 时把 `param` 一并传给 kernel：

```cpp
GET_TILING_DATA_WITH_STRUCT(BatchMatMulV3BasicTilingData, tilingData, tilingGM);
MatmulV3Advanced::MatMulActKernel<DTYPE_X1, DTYPE_X2, DTYPE_Y, DTYPE_BIAS, aLayout, bLayout, layout::RowMajor, 0,
                                  OP_TYPE_RELU_VALUE>(
#ifdef CV_UB_FUSION
    aGM, bGM, biasGM, cGM, workspaceGM, tilingData.matMulTilingData, param, tilingData.batchDimAll);
#else
    aGM, bGM, biasGM, cGM, workspaceGM, tilingData.matMulTilingData, tilingData.batchDimAll);
#endif
```

而 high-level 场景（[batch_matmul.h:L76-L87](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/v35/ascendc/api_cube/batch_matmul.h#L76-L87) 的 `BMMV3_IMPL_CLASS_TRANS` 宏）走的是另一条路：用 `MatmulType<GM, format, DTYPE, trans>` 组装类型后实例化 `BatchMatMulAswKernel`，`op.Init(...)` + `op.Process()` 两段式执行。文件里的中文注释（[L75](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_cube/batch_matmul.h#L75)、[L22](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_cube/batch_matmul.h#L22)）明确标注「和 act 模板代码有差异，注意保留 GET_TILING_DATA_WITH_STRUCT / 注意不要 DTYPE_BIAS」——这是维护时最容易踩的两处差异点。

非 batch 的 `mat_mul_v3` 结构相同但维度更多：[matmul.h:L81-L104](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_cube/matmul.h#L81-L104) 是入口与 tiling 注册，[matmul.h:L106-L198](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_cube/matmul.h#L106-L198) 按api_LEVEL/MODEL/FULL_LOAD/L0C2OUT_MODEL 组合出十余个分支：`MatMulActKernel`（基础）、`MatMulStreamKActKernel`（Stream-K 多核切分）、`MatMulFixpipeOptiActKernel`（Fixpipe 优化搬运）、`MatMulInputKEqZeroClearOutput`（K=0 清零）等。此外 [matmul.h:L64-L80](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_cube/matmul.h#L64-L80) 的 `MMV3_IMPL_CLASS_TRANS` 宏还处理了 `TILINGDATA_SPLIT_NUM` 场景下按 block 偏移读取 tiling data 的逻辑。

conv2d 的封装则薄得多。[conv2d.h:L21-L46](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascendc/api_cube/conv2d.h#L21-L46) 定义了四个 layout（输入输出 NCHW、权重 `CI1KHKWCOCI0` 分形布局），然后模板参数（FmapTiling、L1/L0 PingPong、OutputOrder 等 12 个）直接透传给 `Conv2DV2Advanced::ConvActKernel`：

```cpp
using aLayout = Atcos::Conv::layout::NCHW;
using bLayout = Atcos::Conv::layout::CI1KHKWCOCI0;
...
REGISTER_TILING_DEFAULT(Conv2DTilingData);
GET_TILING_DATA_WITH_STRUCT(Conv2DTilingData, tilingData, tilingGM);
Conv2DV2Advanced::ConvActKernel<DTYPE_X1, DTYPE_X2, DTYPE_Y, DTYPE_BIAS, aLayout, bLayout, cLayout, biasLayout,
                                A_FULL_LOAD_MODE>(...);
```

可以看到：conv2d 的切分策略全部编码在模板参数与 tiling data 里，源文件本身只做「布局定义 + 委托」。

#### 4.1.4 代码实践

**实践目标**：通过对比源码，亲手归纳 cube 与 vector 设备端 API 的差异清单。

**操作步骤**：

1. 打开 `autofuse/v35/ascendc/api_cube/matmul.h`，数一数 `mat_mul_v3` 有多少个 `} else if constexpr (` 分支，记下每个分支选中的 kernel 类名。
2. 打开 u5-l3 讲过的 `autofuse/ascendc/api/reduce.h`（vector 算子代表），对比两者的函数形态：谁是 `__global__` kernel、谁是内联函数；谁的参数里有 `tilingGM`。
3. 在 `autofuse/v35/ascendc/api_cube/matmul/mat_mul_tiling_key.h` 中找到 tiling key 与模板参数组合的对应关系。

**需要观察的现象**：vector 封装是「被调用者」（由 Autofuse 生成的 kernel 主函数调用），cube 封装是「kernel 本身」（host 直接下发）；cube 入口必带 `tilingGM` 且用 `GET_TILING_DATA_WITH_STRUCT` 解析，vector 封装的切分参数则由生成代码以局部变量传入。

**预期结果**：得到一张三列对照表（维度：vector 封装 / cube 入口），至少覆盖「函数角色、参数来源、编译期差异表达方式、存储层次」四行。本实践为源码阅读型，无需上板，观察结论可直接从源码推出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 cube 算子用 int8_t 模板参数 + `if constexpr` 而不是像 vector 算子那样用运行期参数区分场景？

**答案**：因为这些维度（转置、full-load、L0C2OUT 模式等）会改变 `MatmulType` 模板实参与 L1/L0 ping-pong 结构等**编译期布局**，C++ 模板必须在编译期实例化；运行期参数无法表达不同实例的代码结构差异。同时每个实例对应一个 tiling key，host 侧按 tiling 结果选择加载哪个实例。

**练习 2**：`CV_UB_FUSION` 宏在 `batch_matmul.h` 中改变了哪三处行为？

**答案**：(1) kernel 入口多收一个 `AutoFusionVector::Params *param`；(2) 不再打 `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIC_ONLY)` 标记（融合 kernel 是 AIC+AIV 混合）；(3) `MatMulActKernel` 调用多传 `param`，使 cube 段产出能衔接 UB 上的 vector 段。

**练习 3**：conv2d.h 中 `bLayout = CI1KHKWCOCI0` 这种「怪异」布局是什么？

**答案**：卷积权重在 Cube 单元上的分形（fractal）排布：把 `C_in×K_H×K_w×C_out` 的逻辑权重按 Cube 硬件的矩阵分块要求重排（CI1/KHKW/CO 维度交错），使卷积能退化成矩阵乘法在 Cube 上执行。这是 cube 算子特有的格式概念，vector 算子的 UB 数据只需 ND 排布。

### 4.2 matmul/conv2d 的 tiling：借用 CANN op_host 而非重写

#### 4.2.1 概念说明

vector 算子的 tiling 由 Autofuse 自己的 ATT（u7 系列）求解；但 MatMulV3/Conv2D 的 tiling 算法极复杂（涉及 L1/L0A/L0B/L0C 容量、ping-pong、Stream-K 切分、batch 折叠），Autofuse 选择了**复用策略**：在 host 侧直接调用 CANN 原生 op_host 库中已注册的 tiling 函数（`MatMulV3`/`BatchMatMulV3` 的 `tiling` 与 `tiling_parse`），把结果（tiling_data 字节流、tiling_key、block_dim、workspace 大小）取回来供融合 kernel 使用。

这正是 `codegen_tiling_cube_wrapper.h` 存在的意义：它是一份**由 Autofuse 生成、编进融合 kernel host 侧的 wrapper 源码**，充当「Autofuse 的融合 kernel」与「CANN op_host tiling 库」之间的桥梁。

#### 4.2.2 核心流程

```text
DoMatMulTiling(compile_info, inputs, outputs, attrs, is_batch)
  ├─ ValidateTilingRequest        // 指针非空、输入 2~4 个、输出非空
  ├─ ReadMatMulAttrs              // 读 transpose/offset_x/hf32/autofuse_has_bias 等属性
  ├─ IsSupportedTilingTensorDesc  // dtype/format 白名单（float/fp16/bf16 + ND）
  └─ RunSharedCubeTiling
       ├─ MakeRuntimeTilingKey → TryGetCachedTilingResult   // ① 运行期结果缓存命中即返回
       ├─ GetOpHostFuncs(std::call_once)                    // ② 从 op_impl registry 取 tiling/tiling_parse 函数（一次性）
       ├─ GetCompileState(CompileStateKey → cache)          // ③ 编译期状态缓存：compile_json + tiling_parse 结果
       ├─ thread_local TilingScratch 复用缓冲
       ├─ BuildTilingContext → funcs.tiling(tiling_ctx)     // ④ 真正调用 CANN tiling 函数
       └─ FillTilingResultFromContext + CacheTilingResult   // ⑤ 回填并写缓存
```

三层缓存（①⑤ 运行期 tiling 结果、② host 函数表、③ 编译状态）共同保证了：同一个 (soc, dtype, shape, attrs) 组合的 MatMul tiling 只真正计算一次。

#### 4.2.3 源码精读

[codegen_tiling_cube_wrapper.h:L3-L113](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_cube_wrapper.h#L3-L113) 是 wrapper 的**接口半边**，以原始字符串常量 `kCubeKernelTilingWrapperHppValue` 形式存放。它定义了四个通信结构（`TensorInfo`/`AttrInfo`/`CompileInfo`/`TilingResult`）和一个纯接口类 `CubeKernelTilingWrapper`——注意重写后这里只剩声明，实现体整体搬到了 [L115-L1165](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_cube_wrapper.h#L115-L1165) 的 `kCubeKernelTilingWrapperCppValue`。这个「接口/实现分离成两个字符串常量」的形态正是复用编译的前提：接口稳定、实现可独立编成共享库。

`TilingResult`（[codegen_tiling_cube_wrapper.h:L66-L80](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_cube_wrapper.h#L66-L80)）是回传给融合 kernel 的全部产物：tiling 字节流之外，还提炼出 `cube_used_core_num`/`cube_base_m`/`cube_base_n` 三个元信息——它们正是下游 vector 段做 **dtype 感知对齐**（4.3 节）时需要的 cube 输出块尺寸。

核心执行函数 `RunSharedCubeTiling` 在 [codegen_tiling_cube_wrapper.h:L1001-L1074](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_cube_wrapper.h#L1001-L1074)：先查运行期缓存，未命中则取函数表、取编译状态、在 thread_local scratch 上构建 `gert::Tensor` 输入输出描述，最后经 `BuildTilingContext` 调 `funcs.tiling(tiling_ctx)` 并缓存结果。运行期缓存键 `RuntimeTilingKey`（[L178-L216](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_cube_wrapper.h#L178-L216)）覆盖 soc/device/op_type/dtype/format/全部输入输出 shape/bias 与转置属性/核数共 25 个字段，保证缓存键与 tiling 输入一一对应。

`MakeCubeCompileJson`（[codegen_tiling_cube_wrapper.h:L560-L604](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_cube_wrapper.h#L560-L604)）则从 `fe::PlatFormInfos` 抽取 L1/L0A/L0B/L0C/UB 容量、核数与 intrinsic 能力，拼成 CANN tiling 函数期望的 compile json——即「把平台信息翻译给原生 tiling 算法」。

生成侧，[codegen_tiling_cube.cpp:L518-L527](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_cube.cpp#L518-L527) 把这两个字符串常量连同 `CallCubeTiling` host 函数一起装进「文件名 → 内容」映射返回，`CallCubeTiling` 的签名里显式带出 `basem`/`basen`/`tiling_key`/`CVAutofuseTilingData *tiling_data`，供 Inductor 侧的 host 编排调用（对应 [codegen_tiling.h:L204-L208](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.h#L204-L208) 的 `GenCallCubeTilingForInductor` 与缓存读写钩子）。

#### 4.2.4 代码实践

**实践目标**：追踪一次 CV 融合场景下 MatMul tiling 的完整借用链路。

**操作步骤**：

1. 从 `autofuse/codegen/codegen_tiling.h` 的 `GenCallCubeTilingForInductor` 声明出发，在 `codegen_tiling_cube.cpp` 中找到其实现，确认它生成的 `CallCubeTiling` C 函数内部最终会构造 `CubeKernelTilingWrapper` 并调用 `DoMatMulTiling`。
2. 在 `codegen_tiling_cube_wrapper.h` 中按顺序定位：`ValidateTilingRequest` → `ReadMatMulAttrs` → `RunSharedCubeTiling` → `FillTilingMeta`，画出调用链。
3. 阅读 `autofuse/tests/ut/codegen/test_codegen_tiling.cpp` 中与 CubeTilingWrapper 相关的测试断言，确认生成产物中包含 wrapper 头/源两个文件名。

**需要观察的现象**：tiling 借用链路上共出现几层缓存、各自的 key 是什么；`TilingResult` 里哪些字段是给 vector 段对齐用的。

**预期结果**：一张包含「Python 编排 → CallCubeTiling → CubeKernelTilingWrapper::DoMatMulTiling → CANN op_host tiling → TilingResult」的调用链图，并标注三层缓存位置。测试断言部分待本地验证（需构建环境编译 UT）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Autofuse 不像 vector 算子那样用 ATT 求解 MatMul 的 tiling？

**答案**：MatMul tiling 涉及 L1/L0A/L0B/L0C 多级存储容量、ping-pong、Stream-K、batch 折叠等复杂约束，CANN op_host 库已有成熟实现且随版本演进；重写成本与维护风险都高。Autofuse 的定位是融合编译器，只需「取回」tiling 结果（尤其 baseM/baseN/核数）来衔接 vector 段，因此选择复用而非重写。

**练习 2**：`RuntimeTilingKey` 为什么要把 25 个字段全部纳入？

**答案**：tiling 函数是这些输入的纯函数：任一字段（如 has_bias、transpose_x1、aiv_num）变化都可能改变 tiling 输出。少一个字段就会出现缓存污染——用旧的 tiling 结果服务新请求，导致错误切分。

**练习 3**：`TilingScratch` 为什么是 `thread_local`？

**答案**：tiling data 与 workspace 缓冲只在中转时使用，做成 thread_local 既避免了每次调用的堆分配，又天然免除了多线程并发访问的锁开销（多进程/多线程编译是 Inductor 场景的常态）。

### 4.3 cv 融合：tiling wrapper 复用编译与 dtype 感知对齐

#### 4.3.1 概念说明

本次更新的两大改动都服务于 **Inductor CV fusion** 场景（MatMul + 逐元素算子融合）：

- **wrapper 复用编译**：原先每次编译融合 kernel，wrapper 的实现源码（`kCubeKernelTilingWrapperCppValue` 那一千余行）都要跟着 host 链路重新编译一遍，而这些内容对同平台的全部 CV 融合图是完全相同的。改法是把 wrapper 单独编成一个**共享 so**，按内容寻址缓存；主 host 源码只链接这个 so。
- **dtype 感知的 CV 融合**：MatMul 输出 dtype（fp16/bf16/fp32）不同，vector 段在 N 方向的对齐粒度就不同。对齐粒度由「32 字节块 / dtype 字节数」决定：fp16 需 16 个元素、fp32 需 8 个元素对齐。生成代码统一以 `curAivM`/`curAivN` 二维组织 vector 段参数，N 方向 stride 经 `KernelUtils::BlkAlign` 做 32 字节块对齐。

#### 4.3.2 核心流程

wrapper 缓存的命中条件是一个**六元内容寻址 key**：

\[
\text{key} = \mathrm{sha256}(\ \text{wrapper 源内容} \,\|\, \text{ASCEND\_PATH} \,\|\, \text{machine} \,\|\, \text{soc\_version} \,\|\, \text{compile\_options} \,\|\, \text{stage}\ )
\]

任一元变化（如换了 CANN 版本或 SoC）都会得到新的 so 文件名，天然隔离。构建过程用「临时文件 + `os.replace` 原子替换 + `fcntl.flock` 文件锁」保证多进程并发编译时只有一个进程真正编译、其余等待后直接复用。

dtype 感知对齐的量化关系：

\[
\text{align\_elems}(dtype) = \left\lceil \frac{32\,\text{B}}{\text{sizeof}(dtype)} \right\rceil, \qquad
\text{curAlignN} = \lceil \text{curAivN} / \text{align\_elems} \rceil \cdot \text{align\_elems}
\]

即把 cube 输出块的 N 维有效宽度向上取整到 32 字节块边界，`load_dst_stride = curAlignN - curAivN` 描述补齐部分的跳距。

#### 4.3.3 源码精读

Python 侧缓存的全部逻辑在 `ascendc_compile.py`。[ascendc_compile.py:L233-L242](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L233-L242) 计算 so 路径——sha256 依次吃进源文件字节、ASCEND_PATH、machine、soc_version、compile_options、stage 六元，截取 16 位十六进制做文件名：

```python
def get_shared_cv_wrapper_so_path(args, temp_dir, source_file):
    digest = hashlib.sha256()
    digest.update(read_file_bytes(source_file))
    digest.update(str(ASCEND_PATH).encode("utf-8"))
    digest.update(str(machine).encode("utf-8"))
    digest.update(str(getattr(args, "soc_version", "")).encode("utf-8"))
    digest.update(str(getattr(args, "compile_options", "")).encode("utf-8"))
    digest.update(str(getattr(args, "stage", "")).encode("utf-8"))
    so_name = f"{CV_WRAPPER_SO_BASENAME}_{digest.hexdigest()[:16]}.so"
    return os.path.join(get_shared_cv_wrapper_cache_dir(args, temp_dir), so_name)
```

并发安全由 [ascendc_compile.py:L275-L289](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L275-L289) 保证：先无锁探测文件是否已存在（快路径），不存在再加 `LOCK_EX` 文件锁、双重检查后编译，`build_shared_cv_wrapper_so`（[L257-L272](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L257-L272)）内部用 `tmp_so_path` + `os.replace` 原子落盘：

```python
def ensure_shared_cv_wrapper_so(args, temp_dir, source_file):
    so_path = get_shared_cv_wrapper_so_path(args, temp_dir, source_file)
    if os.path.exists(so_path):
        return so_path
    ...
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            if os.path.exists(so_path):
                return so_path
            build_shared_cv_wrapper_so(args, temp_dir, source_file, so_path)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
```

边界保护体现在 [ascendc_compile.py:L292-L313](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L292-L313)：`prepare_shared_cv_wrapper` 只在 `is_cv_fusion_compile(args)` 为真时把 wrapper 源从常规 host 文件中剥离并设置 `args.shared_cv_wrapper_so`；`append_shared_cv_wrapper_so` 再次检查同一条件才把 so 追加进链接对象——即残留的 `shared_cv_wrapper_so` 状态不会污染非 CV 编译链路。`is_cv_fusion_compile` 的判据（[L503](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L503)）是 tiling 源文件内容中出现 `CVAutofuseTilingData` 标识。此外缓存目录名固定为 `cv_tiling_wrapper_cache`（[L44](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L44)），并被排除在 `static_shape_kernel_proc` 的临时目录清理之外，使缓存可跨重编译阶段存活。

dtype 感知对齐的生成侧在 `codegen_kernel.cpp`：[codegen_kernel.cpp:L4263-L4278](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel.cpp#L4263-L4278) 生成的 vector stage 函数统一以 `curAivM`/`curAivN`/`curAlignN`/`shapeN` 为参数，搬运用 `load_dst_stride = curAlignN - curAivN` 处理补齐跳距；对齐宽度本身经 [codegen_kernel.cpp:L1110-L1114](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel.cpp#L1110-L1114) 的 `KernelUtils::BlkAlign<dtype>`（按 dtype 实例化，32 字节块）计算。结合 u8-l3 讲过的 `GetCVAlignedSize` 工具，Cast/取整类算子在 CV 融合场景统一按此粒度对齐，非 CV 场景行为不变。

#### 4.3.4 代码实践

**实践目标**：验证 cv wrapper 缓存的命中与隔离行为。

**操作步骤**：

1. 阅读 `autofuse/tests/ut/python/test_ascendc_compile.py` 中本次新增的约 500 行测试，找出覆盖 `ensure_shared_cv_wrapper_so`、`append_shared_cv_wrapper_so`、缓存 key 变化的测试用例名。
2. 构造思想实验（纸面推演）：同一 wrapper 源码在 (a) 完全相同的环境、(b) 更换 `soc_version` 后，`get_shared_cv_wrapper_so_path` 返回的路径分别是什么关系？
3. 若本地有构建环境，运行 `sh build.sh -u autofuse_framework` 跑相关 Python UT。

**需要观察的现象**：情形 (a) 两次得到完全相同的 so 路径（缓存命中，零次编译）；情形 (b) 得到不同路径（缓存隔离，各自编译一次）。

**预期结果**：能写出六个 key 元素分别「防什么」的清单，例如源内容防代码演进、ASCEND_PATH 防 CANN 版本差异、soc_version 防平台差异。UT 运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 wrapper so 的构建要「临时文件 + `os.replace`」而不是直接写目标路径？

**答案**：多进程并发时，若直接写目标路径，另一个进程可能读到写了一半的坏 so。先写带 pid/时间戳的临时文件再 `os.replace` 原子改名，保证目标路径上的 so 任何时刻都是完整可用的。

**练习 2**：dtype 感知对齐中，fp16 与 fp32 的对齐元素数分别是多少？为什么不同 dtype 必须区别对待？

**答案**：fp16 是 32B/2B = 16 个元素，fp32 是 32B/4B = 8 个元素。Cube 输出经 Fixpipe 进 UB 时按 32 字节块搬运，若 N 向有效宽度不落在块边界上，vector 段读取会跨块错位；不同 dtype 单元素字节不同，同样的元素数对应的字节数不同，所以对齐粒度必须随 dtype 计算。

**练习 3**：`append_shared_cv_wrapper_so` 为什么要同时检查 `shared_cv_wrapper_so` 与 `is_cv_fusion_compile(args)` 两个条件？

**答案**：`shared_cv_wrapper_so` 挂在 args 上是流程中间状态，可能在编译参数对象复用或流程边界残留。双重检查保证即使状态泄漏到非 CV 链路，也不会错误地链接 CV wrapper so 或启用 CV 专用链接库，避免污染普通 host/device 编译。

### 4.4 平台 optimize/codegen 适配：v35 的注册与生成衔接

#### 4.4.1 概念说明

cube 算子进入 Autofuse 融合体系，除了设备端入口（4.1）和 tiling 借用（4.2），还需要在 v35 平台的 ASCIR 注册表里登记，使 optimizer 调度与 codegen 都能识别它。这遵循 u11-l1 讲过的「构建期合流、运行期分流」总策略：v2 注册与 v1 同名共存，由 SoC 版本决定用哪套实现。

#### 4.4.2 核心流程

```text
REG_ASC_IR(MatMul).Impl(v2_soc_versions, {AttImplV2, CodegenImplV2})
  → 图中出现 MatMul 节点时，v35 平台查到 v2 实现
  → ATT 侧：MatMulAscIrAttImplV2 提供 cube 场景的建模/门禁
  → Codegen 侧：MatMulAscIrCodegenImplV2 决定 api_call 形态与 tiling key
  → codegen_tiling_cube.cpp 生成 CallCubeTiling + wrapper 两份产物
  → ascendc_compile.py 把 wrapper 编成共享 so、链接进 host 可执行
```

#### 4.4.3 源码精读

[ascir_builtin_ops_v2.cpp:L1134-L1153](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp#L1134-L1153) 是 MatMul 家族在 v35 的注册：MatMul、MatMulBias、MatMulOffset、MatMulOffsetBias 四个变体，各自绑定 `MatMulAscIrAttImplV2`（ATT 实现）与 `MatMulAscIrCodegenImplV2`（codegen 实现）：

```cpp
REG_ASC_IR(MatMul).Impl(v2_soc_versions, {af::ascir::AscIrImplCreator<MatMulAscIrAttImplV2>(),
                                          af::ascir::AscIrImplCreator<af::ascir::MatMulAscIrCodegenImplV2>(),
REG_ASC_IR(MatMulBias)
    .Impl(v2_soc_versions,
          {af::ascir::AscIrImplCreator<MatMulAscIrAttImplV2>(),
           af::ascir::AscIrImplCreator<af::ascir::MatMulAscIrCodegenImplV2>(),
```

生成衔接处，[codegen_tiling_cube.cpp:L517-L527](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845ae7befd937c/autofuse/codegen/codegen_tiling_cube.cpp#L517-L527) 先从 `FusedScheduledResult` 提取 `MatMulCubeInfo`（含 is_batch），生成 `CallCubeTiling` 函数体，再把 wrapper 的头/源两个字符串常量作为独立文件装入结果映射——这一步正是「wrapper 成为可独立编译单元」的代码生成侧落点。

端到端验证材料在 `autofuse/tests/v35/st/backend_e2e_v2/` 下，例如本次更新的 `matmul_backend_generate.cpp`（含 CV 融合用例）与共享的 `backend_codegen_common.h`，可作为阅读型实践的参照。

#### 4.4.4 代码实践

**实践目标**：走通「注册 → 生成 → 编排」的 v35 cube 适配全链。

**操作步骤**：

1. 在 `autofuse/v35/ascir/generator/ascir_builtin_ops_v2.cpp` 中搜索 `MatMul`，确认四个变体的注册行。
2. 在 `autofuse/v35/ascir/generator/v2_ascir_codegen_impl.h` 中找到 `MatMulAscIrCodegenImplV2`，看它声明的 api_call 名称与头文件加载方法。
3. 阅读 `autofuse/tests/v35/st/backend_e2e_v2/` 下任一 `matmul_backend_generate.cpp`，标出它调用的 codegen 入口与期望产物中的 wrapper 文件名。

**需要观察的现象**：注册的四个 MatMul 变体如何覆盖 bias/offset 的四种组合；e2e 用例对生成产物的断言里出现 `CallCubeTiling` 与 wrapper 文件名。

**预期结果**：一张从 `REG_ASC_IR` 到生成文件名的映射表。e2e 用例实际运行待本地验证（需 v35 环境与 build.sh 构建）。

#### 4.4.5 小练习与答案

**练习 1**：MatMul 家族为什么要注册成四个算子（MatMul/MatMulBias/MatMulOffset/MatMulOffsetBias）而不是一个带可选输入的 MatMul？

**答案**：ASCIR 注册的输入/输出元数据是静态的（见 u5-l1）；拆成四个变体让每个变体的输入槽位确定，codegen 与 tiling wrapper 可以按确定的输入数目生成代码（wrapper 侧对应 `autofuse_has_bias`/`autofuse_has_offset_w` 标记与 2~4 输入校验），避免在设备端入口处理可变参数。

**练习 2**：`MatMulCubeInfo.is_batch` 从注册到生成如何流转？

**答案**：它由 `ExtractMatMulCubeInfoFromFusedResult` 从调度结果中提取，传入 `AppendCvSafetyMixModeHelperDefs` 决定生成 batch/非 batch 两套辅助定义，并最终决定 `CallCubeTiling` 调 `DoMatMulTiling(..., is_batch)` 时查询 `BatchMatMulV3` 还是 `MatMulV3` 的 op_host 函数表。

## 5. 综合实践

**任务**：为「BatchMatMul + Cast（bf16→fp32）」的 CV 融合场景写一份链路说明文档，要求覆盖本讲全部四个模块：

1. **设备端**：指出 `batch_mat_mul_v3` 入口在该场景下走 `if constexpr` 的哪个分支（提示：BASIC_LEVEL + BASIC + NO_FULL_LOAD + ON_THE_FLY + FOR_BATCH），`CV_UB_FUSION` 打开后入口签名多了什么。
2. **tiling 借用**：写出 `DoMatMulTiling` 会读取哪些属性（`adj_x1`/`adj_x2`——注意 batch 版属性名与 MatMul 版 `transpose_x1` 不同）、`RuntimeTilingKey` 会纳入哪些 shape。
3. **对齐**：推导 bf16 输出下 `curAlignN` 的对齐元素数（32B/2B = 16），说明 Cast 段 `load_dst_stride` 的来源。
4. **编排**：说明 wrapper so 的六元缓存 key 在此场景各取什么值，以及为何同一模型内多个 BatchMatMul+Cast 子图共享一次 wrapper 编译。

完成后可与 `autofuse/tests/v35/st/backend_e2e_v2/matmul_backend_generate.cpp` 的实际生成代码交叉验证。生成与运行部分待本地验证。

## 6. 本讲小结

- cube 算子设备端入口是模板参数化的完整 kernel（`__global__ __aicore__`），用 int8_t 模板参数 + `if constexpr` 表达编译期布局差异，每个实例对应一个 tiling key；conv2d 的差异维度（12 个模板参数 + 分形权重布局）同样全部编码在编译期。
- cube 的 tiling 不由 ATT 重写，而是通过 `CubeKernelTilingWrapper` 借用 CANN op_host 的 `MatMulV3`/`BatchMatMulV3` tiling 函数，配运行期结果缓存、函数表 `call_once`、编译状态缓存三层缓存。
- cv tiling wrapper 复用编译：wrapper 精简为纯接口（`kCubeKernelTilingWrapperHppValue`）+ 可独立成 so 的实现，Python 侧按六元内容寻址 key 缓存共享 so，文件锁 + 原子替换保证并发安全，双重条件检查防止污染非 CV 链路。
- dtype 感知 CV 融合：vector 段统一按 `curAivM`/`curAivN` 组织，N 向经 `KernelUtils::BlkAlign<dtype>`（32 字节块 / dtype 字节数）对齐，`curAlignN - curAivN` 作为补齐跳距。
- v35 平台适配遵循「注册表分流」：`REG_ASC_IR(MatMul*)` 绑定 v2 的 ATT/codegen 双实现，经 `CallCubeTiling` 生成物与 `ascendc_compile.py` 编排完成闭环。

## 7. 下一步学习建议

- 进入 **u11-l3（NDDMA 1D 精确性能模型）**：了解 v35 新增的搬运性能建模如何与 `api_perf_register` 注册衔接，以及它为何在 `is_cv_ub_fusion` 场景回退 legacy 模型——本讲的 CV 融合门禁在那里再次出现。
- 若想补齐 CV 融合的 Inductor 侧视角，回读 **u8-l2** 的 `GenerateCVFusion`/`GenerateForInductor` 与 PGO 候选稳定化部分。
- 推荐继续精读 `autofuse/codegen/codegen_tiling_cube.cpp` 全文与 `autofuse/tests/v35/st/backend_e2e_v2/matmul_backend_generate.cpp`，把「生成的 tiling 源码长什么样」从推断变成亲见。
