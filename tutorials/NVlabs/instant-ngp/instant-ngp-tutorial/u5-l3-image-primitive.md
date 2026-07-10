# 图像原语：神经图像拟合

## 1. 本讲目标

学完本讲，你应当能够：

- 理解 instant-ngp 的「图像原语（Image）」本质上是一个 2 进 3 出的坐标回归网络——给定像素坐标 (x, y)，预测其 RGB 颜色，从而用一个小 MLP「记住」一整张图。
- 看懂 `load_image` 如何按扩展名分流到 stbi / EXR / binary 三条加载路径，并能说明 `.bin` 自定义格式为何能让 gigapixel（十亿像素级）大图加载更快。
- 掌握 `ERandomMode`（Random / Halton / Sobol / Stratified）四种像素采样策略的差异，以及它们在 `train_image` 中如何被选用。
- 能够修改 `configs/image/` 下的编码配置，做一次「换编码、看拟合质量」的对照实验。

本讲是 u5「其他原语」单元的第三篇。它承接 u3-l1（`reset_network` 与五大模型对象）和 u3-l2（多分辨率哈希编码），把同一套 HashGrid 编码 + FullyFusedMLP 的积木，套到最简单的二维回归任务上。正因为任务简单，图像原语是理解哈希编码「为什么能拟合高频细节」的最佳入门沙盒。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**坐标网络（coordinate network）。** 普通神经网络做的是「图片→标签」的分类。而图像原语做的是反过来：输入一个归一化坐标，输出这个坐标处的颜色。网络本身就是一个「可微分的、压缩的查表函数」。一旦训练完成，原图的每一个像素都被编码进了网络的权重（以及哈希编码的查找表）里。这和 NeRF（u4）的思想完全一致，只是维度从三维降到二维、输出从 [RGB, 密度] 降到 [RGB]。

**为什么需要编码（encoding）。** 一个裸坐标 (x, y) 直接喂给 MLP，网络很难学到图像里尖锐的边缘和纹理——这是因为 ReLU 网络对低维输入的「高频成分」天生迟钝（即所谓 *spectral bias*）。instant-ngp 的解法是在坐标进入 MLP 前，先用哈希编码把二维坐标「展开」成几十上百维的高维特征（u3-l2）。图像原语正好直观地展示了这一招的效果：开哈希编码，几秒就能拟合出清晰纹理；关掉编码，画面会糊成一片色块。

**归一化坐标约定。** 全图被映射到 [0, 1] × [0, 1] 的单位正方形，包围盒 `m_aabb` 恒为 `{vec3(0), vec3(1)}`。训练时采样的坐标、渲染时由相机投影得到的坐标，都在这个范围内。下文源码会反复出现这个约定。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/testbed_image.cu` | 图像原语全部实现：网络维度、四种采样核、训练 `train_image`、渲染 `render_image`、加载 `load_image`（含 stbi/exr/binary 三路）、全图 MSE 评测 `compute_image_mse`。本讲的中枢文件。 |
| `include/neural-graphics-primitives/testbed.h` | `Image` 结构体、`EDataType`、`Training` 子结构、`random_mode` 字段的定义。 |
| `include/neural-graphics-primitives/common.h` | `ERandomMode` 枚举与 `RandomModeStr` 字符串表。 |
| `configs/image/base.json` | 图像模式默认网络配置：HashGrid 编码 + FullyFusedMLP + L2 损失 + ExponentialDecay→Adam 优化器。 |
| `configs/image/{hashgrid,oneblob,frequency}.json` | 三份用于对照实验的编码变体配置（均 `parent` 继承 base.json）。 |
| `scripts/convert_image.py` | 把任意图片转成 `.bin` 的命令行脚本，调用 `common.py` 的 `read_image`/`write_image`。 |
| `scripts/common.py` | `.bin` 格式的 Python 端读写实现，与 C++ 的 `load_binary_image` 一一对应。 |
| `data/image/albert.exr` | 官方示例图（爱因斯坦肖像），EXR 格式，对应 README 的「Image of Einstein」。 |

提示：图像原语也复用 `src/testbed.cu` 的共用分发逻辑——加载走 `load_training_data`（`testbed.cu:171`），训练走 `train()`（`testbed.cu:4636`），渲染走 `render_frame`（`testbed.cu:4983`）。这些已在 u2-l2 讲过，本讲聚焦 `testbed_image.cu` 内部。

## 4. 核心概念与源码讲解

### 4.1 图像拟合：从 2D 坐标到 RGB 的回归

#### 4.1.1 概念说明

图像原语是四种基元里最简单的一个：它不涉及相机位姿、光线步进、几何求交，仅仅是一个二维回归任务。网络维度被写死成 2 输入、3 输出：

[include/neural-graphics-primitives/testbed.h:947-950 定义 `EDataType`，区分 Float（stbi/exr 路径）与 Half（.bin 路径）两种像素存储精度](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L947-L950)

这给后面 `eval_image_kernel_and_snap` 模板按精度分两个实例提供了依据。网络的输入输出维度在 `network_dims_image` 里固定：

```cpp
Testbed::NetworkDims Testbed::network_dims_image() const {
    NetworkDims dims;
    dims.n_input = 2;     // (x, y)
    dims.n_output = 3;    // (R, G, B)
    dims.n_pos = 2;
    return dims;
}
```

[src/testbed_image.cu:31-37 图像原语的网络维度恒为 2 进 3 出](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L31-L37)

#### 4.1.2 核心流程

整个图像原语的数据流可以画成一条直线：

```
原图(磁盘)
   │  load_image（按扩展名选 stbi / exr / bin）
   ▼
