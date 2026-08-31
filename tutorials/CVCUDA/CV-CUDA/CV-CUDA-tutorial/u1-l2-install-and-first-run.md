# 安装与第一个程序：pip wheel 快速上手

## 1. 本讲目标

学完本讲，你应该能够：

1. 根据自己机器的 CUDA 版本与驱动版本，从 PyPI 安装正确的 CV-CUDA wheel（`cvcuda-cu12` 或 `cvcuda-cu13`），并完成安装后的导入验证。
2. 独立运行 `samples/applications/hello_world.py`，走完一条「JPEG 解码 → 缩放 → 批合并 → 高斯模糊 → 拆批 → JPEG 编码」的全 GPU 图像处理流水线。
3. 逐行读懂这个示例脚本，说清楚 **nvimgcodec（负责解码/编码）与 CV-CUDA（负责像素计算）** 各自承担的环节，以及数据在每一步停留在 CPU 还是 GPU。
4. 通过修改高斯模糊的 kernel 尺寸与 sigma 两组参数，直观感受参数对输出图像的影响。

本讲是手册中第一讲「不用看库源码、只装包就能跑」的一讲：我们先建立「跑得起来」的手感，再在后续单元逐步深入张量模型（u2）与算子内部实现（u5）。

## 2. 前置知识

本讲需要的背景概念不多，全部用通俗语言解释一遍。上一讲（u1-l1）已经介绍过的术语（算子、张量、pybind11、cudaStream、DLPack）这里不再重复定义。

### 2.1 pip、wheel 与虚拟环境

- **pip**：Python 的包管理器，`pip install 包名` 会从 [PyPI](https://pypi.org) 下载并安装包。
- **wheel（`.whl`）**：Python 的预编译二进制包格式。CV-CUDA 的 wheel 里**同时打包了 C++/CUDA 动态库和 Python 绑定**，所以装一个 wheel 就等于装好了整套库，不需要本地编译器。
- **虚拟环境（venv）**：一个隔离的 Python 包目录。官方示例统一推荐先 `python3 -m venv venv_samples && source venv_samples/bin/activate` 再装依赖，避免污染系统 Python。下文的 `pip` 都假定在激活的虚拟环境里执行。

### 2.2 NVIDIA 驱动与 CUDA 版本：装 cu12 还是 cu13

初学者最容易混淆的两个东西：

- **NVIDIA 驱动（如 r525、r580）**：装在操作系统里的显卡驱动，版本号形如 `535.xx`。用 `nvidia-smi` 查看。
- **CUDA 版本（12.x / 13.x）**：CV-CUDA 编译时链接的 CUDA 运行时大版本。CV-CUDA 为此发布两个独立的 wheel：`cvcuda-cu12` 与 `cvcuda-cu13`。

两者的对应关系（来自官方安装文档）：CUDA 12.x 包要求驱动 ≥ r525（但运行本讲的 samples 要求 ≥ r535），CUDA 13.x 包要求驱动 ≥ r580。**同一台机器上 cu12 与 cu13 的 CV-CUDA 包不能共存**，切换大版本前必须先卸载旧包——这是初学者的第一大坑，后文源码精读会给出出处。

另外一个实用结论：**CV-CUDA Samples 目前只官方支持 CUDA 12**（README 的 Known limitations 明确写了这一条），所以本讲推荐初学者首选 `cvcuda-cu12` 路线；如果你的环境只有 CUDA 13，hello_world 通常也能工作，但属于非官方支持组合。

### 2.3 数据「不落回 CPU」：本讲的主线命题

传统图像管线里，解码后的像素在 CPU 内存，要传给 GPU 计算，算完再拷回 CPU 编码保存——两次 PCIe 搬运往往是瓶颈。CV-CUDA 的卖点是像素**从解码到编码全程留在 GPU 显存**，CPU 只负责下发指令和处理压缩后的字节流。hello_world.py 的文档字符串原话是 "All without leaving the GPU."。理解「每一步数据在哪」是本讲的主线之一，4.3 节会专门追踪。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md) | 项目主页：pip 安装命令表、平台兼容矩阵、已知限制（cu12/cu13 互斥等） |
| [docs/sphinx/installation.rst](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/installation.rst) | 完整安装文档：前置条件、wheel/deb/tar 三种预编译包 |
| [docs/sphinx/getting_started.rst](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/getting_started.rst) | 5 分钟快速上手：建 venv、装依赖、运行 hello_world |
| [samples/applications/hello_world.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py) | 本讲主角：全 GPU 图像处理示例 |
| [samples/common.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py) | 示例公共工具：`get_cache_dir`（输出目录）、`zero_copy_split`（零拷贝拆批） |
| [samples/requirements.samples.hello_world_cu12.template](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/requirements.samples.hello_world_cu12.template) | hello_world 最小依赖清单的模板源文件（4 个包） |
| [python/mod_cvcuda/operators/OpGaussian.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpGaussian.cpp) | `cvcuda.gaussian` 的 pybind11 绑定，用于说明脚本里那次调用的真实签名 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 安装通道与兼容性矩阵**、**4.2 hello_world 六步流水线**、**4.3 数据落点追踪**。

### 4.1 安装通道与兼容性矩阵

#### 4.1.1 概念说明

