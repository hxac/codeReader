# 端到端部署串联与调优

## 1. 本讲目标

前面八个单元，我们分别走完了数据切片、YOLOv8 训练、Vitis AI 量化、DPU 编译、KV260 硬件/固件、Vitis AI 框架补丁、板载推理应用、HLS 后处理解码核。每个单元都像一块拼图，本讲的任务是**把所有拼图拼成一张能跑通的完整图**。

学完本讲你应该能够：

- 说出从一份训练好的 `.pt` 出发，到 KV260 板上打印出 FPS 与 JSON 预测，**每一步的输入、命令、产物**是什么。
- 说出板载运行时，**KV260 固件、Vitis AI 框架库、模型目录、HLS xclbin** 这几样东西**该按什么顺序加载**，以及为什么是这个顺序。
- 当端到端跑不通或精度异常时，能定位到「输出层名」「signed int8 归一化」「xmutil 加载」「PIOU2_NMS」等常见失败点，并知道该查哪里。

本讲几乎不引入新的源码文件，而是**复用**前八单元读过的四份 README，把它们重新按「产物链」和「加载顺序」串起来。如果你是跳读进来的读者，建议至少先看 [u1-l3 端到端工作流总览](u1-l3-end-to-end-pipeline.md) 与 [u4-l4 模型导出与 DPU 编译](u4-l4-export-and-compile.md)。

## 2. 前置知识

在开始前，用三段话回顾贯穿全项目的关键概念，它们在本讲会反复出现。

**产物（artifact）与交接（handoff）。** 流水线每个阶段的输出，不只是「一个文件」，而是带着一组**隐式契约**的文件：它假设了输入图像是 800×800、类别数是 3、激活函数已被替换、输出张量叫某个名字。任何一个契约在下一阶段被违背，结果就会从「精度差一点」退化到「完全报错」。本讲的核心就是把这条契约链显式化。

**模型三形态。** 训练产出的是浮点 `.pt`（形态①）；量化后导出的是硬件无关的 Xilinx IR `xmodel`（形态②）；再用 `vai_c_xir` 按 `arch.json` 编译出针对 KV260 DPU 的指令包 `xmodel`（形态③）。②和③**同名却异构**，是最容易混淆的一环（详见 [u4-l4](u4-l4-export-and-compile.md)）。

**三条一致性暗线。** 这是 u1-l3 给出的全链路红线，本讲把它们作为「调优检查表」的主轴：

1. **归一化**：训练侧压成 `uint8 [0,255]`，推理侧映射成 `signed int8 [-128,127]`，二者只差一个 −128 平移，真正的契约是共同的 `[0,1]` 线性映射。
2. **IoU 度量**：训练用 PIoU2 损失，推理 NMS 也用 PIoU2，靠环境变量 `PIOU2_NMS=1` 打开。
3. **输入尺寸**：`imgsz=800` 必须在切片、训练、量化、导出、推理全程逐字一致。

> 名词速查：DPU（Deep learning Processing Unit，Xilinx 的 int8 定点神经网络加速 IP）、PL（Programmable Logic，FPGA 可编程逻辑）、PS（Processing System，KV260 上的 ARM A53）、xclbin（Xilinx 编译出的、可加载到 PL 的容器，含比特流与内核）、prototxt（Vitis AI 的模型后处理配置文件）。

## 3. 本讲源码地图

本讲引用的文件都是前序单元读过、且确实存在的 README。它们分别对应流水线的不同阶段：

| 文件 | 对应阶段 | 在本讲的作用 |
| :--- | :--- | :--- |
| `README.md` | 全局 | 仓库六大组件总图，确认产物归属哪个目录 |
| `software/quantization/README.md` | 量化 + 编译 + 导出 | 提供从 `.pt` 到 compiled `.xmodel` 的全部命令 |
| `platform/kv260/README.md` | 硬件 + 固件 + 部署 | 提供板载固件加载与 DPU 验证命令 |
| `framework/vitis_ai/README.md` | 框架补丁 | 提供 patch 应用与 signed int8 归一化源码 |
| `software/inference_app/README.md` | 板载推理 | 提供推理应用构建与运行命令 |
| `platform/post_processing/README.md` | HLS 后处理 | 提供 HLS 核平台创建四步流程 |

注意：`software/quantization/` 因许可证**不含源码**，只有 README 与 `modifications.md`；`software/training/` 同理只含补丁文件。本讲引用的命令均出自上述 README 的真实代码块。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**阶段产物衔接**、**运行时加载顺序**、**端到端踩坑排查**。

### 4.1 阶段产物衔接

#### 4.1.1 概念说明

「产物衔接」回答的问题是：**上一阶段吐出的东西，凭什么能被下一阶段吃下去？**

直觉上，流水线就是一条命令接一条命令。但真正决定成败的，是每两个阶段之间的**契约**。例如：

