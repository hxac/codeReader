# CV-CUDA 是什么：项目定位与架构总览

> 本讲是《CV-CUDA 项目学习手册》的第一讲，属于入门单元 u1。读完本讲，你应当能回答三个问题：这个仓库是干什么的、里面每个目录放什么、一次 Python 调用如何穿过整个仓库到达 CUDA kernel。

## 1. 本讲目标

- 说出 CV-CUDA 的定位：它是 NVIDIA 开源的 **GPU 加速计算机视觉算子库**，面向 AI 数据预处理/推理管线的高吞吐、低延迟场景。
- 说出它支持的平台（x86_64 / aarch64 / WSL2）、CUDA 版本（CUDA 12.2+ 与 CUDA 13.x）、Python 版本（3.10–3.14）等兼容性要点。
- 描述 `src/`、`python/`、`tests/`、`bench/`、`samples/`、`docs/` 等顶层目录的职责。
- 对照 README 中的架构图，理解 **nvcv 类型层**（张量等数据容器）与 **cvcuda 算子层**（60+ 个视觉算子）以及 **python 绑定层** 的分层关系。
- 完成一个代码实践：在仓库中亲手找出一次 `cvcuda.resize` 调用经过的每一层真实文件。

## 2. 前置知识

本讲面向零基础读者，但以下概念会帮你读得更顺：

| 概念 | 通俗解释 |
|------|----------|
| GPU / CUDA | GPU 是显卡上的并行处理器；CUDA 是 NVIDIA 提供的 GPU 编程平台，「CUDA kernel」就是运行在 GPU 上的函数 |
| 算子（operator） | 图像处理中的一个原子操作，例如缩放（resize）、翻转（flip）、色彩转换（cvtColor） |
| 张量（Tensor） | 多维数组的统称。一张 RGB 图在内存里就是一个 `H×W×3` 的三维张量 |
| pybind11 | 一个 C++ 库，用来把 C++ 函数/类包装成 Python 可以 import 的模块 |
| C ABI / 句柄（handle） | 纯 C 风格的函数接口 + 不透明的指针。「句柄」就是一个代表 C++ 对象的指针，C 代码只传指针不碰内部结构 |
| 流（cudaStream_t） | GPU 上的任务队列，算子被「提交」到流上异步执行，同一流内按顺序运行 |

不需要会写 CUDA 代码——本讲只要求你能看懂「调用穿过了哪几个目录、哪几个文件」。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
|-----------|------|
| [README.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md) | 项目门面：定位说明、安装方式、兼容性矩阵、已知限制 |
| [AGENTS.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md) | 仓库官方「地图 + 规范」：目录职责表、权威文档索引、仓库不变量 |
| [docs/sphinx/content/cvcuda_arch.svg](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/content/cvcuda_arch.svg) | README 中嵌入的官方架构图（内嵌位图，需在浏览器/GitHub 页面查看） |
| `src/nvcv/` | **nvcv 类型层**：Tensor、ImageBatch 等数据容器的 C++/C 定义 |
| `src/cvcuda/` | **cvcuda 算子层**：每个算子的 C API（`.h`）、C++ 类（`.hpp`）、私有实现（`priv/`）与 CUDA kernel（`priv/*.cu`、`priv/legacy/*.cu`） |
| `python/mod_cvcuda/` | **Python 绑定层**：pybind11 封装，把 C++ 算子暴露成 `cvcuda.resize` 这样的 Python 函数 |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**① 项目定位与目标场景 → ② 平台与版本兼容 → ③ 仓库目录地图 → ④ 分层架构与一次 resize 调用的旅程**。

---

### 4.1 CV-CUDA 的定位与目标场景

#### 4.1.1 概念说明

CV-CUDA 解决的核心问题是：**AI 管线中的图像预处理和后处理往往跑在 CPU 上，成为整条管线的瓶颈**。

典型场景：一个视觉搜索 / 推荐服务要同时对成百上千张图做「解码 → 缩放 → 色彩转换 → 归一化 → 送入神经网络」。如果这些步骤用 CPU 串行完成，GPU 推理再快也喂不饱。CV-CUDA 把这几十种视觉算子全部搬到 GPU 上，并且：