m_image.data（GPU 上的像素数组）+ m_image.resolution
   │  train_image 每步：
   │    1) 按随机模式生成 batch 个 (x,y) ∈ [0,1]²
   │    2) eval_image_kernel_and_snap 在原图上双线性插值出这些坐标的 RGB 真值
   │    3) m_trainer->training_step(坐标, 真值) 做一次前向+反向+优化器更新
   ▼
训练好的 m_network（含 HashGrid 查找表 + MLP 权重）
   │  render_image：init_image_coords 把屏幕像素映射成 (x,y) →
   │                m_network->inference 查询颜色 → shade_kernel_image 上屏
   ▼
画面 / 截图
```

注意第 2 步：训练的「真值」不是预先存好的，而是**每步临时从原图插值出来的**。这意味着坐标本身可以是任意连续值（包括 Halton/Sobol 这种拟随机序列），不必落在像素中心——双线性插值负责把它们对到原图上。

#### 4.1.3 源码精读

承载图像全部状态的 `Image` 结构体：

[include/neural-graphics-primitives/testbed.h:952-971 `Image` 结构体：`data` 存像素、`resolution` 存宽高、`Training` 子结构存采样坐标与真值缓冲、`random_mode` 默认 Stratified](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L952-L971)

几个要点：
- `data` 是 `GPUMemory<char>`，是「裸字节」容器，具体解释成 `float` 还是 `__half` 由 `type` 字段决定（与 4.2 的两条加载路径对应）。
- `render_coords` / `render_out` 是渲染期的临时缓冲，与训练期的 `training.positions` / `training.targets` 分开，避免互相干扰。
- `snap_to_pixel_centers`（默认 true）：训练坐标是否对齐到像素中心；关掉后用双线性插值，能学到亚像素细节。
- `linear_colors`（默认 false）：源图色彩空间标志，详见 4.4 的说明。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认图像原语确实只用了 2 维输入，并与 NeRF 对比。

1. 打开 `src/testbed_image.cu:31-37`，确认 `n_input=2, n_output=3`。
2. 在 `src/testbed_nerf.cu` 或 `include/neural-graphics-primitives/nerf_loader.h` 中找到 NeRF 的 `network_dims_nerf`，对比输入维度。
3. 在 `testbed.h` 中分别找到 `m_image` 与 `m_nerf` 两个内嵌结构体，对比字段数量。

**预期结果**：NeRF 的位置输入是 3 维（还需方向 3 维进颜色头），而图像原语只有 2 维输入、无方向；`m_nerf` 结构体远比 `m_image` 庞大（带数据集、密度网格、误差图等）。这正说明图像原语是「剥离了所有 3D 复杂度后的最简回归沙盒」。

#### 4.1.5 小练习与答案

**练习 1**：图像原语的网络输出为什么是 3 维而不是 4 维（NeRF 是 4 维）？
**答**：NeRF 的 4 维是 [R, G, B, 体密度 σ]，密度用于体渲染合成；图像原语是直接的颜色回归，每个坐标对应一个确定像素颜色，不需要透明度/密度，故只有 3 维 RGB。

**练习 2**：`m_image.data` 为什么用 `GPUMemory<char>` 而不是 `GPUMemory<float>`？
**答**：因为它要同时容纳 float（stbi/exr 路径）和 `__half`（.bin 路径）两种精度的像素。用 `char` 作「类型擦除」的裸字节缓冲，再由 `EDataType type` 字段在使用处决定如何 `reinterpret_cast`。

### 4.2 多格式加载：stbi / EXR / binary 三条路径

#### 4.2.1 概念说明

`load_image` 是图像原语的数据入口（由 `load_training_data` 在 `testbed.cu:174` 分发进来）。它按文件扩展名把加载分流到三个函数：

```cpp
void Testbed::load_image(const fs::path& data_path) {
    if (equals_case_insensitive(data_path.extension(), "exr")) {
        load_exr_image(data_path);
    } else if (equals_case_insensitive(data_path.extension(), "bin")) {
        load_binary_image(data_path);
    } else {
        load_stbi_image(data_path);   // 兜底：png/jpg/bmp/tga/...
    }
    m_aabb = m_render_aabb = BoundingBox{vec3(0.0f), vec3(1.0f)};
    m_render_aabb_to_local = mat3::identity();
    tlog::success() << "Loaded a " << ... << " pixels.";
}
```

[src/testbed_image.cu:393-407 `load_image` 按扩展名三路分发，并把包围盒固定为单位立方体](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L393-L407)

注意兜底逻辑：**任何不是 `.exr` / `.bin` 的文件都会走 stbi**，这与 u2-l3 讲过的 `mode_from_scene`「其余扩展名兜底为 Image」相吻合——比如拖入一张 `.png`，Testbed 会自动进入图像模式。

三条路径的关键差别在「精度」与「是否需要解码」：

| 路径 | 函数 | 解码方式 | `EDataType` | 典型格式 |
| --- | --- | --- | --- | --- |
| stbi | `load_stbi_image` | stb_image 解码 | Float | PNG / JPEG / BMP / TGA |
| EXR | `load_exr_image` | tinyexr 解码（HDR） | Float | `.exr` 高动态范围 |
| binary | `load_binary_image` | **无解码，裸读** | Half | `.bin` 自定义 |

#### 4.2.2 核心流程

**stbi / EXR 两条路径**结构几乎一致，差别只在解码库：

[src/testbed_image.cu:424-437 `load_stbi_image`：用 stb_image 解码到 GPU float 数组，标记为 Float 精度](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L424-L437)

[src/testbed_image.cu:409-422 `load_exr_image`：用 tinyexr 解码（支持 HDR），同样标记为 Float 精度](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L409-L422)

二者都是「库解码到 float → 拷到 GPU」，对大图来说，解码本身就慢、float 还占 4 字节/通道。

**binary 路径**则完全不同——它把已解码的原始像素直接以半精度存盘，加载时只做「读文件头 + 一次 `cudaMemcpy`」：

```cpp
std::ifstream f{native_string(data_path), std::ios::in | std::ios::binary};
f.read(reinterpret_cast<char*>(&m_image.resolution.y), sizeof(int));  // 先读高
f.read(reinterpret_cast<char*>(&m_image.resolution.x), sizeof(int));  // 再读宽
size_t n_pixels = (size_t)m_image.resolution.x * m_image.resolution.y;
m_image.data.resize(n_pixels * 4 * sizeof(__half));
std::vector<__half> image(n_pixels * 4);
f.read(reinterpret_cast<char*>(image.data()), sizeof(__half) * image.size());
CUDA_CHECK_THROW(cudaMemcpy(..., cudaMemcpyHostToDevice));
m_image.type = EDataType::Half;
```

[src/testbed_image.cu:439-457 `load_binary_image`：直接读宽高 + 半精度像素，无解码，标记为 Half 精度](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L439-L457)

这里有一个**容易踩坑的细节**：`.bin` 文件头先写的是 `resolution.y`（高），再写 `resolution.x`（宽），顺序与直觉相反。Python 端 `common.write_image` 用 `struct.pack("ii", img.shape[0], img.shape[1])` 写入，其中 `shape[0]` 恰好是高（行数）——两端严格对应，务必不要在二次开发时写反。

体积估算：一张 W×H 的图，
- stbi/exr 解码后 float：\(W \cdot H \cdot 4 \cdot 4\) 字节
- `.bin` 半精度：\(W \cdot H \cdot 4 \cdot 2\) 字节

对一张 gigapixel 图（\(W \cdot H \approx 10^9\)），float 约 16 GB，`.bin` 约 8 GB，且省去了 PNG/JPEG 解码的 CPU 开销。这就是 README 里说的「This custom format improves compatibility and loading speed when resolution is high」。

#### 4.2.3 源码精读：生成 `.bin` 的脚本

`scripts/convert_image.py` 很短，核心是调用 `common.read_image` 读原图、`common.write_image` 落盘成 `.bin`：

[scripts/convert_image.py:24-38 主流程：抬高 PIL 像素上限 → 读图 → 默认输出 `<input>.bin`，以 float16 写出](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/convert_image.py#L24-L38)

注意第 26 行 `PIL.Image.MAX_IMAGE_PIXELS = 10000000000`——这是为大图准备的：PIL 默认对超过 ~178M 像素的图会抛 `DecompressionBombWarning`，gigapixel 必然触发，故必须抬高上限。

Python 端 `.bin` 的读写与 C++ 端互为镜像，二者共同定义了这个自定义格式：

[scripts/common.py:149-164 `write_image` 的 `.bin` 分支：写两个 int32 头（高, 宽）后接 float16 像素](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/common.py#L149-L164)

[scripts/common.py:133-147 `read_image` 的 `.bin` 分支：读 (h, w) 头后按 float16 还原为 [h,w,4] 数组](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/common.py#L133-L147)

#### 4.2.4 代码实践（可操作）

**目标**：亲手把一张图转成 `.bin`，并验证它能被 instant-ngp 加载。

1. 准备一张大图（README 推荐东京街景 gigapixel 图；没有的话任意 PNG 也可）。
2. 运行（`requirements.txt` 已含 `imageio`、`pillow`）：
   ```bash
   python scripts/convert_image.py --input path/to/photo.jpg
   # 默认输出 path/to/photo.bin
   ```
3. 用生成的 `.bin` 启动 instant-ngp：
   ```bash
   ./instant-ngp path/to/photo.bin
   ```
4. **观察**：日志应打印 `Loaded a half-precision image with WxH pixels.`（注意是 **half** 精度，对应 `EDataType::Half`），与直接加载 `.exr` 时打印的 `full-precision` 形成对照。

**预期结果**：`.bin` 加载明显比等价的 `.png` 快（大图差距更大），且日志显示 half 精度。**待本地验证**：在无 GPU 或无编译产物的环境上无法实跑，可退化为阅读 `convert_image.py` 与 `load_binary_image` 对账格式。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `.bin` 一定要存 4 通道（RGBA），即使原图是 3 通道 RGB？
**答**：`common.write_image` 在 `img.shape[2] < 4` 时会 `np.dstack` 补一个全 1 的 alpha 通道；C++ 端 `eval_image_kernel_and_snap` 按 `(tvec<T,4>*)texture` 读取、固定步长 4。统一成 4 通道让 GPU 取样内核无需分支处理通道数。

**练习 2**：如果有人手写脚本生成了 `.bin`，但把头写成「先宽后高」，加载后会怎样？
**答**：`load_binary_image` 先把第一个 int 读进 `resolution.y`、第二个读进 `resolution.x`，于是宽高被对调，后续按 `resolution` 计算的下标与双线性插值会错位，画面会变形/错乱。这正是 4.2.2 强调顺序的原因。

### 4.3 像素采样策略：ERandomMode

#### 4.3.1 概念说明

图像原语每步训练只采样 `batch_size` 个像素（而非全图），用这些小批量算梯度。**用什么样的策略挑选这些坐标**，直接影响收敛速度与最终质量。instant-ngp 提供四种采样模式，定义在一个共享枚举里：

```cpp
enum class ERandomMode : int {
    Random,
    Halton,
    Sobol,
    Stratified,
    NumImageRandomModes,
};
static constexpr const char* RandomModeStr = "Random\0Halton\0Sobol\0Stratified\0\0";
```

[include/neural-graphics-primitives/common.h:90-97 `ERandomMode` 四种采样模式及配套字符串表（供 GUI 下拉框与 pyngp 使用）](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common.h#L90-L97)

四种模式的直觉：

- **Random**：均匀伪随机。最简单，但采样点容易聚团、留空，覆盖性差。
- **Halton**：基于 Halton 序列（以 2、3 为基的低差异序列）的**拟随机（quasi-random）**采样，分布更均匀，确定性可复现。
- **Sobol**：另一种低差异序列（Sobol 序列），同样分布均匀，常用于准蒙特卡洛。
- **Stratified**：分层抖动。先把 batch 划分成方格网，每个格子里放一个随机点，兼顾均匀与随机性。

默认值是 `Stratified`（见 `testbed.h:970` `ERandomMode random_mode = ERandomMode::Stratified;`），可在 GUI 的 `Training coords` 下拉框（`testbed.cu:1261`）或通过 pyngp 修改。

#### 4.3.2 核心流程

`train_image` 里的 `generate_training_data` lambda 按 `random_mode` 选不同内核生成坐标：

```
switch (random_mode):
  Halton    → halton23_kernel      (按 base_idx 推进，确定性序列)
  Sobol     → sobol2_kernel        (按 base_idx + seed 推进)
  Random    → generate_random_uniform  (纯随机)
  Stratified→ 先 generate_random_uniform，再 stratify2_kernel 做分层
