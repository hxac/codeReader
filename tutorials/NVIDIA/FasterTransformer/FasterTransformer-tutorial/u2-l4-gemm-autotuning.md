# GEMM 算法自动调优：cublasAlgoMap 与 gemm_test

## 1. 本讲目标

在 [u2-l3](u2-l3-cublas-gemm.md) 里我们已经知道：FasterTransformer（下称 FT）的矩阵乘骨干是 `cublasMMWrapper`，它把 cuBLAS / cuBLASLt 收拢成一个可复用对象。但还有一个被刻意略过的问题——

> 同样一个 \( C = AB \)，cuBLAS 内部往往有十几甚至上千种「算法（algorithm）」可以完成它，到底该用哪一种？

本讲就回答这个问题。读完本讲你应当能够：

1. 解释 **为什么同一个 GEMM 在不同 \((M,N,K)\) 形状下，最优 cuBLAS 算法会不同**，以及为什么必须为每个形状单独挑选。
2. 看懂 **`cublasAlgoMap`** 这个「key → 最优算法」映射容器的数据结构、文件格式与查询流程。
3. 复述 **`gemm_test` 离线调优工具**（以 `gpt_gemm_func` 为代表）是如何暴力遍历候选算法、计时、并挑选最优算法写回 `gemm_config.in` 的。
4. 把「离线调优产物 → 运行期加载 → 命中算法」这条链路在源码里走通。

本讲只讲 **算法选择 / 离线调优** 这一件事，不展开 cuBLAS 句柄与 GEMM 接口本身的细节（那是 u2-l3 的内容），也不进入任何具体模型的前向流程。

---

## 2. 前置知识

### 2.1 什么是 cuBLAS 的「算法（algo）」

cuBLAS 在做矩阵乘时，并不是只有一种实现。针对同一个 \((M,N,K)\) 的 GEMM，它会提供多种 **算法编号**，例如：

- 经典 cuBLAS（`cublasGemmEx`）下，FP32 有 `CUBLAS_GEMM_DEFAULT`、`CUBLAS_GEMM_ALGO0` … `CUBLAS_GEMM_ALGO23` 等约 24 个枚举值；
- cuBLASLt（`cublasLtMatmul`）下，算法不是单一编号，而是一个 **组合**：`algoId × tile × stages × splitK × swizzle × reductionScheme × workspaceSize …`，组合数可以高达数千上万。

不同算法对应 GPU 上的 **不同分块策略、不同的 tensor core 调度、是否切分 K 维、是否需要 workspace** 等。它们在数学上等价（算出的 C 一样），但在不同形状下性能可能相差 2~3 倍。

### 2.2 为什么不能「一个算法打天下」

GPU 上的 GEMM 性能高度依赖 **形状（shape）**：

- 大 \(M\)（行数）时，一个 block 能分到更多行，某些 tile 更划算；
- 小 \(M\)、大 \(N,K\) 时，另一种 tile 更划算；
- \(K\) 很大时，**split-K**（把 K 维切分到多个 block 再规约）可能更快。

Transformer 推理里，GEMM 的形状随 **batch_size、seq_len、beam_width** 剧烈变化：context 阶段 \(M = \text{batch} \times \text{beam} \times \text{seq\_len}\)，decoder 阶段 \(M = \text{batch} \times \text{beam}\)。两者可能相差几十倍。因此 **「为每个 \((M,N,K)\) 选一个最优算法」是有意义的**，这正是 cuBLAS 提供 `cublasGemmAlgo_t` 参数的原因——它允许调用方指定算法，而不是让库自己猜。

### 2.3 思路：离线调优 + 运行期查表

既然选算法要靠实测，而推理时又不能现场试每种算法（太慢），FT 的做法是经典的 **离线调优 + 运行期查表**：

```
┌─────────────────────┐         gemm_config.in          ┌─────────────────────┐
│  gemm_test 工具      │ ────写入 (M,N,K)->最优algo ────▶ │  磁盘上的文本文件    │
│ （离线，只跑一次）   │                                 │  一行一个形状        │
└─────────────────────┘                                 └─────────────────────┘
                                                              │ 启动时读取
                                                              ▼
                                                 ┌─────────────────────────┐
                                                 │ cublasAlgoMap（内存表） │
                                                 │  (M,N,K,data_type)      │
                                                 │   -> algoId/tile/...    │
                                                 └─────────────────────────┘
                                                              │ 推理时查询
                                                              ▼
                                                 ┌─────────────────────────┐
                                                 │ cublasMMWrapper::Gemm   │
                                                 │ 用查到的 algo 调 cuBLAS  │
                                                 └─────────────────────────┘
```

本讲剩下部分就是逐一拆开这三块。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [cublasAlgoMap.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.h) | 声明「算法信息」与「配置键」两个结构体、`cublasAlgoMap` 类，以及 `gemm_config.in` 等文件名宏 |
| [cublasAlgoMap.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.cc) | 实现：从 `gemm_config.in` 解析、`getAlgo`/`isExist` 查询、稀疏 GEMM 的 algo 查询 |
| [gemm_test/gemm_func.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gemm_func.h) | 调优工具的公共声明：`LtHgemmCustomFind`（cuBLASLt 组合搜索）、`printPerfStructure` 等 |
| [gemm_test/gpt_gemm_func.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc) | GPT 专属调优：枚举 GPT 的 11 个 GEMM 形状、暴力计时、挑最优、写 `gemm_config.in` |
| [models/multi_gpu_gpt/gpt_gemm.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/gpt_gemm.cc) | 调优工具的可执行入口 `main`：解析命令行、分配 scratch buffer、按数据类型分发到模板 |