- 提供 **C/C++ 和 Python 两套 API**，可与现有图像/AI 框架无缝协作；
- 支持**批处理**（一次处理一批图，包括每张尺寸都不同的「变长批」）；
- 大量算子提供**融合版本**（把多步变换合并进一个 kernel，减少显存读写）。

它起源于 NVIDIA 与字节跳动的合作（见 README 的 Acknowledgements 部分）。

#### 4.1.2 核心流程

CV-CUDA 在一条典型 AI 管线中的位置：

```
图片/视频文件
   │  解码（配套库 nvimgcodec，可直接解码到 GPU）
   ▼
cvcuda.Tensor / cvcuda.ImageBatchVarShape   ←── nvcv 类型层提供容器
   │
   │  cvcuda.resize / cvtcolor / normalize / ...   ←── cvcuda 算子层（GPU 上执行）
   ▼
规整好的 GPU 张量
   │
   ▼
神经网络推理（TensorRT / PyTorch ...，通过 DLPack 零拷贝衔接）
```

README 首页给出的一段 5 行代码就是这条链路的缩影：

```python
import cvcuda
from nvidia import nvimgcodec

# Decode image directly to GPU
decoder = nvimgcodec.Decoder()
image = decoder.read("input.jpg")

# Convert to CV-CUDA tensor and process
cvcuda_tensor = cvcuda.as_tensor(image, "HWC")
resized = cvcuda.resize(cvcuda_tensor, (224, 224, 3), cvcuda.Interp.LINEAR)
```

#### 4.1.3 源码精读

**① 定位一句话**：README 用一句话总结了整个项目——「为速度和可扩展性设计的 GPU 加速计算机视觉算法开源库，为 NVIDIA 云、桌面和边缘平台上的 AI 管线提供高吞吐、低延迟的图像/视频处理，并与 C/C++ 和 Python 的图像与 AI 框架无缝协作」：

- [README.md:L29-L31](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L29-L31)：项目定位原文与在线文档入口。

**② 上手示例**：上面引用的 5 行代码来自 README 的 "CV-CUDA in Action" 小节，它演示了「GPU 解码 → 包装为 cvcuda.Tensor → GPU 缩放」的最短路径：

- [README.md:L38-L53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L38-L53)：完整示例代码（本讲引用的代码即出自此处）。

**③ 架构图**：README 在正文里嵌入了官方绘制的管线图和架构图两张 SVG：

- [README.md:L33-L35](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L33-L35)：两张图的引用位置；
- [docs/sphinx/content/cvcuda_arch.svg](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/content/cvcuda_arch.svg)：架构图本体。注意该 SVG 内部是内嵌位图，无法当作文本阅读，请在 GitHub 页面或浏览器中查看图像内容。图中的分层关系可以与本讲 4.4 节从源码验证出的分层对照着看。

**④ 安装方式**：预编译 Python wheel 按 CUDA 大版本分成两个包名：

- [README.md:L60-L63](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L60-L63)：`pip install cvcuda-cu12` / `pip install cvcuda-cu13` 安装表。

#### 4.1.4 代码实践

**实践目标**：确认本机（或你计划使用的机器）属于 CV-CUDA 的目标用户场景。

**操作步骤**：

1. 查看 `nvidia-smi` 输出，记下驱动版本与 CUDA 版本。
2. 对照 README 的兼容性表（见 4.2 节）判断：你的 GPU 架构（Turing/Ampere/Ada/Hopper/Blackwell？）、驱动版本是否达标。
3. 如果有 GPU，执行 `pip install cvcuda-cu12`（或 cu13），然后运行：

```python
# 示例代码：验证安装（如无可用的 GPU 环境则待本地验证）
import cvcuda
print(cvcuda.__version__)
print(cvcuda.Tensor)  # 能打印出类型说明即安装成功
```

**需要观察的现象**：`__version__` 输出版本号（当前仓库为 v0.17.0 开发线）；`cvcuda.Tensor` 是一个可访问的类型对象。

**预期结果**：导入无报错。若报 `ImportError`，多半是 CUDA 运行时与 wheel 的 cu12/cu13 版本不匹配。本实践依赖 GPU 与网络环境，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：CV-CUDA 与 OpenCV 的核心差异是什么？
**答案**：OpenCV 主要面向 CPU（GPU 支持有限且非主线），CV-CUDA 从设计之初就全部运行在 GPU 上，并针对 AI 预处理/后处理管线做了批处理、变长批（每张图尺寸不同）、融合算子、多流并发等优化。

