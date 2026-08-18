# 环境搭建与预训练权重下载

## 1. 本讲目标

上一讲（u1-l1）我们建立了 DreamCraft3D 的全局观：它是一条「coarse-nerf → coarse-neus → geometry → texture」的四阶段流水线。本讲解决把这条流水线真正跑起来之前的第一道关卡——**环境**。读完本讲，你应该能够：

1. 按照官方文档完成本地（venv + pip）或 Docker 两条路径的环境安装；
2. 下载并**摆放到正确位置**的三类预训练权重：Zero123（`stable_zero123.ckpt`）、Omnidata 深度/法向模型、以及由 HuggingFace Hub 自动下载的 DeepFloyd IF 与 Stable Diffusion 2.1；
3. 说清楚 `load/` 目录下每个子目录的作用，以及哪些文件是仓库自带的、哪些必须手动下载；
4. 识别安装过程中最容易踩的坑（隐藏的 CUDA 扩展依赖、不存在的 `load/omnidata/` 目录、gated 模型授权等）。

## 2. 前置知识

- **GPU 与显存（VRAM）**：DreamCraft3D 的训练要在显存里同时装下三维场景表示和多个扩散模型（UNet、VAE、文本编码器），显存直接决定你能不能跑、能跑多大分辨率。
- **CUDA**：NVIDIA 的并行计算平台。PyTorch 的 GPU 版本必须和你机器上的 **NVIDIA 驱动 / CUDA Toolkit 版本兼容**（例如 `torch2.0.0+cu118` 表示适配 CUDA 11.8）。版本不匹配是安装失败的头号原因。
- **CUDA 扩展（CUDA extension）**：一些 Python 包（如 `nerfacc`、`tiny-cuda-nn`、`nvdiffrast`）在安装时需要**现场编译 CUDA C++ 代码**，因此要求机器上有 `nvcc`（CUDA 编译器）和匹配的编译架构列表。它们比纯 Python 包慢得多、也更容易出错。
- **虚拟环境（virtualenv/conda）**：给每个项目一个独立的 Python 包空间，避免不同项目依赖版本互相污染。
- **预训练权重 / checkpoint（ckpt）**：别人已经训练好的模型参数文件。DreamCraft3D 自己不附带这些大文件（git 仓库只放代码），需要我们下载后放到约定路径。
- **HuggingFace Hub**：类似「模型界的 GitHub」。配置里写 `"DeepFloyd/IF-I-XL-v1.0"` 这种字符串时，`diffusers`/`transformers` 库会自动从 Hub 下载模型并缓存在 `~/.cache/huggingface/`，首次运行需要联网，之后复用缓存。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md) | 安装入口章节：GPU 门槛、PyTorch 版本、权重下载命令 |
| [docs/installation.md](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/docs/installation.md) | 上游 threestudio 的详细安装文档：CUDA Toolkit 安装与 Docker 流程 |
| [requirements.txt](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/requirements.txt) | pip 依赖清单，按用途分了四组注释 |
| [docker/Dockerfile](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/docker/Dockerfile) | 容器镜像构建脚本：基础镜像、CUDA 架构、依赖安装顺序 |
| [docker/compose.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/docker/compose.yaml) | `docker compose` 编排：挂载仓库、GPU 直通、用户参数 |
| [preprocess_image.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py) | 输入图预处理脚本，其中硬编码了 Omnidata 权重的相对路径 |
| [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml) | 粗阶段配置，指明 Zero123 权重与 LDM 配置的路径 |
| `load/` 目录 | 权重与静态资源的约定存放地（zero123/tets/lights/images 子目录） |

## 4. 核心概念与源码讲解

### 4.1 安装文档与硬件门槛：README 与 docs/installation.md 的分工

#### 4.1.1 概念说明

项目提供了两份安装说明，读者常混淆两者的定位：