> 说明：除 GPT 外，BERT / ViT / Swin / T5 等模型在 `utils/gemm_test/` 下各有自己的 `xxx_gemm_func.cc`，套路完全一致；本讲以 GPT 为代表讲透即可。

---

## 4. 核心概念与源码讲解

### 4.1 为什么需要为每个 (M,N,K) 选择算法

#### 4.1.1 概念说明

把 cuBLAS 想象成一个「工具箱」，里面有几十把螺丝刀（算法），拧同一颗螺丝（同一个 GEMM）哪把最快，取决于螺丝的大小和材质（矩阵形状）。cuBLAS 提供了两类「算法」：

- **经典 cuBLAS 算法**：单个整数 `algoId`，适合 FP32。
- **cuBLASLt 算法**：一个结构体，由若干字段组合而成，组合空间巨大，适合 FP16/BF16/FP8（能更好地驱动 tensor core）。

无论哪一类，FT 都把它抽象成一个统一的「算法信息」结构体保存下来。

#### 4.1.2 核心流程

挑选最优算法没有解析公式，只能 **实测**：

1. 对一个固定的 \((M,N,K,\text{data\_type})\)，
2. 遍历候选算法集合，
3. 每个算法重复跑 `ites=100` 次取平均耗时，
4. 取耗时最小的那个算法，
5. 把 `(形状 → 最优算法)` 这条记录写到文件。

这里的「候选算法集合」对 cuBLAS 是一个连续的 `algoId` 区间，对 cuBLASLt 是 `LtHgemmCustomFind` 内部枚举的大量组合。

#### 4.1.3 源码精读

先看「算法信息」这个结构体长什么样，它既是调优时的产物，也是运行期查表返回的内容：

