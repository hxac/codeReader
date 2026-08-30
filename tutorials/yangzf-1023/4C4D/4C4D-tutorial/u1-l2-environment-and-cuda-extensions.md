# 环境搭建与四个 CUDA 子包

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `environment.yml` 里每一段依赖的作用，理解「本地路径安装」这种特殊写法。
2. 解剖一个 CUDA 扩展的 `setup.py`：`CUDAExtension`、`BuildExtension`、pybind11 三者如何把 `.cu` 文件变成一个可以 `import` 的 Python 模块。
3. 列出四个 CUDA 子包（`diff_gaussian_rasterization`、`simple_knn`、`pointops2`、`fused_ssim`）各自暴露的 Python 接口入口，以及它们分别被主代码哪一行 `import`。
4. 在有 GPU 的机器上独立编译安装其中一个子包（`simple-knn`），并验证 `import` 成功；没有 GPU 时也能说清楚编译它需要哪些前置条件。

本讲承接 u1-l1：上一讲我们知道了 4C4D 是「Python 训练框架 + CUDA 加速算子」的组合，本讲就把这些 CUDA 算子一个一个认清楚——它们是训练能跑起来、能跑得快的物质基础。

## 2. 前置知识

### 2.1 为什么本项目离不开 CUDA 扩展

3DGS/4DGS 的核心计算有两类，纯 Python 都无法胜任：

- **渲染与反向传播**：每次训练迭代要把几百万个高斯投影到屏幕、按深度排序、做 alpha blending，然后还要把像素误差的梯度回传给每个高斯的属性。这必须写成并行 CUDA kernel。
- **点云几何计算**：初始化时要求每个点到最近邻的距离（决定高斯初始大小）、最远点采样（决定初始点云分布）。这些是 \( O(N^2) \) 级别的邻域计算，CUDA 并行后可以从分钟级降到毫秒级。

PyTorch 提供了 `torch.utils.cpp_extension.CUDAExtension`，让开发者用 C++/CUDA 写代码，再通过 pybind11 暴露成 Python 模块。**编译产物是一个 `.so` 动态库，Python 里 `import` 它就像 import 一个普通包**。4C4D 仓库里的四个子目录就是四个这样的扩展。

### 2.2 关键术语

| 术语 | 通俗解释 |
|---|---|
| conda environment.yml | 一份「配方文件」，`conda env create --file environment.yml` 会按它创建一个名字叫 `4c4d` 的隔离 Python 环境 |
| setuptools / setup.py | Python 的打包构建脚本，`pip install <目录>` 时会执行该目录下的 `setup.py` |
| `CUDAExtension` | PyTorch 提供的构建辅助类，告诉 setuptools「这些 `.cu` 源文件要用 nvcc 编译并链接成 Python 扩展模块」 |
| pybind11 | C++ 与 Python 之间的桥梁库，`PYBIND11_MODULE` 宏把 C++ 函数注册成 Python 可调用的函数 |
| nvcc | NVIDIA 的 CUDA 编译器，编译扩展必须有它（来自 CUDA Toolkit） |
| 可微光栅化（differentiable rasterization） | 渲染过程全程可导，渲染损失能通过 `loss.backward()` 一路传回高斯参数 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `environment.yml` | conda 环境配方：Python/PyTorch 版本 + pip 依赖 + 四个本地 CUDA 扩展的安装路径 |
| `diff-gaussian-rasterization/setup.py` | 4D 高斯可微光栅化扩展的构建脚本（本项目最核心的 CUDA 算子） |
| `simple-knn/setup.py` | 最近邻距离扩展的构建脚本 |
| `simple-knn/ext.cpp` | pybind11 绑定文件，只导出一个函数 `distCUDA2` |
| `pointops2/setup.py` | 点云采样扩展（最远点采样 / KNN）的构建脚本 |
| `fused-ssim-main/setup.py` | 融合 SSIM 损失扩展的构建脚本（支持 CUDA/MPS/XPU 多后端） |
| `scene/gaussian_model.py` | 主代码中 `distCUDA2` 的唯一使用处（高斯初始化） |
| `utils/general_utils.py` | 主代码中 `furthestsampling`/`knnquery` 的 import 与二次封装 |
| `train.py` | 主代码中 `fused_ssim` 的 import 与使用（损失 + 测试指标） |
| `gaussian_renderer/__init__.py` | 主代码中光栅化器的 import 与使用（渲染入口） |