**练习 2**：为什么 README 示例里解码要用 nvimgcodec 而不是 cvcuda 自己解码？
**答案**：CV-CUDA 的范围是「视觉算子」，编解码属于另一个专用库 nvimgcodec 的职责。它可以直接把图解码到 GPU 显存，再经 `cvcuda.as_tensor` 零拷贝包装，两者配合形成全 GPU 链路。这体现了 CV-CUDA 专注做好一层、通过标准接口（DLPack）与生态协作的设计。

---

### 4.2 支持平台与版本兼容矩阵

#### 4.2.1 概念说明

开源库第一件要搞清的事就是「它能不能跑在我的机器上」。CV-CUDA 的兼容性由五个维度共同决定：

1. **平台**：linux-64（x86_64）、win-64 WSL2（不支持原生 Windows）、aarch64（服务器 SBSA 与 Jetson 嵌入式）；
2. **CUDA 版本**：CUDA ≥ 12.2 或 CUDA ≥ 13.0，两套 wheel 包名分别为 `cvcuda-cu12` / `cvcuda-cu13`；
3. **GPU 架构（Compute Capability）**：≥ SM7.5，即 Turing 及之后（Volta 已在 v0.16 起放弃支持）；
4. **驱动**：CUDA 12 包要求 ≥ r525（samples 要求 ≥ r535），CUDA 13 包要求 ≥ r580；
5. **Python**：3.10 – 3.14。

#### 4.2.2 核心流程

判断「我的机器能否运行」的决策链：

```
拿到机器信息（nvidia-smi: 驱动版本 / CUDA 版本 / GPU 型号）
   │
   ├─ GPU 是 Turing(7.5)/Ampere(8.x)/Ada(8.9)/Hopper(9.0)/Blackwell 吗？──否→ 不支持
   ├─ CUDA 12.2+ 还是 13.x？→ 决定装 cvcuda-cu12 还是 cvcuda-cu13
   ├─ 驱动 ≥ r525（cu12）/ ≥ r580（cu13）吗？
   └─ Python 在 3.10–3.14 之间吗？
   ▼
全部满足 → pip install；任一不满足 → 先升级对应组件
```

另一个容易踩的坑：**同一时刻只能安装一个 CUDA 大版本的 CV-CUDA 包**，切换版本前要先卸载干净。

#### 4.2.3 源码精读

**① 完整兼容性矩阵**：README 用一张大表列出了 5 种构建（x86_64_cu12、x86_64_cu13、aarch64_cu12 服务器版、aarch64_cu12 Jetson 版、aarch64_cu13）各自的平台、CUDA 版本、算力、硬件架构、驱动、Python 与编译器要求：

- [README.md:L74-L82](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L74-L82)：兼容性矩阵（Compatibility 一节）。

**② 已知限制**：紧跟矩阵之后的 Known limitations 列出了 8 条边界条件，包括不支持原生 Windows（仅 WSL2）、v0.16 起放弃 CUDA 11/SM7/Ubuntu 20.04/Python 3.8、Jetson Orin 源码构建需 `-DCVCUDA_AARCH64_JETSON=ON`、同一时刻只能装一个 CUDA 版本的包等：

- [README.md:L90-L99](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L90-L99)：已知限制清单。

**③ 徽章速览**：README 顶部的徽章（Platform / CUDA / GCC / Python / CMake）是矩阵的浓缩版，例如 CUDA 徽章标注 `v12.2+ | v13.x`：

- [README.md:L22-L27](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L22-L27)：平台、CUDA、GCC、Python、CMake 版本徽章。

#### 4.2.4 代码实践

**实践目标**：把兼容性矩阵变成对自己机器的明确结论。

**操作步骤**：

1. 运行 `nvidia-smi`，记录：Driver Version、CUDA Version、GPU 型号。
2. 查询该 GPU 型号的 Compute Capability（NVIDIA 官网可查），确认 ≥ 7.5。
3. 运行 `python3 --version`，确认在 3.10–3.14 区间。
4. 按下表逐项打勾（示例表格，自己填）：

