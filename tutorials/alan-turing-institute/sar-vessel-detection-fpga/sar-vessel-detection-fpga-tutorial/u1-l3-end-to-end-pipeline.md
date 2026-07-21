# 端到端工作流总览

## 1. 本讲目标

上一讲（[u1-l2](./u1-l2-repo-structure.md)）给的是一张**空间地图**：哪个目录放什么、谁产出什么文件。但仓库结构是「静态」的——它没有告诉你**真正动手跑这个项目时，事情是按什么顺序发生的**。本讲补上这张「时间地图」：把六个组件串成一条完整的工程流水线，告诉你每一步的输入是什么、用什么命令跑、输出什么产物、产物又交给下一步的谁。

本讲学完后，你应该能够：

1. 说出端到端流水线的**七个阶段**及其先后顺序。
2. 为每个阶段填出「**输入 → 工具/脚本/命令 → 输出产物**」三要素，并说出产物文件如何流转到下一阶段。
3. 拿到任何一个后续讲义（u2～u9），都能立刻判断它**落在流水线的哪一段、上下游是谁**——本讲是整本手册的导航地图。

一句话定位：u1-l2 回答「**有什么**」，u1-l3 回答「**怎么串**」。

## 2. 前置知识

本讲默认你已经读过 [u1-l1](./u1-l1-project-background.md) 和 [u1-l2](./u1-l2-repo-structure.md)。这里只补充三个本讲要用、但在前两讲没展开的概念：

- **阶段（stage）vs 模块（module）**：模块是一个**目录**（静态的代码归属），阶段是一个**时间点**（动态的一次执行）。一个模块可能参与多个阶段（例如 `software/quantization/` 同时做「量化」和「编译」两个阶段），一个阶段也可能跨多个模块。本讲的主角是「阶段」。
- **产物（artifact）与交接（handoff）**：每个阶段都会输出一个文件给下一个阶段当输入。理解流水线的关键，就是盯住**每个阶段产出什么文件、这个文件又被下一阶段的哪条命令消费**。常见的产物有：chips（`*.tif`）、浮点权重（`*.pt`）、量化模型（`*.xmodel`）、编译后 DPU 模型（`*.xmodel` + `*.prototxt`）、固件三件套（`.bit.bin` / `.dtbo` / `shell.json`）、HLS 加速核（`xclbin`）、板载预测（JSON/csv）。
- **两条工具链**：本项目的流水线分属两套完全不同的工具链——**软件工具链**（普通 CPU/GPU 服务器，Python + `yolo` CLI + Vitis AI 量化/编译器，跑前 4 个阶段）和**硬件工具链**（Vivado + PetaLinux + Vitis HLS，跑硬件/固件相关阶段）。记住这条分界线，你就不会被「为什么突然要装 Vivado」绕晕。

本讲的「北极星」始终是根 README 的一句话目标：在 KV260 上**用不到 1 分钟分析一张约 7 亿像素的 SAR 图像，且功耗 <10W**。这条目标决定了为什么流水线里必须塞进「量化」「编译」「HLS 后处理」这些看似多余的阶段——它们都是为了让模型塞进 <10W 的 FPGA。

[README.md:7](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L7) —— 这里写明：在 KV260 上不到 1 分钟分析约 7 亿像素 SAR 图像、功耗 <10W，精度仅比 SOTA GPU 模型低约 2%/3%，计算效率却高约 50×/2500×。这就是整条流水线服务的终极指标。

## 3. 本讲源码地图

本讲顺着流水线读各阶段的「说明书」（README）和唯一一个有源码的入口脚本。注意：这些 README 的**行号就是流水线阶段的顺序**——README 本身就是按阶段来分节写的，这并非巧合。

| 文件 | 在流水线中的角色 | 本讲如何使用 |
| :--- | :--- | :--- |
| `README.md` | 给出整条流水线的终极目标（北极星） | 用 L7 锚定目标 |
| `dataset/README.md` | 阶段 1（数据切片）的说明书 | 看下载 + 切片命令 |
| `dataset/generate_xview3.py` | 阶段 1 唯一的可执行入口脚本 | 看它的 CLI 与切片算法 |
| `software/training/README.md` | 阶段 2（训练）的说明书 | 看 `yolo train` 命令 |
| `software/quantization/README.md` | 阶段 3（量化）+ 阶段 4（编译）的说明书 | 看 PTQ/QAT + `vai_c_xir` 命令 |
| `framework/vitis_ai/README.md` | 阶段 6 的前置（推理框架补丁）说明书 | 看补丁如何贴到框架 |
| `software/inference_app/README.md` | 阶段 6（板载推理）的说明书 | 看 `build.sh` + 两条运行命令 |
| `platform/post_processing/README.md` | 阶段 5/7（硬件/固件 + HLS 后处理）的说明书 | 看四步平台创建流程 |

## 4. 核心概念与源码讲解

在拆成三个最小模块之前，先把**七个阶段的总览**和**导航地图**一次性摆出来。这张表是本讲最重要的产出，请先通读一遍建立全局印象，后面 4.1～4.3 再逐段展开。

### 七个阶段总览 + 导航地图