CV-CUDA 有三种预编译分发方式，加上源码编译共四条安装路线：

| 方式 | 面向人群 | 本讲是否使用 |
|------|---------|------------|
| Python wheel（PyPI） | Python 用户，一条 `pip install` 搞定 | ✅ 本讲主线 |
| Debian 包（`.deb`） | 需要系统级安装 C++ 库/头文件的团队 | 否（u1-l3 之外的进阶话题） |
| tar 压缩包（`.tar.xz`） | 需要可移植安装到任意 Linux 发行版的场景 | 否 |
| 源码编译 | 贡献者/需要自定义编译目标的用户 | 否（下一讲 u1-l3 专门讲） |

为什么 Python 用户首选 wheel？因为 CV-CUDA 本质是一个 C++/CUDA 库加一层 pybind11 绑定（u1-l1 讲过的四层架构），wheel 把编译产物直接分发给你，绕开了本地 CUDA 编译工具链的全部复杂性。

#### 4.1.2 核心流程

选择并安装的正确决策流程：

```text
1. nvidia-smi 查看驱动版本
   ├─ 驱动 ≥ 535  → 可走 cu12（samples 官方支持，推荐初学）
   └─ 驱动 ≥ 580  → 也可走 cu13（samples 非官方支持）
2. 确认没有装过另一个 CUDA 大版本的 CV-CUDA 包
   └─ pip list | grep cvcuda，如有先 pip uninstall
3. python3 -m venv venv_samples && source venv_samples/bin/activate
4. pip install cvcuda-cu12        # 或 cvcuda-cu13
5. python3 -c "import cvcuda; ..." 验证导入
```

#### 4.1.3 源码精读

**安装命令的唯一权威出处是 README 的 Installation 一节。** 它用一张两行的表给出命令：

[README.md:L55-L65](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L55-L65)
这段是 README 的 Installation 章节：说明 CV-CUDA 可通过预编译包（wheel/deb/tar）或源码安装，wheel 覆盖 Python 3.10–3.14 与 x86_64/aarch64 平台，并给出 `pip install cvcuda-cu12`（CUDA 12）与 `pip install cvcuda-cu13`（CUDA 13）两条命令。

**前置条件（操作系统、CUDA、驱动版本）写在安装文档的 Prerequisites 一节：**