- 量化阶段假设网络里 sigmoid/silu 已经被替换成 hsigmoid/hswish，否则校准算出的 scale 与真实推理对不上。
- 编译阶段假设 `arch.json` 描述的 DPU 架构与板子上真实 DPU 的指纹一致，否则指令跑不起来。
- 推理阶段假设输入是 800×800 的 signed int8 三通道 TIFF，且模型 `model.prototxt` 里写的输出层名与编译后模型真实输出张量名一致。

这些契约不会自己写进文件名，它们散落在不同 README 的不同命令里。本模块的任务就是把它们收集到一张「产物—契约」对照表里。

#### 4.1.2 核心流程

从训练好的 `.pt` 到板载 JSON 预测，产物链如下（每行：产物 → 下一个消费者）：

```text
[形态①] float .pt  (训练产物)
   │  calib (PTQ) 或 QAT：观测激活、求 scale
   ▼
quant_info.json  +  含伪量化阈值的 .pt
   │  dump_xmodel=True：固化 scale 为 IR
   ▼
[形态②] nndct_quant/DetectionModel_0_int.xmodel  (硬件无关 Xilinx IR)
   │  vai_c_xir -a arch.json：按 DPU 架构翻译指令
   ▼
[形态③] vai_c_output/my_yolov8_model.xmodel + meta.json + checksum  (DPU 指令包)
   │  + model.prototxt (detect_layer_name 来自 xir subgraph)
   ▼
model/ 目录  ──scp──▶  KV260:/usr/share/vitis_ai_library/models/
   │  xview3_benchmark / xview3_performance
   ▼
output-file.txt  (chip_id,label,x,y,w,h,score)   +   FPS
```

三件套里要特别盯住三处「窄口」：

1. **calib/test 之间**：`quant_mode=calib` 算出的 scale 落盘成 `quant_info.json`，`quant_mode=test` 时再读它固化。两次命令的 `imgsz` 与两个 `--nndct_convert_*` 标志必须逐字一致。
2. **导出/编译之间**：导出的是形态②，编译吃形态②吐形态③，**名字都叫 `.xmodel`**，必须靠路径（`nndct_quant/...` vs `vai_c_output/...`）区分。
3. **编译/prototxt 之间**：编译器会重构计算图并**重命名**输出张量，所以 `detect_layer_name` 不能手写、只能用 `xir subgraph` 查。

#### 4.1.3 源码精读

**全局组件归属。** 先确认每个产物属于仓库哪个目录，避免在错的目录里找文件：