| # | 阶段 | 所属单元 | 主目录 | 输入 | 工具/命令 | 输出产物 |
| :-: | :--- | :---: | :--- | :--- | :--- | :--- |
| 1 | 数据切片 | u2 | `dataset/` | xView3 原始场景 | `generate_xview3.py` | 800×800 chips + 标签 |
| 2 | 模型训练 | u3 | `software/training/` | chips | `yolo train` | 浮点权重 `*.pt` |
| 3 | 模型量化 | u4 | `software/quantization/` | `*.pt` | `yolo ... nndct_quant=True` | 量化 `*.xmodel`（浮点计算图） |
| 4 | DPU 编译 | u4 | `software/quantization/` | `*.xmodel` | `vai_c_xir -a arch.json` | DPU 可执行 `*.xmodel` + `*.prototxt` |
| 5 | 硬件/固件部署 | u5 / u8 | `platform/kv260/` | `*.xsa` | Vivado + PetaLinux + `xmutil` | 固件三件套（DPU 底座） |
| 6 | 板载推理 | u6 / u7 | `framework/vitis_ai/` + `software/inference_app/` | DPU 模型 + 补丁 | `build.sh` + `xview3_benchmark` | JSON/csv 预测 + FPS |
| 7 | HLS 后处理（可选） | u8 | `platform/post_processing/` | DPU 输出特征图 | HLS decode host | 加速解码后的边界框 |

读法提示：

- **阶段 1～4 是软件工具链**（CPU/GPU 服务器即可），产物一路从「原始场景」收敛成「DPU 可执行模型」。
- **阶段 5～7 是硬件工具链**（围绕 KV260），阶段 5 造「底座」（DPU 跑在上面），阶段 6 是「跑模型出预测」，阶段 7 是「用 PL 硬件进一步加速后处理」。
- **阶段 5 与阶段 7 共用 `platform/` 抽象**：前者管整个硬件平台，后者管一个 HLS 加速核；它们在最终的 FPGA 设计里**共存**。这条区分在 [u1-l2 的 4.3 节](./u1-l2-repo-structure.md)已建立，本讲不再重复。
- 表中的「所属单元」列就是**导航地图**：学完本讲后，你想深入任何一段，直接跳到对应单元即可。

下面按三个最小模块展开。

---

### 4.1 数据准备阶段

#### 4.1.1 概念说明

这是流水线的**入口阶段**，没有上游依赖。它要解决的问题是：xView3-SAR 的原始场景是一整张巨大的多通道 GeoTIFF（边长可达数千像素），而 YOLOv8 训练需要固定大小的输入（本项目用 800×800）。所以必须先把大场景**切片（chip）**成小图，并为每张小图生成独立的 YOLO 标签文件。

用「阶段三要素」描述就是：

- **输入**：xView3-SAR 解压后的原始场景（`data/` 下的 `VH_dB.tif` / `VV_dB.tif` / `bathymetry.tif` 三通道）+ `labels/` 下的 `.csv` 标注。
- **工具**：`dataset/generate_xview3.py`（本阶段**唯一**的可执行脚本）。
- **输出**：`images/<name>/*.tif`（800×800 三通道芯片）+ `labels/<name>/*.txt`（YOLO 标签）+ 两个坐标索引文件（`*_positive_coords.txt` / `*_negative_coords.txt`）。

#### 4.1.2 核心流程

阶段 1 的内部步骤如下（命令均来自 `dataset/README.md`）：

```text
1. 下载 xView3-SAR 数据集（aria2），得到一批 .tar.gz
2. 解压：for file in *.tar.gz; do tar xzvf ... ; done
        → 得到 data/ labels/ shoreline/ 三大类原始文件
3. 运行 generate_xview3.py 切片：
   python generate_xview3.py --labels <...> --source <...> --save_dir <...> --imgsz <800|640> --name <...>
        → 在 save_dir 下生成：
           images/<name>/*.tif        （800×800 三通道芯片）
           labels/<name>/*.txt        （YOLO 归一化标签）
           labels/<name>_kp/*.txt     （带关键点的变体标签）
           <name>_positive_coords.txt （正样本芯片的 scene/坐标）
           <name>_negative_coords.txt （负样本芯片的 scene/坐标）
```

两个值得注意的产物细节：

1. **三通道写进一个多波段 GeoTIFF**：切片不是出三张图，而是把 VV、VH、bathymetry 作为同一个 `.tif` 的第 1/2/3 波段写出去（见 4.1.3 源码）。
2. **坐标索引文件是后续「还原全局坐标」的关键**：正/负样本坐标文件记录了每张芯片来自哪个场景、起始行列号。这件事在阶段 2 的验证流程里会用到——把芯片内的局部预测加上 offset 还原成场景全局坐标（见 [u1-l2](./u1-l2-repo-structure.md) 提到的训练侧第 4 处修改）。这是贯穿阶段 1→2 的一个交接点。

#### 4.1.3 源码精读

先看 `dataset/README.md` 给出的下载与解压步骤，这是阶段 1 的最前面两步：

