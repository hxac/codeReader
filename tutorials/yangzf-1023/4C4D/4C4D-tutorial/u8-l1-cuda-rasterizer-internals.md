# u8-l1 CUDA 光栅化器内部实现

## 1. 本讲目标

本讲从 Python 侧的 `_C.rasterize_gaussians_4d` 再往下一层，钻进 `diff-gaussian-rasterization/cuda_rasterizer/` 的纯 CUDA 实现。学完本讲你应该能够：

1. 按执行顺序说出 CUDA 前向渲染的六个阶段：逐高斯预处理 → 前缀和 → 键值复制 → 基数排序 → tile 区间划分 → 分块 alpha blending。
2. 精确定位 `ts`、`scales_t`、`rotations_r` 这三个 4D 属性在 CUDA 侧被消费的位置，并解释时间边缘化（Schur 补 + `marginal_t` + 均值偏移）在核函数中如何完成。
3. 解释反向传播中不透明度梯度的完整链路：单像素从后往前的递推求 \( \partial L/\partial \alpha_i \)，经 `atomicAdd` 跨像素累加成 \( \partial L/\partial o_{\text{eff}} \)，再乘 `marginal_t` 折算回原始不透明度 \( o \)。

本讲依赖 u4-l2 建立的五层架构认知（render() 业务层 → Python 包装层 → pybind 登记表 → C++ 薄壳 → 纯 CUDA 实现），不再重复上层内容。

## 2. 前置知识

本讲是全手册里唯一需要读者具备 CUDA 基本词汇的一讲，先用三段话补齐。

**GPU 的执行层级。** CUDA 代码里的 `__global__` 函数叫核函数（kernel），调用时写成 `kernel<<<grid, block>>>`：`block` 是一个线程块内的线程数，`grid` 是线程块的数量。本项目中一个 block 恰好是 \( 16\times16=256 \) 个线程，负责画面上一个 \( 16\times16 \) 像素的 tile；grid 则是画面被切成的 tile 总数。

**共享内存与同步。** 同一个 block 内的线程可以访问一块极快的小容量 `__shared__` 内存，并用 `block.sync()`（`__syncthreads()` 的 cooperative_groups 封装）同步。前向渲染循环正是利用它让 256 个线程「合作搬运、各自计算」：每轮把 256 个高斯的数据从显存搬到共享内存，再各自对自己的像素做混合。`atomicAdd(&x, v)` 是原子加：多个线程同时往同一个地址累加时不互相覆盖——这是反向传播把几百个像素的梯度累到同一个高斯身上的基础。

**排序与视锥。** 基数排序（radix sort）按位分组排序，复杂度 \( O(k\cdot n) \)，`cub::DeviceRadixSort::SortPairs` 是现成实现；视锥（frustum）是相机能看见的空间区域。另外要记得 u2-l3 的结论：矩阵在 Python 侧统一转置后传入，因为 CUDA 侧按列主序解释——本讲会看到 `transformPoint4x3` 用 `matrix[0], matrix[4], matrix[8]` 取「第一行」。

若这些概念仍模糊，不影响读懂主流程；遇到不理解的行再回来查即可。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [config.h](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/config.h) | 18 | 三个编译期常量：通道数、tile 宽高 |
| [auxiliary.h](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/auxiliary.h) | 173 | 内联工具：`BLOCK_SIZE`、SH 常数、`getRect`、`in_frustum`、矩阵乘法、`CHECK_CUDA` |
| [rasterizer_impl.h](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.h) | 73 | 三个状态结构体（Geometry/Binning/Image）与 `obtain` 对齐分配器 |
| [rasterizer_impl.cu](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu) | 491 | `Rasterizer::forward` 的六阶段编排、`markVisible`、三个辅助核 |
| [forward.cu](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu) | 726 | 前向两大核：`preprocessCUDA`（逐高斯）与 `renderCUDA`（逐 tile） |
| [backward.cu](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu) | 1271 | 反向三大块：`renderCUDA` 反向、`computeCov2DCUDA`、`preprocessCUDA` 反向（含 4D 协方差反向） |

另会少量交叉引用 `rasterize_points.cu`（缓冲的 torch 侧来源）。

## 4. 核心概念与源码讲解

### 4.1 常量与工具函数：config.h 与 auxiliary.h

#### 4.1.1 概念说明

整个光栅化器的「粒度」由三个常量决定。渲染不是逐像素全局扫所有高斯，而是把画面切成 \( 16\times16 \) 的 tile，一个线程块独占一个 tile；这决定了共享内存的大小、排序后的任务划分方式，以及为什么一个高斯要被「复制」到它覆盖的每个 tile。此外，`auxiliary.h` 里藏着两个影响正确性的细节：近平面剔除的判据、以及矩阵的列主序约定。

#### 4.1.2 核心流程

- `NUM_CHANNELS=3`：每个高斯输出 RGB 三通道，模板参数 `C`。
- `BLOCK_X=16, BLOCK_Y=16`：tile 尺寸；`BLOCK_SIZE = BLOCK_X*BLOCK_Y = 256` 为每块线程数，也是反向循环每轮批处理的高斯个数。
- `in_frustum`：唯一有效判据是 `p_view.z <= 0.2`（近平面），x/y 边界检查被注释掉。
- `getRect`：由屏幕半径换算高斯覆盖的 tile 矩形。

#### 4.1.3 源码精读

三个常量只有三行：

