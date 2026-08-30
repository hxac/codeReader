# 复杂算子案例三：SIFT、PairwiseMatcher、FindHomography

## 1. 本讲目标

本讲是第五单元「算子内部解剖」的收官之讲。前面几讲我们解剖了 OSD（状态最复杂的渲染型算子）与 HQResize/PillowResize（滤波型算子），本讲进入另一类完全不同的算子——**分析类算子**：它们的输入是图像或点集，输出却不是图像，而是特征、匹配对、变换矩阵等「数据」。学完后你应当能够：

1. 调用 `cvcuda.sift`、`cvcuda.match`、`cvcuda.findhomography` 三个算子，并把它们串成一条完整的「两视图配准」管线；
2. 解释分析类算子为何输出 Tensor（固定容量 + 计数张量）而非图像，理解「容量 + 有效数」这一输出契约；
3. 读懂三个原生 `.cu` 实现里多阶段 kernel 的流水关系：SIFT 的金字塔→极值→描述子三阶段、PairwiseMatcher 的暴力匹配 + CUB 块内归约、FindHomography 的归一化 DLT + cuSolver 批量特征分解 + Levenberg-Marquardt 精化。

## 2. 前置知识

阅读本讲前，你应当已从前面讲义了解以下概念（忘了可以回看）：

- **算子四层结构**（u5-l1）：Python 绑定层 → C API → priv 实现（`src/cvcuda/priv/`）→ CUDA kernel。本讲的三个算子都是**原生 .cu 形态**：kernel 直接写成 `OpXxx.cu` 编译单元内的自由 `__global__` 函数或类内模板，而非 `priv/legacy/` 下的历史内核。
- **exportData 与 TensorWrap**（u5-l2）：priv 层先用 `exportData<TensorDataStridedCuda>()` 把张量导出为设备视图，判空拦截非显存数据；kernel 侧再用 `cuda::TensorWrap` 系列做轻量寻址。
- **Stream 执行模型**（u4-l1）：所有 kernel 与 `cudaMemsetAsync` 都带 `stream` 参数异步提交。
- **Python 对象缓存**（u4-l2）：算子对象经 `CreateOperator` 走线程局部缓存；本讲会看到两种**截然不同的缓存键策略**。
- 少量线性代数：单应矩阵 \(H\) 是 3×3 射影变换；DLT（Direct Linear Transform）是最小二乘估计 \(H\) 的经典方法；Levenberg-Marquardt（LM）是非线性最小二乘的阻尼迭代法。

几个本讲新术语：

- **关键点/特征（keypoint/feature）**：图像中局部可重复检测的兴趣点，SIFT 用「高斯差分金字塔的极值点」定义它，并附带你一个 128 字节描述子。
- **DoG（Difference of Gaussians）**：两个不同尺度高斯模糊结果之差，近似拉普拉斯算子，是 SIFT 检测极值的载体。
- **描述子匹配**：把每个描述子看作 128 维空间中的一个点，「匹配」就是找距离最近的另一个点——这正是 PairwiseMatcher 的暴力（brute-force）语义。
- **单应性（homography）**：平面场景在两个视图间的射影变换，4 对点即可确定，更多点则做最小二乘。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/cvcuda/priv/OpSIFT.cu](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu) | SIFT 的 priv 实现与全部 CUDA kernel（金字塔、极值、描述子），1455 行 |
| [src/cvcuda/priv/OpPairwiseMatcher.cu](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPairwiseMatcher.cu) | 暴力匹配器的实现与 kernel（含 CUB 块内排序/归约） |
| [src/cvcuda/priv/OpFindHomography.cu](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu) | 单应估计：归一化 DLT + cuSolver + LM 精化的 kernel 与宿主编排 |
| [src/cvcuda/include/cvcuda/OpSIFT.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpSIFT.h) / [OpPairwiseMatcher.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpPairwiseMatcher.h) / [OpFindHomography.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFindHomography.h) | 三个算子的 C API 与 Limitations 支持契约表 |
| [python/mod_cvcuda/operators/OpSIFT.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpSIFT.cpp) / [OpPairwiseMatcher.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpPairwiseMatcher.cpp) / [OpFindHomography.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFindHomography.cpp) | 三个算子的 pybind11 绑定：输出张量的形状推导、算子缓存键 |
| [tests/cvcuda/python/test_opsift.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opsift.py) / [test_opmatch.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opmatch.py) / [test_opfindhomography.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opfindhomography.py) | 官方 Python 测试，展示了正确的调用姿势 |
| [samples/common.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py) | 综合实践要用的 `read_image` / `download_tensor` / `upload_tensor` 辅助函数 |

> **勘误提示**：学习大纲中提到 `samples/operators/sift.py`，该文件在当前仓库中**不存在**（`samples/operators/` 下没有 SIFT 示例）。本讲的实践任务改以官方测试与 `samples/common.py` 的辅助函数为依据，全部代码均可对照真实文件核对。

## 4. 核心概念与源码讲解

### 4.1 分析类算子的公共形态：图像进、数据出

#### 4.1.1 概念说明

前面解剖过的算子（resize、OSD、HQResize）都是「图像进、图像出」。而 SIFT、PairwiseMatcher、FindHomography 组成一条**分析管线**：

```
图像A、图像B ──SIFT──> 关键点 + 128B 描述子（每图一组）
                          │
                          └──PairwiseMatcher──> 匹配对 (i, j) + 距离
                                                    │
                                                    └──FindHomography──> 3×3 单应矩阵 H
                                                                              │
图像A + H ──warp_perspective──> 把 A 对齐到 B 的视角
```

这条链路的本质是：**数据体积逐级坍缩**（百万像素 → 数千特征 → 数百匹配 → 9 个浮点数），而语义逐级提升（像素 → 几何结构）。

分析类算子有一个共同的输出难题：**输出数量在调用前不可知**。一张图有多少 SIFT 特征、两集合间有多少通过交叉检验的匹配，都只有跑完 kernel 才知道。CV-CUDA 的解法是统一的「**容量 + 计数**」契约：

- 输出张量按调用者给定的**容量**（capacity）分配（如 `max_features`）；
- 另有一个小小的 **S32 计数张量**（如 `num_features`、`num_matches`）写出实际有效条数；
- kernel 内部用 `atomicAdd` 竞争槽位，超过容量就丢弃。

这也解释了「为什么输出是 Tensor 而不是 Array」——固定容量的稠密张量可以预先分配、可以被 Python 对象缓存按 shape 复用（呼应 u4-l2），而计数张量补上了「有效前缀」的语义。