[dataset/README.md:3-6](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md#L3-L6) —— 说明用 `aria2` 从 xView3-SAR 竞赛页下载数据，再用一行 `for file in *.tar.gz; do tar xzvf ...; done` 循环解压并删除压缩包。

数据集的组织方式（阶段 1 的输入长什么样）由这棵目录树定义：

[dataset/README.md:8-35](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md#L8-L35) —— 把数据集分成 `data/`（SAR `.tiff` 场景）、`labels/`（`.csv` 标注）、`shoreline/`（`.npy` 海岸线），每类下再分 `training/validation/public`。

阶段 1 的**核心入口命令**就一句：

[dataset/README.md:36-39](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/README.md#L36-L39) —— 说明 YOLOv8 需要裁剪后的芯片和每张芯片独立的 `.txt` 标签，预处理命令是 `python generate_xview3.py --labels ... --source ... --save_dir ... --imgsz <800|640> --name ...`。

这条命令背后是 `generate_xview3.py`。它的 CLI 定义在文件末尾，决定了上面那条命令的所有参数：

[dataset/generate_xview3.py:261-270](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L261-L270) —— 用 `argparse` 定义五个参数：`--labels`、`--source`、`--save_dir`、`--imgsz`（默认 **800**）、`--name`，最后调用 `main(...)`。注意 `imgsz` 默认值就是 800，这正是阶段 6 推理输入「800×800」的来源——**阶段 1 的切片尺寸必须和阶段 6 的推理输入尺寸一致**，否则模型推理会出错。

切片的核心算法是「滑动窗口 + 网格坐标」，集中在 `main()` 里这几行：

[dataset/generate_xview3.py:84-91](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L84-L91) —— 用 `np.arange(0, size-imgsz+1, imgsz)` 生成不重叠的起始坐标，`np.meshgrid` 展开成二维网格，再用 `np.lib.stride_tricks.sliding_window_view` 一次性切出所有芯片视图。这就是「不重叠滑动窗口裁剪」的实现。

三通道被写进同一个多波段 GeoTIFF：

[dataset/generate_xview3.py:134-150](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L134-L150) —— 以 `count=3` 打开一个 GeoTIFF，分别把 `vv_crop`、`vh_crop`、`bathymetry_crop` 写到第 1/2/3 波段，`dtype` 沿用原始 SAR 的 `out_dtype`（int16）。注意这里**没有做归一化**——归一化推迟到阶段 2 训练时才做（这是阶段 1→2 的一个重要约定，见 4.2 节）。

最后，背景负样本按 30% 比例采样：

[dataset/generate_xview3.py:192-197](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L192-L197) —— 计算每个场景的背景芯片数 `n_scene_background = int(scene_chips * 0.3)`，再用 `random.sample` 从该场景所有空芯片里随机抽这么多作为负样本。采样比例 \( r = 0.3 \) 是为训练提供「没有目标」的负样本，提升模型对纯海面的鲁棒性。

#### 4.1.4 代码实践

**实践目标**：亲手把阶段 1 的「输入 → 工具 → 输出」三要素对应到真实代码行，并理解切片尺寸如何贯穿整条流水线。

**操作步骤**：

1. 打开 [dataset/generate_xview3.py:261-270](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L261-L270)，记下 `--imgsz` 的默认值（应为 800）。
2. 打开 [software/inference_app/README.md:10](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L10)，确认阶段 6 推理要求的输入尺寸也是 800×800。
3. 把阶段 1 填进本讲的「输入 → 工具/命令 → 输出」主表（见第 5 节综合实践）。

**需要观察的现象**：

- 阶段 1 的 `imgsz` 默认 800，阶段 6 的输入也写明 800×800——两者必须一致。
- 切片写出的 `.tif` 仍是 int16（未归一化），归一化不在本阶段做。

**预期结果**：你能在主表里写出阶段 1 这一行：「xView3 原始场景 → `generate_xview3.py` → 800×800 三通道 chips + YOLO 标签」。

**待本地验证**：真正运行 `generate_xview3.py` 需要先下载 xView3-SAR 数据集（体积很大），本讲不要求实跑；读懂 CLI 与切片算法即可。若想小范围验证，可构造一个小的 `.csv` + 少量场景测试，但这需要本地具备 `rasterio` 环境。

#### 4.1.5 小练习与答案

**练习 1**：阶段 1 的输出芯片里，VV、VH、bathymetry 是分别存成三张图，还是合在一张图里？

> **参考答案**：合在一张多波段 GeoTIFF 里——以 `count=3` 打开同一个文件，三个波段分别写入 VV/VH/bathymetry（见 [generate_xview3.py:134-150](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py#L134-L150)）。

**练习 2**：为什么阶段 1 写出的芯片**不归一化**，而要等到阶段 2 训练时才归一化？

> **参考答案**：因为芯片以 int16 原始值保存，保留了完整的 SAR 动态范围；归一化是训练框架（Ultralytics）数据加载时在 `get_image_and_label` 里做的，这样可以在训练侧灵活调整归一化参数而无需重新切数据。这也意味着**训练侧和推理侧必须用同一套归一化**——这就是 [u1-l2](./u1-l2-repo-structure.md) 提到的「训推一致」的开端。

**练习 3**：阶段 1 除了芯片和标签，还输出两个 `*_coords.txt` 文件，它们会被流水线的哪个阶段消费？

> **参考答案**：会被阶段 2 的验证流程消费——验证时需要用芯片的起始坐标（offset）把芯片内的局部预测还原成场景全局坐标，才能和 xView3 标注比对、计算指标（见训练侧第 4 处修改）。

---

### 4.2 模型训练与量化阶段

#### 4.2.1 概念说明

这个最小模块横跨**三个阶段（2 训练、3 量化、4 编译）**，它们都属于软件工具链，共同完成一件事：把一张浮点神经网络，逐步变成 DPU 硬件能直接执行的 int8 模型。这条「浮点 → int8 → DPU 可执行」的收敛链，是整条流水线最长、也最需要工具链配合的一段。

三个阶段的三要素：

| 阶段 | 输入 | 工具/命令 | 输出 |
| :-: | :--- | :--- | :--- |
| 2 训练 | 阶段 1 的 chips | `yolo train`（Ultralytics CLI） | 浮点权重 `*.pt` |
| 3 量化 | `*.pt` | `yolo ... nndct_quant=True`（PTQ 校准 或 QAT） | 量化模型 `*.xmodel`（仍是浮点计算图描述） |
| 4 编译 | 量化 `*.xmodel` | `vai_c_xir -a arch.json` | DPU 可执行 `*.xmodel` + `meta.json` + `*.prototxt` |

一个关键区分（初学者常混）：阶段 3 和阶段 4 都产出叫 `.xmodel` 的文件，但它们**不是同一个东西**。阶段 3 的 `.xmodel` 是 Vitis AI 量化器导出的「带量化信息的计算图」（`nndct_quant/DetectionModel_0_int.xmodel`），还不能在 DPU 上跑；阶段 4 用 `vai_c_xir` 把它**编译**成与具体 DPU 架构绑定的指令（`vai_c_output/` 下的 `.xmodel`），这才是板子上能加载的模型。阶段 3→4 的交接文件就是那个量化后的 `.xmodel`。

#### 4.2.2 核心流程

阶段 2～4 的命令链（均来自对应 README）：

```text
[阶段 2 训练]  在 software/training/ 下：
  yolo train model="<...>.yaml" data="<...>.yaml" imgsz=<800|640> ...
      → 产物：浮点权重 best.pt

[阶段 3 量化]  在 software/quantization/ 下（Vitis AI 容器内）：
  方式 A（PTQ，快）：yolo detect val ... nndct_quant=True quant_mode=calib ...
  方式 B（QAT，精度更高）：yolo detect train ... nndct_quant=True epochs=100 ...
      → 产物：量化后的 .pt
  再导出：yolo detect val ... quant_mode=test dump_xmodel=True dump_onnx=True
      → 产物：nndct_quant/DetectionModel_0_int.xmodel（+ .onnx）

[阶段 4 编译]  仍在 software/quantization/ 下：
  vai_c_xir -x nndct_quant/DetectionModel_0_int.xmodel \
            -a .../DPUCZDX8G/KV260/arch.json \
            -o vai_c_output -n my_yolov8_model
      → 产物：vai_c_output/（含编译后 .xmodel + meta.json + checksum）
  再配 model.prototxt（用 xir subgraph 找输出层名填 detect_layer_name）
      → 产物：model.prototxt（与 .xmodel 同名）
```

三个贯穿性的细节：

1. **归一化的一致性线**：阶段 2 在训练侧用 `normalize()` 把 SAR 三通道线性压到 uint8（见 4.2.3）；阶段 6 推理侧的 C++ 补丁用 `image_preprocess` 做同样的归一化，再转成 **signed 8-bit**（`CV_8S`）。这条线把阶段 1（原始 int16）→ 阶段 2（归一化 uint8）→ 阶段 6（归一化 + signed int8）串起来，是「训推一致」的核心。
2. **激活函数替换标志**：阶段 3 的所有命令都带 `--nndct_convert_sigmoid_to_hsigmoid --nndct_convert_silu_to_hswish`，这是为了让激活函数对 DPU 的 int8 定点实现更友好（原理在 u4 单元展开）。
3. **`arch.json` 是阶段 4 与阶段 5 的交接点**：阶段 4 编译时用的 `-a arch.json` 必须和阶段 5 板子上 DPU 的真实架构（`DPUCZDX8G_ISA1_B4096` / 325 MHz）匹配，否则编译出的模型在板子上加载不了。这就是为什么阶段 5 的硬件信息会「反向」影响阶段 4 的编译命令。

#### 4.2.3 源码精读

**阶段 2 的训练命令**来自 `software/training/README.md` 的 Usage 一节：

[software/training/README.md:71-78](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L71-L78) —— 给出 `yolo train / yolo val / yolo predict` 三条 CLI，配置以 `.yaml` 存放在 `ultralytics/cfg/`；`imgsz` 取 800 或 640。这就是阶段 2 的入口命令，产物是浮点 `*.pt`。

阶段 2 里和阶段 1/6 紧密相关的，是那处 SAR 归一化修改：

[software/training/README.md:30-35](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L30-L35) —— `normalize()` 把 bathymetry/SAR 三通道分别按 `[-6000,2000]` / `[-50,20]` 线性归一化，clip 到 `[0,1]` 再映射到 uint8。注意 `min_values = [-6000, -50, -50]` 的顺序对应 bathymetry/VV/VH 三个波段——这条归一化逻辑稍后会在阶段 6 的 C++ 补丁里被「翻译」一遍。

**阶段 3 的量化命令**来自 `software/quantization/README.md`，分 PTQ 与 QAT 两条：

[software/quantization/README.md:39-41](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L39-L41) —— PTQ 校准命令：`yolo detect val ... nndct_quant=True quant_mode=calib imgsz=800 --nndct_convert_sigmoid_to_hsigmoid --nndct_convert_silu_to_hswish`。校准只需前向推理，速度快。

[software/quantization/README.md:47-49](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L47-L49) —— QAT 命令：`yolo detect train ... nndct_quant=True epochs=100 optimizer=SGD momentum=0.9 lr0=0.005 warmup_epochs=0 ...`。QAT 需要完整训练循环，精度通常更高。

阶段 3 把量化模型导出为阶段 4 要用的 `.xmodel`：

[software/quantization/README.md:56-60](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L56-L60) —— 用 `dump_xmodel=True dump_onnx=True` 把量化模型同时导出为 `.xmodel`（给阶段 4 编译）和 `.onnx`（给其他工具链兼容）。这就是阶段 3→4 的交接文件。

**阶段 4 的编译命令**是软件工具链的终点：

[software/quantization/README.md:74-83](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L74-L83) —— `vai_c_xir -x .../DetectionModel_0_int.xmodel -a .../KV260/arch.json -o vai_c_output -n my_yolov8_model`，产出 `vai_c_output/` 目录（编译后 `.xmodel` + `meta.json` + checksum）。注意 `-a arch.json` 把模型绑定到 KV260 的 DPU 架构。

编译后还要配一个 `model.prototxt`，其中 `detect_layer_name` 必须从量化模型的 DPU 子图输出里取：

[software/quantization/README.md:91-99](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L91-L99) —— 用 `xir subgraph yolov8.xmodel | grep DPU` 找到输出张量名（标 `O` 的），填进 `model.prototxt` 的 `detect_layer_name`。这一步把「模型结构」和「后处理配置」对上号，是阶段 4→6 的隐性交接。

最后一步是把编译好的模型 `scp` 到板子，正式进入硬件工具链：

[software/quantization/README.md:101-105](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L101-L105) —— `scp -r model/ <target>:~/usr/share/vitis_ai_library/models/`。阶段 6 的推理应用就从这个目录按「模型名」加载它。

#### 4.2.4 代码实践

**实践目标**：把阶段 2～4 的命令链整理成一张「输入 → 命令 → 输出」表，并定位「同一个名字、不同含义」的 `.xmodel` 在哪两个阶段之间交接。

**操作步骤**：

1. 打开 [software/quantization/README.md:56-83](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L56-L83)，分别记下「导出」和「编译」两条命令的输入文件名和输出目录。
2. 在一张表里对照：阶段 3 导出的 `.xmodel`（`nndct_quant/DetectionModel_0_int.xmodel`）和阶段 4 编译出的 `.xmodel`（`vai_c_output/` 下）有何不同？
3. 圈出阶段 4 命令里 `-a arch.json` 这个参数，标注：「这里依赖阶段 5 板子上 DPU 的真实架构」。

**需要观察的现象**：

- 阶段 3 的 `.xmodel` 路径以 `nndct_quant/` 开头；阶段 4 的输出目录是 `vai_c_output/`——两个 `.xmodel` 不会混。
- `-a arch.json` 是软件工具链里唯一「反向依赖硬件信息」的参数。

**预期结果**：你能写出类似下面的对照（填进第 5 节主表）：

| 阶段 | 命令 | 关键输入 | 关键输出 |
| :-: | :--- | :--- | :--- |
| 3 量化导出 | `yolo ... dump_xmodel=True` | 量化后的 `.pt` | `nndct_quant/DetectionModel_0_int.xmodel` |
| 4 DPU 编译 | `vai_c_xir -x ... -a arch.json` | 上一步的 `.xmodel` | `vai_c_output/*.xmodel` + `meta.json` |

**待本地验证**：训练与量化都需要 GPU/CPU 服务器与 Vitis AI Docker 环境，本讲不要求实跑；重点是理清命令链与交接文件。

#### 4.2.5 小练习与答案

**练习 1**：阶段 3 和阶段 4 都产出 `.xmodel`，它们能互换吗？为什么？

> **参考答案**：不能。阶段 3 的 `.xmodel` 是量化器导出的「带量化信息的计算图」，与硬件无关；阶段 4 的 `.xmodel` 是用 `vai_c_xir` 针对特定 DPU 架构（`arch.json`）编译出的「DPU 指令」。板子上 `inference_app` 加载的是后者。

**练习 2**：阶段 2 训练侧的 `normalize()` 把 SAR 归一化到 uint8；阶段 6 推理侧的 C++ 归一化最后却转成 **signed 8-bit**（`CV_8S`）。为什么推理侧要多一步「转有符号」？

> **参考答案**：因为量化模型（阶段 3 产出）的 int8 输入是有符号的，取值范围是 \([-128,127]\) 而非 \([0,255]\)。推理输入必须匹配量化时的数值约定，否则精度会崩。这条线索把阶段 2（归一化）、阶段 3（int8 量化）、阶段 6（signed int8 输入）三段串成一条「数值一致性」暗线。

**练习 3**：阶段 4 的 `vai_c_xir` 命令里 `-a arch.json` 指向 `DPUCZDX8G/KV260/arch.json`。如果将来换一块 DPU 架构不同的板子，流水线哪一步必须改？

> **参考答案**：阶段 4（重新编译）必须改 `-a` 指向新架构的 `arch.json`，而且阶段 5（硬件/固件）也要换成承载新 DPU 的设计。这正是阶段 4 与阶段 5 通过「DPU 架构名」耦合的体现。

---

### 4.3 硬件部署与推理阶段

#### 4.3.1 概念说明

这个最小模块横跨**三个阶段（5 硬件/固件部署、6 板载推理、7 HLS 后处理）**，都属于硬件工具链，围绕 KV260 板子展开。它们要解决的问题是：让阶段 4 编译出的 DPU 模型，在一块真实的 FPGA 板上跑起来并产出预测。

三要素：

| 阶段 | 输入 | 工具/命令 | 输出 |
| :-: | :--- | :--- | :--- |
| 5 硬件/固件部署 | `*.xsa`（Vivado 硬件设计） | Vivado + PetaLinux + `xmutil` | 固件三件套（DPU 跑在上面） |
| 6 板载推理 | DPU 模型 + 补丁 + `inference_app` | `build.sh` + `xview3_benchmark` / `xview3_performance` | JSON/csv 预测 + FPS |
| 7 HLS 后处理（可选） | DPU 输出特征图 | Vitis HLS decode 核 + host | 加速解码后的边界框 |

阶段 5 是「造底座」，阶段 6 是「在底座上跑模型」，阶段 7 是「用 PL 硬件把后处理再加速一遍」。阶段 5 的产物（固件三件套）让 DPU 可用；阶段 6 的产物（预测 + FPS）是整条流水线的**终点**；阶段 7 是可选的性能优化。

#### 4.3.2 核心流程

阶段 5～7 的命令链：

```text
[阶段 5 硬件/固件部署]  在 platform/kv260/ 下（仓库已附构建好的固件，可直接用）：
  Vivado:  vivado -mode batch -source main.tcl        → .xsa
  PetaLinux: 构建 Linux 镜像（含 DPU 内核驱动）
  固件三件套: project_1.bit.bin + kv260.dtbo + shell.json
  上板: scp 三件套 → xmutil loadapp → xdputil query 验证 DPU
      → 产物：板子上可用的 DPU 运行时

[阶段 6 板载推理]  在 software/inference_app/ 下（先贴 framework/vitis_ai/ 补丁）：
  贴补丁: git apply ../framework/vitis_ai/xview3_yolov8_v3.5.patch（到 Vitis AI 源码）
  编译:   sh build.sh
  跑精度: PIOU2_NMS=<0|1> ./xview3_benchmark <model> <list.txt> <out.txt> -t <N>
  跑吞吐: DEEPHI_PROFILING=<0|1> ./xview3_performance <model> <list.txt> -t <N>
      → 产物：chip_id,label,x,y,w,h,score 的 csv 预测 + FPS + 分段耗时

[阶段 7 HLS 后处理（可选）]  在 platform/post_processing/ 下：
  Vitis HLS 综合 decode_krnl → .xo → 链接 xclbin → 编译 host
  上板运行 decodeapp，把 DPU 输出特征图交给 PL 解码核
      → 产物：硬件加速后的边界框解码
```

三个贯穿性细节：

1. **补丁必须先于推理应用编译**：阶段 6 的 `inference_app` 依赖被补丁修改过的 Vitis AI 库。所以执行顺序是「贴补丁 → 交叉编译 Vitis AI → `build.sh` 编译推理应用」，补丁不能漏。
2. **`PIOU2_NMS` 环境变量是训推一致的运行时开关**：阶段 2 训练用 PIoU2 做 IoU，阶段 6 推理 NMS 也应一致——通过 `PIOU2_NMS=1` 在运行时切到 PIoU2，否则用默认 CIoU。这是「训推一致」在阶段 6 的体现。
3. **阶段 6 与阶段 7 是「软/硬后处理」的关系**：阶段 6 的后处理解码在 CPU 上做（`framework/vitis_ai/` 补丁已把它加速约 27 倍）；阶段 7 把同一步骤搬到 PL 硬件上进一步加速。两者解决同一个问题，层次不同。

#### 4.3.3 源码精读

**阶段 6 的前置——推理框架补丁**：补丁要先贴到 Vitis AI 3.5 源码再交叉编译：

[framework/vitis_ai/README.md:5-11](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md#L5-L11) —— 用 `git apply ../framework/vitis_ai/xview3_yolov8_v3.5.patch` 把补丁贴到 Vitis AI 源码，再按文档用 PetaLinux 交叉编译，二进制部署到 KV260 的 `/usr/local/`。这是阶段 6 编译 `inference_app` 之前的必做步骤。

补丁改的四方面（其中归一化与阶段 2 一一对应）：

[framework/vitis_ai/README.md:13-30](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md#L13-L30) —— (1) `cv::imread` 加 `IMREAD_UNCHANGED` 读 TIFF；(2) `image_preprocess` 做 SAR 归一化并转 **signed 8-bit**（`CV_8S`）；(3) `applyNMS` 加 PIoU2；(4) 优化 `yolov8_post_process` 处理 P2 架构新预测，解码加速约 27 倍。注意第 (2) 条就是把阶段 2 的 `normalize()` 「翻译」到 C++，并多出 signed 8-bit 这一步。

**阶段 6 的推理应用**入口与运行：

[software/inference_app/README.md:6-10](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L6-L10) —— 用 `sh build.sh` 编译；运行时需要一个 `test-image-list.txt`（每行一个 800×800 TIFF 路径）。注意输入尺寸 800×800 与阶段 1 切片尺寸一致。

阶段 6 提供两种功能，分别对应「精度」和「吞吐」两条终点产出：

[software/inference_app/README.md:14-18](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L14-L18) —— 精度基准 `xview3_benchmark`：`PIOU2_NMS=<0|1> ./xview3_benchmark <model> <list.txt> <out.txt> -t <N>`，输出 `chip_id,label,x,y,w,h,score` 的 csv。`PIOU2_NMS=1` 用 PIoU2，否则用默认 CIoU。

[software/inference_app/README.md:20-25](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L20-L25) —— 吞吐测试 `xview3_performance`：`DEEPHI_PROFILING=<0|1> ./xview3_performance <model> <list.txt> -t <N>`，输出 FPS 与平均推理时间；`DEEPHI_PROFILING=1` 还会拆出预处理/DPU/后处理三段耗时。这条产出直接对应第 2 节的「北极星」指标（FPS / <1 分钟 / <10W）。

**阶段 5 与阶段 7 的硬件流程**统一描述在 `platform/post_processing/README.md` 的四步平台创建流程里：

[platform/post_processing/README.md:17-34](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/README.md#L17-L34) —— 四步：Vivado 平台硬件设计（出 `.xsa`）→ Vitis 平台创建（出设备树 overlay + shell）→ 构建内核与应用（HLS 综合 `.xo` → 链接 `xclbin` → 编译 host）→ 部署与平台测试（`xmutil` 加载、板载运行验证）。阶段 5 对应其中的「平台硬件 + 固件」部分，阶段 7 对应「HLS 内核 + host」部分。

阶段 7 这个 HLS 核要解决的问题，README 开篇一句话点明：

[platform/post_processing/README.md:4-5](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/README.md#L4-L5) —— 把 YOLOv8 头部的原始输出特征图变换成边界框坐标、置信度、类别概率，并在 FPGA 硬件上加速这一解码。这正是阶段 6 软件后处理（`yolov8_post_process`）的硬件版。

#### 4.3.4 代码实践

**实践目标**：把阶段 5～7 的「输入 → 命令 → 输出」补进主表，并梳理出阶段 6 运行前必须完成的两个前置条件。

**操作步骤**：

1. 打开 [software/inference_app/README.md:14-25](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L14-L25)，记下 `xview3_benchmark` 和 `xview3_performance` 两条命令及其环境变量。
2. 回顾阶段 4 的 `scp` 命令（[quantization README L101-105](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L101-L105)）和阶段 6 的补丁命令（[vitis_ai README L7-11](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md#L7-L11)），列出阶段 6 运行前的两个前置。
3. 在主表里填阶段 5/6/7 三行。

**需要观察的现象**：

- 阶段 6 运行前的两个前置是：① 阶段 4 编译的模型已 `scp` 到板子；② `framework/vitis_ai/` 补丁已贴到 Vitis AI 源码并交叉编译。
- `xview3_benchmark` 的输出格式 `chip_id,label,x,y,w,h,score` 与阶段 2 验证用的标注字段（`detect_scene_row/column`、`is_vessel`、`is_fishing`）需要通过 chip offset 还原后才能比对——这又回到阶段 1 的坐标索引文件。

**预期结果**：你能写出阶段 6 这一行：「DPU 模型 + 推理框架补丁 + `inference_app` → `build.sh` + `xview3_benchmark` → `chip_id,label,x,y,w,h,score` 的 csv 预测」。

**待本地验证**：阶段 5～7 的命令都需要真实 KV260 硬件与 Xilinx 工具链（Vivado/PetaLinux/Vitis 2023.1），本讲不要求实跑；重点是理清运行顺序与环境变量含义。普通读者可直接用仓库已附带的固件三件套（见 [u1-l2 4.3 节](./u1-l2-repo-structure.md)）跳过阶段 5 的从零构建。

#### 4.3.5 小练习与答案

**练习 1**：阶段 6 运行 `xview3_benchmark` 之前，流水线必须先完成哪两件事？

> **参考答案**：① 阶段 4 用 `vai_c_xir` 编译出的 DPU 模型已 `scp` 到板子的 `/usr/share/vitis_ai_library/models/`；② `framework/vitis_ai/` 补丁已贴到 Vitis AI 源码并交叉编译，使得 `inference_app` 能链接到正确的库。

**练习 2**：`PIOU2_NMS=1` 这个环境变量解决的是流水线里的什么一致性问题？

> **参考答案**：解决「训推一致的 IoU 度量」。阶段 2 训练用 PIoU2 做边界框回归，阶段 6 推理做 NMS 时也应使用 PIoU2 而非默认 CIoU，否则度量不一致会损害精度。`PIOU2_NMS=1` 就是在运行时把推理 NMS 切到 PIoU2。

**练习 3**：阶段 7 的 HLS 后处理核和阶段 6 软件后处理（`yolov8_post_process`）做的是同一件事吗？为什么要在硬件上再做一遍？

> **参考答案**：是同一件事——都是把 DPU 输出的原始特征图解码成边界框。阶段 6 是 CPU 实现（补丁已优化约 27 倍），阶段 7 把它搬到 FPGA 的 PL 上做专用硬件加速，进一步降低后处理耗时，从而提升整体 FPS、逼近 <10W 下的实时性目标。

---

## 5. 综合实践

本讲的综合实践就是本讲的核心产出——**流水线主表**：用一张表列出每个阶段的「输入 → 工具/脚本/命令 → 输出」，并在表后补上「跨阶段交接点」清单。这张表就是你后续学 u2～u9 时的导航地图，建议自己亲手填一遍。

**任务**：把第 4 节开头的「七阶段总览」扩充成一张完整的「输入 → 工具/命令 → 输出」主表，并额外列出流水线里 5 个关键的跨阶段交接点。

**建议步骤**：

1. 仿照下面的模板，逐行填入七个阶段的「输入 / 命令 / 输出」。命令要尽量抄 README 里的原文（带关键参数），不要凭记忆。
2. 在表下方列出 5 个跨阶段交接点（每个交接点写明：上游阶段产物 → 下游阶段命令/消费方）。
3. 用三种颜色（或标注）区分：①软件工具链阶段（1～4）；②硬件工具链阶段（5～7）；③贯穿全链的「一致性暗线」（归一化 / PIoU2 / imgsz）。

**参考输出（主表模板）**：

| # | 阶段 | 工具链 | 输入 | 工具/命令 | 输出产物 |
| :-: | :--- | :---: | :--- | :--- | :--- |
| 1 | 数据切片 | 软件 | xView3 原始场景 | `generate_xview3.py --imgsz 800 ...` | 800×800 chips + 标签 + 坐标索引 |
| 2 | 模型训练 | 软件 | chips | `yolo train ... imgsz=800` | 浮点 `*.pt` |
| 3 | 模型量化 | 软件 | `*.pt` | `yolo ... nndct_quant=True`（PTQ/QAT）→ `dump_xmodel=True` | `nndct_quant/*_int.xmodel` |
| 4 | DPU 编译 | 软件 | 量化 `.xmodel` | `vai_c_xir -x ... -a arch.json -o vai_c_output` | `vai_c_output/*.xmodel` + `meta.json` + `*.prototxt` |
| 5 | 硬件/固件部署 | 硬件 | `*.xsa` | Vivado + PetaLinux + `xmutil loadapp` | 固件三件套（DPU 运行时） |
| 6 | 板载推理 | 硬件 | DPU 模型 + 补丁 | `git apply` 补丁 → `build.sh` → `xview3_benchmark` | `chip_id,label,x,y,w,h,score` csv + FPS |
| 7 | HLS 后处理 | 硬件 | DPU 特征图 | HLS `.xo` → `xclbin` → decode host | 硬件加速解码的边界框 |

**参考输出（5 个跨阶段交接点）**：

1. **阶段 1 → 阶段 2**：切片尺寸 `imgsz=800` 必须与训练/推理输入一致；芯片坐标索引文件供阶段 2 验证时还原全局坐标。
2. **阶段 2 → 阶段 3**：浮点 `*.pt` 是量化器的唯一输入。
3. **阶段 3 → 阶段 4**：量化导出的 `nndct_quant/DetectionModel_0_int.xmodel` 是 `vai_c_xir` 的输入（同名 `.xmodel`，含义不同）。
4. **阶段 4 ↔ 阶段 5**：`vai_c_xir -a arch.json` 反向依赖板子上 DPU 的真实架构（`DPUCZDX8G_ISA1_B4096` / 325 MHz）；编译产物 `scp` 到板子后被阶段 6 加载。
5. **阶段 1/2 → 阶段 6（一致性暗线）**：归一化（uint8 → signed int8）、PIoU2（`PIOU2_NMS=1`）、`imgsz=800` 三条一致性贯穿整条链，任何一处不一致都会掉精度。

**预期结果**：完成这张主表 + 交接点清单后，你应该能回答两个问题——(a)「拿到一个 `.pt`，要到哪一步才能在板子上跑出预测？」(b)「为什么训练和推理都要实现一遍归一化和 PIoU2？」能答出，说明你已经建立了整条流水线的「位置感」。

**待本地验证**：本实践是纯整理型，无需运行任何命令；若想核对某条命令的原文，直接点开本讲给出的永久链接即可。

## 6. 本讲小结

- 本讲把六个组件串成**七个阶段的端到端流水线**：数据切片 → 模型训练 → 模型量化 → DPU 编译 → 硬件/固件部署 → 板载推理 → HLS 后处理。
- 阶段 1～4 属**软件工具链**（CPU/GPU 服务器，Python + `yolo` CLI + Vitis AI），产物一路从原始场景收敛成 DPU 可执行模型；阶段 5～7 属**硬件工具链**（Vivado/PetaLinux/Vitis HLS，围绕 KV260）。
- 每个阶段都有清晰的「**输入 → 工具/命令 → 输出**」三要素；阶段间靠**产物交接**衔接（chips → `.pt` → 量化 `.xmodel` → 编译 `.xmodel` + `.prototxt` → 板载预测）。
- 三个**跨阶段一致性暗线**贯穿全链：归一化（训练 uint8 / 推理 signed int8）、IoU（PIoU2，`PIOU2_NMS=1`）、输入尺寸（`imgsz=800`）——任何一处不一致都会掉精度。
- 阶段 4（`vai_c_xir -a arch.json`）与阶段 5（DPU 真实架构）通过 **`arch.json` 反向耦合**，是软件工具链里唯一依赖硬件信息的地方。
- 本讲的「七阶段总览 + 导航地图」表是后续 u2～u9 的索引：想深入任何一段，直接跳到对应单元。

## 7. 下一步学习建议

有了这张时间地图，后续学习建议沿流水线**从前往后**推进，每篇讲义都对应一个或几个阶段：

- **数据（阶段 1）→ [u2 单元](./u2-l1-dataset-structure.md)**：精读 [dataset/generate_xview3.py](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/dataset/generate_xview3.py)，看滑动窗口切片、标注转换、30% 负样本采样的真实实现。
- **训练（阶段 2）→ [u3 单元](./u3-l1-yolov8-overview.md)**：读 [software/training/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md) 的五处框架修改（加载/归一化/PIoU2/验证/指标）。
- **量化与编译（阶段 3～4）→ [u4 单元](./u4-l1-quantization-ptq.md)**：读 [software/quantization/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md)，理解 PTQ/QAT 与 `vai_c_xir` 编译。
- **硬件/固件（阶段 5）→ [u5 单元](./u5-l1-kv260-dpu-architecture.md)**：读 [platform/kv260/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md) 的四阶段工作流。
- **框架补丁 + 板载推理（阶段 6）→ [u6](./u6-l1-patch-overview.md)、[u7](./u7-l1-inference-build.md) 单元**：读 [framework/vitis_ai/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md) 与 [software/inference_app/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md)。
- **HLS 后处理（阶段 7）→ [u8 单元](./u8-l1-hls-interface.md)**：读 [platform/post_processing/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/README.md) 与解码核源码。
- **端到端串联与权衡 → [u9 单元](./u9-l1-end-to-end-integration.md)**：把前八个单元串起来，讨论性能-精度-功耗取舍。

无论走哪条线，记得随时回到本讲的「七阶段总览表」对号入座——它就是整本学习手册的时间索引。