- `README.md` 的 Installation 章节是 **DreamCraft3D 自己的安装入口**，声明了本项目真实的硬件门槛和权重下载方式；
- `docs/installation.md` 是 **从上游 threestudio 项目继承来的文档**（README 第 50 行明确说「This part is the same as original threestudio」），它补充了 CUDA Toolkit 安装细节和 Docker 完整流程，但其中的硬件数字是上游项目的，不能直接套用在 DreamCraft3D 上。

两份文档的显存要求不一致，这正是初学者第一个要理清的问题。

#### 4.1.2 核心流程

本地安装的主线流程（来自 README）：

```text
确认 NVIDIA GPU + 驱动 + CUDA
        │
        ▼
Python >= 3.8，创建 virtualenv 并升级 pip
        │
        ▼
安装 PyTorch（torch1.12.1+cu113 或 torch2.0.0+cu118 二选一）
        │
        ▼
（可选）pip install ninja   ← 加速后续 CUDA 扩展编译
        │
        ▼
pip install -r requirements.txt
        │
        ▼
下载预训练权重到 load/ 对应子目录（4.4 节详述）
```

#### 4.1.3 源码精读

- [README.md:54](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L54)：本项目要求 **至少 20GB 显存的 NVIDIA 显卡**并已安装 CUDA。
- [docs/installation.md:5](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/docs/installation.md#L5)：上游文档写的是「至少 6GB 显存」。矛盾的原因：threestudio 框架本身很轻，但 DreamCraft3D 的四阶段训练要在显存中同时容纳 DeepFloyd IF、Zero123、Stable Diffusion 2.1 等多个扩散模型，因此门槛大幅提高。**以 README 的 20GB 为准**；README 的 Tips 还提到默认配置是在 **40GB A100** 上跑的（[README.md:169](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L169)），显存不足时可用 `data.height=128 data.width=128 data.random_camera.height=128 data.random_camera.width=128` 降低渲染分辨率。
- [README.md:67-L74](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L67-L74)：官方测试过 `torch1.12.1+cu113` 和 `torch2.0.0+cu118` 两个版本组合，其他版本「应该也行」但未测试。建议照抄这两个组合之一。
- [docs/installation.md:14-L18](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/docs/installation.md#L14-L18)：CUDA Toolkit 的安装示例（Ubuntu 22.04 与 WSL2 两条路径），已装新版本或使用 Docker 可跳过。
- [docs/installation.md:53-L59](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/docs/installation.md#L53-L59)：一个重要的 Docker 已知问题——容器里 OpenGL 光栅化器会报错，需改用 CUDA 光栅化器：训练时加 `system.renderer.context_type=cuda`，导出网格时加 `system.exporter.context_type=cuda`。这会在后续 u5-l4（nvdiffrast 光栅化）和 u2-l4（网格导出）两讲中再次出现。

#### 4.1.4 代码实践

1. **实践目标**：摸清自己机器的 GPU/驱动/CUDA 状况，判断能否安装。
2. **操作步骤**：
   ```sh
   nvidia-smi                       # 查看显存总量与驱动版本（Driver >= CUDA 版本对应要求）
   nvcc --version                   # 查看 CUDA Toolkit 版本（若装了的话）
   python3 --version                # 确认 >= 3.8
   ```
3. **需要观察的现象**：`nvidia-smi` 顶部会显示 `CUDA Version: XX.X`——这是**驱动支持的最高 CUDA 版本**，不是已安装版本；只要它 >= 你选的 torch 包（cu113 或 cu118）即可。
4. **预期结果**：显存 ≥ 20GB 可跑默认配置；显存更小则要准备降低分辨率的配置覆盖。本步骤结果因机器而异，**待本地验证**。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `docs/installation.md` 说 6GB 显存就够，README 却要求 20GB？
  **答案**：`docs/installation.md` 是上游 threestudio 框架的通用文档，框架本体（可微渲染 + Lightning 训练循环）确实轻量；DreamCraft3D 在框架之上叠加了多个大型扩散先验（DeepFloyd IF、Zero123、SD2.1），它们才是显存大户，所以以 README 的 20GB 为准。
- **练习 2**：`nvidia-smi` 显示的 `CUDA Version` 和 `nvcc --version` 显示的版本，哪个决定你能装哪个 torch？
  **答案**：驱动支持的版本（`nvidia-smi` 显示的）是上限，torch 的 `+cuXXX` 后缀不能超过它；`nvcc` 只在编译 CUDA 扩展（nerfacc 等）时需要，且其版本要与 torch 的 CUDA 版本尽量一致。

### 4.2 依赖清单精读：requirements.txt 与「不在清单里」的 CUDA 扩展

#### 4.2.1 概念说明

`requirements.txt` 是 pip 依赖清单。DreamCraft3D 的清单用注释分了四组，每组对应一块功能。但**清单并不完整**：三个需要现场编译的 CUDA 扩展（`nerfacc`、`tiny-cuda-nn`、`nvdiffrast`）以及预处理用的 `carvekit`、`gdown` 都不在里面——它们要么记录在 Dockerfile 里，要么是文档隐含的前置条件。这是新手安装时最容易卡住的地方。

#### 4.2.2 核心流程

依赖安装的正确顺序（顺序很重要）：

```text
1. torch + torchvision          ← 必须最先装，其余包要链接它
2. ninja                        ← 让下面的编译更快
3. nerfacc (v0.5.2)             ← git 安装，现场编译 CUDA
4. tiny-cuda-nn                 ← git 安装，现场编译 CUDA
5. nvdiffrast                   ← git 安装，现场编译 CUDA
6. pip install -r requirements.txt
7. pip install carvekit gdown   ← 预处理脚本需要（清单中没有）
```

#### 4.2.3 源码精读

- [requirements.txt:1-L19](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/requirements.txt#L1-L19)：第一组**核心训练依赖**。注意两个**精确锁死**的版本：`lightning==2.0.0`（PyTorch Lightning 训练框架，`launch.py` 的 Trainer 由它提供）和 `omegaconf==2.3.0`（配置系统，u2-l2 会精读）；`diffusers<=0.23.0` 是**上界锁定**，因为扩散模型 API 在高版本变动剧烈。
- [requirements.txt:22-L27](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/requirements.txt#L22-L27)：第二组注释 `# deepfloyd`——DeepFloyd IF 文生图模型所需（`xformers` 省显存注意力、`bitsandbytes`、`sentencepiece` 分词器、`safetensors`、`huggingface_hub`）。
- [requirements.txt:29-L32](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/requirements.txt#L29-L32)：第三组 `# for zero123`——Zero123 视图条件模型所需，其中 `taming-transformers-rom1504` 是 Zero123 的 LDM 代码依赖的 VQGAN 库。
- [requirements.txt:34-L36](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/requirements.txt#L34-L36)：第四组 `#controlnet` 与 `numpy>=1.22.2`（安全漏洞下界）。
- **不在清单里的证据一**：[threestudio/models/renderers/base.py:3](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/base.py#L3) `import nerfacc`（体渲染加速库，四个 renderer 都依赖）；[threestudio/models/networks.py:3](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py#L3) `import tinycudann as tcnn`（哈希编码，u5-l1 会精读）；[threestudio/utils/rasterize.py:1](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/rasterize.py#L1) `import nvdiffrast.torch as dr`（可微光栅化，DMTet 阶段与网格导出需要）。三者都要单独安装：
  ```sh
  pip install git+https://github.com/KAIR-BAIR/nerfacc.git@v0.5.2
  pip install git+https://github.com/NVlabs/tiny-cuda-nn.git#subdirectory=bindings/torch
  pip install git+https://github.com/NVlabs/nvdiffrast/
  ```
  其中前两条正是 Dockerfile 中记录的版本（见 4.3.3）。
- **不在清单里的证据二**：[preprocess_image.py:18](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L18) `from carvekit.api.high import HiInterface`（去背景）——`carvekit` 与 README 用到的 `gdown`（下载 Omnidata）都需要额外 `pip install`。

#### 4.2.4 代码实践

1. **实践目标**：验证依赖装齐，尤其是三个 CUDA 扩展能否 import。
2. **操作步骤**（在虚拟环境中，按 4.2.2 的顺序安装后执行）：
   ```sh
   python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
   python -c "import nerfacc; print('nerfacc ok')"
   python -c "import tinycudann; print('tcnn ok')"
   python -c "import nvdiffrast.torch; print('nvdiffrast ok')"
   ```
3. **需要观察的现象**：`torch.cuda.is_available()` 为 `True`；三个扩展 import 不报错（首次 import `tinycudann`/`nvdiffrast` 时可能还会触发一次运行时 JIT 编译，属正常）。
4. **预期结果**：四条命令全部成功，即可进入权重下载环节。CUDA 扩展编译失败通常与 `nvcc` 版本和 torch CUDA 版本不一致有关。**待本地验证**。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `lightning` 和 `omegaconf` 用 `==` 锁死，而 `transformers`、`opencv-python` 不锁？
  **答案**：`launch.py` 的 Trainer 组装与配置系统的 resolver 行为强依赖 `lightning 2.0.0` / `omegaconf 2.3.0` 的具体 API，升级可能直接崩；而后者的 API 相对稳定，宽松约束减少与其他包的冲突。
- **练习 2**：如果跳过 `pip install ninja` 直接装 `tiny-cuda-nn`，会发生什么？
  **答案**：也能装，但 CUDA 扩展会用默认的构建方式编译，速度明显更慢；ninja 提供增量编译，Dockerfile 第 51 行也是先装 ninja 再装 torch 和扩展。

### 4.3 Docker 安装路径：Dockerfile 与 compose.yaml

#### 4.3.1 概念说明

Docker 把「操作系统 + CUDA + Python + 所有依赖」打包成一个镜像，避免「在我机器上能跑」的问题。DreamCraft3D 的镜像基于 NVIDIA 官方 CUDA 开发镜像，把 4.2 节的整个安装顺序固化成脚本；`compose.yaml` 则描述如何从这个镜像启动容器：把仓库目录挂载进去、把宿主机 GPU 直通进来。

#### 4.3.2 核心流程

```text
宿主机：NVIDIA 驱动 + Docker Engine + NVIDIA Container Toolkit
        │
        ▼
cd docker/ && docker compose build   ← 按 Dockerfile 构建镜像（首次很慢，要编译 CUDA 扩展）
        │
        ▼
docker compose up -d                 ← 后台启动容器
        │
        ▼
docker compose exec threestudio bash ← 进入容器，仓库已挂载在 ~/threestudio
        │
        ▼
（容器内）下载权重到 load/ → 开始训练
```

#### 4.3.3 源码精读

- [docker/Dockerfile:5](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/docker/Dockerfile#L5)：基础镜像 `nvidia/cuda:11.8.0-devel-ubuntu22.04`——注意是 `devel` 版，自带 `nvcc` 编译器（CUDA 扩展编译必需），Ubuntu 22.04 自带 Python 3.10。
- [docker/Dockerfile:14-L21](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/docker/Dockerfile#L14-L21)：`TORCH_CUDA_ARCH_LIST` 与 `TCNN_CUDA_ARCHITECTURES` 列出**所有**支持的 GPU 架构（6.0 到 9.0+PTX），编译时要为每个架构生成代码，所以默认构建很慢。注释给出了提速方法：只用自己显卡的架构，例如 RTX 30xx 打开第 17-18 行（`8.6`），RTX 40xx 打开第 19-20 行（`8.9`）。查自己显卡架构号：[developer.nvidia.com/cuda-gpus](https://developer.nvidia.com/cuda-gpus)。
- [docker/Dockerfile:23-L26](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/docker/Dockerfile#L23-L26)：设置 `CUDA_HOME`、`PATH`、`LD_LIBRARY_PATH`，让 pip 编译扩展时能找到 CUDA——本地安装没配这些环境变量是编译失败的常见原因。
- [docker/Dockerfile:29-L44](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/docker/Dockerfile#L29-L44)：apt 安装的系统库。注意 `libegl1-mesa-dev`、`libgl1-mesa-dev`、`libgles2-mesa-dev` 是 OpenGL 头文件——给 nvdiffrast 的 OpenGL 光栅化路径用（正是 4.1.3 提到的容器里仍有问题的那条路径）。
- [docker/Dockerfile:51-L56](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/docker/Dockerfile#L51-L56)：安装顺序的证据：先 `torch==2.0.1+cu118`，然后**在 requirements.txt 之前**安装 `nerfacc@v0.5.2` 和 `tiny-cuda-nn`，注释写明「these two installations are time consuming and error prone」——把它们单独列出来既是为了利用 Docker 分层缓存（这两个不常变，requirements 变了也不用重编），也是给本地安装者的顺序提示。（注意 Dockerfile 里同样没有 nvdiffrast，见 4.2.3。）
- [docker/Dockerfile:58-L60](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/docker/Dockerfile#L58-L60)：最后拷贝 requirements.txt 安装，工作目录设为 `/home/dreamer/threestudio`——与 compose.yaml 的挂载点对应。
- [docker/compose.yaml:6-L12](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/docker/compose.yaml#L6-L12)：构建参数从宿主机环境变量取值（`HOST_UID=$(id -u)` 等），使容器内用户与宿主机用户同 UID，避免挂载目录的权限问题。
- [docker/compose.yaml:13-L21](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/docker/compose.yaml#L13-L21)：运行时配置——`NVIDIA_DISABLE_REQUIRE: 1` 规避 `nvidia-container-cli` 的驱动版本检查报错；`shm_size: '4gb'` 扩大共享内存（PyTorch DataLoader 多进程需要）；卷挂载 `../:/home/dreamer/threestudio` 把整个仓库映射进容器；`deploy.resources` 段声明 GPU 直通。

#### 4.3.4 代码实践

1. **实践目标**：用 Docker 构建一个「只为自己的 GPU 优化」的镜像构建计划（不要求真的执行）。
2. **操作步骤**：
   - 查出自己的显卡架构号（例如 RTX 4090 = 8.9）；
   - 编辑 `docker/Dockerfile`：注释掉第 14-15 行的全架构列表，取消注释第 19-20 行（8.9 那组）；
   - 计划命令：`cd docker/ && docker compose build`。
3. **需要观察的现象**：构建日志中 `nerfacc` 和 `tiny-cuda-nn` 的编译阶段只出现一个架构号，耗时应显著低于全架构构建。
4. **预期结果**：构建完成后 `docker compose up -d && docker compose exec threestudio bash` 进入容器，`python -c "import torch; print(torch.cuda.is_available())"` 输出 `True`。构建耗时取决于机器，**待本地验证**。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 Dockerfile 选 `nvidia/cuda:11.8.0-devel-ubuntu22.04` 而不是 `-runtime-` 版镜像？
  **答案**：`runtime` 版只有运行 CUDA 程序的库，没有 `nvcc`；而 nerfacc、tiny-cuda-nn 安装时要现场编译 CUDA 代码，必须用带编译工具链的 `devel` 版。
- **练习 2**：容器里已经装好了依赖，为什么还要挂载宿主机仓库目录，而不是把代码 COPY 进镜像？
  **答案**：挂载（compose.yaml 第 17 行）让容器内外共享同一份代码——你在宿主机改代码、下载权重，容器里立即生效；镜像只固化「环境」，代码和权重保持在外部，镜像可以反复复用。
- **练习 3**：`shm_size: '4gb'` 去掉会怎样？
  **答案**：Docker 默认共享内存只有 64MB，PyTorch 多进程 DataLoader 在张量较大时可能报 `Bus error` / shared memory 耗尽错误，所以预先调大。

### 4.4 load/ 目录组织与预训练权重的正确摆放

#### 4.4.1 概念说明

`load/` 是 DreamCraft3D 约定的「静态资源 + 大文件」目录。git 仓库里只有小文件（配置、网格数据、示例图），大权重必须手动下载放进去。摆放位置不是随意的——**代码和 yaml 配置里硬编码了相对路径**，放错位置训练时会直接 `FileNotFoundError`。同时，DeepFloyd IF 和 Stable Diffusion 2.1 这类以 HuggingFace 仓库 ID 引用的模型**不需要**手动下载，首次运行时自动拉取。

#### 4.4.2 核心流程

权重分为「手动下载到 `load/`」与「HF Hub 自动缓存」两类：

| 权重 | 引用方式 | 获取方式 | 存放位置 |
| --- | --- | --- | --- |
| stable_zero123.ckpt | yaml 相对路径 | HF `stabilityai/stable-zero123` 手动下载 | `load/zero123/` |
| zero123-xl.ckpt（论文用） | 同上（改配置） | `load/zero123/download.sh` | `load/zero123/` |
| omnidata_dpt_depth_v2.ckpt | preprocess_image.py 相对路径 | `gdown '1Jrh-bRnJEjyMCS7f-WsaFlccfPjJPPHI'` | `load/omnidata/`（**需自建**） |
| omnidata_dpt_normal_v2.ckpt | 同上 | `gdown '1wNxVO4vVbDEMEpnAi_jwQObf2MFodcBR'` | `load/omnidata/`（**需自建**） |
| DeepFloyd/IF-I-XL-v1.0 | HF 仓库 ID | 首次运行自动下载（gated，需同意许可并登录） | `~/.cache/huggingface/` |
| stabilityai/stable-diffusion-2-1-base | HF 仓库 ID | 首次运行自动下载 | `~/.cache/huggingface/` |

`load/` 下仓库自带的内容：`zero123/`（LDM 结构配置 yaml + 下载脚本）、`tets/`（DMTet 四面体网格）、`lights/`（HDR 环境光与 BRDF 查找表）、`images/`（已预处理好的示例图四件套）、`prompt_library.json`（提示词库）。

#### 4.4.3 源码精读

- [README.md:88-L93](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L88-L93)：Zero123 权重说明——默认用最新的 `stable_zero123.ckpt`（从 [huggingface.co/stabilityai/stable-zero123](https://huggingface.co/stabilityai/stable-zero123) 手动下载）放入 `load/zero123/`；论文实验用的是 `zero123-xl.ckpt`，可用 `load/zero123/download.sh` 里的 wget 下载。
- [load/zero123/download.sh:1-L4](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/load/zero123/download.sh#L1-L4)：脚本内容——第 3 行 `wget https://zero123.cs.columbia.edu/assets/zero123-xl.ckpt`（zero123-xl），第 4 行是注释，提示 `stable_zero123.ckpt` 要去 HF 页面手动下载。即**这个脚本只下载 zero123-xl，不含 stable_zero123**。
- [README.md:95-L100](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L95-L100)：Omnidata 权重下载命令。注意 README 写 `cd load/omnidata`，但**仓库里并没有 `load/omnidata/` 目录**（`ls load/` 只有 images/lights/tets/zero123 等），需要先 `mkdir -p load/omnidata`。存放位置的权威依据是代码：
- [preprocess_image.py:68-L79](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L68-L79)：`DPT` 类按任务加载权重——`task='depth'` 时读 `load/omnidata/omnidata_dpt_depth_v2.ckpt`（第 69 行），否则读 `load/omnidata/omnidata_dpt_normal_v2.ckpt`（第 78 行）。路径是**相对路径**，所以 `preprocess_image.py` 必须在仓库根目录下运行。
- [configs/dreamcraft3d-coarse-nerf.yaml:96](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L96) 与 [configs/dreamcraft3d-coarse-nerf.yaml:103-L104](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L103-L104)：粗阶段的两个扩散先验来源——DeepFloyd 用 HF ID `DeepFloyd/IF-I-XL-v1.0`（自动下载，该模型是 gated 仓库，需先在 HF 页面同意许可并 `huggingface-cli login`）；Zero123 用 `./load/zero123/stable_zero123.ckpt` 权重 + `./load/zero123/sd-objaverse-finetune-c_concat-256.yaml` 结构配置（后者仓库已自带）。geometry、coarse-neus、texture 阶段的配置同样引用这些路径。
- [threestudio/models/guidance/stable_zero123_guidance.py:40-L49](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L40-L49)：`load_model_from_config` 的加载逻辑——用 yaml 配置实例化模型结构，`torch.load(ckpt)` 读权重，`load_state_dict(strict=False)` 载入。放错路径时在这里报 `FileNotFoundError`。`vram_O=True` 分支还会删掉 VAE 解码器省显存（u7-l3 精读）。
- 仓库自带资源的使用方：[threestudio/models/geometry/tetrahedra_sdf_grid.py:74](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L74) 读取 `load/tets/{分辨率}_tets.npz`（DMTet 四面体网格，u5-l4）；[threestudio/models/materials/pbr_material.py:22](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/pbr_material.py#L22) 读取 `load/lights/mud_road_puresky_1k.hdr`（导出网格时的环境光照）。
- [configs/dreamcraft3d-texture.yaml:73-L82](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L73-L82)：纹理阶段的三条 StableDiffusionPipeline 全部用 HF ID `stabilityai/stable-diffusion-2-1-base`（BSD 三管线，u7-l4 精读），无需手动下载。

#### 4.4.4 代码实践

1. **实践目标**：把需要手动下载的权重放到正确位置，并用一个检查脚本核对完整性。
2. **操作步骤**：
   ```sh
   # Zero123（默认）：到 https://huggingface.co/stabilityai/stable-zero123 手动下载后
   cp /path/to/stable_zero123.ckpt load/zero123/

   # Omnidata：注意先创建目录（README 没提这一步）
   mkdir -p load/omnidata && cd load/omnidata
   gdown '1Jrh-bRnJEjyMCS7f-WsaFlccfPjJPPHI&confirm=t'   # 深度模型
   gdown '1wNxVO4vVbDEMEpnAi_jwQObf2MFodcBR&confirm=t'   # 法向模型
   cd ../..

   # DeepFloyd 是 gated 模型：先在 HF 页面同意许可，然后
   huggingface-cli login
   ```
3. **需要观察的现象**：`ls -lh load/zero123/ load/omnidata/` 能看到三个 ckpt；`stable_zero123.ckpt` 约为数 GB 量级，两个 omnidata ckpt 各几百 MB。
4. **预期结果**：检查脚本（见第 5 节综合实践）全部输出 `[OK]`。文件大小与下载渠道有关，**待本地验证**。

#### 4.4.5 小练习与答案

- **练习 1**：把 `stable_zero123.ckpt` 放到 `load/` 根目录（而不是 `load/zero123/`）会怎样？错误在哪个环节暴露？
  **答案**：训练启动、system 构建 `stable-zero123-guidance` 时，[stable_zero123_guidance.py:41](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_zero123_guidance.py#L41) 的 `torch.load(ckpt)` 抛 `FileNotFoundError`，因为 yaml 里写死了 `./load/zero123/stable_zero123.ckpt`（也可以用命令行覆盖 `system.guidance_3d.pretrained_model_name_or_path` 改路径）。
- **练习 2**：为什么 Omnidata 必须手动下载，而 Stable Diffusion 2.1 不用？
  **答案**：配置引用方式不同。Omnidata 在 `preprocess_image.py` 里用本地文件路径 `load/omnidata/xxx.ckpt` 加载；SD2.1 在 texture 配置里用 HF 仓库 ID 引用，`diffusers` 库会自动从 Hub 下载到本地缓存。
- **练习 3**：`load/images/` 下的 `mushroom_log_rgba.png`、`mushroom_log_depth.png`、`mushroom_log_normal.png` 三件套是怎么来的？
  **答案**：就是 `preprocess_image.py` 对原始输入图跑「去背景 + Omnidata 深度/法向预测」的产物（输出命名规则见 [preprocess_image.py:118-L121](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L118-L121)）。这批示例图让你在还没跑通预处理前就能先用现成数据训练——这正是下一讲（u2-l1）的内容。

## 5. 综合实践

编写一个约 20 行的权重检查脚本 `check_load.py`（**示例代码**，非项目自带，放在仓库根目录运行）：

```python
import os

# (描述, 约定路径, 参考大小下限)  大小仅用于粗查下载截断，实际以官方发布为准
WEIGHTS = [
    ("Zero123 权重 (coarse 阶段 guidance_3d)", "load/zero123/stable_zero123.ckpt", 1e9),
    ("Omnidata 深度模型 (preprocess_image.py)", "load/omnidata/omnidata_dpt_depth_v2.ckpt", 1e8),
    ("Omnidata 法向模型 (preprocess_image.py)", "load/omnidata/omnidata_dpt_normal_v2.ckpt", 1e8),
    ("DMTet 四面体网格 (geometry 阶段)", "load/tets/128_tets.npz", 1e4),
    ("Zero123 LDM 结构配置 (仓库自带)", "load/zero123/sd-objaverse-finetune-c_concat-256.yaml", 1e2),
]

for desc, path, min_size in WEIGHTS:
    if not os.path.isfile(path):
        print(f"[缺失] {desc:45s} {path}")
    elif os.path.getsize(path) < min_size:
        print(f"[可疑] {desc:45s} {path}  仅 {os.path.getsize(path)/1e6:.1f} MB, 疑似下载不完整")
    else:
        print(f"[OK  ] {desc:45s} {path}  {os.path.getsize(path)/1e9:.2f} GB")
```

运行 `python check_load.py`：

- **目标**：核对 4.4 节的所有手动权重与自带资源是否齐备；
- **观察**：每行输出 `[OK]`/`[缺失]`/`[可疑]` 三种状态；大小下限是粗查（防止 wget 中断产生截断文件），不是官方校验值；
- **预期**：三个 ckpt 为 `[OK]`（或按提示补下载）；两个仓库自带文件始终为 `[OK]`。若 `load/omnidata/` 两项 `[缺失]`，先确认是否执行过 `mkdir -p load/omnidata`。实际数值**待本地验证**。

## 6. 本讲小结

- DreamCraft3D 的硬件门槛以 README 为准（**≥20GB 显存**），`docs/installation.md` 的 6GB 是上游 threestudio 的数字，只作参考。
- 依赖安装的隐藏难点是三个**不在 requirements.txt 里的 CUDA 扩展**：`nerfacc@v0.5.2`、`tiny-cuda-nn`、`nvdiffrast`，Dockerfile 记录了前两者的权威版本与安装顺序；预处理另需 `carvekit` 和 `gdown`。
- Docker 路径把整个环境固化在 `nvidia/cuda:11.8.0-devel-ubuntu22.04` 之上，按自己显卡编辑 `TORCH_CUDA_ARCH_LIST` 可大幅加速构建；容器内 nvdiffrast 的 OpenGL 光栅化有已知问题，需加 `system.renderer.context_type=cuda`。
- 权重分两类：**手动下载进 `load/`** 的 Zero123 与 Omnidata（注意 `load/omnidata/` 目录需自建）；**HF Hub 自动下载**的 DeepFloyd IF（gated，需同意许可并登录）与 SD2.1。
- `load/` 下仓库自带的 `tets/`、`lights/`、`images/`、`zero123/*.yaml` 分别服务于 DMTet 几何、网格导出光照、示例数据与 Zero123 结构配置，代码以相对路径硬编码引用，位置不可移动。

## 7. 下一步学习建议

环境就绪后，建议：

1. 先学 **u2-l1（输入图像预处理）**——亲手跑一遍 `preprocess_image.py`，理解 `load/images/` 下 rgba/depth/normal 三件套的来历，本讲下载的 Omnidata 权重会立即派上用场；
2. 再学 **u1-l3（仓库目录结构与代码地图）**与 **u1-l4（launch.py 入口）**，搞清楚命令行如何变成一次训练；
3. 权重加载的源码细节（`load_model_from_config` 的完整逻辑）将在 **u7-l3（Stable Zero123）**中展开。
