# 仓库结构与各模块职责

## 1. 本讲目标

在上一讲（[u1-l1](./u1-l1-project-background.md)）里，我们已经知道这个项目要解决什么问题：在卫星上用 SAR（合成孔径雷达）做船舶检测，并把功耗压在 10W 以内，硬件选 Kria KV260。但「知道目标」和「能读懂代码」之间还差一步——你得先知道**代码被放在了哪里、每个目录管什么**。

本讲学完后，你应该能够：

1. 说出仓库的六大核心组件，以及它们各自承担的职责。
2. 清楚地把「软件侧（数据准备 / 训练 / 量化 / 推理）」和「平台侧（硬件设计 / 固件 / HLS 加速核）」区分开来。
3. 在每个目录里找到它的入口文件（通常是 `README.md` 或主脚本），并知道该去哪里继续读下一讲。
4. 画出一张「数据从 xView3 原始场景流向板载推理结果」的模块依赖关系图。

本讲是后续所有讲义的「地图」——后面每一讲都会落到某一个具体目录里，所以请务必先把这张地图印在脑子里。

## 2. 前置知识

- **SAR 与 xView3-SAR 数据集**：上一讲已经介绍，SAR 场景由 VV、VH、bathymetry（水深）三个通道组成，原始场景很大，需要裁剪成固定大小（如 800×800）的「芯片（chip）」才能训练。
- **YOLOv8 与 DPU**：YOLOv8 是一个目标检测神经网络；DPU（Deep learning Processing Unit）是 Xilinx FPGA 上专门跑神经网络的硬件 IP。浮点模型不能直接在 DPU 上跑，要先**量化**成 int8。
- **PS 与 PL**：KV260 这类 MPSoC（多核异构片上系统）分两部分——PS（Processing System，ARM CPU）负责跑 Linux 和普通程序，PL（Programmable Logic，FPGA）负责跑 DPU 和 HLS 加速核。
- **「产物（artifact）」的概念**：每一步工程都会输出一个文件给下一步用。比如训练输出 `.pt` 权重，量化输出 `.xmodel`，硬件设计输出 `.xsa`。理解仓库结构的关键，就是理解**这些产物在目录之间如何流转**。

> 对初学者的一句话提醒：你不需要现在就懂量化和 HLS 是什么。本讲只要求你「知道这些目录存在、它们大致管什么」；具体原理在后续单元（u3、u4、u8）再展开。

## 3. 本讲源码地图

本讲主要阅读各目录的 `README.md`（它们是每个组件的「说明书」），以及仓库根 README 的「Repository Structure」一节：

| 文件 | 作用 | 本讲如何使用 |
| :--- | :--- | :--- |
| `README.md` | 仓库总说明，给出六大组件的一句话职责 | 看它如何划分目录 |
| `dataset/README.md` | 数据准备说明：目录约定 + 切片脚本用法 | 理解数据流入口 |
| `software/training/README.md` | YOLOv8 训练说明 + 对框架的五处修改 | 理解训练侧职责 |
| `platform/kv260/README.md` | KV260 硬件设计 + PetaLinux 镜像 + 固件部署 | 理解平台/固件职责 |
| `platform/post_processing/README.md` | HLS 解码核的目录结构 + 平台创建流程 | 理解 PL 加速核职责 |

补充阅读（不在本讲重点源码列表，但有助于完整理解软件栈）：

| 文件 | 作用 |
| :--- | :--- |
| `software/quantization/README.md` | 量化（PTQ/QAT）+ DPU 编译流程 |
| `software/inference_app/README.md` | 板载 C++ 推理应用的构建与运行 |
| `framework/vitis_ai/README.md` | 对 Vitis AI 推理框架的 C++ 补丁说明 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 顶层目录划分** —— 先建立全局地图。
- **4.2 软件栈组件** —— 数据、训练、量化、推理框架补丁、板载推理应用。
- **4.3 平台与硬件组件** —— KV260 硬件/固件、HLS 后处理解码核。

### 4.1 顶层目录划分

#### 4.1.1 概念说明

一个跨领域项目（遥感 + 深度学习 + FPGA + HLS + C++ 边缘推理）最容易让初学者迷失，因为它把**很多本来在完全不同工具链里的东西**塞进了一个仓库。作者的做法是：按「工程阶段」来切分目录，每个阶段对应一个相对独立的工作流和产物。

仓库根 `README.md` 的「Repository Structure」一节正式定义了六大核心组件（外加 `dataset/` 数据准备和 `assets/` 配图）。这些目录不是随便命名的，而是**严格对应端到端流水线的不同阶段**：