[README.md:12-19](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/README.md#L12-L19) 列出六大组件，其中 `software/quantization/`（产出 `.xmodel`）、`framework/vitis_ai/`（推理库补丁）、`platform/kv260/`（固件）、`platform/post_processing/`（HLS 核）、`software/inference_app/`（板载推理）正是本讲产物链上的五个目录。

**量化两路线与导出。** PTQ 与 QAT 的分水岭是「是否改权重」，但二者**最后都汇入同一条导出命令**：

[software/quantization/README.md:39-41](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L39-L41) 是 PTQ 校准命令（`quant_mode=calib`，只前向，分钟级）；

[software/quantization/README.md:47-49](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L47-L49) 是 QAT 命令（`detect train ... epochs=100`，完整训练循环）。两条命令都带相同的 `imgsz=800` 与两个 `--nndct_convert_*` 标志——这就是契约①「激活替换全程一致」的落点。

随后无论走哪条路，都用同一条导出命令把 scale 固化成 IR：

[software/quantization/README.md:56-66](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L56-L66) 给出 `quant_mode=test batch=1 dump_xmodel=True dump_onnx=True`（形态②导出）与 `dump_xmodel=False` 的纯评估两条命令。

**编译与 prototxt。** 形态②到形态③靠 `vai_c_xir`，`-a` 指向 `arch.json` 是阶段④与阶段⑤之间**唯一的硬件耦合点**：

[software/quantization/README.md:76-83](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L76-L83) 是编译命令，产物目录含 `.xmodel`、`meta.json`、`checksum` 三件。

紧接着要为这个编译模型配 `model.prototxt`，其中 `detect_layer_name` **必须**用 `xir subgraph | grep DPU` 查标 `O` 的张量名填入（编译器会重命名输出层，无法手写）：

[software/quantization/README.md:89-99](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L89-L99) 给出 prototxt 配置与 `xir subgraph` 查输出层名两步。**注意**：本项目用了 P2 高分辨率检测头（见 [u6-l3](u6-l3-postprocess-optimization.md)），比标准 YOLOv8 多一个输出头，所以 `detect_layer_name` 的**条数也要相应多一条**，少了会漏掉一整个尺度的预测。

**部署到板。** 最后把整个 `model/` 目录 scp 上板，放在 Vitis AI 约定的模型搜索路径下：

[software/quantization/README.md:101-105](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L101-L105) 把 `model/` 拷到 `~/usr/share/vitis_ai_library/models/`。注意 README 写的是 `~/usr/share/...`（家目录下的 `usr`），与 Vitis AI 默认的 `/usr/share/...` 略有差异，部署时以你板上 Vitis AI 实际搜索的路径为准。

#### 4.1.4 代码实践

**实践目标**：把抽象的「产物链」落成一张可勾选的对照表，强迫自己写出每个产物的路径、来源命令与它对下游的契约。

**操作步骤**：

1. 新建一个 Markdown 或表格文件 `deploy-artifacts.md`（写在项目仓库之外即可，不要改本仓库源码）。
2. 按下面五行表头填满，命令直接从上面四个永久链接抄真实命令：

| # | 产物（路径） | 产生它的命令 | 消费它的下一阶段 | 它向下游强制的契约 |
| :--: | :-- | :-- | :-- | :-- |
| 1 | `<model>.pt`（形态①） | （训练，u3） | calib / QAT | 类别数=3、imgsz=800 |
| 2 | `quant_info.json` | `quant_mode=calib` | `quant_mode=test` 导出 | imgsz + 两个激活标志 |
| 3 | `nndct_quant/DetectionModel_0_int.xmodel`（形态②） | `dump_xmodel=True` | `vai_c_xir` | batch=1 |
| 4 | `vai_c_output/<name>.xmodel`（形态③）+ `meta.json` | `vai_c_xir -a arch.json` | prototxt + 板载 | arch 与板上 DPU 指纹一致 |
| 5 | `model.prototxt`（含 `detect_layer_name`） | 手写 + `xir subgraph` 查名 | 板载推理 app | 层名/条数 = P2 头数 |
| 6 | `output-file.txt`（预测）+ FPS | `xview3_benchmark/performance` | xview3_metrics 评估 | 坐标需加 chip offset |

**需要观察的现象**：填表过程中，你会发现自己对第 5 行「契约」最不确定——这正是最容易踩坑的地方，留到 4.3 展开。

**预期结果**：得到一张 6 行的表，每行的「契约」一列都对应一条前序单元讲过的一致性约束。

#### 4.1.5 小练习与答案

**练习 1**：形态②的 `.xmodel` 与形态③的 `.xmodel` 文件扩展名相同，部署时如何避免拿错？

> **答案**：靠**路径**区分。形态②在 `nndct_quant/` 下（量化导出产物），形态③在 `vai_c_output/` 下（编译产物，且同目录还有 `meta.json` 与 `checksum`）。scp 上板的是形态③整个 `model/` 目录。绝对不要把 `nndct_quant/` 下的 IR 直接拷上板——它还没翻译成 DPU 指令。

**练习 2**：为什么 `model.prototxt` 里的 `detect_layer_name` 不能照着训练时的网络 yaml 直接抄？

> **答案**：因为 `vai_c_xir` 编译时会重构计算图并**重命名**输出张量。prototxt 必须填「编译后」真实的输出张量名，这些名字只能用 `xir subgraph yolov8.xmodel | grep DPU` 从输出列表（标 `O`）里查到。此外本项目有 P2 头，输出层条数比标准 YOLOv8 多一条，数量也必须对齐。

---

### 4.2 运行时加载顺序

#### 4.2.1 概念说明

「加载顺序」回答的问题是：**板子开机后，要依次把哪些东西放到位，推理才能跑起来？**

KV260 上跑一次 YOLOv8 推理，需要四个独立的运行时组件**同时在线**：

1. **Linux 内核 + DPU 驱动**（来自 SD 卡启动镜像）——没有它，连 `xdputil` 都跑不了。
2. **PL 里的 DPU 电路**（来自加速器固件三件套）——DPU 是 FPGA 上由比特流配置出来的硬件，不加载固件，PS 就找不到 DPU。
3. **Vitis AI 框架库**（含本项目补丁）——推理应用链接的就是这套库，它封装了「跑 DPU + 后处理」。
4. **模型目录**（compiled `.xmodel` + `meta.json` + `model.prototxt`）——框架按名字加载它。

（可选第 5 个：**HLS 后处理 xclbin**，若把解码下放到 PL 解码核，见 [u8](u8-l1-hls-interface.md)。）

关键认知是：**这四样东西由不同单元产出、放在不同路径、由不同命令激活，但存在严格的先后依赖**。顺序错了，现象往往是「明明文件都在，却报找不到 DPU / 找不到模型」。

#### 4.2.2 核心流程

板载从开机到出结果的正确顺序：

```text
① 刷 SD 卡启动镜像 (BOOT.BIN + image.ub + ext4 rootfs)   ── u5-l3 产出
        │  开机进入 Linux，DPU 内核驱动已编入
        ▼
② scp 加速器固件三件套 → /lib/firmware/xilinx/<app>/     ── u5-l4 产出
   (project_1.bit.bin + kv260.dtbo + shell.json)
        │  xmutil unloadapp → xmutil loadapp <app>
        ▼
③ xdputil query 验证 DPU 架构/频率/指纹                   ── 验收闸口
        │  确认 DPUCZDX8G_ISA1_B4096 / 325MHz
        ▼
④ scp Vitis AI 框架编译产物（含补丁）→ /usr/local/         ── u6 产出
        │  含 SAR 归一化 / PIoU2 NMS / P2 后处理
        ▼
⑤ scp model/ → /usr/share/vitis_ai_library/models/        ── u4-l4 产出
        │  compiled .xmodel + meta.json + model.prototxt
        ▼
⑥ 板上 sh build.sh 编译推理应用                           ── u7 产出
        │  产出 xview3_benchmark / xview3_performance
        ▼
⑦ 跑推理：PIOU2_NMS=1 ./xview3_benchmark ...             ── 出结果
```

**顺序的物理依据**：

- ② 必须在 ③ 之前：`xdputil query` 读的是 PL 里已配置的 DPU，固件没加载就读不到。
- ③ 必须在 ⑦ 之前：推理要往 DPU 提交任务，DPU 不在线会直接报错。
- ④ 必须在 ⑥ 之前（若在板上编译）：`build.sh` 链接的就是 `/usr/local/` 下的 Vitis AI 库（见 [u7-l1](u7-l1-inference-build.md)）。
- ⑤ 必须在 ⑦ 之前：推理命令第一个参数就是模型名，框架按名字到 models 目录找。

**两条并行的加载轴**：注意固件轴（②③，配置 PL 硬件）与软件轴（④⑤⑥⑦，准备库/模型/应用）相互独立，可以先做任意一条，但**两条都完成**才能进入⑦。这也是「硬件工具链」与「软件工具链」在运行时的最终汇合点（承接 [u1-l3](u1-l3-end-to-end-pipeline.md)）。

#### 4.2.3 源码精读

**固件三件套的上板与加载（轴①：硬件）。** 这是「文件都在却跑不了 DPU」最常见的盲区——光 scp 不够，还要 `xmutil loadapp` 把比特流配进 PL：

[platform/kv260/README.md:209-212](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L209-L212) 把 `kv260.dtbo`、`project_1.bit.bin`、`shell.json` 三个文件 scp 到板上 `/home/root/`；

[platform/kv260/README.md:214-234](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L214-L234) 是真正的「激活」步骤：建 `/lib/firmware/xilinx/kv260-dpu-trd/` 目录、把三文件移进去、`xmutil unloadapp` 卸掉当前应用、`xmutil loadapp kv260-dpu-trd` 加载（预期输出 `Accelerator loaded to slot 0`）。**目录名即应用名**，`loadapp` 的参数必须与目录名一致。

加载成功后，用 `xdputil query` 验收：

[platform/kv260/README.md:236-265](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L236-L265) 的 JSON 输出里有三个关键字段：`DPU Arch: DPUCZDX8G_ISA1_B4096`、`DPU Frequency (MHz): 325`、`fingerprint: 0x101000056010407`、`is_vivado_flow: true`。其中 `DPU Arch` 与 `fingerprint` 必须与 4.1 编译模型用的 `arch.json` 对账——**换板必换 arch、必重编译**（承接 [u4-l4](u4-l4-export-and-compile.md)）。

**框架补丁的应用与部署（轴②：软件）。** Vitis AI 框架本身要从 GitHub 下载，本项目的补丁贴到它的源码上、再随 PetaLinux 交叉编译：

[framework/vitis_ai/README.md:7-11](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md#L7-L11) 给出 `cd vitis_ai && git apply ../framework/vitis_ai/xview3_yolov8_v3.5.patch`，并说明编译后的二进制要传到 KV260 的 `/usr/local/`。这一步把训练侧的三条数据约定（TIFF 加载、SAR 归一化、PIoU2 NMS）在 C++ 推理侧复刻（承接 [u6-l1](u6-l1-patch-overview.md)）。

**推理应用的构建与运行（软件轴末端）。** 框架库就位后，编译并运行推理应用：

[software/inference_app/README.md:6-8](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L6-L8) 是 `sh build.sh`；

[software/inference_app/README.md:10](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L10) 约定输入：一个 `test-image-list.txt`，每行一个 800×800 TIFF 路径——这正是 4.1 契约③「输入尺寸 800」在推理侧的落点。

最后两条运行命令分别对应精度与吞吐：

[software/inference_app/README.md:14-18](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L14-L18) 是精度基准 `PIOU2_NMS=<0|1> ./xview3_benchmark <model> <list> <out> -t <N>`，输出 `chip_id,label,x,y,w,h,score`；

[software/inference_app/README.md:20-25](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L20-L25) 是吞吐测试 `DEEPHI_PROFILING=<0|1> ./xview3_performance <model> <list> -t <N>`，输出 FPS 与三段耗时。

**HLS 后处理核的平台创建（可选第三轴）。** 若使用 PL 解码核，它有自己的「平台创建四步」流程，与上面两轴并行：

[platform/post_processing/README.md:18-34](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/README.md#L18-L34) 列出 Vivado 平台硬件设计 → Vitis 平台创建（产出 `dtg_output/` 设备树 overlay 与 shell 描述）→ 编译内核与 host 应用并打包 XCLBIN → 部署上板用 `xmutil` 加载。注意第 4 步同样以 `xmutil` 加载收尾，与 4.2 轴①的加载机制是同一套（承接 [u8-l5](u8-l5-hls-synthesis-packaging.md)）。

#### 4.2.4 代码实践

**实践目标**：把 4.2.2 的七步流程落成一份「加载顺序 checklist」，并标注每步的前置依赖。

**操作步骤**：

1. 在 4.1.4 的 `deploy-artifacts.md` 同一文件里新增「运行时加载顺序」小节。
2. 按 ①→⑦ 顺序，每步写三栏：**命令**（从上面永久链接抄真实命令）、**预期输出关键字**、**前置依赖**（哪一步必须先完成）。
3. 例如第 ③ 步：命令 `xdputil query`，预期关键字 `DPUCZDX8G_ISA1_B4096` / `Accelerator loaded to slot 0`（上一步），前置依赖 `xmutil loadapp` 成功。
4. 在第 ⑦ 步旁边显式标注三个环境变量的取值：`PIOU2_NMS=1`（训推一致）、`imgsz=800`（隐含在 TIFF 与模型里）、`DEEPHI_PROFILING=1`（仅性能测试时）。

**需要观察的现象**：写「前置依赖」栏时，你会确认 ②→③ 是硬依赖（固件加载→查询），而 ④⑤ 与 ②③ 之间无依赖、可并行准备。

**预期结果**：得到一份 7 步 checklist，每步都能独立验证（有预期输出），任何一步失败都能定位是哪一轴的问题。**待本地验证**：实际 KV260 板上的路径（`/usr/local/`、`/usr/share/vitis_ai_library/models/`）与权限可能因 PetaLinux rootfs 配置不同而略有差异，以你板上实际为准。

#### 4.2.5 小练习与答案

**练习 1**：假如你跳过 `xmutil loadapp`，直接 `scp` 完模型就跑 `xview3_benchmark`，最可能报什么错？

> **答案**：大概率报「找不到 DPU / DPU core 未就绪」之类。因为 DPU 是 PL 里由比特流配置出来的硬件，光 scp 固件文件不会配置 PL；必须 `xmutil loadapp` 把 `project_1.bit.bin` 灌进 PL、并用 `kv260.dtbo` 向内核声明设备，PS 才看得到 DPU。`xdputil query` 在这一步之前也会读不到架构信息。

**练习 2**：固件轴（②③）与软件轴（④⑤⑥）能否调换先后？

> **答案**：可以。两轴相互独立：固件轴配置 PL 硬件，软件轴准备库/模型/应用，互不读取对方的产物。实际操作中常先在主机交叉编译好软件包，同时让板子跑固件加载，最后汇合。但**两轴都完成**是进入第⑦步推理的前提。

**练习 3**：`xdputil query` 输出里的 `fingerprint` 字段，和 4.1 的哪一步直接相关？

> **答案**：与 `vai_c_xir -a arch.json` 编译那步直接相关。`arch.json` 描述的 DPU 架构必须与板上 `xdputil query` 报出的 `DPU Arch` / `fingerprint` 一致；若换了不同型号或不同配置的板子（指纹变了），用旧 `arch.json` 编译的模型会无法运行，必须重编译。

---

### 4.3 端到端踩坑排查

#### 4.3.1 概念说明

「踩坑排查」回答的问题是：**当端到端跑不通、或跑通了但精度/速度不对，该按什么顺序怀疑？**

本模块不教新机制，而是把前八单元反复强调的**三条一致性暗线**（归一化、IoU、输入尺寸）外加**两条加载契约**（输出层名、固件加载）整理成一张「症状→根因→检查点」的排查表。

经验法则是：**先查「能不能跑」（加载类问题），再查「跑得对不对」（一致性类问题）**。加载类问题通常直接报错、容易定位；一致性类问题往往不报错、只是精度悄悄掉几个点，更危险。

#### 4.3.2 核心流程

排查决策树（按从「硬」到「软」的顺序）：

```text
症状：跑不起来 / 报错
├─ 找不到 DPU？           → 查固件：xmutil loadapp 是否成功、xdputil query 是否有架构
├─ 找不到模型？           → 查路径：model/ 是否在 Vitis AI 搜索路径、prototxt 是否同名
├─ 框架库链接/符号缺失？  → 查补丁：git apply 是否成功、/usr/local/ 是否是带补丁的编译产物
└─ 输出层名报错？         → 查 detect_layer_name：是否用 xir subgraph 查的、条数是否= P2 头数

症状：跑通了但精度异常
├─ 整体掉点几个百分点？   → 查归一化：是否转成 signed int8、min/range 标量顺序是否对
├─ 框重叠区多检/漏检？    → 查 PIOU2_NMS：是否 =1（否则用了默认 CIoU，与训练损失不一致）
├─ 小目标漏检多？         → 查 P2 头：prototxt 是否少了一条 detect_layer_name
└─ 量化后掉点明显？       → 查激活标志：calib/QAT/export/eval 四条命令的 --nndct_convert_* 是否逐字一致

症状：跑通了但 FPS 低
└─ 查 DEEPHI_PROFILING：  pre/DPU/post 哪段最长 → post 长则考虑 HLS 解码核（u8）
```

排查时要带着一个意识：**同一份契约在多个 README 里各写了一半**。例如「signed int8 归一化」的公式在 `framework/vitis_ai/README.md`，而它必须匹配的训练侧 `normalize()` 在 `software/training/`（见 [u3-l2](u3-l2-sar-normalization.md)）；「PIOU2」的开关在 `software/inference_app/README.md`，而它必须匹配的训练损失在 `software/training/metrics.py`（见 [u3-l3](u3-l3-piou2-loss.md)）。**跨文件对账**是排查一致性问题的基本动作。

#### 4.3.3 源码精读

**坑①：输出层名 / P2 头条数。** 这是最隐蔽的加载类问题——prototxt 写错不一定报错，而是少解码一个尺度：

如 4.1.3 所引 [software/quantization/README.md:89-99](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L89-L99)，`detect_layer_name` 必须取 `xir subgraph | grep DPU` 输出列表里标 `O` 的张量名。本项目 P2 架构有 **4 个检测头**（P2/P3/P4/P5），所以 prototxt 里 `detect_layer_name` 也应是 **4 条**；若照抄标准 YOLOv8 的 3 条，会整段丢失最高分辨率的小目标预测。

**坑②：signed int8 归一化。** 这是典型「不报错只掉点」的一致性问题。推理侧必须把 SAR 三波段按完全相同的线性映射压成 `[-128,127]`：

[framework/vitis_ai/README.md:16-30](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md#L16-L30) 给出四步 `subtract/divide/min/max` 后 `convertTo(output_image, CV_8S, 255, -128)`。两个易错点：(a) `CV_8S` 是**有符号** int8，因为 DPU（DPUCZDX8G）是有符号 8 位定点加速器，写成无符号 `CV_8U` 会整体偏移 128；(b) 标量顺序是 `(-6000, -50, -50)` / `(8000, 70, 70)`，对齐的是 OpenCV 读取后的通道内存顺序（bathymetry, VV, VH），与磁盘写盘顺序（VV, VH, bathymetry）不同——这正是 [u3-l2](u3-l2-sar-normalization.md) 讲过的「通道反转」在推理侧的对应。标量顺序填反，会把水深当成回波强度归一化，结果全是噪声。

**坑③：PIOU2_NMS 开关。** 训练用 PIoU2 损失、推理 NMS 默认却走 CIoU，会导致重叠区判重口径与训练不一致：

[software/inference_app/README.md:14-18](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L14-L18) 写明 `PIOU2_NMS=1` 才用 PIoU2，否则用默认（README 措辞为 CIoU）。推荐部署设 `PIOU2_NMS=1` 以保持训推一致（承接 [u6-l2](u6-l2-piou2-nms.md)）。这个开关通过 `DEF_ENV_PARAM` 编译期注册、`export` 运行时改，无需重编译二进制。

**坑④：激活替换标志不一致。** 两个 `--nndct_convert_*` 标志必须在 calib、QAT、导出、评估**四类命令**里逐字一致，否则量化参数（scale）与模型结构错配：

见 4.1.3 所引 [software/quantization/README.md:39-41](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L39-L41) 与 [software/quantization/README.md:47-49](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L47-L49)，两条命令末尾都挂着相同的两个标志。漏掉一个，会让 sigmoid/silu 在量化图里未被替换，DPU 无法高效执行或精度劣化（承接 [u4-l3](u4-l3-activation-replacement.md)）。

**坑⑤：输出坐标是芯片局部坐标。** 这是评估阶段的坑，不在板上报错，但会让 `xview3_metrics` 算出荒谬的 F1：

[software/inference_app/README.md:14-18](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/inference_app/README.md#L14-L18) 输出格式 `chip_id,label,x,y,w,h,score` 里的 `x,y` 是**芯片局部像素坐标**（0~800），而 xView3 标注用的是**场景全局坐标**。喂给 [u3-l4](u3-l4-xview3-metrics.md) 评估前，必须用 `chip_id` 反查切片时的 chip offset（`column=offset_x+x`、`row=offset_y+y`）还原成全局 `detect_scene_row/column`（承接 [u3-l5](u3-l5-validation-nms.md)）。不做这一步，所有检测点都挤在 (0,800) 范围，匹配全部失败。

#### 4.3.4 代码实践

**实践目标**：把 4.3.2 的决策树落成一份「症状→检查点→修复」三栏排查手册。

**操作步骤**：

1. 在 `deploy-artifacts.md` 再加一节「踩坑排查表」。
2. 至少填入下面 6 行，每行的「检查点」必须指向一个本讲引用过的永久链接或前序单元：

| 症状 | 首先检查 | 修复动作 / 依据 |
| :-- | :-- | :-- |
| 找不到 DPU | `xmutil loadapp` / `xdputil query` | 重载固件三件套（[kv260 README](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L214-L234)） |
| 小目标整段漏检 | `detect_layer_name` 条数 | 应为 4 条（P2 头），用 `xir subgraph` 重查 |
| 整体掉点且不报错 | signed int8 归一化 | 确认 `CV_8S`、标量顺序（[framework README](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/framework/vitis_ai/README.md#L16-L30)） |
| 重叠区多检/漏检 | `PIOU2_NMS` | 设为 1（训推一致） |
| 量化后明显掉点 | 四条命令的 `--nndct_convert_*` | 逐字对齐 calib/QAT/export/eval |
| 评估 F1 异常低 | 输出坐标空间 | 加 chip offset 还原全局坐标 |

3. 对每一行，再写一句「为什么」（即它对应哪条一致性暗线或加载契约）。

**需要观察的现象**：填表时你会发现，「不报错只掉点」的坑（②③④⑤）比「报错」的坑（①）更值得警惕——因为它们不会主动告诉你。

**预期结果**：得到一份可随部署过程逐项勾选的排查手册。**待本地验证**：实际症状的具体报错文本因 Vitis AI / PetaLinux 版本而异，以板上日志为准。

#### 4.3.5 小练习与答案

**练习 1**：精度只比预期低 ~1%，且没有任何报错，按本讲决策树应优先怀疑哪一类？

> **答案**：优先怀疑**一致性类**（不报错只掉点）。按决策树，先查归一化（是否 `CV_8S`、标量顺序是否对）、再查 `PIOU2_NMS` 是否设 1、再查激活替换标志是否四命令一致。这几个都不会报错，但各自能让精度悄悄偏移。

**练习 2**：为什么「输出坐标是芯片局部坐标」这个坑不会在板上暴露，却会让评估全错？

> **答案**：因为板载推理程序只负责输出 `chip_id,label,x,y,w,h,score`，`x,y` 是芯片内 0~800 的局部坐标，程序本身不会报错。但 xView3 标注用的是场景全局坐标，评估时若不先用 chip offset 把局部坐标平移回全局 `detect_scene_row/column`，所有预测点会挤在 (0,800) 的小方块里、与全局真值完全错位，匈牙利匹配几乎全失败，F1 接近 0。这是典型的「上下游坐标空间契约」问题。

**练习 3**：`xdputil query` 报的 `DPU Arch` 与编译用的 `arch.json` 不一致时，该改哪一边？

> **答案**：以**板上真实 DPU**（`xdputil query`）为准，回头改 `vai_c_xir -a` 指向的 `arch.json` 并**重新编译**模型。`arch.json` 是「软件工具链」对硬件的描述，必须服从板上真实硬件；硬改硬件（重新综合比特流）成本远高于重编译，除非你确实要换 DPU 配置。

---

## 5. 综合实践

把本讲三个模块合成一份**端到端部署 checklist**。这是本讲的核心交付物，也是后续真正上板时的操作手册。

**任务**：从一份训练好的 `model.pt` 出发，写出直到板上打印 FPS 与 JSON 预测所需的**每一步命令与产物**，并标注哪些步骤依赖前序单元的修改。

**要求**：

1. 按「产物链」与「加载顺序」两条轴组织，最终汇合到一次推理命令。
2. 每一步写出：**命令**（真实命令，引自本讲永久链接）、**产物**（路径）、**依赖的前序修改**（用 `→` 指向某单元）。
3. 在 checklist 末尾用一段话说明：哪三个环境变量/标志决定了「训推一致性」（答案：`imgsz=800`、两个 `--nndct_convert_*`、`PIOU2_NMS=1`），以及它们的值必须与训练侧的什么对应。

**参考骨架**（请补全命令与产物）：

```text
[A. 软件工具链 —— 产物链]
1. PTQ 校准:  yolo detect val ... quant_mode=calib ...  → quant_info.json
   依赖: → u4-l1/u4-l3 (imgsz=800 + 两个激活标志)
2. 导出 IR:   yolo detect val ... quant_mode=test dump_xmodel=True ...
   → nndct_quant/DetectionModel_0_int.xmodel (形态②)
3. 编译:      vai_c_xir -x ... -a arch.json -o vai_c_output -n <name>
   → vai_c_output/<name>.xmodel + meta.json + checksum (形态③)
   依赖: → u4-l4 (arch.json 与板上 DPU 指纹一致)
4. 配 prototxt: xir subgraph ... | grep DPU → 填 detect_layer_name (4 条, P2)
   → model/<name>.prototxt
   依赖: → u6-l3 (P2 多一个头)

[B. 硬件工具链 —— 加载顺序(固件轴)]
5. 刷 SD 卡启动镜像 → 开机 Linux + DPU 驱动          → u5-l3
6. scp 固件三件套 + xmutil loadapp + xdputil query   → u5-l4

[C. 软件部署 —— 加载顺序(软件轴, 与 B 并行)]
7. git apply 补丁 + 交叉编译 Vitis AI → scp /usr/local/  → u6
8. scp model/ → /usr/share/vitis_ai_library/models/    (来自步骤 3+4)
9. sh build.sh 编译推理应用                             → u7-l1

[D. 汇合 —— 跑推理]
10. 精度: PIOU2_NMS=1 ./xview3_benchmark <name> <list> <out> -t <N>
    → output.txt (chip_id,label,x,y,w,h,score)
    依赖: → u6-l1 (signed int8 归一化) / u6-l2 (PIOU2_NMS)
11. 吞吐: DEEPHI_PROFILING=1 ./xview3_performance <name> <list> -t <N>
    → FPS + pre/DPU/post 三段耗时
12. 评估前: 用 chip offset 还原全局坐标 → 喂 xview3_metrics  → u3-l4/u3-l5
```

**验收标准**：checklist 中每一步都能在前序单元找到依据；三个一致性开关在末尾被显式列出并说明对应关系。**待本地验证**：所有命令的实际参数（模型名、IP、线程数、路径）需按你的环境填充；板上 Vitis AI 库与模型搜索路径以实际 PetaLinux rootfs 为准。

## 6. 本讲小结

- **产物链**：从 `.pt`（形态①）经 calib/QAT、`dump_xmodel`、`vai_c_xir` 三跳，变成板载可执行的 compiled `.xmodel`（形态③）；②③同名却异构，靠路径区分，scp 上板的是形态③整个 `model/` 目录。
- **加载顺序**：板载需 Linux+DPU 驱动（SD 卡）→ 固件三件套（`xmutil loadapp`）→ Vitis AI 框架库（`/usr/local/`）→ 模型目录四样同时在线；固件轴与软件轴相互独立，但都完成才能推理。
- **验收闸口**：`xdputil query` 报出的 `DPU Arch` / `fingerprint` 必须与编译用的 `arch.json` 对账，是硬件与软件工具链的汇合验收点。
- **三条一致性暗线**是调优主轴：归一化（signed int8，`CV_8S` + 正确标量顺序）、IoU（`PIOU2_NMS=1`）、输入尺寸（`imgsz=800`），任一不一致都会掉精度。
- **两类坑**：加载类（输出层名/P2 头条数、xmutil 加载、arch 对账）通常报错易定位；一致性类（归一化、PIOU2_NMS、激活标志、坐标空间）不报错只掉点，更需主动排查。
- **跨文件对账**是基本功：同一契约的训练侧与推理侧实现散落在不同目录（如 `software/training/` 与 `framework/vitis_ai/`），排查时必须两边对照。

## 7. 下一步学习建议

- 本讲是单元 9 的第一篇，把链路「跑通」。下一篇 [u9-l2 性能-精度-功耗权衡分析](u9-l2-performance-tradeoffs.md) 会在跑通的基础上，用 `xview3_models.jpg` 与 `inference_breakdown.jpg` 两张图分析本项目相对 SOTA 的精度-效率位置，以及量化、P2 头、HLS 后处理各自的贡献。
- 若你负责**精度回归**：重读 [u3-l4 xView3 评估指标](u3-l4-xview3-metrics.md) 与 [u3-l5 验证与 NMS](u3-l5-validation-nms.md)，把板载输出严格按本讲步骤 12 还原全局坐标后再评估。
- 若你负责**吞吐优化**：重读 [u7-l3 performance profiling](u7-l3-performance-profiling.md)，用 `DEEPHI_PROFILING=1` 拆出 pre/DPU/post 三段，确认 post（YOLOV8_DECODING）是否为瓶颈；若是，深入 [u8 HLS 解码核](u8-l1-hls-interface.md) 把后处理下放到 PL。
- 若你负责**换板/换模型**：抓住本讲的两个「唯一耦合点」——`arch.json`（编译↔DPU 指纹）与 `detect_layer_name`（prototxt↔编译后图），换板重编译、换模型重查输出层名。
- 继续阅读源码建议：把 `software/quantization/README.md` 的四节命令与 `platform/kv260/README.md` 的部署第四节对照通读一遍，亲手把本讲的 checklist 每一步对应到具体命令行。