[docs/sphinx/installation.rst:L24-L35](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/installation.rst#L24-L35)
这段列出安装前的硬性要求：Ubuntu ≥ 22.04、CUDA Toolkit ≥ 12.2、驱动 r525+（CUDA 12.x）或 r580+（CUDA 13.x），并给 WSL2 用户指了专门的设置文档链接。

**三种预编译包的对比表：**

[docs/sphinx/installation.rst:L37-L57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/installation.rst#L37-L57)
这张表区分三种包：Python Wheels（自带 C++/CUDA 库与绑定，推荐 Python 用户）、Debian Packages（系统级安装，拆成 lib/dev/python/tests 多个模块）、Tar Archives（可移植安装）。deb 与 tar 都挂在 GitHub Releases 的 assets 里。

**平台兼容矩阵（哪个包能装在你的机器上）：**

[README.md:L74-L88](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L74-L88)
这是完整的兼容性矩阵：按 `x86_64_cu12` / `x86_64_cu13` / `aarch64_cu12` / `aarch64_cu13` 四种构建分别列出支持的 CUDA 版本（≥12.2 / ≥13.0）、计算能力（≥SM7.5，即 Turing 及之后）、驱动版本、Python 3.10–3.14、编译器与发行版要求。脚注还说明：aarch64 的常规发布物是 SBSA 兼容版，Jetson 专用包在 GitHub Release 里单独命名。

**最容易被忽略的「不能共存」限制：**

[README.md:L90-L99](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L90-L99)
这是 Known limitations 列表，与本讲直接相关的三条：不支持原生 Windows（只支持 WSL2）；**CV-CUDA Samples 只官方支持 CUDA 12**；**同一时刻只能安装一个 CUDA 大版本的 CV-CUDA 包**（deb/tar/wheel 均如此），换版本前必须先卸载干净。

#### 4.1.4 代码实践：安装并验证导入

> 本实践需要一台有 NVIDIA GPU（计算能力 ≥ SM7.5）且驱动 ≥ r535 的 Linux 机器。以下命令的具体输出**待本地验证**。

1. **实践目标**：装好 `cvcuda-cu12` 并确认 Python 能导入、能在 GPU 上创建张量。

2. **操作步骤**：

   ```bash
   # (1) 确认驱动与 GPU 满足要求
   nvidia-smi

   # (2) 建虚拟环境并激活
   python3 -m venv venv_samples
   source venv_samples/bin/activate

   # (3) 安装 CV-CUDA（CUDA 12 路线；CUDA 13 用户改装 cvcuda-cu13）
   pip install cvcuda-cu12

   # (4) 验证导入 + 在 GPU 上创建一个张量
   python3 -c "
   import numpy as np
   import cvcuda
   t = cvcuda.Tensor((4, 320, 320, 3), np.uint8, layout='NHWC')
   print('shape :', t.shape)
   print('layout:', t.layout)
   print('dtype :', t.dtype)
   "
   ```

   其中 `cvcuda.Tensor(shape, dtype, layout=...)` 的构造写法与官方测试一致，可对照 [tests/cvcuda/python/test_tensor.py:L409](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_tensor.py#L409) 与 [tests/cvcuda/python/test_tensor.py:L502](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_tensor.py#L502) 中的真实用法。

3. **需要观察的现象**：第 (4) 步打印出 `shape : (4, 320, 320, 3)` 之类的结果，且过程中没有 `ImportError`、没有 CUDA driver 报错。

4. **预期结果**：若打印正常，说明 wheel 安装成功、Python 绑定可用、GPU 运行时已就绪。若报 `CUDA driver version is insufficient` 之类的错误，回到 4.1.2 的决策流程检查驱动版本；若报找不到 `cvcuda`，确认虚拟环境已激活。

5. 上述现象描述基于源码与文档推断，**具体输出待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：你的同事在 Docker 容器里先装了 `cvcuda-cu12`，后来又执行 `pip install cvcuda-cu13`，会遇到什么问题？正确做法是什么？

<details>
<summary>参考答案</summary>

根据 [README.md:L98](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L98)，同一时刻只能安装一个 CUDA 大版本的 CV-CUDA 包。两个包的模块名都叫 `cvcuda`，直接叠加安装会互相覆盖/冲突。正确做法是先 `pip uninstall cvcuda-cu12`（必要时连依赖的 `nvidia-*-cu12` 系列包一起清理），再安装 `cvcuda-cu13`。
</details>

**练习 2**：为什么本讲推荐初学者选 `cvcuda-cu12` 而不是 `cvcuda-cu13`？

<details>
<summary>参考答案</summary>

两个原因：其一，README 的 Known limitations 写明 CV-CUDA Samples 只官方支持 CUDA 12（[README.md:L97](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L97)），而本讲的实践全部围绕 samples 里的 hello_world；其二，cu13 包要求驱动 ≥ r580（[README.md:L87](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L87)），门槛更高。当然，若你的驱动只有 CUDA 13 环境，cu13 也能安装使用，只是 samples 属于非官方支持组合。
</details>

**练习 3**：deb 包和 wheel 包都包含 Python 绑定，它们的安装粒度有什么不同？

<details>
<summary>参考答案</summary>

按 [docs/sphinx/installation.rst:L37-L57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/installation.rst#L37-L57)：wheel 是一个自包含包（库 + 绑定一起装进 Python 环境）；deb 则拆成 `cvcuda-lib`/`cvcuda-dev`（C++ 库与头文件）、`cvcuda-python<py_ver>`（绑定，按 Python 版本区分）、`cvcuda-tests` 等多个系统包，由管理员按需组合安装（具体包名见 [docs/sphinx/installation.rst:L90-L124](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/installation.rst#L90-L124)）。
</details>

### 4.2 hello_world.py：六步全 GPU 流水线

#### 4.2.1 概念说明

`samples/applications/hello_world.py` 是官方设计的入门示例，它演示了一条最小但完整的 AI 预处理风格管线：

- **nvimgcodec**（NVIDIA 的图像编解码库）负责两端：把 JPEG/PNG **解码**成 GPU 上的像素，以及把 GPU 像素**编码**回 JPEG 写盘。
- **CV-CUDA** 负责中间的全部像素计算：`resize`（几何变换）、`stack`（批合并）、`gaussian`（滤波）。

这个分工正是 CV-CUDA 生态的典型用法：编解码、推理（TensorRT）、像素处理（CV-CUDA）各司其职，通过共享 GPU 显存协作（互操作细节在 u2-l4 与 u9-l3 展开）。

#### 4.2.2 核心流程

脚本整体是一条六步流水线，每步都被一个计时器包裹：

```text
读取参数并校验路径/格式
        │
        ▼
① Load    nvimgcodec.Decoder().read(文件) ──► GPU 上的 Image
          cvcuda.as_tensor(image, "HWC") ──► 零拷贝包装成 cvcuda.Tensor
        │
        ▼
② Resize  cvcuda.resize(tensor, (H, W, 3), interp=LINEAR)   （每张各缩放到统一尺寸）
        │
        ▼
③ Batch   cvcuda.stack(resized_tensors) ──► 合并为一个 NHWC 批张量
        │
        ▼
④ Gaussian cvcuda.gaussian(批张量, (k,k), (σ,σ), Border.CONSTANT)
        │
        ▼
⑤ Split   zero_copy_split(批张量) ──► 按偏移指针零拷贝切回单张 HWC 张量
        │
        ▼
⑥ Save    nvimgcodec.Encoder().write(文件, as_image(tensor.cuda()))
```

其中第 ④ 步高斯模糊的两个可调参数值得先建立直觉。二维高斯核的权重分布为：

\[ G(x, y) = \frac{1}{2\pi\sigma^2}\, e^{-\frac{x^2+y^2}{2\sigma^2}} \]

- **kernel 尺寸 \( k \)**：滤波窗口的边长（脚本里传 `(k, k)`，即方形窗口）。\( k \) 越大，参与加权平均的邻域像素越多。
- **sigma \( \sigma \)**：权重随距离衰减的速度。\( \sigma \) 越大，远处像素权重衰减越慢，图像越模糊。

所以「小 kernel + 小 sigma」≈ 几乎原图，「大 kernel + 大 sigma」≈ 强烈模糊——这正是本讲实践任务要观察的现象。

#### 4.2.3 源码精读

**脚本自己声明了它要做的事：**

[samples/applications/hello_world.py:L15-L27](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L15-L27)
文档字符串列出 5 个步骤（载入、缩放、批合并、高斯模糊、保存），并以 "All without leaving the GPU." 强调整条管线不离开 GPU——这句话就是本讲 4.3 节要逐行验证的命题。

[samples/applications/hello_world.py:L28-L53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L28-L53)
用法示例：默认不带参数即用 `samples/assets/images/tabby_tiger_cat.jpg` 并写出到缓存目录；也支持 `-i/-o` 多图、`--width/--height`，以及本讲实践要用的 `-k`（kernel）与 `-s`（sigma）自定义参数，例如 `python3 hello_world.py -i input.jpg -o output.jpg --width 512 --height 512 -k 7 -s 2.0`。

**计时器：脚本自带每步耗时打印：**

[samples/applications/hello_world.py:L77-L90](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L77-L90)
`timer` 是一个上下文管理器：进入时记 `perf_counter`，退出时打印该阶段名与毫秒级耗时。运行脚本时你会看到 Load images / Resize images / Batch images / Apply Gaussian blur / Split images / Write images to disk 六段输出——这是最朴素的性能观测手段（更专业的 NVTX 分析在 u7-l4）。

**参数与默认值：**

[samples/applications/hello_world.py:L98-L136](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L98-L136)
argparse 定义了 `--inputs/-i`（默认示例虎猫图）、`--outputs/-o`（默认缓存目录 `cat_hw.jpg`）、`--width/--height`（默认 224×224）、`--kernel/-k`（默认 5）与 `--sigma/-s`（默认 1.0）。注意 `-i/-o` 都是 `nargs="+"`，可以一次传多张图。

[samples/applications/hello_world.py:L143-L174](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L143-L174)
运行前的防御性校验：输入必须存在且后缀属于 `.jpg/.jpeg/.png`，输出目录必须已存在，且输入输出数量必须一致。这解释了实践中常见的报错（比如给了一个不存在的输出目录）。

**第 ① 步——解码与零拷贝包装（nvimgcodec → CV-CUDA 的交接点）：**

[samples/applications/hello_world.py:L182-L192](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L182-L192)
`nvimgcodec.Decoder()` 逐个 `read` 输入文件得到 `nvimgcodec.Image`（README 对应示例的注释原话是 "Decode image directly to GPU"，即解码结果直接落在 GPU），随后 `cvcuda.as_tensor(image, "HWC")` 把它零拷贝包装成 `cvcuda.Tensor`，并声明其布局为 HWC（高×宽×通道的交错存储）。没有出现任何 `cudaMemcpy`——两个库直接共享同一块显存。

**第 ②③ 步——缩放与批合并：**

[samples/applications/hello_world.py:L194-L205](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L194-L205)
对每张图调用 `cvcuda.resize(tensor, (height, width, 3), interp=cvcuda.Interp.LINEAR)`，用双线性插值统一到目标尺寸。注意目标 shape 是三元组 `(H, W, C)`——resize 作用在单张 HWC 张量上。

[samples/applications/hello_world.py:L207-L211](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L207-L211)
`cvcuda.stack(resized_tensors)` 把 N 个同形状的 HWC 张量合并成一个 NHWC 批张量。之所以必须先统一尺寸再 stack，是因为普通 Tensor 要求各维度固定——变长批（ImageBatchVarShape）要等到 u2-l3 才登场。

**第 ④ 步——高斯模糊，以及它在绑定层的真实签名：**

[samples/applications/hello_world.py:L213-L222](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L213-L222)
对整个批张量调用 `cvcuda.gaussian(batch_tensor, (kernel, kernel), (sigma, sigma), cvcuda.Border.CONSTANT)`：kernel 与 sigma 都允许横向/纵向不同，这里取相同值；`Border.CONSTANT` 表示越界像素按常量（黑边）处理。

这个调用在 pybind11 绑定层的定义可以在下面看到：

[python/mod_cvcuda/operators/OpGaussian.cpp:L60-L66](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpGaussian.cpp#L60-L66)
这是 `cvcuda.gaussian`（allocating 变体）的 C++ 实现入口：先用 `Tensor::Create(input.shape(), input.dtype())` 分配一个与输入同形状同 dtype 的新张量作为输出，再转交 `GaussianInto` 执行。也就是说，脚本里每调用一次 `cvcuda.gaussian(...)`，都隐式分配了一次输出显存（Python 侧有对象缓存兜底复用，详见 u3-l3 与 u4-l2）。

[python/mod_cvcuda/operators/OpGaussian.cpp:L35-L58](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpGaussian.cpp#L35-L58)
`GaussianInto` 展示了所有算子绑定的标准骨架：未显式传 stream 时取 `Stream::Current()`；用 `ResourceGuard` 对输入加读锁、输出加写锁；最后 `gaussian->submit(...)` 把 CUDA kernel 提交到流上。本讲只需看懂「调用即异步提交到当前流」这一层，锁与流的细节留给 u4-l1/u8-l2。

[python/mod_cvcuda/operators/OpGaussian.cpp:L106-L107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpGaussian.cpp#L106-L107)
`m.def("gaussian", ...)` 把上面的 C++ 函数导出为 Python 的 `cvcuda.gaussian`，参数名 `src / kernel_size / sigma / border / stream`——这与脚本里的实参顺序一一对应。

**第 ⑤⑥ 步——拆批与写盘：**

[samples/applications/hello_world.py:L224-L235](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L224-L235)
先用 `zero_copy_split` 把 NHWC 批张量零拷贝切成 N 个 HWC 张量（实现见 4.3.3），再对每张执行 `nvimgcodec.as_image(tensor.cuda())` 把 GPU 张量交回 nvimgcodec，由 `encoder.write(路径, ...)` 编码写盘。`tensor.cuda()` 返回一个带 `__cuda_array_interface__` 的缓冲视图，是 CV-CUDA 与其他 GPU 库交换显存的通用接口。

[samples/applications/hello_world.py:L237-L241](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L237-L241)
最后逐个断言输出文件存在并打印路径——脚本成功的标志就是这几行输出。

#### 4.2.4 代码实践：运行 hello_world 并对比两组模糊参数

> 需要 GPU 环境与 4.1.4 的安装完成。以下运行现象**待本地验证**。

1. **实践目标**：跑通全 GPU 流水线；用两组极端的 `(kernel, sigma)` 参数直观感受高斯模糊的行为。

2. **操作步骤**：

   官方快速上手路径（[docs/sphinx/getting_started.rst:L44-L68](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/getting_started.rst#L44-L68)）是：

   ```bash
   # (1) 在仓库根目录，生成/安装 hello_world 最小依赖（二选一）
   #     方式 A：按官方文档用 requirements 文件（.txt 由模板生成，见下方说明）
   bash generate_requirements.sh        # 从 *.template + versions.env 生成各 requirements.txt
   python3 -m venv venv_samples && source venv_samples/bin/activate
   pip install -r samples/requirements.samples.hello_world_cu12.txt

   #     方式 B：直接手动安装这 4 个包（与该 requirements 等价）
   # pip install cvcuda-cu12 nvidia-nvimgcodec-cu12 numpy cuda-python

   # (2) 运行示例（默认输入 samples/assets/images/tabby_tiger_cat.jpg）
   python3 samples/applications/hello_world.py

   # (3) 两组参数对比：轻微模糊 vs 强烈模糊
   python3 samples/applications/hello_world.py -o .cache/cat_hw_k3_s05.jpg -k 3 -s 0.5
   python3 samples/applications/hello_world.py -o .cache/cat_hw_k15_s50.jpg -k 15 -s 5.0
   ```

   关于方式 A 的一个仓库细节：getting_started 引用的 `samples/requirements.samples.hello_world_cu12.txt` **在干净的 clone 里并不存在**，仓库里只有它的模板源文件 [samples/requirements.samples.hello_world_cu12.template:L25-L29](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/requirements.samples.hello_world_cu12.template#L25-L29)，其中列出的正是 `cuda-python`、`cvcuda-cu12`、`nvidia-nvimgcodec-cu12`、`numpy` 这 4 个包。按照仓库规范，所有 requirements `.txt` 都由 `bash generate_requirements.sh` 从模板 + `versions.env` 生成——这也是方式 B 直接手动装这 4 个包的依据。

   如果你更喜欢「修改脚本」而不是敲参数：把 [samples/applications/hello_world.py:L127-L135](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L127-L135) 中 `--kernel` 的 `default=5` 与 `--sigma` 的 `default=1.0` 改成你的两组值，分两次运行即可（建议在副本上改，或改完还原，不要把改动留在源码里）。

3. **需要观察的现象**：
   - 终端依次打印 `Parse Arguments / Load images / Resize images / Batch images / Apply Gaussian blur / Split images / Write images to disk` 七段计时输出，最后打印 `Wrote image to ...`；
   - 仓库根下出现 `.cache/` 目录与输出图片（默认 `cat_hw.jpg`）；
   - 打开三张输出图对比：`k3_s05` 版本与仅缩放的结果接近、细节基本保留；`k15_s50` 版本明显糊成一团、只剩大轮廓。

4. **预期结果**：输出图都是 224×224（默认 `--width/--height`）；kernel 与 sigma 越大图像越模糊，符合 4.2.2 的高斯核直觉。若报 `Input file does not exist`，检查是否在仓库根目录运行（默认输入路径按脚本位置解析，脚本自己会定位到 `samples/assets/images/`，但你的 `-o` 路径的父目录必须已存在，见 [samples/applications/hello_world.py:L160-L167](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L160-L167) 的校验逻辑）。

5. 以上现象为基于源码的推断，**具体计时数值与图像效果待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把 `-i` 换成两张分辨率不同的图片（例如 `samples/assets/images/` 下还有 `Weimaraner.jpg` 与 `peoplenet.jpg`），管线在哪一步之前必须保证两张图尺寸一致？为什么？

<details>
<summary>参考答案</summary>

在 ② Resize 之后、③ `cvcuda.stack` 之前。`stack` 要把 N 个张量拼成同一个 NHWC 张量，而普通 Tensor 的每个维度必须固定，因此所有成员必须同形状——这正是脚本先用 resize 把每张图统一到 `(height, width, 3)` 的原因（[samples/applications/hello_world.py:L194-L211](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L194-L211)）。若想批内保留不同尺寸，需要 u2-l3 的 `ImageBatchVarShape`。
</details>

**练习 2**：脚本里 `cvcuda.gaussian(batch_tensor, (5, 5), (1.0, 1.0), cvcuda.Border.CONSTANT)` 的四个实参分别对应绑定层定义中的哪些形参？这个调用有没有显式传 stream？

<details>
<summary>参考答案</summary>

对照 [python/mod_cvcuda/operators/OpGaussian.cpp:L106-L107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpGaussian.cpp#L106-L107)：批张量 → `src`，`(5, 5)` → `kernel_size`（允许横纵不同），`(1.0, 1.0)` → `sigma`（同样允许横纵不同），`Border.CONSTANT` → `border`。脚本没有传 `stream`，此时绑定层内部取 `Stream::Current()`（[python/mod_cvcuda/operators/OpGaussian.cpp:L38-L41](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpGaussian.cpp#L38-L41)），即提交到当前默认流。
</details>

**练习 3**：不修改任何代码，如何把输出图变成 512×512、kernel=7、sigma=2.0？

<details>
<summary>参考答案</summary>

脚本文档字符串本身就给了答案（[samples/applications/hello_world.py:L39-L41](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L39-L41)）：

```bash
python3 samples/applications/hello_world.py -o .cache/out.jpg --width 512 --height 512 -k 7 -s 2.0
```
</details>

### 4.3 数据落点追踪：每一步数据在 CPU 还是 GPU

#### 4.3.1 概念说明

「All without leaving the GPU」不能只当口号——初学者必须能亲手指出每个变量住在哪。本模块把 hello_world 的变量逐个归类，并读懂两个支撑设施：

- `get_cache_dir()`：输出文件到底写到哪去了；
- `zero_copy_split()`：为什么「把批拆回单张」不需要任何像素拷贝。

另外还要回答一个安装期的疑问：hello_world 只 `import cvcuda` 和 `nvimgcodec`，为什么最小依赖清单里还有 `numpy` 和 `cuda-python`？答案藏在 `samples/common.py` 的 import 链里。

#### 4.3.2 核心流程

数据的「住所」随流水线推进的流转图（★ = 像素数据实际所在）：

```text
磁盘上的 JPEG 字节          CPU 文件系统读取
      │ nvimgcodec.Decoder().read()
      ▼
nvimgcodec.Image        ★ GPU 显存（解码直出 GPU）
      │ cvcuda.as_tensor(image, "HWC")        零拷贝，仅建视图
      ▼
cvcuda.Tensor (HWC)     ★ GPU 显存（同一块！）
      │ cvcuda.resize / cvcuda.stack / cvcuda.gaussian   全部在 GPU 上读写
      ▼
cvcuda.Tensor (NHWC)    ★ GPU 显存
      │ zero_copy_split                       零拷贝，仅算偏移指针
      ▼
list[cvcuda.Tensor]     ★ GPU 显存（同一块的 N 个视图）
      │ tensor.cuda() → __cuda_array_interface__ 视图
      ▼
nvimgcodec.Encoder().write()  从 GPU 缓冲读像素做编码，
      │                        压缩后的字节流经主机写入磁盘文件
      ▼
磁盘上的 JPEG 字节
```

要点：**像素矩阵自始至终只有一份，且一直在 GPU 显存里**；CPU 侧经手的只有压缩前/后的 JPEG 字节流和 Python 对象（句柄、形状元数据）。

#### 4.3.3 源码精读

**输出目录的确定——为什么结果在仓库根的 `.cache/` 下：**

[samples/common.py:L44-L55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L44-L55)
`get_cache_dir()` 取 `common.py` 所在目录（`samples/`）的上一级——即**仓库根目录**——拼接 `.cache` 并确保其存在。所以默认输出是 `<仓库根>/.cache/cat_hw.jpg`。getting_started 文档里写的 `cvcuda/.cache/cat_hw.jpg` 是把仓库 clone 成名为 `cvcuda` 的目录时的同一件事。

**零拷贝拆批——`zero_copy_split` 的偏移指针技巧：**

[samples/common.py:L290-L330](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L290-L330)
函数先取出批张量的 `__cuda_array_interface__`（GPU 缓冲的指针、形状与 **strides**），算出「批内每张图占多少字节」（`batch_stride_bytes`）：若接口报告了 strides 就用真实的批维 stride（CV-CUDA 会把行对齐到硬件边界，stride 通常大于按形状紧排的字节数），否则按 `H*W*C*itemsize` 紧排计算。这一步是正确切批的关键——用错 stride 图像会被「剪切」错位。

[samples/common.py:L331-L352](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L331-L352)
然后对批内第 i 张图，把设备指针加上 `i * batch_stride_bytes` 得到偏移指针，手工构造一个带 `__cuda_array_interface__` 的 `CudaBuffer`（保留 HWC 的 strides、并把 `obj` 指回原始缓冲以延长其生命周期），最后 `cvcuda.as_tensor(offset_buffer, layout="HWC")` 包装成新 Tensor。整个过程**没有搬运任何像素**——N 个「新」张量只是同一块显存的 N 个窗口。这与 PyTorch 的 tensor view 是同一思想。

**依赖链——为什么需要 numpy 和 cuda-python：**

[samples/common.py:L33-L41](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L33-L41)
`common.py` 顶层 `import cvcuda`、`import cuda.bindings.runtime as cudart`（来自 `cuda-python` 包）、`import numpy`、`from nvidia import nvimgcodec`。hello_world 通过 `from common import get_cache_dir, zero_copy_split` 触发整个模块加载，所以这 4 个包缺一不可——这正是 [samples/requirements.samples.hello_world_cu12.template:L25-L29](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/requirements.samples.hello_world_cu12.template#L25-L29) 最小依赖清单的由来。读 import 链而不是只看脚本头部，是排查 `ModuleNotFoundError` 的基本功。

**官方文档对输出位置的说明：**

[docs/sphinx/getting_started.rst:L70-L83](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/getting_started.rst#L70-L83)
快速上手一节的「See Results」告诉你去 `.cache` 目录找输出图，并附注：hello_world 的 requirements 只有 4 个包，是为了快速体验；要跑其他 samples（operators/applications/interoperability）请改用 `samples/install_samples_dependencies.sh` 安装完整依赖。

#### 4.3.4 代码实践：给流水线每一步标注 CPU / GPU

1. **实践目标**：把 4.3.2 的流转图变成你自己验证过的结论——在脚本里插桩，观察张量的接口信息，亲手证明「像素未离开 GPU」。

2. **操作步骤**：

   复制一份脚本（示例代码，不要改仓库源文件）：

   ```bash
   cp samples/applications/hello_world.py /tmp/hello_world_probe.py
   ```

   然后在 `/tmp/hello_world_probe.py` 中每个阶段之后插入一行探针（示例代码）：

   ```python
   # ① 解码+包装后
   print("after as_tensor :", tensors[0].cuda().__cuda_array_interface__["typestr"])
   # ② 缩放后
   print("after resize    :", resized_tensors[0].shape, resized_tensors[0].layout)
   # ③ 批合并后
   print("after stack     :", batch_tensor.shape, batch_tensor.layout)
   # ⑤ 拆批后
   split = zero_copy_split(blurred_tensor_batch)
   iface = split[0].cuda().__cuda_array_interface__
   print("after split     : ptr =", hex(iface["data"][0]))
   batch_iface = blurred_tensor_batch.cuda().__cuda_array_interface__
   print("batch ptr       : ptr =", hex(batch_iface["data"][0]))
   ```

   运行 `python3 /tmp/hello_world_probe.py`（需在仓库根且 `samples` 在路径上，脚本第 [L71-L74](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L71-L74) 已自动把 `samples/` 加入 `sys.path`）。

3. **需要观察的现象**：
   - `typestr` 形如 `|u1`（uint8）；
   - `after resize` 打出 `(224, 224, 3) HWC`，`after stack` 打出 `(N, 224, 224, 3) NHWC`；
   - `after split` 的指针与批张量指针**数值相同或仅差一个小于批总字节的偏移**（差值 = `i * batch_stride_bytes`）。

4. **预期结果**：指针相同/相近直接证明拆批是同一块显存的视图；各阶段 shape/layout 的变化与 4.3.2 流程图一致。若把探针里 `tensor.cuda()` 换成 `.cpu()`（如果存在此类 API）或试图直接 `np.array(tensor)`，预期会失败或触发一次显式拷贝——这反过来说明默认路径不做隐式主机拷贝。

5. 探针输出**待本地验证**；接口字段含义可对照 `samples/common.py` 里现成的 `debug_helper`（[samples/common.py:L879-L901](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L879-L901)），它就是官方写好的一次性打印 shape/dtype/layout/strides 的调试函数。

#### 4.3.5 小练习与答案

**练习 1**：hello_world.py 的哪几行代码是「nvimgcodec 世界」与「CV-CUDA 世界」之间的边界？各用了什么接口？

<details>
<summary>参考答案</summary>

两个方向各一处：入向是 `cvcuda.as_tensor(image, "HWC")`（[samples/applications/hello_world.py:L189-L191](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L189-L191)），把 nvimgcodec 的 GPU 图像零拷贝纳管为 CV-CUDA 张量；出向是 `nvimgcodec.as_image(tensor.cuda())`（[samples/applications/hello_world.py:L233-L235](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L233-L235)），其中 `tensor.cuda()` 暴露 `__cuda_array_interface__` 给 nvimgcodec 消费。两个方向都不搬运像素。
</details>

**练习 2**：`zero_copy_split` 为什么要优先使用接口报告的 `strides[0]`，而不是直接用 `H*W*C*itemsize`？

<details>
<summary>参考答案</summary>

因为 CV-CUDA 分配张量时会把行对齐到硬件边界，实际行距（pitch）可能大于按形状紧排的宽度，批维 stride 也因此变大（`common.py` 的 `_tensor_copy_geometry` 文档注释明确说明了这一点，见 [samples/common.py:L148-L167](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L148-L167)）。若按紧排字节数切批，第 2 张图起就会从错误的偏移开始，图像被剪切错位。`strides` 为 `None` 时才说明内存完全连续，可以按紧排计算。
</details>

**练习 3**：运行 hello_world 报 `ModuleNotFoundError: No module named 'cuda'`，但你明明只记得脚本 import 了 cvcuda 和 nvimgcodec。原因是什么？怎么修？

<details>
<summary>参考答案</summary>

hello_world 执行了 `from common import get_cache_dir, zero_copy_split`，而 `samples/common.py` 顶层 `import cuda.bindings.runtime`（即 `cuda-python` 包，见 [samples/common.py:L39](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L39)）。修法：`pip install cuda-python`（或直接按 `samples/requirements.samples.hello_world_cu12.txt` 装齐 4 个包）。
</details>

## 5. 综合实践

**任务：一条属于你自己的全 GPU 迷你管线。**

在完成前面三个模块实践的基础上，做一次串联（需要 GPU 环境，结果**待本地验证**）：

1. 准备两张你自己的图片（或用 `samples/assets/images/` 下的 `Weimaraner.jpg` 与 `peoplenet.jpg`），以多输入方式运行：

   ```bash
   python3 samples/applications/hello_world.py \
       -i samples/assets/images/Weimaraner.jpg samples/assets/images/peoplenet.jpg \
       -o .cache/out_dog.jpg .cache/out_people.jpg
   ```

2. 记录终端里六个阶段的计时输出，做成一张表（阶段 / 耗时 ms）。
3. 保持输入不变，分别用 `(k=3, s=0.5)` 与 `(k=15, s=5.0)` 再跑两轮，对比三组输出图的模糊程度，并用一句话解释原因（提示：回到 4.2.2 的高斯核公式——\( k \) 控制窗口大小，\( \sigma \) 控制权重衰减速度）。
4. 填写下面这张「数据落点表」（答案已在 4.3.2 给出，此处作为自查）：

   | 流水线阶段 | 关键函数调用 | 像素数据位置 |
   |---|---|---|
   | 解码 | `decoder.read()` | ？ |
   | 包装 | `cvcuda.as_tensor` | ？ |
   | 缩放/批合并/模糊 | `resize` / `stack` / `gaussian` | ？ |
   | 拆批 | `zero_copy_split` | ？ |
   | 编码写盘 | `encoder.write` | ？（提示：像素与压缩字节流要分开说） |

5. 进阶（可选）：仿照 `zero_copy_split` 的思路，写一个 `zero_copy_crop(tensor, top, left, h, w)`（示例代码，仅作思路练习）：对一张 HWC 张量，用 `top * stride_h + left * stride_w` 计算偏移指针，构造带 strides 的 `__cuda_array_interface__` 视图再 `cvcuda.as_tensor` 包装。不需要跑通 CUDA 编译，写出 Python 代码并推演偏移量即可。

## 6. 本讲小结

- CV-CUDA 的安装以 `pip install cvcuda-cu12`（或 `cvcuda-cu13`）为最短路径；cu12/cu13 两个包**不可共存**，切换前必须先卸载，且 samples 目前只官方支持 CUDA 12。
- 预编译包有 wheel / deb / tar 三种形态；wheel 自带 C++/CUDA 库与 Python 绑定，deb/tar 面向系统级与可移植安装，均在 GitHub Releases 分发。
- `hello_world.py` 用六步构成全 GPU 流水线：nvimgcodec 解码 → `as_tensor` 零拷贝纳管 → `resize` → `stack` 批合并 → `gaussian` 模糊 → `zero_copy_split` 拆批 → nvimgcodec 编码写盘；每步的耗时由内置 `timer` 打印。
- `cvcuda.gaussian` 是 allocating 变体：绑定层先用 `Tensor::Create` 分配输出再提交到当前流；kernel 尺寸决定滤波窗口，sigma 决定模糊强度。
- 像素矩阵全程只有一份且一直在 GPU 显存；`zero_copy_split` 靠偏移指针 + `__cuda_array_interface__` 实现免拷贝的批切分；stride 必须用接口报告的真实值，否则图像会错位。
- getting_started 引用的 `requirements.samples.hello_world_*.txt` 由 `bash generate_requirements.sh` 从模板生成，干净 clone 里需先生成，或直接手动安装 `cvcuda-cu12 / nvidia-nvimgcodec-cu12 / numpy / cuda-python` 4 个包。

## 7. 下一步学习建议

- **下一讲（u1-l3）**：如果你想脱离预编译包、亲手编译整个库（`build.sh`、CMake preset、Docker devel 镜像），进入源码构建路线。
- **再下一讲（u1-l4）**：学习仓库代码地图，掌握「凭文件名定位任意算子的全部相关文件」的检索套路，为进入源码世界做准备。
- **并行阅读建议**：动手跑 `samples/operators/` 下的其他单算子示例（如 `resize.py`），体会「README → getting_started → 单算子示例」的学习路径；运行前记得用 `samples/install_samples_dependencies.sh` 安装完整依赖（hello_world 的 4 包清单不够）。
- **回看源码**：把本讲 4.2.3 里 `OpGaussian.cpp` 的 `ResourceGuard` 与 `Stream::Current()` 圈出来——它们是 u4-l1（Stream 执行模型）与 u8-l2（Python 绑定解剖）的伏笔。