另一个公共点是**非确定性**。三个算子的 C 头都声明输出顺序不保证确定（例如 [OpSIFT.h:67-70](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpSIFT.h#L67-L70) 指出特征总数可能超过容量上限、顺序因运行而异）：GPU 上成千上万线程并发 `atomicAdd` 抢占输出槽，谁先谁后无法预知。**做数值比对时必须先做排序或集合化处理**。

#### 4.1.2 核心流程

三个算子的 Limitations 契约表（各 C 头文件）汇总：

| 算子 | 输入 | 输出 | 关键限制 |
|------|------|------|----------|
| SIFT | `[HWC/NHWC/CHW/NCHW]` U8、**通道数=1** | `featCoords` N×M×4 F32、`featMetadata` N×M×3 F32、`featDescriptors` N×M×128 U8、`numFeatures` N S32 | 仅 U8 灰度图；输出最内维必须**打包**（stride 精确等于 4/3/128 字节） |
| PairwiseMatcher | `set1/set2` `[NMD]` rank-3，dtype U8/U32/F32 | `matches` N×M×2 S32、可选 `numMatches` N S32、`distances` N×M F32 | 输入不是图像（`Planar image layouts: Not applicable`）；cross_check 要求 matchesPerPoint=1 且必须给 numMatches |
| FindHomography | `srcPts/dstPts` `[NW]` 2F32 或 `[NWC]` F32(C=2) | `models` `[N,3,3]` F32 (NHW) | 点数 ≥4；最内维 stride 必须=8 字节 |

#### 4.1.3 源码精读

输出契约在 Python 绑定层最直观。看 SIFT 的 allocating 变体如何创建四个输出张量（[python/mod_cvcuda/operators/OpSIFT.cpp:196-207](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpSIFT.cpp#L196-L207)）：

```cpp
maxFeatures = maxFeatures == 0 ? GetDefaultMaxFeatures(...) : maxFeatures;

// Row align must be 1 in below tensors so last 2 dimensions are packed
auto featCoords      = Tensor::Create({{numSamples, maxFeatures, 4}, "NMC"}, nvcv::TYPE_F32, 1);
auto featMetadata    = Tensor::Create({{numSamples, maxFeatures, 3}, "NMC"}, nvcv::TYPE_F32, 1);
auto featDescriptors = Tensor::Create({{numSamples, maxFeatures, 128}, "NMD"}, nvcv::TYPE_U8, 1);
auto numFeatures     = Tensor::Create({{numSamples, 1}, "NC"}, nvcv::TYPE_S32, 1);
```

这段代码说明两件事：其一，`max_features` 缺省为 0 时按「**像素总数的 5%、至少 1**」取默认（[OpSIFT.cpp:184-188](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpSIFT.cpp#L184-L188) 中 `max(width * height / 20, 1)`）；其二，注释「Row align must be 1」点破了一个隐蔽契约——nvcv 张量默认会把行距对齐到设备对齐边界，而 priv 层会**逐项校验最内维 stride 必须精确打包**（后面 4.2.3 会看到 L1327-1336 的检查），所以这里显式传 1。

「容量 + 计数」中计数一侧的语义在 priv 校验里也能看到：SIFT 要求 `featCoords.shape(0)` 等于输入样本数、`shape(1)` 就是容量 `maxCapacity`（[src/cvcuda/priv/OpSIFT.cu:1320-1338](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L1320-L1338)），三个输出张量的第二维必须一致。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：用 Limitations 契约表预判调用是否合法，养成「写管线前先查表」的习惯。
2. **操作步骤**：
   - 打开 [OpSIFT.h:72-88](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpSIFT.h#L72-L88)、[OpPairwiseMatcher.h:67-90](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpPairwiseMatcher.h#L67-L90)、[OpFindHomography.h:59-85](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFindHomography.h#L59-L85)。
   - 回答三个问题：① 能否直接把 `read_image` 返回的 RGB8 张量喂给 `cvcuda.sift`？② 能否给 `cvcuda.match` 传 `cross_check=True, matches_per_point=2`？③ `findhomography` 的点数下限是多少？
3. **需要观察的现象**：三个表中 dtype 允许列的交集非常小，这正是管线上下游必须精确对接的原因。
4. **预期结果**：① 不能——SIFT 限制 Channels=[1]，必须先 `cvcuda.cvtcolor(code=cvcuda.ColorConversion.RGB2GRAY)`（该转换码真实存在于 [python/mod_cvcuda/ColorConversionCode.cpp:41](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/ColorConversionCode.cpp#L41)）；② 不能——priv 层 [OpPairwiseMatcher.cu:773-777](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPairwiseMatcher.cu#L773-L777) 显式拒绝；③ 4 对，[OpFindHomography.cu:1457-1461](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu#L1457-L1461)。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SIFT 的输出不直接做成「有多少特征就分配多大」？
**答案**：输出数量只有 kernel 跑完才知道，而 GPU 异步模型下调用返回时 kernel 可能还没执行。固定容量让输出张量可以在提交前分配（甚至被对象缓存复用），计数张量事后补上有效数；代价是超出容量的特征被丢弃（[OpSIFT.cu:855-864](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L855-L864) 里 `firstFeatIdx >= maxCapacity` 即跳过）。

**练习 2**：`cvcuda.match` 的输入为什么声明 `Planar image layouts: Not applicable`？
**答案**：它的输入是 NMD 描述子集合、输出是索引与距离，根本不是图像，没有「图像布局」可言；这是仓库对非图像算子的规范声明（AGENTS.md 的仓库不变量之一）。

### 4.2 SIFT：金字塔、极值与描述子的三阶段流水线

#### 4.2.1 概念说明

SIFT（Scale-Invariant Feature Transform）把「找特征」拆成三步：

1. **尺度空间金字塔**：对图像做逐倍降采样（octave），每个 octave 内再做多层不同 σ 的高斯模糊；相邻层相减得到 DoG 金字塔。DoG 近似尺度归一化的拉普拉斯响应，其沿尺度的极值就是「在某个尺度下特别醒目」的点：

   \[ D(x, y, \sigma) = L(x, y, k\sigma) - L(x, y, \sigma) \]

2. **极值检测与筛选**：每个像素与它 3×3×3 邻域的 **26 个邻居**（同层 8 + 上层 9 + 下层 9）比较，找出局部极大/极小；再做亚像素插值定位、对比度阈值与边缘响应剔除，最后用 36 桶梯度方向直方图给特征定主方向（一个极值点可能产生多个方向的特征）。
3. **描述子计算**：在特征邻域内统计旋转后的梯度直方图，4×4 个子区域 × 8 个方向桶 = **128 字节**描述子，归一化并截断后量化为 U8。

CV-CUDA 的实现把这三步映射成**三组 kernel + 一组宿主编排函数**，全部在 [OpSIFT.cu](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu) 一个文件里——这正是 u5-l3 所说的「原生内核形态」：kernel 是编译单元内的模板函数，直接使用 `cuda_tools` 的 TensorWrap/BorderWrap 与 `cuda::math` 线代工具。

#### 4.2.2 核心流程

`operator()` 的总编排（[OpSIFT.cu:1446-1452](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L1446-L1452)）：

```
校验输入/输出（布局、U8、单通道、容量、stride 打包）
   ↓
ReshapePyramids(inShape, numOctaves, numOctaveLayers)   ← 把"最大金字塔"裁成"本次运行金字塔"视图
   ↓
ComputePyramids<uint8_t>(...)                            ← 阶段一：构建高斯 + DoG 金字塔
   ↓
FindExtrema<uint8_t>(...)                                ← 阶段二+三：极值检测 + 描述子（交错执行）
```

关键常量（[OpSIFT.cu:54-81](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L54-L81)）：`kMaxKernelSize=59`（高斯核上限）、`kDescriptorOctaveBatch=2`（**每 2 个 octave 批量启动一次描述子 kernel**）、`kHistogramBins=36`（方向直方图桶数）、`kDescWidth=4`/`kDescHistBins=8`（描述子 4×4×8）、`kDescF32toU8Ratio=512`（float→U8 量化系数）。

octave 数量由图像尺寸推导（[OpSIFT.cu:116-119](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L116-L119)）：

```cpp
inline __host__ int ComputeNumberOfOctaves(int width, int height)
{
    return std::round((std::log2(std::min(width, height))) - 2) + 1;
}
```

即不断减半直到边长接近 4~8 像素为止。

**内存策略是本算子最值得学习的工程点**：构造函数按 `maxShape` 一次性分配「最大金字塔」（[OpSIFT.cu:1197-1216](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L1197-L1216) 调 [CreatePyramids，L1152-1193](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L1152-L1193)）；每次调用时 `ReshapePyramids`（[L1110-1150](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L1110-L1150)）不重新分配，而是通过 `GetViewFrom`（[L97-113](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L97-L113)）把大张量「裁剪」成当前尺寸的视图——源码注释（L1122-1124）明确说明丢弃视图不会释放父张量内存。这就是 Python 侧能缓存复用 SIFT 算子对象的物质基础。

#### 4.2.3 源码精读

**入口校验**（[OpSIFT.cu:1220-1253](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L1220-L1253)）：布局白名单 + U8 + 单通道单平面，是 u5-l1「priv 先校验后执行」套路的又一实例：

```cpp
if (!(in.layout() == nvcv::TENSOR_HWC || in.layout() == nvcv::TENSOR_NHWC
      || in.layout() == nvcv::TENSOR_CHW || in.layout() == nvcv::TENSOR_NCHW)) { throw ... }
...
if (inData->dtype() != nvcv::TYPE_U8) { throw ... "Input tensor dtype must be U8"; }
...
if (inAccess->numChannels() > 1 || inAccess->numPlanes() > 1) { throw ... }
```

注意 `NVCV_SIFT_USE_EXPANDED_INPUT` 标志（默认开启，见 [OpSIFT.cpp:221](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpSIFT.cpp#L221)）会把输入先 2 倍上采样（[OpSIFT.cu:1263-1272](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L1263-L1272)），以提高小特征检出率——这也是 Python 绑定在创建算子前先把 `inShape` 翻倍的原因（[OpSIFT.cpp:158-165](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpSIFT.cpp#L158-L165)）。

**阶段一：金字塔 kernel**。宿主函数 `ComputePyramids`（[OpSIFT.cu:972-1107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L972-L1107)）先做基座拷贝：`expandInput` 时用插值 wrap 做 2 倍上采样 `UpCopy`，否则 `Copy` 直接搬运并把 U8 转成 F32（L990-1008）；随后双重循环「octave × layer」，octave>0 的第 0 层用 `DownCopy` 隔行取数降采样（L1053），其余层计算当层 σ 对应的可分离高斯核（CPU 侧归一化，L1066-1085）后启动核心 kernel：

```cpp
DoComputePyramids<BW, BH><<<compBlocks, compThreads, smemSize, stream>>>(
    prevGaussBW, currGaussTW, currDoGTW, currShape, layer, ksize, gaussSepKernel);
```

`DoComputePyramids`（[L176-308](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L176-L308)）是教科书级的共享内存滤波：源码头部的注释（L182-186）精确记录了资源占用——32×32 数据瓦片 + 核支撑 halo 的 SMEM 约 44.7KB。它分四步：带边界处理地加载瓦片（用 `BorderWrapLNHW` 处理 halo，L211-220）→ 水平卷积 → 垂直卷积 → 同一脚写回高斯结果并顺手算出 DoG（[L297-306](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L297-L306)）：

```cpp
currGauss[gc] = result;          // 当前层高斯
if (gc.w > 0) {
    gc.w -= 1;
    currDoG[gc] = result - gaussIn[pc];   // DoG = 本层 - 上一层
}
```

金字塔的层级冗余也有注释可查（L233-236、L263-266）：高斯金字塔比 DoG 多一个「基座 octave」，DoG 的每层等于高斯金字塔相邻层之差，两端各多留 2 层给极值检测当上下邻居。

**阶段二：极值 kernel**。宿主 `FindExtrema`（[OpSIFT.cu:890-970](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L890-L970)）先把 `numFeatures` 清零（L918），再逐 octave 启动 `DoFindExtrema`（L953）。kernel（[L576-881](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L576-L881)）每个线程负责一个像素位置，贯穿全部中间层（`gc.w` 从 1 到 numOctaveLayers，L617），先做 26 邻居极值判定（[L621-654](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L621-L654)，两串手写展开的比较链），然后：

- **亚像素定位**：解 3×3 线性系统做二次曲面拟合（注释 L658-662），用 `cuda::math::solve_inplace`（[L691](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L691)），步长小于半像素才算收敛（L698-702），最多迭代 5 步；
- **两个剔除器**：对比度阈值（L729-733）与基于 Hessian 迹和行列式的边缘剔除（L737-743，即 \(\text{tr}^2 \cdot r \ge (r+1)^2 \cdot \det\) 判据）；
- **方向直方图**：36 桶梯度方向统计、循环平滑、找峰（L753-847），峰值 80% 以上的次峰也生成特征（`kHistogramPeakRatio=0.8`）；
- **竞争输出槽**：`atomicAdd(numFeatures.ptr(ic.z), validAngles)`（[L854](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L854)）拿起始下标，随后写入坐标四元组 `(x, y, octave, layer)`（换算回原图尺度）与三元组元数据 `(angle, score, diameter)`（[L872-876](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L872-L876)）。

**阶段三：描述子 kernel**。它不是单独一遍跑完，而是**每 2 个 octave 与极值检测交错启动一次**（`kDescriptorOctaveBatch=2`，宿主侧 [L957-965](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L957-L965)：`if (descriptorOctaveIndex == kDescriptorOctaveBatch - 1 || octave == numOctaves - 1)`），`compBlocks2(maxCapacity, 1, currShape.z)` 即 **blockIdx.x=特征下标、blockIdx.z=样本**（L905），一次 kernel 覆盖两个 octave 的特征。`DoComputeDescriptors`（[L323-573](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L323-L573)）每个 block 处理一个特征：先按特征所属 octave 选择金字塔数据（L354-369），把邻域加载进 SMEM（L389-400），然后在旋转坐标系里做**三线性插值投票**——每个邻居像素的梯度幅值按 (行, 列, 方向) 三个小数分量分配到 8 个相邻直方图桶，用 8 次 `atomicAdd`（[L490-497](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L490-L497)）；最后单线程完成环形边界补全、L2 归一化、0.2 截断（迟滞阈值，L528-551）与 ×512 量化到 U8（[L555-569](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L555-L569)）。

#### 4.2.4 代码实践（示例代码，待本地验证）

1. **实践目标**：跑通最小 SIFT 调用，观察「容量 + 计数」契约的实际数值。
2. **操作步骤**：在有 GPU 与 cvcuda wheel 的环境运行下面脚本（依据官方 [test_opsift.py:38-61](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opsift.py#L38-L61) 改写，标注为示例代码）：

```python
import numpy as np, cvcuda
from common import download_tensor   # samples/common.py，需把 samples 目录加入 sys.path

# 造一张 256x256 的渐变+圆点图（HWC U8 单通道）
x = np.arange(256, dtype=np.float32)
img = np.outer(x, x * 0.5) % 255
yy, xx = np.mgrid[0:256, 0:256]
img[(xx - 128) ** 2 + (yy - 128) ** 2 < 40 ** 2] = 255
src = cvcuda.Tensor((256, 256, 1), np.uint8, "HWC")   # 示例代码；生产中用 read_image 读图

feat_coords, feat_meta, feat_desc, num_feat = cvcuda.sift(src, max_features=5000)
print("容量:", feat_coords.shape)          # 预期 (1, 5000, 4) —— 注意 HWC 输入被当作 N=1
print("实际特征数:", download_tensor(num_feat))  # S32 计数张量
print("描述子 dtype/形状:", feat_desc.dtype, feat_desc.shape)  # U8, (1, 5000, 128)
```

3. **需要观察的现象**：`num_feat` 的值远小于容量 5000，且**两次运行可能不同**（非确定性）；`feat_coords` 的前 `num_feat` 行才是有效前缀。
4. **预期结果**：四元组坐标打印出 `(x, y, octave, layer)`，元数据为 `(angle, score, diameter)`。本环境无 GPU，**待本地验证**。
5. 若无 GPU 环境，替代方案是运行 `pytest tests/cvcuda/python/test_opsift.py -k api`（仍需 GPU），或纯阅读 4.2.3 的三段源码。

#### 4.2.5 小练习与答案

**练习 1**：为什么描述子 kernel 的网格用 `maxCapacity` 作为 gridDim.x，而不是实际特征数？
**答案**：启动 kernel 时特征数还在 GPU 上、宿主不可见（异步模型）。所以按容量上限铺满网格，block 内用 `if (featIdx >= numFeatures[sampleIdx]) return;` 提前退出（[OpSIFT.cu:346-352](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L346-L352)）。

**练习 2**：`maxShape` 为 (640, 480, 4) 的 SIFT 算子能处理 (320, 240, 4) 的输入吗？能处理 (1280, 960, 4) 吗？
**答案**：能、不能。构造时的金字塔按 maxShape 分配，小输入只是取小视图；超过 maxShape 则被 [OpSIFT.cu:1274-1281](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L1274-L1281) 拒绝。Python 缓存键也按此语义兼容（见 4.4.3 的对比讨论）。

**练习 3**：描述子为什么截断到最大值 0.2×范数后再归一化一次？
**答案**：抑制偶然的强梯度主导整个向量，提高光照鲁棒性；对应源码 [OpSIFT.cu:528-551](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpSIFT.cu#L528-L551) 的两轮循环（先算 `histMax = sqrt(norm) * 0.2` 截断，再重新累计范数）。

### 4.3 PairwiseMatcher：一个 block 一个点的暴力匹配

#### 4.3.1 概念说明

匹配器的语义极简：对 set1 中的每个点 \(p\)，在 set2 中找距离最近的点。距离按 `norm_type` 计算，支持三种（[OpPairwiseMatcher.cu:241-260](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPairwiseMatcher.cu#L241-L260)）：

- **HAMMING**：按位异或数 1 的个数（`__popc`），给 ORB 这类二进制描述子用，输入必须是整型；
- **L1**：绝对差之和；
- **L2**：平方差之和（比较时不开方，写出时才开方——经典的「推迟开方」优化，L256-259 注释）。

SIFT 的 128B U8 描述子用 **L2**（这也是 Python 绑定的默认值，[OpPairwiseMatcher.cpp:45-48](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpPairwiseMatcher.cpp#L45-L48)）。

「暴力（brute-force）」意味着复杂度 \(O(|S_1| \times |S_2| \times D)\)，没有任何近似。它之所以在 GPU 上可行，靠的是完美的并行划分：**一个 block 负责一个查询点**，block 内 64 线程分头扫描 set2、块内合并出 top-N。

`cross_check`（交叉检验）是提高匹配质量的关键开关：\(p_1 \to p_2\) 只有在 \(p_2 \to p_1\) 也互为最近邻时才算匹配。它过滤掉大量「错误吸引」的匹配——这正是后面单应估计需要的干净输入。

#### 4.3.2 核心流程

kernel 划分（[OpPairwiseMatcher.cu:385-396](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPairwiseMatcher.cu#L385-L396)）：

```
grid = (numSamples, set1Capacity, 1)   ← blockIdx.x=样本，blockIdx.y=set1 中的点
block = (64, 1, 1)                     ← kNumThreads = 64
```

单个 block 的算法：

```
p ← 加载 set1[blockIdx.y]（可能整点缓存在寄存器）
每个线程 strided 扫描 set2，维护本地 (bestDist, bestIdx)
   ↓ matchesPerPoint == 1 → cub::BlockReduce + 自定义 minkey 归约出全局最近（快路径）
   ↓ matchesPerPoint  > 1 → cub::BlockRadixSort 块内排序取前 N
cross_check ? 反向再扫一遍 set1，仅当互为最近才 atomicAdd(numMatches) 后写出
            : 直接按 set1Idx * matchesPerPoint + threadIdx.x 的确定性下标写出
```

一个漂亮的微优化是 `PackedU8Point32`（[L120-201](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPairwiseMatcher.cu#L120-L201)）：32 字节 ORB 描述子存成 8 个**独立命名的** `uint32_t` 成员而不是数组，源码注释（L120-121）解释道——直接索引数组会让 nvcc 把缓存寄存器溢出到 local memory，而按名字访问（模板递归 `word<I>()`，L262-273）编译器能把整个描述子钉在寄存器里逐字算 Hamming。

#### 4.3.3 源码精读

**缓存特化分派**是宿主侧最有讲头的部分（[OpPairwiseMatcher.cu:549-586](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPairwiseMatcher.cu#L549-L586)）。注释直言这套特化「适用于 32B 与 128B 描述子，如 ORB 与 SIFT」，代价是编译慢（约 30 秒）：

```cpp
if (isCompatible<SrcT, 32>(numDim))       { CVCUDA_BFM_RUN(32); }   // 点 ≤32B：寄存器缓存
else if (isCompatible<SrcT, 128>(numDim)) { CVCUDA_BFM_RUN(128); }  // 点 ≤128B：SIFT 走这里
...
CVCUDA_BFM_RUN(0);   // 兜底：不缓存，逐元素直读 GMEM
```

即**同一个算法按数据大小编译出三个版本**，运行期按 `numDim` 与 stride 条件选择。外层还有两级分派：dtype（U8/U32/F32，[L628-650](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPairwiseMatcher.cu#L628-L650)）与范数（含「Hamming 配浮点输入直接抛异常」的编译期防御，[L598-609](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPairwiseMatcher.cu#L598-L609)）——三重模板分派构成一个 dtype × norm × 缓存大小的实例立方体。

**cross_check 分支**（[L426-453](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPairwiseMatcher.cu#L426-L453)）复用了同一个 `SortKeyValue`：先正向找 set1→set2 最近邻，把结果下标通过共享变量广播（L428-435），再**反向**扫 set1 验证互异性：

```cpp
SortKeyValue<NORM>(dist2, set1Idx2, p, set1, numDim, matchesPerPoint, sampleIdx, set1Size);
if (threadIdx.x == 0 && set1Idx2 == set1Idx) {        // 互为最近邻才有效
    int matchIdx = atomicAdd(numMatches.ptr(sampleIdx), 1);   // 数量不定 → 计数竞争
    if (matchIdx < outCapacity) { WriteMatch<NORM>(...); }
}
```

与不带交叉检验的分支（[L456-464](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPairwiseMatcher.cu#L456-L464)，下标确定性、数量固定，由 `WriteNumMatches` kernel 单独填写数量，[L468-476](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPairwiseMatcher.cu#L468-L476)）对照，可以清楚看到「**输出数量是否可预知**」直接决定了写出策略：可预知→直接寻址；不可预知→`atomicAdd` 计数 + 容量截断。这与 SIFT 的输出契约完全同构。

Python 侧 allocating 变体的容量推导（[OpPairwiseMatcher.cpp:100-122](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpPairwiseMatcher.cpp#L100-L122)）：`maxMatches = max(set1.shape[1], set2.shape[1]) * matchesPerPoint`，matches 形状 `[N, maxMatches, 2]`（"NMA"）。

#### 4.3.4 代码实践（示例代码，待本地验证）

1. **实践目标**：量化 `cross_check` 对匹配数量的过滤作用。
2. **操作步骤**（示例代码，改写自 [test_opmatch.py:111-131](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opmatch.py#L111-L131)）：

```python
import numpy as np, cvcuda
from common import upload_tensor, download_tensor

rng = np.random.default_rng(0)
M, D = 200, 32
s1 = rng.integers(0, 255, (1, M, D), dtype=np.uint8)
s2 = np.concatenate([s1[:, :50], rng.integers(0, 255, (1, M - 50, D), dtype=np.uint8)], axis=1)
# 前 50 个点两集合完全相同 → 理想内点；其余是随机干扰

set1 = cvcuda.Tensor((1, M, D), np.uint8, "NMD")
set2 = cvcuda.Tensor((1, M, D), np.uint8, "NMD")
upload_tensor(s1, set1); upload_tensor(s2, set2)

m_plain, _, _ = cvcuda.match(set1, set2, num_matches=True)
m_cross, n_cross, _ = cvcuda.match(set1, set2, num_matches=True, cross_check=True)
print("无交叉检验 numMatches =", download_tensor(m_plain if False else n_cross))  # 见下方说明
```

> 说明：`cvcuda.match` 返回 `(matches, num_matches, distances)` 三元组，后两者可为 None；上例两次调用的计数应分别接住再对比（示例代码从简，请自行分别接收）。

3. **需要观察的现象**：无交叉检验时匹配数固定为 `min(M, M) * 1`；开交叉检验后应接近 50（只有重复的那段点能互为最近邻）。
4. **预期结果**：交叉检验数 < 无交叉检验数，且前 50 条 matches 的 `(i, j)` 恰为 `(k, k)`。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 L2 距离比较时不开平方根？
**答案**：开方是单调变换，不改变最近邻的排序；推迟到 `WriteMatch` 写出时才做（[OpPairwiseMatcher.cu:374-382](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPairwiseMatcher.cu#L374-L382)），把每点 O(|S2|) 次开方降到每匹配 1 次。

**练习 2**：`matches_per_point=2` 时为什么必须用块内排序而不能用归约？
**答案**：`BlockReduce` 只能合并出单个最小值；top-2 需要完整次序信息，故用 `cub::BlockRadixSort` 对 64 个 `(dist, idx)` 对排序后取前 N（[L347-363](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPairwiseMatcher.cu#L347-L363)）。这也是 `matchesPerPoint` 上限为 64（线程数）的原因（[L768-772](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPairwiseMatcher.cu#L768-L772)）。

### 4.4 FindHomography：归一化 DLT、cuSolver 特征分解与 LM 精化

#### 4.4.1 概念说明

单应矩阵 \(H\) 把源平面点映到目标平面点（齐次坐标）：

\[
\begin{pmatrix} u \\ v \\ w \end{pmatrix} = H \begin{pmatrix} x \\ y \\ 1 \end{pmatrix},
\qquad (u', v') = \left(\tfrac{u}{w}, \tfrac{v}{w}\right), \qquad H \in \mathbb{R}^{3\times 3},\ h_{22}=1
\]

给定 \(K \ge 4\) 对点，估计 \(H\) 的经典路线是 **DLT**：每对点贡献两个关于 \(h\)（H 展平成 9 维、去掉固定 h₂₂ 后 8 个自由度）的线性方程，最小二乘解 \( \min_h \|L h\|^2 \) 等价于求 \(L^\top L\) **最小特征值对应的特征向量**。原始 DLT 数值病态（坐标大时 \(L^\top L\) 条件数爆炸），所以先做 **Hartley 归一化**：平移使点集质心在原点、缩放使平均距离为 \(\sqrt2\)，估计完再反归一化。

但 8 参数的线性解只是初值——重投影误差才是真正想最小化的目标，而它对 \(h\) 是**非线性**的（分母 \(w\) 里有 \(h\)）。所以实现接着做 **Levenberg-Marquardt 精化**：每轮解阻尼正规方程

\[
(J^\top J + \lambda\,\mathrm{diag}(J^\top J))\,\delta = -J^\top r
\]

其中 \(r\) 是重投影残差、\(J\) 是解析雅可比（源码手推写出，[L277-296](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu#L277-L296)），按增益比在 \(R_{lo}=0.25\)、\(R_{hi}=0.75\) 区间调节 \(\lambda\)。

**与 OpenCV 的关键差异**：这里没有 RANSAC。`findhomography` 是**纯最小二乘估计器**——喂给它的点必须已经筛过（这正是上一节 cross_check 的价值）；混入离群点会均匀地拉偏结果。

#### 4.4.2 核心流程

宿主编排函数 `FindHomographyWrapper`（[OpFindHomography.cu:1323-1432](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu#L1323-L1432)）是一条五拍流水，全部提交到同一 stream：

```
① 一批 cudaMemsetAsync 清零工作缓冲（L1347-1357）
② compute_src_dst_mean × 2：算 src/dst 各自质心、再算归一化位移和（L1359-1373）
③ compute_LtL：45 个线程各算 L^T L 的一个上三角元素（9×10/2=45，L1379-1382）
④ cusolverDnSsyevjBatched：批量 9×9 对称特征分解，得特征值 W 与特征向量（L1392-1396）
⑤ computeModel：最小特征向量 → 反归一化 → 初值；若点数 > 4 再做 LM 迭代精化（L1423-1431）
```

缓冲区全部来自算子**构造时**一次性分配的 `DeviceState`（[L1508-1563](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu#L1508-L1563)）：均值/位移各 `batch` 个 float2、`LtL` 81×batch、`r` 与 `J` 按 `maxNumPoints` 分配，外加 cuSolver 句柄、syevj 参数（容差 1e-7、最多 15 次 sweep）与工作区。析构 `cleanup()`（[L1570-1606](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu#L1570-L1606)）逐项释放。

`computeModel` 的 block 尺寸还有一个值得咀嚼的细节（[L1423-1427](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu#L1423-L1427)）：block.x 按残差工作量 `2*numPoints` 在 32/64/128/256 里挑——注释说明这是为了让小点集别占着多余 warp，好让更多独立批次同时驻留 SM。**一个 block 拥有一个模型（batch）**。

#### 4.4.3 源码精读

**入口校验**（`RunFindHomography`，[L1434-1492](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu#L1434-L1492)）浓缩了输入契约：rank 2 或 3、src/dst/models 批数一致、**点数 ≥4**（L1457-1461）、models 必须是 `[N,3,3]` F32、坐标最内维 **stride 精确等于 `sizeof(float2)`**（L1481-1485）。两种合法表示：`[N, 2K]` + `2F32` 打包类型，或 `[N, K, 2]` + F32。

**残差与解析雅可比**（`calculate_residual_and_jacobian_device`，[L219-298](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu#L219-L298)）先做射影映射：

```cpp
float ww = h[6] * Mx + h[7] * My + 1.;
ww       = fabs(ww) > FLT_EPSILON ? 1. / ww : 0;   // 防除零
float xi = (h[0] * Mx + h[1] * My + h[2]) * ww;
float yi = (h[3] * Mx + h[4] * My + h[5]) * ww;
errptr[tid * 2]     = xi - mx;                     // 重投影残差
```

随后手推填出 2K×8 雅可比的每一列（L277-296，含分母 \(w^2\) 项）。

**块内 8×8 线代**：LM 每轮要求解 8×8 线性方程组。kernel 里没有调库，而是自带一套 device 实现——`backsolve_inplace`（L661）、基于 QR 的 `solve8x8`（L677）、`invert8x8`（L699），配合 `calculate_JtJ`/`calculate_Jtr`（声明见 L223-228）在 block 内协同归约正规方程。这就是把「每批一个小矩阵问题」彻底 GPU 化的写法：**cuSolver 只负责批量特征分解这一个真正需要库的环节**（[L1392-1396](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu#L1392-L1396)）：

```cpp
CUSOLVER_CHECK_ERROR(cusolverDnSsyevjBatched(
    cusolverH, CUSOLVER_EIG_MODE_VECTOR, CUBLAS_FILL_MODE_LOWER,
    9, LtL, 9, W, cusolverBuffer, lwork, cusolverInfo, syevj_params, batchSize), ...);
```

注意它先 `cusolverDnSetStream` 绑定到算子的 stream（L1393）——库调用也遵守 CV-CUDA 的流式执行模型。

**computeModel kernel**（[L949-1276](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu#L949-L1276)）入口先取最小特征值对应的特征向量做初值并反归一化（`compute_model_estimate` 调用与 `T⁻¹HT` 反归一化乘法见 [L987](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu#L987) 与 [L859-916](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu#L859-L916)，最后整除 \(h_{22}\) 归一）。然后是本算子最妙的一行条件（[L988](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu#L988)）：

```cpp
int ret = compute_model_estimate(cM, cm, sM, sm, W, V, x, model, batch, numPoints);
if (!(ret || numPoints == 4)) {   // 恰好 4 点时解精确，无需精化
    ... // LM 迭代：lambda=1, maxIters=10, epsx/epsf=FLT_EPSILON 量级（L1050-1054）
```

**两种 Python 缓存键策略的对比**（呼应 u4-l2）：SIFT 的缓存键问「我装得下吗」（`canBeUsedWith` 允许大算子服务小请求，[OpSIFT.cpp:50-54](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpSIFT.cpp#L50-L54)，fetch 时挑 payload 最大的，[L99-125](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpSIFT.cpp#L99-L125)）；FindHomography 的缓存键却是**严格相等**（[OpFindHomography.cpp:40-69](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFindHomography.cpp#L40-L69)）——文件头注释（L40-46）解释了原因：DeviceState 的 `r`/`J` 等缓冲按 `(batchSize, maxNumPoints)` 精确分配，复用更大的算子会让 memset 与 kernel 写越界。**「能不能复用」不是缓存层的偏好，而是由算子内部缓冲的分配语义决定的**——这是二次开发新算子时最容易踩的坑。

顺带一提，这个绑定还暴露了 `get_findhomography_operator` / `findhomography_into_with_op`（[L210-223](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFindHomography.cpp#L210-L223)、[L321-360](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFindHomography.cpp#L321-L360)）：把算子装进 capsule 由调用者长期持有，绕开缓存查找，用于消除重复调用时的双峰耗时——一个绑定层为基准性能开后门的真实案例。

#### 4.4.4 代码实践（示例代码，待本地验证）

1. **实践目标**：用已知真值 H 生成合成点，验证估计器能把 H 找回来。
2. **操作步骤**（示例代码；点张量采用官方测试验证过的 `(N, 2K)` + `2F32` 打包形式，[test_opfindhomography.py:33-38](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opfindhomography.py#L33-L38)）：

```python
import numpy as np, cvcuda
from common import download_tensor

H_true = np.array([[1.1, 0.02, 12.0],
                   [-0.01, 0.95, -8.0],
                   [2e-5, -1e-5, 1.0]], dtype=np.float64)
rng = np.random.default_rng(0)
pts = rng.uniform(0, 500, (64, 2))                    # 64 个源点
proj = pts @ H_true[:2, :2].T + H_true[:2, 2]
w = pts @ H_true[2, :2] + 1.0
dst = proj / w[:, None]                               # 射影映射到目标点

src_t = cvcuda.Tensor((1, 64 * 2), cvcuda.Type._2F32, "NW")
dst_t = cvcuda.Tensor((1, 64 * 2), cvcuda.Type._2F32, "NW")
# 填充设备张量：可用 samples/common.py 的 upload_tensor（dtype 字节数须一致，见其实现）
H_est_t = cvcuda.findhomography(src_t, dst_t)
H_est = download_tensor(H_est_t).reshape(3, 3)
print("H_est / H_est[2,2]:\n", H_est / H_est[2, 2])   # 归一化后与 H_true 对比
```

3. **需要观察的现象**：`H_est / H_est[2,2]` 各元素与 `H_true` 的偏差应在 1e-3 量级以内（无噪声合成点的最小二乘解）。
4. **预期结果**：估计矩阵与真值几乎一致；若把其中 10 对点改成随机值再跑，结果会被明显拉偏——验证「无 RANSAC、对离群点敏感」。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `compute_LtL` 的网格 `grid.y` 恰好是 45？
**答案**：\(L^\top L\) 是 9×9 对称矩阵，上三角含对角线共 9×10/2 = 45 个独立元素，一个线程算一个（[L1379-1382](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu#L1379-L1382)），天然免同步。

**练习 2**：为什么恰好在 `numPoints == 4` 时跳过 LM 精化？
**答案**：4 对点恰好提供 8 个方程解 8 个自由度，DLT 解是精确解、残差为零，精化无事可做（[L988](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu#L988) 的 `ret || numPoints == 4`）。

**练习 3**：TensorBatch 变体的 `operator()`（[L1653-1689](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFindHomography.cu#L1653-L1689)）与 Tensor 变体共享同一份 `RunFindHomography`，这样做的条件是什么？
**答案**：批内每张点张量的 numPoints 不得超过构造时的 maxNumPoints——Python 绑定在 VarShape 入口取批内最大点数建算子（[OpFindHomography.cpp:165-175](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFindHomography.cpp#L165-L175)），否则 DeviceState 缓冲会写越界（同 4.4.3 的严格相等缓存键问题）。

## 5. 综合实践

把三个算子串成完整的「两视图配准」管线——这是本讲的收官任务，对应大纲指定的实践：**对同一场景的两张视角不同的图片依次执行 SIFT → PairwiseMatcher → FindHomography，得到单应矩阵，再用 warp_perspective 对齐两图并保存叠加效果图**。

前置：GPU + cvcuda wheel + nvimgcodec（samples 依赖），两张有视角差、有重叠内容的照片（如手机对同一桌面平移拍摄两张）。把工作目录切到 `samples/` 下运行以便 `from common import ...`（与 [resize.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/resize.py#L28) 同款导入方式）。

以下为**示例代码**骨架（各步 API 均可对照前文引用的绑定与测试核实；整体运行结果**待本地验证**）：

```python
import sys, numpy as np, cvcuda
from common import read_image, write_image, download_tensor, upload_tensor

imgA = read_image("viewA.jpg")            # RGB8 HWC 张量（samples/common.py:355-371）
imgB = read_image("viewB.jpg")

grayA = cvcuda.cvtcolor(imgA, code=cvcuda.ColorConversion.RGB2GRAY)   # SIFT 要求单通道
grayB = cvcuda.cvtcolor(imgB, code=cvcuda.ColorConversion.RGB2GRAY)

# ① SIFT：描述子形状 (1, M, 128) U8 —— 正是 match 想要的 NMD rank-3 输入
coordsA, metaA, descA, numA = cvcuda.sift(grayA, max_features=8192)
coordsB, metaB, descB, numB = cvcuda.sift(grayB, max_features=8192)

# ② 匹配：交叉检验过滤错配；num_set1/2 是 S32 计数张量，形状 [N,1] 满足 "[NC]" 约定
matches, num_matches, dists = cvcuda.match(
    descA, descB, num_set1=numA, num_set2=numB,
    num_matches=True, distances=True, cross_check=True)

# ③ CPU 侧收集匹配点对（匹配数量只在设备上可知，必须下载后处理）
m  = download_tensor(matches)[0]          # (maxMatches, 2) 索引对
k  = int(download_tensor(num_matches)[0]) # 有效匹配数
cA = download_tensor(coordsA)[0]          # (M, 4)：x, y, octave, layer
cB = download_tensor(coordsB)[0]
idx = m[:k]
src_xy = cA[idx[:, 0], :2].astype(np.float32)   # A 图中的源点
dst_xy = cB[idx[:, 1], :2].astype(np.float32)   # B 图中的目标点

# ④ 单应估计：点张量用官方测试验证过的 (N, 2K)+2F32 打包形式（test_opfindhomography.py:34）
srcPts = cvcuda.Tensor((1, 2 * k), cvcuda.Type._2F32, "NW")
dstPts = cvcuda.Tensor((1, 2 * k), cvcuda.Type._2F32, "NW")
upload_tensor(src_xy.reshape(1, -1), srcPts)    # 字节数一致即可，见 upload_tensor 实现
upload_tensor(dst_xy.reshape(1, -1), dstPts)
H = cvcuda.findhomography(srcPts, dstPts)       # (1, 3, 3) F32

# ⑤ 用 H 把 A 对齐到 B 的视角：warp_perspective 的 Tensor 入口收 numpy 3x3（OpWarpPerspective.cpp:143-160）
H_np = download_tensor(H)[0]                    # H: A→B 的正向映射
warpedA = cvcuda.warp_perspective(
    imgA, xform=H_np, flags=cvcuda.Interp.LINEAR | cvcuda.Interp.WARP_INVERSE_MAP,
    border_mode=cvcuda.Border.CONSTANT, border_value=np.array([0, 0, 0], dtype=np.uint8))
write_image(warpedA, "aligned_A.png")
```

**必须想清楚的三个点**（也是检查你有没有读懂本讲的试金石）：

1. **为什么 `WARP_INVERSE_MAP`**：`warp_perspective` 默认把 `xform` 解释为「输出像素 → 输入采样坐标」的逆映射（[OpWarpPerspective.cpp:151-153](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpWarpPerspective.cpp#L151-L153)）。我们手里的 H 是 A→B 正向映射，直接传它必须加逆映射标志；否则要先 `np.linalg.inv(H_np)`。
2. **容量与有效前缀**：`descA` 的形状是容量 8192 而非实际特征数；不传 `num_set1/num_set2` 会让 matcher 把 8192 行全当有效点（内含未初始化数据），匹配质量崩塌。priv kernel 里 `numSet` 超容量会被钳制（[OpPairwiseMatcher.cu:398-415](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpPairwiseMatcher.cu#L398-L415)），但**小于容量时以计数为准**。
3. **没有 RANSAC**：第 ④ 步是无鲁棒性的最小二乘，全靠第 ② 步交叉检验保 purity。可做的增强：用下载回来的 `dists` 做距离比筛选（最近/次近 < 0.8，需要 `matches_per_point=2` 且关掉 cross_check）后再喂给 findhomography，对比对齐效果。

**验收标准**：`aligned_A.png` 中场景结构应与 `viewB.jpg` 基本重合（平移/透视畸变被校正）；把两图各取 50% 透明度叠加保存，重叠区域错位应小于数个像素。若对齐失败，依次排查：特征数是否为 0（图太素或 `contrast_threshold` 太高）、匹配数是否过少（视角差太大）、H 是否退化（`H[2,2]` 接近 0 或分母 w 过零）。

## 6. 本讲小结

- **分析类算子的输出契约是「容量 + 计数」**：固定容量张量保证可预分配、可缓存复用，S32 计数张量补上有效前缀；输出顺序非确定，比较前必须排序或集合化。
- **SIFT 是三阶段 kernel 流水线**：金字塔（SMEM 瓦片可分离高斯 + DoG 同步算出）→ 26 邻居极值（atomicAdd 竞争输出槽）→ 128B 描述子（每 block 一个特征、三线性直方图投票，每 2 个 octave 与极值交错启动）；金字塔内存在构造期按 maxShape 一次分配、运行期只取视图。
- **PairwiseMatcher 用「一个 block 一个查询点」的划分把暴力匹配 GPU 化**：块内 64 线程 strided 扫描 + CUB BlockReduce/BlockRadixSort 取 top-N；按数据宽度 0/32/128B 三级模板特化，32B ORB 描述子有寄存器友好的 `PackedU8Point32` 专门优化；cross_check 反向复扫一遍，把「数量可预知的确定性写出」变成「计数竞争写出」。
- **FindHomography = Hartley 归一化 DLT + cuSolver 批量 9×9 特征分解 + 手写块内 8×8 线代做 LM 精化**；恰好 4 点时跳过精化；没有 RANSAC，离群点靠上游匹配质量消化。
- **cuSolver 调用也绑定算子 stream**（`cusolverDnSetStream`），库调用不破坏 CV-CUDA 的流式执行模型。
- **Python 算子缓存的复用语义由算子内部缓冲决定**：SIFT「大算子可服务小请求」，FindHomography「严格相等否则越界」——设计新算子的 workspace 时就要想清楚它属于哪一类。

## 7. 下一步学习建议

- **第六单元（u6-l1/u6-l2）**：回到 C/C++ 侧，用 `cvcudaSIFTCreate/Submit/Destroy` 等句柄式 C API 重写本讲的综合实践，体会「capacity + 计数」契约在 C 侧如何手工管理。
- **u7-l4（NVTX 与性能分析）**：用 nsys 观察 SIFT 的多阶段 kernel 在时间线上的交错模式（每 2 个 octave 一次描述子启动），找出金字塔阶段的带宽瓶颈——分析类算子 kernel 多而短，是最适合练 profiling 的对象。
- **u8-l3（Workspace 与 per-stream 缓存）**：对照 SIFT 的「maxShape 金字塔 + 视图重塑」与 FindHomography 的「DeviceState 精确分配」，思考算子持有状态的两种范式与缓存键的耦合关系。
- 想继续读分析类算子源码的读者，可以顺着 [src/cvcuda/priv/](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv) 里其他原生 `.cu`（如 `OpPillowResize.cu`、`OpHQResize.cu`，见 u5-l5）比较「图像出」与「数据出」两类算子在 kernel 组织上的差异。