| 检查项 | 要求 | 我的机器 | 结论 |
|--------|------|----------|------|
| GPU 算力 | ≥ SM7.5 | （填写） | |
| CUDA | 12.2+ 或 13.x | （填写） | |
| 驱动 | ≥r525（cu12）/ ≥r580（cu13） | （填写） | |
| Python | 3.10–3.14 | （填写） | |

**需要观察的现象 / 预期结果**：四项全部达标则直接 `pip install`；否则先记录哪一项不满足、需要升级什么。这是纯阅读 + 检查型实践，不需要 GPU 也能完成前三步的资料收集。

#### 4.2.5 小练习与答案

**练习 1**：一台 x86_64 机器装了 r530 驱动，想跑 samples 里的示例，行吗？
**答案**：装 `cvcuda-cu12` 库本身可以（要求 ≥ r525），但 README 脚注明确说明 samples 要求驱动 ≥ r535，所以跑示例需要先升级驱动。

**练习 2**：为什么 `pip install cvcuda-cu12` 和 `pip install cvcuda-cu13` 不能共存？
**答案**：README 已知限制中写明：同一时刻只能安装一个 CUDA 版本的 CV-CUDA 包（deb、tar、wheel 皆然），切换前需卸载旧版本，否则会出现符号/运行时冲突。

---

### 4.3 仓库目录地图

#### 4.3.1 概念说明

CV-CUDA 是一个大型 C++/CUDA/Python 混合仓库，顶层有 10+ 个目录。官方在 `AGENTS.md`（本仓库给 AI 代理与贡献者的规范文件，`CLAUDE.md` 是它的符号链接）里维护了一张权威目录表。理解这张表后，你在仓库里就不会迷路。

#### 4.3.2 核心流程

按「使用者 → 集成者 → 贡献者」的顺序，各目录进入视野的先后不同：

```
使用者视角：   README.md → docs/ → samples/
集成者视角：   + python/（绑定与打包） + src/（C/C++ API）
贡献者视角：   + tests/ + bench/ + tools/ + docker/ + ci/ + lint/
```

#### 4.3.3 源码精读

**① 官方目录职责表**（AGENTS.md 的 Repository map 一节）：

- [AGENTS.md:L23-L35](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L23-L35)：九个顶层目录的职责定义。

逐条翻译并补充本讲验证过的实际内容：

| 目录 | 官方定义 | 本讲补充（实际观察到的内容） |
|------|----------|------------------------------|
| `src/` | C++ 核心库、C API、私有算子实现与 nvcv 类型 | 下分 `src/cvcuda`（算子层，含 67 个 `Op*.cpp`）与 `src/nvcv`（类型层，含 Tensor/Array/ImageBatch 等实现） |
| `python/` | 基于 pybind11 的 Python 绑定与 wheel 打包 | 核心在 `python/mod_cvcuda/`，含 `Main.cpp` 与 `operators/` 下每个算子的绑定文件 |
| `tests/` | C++ googletest 与 Python pytest 测试套件 | 含 `tests/cvcuda/`、`tests/nvcv_types/`、`tests/common/` |
| `bench/` | C++ 与 Python nvbench 基准，共享配置在 `bench/config/` | 含 `bench/cpp/`、`bench/python/`、`compare_to_baseline.py` |
| `samples/` | 示例应用与互操作示例 | 含 `applications/`（hello_world 等 4 个端到端应用）、`operators/`、`datatypes/`、`interoperability/`、`object_cache/` |
| `docs/` | Sphinx 与 Doxygen 文档源 | `docs/sphinx/` 下的 rst 文档与架构图 |
| `docker/` | 多架构构建与开发 Docker 镜像 | — |
| `ci/` | CI 工具与流水线配置 | — |
| `lint/` | pre-commit 钩子与仓库检查 | — |

**② 权威文档索引**：AGENTS.md 还给出了「哪类问题查哪个文档」的索引表（安装看 `docs/sphinx/installation.rst`、测试看 `tests/README.md`、基准看 `bench/README.md`、示例看 `samples/README.md`、新增算子看 `.agents/guidance/MAKE_OP_GUIDELINES.md`）：

- [AGENTS.md:L37-L52](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L37-L52)：权威文档索引。