随后用 eval_image_kernel_and_snap 把这些坐标对到原图取真值
```

注意一个细节：序列型采样（Halton/Sobol）用 `(size_t)batch_size * m_training_step` 作为 `base_idx`，这意味着**每一步推进到序列的下一段**，长期来看能覆盖整个 [0,1]²；而 Random/Stratified 每步独立重抽。

#### 4.3.3 源码精读

四种采样内核都很短，集中在本文件开头：

[src/testbed_image.cu:39-46 `halton23_kernel`：对每个输出元素，用 Halton 基 2 与基 3 生成二维低差异点](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L39-L46)

[src/testbed_image.cu:48-55 `sobol2_kernel`：调用 `ld_random_val_2d` 生成 Sobol 二维点，带 seed](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L48-L55)

[src/testbed_image.cu:66-82 `stratify2_kernel`：把 batch 当作 size×size 方格，把每个随机点限制在它所在的格子里，实现分层抖动](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L66-L82)

调度它们的分支逻辑：

```cpp
if (m_image.random_mode == ERandomMode::Halton) {
    linear_kernel(halton23_kernel, 0, stream, n_elements,
                  (size_t)batch_size * m_training_step, m_image.training.positions.data());
} else if (m_image.random_mode == ERandomMode::Sobol) {
    linear_kernel(sobol2_kernel, 0, stream, n_elements,
                  (size_t)batch_size * m_training_step, m_seed, m_image.training.positions.data());
} else {
    generate_random_uniform<float>(stream, m_rng, n_elements * n_input_dims,
                                   (float*)m_image.training.positions.data());
    if (m_image.random_mode == ERandomMode::Stratified) {
        // 仅当 batch_size 是 2 的幂且为完全平方数时才真正分层，否则降级为纯随机并告警
        ...
        linear_kernel(stratify2_kernel, 0, stream, n_elements, log2_batch_size, ...);
    }
}
```

[src/testbed_image.cu:241-260 采样模式分支：Halton/Sobol 走序列核；Random 直接均匀；Stratified 在均匀基础上再分层](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L241-L260)

`Stratified` 有两个隐含约束值得留意（代码里以 `tlog::warning` 告警）：batch_size 必须是 2 的幂（`is_pot`），且开方后仍为整数（即「完全平方数」个格子），否则无法整齐切成 size×size 网格，会**降级成纯 Random**。

#### 4.3.4 代码实践（可操作）

**目标**：直观对比四种采样模式对收敛的影响。

1. 用 `./instant-ngp data/image/albert.exr` 加载示例图。
2. 在 GUI 的 Rendering 面板找到 `Training coords` 下拉框（对应 `testbed.cu:1261`），依次切换 Random / Halton / Sobol / Stratified。
3. 每次切换后重新训练（Snapshot 面板或按重置），观察前几百步画面从噪声收敛起来的速度。

**预期结果**：Random 初期会出现明显「聚团/留白」造成的色块；Halton/Sobol 由于分布均匀，画面更平滑地填满；Stratified 在保留随机性的同时覆盖均匀，通常是默认首选。**待本地验证**：具体收敛步数依赖硬件与 batch_size。

**纯阅读型替代实践**：若无法运行 GUI，可在 `train_image` 的 `generate_training_data` lambda 入口处加一行 `tlog::info() << "mode=" << (int)m_image.random_mode;`，确认四种模式确实走到不同分支；并对照 `stratify2_kernel` 的 `log2_batch_size / 2` 推导出网格边长 `size = 1 << (log2_batch_size/2)`，自行验证 batch_size=65536 时 `size=256`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Halton/Sobol 的 `base_idx` 要乘以 `batch_size * m_training_step`？
**答**：低差异序列的特性是「连续取一段」覆盖均匀。每步推进 `batch_size` 个位置，保证每一步采样的是序列的「下一段」，避免重复采样同一些点；长期累计覆盖整个 [0,1]²。

**练习 2**：把 `random_mode` 设为 Stratified，但 `batch_size = 1000`（非 2 的幂），会发生什么？
**答**：`is_pot` 检查失败，打印 `Can't stratify a non-pot batch size` 告警，**降级为纯 Random**（不调用 `stratify2_kernel`），分层效果失效。