## 4. 核心概念与源码讲解

### 4.1 environment.yml：一条 conda 命令装了什么

#### 4.1.1 概念说明

`environment.yml` 是整个环境的「总装清单」。它分两层：conda 层负责 Python 解释器、PyTorch 框架和 CUDA 运行时；pip 层负责纯 Python 小工具，以及**用本地路径安装四个 CUDA 扩展**。理解这份文件，就理解了 4C4D 环境的最小闭包。

#### 4.1.2 核心流程

按 `conda env create --file environment.yml` 执行时的顺序：

1. 创建名为 `4c4d` 的环境，从 `pytorch`、`conda-forge`、`defaults` 三个频道解析依赖。
2. 安装 conda 层依赖：`python=3.7.13`、`pytorch=1.12.1`（及配套的 `torchvision`/`torchaudio`）、`cudatoolkit=11.6`、`plyfile`、`colmap`。
3. 环境内切换到 pip，先装 5 个纯 Python 包（`tqdm`、`torchmetrics`、`imagesize`、`kornia`、`omegaconf`）。
4. pip 依次进入 `./simple-knn`、`./pointops2`、`./fused-ssim-main`、`./diff_gaussian_rasterization` 四个本地目录，执行各自的 `setup.py`——**这一步会调用 nvcc 编译 CUDA 代码**，编译产物（`.so` 文件）被安装进当前 conda 环境。

#### 4.1.3 源码精读