[cublasAlgoMap.h:35-46](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.h#L35-L46) —— 一组字段共同描述一个算法，`exec_time` 保存调优时测得的最优耗时：

```cpp
typedef struct {
    int algoId, customOption, tile, splitK_val;
    int swizzle, reductionScheme, workspaceSize;
    int stages;          // cublasLt >= 11.0 才用；也是 cublasLt vs cublas 的判别位
    // ... 某些 cuBLAS 版本下还有 inner/cluster shape 字段 ...
    float exec_time;     // 该算法调优时的执行耗时（ms）
} cublasLtMatmulAlgo_info;
```

注意 `stages` 字段：它在运行期被 `cublasMMWrapper` 当作 **「这条记录到底来自 cuBLASLt 还是经典 cuBLAS」** 的判别位（见 4.2.3）。

调优时还有一个临时结构体，记录每次试跑的结果，用于在组合空间里挑最优：

[cublasAlgoMap.h:48-58](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.h#L48-L58) —— `customMatmulPerf_t`，含 `algo`、`status`、`time`、`workspaceSize` 等，是 cuBLASLt 组合搜索的产物，**只在调优期间存在**，不进文件、不进运行期。

#### 4.1.4 代码实践

**目标**：在源码里确认「不同数据类型对应不同的候选算法区间」，从而直观理解「算法空间是枚举出来的」。

**步骤**：

1. 打开 [gpt_gemm_func.cc:234-294](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L234-L294)。
2. 找到 `if (std::is_same<T, float>::value)` 分支，记录它的 `startAlgo` / `endAlgo`。
3. 再找到 `half`、`__nv_bfloat16`、`__nv_fp8_e4m3` 三个分支，对比它们的区间。

**需要观察的现象**：
- FP32 的区间是 `CUBLAS_GEMM_DEFAULT` 到 `CUBLAS_GEMM_ALGO23`（经典 cuBLAS，约 24 个整数编号）。
- FP16 / BF16 / FP8 的区间是 `CUBLAS_GEMM_DEFAULT_TENSOR_OP` 到 `CUBLAS_GEMM_ALGO15_TENSOR_OP`（tensor op 系列），且后面还会再叠加一次 cuBLASLt 的组合搜索（见 4.3）。

**预期结果**：你会看到「算法集合」不是抽象概念，而是源码里实打实的两个 `int` 端点，`for (int algo = startAlgo; algo <= endAlgo; algo++)` 就是枚举它的循环（[gpt_gemm_func.cc:324](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L324)）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FP32 只在经典 cuBLAS 算法区间里搜，而 FP16 还要再跑一遍 cuBLASLt 组合搜索？

**参考答案**：FP32 不是 tensor core 的优势精度，经典 cuBLAS 算法已经够好；FP16/BF16/FP8 能用 tensor core 加速，而 cuBLASLt 提供了远比经典 cuBLAS 丰富的 tensor core 算法组合（tile/stages/splitK 等），所以值得再花时间在 cuBLASLt 的大空间里搜一遍。源码里的判别见 [gpt_gemm_func.cc:442](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L442) 的 `if ((data_type != FLOAT_DATATYPE && ...) || data_type == FP8_DATATYPE)`。

**练习 2**：`ites=100` 这个数字（[gpt_gemm_func.cc:230](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L230)）为什么不是 1？

**参考答案**：单次 kernel 耗时受系统抖动、GPU 频率漂移影响很大，取多次平均才能稳定比较。调优是离线一次性开销，多跑 100 次换来更可信的算法选择是划算的。

---

### 4.2 cublasAlgoMap：算法映射的存取

#### 4.2.1 概念说明

`cublasAlgoMap` 是运行期持有的一个 **内存中的哈希表**，它的职责非常单一：

- **启动时**：从磁盘上的 `gemm_config.in` 把所有 `(形状 → 算法)` 记录读进哈希表；
- **推理时**：给定 \((batch\_count, m, n, k, data\_type)\)，O(1) 查出对应的最优算法信息。

它本身不做任何 GEMM 计算，只是 `cublasMMWrapper` 的一个查表工具。

#### 4.2.2 核心流程

```
启动:
  cublasAlgoMap 构造 ──▶ loadGemmConfig() ──▶ 逐行 fscanf gemm_config.in
                                              ──▶ 组装 key {batch,m,n,k,data_type}
                                              ──▶ algo_map_[key] = algo_info
                                              （key 已存在则跳过，先到先得）

推理（每个 GEMM 调用前）:
  isExist(bc,m,n,k,dtype) ──▶ 在表里能否查到?
       │ 是                         │ 否
       ▼                            ▼
  getAlgo(...) 返回表中信息    返回默认 algo（CUBLAS_GEMM_DEFAULT[_TENSOR_OP]）
```

#### 4.2.3 源码精读

**（a）配置键与哈希函数**。整个表的核心是下面这个「键」和一个为它量身定做的哈希函数：

[cublasAlgoMap.h:60-71](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.h#L60-L71) —— 键由 `batch_count, m, n, k, data_type` 五元组构成：

```cpp
struct cublasAlgoConfig_t {
    int batch_count, m, n, k;
    CublasDataType data_type;
    bool operator==(cublasAlgoConfig_t const& config) const { /* 五元组全相等 */ }
};
```

[cublasAlgoMap.h:73-80](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.h#L73-L80) —— 手写的哈希函数，用几个大质数（98317、49157、24593、196613、6151）做异或乘法来分散键值，避免不同形状撞到同一个桶：

```cpp
std::size_t operator()(cublasAlgoConfig_t const& config) const {
    return config.batch_count * 98317ull ^ config.m * 49157ull ^ config.n * 24593ull
           ^ config.k * 196613ull ^ static_cast<int>(config.data_type) * 6151ull;
}
```

底层容器就是一个 `unordered_map`（[cublasAlgoMap.h:84](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.h#L84)），外加一个独立的 `sp_algo_map_` 给稀疏 GEMM 用。

**（b）构造时一次性读文件**。构造函数接收一个文件名（默认就是宏 `GEMM_CONFIG`，即字符串 `"gemm_config.in"`，见 [cublasAlgoMap.h:29-33](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.h#L29-L33)），并立刻加载：

[cublasAlgoMap.cc:21-26](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.cc#L21-L26)：

```cpp
cublasAlgoMap::cublasAlgoMap(const std::string filename, const std::string sp_config_filename):
    config_filename_(filename), sp_config_filename_(sp_config_filename) {
    loadGemmConfig();
    loadSpGemmConfig();
}
```

`loadGemmConfig` 的核心是一段长 `fscanf`，其格式串正好对应调优工具写出的文件格式——文件每行先写 `batch_size seq_len head_num size_per_head dataType`（人类可读的「这是哪个模型配置」），再跟一个 `###` 分隔符，最后是机器读取的 `batchCount n m k algoId customOption tile ... exec_time`：

[cublasAlgoMap.cc:60-100](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.cc#L60-L100) —— 解析一行并组装键值：

```cpp
while (fscanf(fd, "%d %d %d %d %d ### %d %d %d %d %d %d ..." , 
              &batch_size, &seq_len, &head_num, &size_per_head, &dataType,
              &batchCount2, &n2, &m2, &k2, &algoId, /* ... */ &exec_time) != EOF) {
    // 校验 dataType 合法
    cublasAlgoConfig_t markStr{batchCount2, m2, n2, k2, static_cast<CublasDataType>(dataType)};
    if (algo_map_.find(markStr) == algo_map_.end()) {   // 先到先得
        algo_map_[markStr].algoId = algoId;
        // ... 填充 customOption/tile/splitK_val/swizzle/.../exec_time
    }
}
```

注意两个细节：

- **`###` 前是给人看的，`###` 后才是给机器读的**。`batch_size seq_len head_num size_per_head` 这些字段只用于文件可读性，**不参与哈希键**——真正参与键的是 `###` 之后的 `batchCount/m/n/k/dataType`。
- **先到先得**：`if (algo_map_.find(markStr) == algo_map_.end())` 表示同一个形状若已在文件里出现过，后面重复的行被忽略。这与调优工具的 `is_append` 机制配合（见 4.3.3）。

> ⚠️ **易踩坑（m/n 互换）**：写入文件时 `###` 后第 2~4 个整数是 `n, m, k`（见 [gpt_gemm_func.cc:487-505](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L487-L505) 的 `fprintf`），`loadGemmConfig` 读回来按 `n2, m2, k2` 解析，组装成键 `{batchCount, m2, n2, k2}`；而查询接口 `getAlgo/isExist` 组装的键却是 `{batch_count, n, m, k}`（[cublasAlgoMap.cc:128 与 L135](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.cc#L125-L135)）——m 与 n 在「写/读」和「查询」两侧是互换的。这是为了对齐 cuBLAS 列主序与 FT 行主序存储之间的转置约定（详见 [u2-l3](u2-l3-cublas-gemm.md)）。结论是：**只要调优工具与运行期 GEMM 调用使用同一套布局约定，文件里的记录就能被正确命中**，读者不必在这里深究方向，只需知道这个互换是「成对设计、刻意为之」。

**（c）查询接口**。

[cublasAlgoMap.cc:125-153](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.cc#L125-L153) —— `isExist` 判存亡，`getAlgo` 取记录；查不到时返回一个 **默认算法**：

```cpp
cublasLtMatmulAlgo_info
cublasAlgoMap::getAlgo(const int batch_count, const int m, const int n, const int k, const CublasDataType data_type) {
    cublasAlgoConfig_t mark{batch_count, n, m, k, data_type};
    if (algo_map_.find(mark) != algo_map_.end()) {
        return algo_map_[mark];            // 命中：返回调优过的算法
    } else {
        cublasLtMatmulAlgo_info tmp_algo;
        tmp_algo.algoId = static_cast<int>(data_type == FLOAT_DATATYPE
                                           ? CUBLAS_GEMM_DEFAULT
                                           : CUBLAS_GEMM_DEFAULT_TENSOR_OP);
        tmp_algo.stages = -1; /* 其余字段全 -1 */   // 未命中：退回 cuBLAS 默认算法
        return tmp_algo;
    }
}
```

这里有一个 **关键约定**：`stages = -1` 表示「这条记录没有调优过、走经典 cuBLAS 默认算法」。`cublasMMWrapper` 正是用 `stages != -1` 来区分到底走 cuBLASLt 还是经典 cuBLAS：

[cublasMMWrapper.cc:182-192](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L182-L192) —— 运行期消费侧：

```cpp
int findAlgo = cublas_algo_map_->isExist(batch_count, m, n, k, getCublasDataType(Atype_));
cublasLtMatmulAlgo_info info = cublas_algo_map_->getAlgo(batch_count, m, n, k, getCublasDataType(Atype_));
if (findAlgo) {
    if (info.stages != -1) using_cublasLt = true;   // 有 stages → cuBLASLt
    else                   using_cublasLt = false;  // 无 stages → 经典 cuBLAS
}
```

这就把 4.1 里「两种算法空间」和运行期「两条执行路径」串起来了。

**（d）在示例里如何被实例化**。在 GPT 的 C++ 示例里，`cublasAlgoMap` 是一个栈上对象，默认构造（用 `GEMM_CONFIG` 宏指定的 `"gemm_config.in"`），再以引用形式喂给 `cublasMMWrapper`：

[multi_gpu_gpt_example.cc:104-117](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/multi_gpu_gpt/multi_gpu_gpt_example.cc#L104-L117)：

```cpp
cublasAlgoMap    cublas_algo_map;          // 默认构造 → 读 ./gemm_config.in
// ...
cublasMMWrapper cublas_wrapper = init_cublas_ctx(..., cublas_algo_map ...);
```

如果运行目录下没有 `gemm_config.in`，`loadGemmConfig` 会打印 `[WARNING] ... is not found; using default GEMM algo`（[cublasAlgoMap.cc:45-48](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.cc#L45-L48)），程序仍可运行，只是所有 GEMM 都用默认算法——这正是「没调优也能跑，但慢」的兜底路径。

#### 4.2.4 代码实践

**目标**：把「文件格式」与「解析代码」对上号，理解 `###` 前后的分工。

**步骤**：

1. 阅读 [cublasAlgoMap.cc:60-93](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.cc#L60-L93) 的 `fscanf` 格式串，数一下 `###` 前后各有多少个 `%d`。
2. 再看调优工具写入文件的两个 `fprintf`（[gpt_gemm_func.cc:487-505](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L487-L505) 与 [gpt_gemm_func.cc:510-528](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L510-L528)），确认写出的字段顺序与 `fscanf` 完全一致。
3. 注意 cuBLAS 版本相关的条件编译（`#if (CUBLAS_VER_MAJOR == 11 ...)`），它会在某些版本下多写/多读 `inner_shapeId/cluster_shapeId` 等字段——**这就是为什么换 cuBLAS 版本后旧的 `gemm_config.in` 可能对不上、需要重新调优**。

**需要观察的现象**：`###` 之前 5 个整数（模型可读信息），`###` 之后是一长串算法字段，最后一个是 `exec_time`（`%f`）。写入与读取的字段数和顺序必须严格对齐，否则整张表会解析错乱。

**预期结果**：你能口头复述「文件一行 = 模型信息 `###` 算法字段 + 耗时」，并理解为什么换 cuBLAS 大版本要重新生成配置文件。

> 待本地验证：如果你手头有编译好的 FT，跑一次 `./bin/gpt_gemm ...`（见 4.3）后用文本编辑器打开生成的 `gemm_config.in`，对照本节格式逐字段核对。

#### 4.2.5 小练习与答案

**练习 1**：如果运行目录下没有 `gemm_config.in`，程序会崩溃吗？性能会怎样？

**参考答案**：不会崩溃。`loadGemmConfig` 在 `fopen` 返回 `NULL` 时只打印一条 `[WARNING]` 并 `return`（[cublasAlgoMap.cc:44-48](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.cc#L44-L48)）；之后每次 `getAlgo` 都查不到，返回 `stages=-1` 的默认算法，于是所有 GEMM 走 cuBLAS 默认路径。功能正常，但性能通常明显劣于调优过的情况。

**练习 2**：`stages` 字段在运行期被用来做什么判别？为什么用 `-1` 当哨兵？

**参考答案**：`cublasMMWrapper` 用 `info.stages != -1` 判断该算法是否来自 cuBLASLt（[cublasMMWrapper.cc:186](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L182-L192)）。`stages` 是 cuBLASLt 才有的概念（见结构体注释 *only used in cublasLt >= 11.0*），经典 cuBLAS 没有这个维度，所以用 `-1` 表示「未设置 / 来自经典 cuBLAS」是安全的哨兵值。

**练习 3**：为什么 `cublasAlgoConfig_t` 的键里要带上 `data_type`？

**参考答案**：同一个 \((M,N,K)\) 在 FP16 和 BF16 下，最优算法很可能不同（tile/stages 选择依赖 tensor core 的具体数据类型支持）。把 `data_type` 放进键，可以让一份 `gemm_config.in` 同时承载多种精度的调优结果而不互相覆盖。

---

### 4.3 gemm_test 离线调优流程

#### 4.3.1 概念说明

`gemm_test` 是一个 **独立的可执行程序**（编译产物 `./bin/gpt_gemm`、`./bin/bert_gemm` 等），它 **不加载任何模型权重**，只关心一件事：给定模型的形状参数（batch、head_num、size_per_head、inter_size、vocab_size、tensor_para_size、data_type），把该模型推理过程中会出现的所有 GEMM 形状枚举出来，逐个调优，写成 `gemm_config.in`。

它解决的核心矛盾是：**推理时不能现场试算法（太慢），但算法又必须靠实测来选**。于是把实测挪到离线，一次性做完。

#### 4.3.2 核心流程

以 GPT 为例（`generate_gpt_gemm_config`），调优流程是：

```
1. 由模型结构推导出本次推理会出现的 11 个 GEMM 形状 (M[i],N[i],K[i],batchCount[i])
     - i=0..5 : context 阶段（处理整段 prompt，seq = max_input_len）
     - i=6..9 : decoder 阶段（逐 token，seq = 1）
     - i=10   : logits 投影
2. 对每个形状：
     a. 暴力遍历 cuBLAS 算法区间 [startAlgo, endAlgo]，每个算法跑 100 次取均值
     b. 若是 FP16/BF16/FP8，再用 cublasLt 在 5000 个组合里搜索（LtHgemmCustomFind）
     c. 取 cuBLAS 与 cuBLASLt 两者中更快的，写成一行
3. （可选）若开启 SPARSITY_ENABLED 且硬件支持，再为前 8 个形状调优 cusparseLt 稀疏算法
4. 全部写入 ./gemm_config.in（首行是表头）
```

#### 4.3.3 源码精读

**（a）入口与命令行**。工具入口在 `gpt_gemm.cc` 的 `main`，参数恰好是「模型形状」而非「算法」：

[gpt_gemm.cc:22-48](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/gpt_gemm.cc#L22-L48) —— 参数依次为 `batch_size beam_width max_input_len head_num size_per_head inter_size vocab_size data_type tensor_para_size is_append`：

```cpp
if (argc < 9 || argc > 11) {
    FT_LOG_ERROR("./bin/gpt_gemm batch_size beam_width max_input_len head_number "
                 "size_per_head inter_size vocab_size data_type tensor_para_size is_append ...");
    FT_LOG_ERROR("e.g. ./bin/gpt_gemm 8 4 32 96 128 49152 51200 1 8 1");
    return 0;
}
```

其中 `data_type` 是 `CublasDataType` 枚举：`0=FP32, 1=FP16, 2=BF16`（[gpt_gemm.cc:46](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/gpt_gemm.cc#L46)）。`main` 先用 `calGptGemmTestBufSizeInByte` 算出调优需要的 scratch buffer 大小，检查显存够不够（[gpt_gemm.cc:63-85](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/gpt_gemm.cc#L63-L85)），再按数据类型分发到模板 `generate_gpt_gemm_config<T>`。

**（b）由模型结构推导 GEMM 形状**。这是理解「为什么调优必须按模型参数进行」的关键。函数从 `head_num / size_per_head / inter_size / vocab_size / tensor_para_size` 推导出 11 个 GEMM 的 \((M,N,K)\)：

[gpt_gemm_func.cc:93-97](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L93-L97)：

```cpp
const int hidden_units         = head_num * size_per_head;
const int local_head_num       = head_num / tensor_para_size;     // 张量并行切分后本卡的 head 数
const int local_hidden_units   = local_head_num * size_per_head;
const int max_input_len_padded = (max_input_len + 15) / 16 * 16;  // 16 对齐
const int gemm_num             = 11;
```

> 注意：这里的局部常量 `gemm_num = 11` 与 [cublasAlgoMap.h:29](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.h#L29) 的宏 `GEMM_NUM = 6` **不是一回事**。`GEMM_NUM` 是早期 BERT 模型的 GEMM 数（用于 `MAX_CONFIG_NUM * GEMM_NUM` 截断逻辑，见 4.3.3 末尾），GPT 自己定义了局部 `gemm_num = 11`。看到两个不同数字不要困惑。

接着逐个填形状，例如 context 阶段的 QKV 投影与 decoder 阶段的 QKV 投影形状完全不同——前者 \(M\) 含 `max_input_len`，后者 \(M\) 只含 `1`：

[gpt_gemm_func.cc:108-116](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L108-L116) —— context 阶段 QKV：

```cpp
// gemm 0: context from_tensor * weightQKV
M[0] = batch_size * beam_width * max_input_len;   // 行数 = 所有 token
K[0] = hidden_units;
N[0] = 3 * local_hidden_units;                    // QKV 合并，且按 tensor_para 切了
```

[gpt_gemm_func.cc:168-176](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L168-L176) —— decoder 阶段 QKV，注意 \(M\) 的差别：

```cpp
// gemm 6: from_token * weightQKV  （每步只生成 1 个 token）
M[6] = batch_size * beam_width;                   // 没有 max_input_len！
K[6] = hidden_units;
N[6] = 3 * local_hidden_units;
```

这正是「同一模型、context 与 decoder 两阶段需要不同算法」的根源：gemm 0 和 gemm 6 的 \(M\) 相差 `max_input_len` 倍，最优 tile/stages 几乎肯定不同。同理，**改 batch_size 或 beam_width 也会改变所有 \(M\)**，所以换 batch 就要重新调优（或用 `is_append` 追加新形状）。

**（c）暴力计时循环**。对每个形状，遍历候选算法、每个跑 100 次、取最小：

[gpt_gemm_func.cc:312-438](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L312-L438)（节选关键逻辑）：

```cpp
for (int i = 0; i < gemm_num; ++i) {
    int m = M[i], n = N[i], k = K[i];
    float exec_time = 99999.0f;
    int   fast_algo = 0;
    for (int algo = startAlgo; algo <= endAlgo; algo++) {        // 经典 cuBLAS 区间
        cudaDeviceSynchronize();
        gettimeofday(&start, NULL);
        for (int ite = 0; ite < ites; ++ite) {                   // ites = 100
            status = cublasGemmEx(..., static_cast<cublasGemmAlgo_t>(algo));   // i==1/2/10 用 batched/logits 变体
            if (status != CUBLAS_STATUS_SUCCESS) break;          // 不支持的算法直接跳过
        }
        cudaDeviceSynchronize();
        gettimeofday(&end, NULL);
        if (status == CUBLAS_STATUS_SUCCESS && diffTime(start,end)/ites < exec_time) {
            exec_time = diffTime(start, end) / ites;
            fast_algo = algo;
        }
    }
    // ... 紧接着的 cuBLASLt 搜索见下 ...
}
```

注意 `if (status != CUBLAS_STATUS_SUCCESS) break;`——并非所有算法都支持所有形状（比如某些算法要求 \(M\) 是 8 的倍数），遇到不支持的算法 cuBLAS 会返回错误，调优工具直接跳过它，这正是「枚举+实测」的稳健之处。

**（d）cuBLASLt 组合搜索**。对 FP16/BF16/FP8，经典 cuBLAS 区间扫完后，还会再用 `LtHgemmCustomFind` 在 cuBLASLt 的组合空间里搜（默认 `ALGO_COMBINATIONS = 5000` 个组合），并与经典 cuBLAS 的最优结果比大小：

[gpt_gemm_func.cc:442-485](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L442-L485)：

```cpp
if ((data_type != FLOAT_DATATYPE && i != 1 && i != 2 && i != 10) || data_type == FP8_DATATYPE) {
    int ALGO_COMBINATIONS = 5000;
    customMatmulPerf_t perfResults[ALGO_COMBINATIONS];
    LtHgemmCustomFind<T, float>(ltHandle, ..., n, m, k, ..., perfResults, ALGO_COMBINATIONS, ...);
    if (perfResults[0].time < exec_time) {
        printPerfStructure(..., perfResults[0], fd, ...);   // cuBLASLt 更快 → 写 cuBLASLt 算法（带 stages 等）
    } else {
        fprintf(fd, "... %d -1 -1 -1 -1 -1 -1 -1 ... %f\n", ..., fast_algo, exec_time);  // 否则写经典 cuBLAS algo
    }
}
```

`LtHgemmCustomFind` 的声明在 [gemm_func.h:51-74](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gemm_func.h#L51-L74)，它内部用 `cublasLtMatmulAlgoGetHeuristic` 拿到一批候选算法组合，逐一 `cublasLtMatmul` 计时，按耗时排序后把最优的放进 `perfResults[0]`。注意它接收 `strideA/strideB/strideD` 和 `batchCount`，所以连 `i==1/2` 那种 strided batched GEMM 也能在 cuBLASLt 下统一调优。

> 之所以 `i==1`（Q\*K^T）、`i==2`（QK\*V^T）、`i==10`（logits）在 FP16 下 **不走 cuBLASLt 搜索**，是因为它们是 strided batched 或大 N 的特殊形状，FT 在运行期对它们另有处理路径（attention 融合 kernel、logits 投影）。理解到「这些形状被排除」即可，不必在此深究。

**（e）append 机制与历史记录截断**。调优工具支持「为多个不同 batch 各跑一次、追加到同一文件」：

[gpt_gemm_func.cc:69-91](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L69-L91)：

- `is_append=false` → `fopen("w+")`，覆盖重写；
- `is_append=true` → `fopen("a+")`，追加。但当文件行数超过 `MAX_CONFIG_NUM * GEMM_NUM + 1`（即 `20 * 6 + 1 = 121` 行，`MAX_CONFIG_NUM` 定义在 [cuda_utils.h:36](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_utils.h#L36)）时，会丢弃最旧的记录、只保留最近 `MAX_CONFIG_NUM-1` 组形状。这避免了「服务很多种 batch 时配置文件无限膨胀」。

[gpt_guide.md:409-414](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L409-L414) 给出了典型用法——先以 `is_append=0` 生成 batch=8 的配置，再以 `is_append=1` 追加 batch=16 的配置：

```bash
./bin/gpt_gemm 8 1 32 12 128 6144 51200 1 1 0   # bs 8, 覆盖写
./bin/gpt_gemm 16 1 32 12 128 6144 51200 1 1 1  # bs 16, 追加
```

**（f）scratch buffer 大小**。调优需要在显存里放得下最大的那个 GEMM 的 A/B/C 矩阵，由 `calGptGemmTestBufSizeInByte` 估算（[gpt_gemm_func.cc:754-796](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L754-L796)），它对 context QKV / context batched / context ffn / vocab 四类形状各算一个 buffer 需求，取最大值，再加上 FP16/BF16/FP8 时的 32MB cuBLAS workspace（`CUBLAS_WORKSPACE_SIZE = 33554432`，定义在 [cuda_utils.h:39](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cuda_utils.h#L39)）。`main` 在分配前会先 `cudaMemGetInfo` 检查空闲显存（[gpt_gemm.cc:73-82](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/gpt_gemm.cc#L73-L82)），不够则直接报错退出。

#### 4.3.4 代码实践

**目标**：解释「为什么同一模型在不同 batch/seq 下需要不同的 algoMap」，并描述 `gemm_test` 如何遍历候选算法挑最优。

**步骤**：

1. 打开 [gpt_gemm_func.cc:108-216](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L108-L216)，把 11 个 GEMM 的 \((M,N,K)\) 抄成一张表，标注每个属于 context（`i<=5`）还是 decoder（`i>=6`）。
2. 假设 `batch_size=8, beam_width=1, max_input_len=32, head_num=12, size_per_head=128, tensor_para_size=1`，手算 gemm 0（context QKV）和 gemm 6（decoder QKV）的 \(M\)，体会两者相差多少倍。
3. 把 `batch_size` 改成 16，重算 gemm 0 的 \(M\)，确认它变了——所以最优算法很可能也变了，需要重新调优或 `is_append`。
4. 描述遍历流程：对每个形状，`for (algo = startAlgo; algo <= endAlgo; algo++)` 跑 100 次取均值（[gpt_gemm_func.cc:324-436](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L324-L436)），FP16/BF16/FP8 再叠加一次 5000 组合的 cuBLASLt 搜索（[gpt_gemm_func.cc:442-485](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L442-L485)），最终取两者更快的写文件。

**需要观察的现象 / 预期结果**：

- gemm 0 的 \(M = 8 \times 1 \times 32 = 256\)，gemm 6 的 \(M = 8 \times 1 = 8\)，相差 32 倍。这两个形状几乎不可能共用同一个最优算法——这正是必须为每个形状单独建一条 algoMap 记录的根本原因。
- 改 batch 后 gemm 0 的 \(M\) 变成 512，原来的 algoMap 记录（key 里不含 batch，但 \(M\) 已经不同，所以是不同的 key）查不到，于是需要重新调优。

> 待本地验证：以上手算可对照 `gpt_gemm` 实际跑出的 `gemm_config.in` 中 `###` 后的 `batchCount n m k` 字段确认。

#### 4.3.5 小练习与答案

**练习 1**：调优工具 `gpt_gemm` 加载模型权重吗？为什么？

**参考答案**：不加载。`main` 只分配一块 scratch buffer 并填入随机/未初始化数据（`generate_gpt_gemm_config` 内部直接用 `buffer` 当 A/B/C，从未读权重文件）。因为调优只关心 **形状与耗时**，不关心数值正确性，cuBLAS 对任意输入矩阵的耗时基本一致。这让工具极轻量、可独立运行。

**练习 2**：为什么 `is_append=1` 时文件超过 121 行要截断旧记录？

**参考答案**：在线服务可能面对多种 batch，每种都要一条调优记录。但 `cublasAlgoMap` 在启动时把整文件读进内存，文件越大启动越慢、内存越多。`MAX_CONFIG_NUM=20` 把形状种类限制在最近 20 组（`20 * GEMM_NUM + 表头`），丢弃过时的形状记录，兼顾「覆盖常见 batch」与「启动开销可控」。见 [gpt_gemm_func.cc:80-90](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L80-L90)。

**练习 3**：调优时为什么对每个算法要 `cudaDeviceSynchronize()` 两次（[gpt_gemm_func.cc:326 与 L426](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L324-L427)）？

**参考答案**：cuBLAS 调用是异步的（在 CUDA stream 上排队）。要测真实耗时，必须在计时起点前同步一次（确保上一轮全部完成、start 时刻干净），在 100 次循环结束后再同步一次（确保最后一次真正完成），用 `gettimeofday` 测 wall-clock 时间差。两次同步夹住循环，才能得到可靠的平均 kernel 耗时。

---

## 5. 综合实践

把本讲三块内容串起来，完成下面这个 **「从形状到算法」的端到端追踪任务**：

**背景**：假设你要在一个 12 层、`head_num=12, size_per_head=128, inter_size=49152, vocab_size=51200` 的 GPT 模型上，分别用 `batch_size=8` 和 `batch_size=16` 做推理，数据类型 FP16，单 GPU。

**任务**：

1. **写调优命令**：参考 [gpt_gemm.cc:35](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/gpt_gemm.cc#L35) 与 [gpt_guide.md:411-413](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L411-L413)，写出两条命令：第一条以 `is_append=0` 调优 batch=8，第二条以 `is_append=1` 追加 batch=16。说明 `data_type` 参数取 `1` 的含义。
2. **预测文件内容**：根据 [gpt_gemm_func.cc:108-216](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/gpt_gemm_func.cc#L108-L216)，说明跑完这两次后 `gemm_config.in` 里大约会有多少行有效的形状记录（提示：每次 11 个形状 × 2 次，但要考虑 `MAX_CONFIG_NUM` 截断与表头），以及 `###` 之前 5 个整数分别对应什么。
3. **追踪运行期命中**：假设推理时执行到 context 阶段的 QKV GEMM（batch=16），描述 `cublasMMWrapper::Gemm` 如何通过 [cublasMMWrapper.cc:182-192](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L182-L192) 调用 `cublas_algo_map_->isExist / getAlgo`，命中 batch=16 那次调优写下的记录，并根据 `stages != -1` 走 cuBLASLt 路径。
4. **思考兜底**：如果有人误删了 `gemm_config.in`，推理会发生什么？（对照 [cublasAlgoMap.cc:44-48](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.cc#L44-L48) 与 4.2.5 练习 1 给出准确回答。）

**交付物**：一份简短文档，含两条调优命令、文件行数估算、运行期命中链路的 3~4 句话描述、以及兜底行为的结论。

> 待本地验证：如有编译好的 FT 环境，实际执行步骤 1 的两条命令，用 `wc -l gemm_config.in` 与 `head gemm_config.in` 核对你的估算。

---

## 6. 本讲小结

- cuBLAS 对同一个 GEMM 提供多种算法（经典 `algoId` 或 cuBLASLt 的组合），**最优算法依赖 \((M,N,K,\text{data\_type})\) 形状**，因此需要按形状分别挑选。
- FT 采用 **离线调优 + 运行期查表**：`gemm_test` 工具（`gpt_gemm` 等）枚举模型所有 GEMM 形状、暴力计时挑最优、写入 `gemm_config.in`；运行期由 `cublasAlgoMap` 加载这张表供 `cublasMMWrapper` 查询。
- `cublasAlgoMap` 的核心是一个 `unordered_map<cublasAlgoConfig_t{batch,m,n,k,data_type}, cublasLtMatmulAlgo_info>`；`###` 之前是给人看的模型信息、之后是给机器读的算法字段；查不到时返回 `stages=-1` 的默认算法。
- `stages` 字段是运行期判别 **cuBLASLt vs 经典 cuBLAS** 的哨兵：`stages != -1` 走 cuBLASLt，否则走经典 cuBLAS（[cublasMMWrapper.cc:186](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasMMWrapper.cc#L182-L192)）。
- GPT 调优枚举 **11 个 GEMM 形状**（context 阶段 `i=0..5`、decoder 阶段 `i=6..9`、logits `i=10`），context 与 decoder 的 \(M\) 相差 `max_input_len` 倍，所以必须分别调优；改 batch/beam 也要重新调优或用 `is_append` 追加。
- FP16/BF16/FP8 在经典 cuBLAS 区间扫描之外，**还会用 `LtHgemmCustomFind` 在约 5000 个 cuBLASLt 组合里再搜一遍**，取两者更快者写入文件；FP32 只扫经典 cuBLAS 区间。

---

## 7. 下一步学习建议

本讲把「GEMM 算法选择」讲透了，接下来可以：

1. **进入模型层**：[u4-l1（BERT 模型与 forward 主流程）](u4-l1-bert-model.md) 会展示这些 GEMM 如何被串成一个完整的 transformer block，你会看到本讲的「gemm 0/3/4/5」等抽象形状对应到 BERT 里的 QKV/projection/FFN。
2. **看 INT8/FP8 量化版本的 GEMM**：[u9-l1（INT8 量化）](u9-l1-int8-quantization.md) 与 [u9-l3（FP8 推理）](u9-l3-fp8-inference.md) 会介绍 `cublasINT8MMWrapper`、`cublasFP8MMWrapper` 以及对应的 `igemm_config.in` 调优——它们与本讲是平行的另一套「算法映射」机制（见 [cublasAlgoMap.h:31](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/cublasAlgoMap.h#L31) 的 `IGEMM_CONFIG`）。
3. **阅读权重组件**：在进入模型前，建议先读 [u2-l5（权重容器与权重加载）](u2-l5-weight-containers.md)，把「算法怎么算」和「权重怎么存」两块基础设施一起补齐。
4. **想动手**：参考 [u1-l4（第一个示例）](u1-l4-first-run-examples.md) 的运行方式，实际编译并跑一次 `./bin/gpt_gemm`，亲眼看看生成的 `gemm_config.in`，把本讲的所有结论对号入座。