- [config.h:L15-L17](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/config.h#L15-L17) 定义 `NUM_CHANNELS=3`、`BLOCK_X=16`、`BLOCK_Y=16`。后续所有核函数的并行粒度都由这里决定，改这两个数需要重新编译整个扩展。

- [auxiliary.h:L18-L20](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/auxiliary.h#L18-L20) 派生 `BLOCK_SIZE=256`、`NUM_WARPS=8`，并定义 4D 球谐用到的 `MY_PI`。

- [auxiliary.h:L140-L163](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/auxiliary.h#L140-L163) 是视锥剔除 `in_frustum`：把点经 `projmatrix` 投影、经 `viewmatrix` 变到相机系，[L153](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/auxiliary.h#L153) 只判 `p_view.z <= 0.2f`，x/y 的 `±1.3` 检查整段被注释。这正是 u4-l2 讲过的「markVisible 只是保守粗筛」的 CUDA 侧实现；渲染主路径 `preprocessCUDA` 内部也调用同一函数（见 4.3）。

- [auxiliary.h:L47-L57](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/auxiliary.h#L47-L57) `getRect` 用 `(p ± max_radius)/BLOCK_X` 向零取整得到 tile 矩形端点，并 clamp 到 `grid` 范围。

- [auxiliary.h:L59-L77](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/auxiliary.h#L59-L77) `transformPoint4x3/4x4` 用 `matrix[0], matrix[4], matrix[8]` 索引——列主序读法，解释了 Python 侧存储前为何统一转置（承接 u2-l3）。

- [auxiliary.h:L165-L172](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/auxiliary.h#L165-L172) `CHECK_CUDA` 宏默认不做 `cudaDeviceSynchronize`，只在 `debug=true` 时检查错误；所以训练中 CUDA 报错的栈经常滞后于真正出错的核。

#### 4.1.4 代码实践

1. **实践目标**：建立 tile 划分的数量直觉。
2. **操作步骤**（纸笔计算，示例数据）：设渲染分辨率 \( W=1280, H=720 \)。
   - tile 网格：\( \lceil 1280/16\rceil \times \lceil 720/16\rceil = 80\times45 = 3600 \) 个 tile；
   - `renderCUDA` 的启动配置为 `grid=(80,45,1), block=(16,16,1)`，共 921 600 线程 = 像素数；
   - 每个线程块的共享内存固定为 `256×(4+8+16) ≈ 7 KB`（`int` id + `float2` xy + `float4` conic_opacity）。
3. **需要观察的现象**：block 数只与分辨率有关，与高斯数量无关；高斯数量只影响每个 tile 要处理的 `range` 长度。
4. **预期结果**：能回答「分辨率翻倍、高斯数量翻倍分别影响 grid 维度还是 range 长度」。
5. 本实践为纯计算，无需 GPU（无「待本地验证」项）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `in_frustum` 只保留近平面检查而注释掉 x/y 检查？
**答案**：x/y 方向靠后面的 tile 矩形覆盖来兜底——超出屏幕的高斯 `getRect` 会被 clamp 到 `grid` 边界，覆盖 0 个 tile 即被丢弃；而 `z<=0.2` 若不检查会导致除以接近 0 的深度产生 NaN，必须提前拦截。

**练习 2**：`BLOCK_SIZE` 出现在反向循环的 `rounds` 计算里，它同时扮演哪两种角色？
**答案**：既是线程块内的线程数（每轮合作搬运 256 个高斯到共享内存），也是分批处理高斯的批大小（`rounds = ceil(range长度/256)`）。

### 4.2 rasterizer_impl.cu：前向六阶段流水线与三块状态缓冲

#### 4.2.1 概念说明

`Rasterizer::forward` 本身几乎不做数学，它是**调度器**：把前向渲染拆成「逐高斯预处理 → 前缀和 → 键值复制 → 排序 → tile 区间划分 → 逐 tile 混合」六个阶段，前五个阶段都在为最后一个阶段准备一份「每个 tile 该按什么顺序处理哪些高斯」的列表。理解这份列表的构造过程，就理解了 tile-based splatting 的全部工程要点。

#### 4.2.2 核心流程

```text
阶段 0  分配三块状态缓冲（Geometry/Binning/Image），从单一字节块中切割
阶段 1  preprocessCUDA<<<ceil(P/256),256>>>   每个 Gaussian 一线程
        ├── 4D→3D 时间边缘化（Schur 补 + marginal_t + 均值偏移）
        ├── 视锥剔除、投影、EWA 2D 协方差、conic（逆协方差）
        ├── 屏幕半径 radius → 覆盖 tile 数 tiles_touched
        └── SH → RGB，写 conic_opacity（含有效不透明度）
阶段 2  cub::DeviceScan::InclusiveSum(tiles_touched) → point_offsets
        把最后一个前缀和拷回主机得到 num_rendered（实例总数）
阶段 3  duplicateWithKeys<<<ceil(P/256),256>>>
        每个可见高斯对其覆盖的每个 tile 发一条 64 位键值对
        key = (tile_id << 32) | depth 的 IEEE754 位模式, value = gaussian_id
阶段 4  cub::DeviceRadixSort::SortPairs  按 key 排序（先 tile 后深度）
阶段 5  identifyTileRanges<<<ceil(num_rendered/256),256>>>
        在有序列表上标记每个 tile 的 [start, end)
阶段 6  renderCUDA<<<tile_grid,(16,16)>>>    一个 block 一个 tile
        从前往后 alpha blending，写 out_color/out_T/n_contrib
```

一个关键设计：**同一个高斯会出现在多个 tile 的列表里**（被「实例化」了），所以 `num_rendered` 远大于高斯总数 \( P \)；反向传播要重建完全相同的列表，因此三个状态缓冲在前向结束后原样交还给 Python（u4-l2 所说的「不透明句柄」），反向时按同样的 `fromChunk` 重新切割。

#### 4.2.3 源码精读

- [rasterizer_impl.cu:L237-L252](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L237-L252) 阶段 0：按 `required<GeometryState>(P)` 申请 Geometry 缓冲，[L246-L247](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L246-L247) 计算 tile 网格与 block 维度。三个缓冲的实际字节来自 `rasterize_points.cu` 里 `torch::empty` 的 uint8 张量（见 [rasterize_points.cu:L88-L93](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/rasterize_points.cu#L88-L93)），因此由 PyTorch 的 caching allocator 管理生命周期。

- [rasterizer_impl.h:L22-L27](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.h#L22-L27) `obtain` 是简易对齐分配器：在字节块上按 128 字节对齐切出一段，并把游标推进。`GeometryState/BinningState/ImageState` 三个结构体（[L29-L65](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.h#L29-L65)）各自声明自己需要哪些数组；[rasterizer_impl.cu:L156-L195](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L156-L195) 的三个 `fromChunk` 用同一游标逻辑切割，从而前向与反向能对同一块内存得到完全相同的视图。

- [rasterizer_impl.cu:L260-L292](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L260-L292) 阶段 1：启动 `FORWARD::preprocess`，注意传参里包含 `ts、scales_t、rotations_r、timestamp、time_duration、rot_4d`——4D 属性从这一行开始进入 CUDA（细节在 4.3）。

- [rasterizer_impl.cu:L294-L304](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L294-L304) 阶段 2：`InclusiveSum` 把每个高斯的 `tiles_touched` 累成 `point_offsets`（注释里的例子 `[2,3,0,2,1] -> [2,5,5,7,8]`），最后一个元素即实例总数，`cudaMemcpy` 拷回主机得到 `num_rendered`，再据此分配 Binning 缓冲。这是全流程唯一一次显式的 D2H 同步点。

- [rasterizer_impl.cu:L71-L112](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L71-L112) 阶段 3 `duplicateWithKeys`：[L86](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L86) 只对 `radii[idx]>0`（预处理后仍可见）的高斯发射键值对；[L103-L108](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L103-L108) 构造 64 位键：高 32 位 tile id，低 32 位深度的位模式（`*((uint32_t*)&depths[idx])`），并经 `point_offsets` 定位各自不重叠的写入偏移。

- [rasterizer_impl.cu:L319-L328](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L319-L328) 阶段 4：`bit` 被硬编码为 32（[L319](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L319) 注释掉了原 3DGS 的 `getHigherMsb` 调用，该函数现为遗留代码），即对完整 64 位键排序。由于深度经过视锥检查恒为正（\( z>0.2 \)），IEEE754 位模式按无符号整数比较与浮点大小同序，排序结果就是「先按 tile、再按深度从近到远」。

- [rasterizer_impl.cu:L330-L338](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L330-L338) 阶段 5：先把 `ranges` 清零，再由 [identifyTileRanges（L117-L139）](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L117-L139) 在有序键上检测 tile 边界：当前 key 的高 32 位与前一 key 不同，就写入前一 tile 的结束与当前 tile 的开始。

- [rasterizer_impl.cu:L340-L361](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L340-L361) 阶段 6：启动 `FORWARD::render`（见 4.4），随后把每像素透射率 `accum_alpha` 拷到 `out_T`（[L360](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L360)），返回 `num_rendered` 供反向重建 BinningState。

- `markVisible` 在 [L142-L154](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L142-L154)：只是把 [checkFrustum 核（L54-L67）](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L54-L67) 启动一遍，逐高斯调用 `in_frustum` 写 `present[idx]`，即 u4-l2 已讲的空间可见性来源。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证六阶段的执行顺序与数据依赖。
2. **操作步骤**：通读 `Rasterizer::forward`（L199-L362），在**自己的笔记**（不要改动源码）里为每个 `CHECK_CUDA(...)` 语句标注阶段编号与它读/写的缓冲名；再核对 `rasterize_points.cu` L142-L146 返回给 Python 的 11 元组中，哪些成员只是这三个缓冲的引用。
3. **需要观察的现象**：阶段 2 的 D2H 拷贝是唯一同步点；阶段 3 的写入偏移完全由阶段 2 的前缀和决定——若删掉 `InclusiveSum`，`duplicateWithKeys` 会全部写到偏移 0 互相覆盖。
4. **预期结果**：得到一张「阶段 → 消费的生产者 → 产出的消费者」表格；能指出 `num_rendered` 为何必须回传主机（Binning 缓冲大小依赖它）。
5. 本实践为源码阅读型，无需 GPU（无「待本地验证」项）。

#### 4.2.5 小练习与答案

**练习 1**：`ImageState` 的三个数组按 `width*height` 分配，但 `ranges` 实际只用了 `tile_grid.x*tile_grid.y` 个 `uint2`，这有问题吗？
**答案**：功能上没问题，只是过度分配（tile 数恒 ≤ 像素数）；L330 的 memset 也只按 tile 数清零。属于无害的冗余。

**练习 2**：为什么反向不需要重新执行阶段 2-5，直接从 `binning_buffer` 切回 `BinningState` 即可？
**答案**：三个缓冲保存了排序后的 `point_list`、`ranges`、`conic_opacity`、`depths` 等全部中间量；反向只需要按与前向相同的视图重建指针（[rasterizer_impl.cu:L412-L425](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L412-L425)），既省时间也保证前后向遍历的是同一份列表。

**练习 3**：把 `bit` 从 32 改回 `getHigherMsb(tile_grid.x*tile_grid.y)` 会发生什么？
**答案**：排序只比较「tile id + 深度的高位 bit」，同一 tile 内深度只在高位近似有序，混合顺序可能与前向精确版本不同（更接近原 3DGS 的做法）；由于渲染结果依赖顺序，可能引入细微数值差异。属于源码阅读推断，待本地验证。

### 4.3 forward.cu（上）：preprocessCUDA 与时间边缘化的 CUDA 实现

#### 4.3.1 概念说明

`preprocessCUDA` 每个线程处理一个高斯，回答三个问题：**这个高斯在渲染时刻 τ 长什么样（3D 协方差与有效不透明度）、投影到屏幕哪里多大（conic 与半径）、是什么颜色（SH→RGB）**。其中第一问就是 4C4D 相对 3DGS 的核心改造点：把 4D 高斯在超平面 \( t=\tau \) 上「切片」。u3-l3 已经从 Python 侧（`build_covariance_from_scaling_rotation_4d`）推导过 Schur 补公式，本节看它在 CUDA 里的逐行实现——这也是本讲规格要求的「定位 `ts/scales_t/rotations_r` 的消费位置」。

#### 4.3.2 核心流程

每个高斯在 `preprocessCUDA` 中依次经过：

1. `radii=0, tiles_touched=0` 初始化（不可见即保持 0）；
2. 取 `p_orig` 与原始不透明度 `opacity = opacities[idx]`；
3. 协方差三分支：`cov3D_precomp` 直接用 / `rot_4d` 走 `computeCov3D_conditional` / 否则走 3D `computeCov3D`＋独立时间衰减；
4. 视锥剔除（近平面）；
5. 投影 → `computeCov2D`（EWA）→ conic（2D 协方差求逆）；
6. 由 2D 协方差特征值求屏幕半径 → `getRect` 覆盖 tile 数；
7. SH→RGB（3D 或 4D 球谐）；
8. 落盘 `depths/radii/points_xy_image/conic_opacity/tiles_touched`。

时间边缘化的数学（与 u3-l3 同一套公式，符号对齐 CUDA 变量名）：设 4D 协方差分块

\[
\Sigma=\begin{pmatrix}\Sigma_{xx} & \Sigma_{xt}\\ \Sigma_{tx} & \sigma_t^2\end{pmatrix},\qquad
\Delta t=\tau-t ,
\]

则固定时刻的 3D 条件协方差与均值偏移为

\[
\Sigma_{x|t}=\Sigma_{xx}-\frac{\Sigma_{xt}\Sigma_{tx}}{\sigma_t^2},\qquad
\delta\mu=\frac{\Sigma_{xt}}{\sigma_t^2}\Delta t ,
\]

时间衰减（边缘化到一维时间高斯）为

\[
m=\exp\!\left(-\frac{\Delta t^2}{2\sigma_t^2}\right),
\]

有效不透明度 \( o_{\text{eff}}=o\cdot m \)。剔除阈值 \( m>0.05 \) 等价于 \( |\Delta t|<\sqrt{2\ln 20}\,\sigma_t\approx2.45\sigma_t \)。

#### 4.3.3 源码精读

**入口与消费点。** [forward.cu:L355-L387](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L355-L387) 是 `preprocessCUDA` 的签名：`ts` 在 [L359](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L359)、`scales_t` 在 [L361](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L361)、`rotations_r` 在 [L364](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L364)。它们在核内真正被读取的位置只有两处：

- [forward.cu:L414-L424](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L414-L424)：`rot_4d` 分支调用 `computeCov3D_conditional(scales[idx], scales_t[idx], ..., rotations[idx], rotations_r[idx], ..., ts[idx], timestamp, ...)`，随后把带均值偏移的 `p_orig` 写进 `out_means3D`（[L421-L423](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L421-L423)）。时间掩码不通过则 [L419](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L419) 直接 return，`radii` 保持 0，该高斯从此退出渲染。
- [forward.cu:L429-L435](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L429-L435)：非 `rot_4d` 且 `gaussian_dim==4` 时，用 \( m=\exp(-0.5\Delta t^2/\sigma) \)（`sigma = scales_t[idx]*scale_modifier`）独立衰减不透明度。**注意语义差异**：这条路径的 `scales_t` 直接充当方差 \( \sigma \)（分母一次方），而 `rot_4d` 路径的 `cov_t` 是 \( \sigma_t^2 \)（分母平方）——即 u3-l3 指出的「`_scaling_t` 在 rot_4d 开关下语义差一个平方」。

**computeCov3D_conditional 逐段。** [forward.cu:L279-L352](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L279-L352)：

- [L284-L289](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L284-L289) `dt = timestamp - t`（注意符号是 \( \tau-t \)），并构造 4×4 对角缩放阵 \( S=\mathrm{diag}(s_x,s_y,s_z,s_t) \)。
- [L315-L331](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L315-L331) 由左右四元数组装 \( M_l,M_r \)，\( R=M_r M_l \)（双四元数覆盖 SO(4)），\( M=SR \)，\( \Sigma=M^{\mathsf T}M \)。上方的注释矩阵是推导痕迹，保留供对照。
- [L332-L336](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L332-L336) 时间边缘化的三行核心：`cov_t=Σ[3][3]`、`marginal_t=__expf(-0.5*dt*dt/cov_t)`、`mask = marginal_t>0.05`、通过则 `opacity *= marginal_t`。**u4-l1 说「默认路径下 opacity 与 marginal_t 的乘法在 CUDA 核内恰好发生一次」，指的就是 L336 这一行。**
- [L337-L339](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L337-L339) Schur 补：`cov11 − outerProduct(cov12,cov12)/cov_t`，其中 `cov12` 取 `Σ[0][3],Σ[1][3],Σ[2][3]`（反向侧取 `Σ[3][0..2]`，因 Σ 对称两者相等）。
- [L348-L351](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L348-L351) 均值偏移 \( \delta\mu=\text{cov12}/\sigma_t^2\cdot\Delta t \) 加回 `p_orig`——漏掉它运动区域会拖尾（承接 u3-l3）。

**其余预处理。** [forward.cu:L439-L470](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L439-L470)：视锥检查（L439-L441）→ 投影（L444-L446）→ [computeCov2D（L198-L237）](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L198-L237) 做 EWA 投影（雅可比 J、\( T=WJ \)、\( \Sigma'=T^{\mathsf T}\Sigma T \)，并在 [L234-L235](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L234-L235) 加 0.3 的低通滤波保证至少一个像素宽）→ conic 求逆（L452-L456）→ 特征值求半径 `ceil(3√λ)`（L462-L465，3σ 准则）→ `getRect` 与零覆盖剔除（L468-L470）。

**4D 球谐的 `ts` 消费。** [forward.cu:L474-L485](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L474-L485) 按 `gaussian_dim==3 || force_sh_3d` 选择 3D/4D 求值。[computeColorFromSH_4D（L73-L195）](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L73-L195) 在 [L83](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L83) 计算 `dir_t = ts[idx]−timestamp`，[L143](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L143) 与 [L163](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L163) 分别以 \( \cos(2\pi\Delta t/T) \)、\( \cos(4\pi\Delta t/T) \) 调制 sh[16..31]、sh[32..47] 两块系数——与 u3-l4 的「空间 SH × 时间余弦基」账目一致，最后 `+0.5` 并按 `clamped` 记录负值截断（L187-L194）。

**落盘。** [forward.cu:L488-L493](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L488-L493) 写出五个量，其中 [L492](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L492) 的 `conic_opacity[idx] = {conic.x, conic.y, conic.z, opacity}` 把 **已经是 \( o\cdot m \) 的有效不透明度** 与 conic 打包进一个 `float4`——这是时间信息进入后续 alpha blending 的唯一通道。

#### 4.3.4 代码实践

1. **实践目标**：用具体数值走一遍 CUDA 内的时间边缘化（本讲规格指定的实践前半部分）。
2. **操作步骤**（纸笔或 Python，示例代码）：

```python
import math
# 与 forward.cu L279-L352 对齐的单高斯数值实验（示例代码）
t, tau = 2.0, 3.0          # ts[idx]=2.0, timestamp=3.0
s_t, mod = 0.5, 1.0        # scales_t[idx], scale_modifier（rot_4d 语义：尺度）
o = 0.9                    # 原始不透明度 opacities[idx]

cov_t = (mod * s_t) ** 2   # Σ[3][3] = σ_t²
dt = tau - t               # L284：注意是 τ−t
marginal = math.exp(-0.5 * dt * dt / cov_t)   # L333
print(marginal > 0.05, marginal, o * marginal)  # L334/L336
# 阈值反推：|dt| < sqrt(2*ln20)*σ_t
print(math.sqrt(2 * math.log(20)) * s_t)
```

3. **需要观察的现象**：`marginal ≈ 0.1353 > 0.05`，高斯保留，有效不透明度变为 \( 0.9\times0.1353\approx0.1218 \)；阈值半径 \( \sqrt{2\ln 20}\times0.5\approx1.225 \)，而 \( |dt|=1 \) 在界内。
4. **预期结果**：把 `t` 改成 1.0（\( \Delta t=2 \)）后 `marginal≈0.018<0.05`，对应核内 L419 提前 return、`radii` 保持 0；再对比把 `rot_4d` 关闭后的公式（`sigma = s_t`，即 `cov_t` 少一次平方），观察同一组数下 marginal 大很多——直观感受「差一个平方」。
5. 纯 CPU 计算，无「待本地验证」项。

#### 4.3.5 小练习与答案

**练习 1**：`conic_opacity` 里的第 4 个分量是原始不透明度还是有效不透明度？依据是哪一行？
**答案**：有效不透明度。`opacity` 在 [L405](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L405) 取原始值，经 L336（rot_4d）或 L434（非 rot_4d）乘上 `marginal_t` 后，直到 L492 才被打包——期间没有任何还原操作。

**练习 2**：`out_means3D` 在 3D 路径下等于什么？为什么反向传播需要它？
**答案**：3D 路径不写它，保持 `rasterize_points.cu` L84 `means3D.clone()` 的值即原始均值；rot_4d 路径写入 \( \mu+\delta\mu \)。反向的 `computeCov2DCUDA` 与 `preprocessCUDA` 都以它为「均值」（rasterizer_impl.cu L460/L459 传入），保证与前向使用同一个（含偏移的）投影一致。

**练习 3**：为什么时间剔除阈值取 0.05 而不是更小？
**答案**：\( m=0.05 \) 对应约 \( 2.45\sigma_t \) 的时间半宽，之外的高斯对当前帧的贡献上限已低于不透明度剪枝线 0.005 的量级，且被 Python 侧 `get_marginal_t(...) > 0.05`（u4-l3、u6-l3）采用同一常数，两侧口径一致。

### 4.4 forward.cu（下）：renderCUDA 分块渲染循环与 alpha blending

#### 4.4.1 概念说明

`renderCUDA` 是「一个 block 一个 tile、一个线程一个像素」的混合核。因为阶段 4 已保证 tile 内列表按深度从近到远有序，混合就是经典的从前往后 alpha compositing。它的两个工程技巧值得学习：**合作预取**（每轮 256 个高斯的数据进共享内存，摊薄全局显存访问延迟）与**提前终止**（透射率低于 \(10^{-4}\) 后整块投票退出），后者也是反向传播需要 `n_contrib` 记录「最后贡献者」的原因。

#### 4.4.2 核心流程

对像素 \( p \)，设该 tile 的有序高斯为 \( i=1..N \)，核函数计算：

\[
\alpha_i=\min\!\big(0.99,\; o_{\text{eff},i}\cdot G_i\big),\qquad
G_i=\exp\!\Big(-\tfrac12 d^{\mathsf T}\Sigma_i^{\prime -1} d\Big),
\]

\[
C(p)=\sum_{i=1}^{N} c_i\,\alpha_i T_i \;+\; T_N\, b_{\text{bg}},\qquad
T_i=\prod_{j<i}(1-\alpha_j),
\]

其中 \( \Sigma^{\prime-1} \) 即 conic，\( d \) 是像素到高斯屏幕中心的偏移。跳过与终止条件：`power>0` 跳过、`alpha < 1/255` 跳过、`T·(1−α) < 1e-4` 置 done。`n_contrib` 记录最后一个真正混入颜色的高斯序号，`final_T` 记录最终透射率——两者是反向的存档。

#### 4.4.3 源码精读

- [forward.cu:L499-L524](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L499-L524) 核签名带 `__launch_bounds__(BLOCK_X*BLOCK_Y)`（告诉编译器每块恰好 256 线程，便于寄存器分配）；[L517-L524](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L517-L524) 由 block 索引换算 tile 与像素坐标，`inside` 标记越界线程（它们只帮忙搬运不参与计算）。

- [forward.cu:L531-L547](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L531-L547) 取本 tile 的 `range`，算出轮数 `rounds`；[L537-L539](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L537-L539) 声明三个共享数组；[L542-L547](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L542-L547) 初始化 \( T=1 \)、累计色 \( C=0 \) 与 `contributor/last_contributor` 计数。

- [forward.cu:L550-L566](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L550-L566) 每轮先 `__syncthreads_count(done)` 投票：**256 个线程全部 done 才整块退出**；随后各线程从 `point_list` 取自己负责的一个高斯（`progress = i*BLOCK_SIZE + thread_rank`）填进共享内存。

- [forward.cu:L576-L595](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L576-L595) 单高斯混合：[L579](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L579) 用 conic 三分量算 `power`（即 \( -\tfrac12 d^{\mathsf T}\Sigma'^{-1}d \)）；[L587](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L587) `alpha = min(0.99f, con_o.w * exp(power))`——`con_o.w` 正是 4.3 打包进来的 \( o_{\text{eff}} \)；[L588-L595](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L588-L595) 小于 1/255 跳过、透射率饱和即置 done。

- [forward.cu:L597-L609](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L597-L609) 累加：`C[ch] += features[id]*alpha*T`（L598-L599，注意用的是**乘 α 之前的 T**，对应公式里的 \( T_i \)），深度与 flow 同式累加（L600-L602），随后 `T = test_T`（L604）、`last_contributor = contributor`（L608）。

- [forward.cu:L614-L623](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L614-L623) 落盘：`final_T`（透射率）、`n_contrib`（最后贡献者序号）、`out_color = C + T*bg`（背景以 over 操作符混入，即 u4-l3 的 \( C+(1-\alpha_{\text{总}})b \)）、`out_flow/out_depth`。flow 通道在本项目中恒为零：Python 包装层 `flow_2d` 缺省传空张量（[diff_gaussian_rasterization/__init__.py:L264-L265](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L264-L265)），属继承自 flow 变体的代码路径。

- 启动器 [forward.cu:L626-L658](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L626-L658) 只是把 `tile_grid/block` 与各缓冲转发给模板实例 `renderCUDA<NUM_CHANNELS>`。

#### 4.4.4 代码实践

1. **实践目标**：用 NumPy 复刻单像素的前向混合，验证公式与跳过条件（为 4.5 的反向实践打底）。
2. **操作步骤**（示例代码，纯 CPU）：

```python
import numpy as np
# 3 个高斯共用一个像素，已按深度从近到远排序（示例代码）
o_eff = np.array([0.9, 0.8, 0.6])        # conic_opacity.w（已含 marginal_t）
G     = np.array([1.0, 0.5, 0.2])        # exp(power)
c     = np.eye(3)                        # 三个高斯的 RGB
bg    = np.zeros(3)

alpha = np.minimum(0.99, o_eff * G)      # forward.cu L587
T, C = 1.0, np.zeros(3)
n_contrib = 0
for i in range(3):
    if alpha[i] < 1.0 / 255.0:           # L588
        continue
    if T * (1 - alpha[i]) < 1e-4:        # L590-L591：饱和终止
        break
    C = C + c[i] * alpha[i] * T          # L598-L599
    T = T * (1 - alpha[i])               # L604
    n_contrib = i + 1                    # L608（1-based 计数）
C = C + T * bg                           # L619
print(alpha, C, T, n_contrib)
```

3. **需要观察的现象**：`alpha=[0.9, 0.4, 0.12]`；循环结束后 \( T=(1-0.9)(1-0.4)(1-0.12)=0.1\times0.6\times0.88=0.0528 \)，`n_contrib=3`。把 `o_eff[0]` 改成 0.999 再看：`alpha[0]` 被 clamp 到 0.99，体现了 `min(0.99,·)` 的数值稳定作用。
4. **预期结果**：手算 \( C \) 与程序输出一致；理解 `n_contrib` 为何等于「最后一个混入颜色者」而不是列表长度。
5. 纯 CPU 计算，无「待本地验证」项。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接 `C += c*alpha*T` 与 `T *= (1-alpha)` 写成一行？
**答案**：公式要求第 \( i \) 项使用**乘之前的** \( T_i \)；两行顺序颠倒会把第 \( i \) 个高斯乘上 \( T_{i+1} \)，等价于把它挤到身后一位，颜色系统性偏暗。

**练习 2**：提前终止后，列表中剩余高斯对本像素的贡献被忽略，反向传播如何知道「从哪往前算」？
**答案**：前向把 `last_contributor` 存进 `n_contrib[pix_id]`（L617）；反向遍历时凡 `contributor >= last_contributor` 的高斯直接跳过（见 4.5），保证不给被饱和遮蔽的高斯发梯度。

**练习 3**：`__syncthreads_count(done)` 为什么在循环开头而不是结尾？
**答案**：放在开头使得「上一轮已全部 done」的块在进入新一轮预取前就能整体退出，省掉最后一次共享内存搬运；放在结尾则至少多执行一轮空循环。

### 4.5 backward.cu：反向渲染与 4D 链式梯度

#### 4.5.1 概念说明

反向由 `Rasterizer::backward`（[rasterizer_impl.cu:L366-L492](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L366-L492)）编排为两步：先 `BACKWARD::render` 把每像素损失梯度 \( \partial L/\partial C(p) \) 变成逐高斯的 \( \partial L/\partial\{\text{mean2D, conic, }o_{\text{eff}}, \text{color}\} \)，再 `BACKWARD::preprocess` 把这些量链式折回 3D 均值、SH、尺度、四元数与时间参数。**一个像素的梯度如何分配回多个重叠高斯**是第一步的核心；**不透明度梯度的累加与折算**横跨两步，是本讲规格指定的第二个追踪目标。

#### 4.5.2 核心流程

**第一步 renderCUDA 反向（从后往前）**。对像素 \( p \) 的有序列表从最后一个开始遍历。设处理到第 \( i \) 个高斯时：

- 透射率「反除」还原：\( T_i \leftarrow T_{i+1}/(1-\alpha_i) \)，从存档的 \( T_{\text{final}} \) 出发逐个除回去；
- 身后累积色递推：\( A_i \leftarrow \alpha_{i+1} c_{i+1} + (1-\alpha_{i+1}) A_{i+1} \)，\( A_N=0 \)；
- 该像素对 \( \alpha_i \) 的梯度：
  \[
  \frac{\partial L}{\partial \alpha_i}
  = T_i\sum_{ch}\big(c_{i,ch}-A_{i,ch}\big)\frac{\partial L}{\partial C_{ch}}
  \;-\;\frac{T_{\text{final}}}{1-\alpha_i}\sum_{ch} b_{\text{bg},ch}\frac{\partial L}{\partial C_{ch}} ;
  \]
- 四路 `atomicAdd`：\( \partial L/\partial c_i \)（乘 \( \alpha_iT_i \)）、\( \partial L/\partial\mu_{2D} \)、\( \partial L/\partial\Sigma_i^{\prime-1} \)、\( \partial L/\partial o_{\text{eff},i}=G_i\cdot\partial L/\partial\alpha_i \)。

**第二步 preprocess 反向**。`computeCov2DCUDA` 把 conic 梯度经 EWA 链回 \( \partial L/\partial\Sigma_{3D} \) 与均值；`preprocessCUDA` 再分三路：mean2D→mean3D、SH 反向（4D 版额外产 \( \partial L/\partial t \)）、`rot_4d` 时 `computeCov3D_conditional` 反向把 Schur 补、均值偏移、`marginal_t` 的梯度折到 \( s_t \)、左右四元数、\( t \) 与原始 \( o \)。

#### 4.5.3 源码精读

**（a）编排。** [rasterizer_impl.cu:L412-L425](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L412-L425) 先从三块缓冲重建三个状态（注意 `BinningState::fromChunk(binning_buffer, R)` 用的是前向返回的实例数 R）；[L432-L453](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L432-L453) 启动 `BACKWARD::render`，[L458-L491](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L458-L491) 启动 `BACKWARD::preprocess`（cov3D 优先用 precomp，否则用前向缓存的 `geomState.cov3D`）。

**（b）renderCUDA 反向。** [backward.cu:L923-L946](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L923-L946) 核签名与前向同构，多收 `dL_dpixels/dL_depths/dL_masks/dL_dpix_flow` 与前向存档 `final_Ts/n_contrib`。[L972-L1004](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L972-L1004) 初始化：`T = T_final`、`contributor = toDo`（从列表末尾计数）、`last_contributor = n_contrib[pix_id]`，并把每像素梯度读进寄存器。[L1012-L1030](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L1012-L1030) 预取时用 `point_list[range.y - progress - 1]`——**从后往前**搬运。

- [backward.cu:L1033-L1055](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L1033-L1055)：`contributor--` 后 [L1038](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L1038) 跳过 `contributor >= last_contributor` 的饱和段；随后**逐字重算**前向的 `power/G/alpha`（L1042-L1052），并 [L1054](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L1054) `T = T/(1.f-alpha)` 还原 \( T_i \)，[L1055](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L1055) 记 `dchannel_dcolor = alpha*T`。

- [backward.cu:L1060-L1075](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L1060-L1075)：核心递推。[L1066](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L1066) `accum_rec = last_alpha*last_color + (1-last_alpha)*accum_rec` 用**上一个（更靠后的）高斯**更新身后累积色 \( A_i \)；[L1070](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L1070) `dL_dalpha += (c - accum_rec)*dL_dchannel`；[L1074](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L1074) `atomicAdd(&dL_dcolors[id*C+ch], dchannel_dcolor*dL_dchannel)`——颜色梯度开始跨像素累加。深度（L1092-L1096）与 mask（L1098-L1100）走同一套 \( A \) 递推。

- [backward.cu:L1102-L1111](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L1102-L1111)：`dL_dalpha *= T`（补上 \( T_i \) 因子），再补背景项 \( -T_{\text{final}}/(1-\alpha_i)\cdot\langle b_{\text{bg}},\partial L/\partial C\rangle \)（背景透射率也受 \( \alpha_i \) 影响）。

- [backward.cu:L1115-L1132](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L1115-L1132)：链到几何量。由 \( \alpha=o_{\text{eff}}G \) 得 \( \partial\alpha/\partial G=o_{\text{eff}} \)（L1115 `dL_dG = con_o.w*dL_dalpha`），再经 \( G=\exp(\text{power}) \) 对像素坐标与 conic 求偏导，分别 `atomicAdd` 到 `dL_dmean2D.x/.y`（L1122-L1123，乘 \( 0.5W/0.5H \) 完成 NDC→像素的链式）与 `dL_dconic2D`（L1127-L1129）；`dL_dmean2D.z` 存深度通路梯度（L1124）。最后 [L1132](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L1132) `atomicAdd(&dL_dopacity[global_id], G*dL_dalpha)`——由 \( \partial\alpha/\partial o_{\text{eff}}=G \)（未触 0.99 clamp 与 1/255 跳过时）得到**有效不透明度梯度**，每个被该高斯覆盖的像素/线程都原子加一次，这就是「一个像素的梯度分配回多个重叠高斯、一个高斯汇聚多个像素梯度」的全部机制。

**（c）computeCov2DCUDA。** [backward.cu:L486-L617](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L486-L617)：仅处理 `radii>0` 的高斯（L499），重算前向的 2D 协方差（含 +0.3 低通，L540-L542），先由 conic 梯度解出对 \( \Sigma'_{2D} \) 三元素的梯度（L553-L555），再经 \( \Sigma'=T^{\mathsf T}\Sigma T \) 与 \( T=WJ \) 两条矩阵链折回 `dL_dcov`（L560-L570）与 3D 均值（L605-L616，其中 L611 把 `dL_dmean2D[idx].z` 即深度梯度并入）。此核不需要 4D 信息，因为它消费的是**已经切片好的**条件协方差。

**（d）preprocessCUDA 反向。** [backward.cu:L838-L921](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L838-L921)：双重守卫 `radii>0 && tiles_touched>0`（L870-L873，时间不可见的高斯在前向已被丢弃，此处天然无梯度）。mean2D→mean3D 的投影链在 L877-L892。SH 分支（L894-L904）调 [computeColorFromSH_4D 反向（L144-L481）](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L144-L481)：sh[16..31]/sh[32..47] 的梯度分别乘 \( t_1/t_2 \)（L305-L320、L386-L401），时间基导数 `dt1_dt/dt2_dt` 在 [L302-L303](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L302-L303) 与 [L383-L384](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L383-L384)，\( \partial L/\partial t \) 的 SH 来源在 [L480](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L480) `dL_dts[idx] += dot(dRGBdt, dL_dRGB)`。**源码观察**：\( \mathrm{d}\cos(2\pi\Delta t/T)/\mathrm{d}t \) 的标准形式是 \( -\sin(\cdot)\cdot 2\pi/T \)，而 L303 写作 `+sin(...)*2*MY_PI/time_duration`，符号与标准导数相反（L384 同）；该差异是否被其他环节抵消，建议用有限差分对拍确认（待本地验证）。

**（e）computeCov3D_conditional 反向。** [backward.cu:L689-L833](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L689-L833) 重算前向同一套 \( M_l,M_r,R,M,\Sigma \)（L695-L748，含同样的 `mask = marginal_t>0.05` 早退），然后四段链式：

- [L755-L765](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L755-L765)：由 \( \Sigma_{x|t}=\Sigma_{xx}-\Sigma_{xt}\Sigma_{tx}/\sigma_t^2 \) 反解出对 \( \Sigma_{xt} \)（`dL_dcov12`）与 \( \sigma_t^2 \)（`dL_dcovt`）的梯度，off-diagonal 因对称出现两次而乘 0.5 或 2 的细节与前向存上三角一致。
- [L767-L773](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L767-L773) **不透明度梯度的最终折算**：`dL_dopacity` 此刻装的是 (b) 中累加出的 \( \partial L/\partial o_{\text{eff}} \)；由 \( o_{\text{eff}}=o\cdot m \) 得
  \[
  \frac{\partial L}{\partial o}=\frac{\partial L}{\partial o_{\text{eff}}}\cdot m,\qquad
  \frac{\partial L}{\partial m}=\frac{\partial L}{\partial o_{\text{eff}}}\cdot o ,
  \]
  对应 L769 的 `dL_dopacity[idx] *= marginal_t` 与 L768 的 `dL_dmarginal_t = dL_dopacity[idx]*opacity`（注意 `opacity` 形参传入的是**原始** `opacities[idx]`，见调用点 [L909-L911](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L909-L911)）。\( m \) 再经 \( m=\exp(-\Delta t^2/2\sigma_t^2) \) 链到 \( \sigma_t^2 \)（L770 `m·dt²/2σ_t⁴`）与 \( t \)（L771 `m·dt/σ_t²`）。
- [L775-L781](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L775-L781)：均值偏移 \( \delta\mu=\Sigma_{xt}\Delta t/\sigma_t^2 \) 的梯度——`dL_dmeans` 同时流入 `dL_dcov12` 与 `dL_dcovt`，并直接贡献 `dL_dts[idx] += dL_dt`。**`dL_dts` 因此有两个来源：SH 时间基（L480）＋条件协方差（L781）。**
- [L783-L833](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L783-L833)：组装 4×4 `dL_dSigma`，经 `dL_dM = 2M·dL_dSigma`（L791，因 \( \Sigma=M^{\mathsf T}M \)）得到尺度梯度（L796-L801，第 4 个即 `dL_dscales_t[idx] = dot(Rt[3], dL_dMt[3])`），最后由 `dL_dMt` 与 \( M_r/M_l \) 的矩阵积提取左右四元数各自的梯度（L808-L829），写入 `dL_drots/dL_drots_r`。

**（f）一个值得注意的空缺。** 非 `rot_4d` 的 4D 分支（[L913-L918](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/backward.cu#L913-L918)）只调 3D `computeCov3D`，`// marginal opacity` 处是空注释——即该路径下 `dL_dopacity` **没有**乘回 `marginal_t`。官方配置 `rot_4d: True`（[configs/dynerf/flame_steak.yaml:L5](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L5)），走的是 (e) 的完整路径，故此空缺只影响把 `rot_4d` 关掉做实验时的一致性；其数值影响待本地验证。

#### 4.5.4 代码实践

本实践是规格任务的后半部分：**追踪一个像素的梯度如何分配回多个重叠高斯**，并写成执行流程说明。

1. **实践目标**：在 4.4 的三高斯例子上补出反向，用有限差分验证 CUDA 的递推公式。
2. **操作步骤**（示例代码，纯 CPU；`bg=0` 时背景项为 0）：

```python
# 接 4.4 的变量 alpha/c/G（示例代码）
T_final = T
dL_dpixel = np.array([1.0, -0.5, 0.25])      # 任意的 dLoss/dC(pix)

T = T_final
A = np.zeros(3)                              # accum_rec
last_alpha, last_color = 0.0, np.zeros(3)
dL_dopacity, dL_dcolor = np.zeros(3), np.zeros((3, 3))
for i in reversed(range(3)):
    T = T / (1.0 - alpha[i])                               # L1054
    A = last_alpha * last_color + (1 - last_alpha) * A     # L1066（先用上一个）
    dL_da = np.dot(c[i] - A, dL_dpixel)                    # L1070
    dL_da *= T                                             # L1102
    dL_da += (-T_final / (1 - alpha[i])) * np.dot(bg, dL_dpixel)  # L1108-L1111
    dL_dcolor[i] = alpha[i] * T * dL_dpixel                # L1074
    dL_dopacity[i] = G[i] * dL_da                          # L1132
    last_color, last_alpha = c[i], alpha[i]                # L1067/L1104

# 有限差分对拍（前向来自 4.4 的循环，bg=0）
def render(o_eff_):
    a_ = np.minimum(0.99, o_eff_ * G)
    T_, C_ = 1.0, np.zeros(3)
    for i in range(3):
        C_ = C_ + c[i] * a_[i] * T_
        T_ = T_ * (1 - a_[i])
    return np.dot(C_, dL_dpixel)                           # 标量损失
eps = 1e-6
num = np.array([(render(o_eff + eps*np.eye(1,3,k)[0]) -
                 render(o_eff - eps*np.eye(1,3,k)[0])) / (2*eps) for k in range(3)])
print(dL_dopacity, num)
```

3. **需要观察的现象**：`dL_dopacity` 与有限差分 `num` 应逐位吻合（误差在 \(10^{-9}\) 量级）；最近的高斯（i=0）梯度最大，因为它同时压低了自己与身后所有高斯的透射率——这正是 \( (c_i-A_i) \) 项的含义：**把自己颜色与「身后等效颜色」之差**作为分配依据。
4. **预期结果**：能据此写出执行流程说明（见下方「答案模板」）；再验证「不透明度折算」：令 `o_eff = o_raw*m`、`m=0.1353`，检查 `dL/do_raw = dL_dopacity*m` 与 `dL/dm = dL_dopacity*o_raw`（对应 backward.cu L768-L769）。
5. 纯 CPU 计算，无「待本地验证」项（GPU 路径的端到端对拍见综合实践）。

**执行流程说明模板**（供读者填写后对照）：

> 对像素 p：① 从 `n_contrib` 取最后贡献者，从 `final_T` 取 \( T_{\text{final}} \)；② 从列表末尾向前遍历，跳过 `contributor >= last_contributor` 的饱和段；③ 对每个高斯重算 \( G_i,\alpha_i \)，反除还原 \( T_i \)，用上一个高斯更新身后累积色 \( A_i \)；④ 算 \( \partial L/\partial\alpha_i \)（含背景项），乘 \( T_i \)；⑤ 四路 `atomicAdd` 分配到该高斯的 color/mean2D/conic/opacity；⑥ 换下一个高斯。多个重叠高斯各自拿到自己那份，一个高斯再把覆盖到的所有像素的贡献原子累加。

#### 4.5.5 小练习与答案

**练习 1**：反向为什么必须重算 `power/G/alpha` 而不是把前向结果存下来？
**答案**：存档会增加显存与带宽（每像素×每高斯一个 `power` 代价过大），而重算只需 `conic_opacity/xy/color` 这几个逐高斯量，它们已缓存在 GeometryState 里；autograd 体系里「前向存少量、反向重算」是标准权衡（u5-l2 的 fused_ssim 是反例：它选择前向预计算偏导）。

**练习 2**：`dL_dopacity` 在 (b) 结束时与 (e) L769 之后分别代表什么？
**答案**：前者是 \( \partial L/\partial o_{\text{eff}} \)（对「已乘 marginal_t 的有效不透明度」），后者乘上 \( m \) 折算为 \( \partial L/\partial o \)（对裸值 `_opacity` 激活前的输入），随后才经 PyTorch 的 sigmoid 反传到 `_opacity`（u3-l1 的读时激活）。

**练习 3**：若某高斯时间上不可见（`marginal_t<=0.05`），它的 `dL_dopacity`、`dL_dts`、`dL_dscale_t` 分别是多少？
**答案**：全为 0。前向它在 L419 提前 return，`radii=0`、`tiles_touched=0`，于是反向 `renderCUDA` 的列表里没有它的实例（`duplicateWithKeys` 只发射 `radii>0` 者），`preprocessCUDA` 的双重守卫（L871-L873）与 `computeCov3D_conditional` 的 mask 早退（L748、L912）再拦两道——梯度缓冲本来就是 `torch.zeros` 初始化（rasterize_points.cu L198-L209），保持为零。

## 5. 综合实践

**任务：写一个「单像素 4D mini 光栅器」，把本讲的 marginal_t、alpha blending、三段式梯度全部串起来，并用有限差分验证整体正确性。**

1. **实践目标**：在一个只依赖 NumPy 的脚本里，复现 `preprocessCUDA`（时间边缘化部分）→ `renderCUDA`（前向混合）→ `renderCUDA` 反向 → `computeCov3D_conditional` 反向（不透明度与时间折算）这条最小闭环，验证你理解的公式与 CUDA 实现一致。
2. **操作步骤**：
   - 参数：\( K=3 \) 个高斯，每个有 \( o_k, t_k, s_t \)（固定 `rot_4d` 语义：`cov_t=s_t²`）；固定 \( G_k \)（把 2D 高斯核当作常量，即只考察不透明度与时间通路）、固定颜色 \( c_k \)、渲染时刻 \( \tau \)、\( b_{\text{bg}}=0 \)；
   - 前向：`m = exp(-0.5*(tau-t)**2/s_t**2)`（forward.cu L333）→ `o_eff = o*m`（L336）→ `alpha=min(0.99,o_eff*G)`（L587）→ 从前往后混合（L598-L604），损失取 \( L=\langle C(p), g\rangle \)；
   - 反向：按 4.5.4 的递推算 `dL_dalpha`、`dL_dopacity_eff`；再补两行折算 `dL_do = dL_dopacity_eff*m`、`dL_dt += dL_dopacity_eff*o*m*(tau-t)/s_t**2`（backward.cu L768-L773；本例无 SH 项，`dL_dts` 只剩这一来源）；
   - 对拍：对每个 \( o_k,t_k \) 做中心差分（\( \varepsilon=10^{-6} \)），比较解析梯度与数值梯度。
3. **需要观察的现象**：两组梯度相对误差应小于 \(10^{-6}\)；把某个 \( t_k \) 挪到 \( |\tau-t_k|>2.45s_t \) 后，该高斯的解析与数值梯度**同时**归零（前向剔除使然）；调小 \( s_t \) 会看到时间梯度被 \( 1/s_t^2 \) 放大。
4. **预期结果**：一个 ~60 行、可复跑的验证脚本；它证明了你对「时间边缘化在 CUDA 中如何完成」与「梯度如何分配与折算」的理解没有偏差。若想进一步对拍真实 CUDA 版本，需编译本仓库扩展后在随机小输入上比较 `_C.rasterize_gaussians_4d` 的 autograd 梯度与本脚本（待本地验证，需 GPU 与编译环境）。
5. 示例代码骨架可直接扩展 4.4/4.5 的两段脚本，标注「示例代码」。

## 6. 本讲小结

- CUDA 前向是六阶段流水线：`preprocessCUDA`（逐高斯：4D 切片、EWA、conic、SH）→ `InclusiveSum` → `duplicateWithKeys`（64 位键 `tile<<32|depth`）→ 基数排序 → `identifyTileRanges` → `renderCUDA`（16×16 tile 从前往后 alpha blending）；`num_rendered` 是唯一 D2H 同步点。
- `ts/scales_t/rotations_r` 的消费点集中在 `forward.cu` 的 `computeCov3D_conditional`（L279-L352，Schur 补 + `marginal_t` + 均值偏移 + `opacity*=marginal_t`）、非 rot_4d 分支（L429-L435）与 `computeColorFromSH_4D` 的 `dir_t`（L83、L143、L163）；时间信息经 `conic_opacity` 的第 4 分量（有效不透明度）进入混合。
- 反向分两步：`renderCUDA` 反向用「从后往前 + 反除还原 \( T_i \) + 身后累积色递推 \( A_i \)」得到每像素的 \( \partial L/\partial\alpha_i \)，经 `atomicAdd` 分配到 color/mean2D/conic/opacity；`preprocessCUDA` 反向再链回 SH、尺度、双四元数与时间。
- 不透明度梯度三段式：逐像素 \( G\cdot\partial L/\partial\alpha_i \) 原子累加成 \( \partial L/\partial o_{\text{eff}} \)，乘 \( m \) 折算回 \( \partial L/\partial o \)，同时 \( \partial L/\partial m \) 继续链到 \( \sigma_t^2 \) 与 \( t \)；`dL_dts` 有 SH 与条件协方差两个来源。
- 前后向靠三块状态缓冲（Geometry/Binning/Image）+ `fromChunk` 对齐切割实现「前向存档、反向重建」，是 u4-l2 所说不透明句柄的底层机制。
- 两处源码观察供深入：`dt1_dt/dt2_dt` 的符号与 cos 的标准导数相反（backward.cu L303/L384）；非 rot_4d 的 4D 路径缺 `marginal_t` 折算（L913-L918）。均待本地验证。

## 7. 下一步学习建议

本讲之后，单元 8 还剩三个方向：**u8-l2** 转向 pointops2 的最远点采样与 KNN 算子（另一套「Python 包装 + CUDA 实现」的样板，可对照本讲的绑定层次）；**u8-l3** 回到系统层，把稀疏视角策略（MASt3R 初始化、opacity decay）与消融实验设计串起来；**u8-l4** 讲二次开发与调试工具箱。若想继续深挖本讲内容，建议通读 `rasterize_points.cu` 的 `markVisible` 包装与 `RasterizeGaussiansBackwardCUDA` 的梯度重排（承接 u4-l2 的 13 元组顺序），并尝试给 `preprocessCUDA` 加一段 `printf` 调试（需重新编译扩展，参考 u1-l2 的本地安装步骤）。