[README.md:12-19](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L12-L19) —— 这里用项目符号列出了六大组件（`framework/vitis_ai/`、`platform/kv260/`、`platform/post_processing/`、`software/training/`、`software/quantization/`、`software/inference_app/`），每条后面跟着一句话职责。

把这段话翻译成一张「目录 → 职责 → 主要产物」表：

| 顶层目录 | 一句话职责 | 主要产物（给下游的文件） |
| :--- | :--- | :--- |
| `dataset/` | 把 xView3 原始大场景裁成可训练的芯片 + YOLO 标签 | `*.tif` 芯片、`*.txt` 标签 |
| `software/training/` | 在 xView3-SAR 上训练定制 YOLOv8（含对框架的修改） | 浮点权重 `*.pt` |
| `software/quantization/` | 把 `.pt` 量化为 int8 并编译给 DPU | `*.xmodel`（DPU 可执行）、`.prototxt` |
| `software/inference_app/` | 板载 C++ 推理应用（精度基准 / 吞吐测试） | `xview3_benchmark`、`xview3_performance` 可执行文件、JSON/csv 预测 |
| `framework/vitis_ai/` | 对 Vitis AI 推理框架打 C++ 补丁（图像加载/归一化/NMS/后处理） | `xview3_yolov8_v3.5.patch` |
| `platform/kv260/` | Vivado 硬件设计 + PetaLinux 镜像 + 加速器固件 | `.xsa`、`project_1.bit.bin`、`.dtbo`、`shell.json` |
| `platform/post_processing/` | YOLOv8 解码步骤的 HLS 内核（PL 侧加速） | `.xo`、`xclbin`、host 程序 |
| `assets/` | README 用的配图（架构图、对比图） | `*.jpg` |

#### 4.1.2 核心流程

理解仓库结构的最有效方式，是顺着「数据怎么一路变成板载预测结果」走一遍。下面这张图把六个组件串成了一条流水线（箭头表示产物流转）：

```
        xView3 原始大场景 (.tif 三通道)
                      │
                      ▼
   ┌─────────────────────────────────────┐
   │ dataset/  (generate_xview3.py)      │  →  800×800 chips + YOLO 标签
   └─────────────────────────────────────┘
                      │
                      ▼
   ┌─────────────────────────────────────┐
   │ software/training/   (yolo train)   │  →  浮点权重 .pt
   └─────────────────────────────────────┘
                      │
                      ▼
   ┌─────────────────────────────────────┐
   │ software/quantization/              │  →  int8 .xmodel (DPU 可执行)
   │   PTQ/QAT → vai_c_xir 编译          │     + model.prototxt
   └─────────────────────────────────────┘
                      │
                      ▼
   ┌─────────────────────────────────────┐
   │ software/inference_app/             │  →  板载 JSON/csv 预测
   │   + framework/vitis_ai/ (补丁)      │
   └─────────────────────────────────────┘
            ▲                      ▲
            │                      │
   ┌───────────────────┐   ┌───────────────────────────┐
   │ platform/kv260/   │   │ platform/post_processing/ │
   │ 硬件设计 + 固件    │   │ HLS 解码核 xclbin          │
   │ (DPU 跑在上面)    │   │ (PL 侧加速后处理)          │
   └───────────────────┘   └───────────────────────────┘
```

伪代码式总结这条链路：

```text
原始场景 --dataset/--> chips --software/training/--> .pt
.pt --software/quantization/--> .xmodel
.xmodel + framework/vitis_ai 补丁 + software/inference_app --> 板载预测
板载运行的「底座」由 platform/kv260/ 提供（DPU 在此）
板载后处理加速由 platform/post_processing/ 提供（HLS 解码核）
```

注意三个观察点：

1. **`dataset/` 是整条链路的入口**，它没有上游依赖；后面所有组件都建立在它产出的芯片之上。
2. **`platform/` 下的两个目录是「横向支撑」**：它们不直接消费上游的 `.xmodel`，而是提供「模型在上面跑的硬件底座」和「加速后处理的 HLS 核」。所以图里用向上的箭头表示「支撑」，而不是顺序流转。
3. **`framework/vitis_ai/` 是一个补丁（patch）文件**，它本身不是可运行程序，而是要被「贴」到 Vitis AI 框架源码上重新编译——所以它和 `inference_app/` 紧挨在一起。

#### 4.1.3 源码精读

确认每个目录确实存在、且职责和上表一致。用只读 git 命令列出仓库顶层目录的所有文件：

[.gitignore 与顶层文件的真实清单可在仓库内用 `git ls-files` 查看] —— 本讲在「4.1.4 代码实践」里会带你实际跑这条命令。