**③ 算子层的文件规模**（本讲实际统计）：`src/cvcuda/` 顶层就有 67 个 `Op*.cpp` 文件——每个文件是一个算子的 C API 实现入口；`src/cvcuda/include/cvcuda/` 下每个算子成对提供 `.h`（C 接口）与 `.hpp`（C++ 类）；`src/cvcuda/priv/` 是私有实现；`src/cvcuda/priv/legacy/` 下有 78 个文件，主要是历史遗留的 `.cu` kernel。这就是「60+ 算子」在文件系统里的直观样子。

#### 4.3.4 代码实践

**实践目标**：不看本讲，独立说出每个顶层目录的职责。

**操作步骤**：

1. 在仓库根目录执行 `ls`，对照 AGENTS.md 的表逐个说出职责。
2. 进入 `samples/` 看四个子目录（`applications`、`operators`、`datatypes`、`interoperability`）各自的示例文件名。
3. 进入 `src/cvcuda/include/cvcuda/`，随机挑 3 个算子（如 `OpFlip.h`、`OpResize.h`、`OpCvtColor.h`），确认它们都成对存在 `.h`/`.hpp`。

**需要观察的现象**：`include/cvcuda/` 下文件总是成对出现；`priv/` 下的文件名与 `include/cvcuda/` 一一呼应。

**预期结果**：形成一张自己画的目录职责草图。纯文件浏览，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：想给某个算子的 Python 用法找例子，最先去哪个目录？
**答案**：`samples/operators/`（每个算子一个示例脚本）；若是端到端管线示例则在 `samples/applications/`。依据是 AGENTS.md 中 `samples/` 的定义与实际目录内容。

**练习 2**：`src/cvcuda/priv/legacy/` 这个名字里的 legacy 是什么含义？
**答案**：它是历史上第一批 CUDA kernel 的存放地（命名空间为 `nvcv::legacy::cuda_op`，使用 `DataShape` 等旧抽象），共 78 个文件；新写的算子则直接把 kernel 放在 `src/cvcuda/priv/Op*.cu` 中。第 u5-l3 讲会专门对比这两种形态。

---

### 4.4 分层架构：一次 `cvcuda.resize` 调用的旅程

#### 4.4.1 概念说明

这是本讲最重要的模块。CV-CUDA 的代码分成职责分明的几层：

```
┌───────────────────────────────────────────────────┐
│  Python 层   python/mod_cvcuda/                    │
│  （pybind11 绑定：cvcuda.resize、Tensor、Stream…）  │
├───────────────────────────────────────────────────┤
│  C API 层    src/cvcuda/Op*.cpp + include/cvcuda/  │
│  （cvcudaResizeCreate/Submit 纯 C 函数，ABI 稳定） │
├───────────────────────────────────────────────────┤
│  C++ 私有实现 src/cvcuda/priv/Op*.cpp              │
│  （cvcuda::priv::Resize，参数校验、数据导出）      │
├───────────────────────────────────────────────────┤
│  CUDA kernel  src/cvcuda/priv/Op*.cu、priv/legacy/ │
│  （__global__ 函数 + <<<grid,block,stream>>> 启动）│
├───────────────────────────────────────────────────┤
│  nvcv 类型层  src/nvcv/                            │
│  （Tensor/ImageBatch 等容器，被上面所有层使用）    │
└───────────────────────────────────────────────────┘
```

关键认知有两点：

1. **nvcv 是「数据」层，cvcuda 是「操作」层**。`nvcv::Tensor` 描述数据长什么样（形状、布局、显存位置），`cvcuda::Resize` 等算子消费这些数据。两层通过头文件解耦。
2. **Python 只是薄封装**。`cvcuda.resize(...)` 背后是真实的 C++ 对象与 CUDA kernel 调用，性能与 C++ 一致。

#### 4.4.2 核心流程

一次 `cvcuda.resize(tensor, (224,224,3), cvcuda.Interp.LINEAR)` 的完整旅程（文件均为仓库真实路径，行号见 4.4.3）：