### 4.4 训练与渲染管线：train_image / render_image / compute_image_mse

#### 4.4.1 概念说明

前两节讲了「数据怎么来」「坐标怎么采」，本节把它们串成完整的训练步与渲染步，并解释两个贯穿全文件的关键开关：`snap_to_pixel_centers` 与 `linear_colors`。

- **`snap_to_pixel_centers`**（默认 true）：训练时把连续坐标吸附到最近的像素中心。这样真值就是确定的一个像素，等价于「逐像素记忆」，收敛快但学不到亚像素结构；关闭后改用双线性插值取真值，坐标可以是任意连续值。
- **`linear_colors`**（默认 false）：色彩空间标志。false 表示源图是 sRGB（普通照片/截图的默认情况），训练在 sRGB 空间进行；true 表示源图已是线性（如 HDR/EXR）。它同时控制「取真值」与「上屏着色」两处的 sRGB↔linear 转换，二者互为逆运算，保证网络所在空间与帧缓冲（线性）一致。

#### 4.4.2 核心流程

**一步训练 `train_image`**：

```
1. enlarge positions / targets 缓冲到 batch_size
2. generate_training_data():
     a. 按随机模式生成 batch 个 (x,y)
     b. eval_image_kernel_and_snap: 对每个 (x,y)，从 m_image.data
        双线性插值（或吸附像素中心）出 RGB，写入 targets
3. 把 positions 包成 2×batch 矩阵、targets 包成 3×batch 矩阵
4. m_trainer->training_step(stream, 输入矩阵, 目标矩阵)  ← 前向+损失+反向+优化器
5. 可选 m_loss_scalar.update(m_trainer->loss(...))       ← 取损失标量上 GUI
6. m_training_step++
```