回到根 README，注意它如何用一句话为每个组件定位。以 `platform/kv260/` 为例：

[README.md:15](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L15) —— 说明该目录包含「硬件设计文件、脚本和构建 KV260 上 Vitis AI 3.5 固件的说明」，包括 Vivado 设计、DPU 配置和硬件平台构建脚本。

[README.md:16](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L16) —— 说明 `platform/post_processing/` 是「YOLOv8 边界框解码后处理步骤的 Vitis HLS 实现，针对 KV260 上 PL 执行做了优化」。

这两句话非常重要，因为它们已经点破了 `platform/` 下两个子目录的根本区别：`kv260/` 管「整个硬件平台 + 固件」，`post_processing/` 管「一个具体的 PL 加速核」。这个区别会在 4.3 节展开。

#### 4.1.4 代码实践

**实践目标**：亲手确认仓库的真实目录结构，而不是只看本讲的表格。

**操作步骤**：

1. 在仓库根目录执行只读命令，列出所有被 git 跟踪的文件：
   ```bash
   git ls-files
   ```
2. 观察输出的**路径前缀**，把它们归类到顶层目录。
3. 对照本讲 4.1.1 的表格，确认六大组件确实存在，且每个目录里都有一个 `README.md`。

**需要观察的现象**：

- 输出里应该能看到 `dataset/README.md`、`dataset/generate_xview3.py`、`software/training/README.md`、`platform/kv260/README.md`、`platform/post_processing/README.md` 等路径。
- `assets/` 下应该有若干 `.jpg` 配图（如 `yolov8_diagram.jpg`、`inference_breakdown.jpg`）。

**预期结果**：你会得到一张与本讲表格一致的目录清单。注意 `software/quantization/` 下**没有** Python 源码，只有 `README.md` 和 `modifications.md`（因为许可证限制，量化代码只给了修改说明，这一点在 4.2 节会详谈）。

**待本地验证**：上述命令本身无害，可直接运行；不同 checkout 的文件清单应当一致。

#### 4.1.5 小练习与答案

**练习 1**：仓库根 README 的「Repository Structure」一节列出了几个核心组件？`dataset/` 是否在其中？

> **参考答案**：列出了六个核心组件（`framework/vitis_ai/`、`platform/kv260/`、`platform/post_processing/`、`software/training/`、`software/quantization/`、`software/inference_app/`）。`dataset/` 没有被列进这一节，但它有独立的 `dataset/README.md`，是数据准备的入口，属于事实上的第七个组件。

**练习 2**：如果有人说「我要把训练好的模型部署到板子上」，他会依次用到哪几个顶层目录？

> **参考答案**：`software/quantization/`（量化 + 编译出 `.xmodel`）→ `framework/vitis_ai/`（给推理框架打补丁）→ `software/inference_app/`（板载推理应用），同时依赖 `platform/kv260/`（提供能跑 DPU 的硬件/固件底座）和 `platform/post_processing/`（可选的 HLS 后处理加速）。

**练习 3**：`assets/` 目录属于「软件侧」还是「平台侧」？

> **参考答案**：都不属于。`assets/` 只是各 README 引用的配图（架构图、对比图、资源截图），不参与任何工程流转，是文档资产。

---

### 4.2 软件栈组件

#### 4.2.1 概念说明

「软件侧」指的是**不依赖具体 FPGA 硬件就能完成（或主要完成）**的那部分工作：准备数据、训练模型、量化模型、以及在 CPU 上可读/可写的推理代码。在本仓库里，软件侧包含五个目录：

| 目录 | 子主题 | 你需要记住的一点 |
| :--- | :--- | :--- |
| `dataset/` | 数据准备 | 把「大场景」变成「小芯片」 |
| `software/training/` | 模型训练 | 基于改过的 Ultralytics YOLOv8 |
| `software/quantization/` | 模型量化 | 浮点 → int8，并编译给 DPU |
| `framework/vitis_ai/` | 推理框架补丁 | 一个 `.patch` 文件，不是可运行程序 |
| `software/inference_app/` | 板载推理应用 | C++ 写的板载程序，输出预测 |

区分软件侧与平台侧的意义在于：**软件侧的大多数步骤可以在普通 CPU/GPU 服务器上完成**（训练尤其如此），只有最后一步（`inference_app` 真正运行）和量化里的「编译」步骤才需要与具体硬件绑定。所以如果你想先理解算法，完全可以只读软件侧；平台侧是「让它跑在 FPGA 上」的工程化部分。

#### 4.2.2 核心流程

软件侧的内部流转如下：