完整配方：[environment.yml:L1-L24](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/environment.yml#L1-L24)——定义环境名 `4c4d`、三个依赖频道，以及全部 conda/pip 依赖。

conda 层依赖：[environment.yml:L7-L14](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/environment.yml#L7-L14)——锁定 `python=3.7.13` + `pytorch=1.12.1` + `cudatoolkit=11.6` 这个较老的组合（4DGS 系代码的通行配置）；`plyfile` 用于读写高斯模型的 PLY 文件；`colmap` 直接把 SfM 重建工具装进环境，供数据准备阶段使用。

pip 层与本地路径安装：[environment.yml:L15-L24](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/environment.yml#L15-L24)——注意最后四行不是 PyPI 包名，而是**相对路径**：`./simple-knn`、`./pointops2`、`./fused-ssim-main`、`./diff_gaussian_rasterization`。pip 看到路径就会进入该目录就地构建安装，这正是四个 CUDA 扩展进入环境的方式。

README 中的安装命令：[README.md:L40-L45](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L40-L45)——官方安装入口就是上面这条 `conda env create` + `conda activate 4c4d`。

> **两个容易踩的坑（重要）**
>
> 1. **目录名不匹配**：`environment.yml` 第 24 行写的是 `./diff_gaussian_rasterization`（下划线），而仓库中的实际目录名是 `diff-gaussian-rasterization`（连字符）。直接 `conda env create` 时，pip 在解析到这一行会找不到该路径而报错。常见解决办法是改用手工安装：先装前三个，再执行 `pip install -e ./diff-gaussian-rasterization`（把 yml 里的路径改成连字符目录亦可）。待本地验证。
> 2. **`python install.py` 不属于本项目**：[README.md:L51-L57](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L51-L57) 中的 `install.py` 是外部仓库 MAtCha（MASt3R 稠密重建）的安装脚本；本仓库根目录经检索**不存在** `install.py`，不要在 4C4D 里找它。

#### 4.1.4 代码实践

1. **实践目标**：搞清楚环境的组成，并提前发现安装路上的坑。
2. **操作步骤**：
   - 打开 `environment.yml`，把依赖分成三列抄下来：conda 层 / pip 层纯 Python 包 / pip 层本地路径。
   - 用 `ls` 查看仓库根目录，把四个本地路径与实际目录名逐一比对。
   - 对照 README「Installation」小节，标出哪些命令属于 4C4D、哪些属于 MAtCha。
3. **需要观察的现象**：`./diff_gaussian_rasterization`（下划线）在仓库里找不到同名目录；实际目录是 `diff-gaussian-rasterization`。
4. **预期结果**：得到一张三列依赖表，并记录「目录名不匹配，需要手动 pip install」这一结论。
5. 本实践为纯阅读型，无需运行即成立；实际安装表现待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么四个 CUDA 扩展不发布到 PyPI，而是放在仓库里用本地路径安装？

**答案**：它们依赖特定版本的 PyTorch/CUDA 头文件，且 `diff-gaussian-rasterization` 在本项目中已被修改以支持 4D 高斯（时间维输入 `ts`、`scales_t`、`rotations_r` 等），与上游 3DGS 版本不兼容。本地路径安装保证用户拿到的就是与本框架代码配套的定制版本。

**练习 2**：`environment.yml` 里 `cudatoolkit=11.6` 和 `colmap` 分别服务于什么？

**答案**：`cudatoolkit` 提供 CUDA 运行时和 nvcc 编译环境，是四个扩展编译与运行的必要条件（`python=3.7.13` + `pytorch=1.12.1` + `cudatoolkit=11.6` 是 4DGS 系项目的经典锁版组合）；`colmap` 是 SfM 重建工具链，供数据准备阶段从多视角图像估计相机位姿与稀疏点云。

### 4.2 解剖一个 CUDA 扩展的 setup.py

#### 4.2.1 概念说明

四个子包的 `setup.py` 长得不一样，但骨架相同，都是三件事：

1. 用 `CUDAExtension(name=..., sources=[...])` 声明「扩展模块叫什么名字、由哪些源文件编译而来」；
2. 用 `cmdclass={'build_ext': BuildExtension}` 声明用 PyTorch 的构建器（它会自动带上 PyTorch 头文件路径、处理 ABI 兼容）；
3. C++ 侧用 `PYBIND11_MODULE` 把函数注册给 Python。

差异在于**扩展模块的命名方式**和**构建复杂度**，这正是本模块要对比的。

#### 4.2.2 核心流程

一个扩展从源码到 `import` 的生命周期：

```text
pip install ./simple-knn
  → 执行 simple-knn/setup.py
  → CUDAExtension 声明: 模块名 simple_knn._C, 源文件 [spatial.cu, simple_knn.cu, ext.cpp]
  → BuildExtension 调用 nvcc 编译 .cu、调用 c++ 编译 ext.cpp（内含 PYBIND11_MODULE）
  → 链接生成 simple_knn/_C.so 装入 site-packages
  → Python: from simple_knn._C import distCUDA2
```

四个扩展模块名对比（**重点**，决定了主代码怎么 import）：

| 子包 | setup.py 包名 | CUDA 扩展模块名 | 命名风格 |
|---|---|---|---|
| simple-knn | `simple_knn` | `simple_knn._C` | 包内子模块 `_C` |
| diff-gaussian-rasterization | `diff_gaussian_rasterization` | `diff_gaussian_rasterization._C` | 包内子模块 `_C` |
| pointops2 | `pointops2` | `pointops2_cuda` | **顶层独立模块**（不在包内） |
| fused-ssim-main | `fused_ssim` | `fused_ssim_cuda`（按后端可为 `_mps`/`_xpu`） | **顶层独立模块**，按后端三选一 |

#### 4.2.3 源码精读

最简形态：[simple-knn/setup.py:L21-L35](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/simple-knn/setup.py#L21-L35)——整个构建声明不到 15 行：3 个源文件编译成 `simple_knn._C`，无额外编译参数（仅 Windows 下加 `/wd4624` 关闭一个编译警告）。

最核心的扩展：[diff-gaussian-rasterization/setup.py:L17-L34](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/setup.py#L17-L34)——5 个源文件（`rasterizer_impl.cu`、`forward.cu`、`backward.cu`、`rasterize_points.cu`、`ext.cpp`）编译为 `diff_gaussian_rasterization._C`；nvcc 参数 `-O3`，并通过 `-I third_party/glm/` 引入 glm 头文件库（光栅化中的矩阵运算使用）。

自动收集源文件 + 顶层模块命名：[pointops2/setup.py:L11-L30](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/setup.py#L11-L30)——用 `os.walk('src')` 把 src 下所有 `.cpp`/`.cu` 收进 sources；CUDA 扩展名是 `pointops2_cuda`（顶层模块）；另注意 `package_dir={"pointops2": "functions"}`：**Python 包 `pointops2` 的内容映射自 `functions/` 目录**，这个细节在 4.4 节还会展开。

多后端探测：[fused-ssim-main/setup.py:L99-L107](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/fused-ssim-main/setup.py#L99-L107)——安装时先检查 `torch.cuda.is_available()`，其次 Apple MPS，再次 Intel XPU，据此选择 `CUDAExtension`/`CppExtension` 与对应源文件（`ssim.cu`/`ssim.mm`/`ssim_sycl.cpp`）；GPU 架构选择逻辑在 [fused-ssim-main/setup.py:L18-L69](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/fused-ssim-main/setup.py#L18-L69)：优先读 `CUDA_ARCHITECTURES` 环境变量，其次用 `torch.cuda.get_device_capability()` 探测本机 GPU，都失败则回退到 sm_75/80/89 三个架构。最终安装声明在 [fused-ssim-main/setup.py:L124-L140](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/fused-ssim-main/setup.py#L124-L140)。

#### 4.2.4 代码实践

1. **实践目标**：建立「读 setup.py 就能推断出 import 语句」的能力。
2. **操作步骤**：逐一打开四个 `setup.py`，填写下面的表（每列都直接来自源码）：

   | 子包 | 源文件个数 | 扩展模块名 | nvcc 优化参数 | 特殊之处 |
   |---|---|---|---|---|
   | simple-knn | 3 | `simple_knn._C` | （无） | 无 Python 包目录，纯扩展 |
   | diff-gaussian-rasterization | 5 | `diff_gaussian_rasterization._C` | `-O3` | 额外引入 glm 头文件 |
   | pointops2 | ? | `pointops2_cuda` | `-O2` | `os.walk` 收集源文件 |
   | fused-ssim-main | 2 | ? | `-O3` + fast_math | 多后端探测 |

3. **需要观察的现象**：pointops2 的源文件个数需要你数 `src/` 目录下 `.cpp`/`.cu` 文件个数；fused-ssim 的扩展模块名随本机后端变化。
4. **预期结果**：表格填满后，对着「扩展模块名」一列，你应该能直接写出四条主代码 import 语句（答案见 4.4 的对照表）。
5. 本实践为纯阅读型，结论可直接从源码验证。

#### 4.2.5 小练习与答案

**练习 1**：`CUDAExtension` 与普通 `Extension` 的区别是什么？`BuildExtension` 又额外做了什么？

**答案**：`CUDAExtension` 会把 `.cu` 文件交给 nvcc 编译、并自动链接 CUDA 运行时库；`BuildExtension` 是 PyTorch 提供的构建器，自动添加 PyTorch 的头文件与库路径、检查编译器 ABI 与当前 PyTorch 是否兼容（不兼容直接报错，避免装出一个 import 即崩的模块）。

**练习 2**：为什么 fused-ssim 的 `setup.py` 顶部要 `import torch`？

**答案**：构建期就要调用 `torch.cuda.is_available()`、`torch.cuda.get_device_capability()` 等接口来决定后端与目标架构（[fused-ssim-main/setup.py:L1-L5](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/fused-ssim-main/setup.py#L1-L5)），所以它必须在**已装有 PyTorch 的环境里**执行 `pip install`——这也是四个扩展都要在 conda 环境建好之后再安装的原因。

### 4.3 simple_knn._C：一个最小 CUDA 扩展样本

#### 4.3.1 概念说明

四个扩展里 `simple-knn` 是最好的学习样本：它只有一个导出函数、没有 Python 包装层、用途单一——**在高斯初始化时为每个点估计一个合理的初始尺度**。把它彻底搞懂，其余三个扩展不过是同样套路的不同规模。

#### 4.3.2 核心流程

`distCUDA2` 在初始化链条中的位置（对应 `create_from_pcd`，详见 u3-l5）：

```text
输入点云 pcd.points (N×3, numpy)
  → 送入 distCUDA2（CUDA KNN）
  → 返回 dist2: 每个点到近邻的平均距离平方 (N,)
  → clamp_min(dist2, 1e-7) 防止 0
  → scales = log(sqrt(dist2))   ← 高斯初始空间尺度，取 log 存进参数
```

直觉：**点密集的地方高斯初始化得小，点稀疏的地方高斯初始化得大**——让每个高斯的初始体积与局部点云密度匹配，避免一开始就过度重叠或过度稀疏。

#### 4.3.3 源码精读

pybind11 注册处：[simple-knn/ext.cpp:L15-L17](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/simple-knn/ext.cpp#L15-L17)——整个扩展对 Python 只暴露一个函数：`m.def("distCUDA2", &distCUDA2);`，即 `simple_knn._C.distCUDA2`。

主代码唯一 import 处：[scene/gaussian_model.py:L20](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L20)——`from simple_knn._C import distCUDA2`，直接从编译出的 `.so` 拿函数，中间没有任何 Python 文件。

实际调用处：[scene/gaussian_model.py:L422-L425](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L422-L425)——`dist2 = torch.clamp_min(distCUDA2(points.float().cuda()), 0.0000001)`，随后 `scales = torch.log(torch.sqrt(dist2))[..., None].repeat(1, 3)` 把标量距离扩展成 x/y/z 三轴尺度。注意第 427 行被注释掉的 `dist_t = ...distCUDA2(...)`：时间维尺度**没有**用 KNN 估计，而是固定取 `time_duration / 5`（第 428 行）。

#### 4.3.4 代码实践

1. **实践目标**：亲手编译安装一个最小 CUDA 扩展并验证可用。
2. **操作步骤**（有 NVIDIA GPU 与 nvcc 的机器）：
   ```bash
   conda activate 4c4d          # 或任何装有 torch 的环境
   pip install -e ./simple-knn  # 就地编译安装
   python -c "import torch; from simple_knn._C import distCUDA2; \
     pts = torch.rand(1000, 3).float().cuda(); \
     print(distCUDA2(pts).shape, distCUDA2(pts)[:5])"
   ```
   无 GPU 时改为完成「编译依赖清单」：确认本机已装 `pytorch`（构建期 `import torch` 要用）、`CUDA Toolkit`（提供 nvcc，`which nvcc` 可查）、与 PyTorch 匹配的 C++ 编译器（Linux 为 g++），并说明缺一不可的原因。
3. **需要观察的现象**：安装过程会打印 nvcc 编译 `spatial.cu`、`simple_knn.cu` 的命令行；验证命令输出形状 `(1000,)` 与 5 个正数距离值。
4. **预期结果**：`import` 无报错、输出形状为 `(1000,)`、所有值 > 0（近邻距离平方非负）。
5. 本次生成环境无 GPU，以上命令**未实际运行**，结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_scaling` 里存的是 `log(sqrt(dist2))` 而不是 `sqrt(dist2)` 本身？

**答案**：训练时优化的是无约束的实数参数，渲染前经 `exp` 激活保证尺度恒为正（3DGS 的标准做法）。`scene/gaussian_model.py` 中 `get_scaling` 这个 property 做的就是 `exp` 变换，所以初始化时先取好 `log` 存进去，二者正好互逆。

**练习 2**：如果去掉 `clamp_min(dist2, 1e-7)`，什么时候会出问题？

**答案**：当点云里存在重复点（两个点坐标完全相同）时，最近邻距离为 0，`sqrt(0)=0`，`log(0)=-inf`，初始尺度变成负无穷，训练一开始就会产生 NaN。`clamp_min` 就是这个数值防线。

### 4.4 四个子包的接口地图：谁暴露什么、被谁 import

#### 4.4.1 概念说明

前三个模块看的是「怎么装」，本模块回答「装完之后长什么样」：四个扩展各自的 Python 入口文件是什么、暴露哪些符号、主代码在哪一行取用它们。这张地图是后续阅读渲染（u4）与训练（u5）时反复回查的工具。

#### 4.4.2 核心流程

一次训练启动时，四个扩展按以下 import 链被加载：

```text
train.py
 ├─ from fused_ssim import fused_ssim as fast_ssim        (train.py:36)
 ├─ import scene → scene/gaussian_model.py
 │    └─ from simple_knn._C import distCUDA2              (gaussian_model.py:20)
 ├─ import utils.general_utils
 │    └─ from pointops2.functions.pointops import furthestsampling, knnquery  (general_utils.py:17)
 └─ import gaussian_renderer
      └─ from diff_gaussian_rasterization import GaussianRasterizationSettings, GaussianRasterizer  (gaussian_renderer/__init__.py:15)
```

#### 4.4.3 源码精读

**① diff_gaussian_rasterization —— 4D 高斯可微光栅化（核心）**

绑定入口：[diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py:L15](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L15)——`from . import _C` 加载编译出的 CUDA 模块；前向在 [diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py:L108-L114](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L108-L114) 调 `_C.rasterize_gaussians(...)`，反向调 `_C.rasterize_gaussians_backward(...)`。该文件同时定义了纯 Python 的配置类 `GaussianRasterizationSettings` 与包装类 `GaussianRasterizer`（[L206-L240](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L206-L240)），后者还封装了 `_C.mark_visible` 做视锥剔除。

主代码取用处：[gaussian_renderer/__init__.py:L15](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L15) 只 import 两个类；[L36-L55](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L36-L55) 用相机与模型参数组装 `GaussianRasterizationSettings`（含 `timestamp`、`time_duration`、`rot_4d`、`gaussian_dim` 等 4D 专属字段），[L57](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L57) 构造光栅化器，[L63-L69](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L63-L69) 中 opacity decay 分支还调用了 `rasterizer.markVisible(means3D)` 来取得空间可见性掩码——这正是 u1-l1 提到的衰减接入点的底层支撑。

**② simple_knn —— 初始化尺度估计**

见 4.3，入口即 `simple_knn._C`，无 Python 包装文件。

**③ pointops2 —— 点云采样算子**

Python 包装：[pointops2/functions/pointops.py:L11](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/functions/pointops.py#L11)——`import pointops2_cuda as pointops_cuda` 直接加载顶层 CUDA 模块；[L14-L31](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/functions/pointops.py#L14-L31) 用 `torch.autograd.Function` 包装出 `furthestsampling`（最远点采样），[L34-L49](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/functions/pointops.py#L34-L49) 包装出 `knnquery`（K 近邻查询）。

主代码取用处：[utils/general_utils.py:L17](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L17) import 两个算子；[L170-L184](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L170-L184) 的 `knn()` 与 [L186-L194](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L186-L194) 的 `fps()` 把它们二次封装成 batch 接口——用 `torch.cumsum` 把每批点数构造成 `offset` 数组，这是 pointops2 约定的「变长批次」表示法（u8-l2 会深入）。

> **一个值得注意的细节**：`setup.py` 里 `package_dir={"pointops2": "functions"}` 意味着 pip 安装出的 `pointops2` 包内容直接来自 `functions/` 目录（安装后布局是 `pointops2/pointops.py`）；而主代码 import 的是 `pointops2.functions.pointops`，这个路径对应的是**源码树**布局（`pointops2/functions/pointops.py`）。由于 `train.py` 从仓库根目录启动时 `sys.path[0]` 就是仓库根，本地的 `pointops2/` 目录（内含空的顶层 `__init__.py` 和 `functions/` 子包）会先于 site-packages 中的安装包被解析——这就是该 import 能工作的原因。待本地验证。

**④ fused_ssim —— CUDA 版 SSIM**

按后端加载：[fused-ssim-main/fused_ssim/__init__.py:L5-L10](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/fused-ssim-main/fused_ssim/__init__.py#L5-L10)——CUDA 可用则 `from fused_ssim_cuda import fusedssim, fusedssim_backward`，MPS/XPU 各有对应分支；对外的 `fused_ssim(img1, img2, ...)` 在 [L41-L49](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/fused-ssim-main/fused_ssim/__init__.py#L41-L49)，内部走 `FusedSSIMMap` 这个 autograd.Function 并返回 `map.mean()`。

主代码取用处：[train.py:L36](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L36) `from fused_ssim import fused_ssim as fast_ssim`；训练损失 [train.py:L144](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L144) `Lssim = 1.0 - fast_ssim(...)`，测试指标 [train.py:L337](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L337) 也用一个函数两处复用。

**汇总表（本讲的核心产出）**

| 扩展目录 | 暴露的 Python 模块/符号 | 被主代码 import 的位置 | 用途 |
|---|---|---|---|
| `simple-knn/` | `simple_knn._C.distCUDA2` | `scene/gaussian_model.py:20` | 初始化时按近邻距离估计高斯空间尺度 |
| `pointops2/` | `pointops2.functions.pointops` 的 `furthestsampling`、`knnquery`（底层是 `pointops2_cuda`） | `utils/general_utils.py:17` | 初始点云的最远点采样下采样与 KNN 邻域查询 |
| `fused-ssim-main/` | `fused_ssim.fused_ssim`（底层是 `fused_ssim_cuda`） | `train.py:36` | 训练损失与测试指标中的 CUDA 加速 SSIM |
| `diff-gaussian-rasterization/` | `GaussianRasterizationSettings`、`GaussianRasterizer`（底层是 `diff_gaussian_rasterization._C`） | `gaussian_renderer/__init__.py:15` | 4D 高斯可微光栅化：前向渲染 + 反向梯度 + `markVisible` 视锥剔除 |

#### 4.4.4 代码实践

1. **实践目标**：亲手验证上表的 import 关系，确认「扩展名 ↔ import 行」一一对应。
2. **操作步骤**：在仓库根目录执行（或用编辑器全局搜索）：
   ```bash
   grep -rn "from simple_knn" --include="*.py" .
   grep -rn "from pointops2" --include="*.py" .
   grep -rn "from fused_ssim" --include="*.py" .
   grep -rn "from diff_gaussian_rasterization" --include="*.py" .
   ```
   然后把每条命中（排除扩展目录自身）填进 4.4.3 的汇总表。
3. **需要观察的现象**：四条 grep 各自在主代码（`scene/`、`utils/`、`train.py`、`gaussian_renderer/`）中恰有一处命中；扩展目录内部的命中（如 `pointops2/functions/pointops.py:11`）属于扩展自身，不算主代码取用。
4. **预期结果**：得到与 4.4.3 汇总表完全一致的 import 清单——这张表就是后续所有讲义里「主框架 ↔ CUDA 算子」的对照字典。
5. grep 属只读检索，可在任何环境执行；若本机无 grep 环境也可用编辑器搜索完成，结果一致。

#### 4.4.5 小练习与答案

**练习 1**：四个扩展中哪两个的 CUDA 模块名**不是**「包名 + `._C`」的形式？这种命名带来什么后果？

**答案**：`pointops2`（模块名 `pointops2_cuda`）和 `fused_ssim`（模块名 `fused_ssim_cuda`/`_mps`/`_xpu`）。后果是这些 `.so` 会以**顶层模块**身份装进 site-packages，与 Python 包同名但彼此独立；fused-ssim 更是因为模块名随硬件后端变化，必须在 Python 侧用 `if torch.cuda.is_available()` 等条件分支来 import（`fused_ssim/__init__.py` 第 5-10 行）。

**练习 2**：`distCUDA2` 和 `knnquery` 都做近邻计算，为什么 4C4D 同时保留两个扩展而不合并？

**答案**：二者接口形态与使用场景不同。`distCUDA2` 是无 batch 的单函数（输入 N×3 点云即可），只服务初始化这一处；`knnquery` 遵循 pointops2 的 `(offset, new_offset)` 变长批次约定，还配套 `furthestsampling` 等一族算子，服务数据加载阶段的下采样。它们来自不同的上游项目（simple-knn 出自 3DGS 官方，pointops2 出自点云注意力工作），4C4D 各取所需、不做合并，属于研究代码的典型取舍。

**练习 3**：如果 `diff_gaussian_rasterization` 没装成功，训练会在哪一步报错？

**答案**：不是在 `python train.py` 回车瞬间，而是在模块导入阶段——`train.py` 导入 `gaussian_renderer`，后者第一屏就执行 `from diff_gaussian_rasterization import ...`（`gaussian_renderer/__init__.py:15`），立刻抛出 `ModuleNotFoundError`。这也是排查环境问题时最先该检查的四个 import 之一。

## 5. 综合实践

**任务：为 4C4D 写一份《CUDA 依赖体检报告》。**

1. **整理对照表**：完成 4.2.4 与 4.4.4 两张表，合并成一张总表，包含：扩展目录 / setup.py 包名 / CUDA 模块名 / Python 入口文件 / 暴露符号 / 主代码 import 行号 / 一句话用途。
2. **画 import 依赖图**：以 `train.py` 为根，按 4.4.2 的流程画出「主框架文件 → CUDA 扩展」的四条边，标注每条边上的 import 行号。
3. **体检清单**：写一份 10 行以内的安装自检清单，每行一个可执行命令与预期输出，例如：
   ```bash
   python -c "import torch; print(torch.__version__, torch.cuda.is_available())"   # 1.12.1 True
   python -c "from simple_knn._C import distCUDA2; print('ok')"                     # ok
   python -c "from pointops2.functions.pointops import furthestsampling; print('ok')"  # ok
   python -c "from fused_ssim import fused_ssim; print('ok')"                       # ok
   python -c "from diff_gaussian_rasterization import GaussianRasterizer; print('ok')" # ok
   ```
4. **（有 GPU 时）实测一个扩展**：按 4.3.4 安装 `simple-knn` 并跑通 `distCUDA2` 最小示例，把输出贴进报告。
5. **预期成果**：一份可以直接交给新同学的自检文档；四个 `print('ok')` 全部通过即说明环境的核心依赖闭包完整（剩余的纯 Python 依赖问题会在实际启动训练时才暴露）。本次生成环境无 GPU，第 4 步待本地验证。

## 6. 本讲小结

- `environment.yml` 分 conda 层（Python 3.7.13 + PyTorch 1.12.1 + cudatoolkit 11.6 + colmap）与 pip 层，pip 层用**本地路径**安装四个 CUDA 扩展；其中 `./diff_gaussian_rasterization`（下划线）与实际目录 `diff-gaussian-rasterization`（连字符）不一致，需要手动补装。
- CUDA 扩展的骨架是 `CUDAExtension`（声明模块名与源文件）+ `BuildExtension`（PyTorch 构建器）+ `PYBIND11_MODULE`（C++ 函数注册给 Python）。
- `simple_knn._C` 只导出一个 `distCUDA2`，在 `scene/gaussian_model.py:422` 用于按近邻距离初始化高斯空间尺度（`log(sqrt(dist2))`）。
- 四个扩展的入口各不相同：`simple_knn._C`（裸模块）、`pointops2.functions.pointops`（autograd 包装 + `pointops2_cuda` 底层）、`fused_ssim`（按硬件后端三选一加载）、`diff_gaussian_rasterization`（`GaussianRasterizer` 类封装 `_C`）。
- `train.py` 启动时就会沿四条 import 链把四个扩展全部加载，任何一个缺失都会在导入阶段直接 `ModuleNotFoundError`。

## 7. 下一步学习建议

下一讲（u1-l3「目录结构与两大入口」）将把视角从 CUDA 子包拉回 Python 主框架：梳理 `scene`、`module`、`gaussian_renderer`、`utils`、`scripts`、`configs` 各目录的职责，并从 `train.py` 与 `render.py` 的入口函数走一遍顶层调用链。之后建议重点阅读的源码：

- `gaussian_renderer/__init__.py` 的 `render()`（u4-l1 精读，本讲已见到它的第 36/57/66 行）；
- `scene/gaussian_model.py` 的 `create_from_pcd`（u3-l5 精读，本讲已见到 `distCUDA2` 调用处）；
- `diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py` 全文（不到 300 行，是理解 Python↔CUDA 边界的最佳材料，u4-l2 精读）。