```
Python: cvcuda.resize(src, shape, interp)
   │
   ① python/mod_cvcuda/operators/OpResize.cpp
   │     Resize()  →  Tensor::Create() 分配输出
   │     ResizeInto()  →  CreateOperator<cvcuda::Resize>() 创建 C++ 算子
   │                     ResourceGuard 加读/写锁后调用 resize->submit(...)
   │                     （submit 内部最终走到 operator()）
   ▼
   ② src/cvcuda/OpResize.cpp（C API 边界）
   │     cvcudaResizeSubmit()：把 NVCVTensorHandle 包回 C++ 对象，
   │     经 ProtectCall 调 priv::Resize 的 operator()
   │     （注意：Python 绑定直接持有 C++ 对象，平时绕过 C API；
   │       C API 是给纯 C 使用者准备的稳定 ABI 边界）
   ▼
   ③ src/cvcuda/priv/OpResize.cpp
   │     Resize::operator()：exportData<TensorDataStridedCuda>()
   │     把抽象 Tensor 导出为带 stride 的 GPU 数据视图，校验失败抛异常
   ▼
   ④ src/cvcuda/priv/OpResize.cu
   │     RunResize()：按插值类型/放大缩小场景分派到具体 kernel，
         例如 LinearResize<<<blocks,threads,0,stream>>>(...) 启动 GPU kernel
```

变长批（`ImageBatchVarShape` 输入）路径的 kernel 则在 `src/cvcuda/priv/legacy/resize_var_shape.cu`。

#### 4.4.3 源码精读

以下逐层引用真实代码。

**① Python 绑定层**——`Resize` 先分配输出张量再委托给 `ResizeInto`；`ResizeInto` 里创建 C++ 算子对象、用 `ResourceGuard` 声明输入读锁/输出写锁，最后在 guard 内提交算子：