```text
dataset/        →  产 出: chips + labels
                        │ (被 training 当作训练集)
                        ▼
software/training/  →  产 出: .pt (浮点权重，含本工作对 YOLOv8 的 5 处修改)
                        │ (被 quantization 当作输入)
                        ▼
software/quantization/ → 产 出: .xmodel (int8, DPU 可执行) + .prototxt
                        │ (连同 framework/vitis_ai 补丁一起部署到板子)
                        ▼
software/inference_app/ → 产 出: 板载 JSON/csv 预测
   ▲
   │ framework/vitis_ai/ 提供 C++ 补丁（在编译 inference_app 之前贴到 Vitis AI 源码上）
```

注意两个关键衔接点：

1. **`dataset/` → `software/training/`**：训练侧用 `yolo train` 时，`data=` 参数指向的就是 `dataset/` 产出的芯片目录。
2. **`software/quantization/` → `software/inference_app/`**：量化编译出的 `.xmodel` 会被 `scp` 到板子的 `/usr/share/vitis_ai_library/models/`，而 `inference_app` 通过模型名加载它。中间夹着的 `framework/vitis_ai/` 补丁，必须先应用到 Vitis AI 框架源码并交叉编译，否则 `inference_app` 链接不到正确的库。

#### 4.2.3 源码精读

**（a）`dataset/`：数据目录约定与切片入口**

`dataset/README.md` 用一棵目录树说明了 xView3-SAR 数据集的组织方式：