**一次渲染 `render_image`**：

```
1. init_image_coords: 把屏幕每个像素经相机投影，求与 z=0.5 平面的交点，
   得到该像素对应的图像 (x,y)（图像被显示在 z=0.5 平面上）
2. eval_image_kernel_and_snap 或 m_network->inference:
     - m_render_ground_truth=true 时直接显示原图（取真值）
     - 否则用训练好的网络查询颜色
3. shade_kernel_image: 把颜色写入帧缓冲（必要时 srgb_to_linear）
```

**全图评测 `compute_image_mse`**：不采样，而是逐像素遍历整张图，算网络输出与原图的 MSE，可选 `quantize_to_byte`（先量化到 8bit 再算误差，更贴近人眼/显示效果）。

#### 4.4.3 源码精读

**取真值内核** `eval_image_kernel_and_snap` 是训练和渲染共用的「采样器」：

[src/testbed_image.cu:176-229 `eval_image_kernel_and_snap`：吸附像素中心时直接取整取值；否则做双线性插值；按 `linear_colors` 决定是否 linear_to_srgb](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L176-L229)

双线性分支（211-219 行）的四项加权是教科书式的面积加权插值：

```cpp
val = (1-wx)*(1-wy)*read_val(x,   y)
    + (  wx)*(1-wy)*read_val(x+1, y)
    + (1-wx)*(  wy)*read_val(x,   y+1)
    + (  wx)*(  wy)*read_val(x+1, y+1);
```