- [python/mod_cvcuda/operators/OpResize.cpp:L42-L67](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResize.cpp#L42-L67)：`ResizeInto` 与 `Resize` 两个函数（上面流程 ① 的代码出处）。
- [python/mod_cvcuda/operators/OpResize.cpp:L109-L111](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResize.cpp#L109-L111)：`m.def("resize", ...)` 把 C++ 函数注册为 Python 的 `cvcuda.resize`，并用 `NvtxTrace` 包一层性能打点。

这些导出函数在模块初始化时被统一调用：

- [python/mod_cvcuda/Main.cpp:L62](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/Main.cpp#L62)：`PYBIND11_MODULE(_cvcuda, m)` 模块入口；
- [python/mod_cvcuda/Main.cpp:L182](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/Main.cpp#L182)：`ExportOpResize(m)`——把 resize 纳入 Python 模块的注册点（同文件还注册了其余 60+ 个 `ExportOp*`）。

**② C API 层**——`cvcudaResizeSubmit` 是给纯 C 使用者的稳定接口：接收不透明句柄，包装回 C++ 对象后调用私有实现；`ProtectCall` 负责把 C++ 异常翻译成 `NVCVStatus` 错误码：

- [src/cvcuda/OpResize.cpp:L45-L57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpResize.cpp#L45-L57)：`cvcudaResizeSubmit` 实现（流程 ② 的代码出处）；
- [src/cvcuda/OpResize.cpp:L30-L43](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpResize.cpp#L30-L43)：`cvcudaResizeCreate` 创建算子句柄。

C 头文件里还写有每个算子的「支持矩阵契约」，例如 Resize 支持的布局与 dtype：

- [src/cvcuda/include/cvcuda/OpResize.h:L57-L69](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpResize.h#L57-L69)：Limitations 契约：布局 `[kNHWC, kHWC, kNCHW, kCHW]`、通道 `[1,3,4]`，以及一张 dtype 支持表。

**③ C++ 私有实现层**——`cvcuda::priv::Resize::operator()` 先把 `nvcv::Tensor` 导出为 `TensorDataStridedCuda`（可在 GPU 上按 stride 访问的视图），失败即抛异常，然后调用 kernel 分派函数 `RunResize`：

- [src/cvcuda/priv/OpResize.cpp:L42-L61](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cpp#L42-L61)：Tensor 路径的 `operator()`（流程 ③ 的代码出处）；
- [src/cvcuda/priv/OpResize.cpp:L63-L75](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cpp#L63-L75)：变长批路径的 `operator()`——注意它对 LINEAR 插值显式检查每张图尺寸 ≥ 2×2，小图会抛异常。

**④ CUDA kernel 层**——`OpResize.cu` 中定义了一族 `__global__` kernel（Nearest/Linear/Cubic/Area 各有多个特化版本），并在分派逻辑中以 `<<<blocks, threads, 0, stream>>>` 语法启动，注意 launch 的第 4 个参数就是贯穿全链路的 CUDA 流：

- [src/cvcuda/priv/OpResize.cu:L373](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cu#L373)：`__global__ void LinearResize(...)` kernel 定义；
- [src/cvcuda/priv/OpResize.cu:L1376-L1397](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cu#L1376-L1397)：`NearestResize<<<...>>>` 与 `LinearResize<<<...>>>` 的 kernel 启动语句（流程 ④ 的代码出处）。

**⑤ nvcv 类型层**——所有层共同依赖的数据容器定义在 `src/nvcv`。`nvcv::Tensor` 是包装 C 句柄 `NVCVTensorHandle` 的 C++ RAII 类，提供 `Create(shape, dtype)` 等工厂：

- [src/nvcv/src/include/nvcv/Tensor.hpp:L41](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.hpp#L41)：`class Tensor : public CoreResource<NVCVTensorHandle, Tensor>` 声明；
- [src/nvcv/src/include/nvcv/Tensor.hpp:L170-L173](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.hpp#L170-L173)：三个 `Tensor` 构造重载（按 Requirements / TensorShape / 图像尺寸+格式创建）。

> 说明：`docs/sphinx/content/cvcuda_arch.svg` 是官方绘制的架构图，读者可在浏览器打开对照；上文的分层与调用链全部是从本仓库源码逐行验证得出的，可与图互相印证。

#### 4.4.4 代码实践（本讲核心实践）

**实践目标**：亲手验证 4.4.2 的调用链——在仓库中找出 `cvcuda.resize` 从 Python 到 kernel 经过的每个目录的一个真实文件，并记录路径。

**操作步骤**：

1. 打开 [README.md:L35](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L35) 引用的架构图（`docs/sphinx/content/cvcuda_arch.svg`），在浏览器中查看。
2. 在纸上画四个方框：`python/mod_cvcuda` → `src/cvcuda`（C API + priv）→ kernel（`src/cvcuda/priv/*.cu`），旁边画一个贯穿的 `src/nvcv`（数据层）。
3. 用下表在仓库里逐个找到文件并打开确认（答案已给出行号，可自查）：

| 层 | 文件 | 你要确认的东西 |
|----|------|----------------|
| Python 绑定 | `python/mod_cvcuda/operators/OpResize.cpp` | 第 109 行附近的 `m.def("resize", ...)` |
| C API | `src/cvcuda/OpResize.cpp` | 第 45 行的 `cvcudaResizeSubmit` |
| C API 头 | `src/cvcuda/include/cvcuda/OpResize.h` | 第 57 行起的 Limitations 支持矩阵 |
| C++ 类头 | `src/cvcuda/include/cvcuda/OpResize.hpp` | 第 42 行的 `class Resize final : public IOperator` |
| 私有实现 | `src/cvcuda/priv/OpResize.cpp` | 第 42 行的 `Resize::operator()` 与 `exportData` |
| kernel | `src/cvcuda/priv/OpResize.cu` | 第 373 行的 `LinearResize` 与第 1395 行的 `<<<...>>>` 启动 |
| 数据类型 | `src/nvcv/src/include/nvcv/Tensor.hpp` | 第 41 行的 `class Tensor` |

4. 在每个文件里用 `grep -n "关键词"` 找到上表位置，抄下你看到的那一行代码。

**需要观察的现象**：每一层的文件都能打开且行号对得上；`priv/OpResize.cpp` 与 `priv/OpResize.cu` 是同名的两个文件（一个管逻辑、一个管 kernel）。

**预期结果**：得到一张「一层一行真实代码」的调用路径图。这是纯源码阅读实践，不需要 GPU。

#### 4.4.5 小练习与答案

**练习 1**：Python 绑定层的 `resize->submit(...)` 与 C API 的 `cvcudaResizeSubmit` 是什么关系？最终都到哪里汇合？
**答案**：两者是并列的入口：Python 绑定直接持有 `cvcuda::priv::Resize` C++ 对象（通过 `CreateOperator<cvcuda::Resize>()` 创建），C API 则用句柄包装同一类对象。它们都汇合到 `cvcuda::priv::Resize::operator()`（`src/cvcuda/priv/OpResize.cpp:L42`），再进入 `OpResize.cu` 的 kernel 分派。

**练习 2**：为什么 Python 侧 `ResizeInto` 里要写 `guard.add(LockMode::LOCK_MODE_READ, {input})` 和 `WRITE {output}`？
**答案**：`ResourceGuard` 在多流/多线程场景下对输入输出资源加读写锁，保证同一数据不会同时被两条流一个读一个写，避免竞态。这是 CV-CUDA 并发安全的关键机制之一，第 u4 单元会展开。

**练习 3**：`docs/sphinx/content/cvcuda_arch.svg` 里的图和本讲的分层图来源有何不同？
**答案**：SVG 是官方绘制的示意图（内嵌位图，只能看不能读文本）；本讲的分层图与调用链是从源码逐文件、逐行号验证归纳出来的，可与官方图互相印证。

---

## 5. 综合实践

**任务：给「一次 GPU 图像缩放」写一份完整的仓库路径档案。**

1. **画图**：参照 4.4.2，自己动手（不看答案）画出 `cvcuda.resize` 从 Python 调用到 CUDA kernel 的路径图，标出每一层所在的目录。
2. **标注文件**：在图中每一层贴上你亲手打开并核实过的文件路径与关键行号（共 7 个文件，见 4.4.4 的表格）。
3. **对照架构图**：打开 `docs/sphinx/content/cvcuda_arch.svg`，比较官方图与你的图，把官方图里有而你图上没有的元素记下来（例如编解码、推理框架的位置），下一讲安装运行 hello_world 时回头验证。
4. **延伸一笔**：在图上补一条「变长批」支线——`ImageBatchVarShape` 输入时 kernel 落在 `src/cvcuda/priv/legacy/resize_var_shape.cu`（用 `ls src/cvcuda/priv/legacy/ | grep resize` 确认该文件存在）。

**验收标准**：合上讲义，你能对着自己画的图，向同事讲清「cvcuda.resize 一行代码在仓库里经历了什么」。

## 6. 本讲小结

- CV-CUDA 是 NVIDIA 开源的 GPU 计算机视觉算子库，定位是 AI 预处理/后处理管线的高吞吐、低延迟加速，支持 x86_64/aarch64/WSL2、CUDA 12.2+ 与 13.x、Python 3.10–3.14、SM7.5+。
- 仓库顶层按职责分层：`src/`（核心实现）、`python/`（绑定）、`tests/`、`bench/`、`samples/`、`docs/`、`docker/`、`ci/`、`lint/`，官方权威目录表在 AGENTS.md。
- 代码内部分四层：Python 绑定（`python/mod_cvcuda`）→ C API（`src/cvcuda/Op*.cpp` + `include/cvcuda/*.h`）→ C++ 私有实现（`src/cvcuda/priv/`）→ CUDA kernel（`priv/Op*.cu` 与 `priv/legacy/*.cu`）。
- `nvcv` 是被所有层依赖的数据类型层（Tensor、ImageBatch 等），「数据」与「操作」分离是理解本仓库的第一把钥匙。
- 每个算子在 C 头文件里带一份 Limitations 支持矩阵（dtype/布局/通道），这是判断「某算子支不支持我的数据」的最快途径。
- 一次 `cvcuda.resize` 调用穿越约 7 个文件，本讲已给出全部真实路径与行号。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：安装与第一个程序——用 `pip install cvcuda-cu12/cvcuda13` 跑通 `samples/applications/hello_world.py`（解码 → 缩放 → 高斯模糊 → 编码的全 GPU 流程），把本讲画在纸上的架构落到真实运行。
- **延伸阅读**：`docs/sphinx/installation.rst`（完整安装方式）、`samples/README.md`（示例总览）、`AGENTS.md` 的 Authoritative docs 一节（按主题找权威文档的方法）。
- **为后续单元做准备**：第 u2 单元将深入 `src/nvcv` 的 Tensor/TensorShape/DataLayout，建议先浏览 `src/nvcv/src/include/nvcv/` 目录下的头文件名，混个眼熟。