[dataset/README.md:9-35](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md#L9-L35) —— 这棵树把数据集分成 `data/`（SAR `.tiff` 场景）、`labels/`（`.csv` 标注）、`shoreline/`（`.npy` 海岸线坐标）三大类，每类下再分 `training/validation/public`。

紧接着，README 给出了切片脚本的用法：

[dataset/README.md:36-39](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md#L36-L39) —— 说明 YOLOv8 需要裁剪后的芯片和每张芯片独立的 `.txt` 标签，预处理命令是 `python generate_xview3.py --labels ... --source ... --save_dir ... --imgsz <800|640> --name ...`。

这就是 `dataset/` 目录唯一的可执行脚本入口：`generate_xview3.py`（下一讲 u2 会逐行精读它）。

**（b）`software/training/`：训练 + 对框架的五处修改**

`software/training/README.md` 开篇就声明本工作基于 Ultralytics YOLOv8 release `v8.2.91` 做了定制：

[software/training/README.md:3-6](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L3-L6) —— 说明训练基于改自 `v8.2.91` 的 Ultralytics 框架，并配有 YOLOv8 架构图（backbone / neck / head 三段式）。

README 最重要的部分是「Framework Modifications」，列出了五处修改：

[software/training/README.md:24-66](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L24-L66) —— 五处修改分别是：(1) 图像加载加 `cv2.IMREAD_UNCHANGED` 以读 TIFF；(2) 自定义 SAR 多通道线性归一化；(3) 用 PIoU2 替代默认 CIoU；(4) 验证时把预测还原到场景全局坐标；(5) 实现 xView3 竞赛指标。

这五处修改是 u3 单元（训练与框架定制）的全部主线，本讲只需要知道「它们都住在 `software/training/` 里」。

训练侧的 CLI 入口在 README 的「Usage」一节：

[software/training/README.md:71-78](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L71-L78) —— 给出 `yolo train / yolo val / yolo predict` 三条命令，配置以 `.yaml` 形式存放在 `ultralytics/cfg/`。

**（c）`software/quantization/`：量化的特殊性**

这个目录有个**对初学者很容易踩坑**的特点：它没有 Python 源码。

[software/quantization/README.md:3-5](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L3-L5) —— 明确说明：由于 YOLOv8 原仓库的许可证限制，代码不随仓库提供，只给出修改概述 `modifications.md`。

所以 `software/quantization/` 里你会看到 `README.md`（量化/编译/部署流程）和 `modifications.md`（QAT 改造细节），但没有可直接运行的 `.py`。README 把流程分成四步：量化（PTQ/QAT）→ 导出（`dump_xmodel`）→ 编译（`vai_c_xir`）→ 部署（`scp` + `prototxt`）。这部分由 u4 单元展开。

**（d）`framework/vitis_ai/`：一个补丁文件**

这个目录的核心只有一个文件：`xview3_yolov8_v3.5.patch`。README 解释了它的作用与用法：

[framework/vitis_ai/README.md:3-11](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md#L3-L11) —— 说明这是针对 Vitis AI 3.5 框架的补丁，用 `git apply` 贴到框架源码上，再交叉编译部署到 KV260 的 `/usr/local/`。

补丁改了四个方面：

[framework/vitis_ai/README.md:13-30](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md#L13-L30) —— (1) C++ 侧 `cv::imread` 加 `IMREAD_UNCHANGED`；(2) `image_preprocess` 里做 SAR 归一化并转成 **signed 8-bit**；(3) `applyNMS` 里加 PIoU2；(4) 优化 `yolov8_post_process` 以处理 P2 架构的新预测，把解码加速约 27 倍。

这条「signed 8-bit」线索很重要——它把训练侧（u3-l2 的归一化）和推理侧（u6 的补丁）串了起来，是后续理解「训推一致」的关键。

**（e）`software/inference_app/`：板载推理应用**

这是软件侧的终点：一个跑在 KV260 板子（ARM CPU）上的 C++ 程序。

[software/inference_app/README.md:4-10](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L4-L10) —— 说明该应用用 `sh build.sh` 编译，运行时需要一个 `test-image-list.txt`（每行一个 800×800 TIFF 路径）。

它提供两种功能：

[software/inference_app/README.md:13-25](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L13-L25) —— 一个是精度基准 `xview3_benchmark`（受 `PIOU2_NMS` 环境变量控制 IoU 度量），输出 `chip_id,label,x,y,w,h,score` 的 csv；另一个是吞吐测试 `xview3_performance`（受 `DEEPHI_PROFILING` 控制），输出 FPS 和分段耗时。

这两个可执行文件就是整条流水线在板子上的最终产物。

#### 4.2.4 代码实践

**实践目标**：通过阅读各 README，把「软件侧五处修改」归位到正确的目录，体会它们是如何分工的。

**操作步骤**：

1. 打开 [software/training/README.md:24-66](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L24-L66)，记下训练侧的五处修改。
2. 打开 [framework/vitis_ai/README.md:13-30](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md#L13-L30)，记下推理框架补丁的四方面修改。
3. 在一张表里对比：哪些修改在「训练侧」和「推理侧」都出现了（比如归一化、IoU）？为什么两边都要做？

**需要观察的现象**：

- 「图像加载（IMREAD_UNCHANGED）」「归一化（SAR 波段 + bathymetry）」「IoU（PIoU2）」这三件事，在训练侧（Python）和推理侧（C++）**都出现了**。
- 这不是重复劳动，而是为了保证**训练时和推理时对图像的处理完全一致**，否则模型精度会掉。

**预期结果**：你会得到一张类似下面的对照表（自己填）：

| 处理环节 | 训练侧（software/training/） | 推理侧（framework/vitis_ai/） |
| :--- | :--- | :--- |
| 读 TIFF | `cv2.IMREAD_UNCHANGED` | `cv::IMREAD_UNCHANGED` |
| 归一化 | `normalize()`（Python） | `image_preprocess`（C++） |
| IoU 度量 | `compute_piou`（PIoU2） | `applyNMS` 中的 `cal_piou2` |

**待本地验证**：本实践是纯阅读 + 整理，无需运行任何命令。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `software/quantization/` 里没有 Python 源码？

> **参考答案**：因为 YOLOv8 原仓库的许可证限制，作者不能直接分发其源码，所以只提供了 `README.md`（流程）和 `modifications.md`（修改概述）。读者需要自行在 Vitis AI 容器里克隆改过的 `ultralytics-vitis-ai` 仓库并按说明修改。

**练习 2**：`framework/vitis_ai/` 里的补丁需要被「贴」到哪里？

> **参考答案**：用 `git apply` 贴到 Vitis AI 3.5 框架源码上，再按 Vitis AI 文档用 PetaLinux 交叉编译，编译出的二进制部署到 KV260 的 `/usr/local/`。它不是独立可运行程序，而是推理框架的源码修改。

**练习 3**：训练侧和推理侧都把归一化做了一遍，这是冗余还是必要？为什么输入张量最后要转成 **signed** 8-bit（`CV_8S`）而不是常见的 unsigned 8-bit？

> **参考答案**：是必要的——为了保证训推一致。转成 signed 8-bit 是为了匹配量化模型 int8 输入的取值范围（量化的 int8 是有符号的），这一点会在 u4（量化）和 u6（框架补丁）里详细展开；本讲只需记住「推理输入是有符号 8-bit」这个事实。

---

### 4.3 平台与硬件组件

#### 4.3.1 概念说明

「平台侧」是让神经网络**真正在 FPGA 硬件上跑起来**的工程部分。它解决两类问题：

1. **底座问题**：怎么在 KV260 上搭出一个「带 DPU 的 Linux 系统」？——由 `platform/kv260/` 负责。
2. **加速问题**：YOLOv8 的后处理（解码边界框）很耗时，能不能用 FPGA 的 PL（可编程逻辑）做一个专用加速核？——由 `platform/post_processing/` 负责。

这两个子目录的根本区别，是**抽象层次不同**：

| 目录 | 抽象层次 | 产出物 | 工具链 |
| :--- | :--- | :--- | :--- |
| `platform/kv260/` | 整个硬件平台 + 操作系统镜像 + 加速器固件 | `.xsa`、PetaLinux 镜像、固件三件套 | Vivado + PetaLinux + XSCT/dtc |
| `platform/post_processing/` | 单个 PL 加速核（HLS）+ 它的 host 程序 | `.xo`、`xclbin`、host 可执行文件 | Vitis HLS + Vitis + CMake |

你可以这样类比：`platform/kv260/` 像是在「造一台装了 GPU 驱动的电脑」，而 `platform/post_processing/` 像是在「给这台电脑写一个利用 GPU 加速的小程序」。前者是地基，后者是地基上的一块砖。

#### 4.3.2 核心流程

`platform/kv260/` 的工作流分四个阶段（README 明确列出）：

```text
1. 硬件设计 (Vivado)     →  产出 .xsa（带 DPU 的硬件平台定义）
2. 软件构建 (PetaLinux)   →  产出 Linux 镜像（含 DPU 内核驱动）
3. 固件准备              →  产出三件套：bit.bin + .dtbo + shell.json
4. 部署与验证            →  scp 上板、xmutil loadapp、xdputil query 验证 DPU
```

`platform/post_processing/` 的工作流则是「Vitis 平台创建」标准流程的四个步骤：

```text
1. Vivado 平台硬件设计    →  产出 .xsa（KV260 平台，仓库已附 kv260_hardware_platform/）
2. Vitis 平台创建         →  产出设备树 overlay + shell 描述（dtg_output/）
3. 构建内核与应用         →  HLS 综合 .xo → 链接 xclbin → 编译 host
4. 部署与平台测试         →  复制 overlay 到板子、xmutil 加载、运行 host 验证
```

两者流程看起来很像（都是「硬件 → 平台 → 内核 → 部署」），但目标不同：`kv260/` 的内核是 **DPU**（神经网络通用加速器），`post_processing/` 的内核是 **YOLOv8 解码核**（一个专用的后处理加速器）。在最终的板子上，这两个内核可以**共存**于同一个 FPGA 设计里。

#### 4.3.3 源码精读

**（a）`platform/kv260/`：四个阶段的工作流**

README 开头的「Workflow Overview」把整个构建分成四步：

[platform/kv260/README.md:13-21](https://github.com/alan-turing-institute-sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L13-L21) —— 四个主阶段：硬件设计（Vivado）、软件构建（PetaLinux）、固件准备、部署与验证。

第 1 阶段的入口是一条批处理命令：

[platform/kv260/README.md:33-37](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L33-L37) —— 用 `vivado -mode batch -source main.tcl` 跑 TCL 脚本生成 DPU 设计；这条命令对应 `platform/kv260/hw/main.tcl`。

紧随其后是一张**FPGA 资源利用率表**，这是评估「这个设计还能不能再塞别的核」的关键依据：

[platform/kv260/README.md:43-63](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L43-L63) —— 列出 LUT（53.03%）、BRAM（75.69%）、URAM（62.5%）等资源占用。BRAM 已经用了 75.69%，说明剩余空间不算多——这正是后续 `post_processing/` 的 HLS 核能否塞进去要考虑的约束。

第 4 阶段验证时，`xdputil query` 输出的 JSON 揭示了 DPU 的真实身份：

[platform/kv260/README.md:253-263](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L253-L263) —— DPU 架构名是 `DPUCZDX8G_ISA1_B4096`，频率 325 MHz。这两个参数决定了量化编译（`vai_c_xir` 的 `arch.json`）必须与之匹配。

> 注意 `platform/kv260/sw/` 下已经**附带**了构建好的固件三件套（`project_1.bit.bin`、`kv260.dtbo`、`shell.json`），所以普通读者不必从零构建，可以直接拿来部署——这是仓库贴心的地方。

**（b）`platform/post_processing/`：HLS 核的目录结构**

README 先用一个「Repository Structure」小节解释了它内部六个子目录：

[platform/post_processing/README.md:8-16](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/README.md#L8-L16) —— `decode_common/`（共享头文件）、`decode_host/`（host 程序 + CMake）、`decode_krnl/`（HLS 内核源码 + testbench + 配置）、`decodeapp/`（高层打包 + 硬件链接）、`kv260_hardware_platform/`（附带的平台 .xsa/.bit）、`reports/`（HLS 报告）、`dtg_output/`（设备树 overlay + shell）。

随后 README 给出「平台创建与部署」的四步流程：

[platform/post_processing/README.md:17-34](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/README.md#L17-L34) —— 四步：Vivado 平台硬件设计 → Vitis 平台创建 → 构建内核与应用（综合 `.xo` → 链接 `xclbin` → 编译 host）→ 部署与平台测试。

这个解码核要解决的问题，README 开篇说得很清楚：

[platform/post_processing/README.md:4-5](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/README.md#L4-L5) —— 把 YOLOv8 头部的原始输出特征图变换成可解释的边界框坐标、置信度、类别概率，并在 FPGA 硬件上加速这一解码过程以支持实时检测。

换句话说，`software/inference_app/` 跑出来的预测，其「解码」这一步本来在 CPU 上做（见 `framework/vitis_ai/` 补丁里的 `yolov8_post_process` 优化），而 `platform/post_processing/` 提供了一个**把这个解码搬到 PL 硬件上**的方案。这是 u8 单元（最硬核的 HLS 内核）的全部主题。

#### 4.3.4 代码实践

**实践目标**：找出平台侧两个目录各自的「交接产物（handoff artifact）」，理解它们如何与软件侧衔接。

**操作步骤**：

1. 在 `platform/kv260/` 里，找到「固件三件套」——用 `git ls-files platform/kv260/sw/` 查看，确认 `.bit.bin`、`.dtbo`、`shell.json` 都在。
2. 在 `platform/post_processing/` 里，找到 HLS 核综合后会产出的内核对象文件——阅读 [platform/post_processing/README.md:8-16](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/README.md#L8-L16)，回答：哪个子目录放 HLS 内核源码？哪个放 host 程序？
3. 把软件侧和平台侧的衔接点写下来。

**需要观察的现象**：

- `platform/kv260/sw/` 下确实有三个文件：`project_1.bit.bin`（比特流）、`kv260.dtbo`（设备树 overlay）、`shell.json`（shell 元数据）。
- HLS 内核源码在 `decode_krnl/`，host 程序在 `decode_host/`。

**预期结果**：你会得到下面这张「衔接点」表：

| 平台产物 | 谁消费它 | 在哪里衔接 |
| :--- | :--- | :--- |
| `platform/kv260/` 的固件三件套 | KV260 板上的 `xmutil loadapp` | 板载 Linux 启动后加载 DPU |
| `platform/kv260/` 的 DPU 架构（`DPUCZDX8G_ISA1_B4096` / 325 MHz） | `software/quantization/` 的 `vai_c_xir -a arch.json` | 量化编译时必须匹配 |
| `platform/post_processing/` 的 `xclbin` | `software/inference_app/`（或独立 host） | 后处理解码加速 |

**待本地验证**：`git ls-files` 命令无害可直接运行；但固件加载、xclbin 运行需要真实 KV260 硬件，本讲不要求实跑。

#### 4.3.5 小练习与答案

**练习 1**：`platform/kv260/` 和 `platform/post_processing/` 各自的「内核」分别是什么？

> **参考答案**：`platform/kv260/` 的内核是 **DPU**（`DPUCZDX8G_ISA1_B4096`，神经网络通用加速器 IP）；`platform/post_processing/` 的内核是 **YOLOv8 边界框解码核**（一个用 Vitis HLS 写的专用后处理加速器）。

**练习 2**：为什么 README 里要附一张 FPGA 资源利用率表（LUT/BRAM/URAM）？

> **参考答案**：因为 KV260 的 FPGA 资源有限。这张表告诉你当前 DPU 设计已经占了多少资源（如 BRAM 75.69%、URAM 62.5%），从而判断还能不能再塞入额外的 PL 核（比如 `post_processing/` 的解码核）。这是硬件/软件协同设计的基本依据。

**练习 3**：`platform/post_processing/` 解决的是推理流水线里的哪一步？它和 `framework/vitis_ai/` 补丁里优化的 `yolov8_post_process` 是什么关系？

> **参考答案**：解决的是**后处理解码**这一步（把 DPU 输出的原始特征图解码成边界框）。`framework/vitis_ai/` 的 `yolov8_post_process` 是软件（CPU）实现并做了约 27 倍优化；`platform/post_processing/` 则提供一个**硬件（PL）实现**，把同一步骤搬到 FPGA 上进一步加速。两者是「软件后处理」与「硬件后处理」的关系，本讲只需建立这个对应，细节在 u6 / u8。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这张「端到端模块依赖关系图」。

**任务**：从 xView3 原始场景出发，追踪数据一直到板载推理结果，标出**经过的每一个顶层目录**，并为每个目录写一句话职责说明。

**建议步骤**：

1. 在仓库根目录运行 `git ls-files`，确认所有顶层目录。
2. 阅读每个目录的 `README.md` 第一段（通常一句话就概括了职责）。
3. 用文本（或手绘）画出类似 4.1.2 节那样的流水线图，但这次**自己写**每条箭头上的「产物」和每个方框里的「一句话职责」。
4. 在图上用不同颜色（或标注）区分：
   - 软件侧目录（`dataset/`、`software/*`、`framework/vitis_ai/`）
   - 平台侧目录（`platform/*`）
5. 额外挑战：在图上标出三个「跨目录衔接点」，并写出衔接的文件名。例如：
   - `software/training/` 的 `.pt` → `software/quantization/` 的输入
   - `software/quantization/` 编译出的 `.xmodel` → `software/inference_app/` 的模型
   - `platform/kv260/` 的 DPU 架构名 → `software/quantization/` 的 `arch.json`

**参考输出格式**（你可以照这个模板填）：

```text
[dataset/]
  职责：把 xView3 大场景裁成 800×800 芯片 + YOLO 标签
  产物：*.tif 芯片、*.txt 标签
        │ 产物: chips
        ▼
[software/training/]
  职责：基于改自 v8.2.91 的 Ultralytics 训练定制 YOLOv8
  产物：浮点 .pt
        │ 产物: .pt
        ▼
[software/quantization/]
  职责：PTQ/QAT 量化为 int8 并用 vai_c_xir 编译给 DPU
  产物：.xmodel + .prototxt
        │ 产物: .xmodel
        ▼
[software/inference_app/]  + [framework/vitis_ai/] 补丁
  职责：板载 C++ 推理，输出预测
  产物：JSON/csv 预测

支撑层（不顺序流转，而是「托底」）：
  [platform/kv260/]      职责：提供带 DPU 的硬件平台 + 固件
  [platform/post_processing/] 职责：PL 侧 HLS 解码核加速后处理
```

**预期结果**：完成这张图后，你应该能一眼看出「任何一个后续讲义（u2~u8）落在哪个目录、它的上下游是谁」。这就是本讲想要建立的「地图感」。

**待本地验证**：画图部分无需运行；若想核对目录清单，运行 `git ls-files` 即可。

## 6. 本讲小结

- 仓库按**工程阶段**切分为六大核心组件（`framework/vitis_ai/`、`platform/kv260/`、`platform/post_processing/`、`software/training/`、`software/quantization/`、`software/inference_app/`），外加 `dataset/` 数据准备和 `assets/` 配图。
- 数据流主线是：`dataset/` → `software/training/` → `software/quantization/` → `software/inference_app/`（+ `framework/vitis_ai/` 补丁），产物依次是 chips、`.pt`、`.xmodel`、板载预测。
- 软件侧（数据/训练/量化/推理）大多可在 CPU/GPU 上完成；平台侧（`platform/kv260/` 提供 DPU 底座、`platform/post_processing/` 提供 HLS 解码核）是「让它在 FPGA 上跑」的工程化部分。
- `dataset/` 的唯一入口脚本是 `generate_xview3.py`；`software/quantization/` 因许可证限制**没有源码**，只有 README + modifications.md。
- 训练侧（Python）和推理侧（C++ 补丁）**重复实现了** TIFF 加载、归一化、PIoU2，这是为了保证训推一致；推理输入还要转成 **signed 8-bit**。
- `platform/kv260/`（整个硬件平台，产出固件三件套）与 `platform/post_processing/`（单个 HLS 解码核，产出 xclbin）抽象层次不同，但最终在板上共存。

## 7. 下一步学习建议

有了这张地图，后续学习建议沿着数据流前进：

- **下一讲 [u1-l3](./u1-l3-end-to-end-pipeline.md)**：会把本讲的目录串成一张更细的「输入→工具/脚本→输出」流水线表，作为后续单元的导航。
- **如果想立刻深入数据**：进入 u2 单元，精读 [dataset/generate_xview3.py](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py)，看裁剪与标注转换到底怎么做。
- **如果想先理解模型**：跳到 u3 单元，读 [software/training/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md) 的五处框架修改。
- **如果对硬件感兴趣**：直接看 [platform/kv260/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md) 的四阶段工作流。

无论走哪条线，记得随时回到本讲的「目录职责表」对号入座——它就是这本学习手册的索引。