**训练步主体**：

[src/testbed_image.cu:231-302 `train_image`：生成坐标+取真值 → 构造输入/目标矩阵 → `m_trainer->training_step` → 取损失](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L231-L302)

注意它把坐标矩阵交给 `m_trainer`（u3-l1 讲过的「总指挥」），由 tiny-cuda-nn 完成前向（NetworkWithInputEncoding：HashGrid 编码 → FullyFusedMLP）、L2 损失、反向、ExponentialDecay→Adam 更新。本仓库在此只做数据搬运，真正的网络算子在依赖库里。

**渲染入口与真值/网络分支**：

[src/testbed_image.cu:304-391 `render_image`：`init_image_coords` 生成查询坐标 → 真值分支或 `m_network->inference` 查色 → `shade_kernel_image` 上屏](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L304-L391)

[src/testbed_image.cu:84-147 `init_image_coords`：把屏幕像素经相机投影到 z=0.5 平面，并把世界系 Y-up 翻转成图像系 Y-down](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L84-L147)

这里有个有意思的设计：图像本可当作纯 2D 任务，但代码特意把图「贴在 z=0.5 平面上」、用 3D 相机投影来生成查询坐标（注释 108-111 行解释了这是为了与 3D 任务共享相机代码、支持运动模糊等）。所以即便图像是 2D，渲染路径里仍有 `pixel_to_ray` 与平面求交 `t = (0.5 - ray.o.z) / ray.d.z`。

**全图 MSE 评测**：

[src/testbed_image.cu:490-547 `compute_image_mse`：分批遍历全图像素，逐像素算 `dot(diff,diff)/3`，最后 `reduce_sum/n_elements`](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L490-L547)

[src/testbed_image.cu:473-488 `image_mse_kernel`：单像素误差，`quantize_to_byte` 时先把预测量化到 8bit](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L473-L488)

这个 MSE 在 GUI 里对应一个数值显示（`testbed.cu:1650`），也是 pyngp 暴露给 Python 的 `compute_image_mse` 接口（`python_api.cu:659`），方便做自动化评测。

#### 4.4.4 代码实践（源码阅读型 + 可选运行）

**目标**：理解训练循环里哪些是「数据准备」、哪些是「网络计算」，并跑一次对照实验。

1. 在 `train_image` 中画出三个阶段的边界：坐标生成（241-260）、真值采样（262-288）、网络训练步（293-299）。
2. **对照实验**：用同一张图分别加载三份编码配置，记录 MSE：
   ```bash
   ./instant-ngp data/image/albert.exr                              # 默认 HashGrid
   ./instant-ngp data/image/albert.exr --network configs/image/frequency.json
   ./instant-ngp data/image/albert.exr --network configs/image/none.json 2>/dev/null || true
   ```
   先看三份配置的差异：
   - [configs/image/base.json:23-29](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/image/base.json#L23-L29)：HashGrid，n_levels=16，n_features_per_level=2，log2_hashmap_size=24。
   - [configs/image/frequency.json:1-7](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/image/frequency.json#L1-L7)：`Frequency`，n_frequencies=16，继承 base 其余字段。
   - [configs/image/oneblob.json:1-7](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/image/oneblob.json#L1-L7)：`OneBlob`，n_bins=32。

3. **观察**：HashGrid 能在很少的参数下快速拟合出胡须、衣纹等高频纹理；Frequency 收敛偏慢且对尖锐边缘更吃力（这正是 u3-l4 讲过的「越弱的编码越依赖大 MLP」）。读取启动日志里的 `total_encoding_params`/`total_network_params` 对比参数量。

**预期结果**：HashGrid 拟合质量与速度均优，体现哈希编码「参数少、高细节」的优势。**待本地验证**：精确 MSE 数值依赖硬件与训练步数。

#### 4.4.5 小练习与答案

**练习 1**：`compute_image_mse` 里 `quantize_to_byte=true` 与 `false` 算出的 MSE 哪个更大？为什么？
**答**：通常 `false`（不量化）更小。`quantize_to_byte` 把预测量化到 8bit 后再算误差，量化本身引入额外的舍入误差，使总误差增大；但它更贴近最终显示到屏幕（8bit/通道）时的真实观感。

**练习 2**：图像是 2D 任务，为什么 `render_image` 里要用 3D 相机和平面求交？
**答**：作者把图像「贴在 z=0.5 平面上」，复用 3D 任务的相机代码（`pixel_to_ray`），这样可共享运动模糊、视差、通用相机交互等逻辑（见 `init_image_coords` 108-111 行注释），代价只是多一次平面求交 `t = (0.5 - ray.o.z)/ray.d.z`。

## 5. 综合实践：端到端拟合一张图并评测

把本讲三块内容（多格式加载、采样策略、训练与评测）串成一个完整任务。

**任务**：把一张本地图片转成 `.bin`，用默认 HashGrid 配置训练，用 pyngp 跑自动化训练并打印 MSE，最后换一种编码复测对比。

**步骤**：

1. 转格式：
   ```bash
   python scripts/convert_image.py --input photo.jpg   # 生成 photo.bin
   ```
2. 编写评测脚本 `my_eval.py`（**示例代码**，仿照 `scripts/run.py` 的 pyngp 用法）：
   ```python
   import pyngp as ngp
   for net in ["base.json", "frequency.json"]:
       t = ngp.Testbed()
       t.mode = ngp.TestbedMode.Image
       t.load_training_data("photo.bin")
       reload = True
       t.reload_network_from_file(net) if False else t.reload_network_from_file("configs/image/" + net)
       for i in range(1000):
           t.train()
       print(net, "MSE =", t.compute_image_mse(quantize=True))
   ```
   （pyngp 的 `compute_image_mse` 绑定见 `src/python_api.cu:659`；`TestbedMode` 枚举与 `load_training_data` 的绑定见 `src/python_api.cu`，参考 u7-l1。）
3. **观察**：对比两种编码的 MSE 与训练耗时；检查日志中 `half-precision` 字样确认 `.bin` 路径生效。
4. **延伸**：把 `t.image.random_mode` 改成不同的 `ERandomMode`（pyngp 暴露见 `src/python_api.cu:874`），观察对 MSE 的影响。

**预期结果**：`.bin` 加载快、显存省；HashGrid 的 MSE 低于 Frequency（同等步数）。若环境无 GPU/未编译 pyngp，可退化为：阅读 `convert_image.py` + `load_binary_image` 对账 `.bin` 格式，并用 GUI 手动完成训练与读数。**待本地验证**具体数值。

## 6. 本讲小结

- 图像原语是 instant-ngp 最简单的基元：一个 2 进 3 出的坐标回归网络，把整张图「记忆」进 HashGrid 查找表 + 小 MLP，是理解哈希编码威力的最佳沙盒。
- `load_image` 按扩展名三路分发：`.exr`→tinyexr、`.bin`→裸读半精度、其余→stb_image；前两者为 Float 精度、`.bin` 为 Half 精度。
- `.bin` 自定义格式把已解码像素以 float16 直存，加载时「读 8 字节头 + 一次 `cudaMemcpy`」，让 gigapixel 大图加载又快又省内存；注意文件头先写高（`resolution.y`）再写宽。
- `ERandomMode`（Random/Halton/Sobol/Stratified）决定每步采哪些坐标；默认 Stratified 兼顾均匀与随机，但对 batch_size 有「2 的幂且完全平方」的约束，否则降级。
- `train_image` = 生成坐标 + 双线性/吸附取真值 + `m_trainer->training_step`；`render_image` 把图贴在 z=0.5 平面上、复用 3D 相机生成查询坐标；`compute_image_mse` 提供全图像素级评测，可选 8bit 量化。
- 网络算子（HashGrid 编码、FullyFusedMLP、L2 损失、Adam）来自依赖库 tiny-cuda-nn，本仓库只做数据搬运与调度——应用层与库层的边界在此清晰可见。

## 7. 下一步学习建议

- **横向对比体素原语**：本系列下一篇 u5-l4 讲体素原语，它同样做体积渲染，但数据来自 NanoVDB 而非 MLP；对比 m_image 与 m_volume 能加深对「数据来源决定管线」的理解。
- **回到编码本身**：若想深究 HashGrid 内部的哈希函数、三线性插值与碰撞回传（本讲只用了、没讲实现），需要跳出本仓库，进入 `dependencies/tiny-cuda-nn/` 阅读其 encoding 源码。
- **自动化进阶**：本讲的 pyngp 用法是 u7 单元的预览；学完 u7-l1（pyngp 绑定架构）和 u7-l2（run.py）后，可以把本讲的 MSE 对比脚本写得更完整，甚至批量评测多张图、多种编码。
- **二次开发**：参考 u8-l5，尝试在 `configs/image/` 下新建一份配置，调整 `n_levels` / `log2_hashmap_size` / `n_neurons`，用 `compute_image_mse` 量化「参数量 vs 拟合质量」的权衡。
