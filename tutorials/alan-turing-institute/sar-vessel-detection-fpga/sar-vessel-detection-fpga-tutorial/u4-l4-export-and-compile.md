# 模型导出与 DPU 编译

## 1. 本讲目标

本讲是第四单元「Vitis AI 量化」的收尾篇，对应端到端流水线里**阶段 ③ 的尾巴（导出）**与**阶段 ④（DPU 编译）**——参见 u1-l3 的七阶段图。经过 u4-l1（PTQ 校准）或 u4-l2（QAT 训练），我们已经得到一个「带量化 scale 的模型」：它知道每层该怎么量化，但还**不是一个能直接跑在硬件上的文件**。本讲就负责走完最后两跳，把量化模型变成 KV260 板上可执行的产物。

读完本讲你应当能够：

- 看懂并写出**导出命令**（`dump_xmodel=True dump_onnx=True quant_mode=test batch=1`），说清每个参数为什么这样取，以及它背后 `validator.py` 调用的两个 `export_*` 函数。
- 区分三种「模型形态」：浮点 `.pt`、量化 `xmodel`（IR）、编译后 `xmodel`（DPU 指令）——它们**都叫 xmodel 却是两样东西**，这是本讲最易混淆、也最关键的认知。
- 写出 **`vai_c_xir` 编译命令**，逐个解释 `-x/-a/-o/-n` 四个参数，并理解 `arch.json` 为何是「阶段 ④ 与阶段 ⑤ 唯一的硬件耦合点」。
- 读懂编译产物目录 `vai_c_output/` 里 `.xmodel`、`meta.json`、`checksum` 三个文件各自的作用。
- 用 `xir subgraph ... | grep DPU` 定位 DPU 子图的输出张量名，把它们填进 `model.prototxt` 的 `detect_layer_name`，并**说清楚为什么这个字段不能手写、必须从子图输出里取**。

本讲承接 u4-l1（PTQ 的 `quant_info.json` 在这里被消费）与 u4-l2（QAT 的 deployable model 在这里被导出），同时呼应 u4-l3 的激活替换一致性。本讲之后，软件工具链就到头了——下一单元 u5 进入硬件平台构建。

> 重要前提（与 u4-l1 一致）：由于上游 Ultralytics 许可证限制，`software/quantization/` **不含量化源码**，只有两份文档：命令清单 `README.md`、改动概述 `modifications.md`。真正的导出/编译逻辑在 AMD 的 Vitis AI 3.5 工具链（`pytorch_nndct`、`vai_c_xir`、`xir`）里。因此本讲「源码精读」以这两份文档为锚点给出永久链接与行号；涉及工具链内部行为处会标注为通用原理，不编造行号。

## 2. 前置知识

在拆解命令之前，先建立一个贯穿全讲的**核心心智模型：模型的三种形态**。很多初学者会把它们混为一谈，结果在排错时一头雾水。

到本讲为止，同一个 YOLOv8 会以三种不同形态出现，**体积与含义逐级变化**：

| 形态 | 出现阶段 | 是什么 | 在哪产生 |
| --- | --- | --- | --- |
| ① 浮点 `.pt` | 训练（u3） | float32 权重，PyTorch 生态 | `yolo detect train` |
| ② 量化 `xmodel` | 导出（本讲 4.1） | Xilinx IR：一张**int8 量化计算图**，含每层 scale，但**与具体 DPU 型号无关** | `dump_xmodel=True` |
| ③ 编译 `xmodel` | 编译（本讲 4.2） | 针对某款 DPU 的**机器指令 + 内存布局**，KV260 专属 | `vai_c_xir` |

三种形态的要点：

- **① → ②** 是「量化」：把浮点算子换成定点算子、把权重压成 int8，但图本身仍是**抽象的**——它描述「算什么」，不描述「在哪种硬件上怎么算」。
- **② → ③** 是「编译」：把抽象图针对**特定 DPU 架构**（本项目是 `DPUCZDX8G`，KV260）编排成指令序列。这一步不可移植——换成 ZCU104 的 DPU 就得重新编译。
- **② 和 ③ 都叫 `.xmodel`，但内容完全不同**：② 是「量化图」（喂给编译器的输入），③ 是「DPU 指令包」（板载运行的输出）。本讲反复要强调的就是这个区别。

> 术语提示：
> - **IR（Intermediate Representation，中间表示）**：编译器内部的「标准图格式」。Xilinx 的 IR 文件后缀就是 `.xmodel`。它起到「前端（PyTorch）与后端（DPU 编译器）解耦」的作用——任何前端只要能导出这份 IR，就能用同一个编译器喂给各种 DPU。
> - **DPU（Deep learning Processing Unit）**：Xilinx FPGA 上的神经网络加速硬核 IP，本项目用的是 `DPUCZDX8G`（详见 u5-l1）。
> - **`arch.json`**：描述某款 DPU「长什么样」的硬件规格文件（算力 B4096、频率 325MHz、是否带 softmax 单元……）。它把「② 量化图」翻译成「③ DPU 指令」时所必需。

最后再提一次 u1-l3 的**训推一致性暗线**：本讲的导出命令仍然要带上 `imgsz=800` 与两个 `--nndct_convert_*` 激活替换标志（u4-l1、u4-l3 已述），且必须与校准/QAT 阶段**逐字一致**——否则导出的量化图拓扑与算 scale 时的图对不上。此外还新增一条「**输入尺寸 + 输出头数量**」的一致性：导出图的输出张量个数要与 `prototxt` 里的 `detect_layer_name` 条数对得上（4.3 详述），尤其是本项目用了 P2 多尺度头（u3-l1、u6-l3），会比标准 YOLOv8 多一个输出头。

## 3. 本讲源码地图

本讲涉及 `software/quantization/` 下两份文档（无源码）：

| 文件 | 作用 |
| --- | --- |
| `software/quantization/README.md` | 量化的**命令清单**。本讲覆盖其中第 2、3、4 节：导出、编译、部署准备（含 `prototxt` 与 `xir subgraph`）。 |
| `software/quantization/modifications.md` | 上游 Ultralytics 四文件的**改动概述**。本讲用它佐证导出命令背后 `validator.py` 调用的 `export_xmodel` / `export_onnx_model` 两个函数。 |

需要再次强调：导出与编译的真正实现在 Vitis AI 3.5 工具链里，不在本仓库。本讲「源码精读」一律以这两份文档原文为锚点给出永久链接与行号。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应「量化模型上板」的最后三跳：**4.1 导出**（得到 IR `xmodel`）→ **4.2 编译**（得到 DPU 指令 `xmodel`）→ **4.3 配 `prototxt` + 部署**（让运行时知道怎么取输出、怎么后处理）。

### 4.1 xmodel/onnx 导出

#### 4.1.1 概念说明

这个模块回答：**校准/QAT 之后，那个「带 scale 的模型」怎么落成一个文件？**

校准（`quant_mode=calib`）的产物是一份 scale 配置（PTQ 写成 `quant_info.json`，QAT 存在 checkpoint 的 `qat_ema_quant_info` 里——见 u4-l1、u4-l2）。但 scale 配置本身不是模型——它只是一张「每层量化参数」表。**导出**要做的，是把「浮点权重 + scale 表」**固化（freeze）**成一张完整的 int8 量化计算图，写成 IR 文件 `.xmodel`。README 用一句话点明了导出的产物与用途：

> [software/quantization/README.md:52-54](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L52-L54) —— 这段话说明：模型量化完成后，可以把它导出成 `xmodel` 格式（用于 FPGA 部署）与 `ONNX` 格式（用于更广泛的生态兼容）。

两种导出格式的定位不同：

- **`xmodel`（主线）**：Xilinx 的 IR，是下一步 `vai_c_xir` 编译器的**唯一合法输入**，也是最终上板的主线产物。
- **`ONNX`（辅线）**：业界通用格式，导出它是为了用别的工具链（如在其他 CPU/GPU 上对比验证量化精度），**不参与上板**。

所以 `dump_xmodel=True` 是必须的，`dump_onnx=True` 是可选的（调试/对比用）。

#### 4.1.2 核心流程

导出在本项目里的流程（对应 README 第 2 节）：

1. **准备量化模型**：已经过 u4-l1 的 PTQ 校准（产出 `quant_info.json`），或 u4-l2 的 QAT（产出 deployable model）。
2. **以 `quant_mode=test` 跑一次验证**：这一步会**读取**校准阶段算好的 scale，把它应用到模型上，做一次「带量化的前向」。
3. **触发导出**：`dump_xmodel=True` 让 quantizer 在前向后把量化图写成 `.xmodel`；`dump_onnx=True` 同理写 `.onnx`。
4. **产物落盘**：`.xmodel` 写到 `nndct_quant/` 目录下，文件名形如 `DetectionModel_0_int.xmodel`——注意 `_int` 后缀，表示这是**整型（int8）量化图**。

这里有个关键的状态机切换：`quant_mode` 在校准时是 `calib`（算 scale），在导出时是 `test`（用 scale）。**先 calib 再 test，顺序不能反**——因为 test 模式会去读 calib 写出的 scale 配置，没有 calib 就没有 scale 可读（u4-l1 已论证）。所以导出命令本质上是「用校准结果做一次固化推理」。

伪代码（描述 nndct quantizer 的典型行为，非仓库代码）：

```
# quant_mode=test 分支（导出）
quantizer = torch_quantizer("test", model, output_dir, bitwidth=8)
quantizer.load_quant_config()              # 读 calib 阶段写出的 scale 表
q_model = quantizer.quant_model            # 应用 scale 的量化模型
for x in eval_set:                         # 一次带量化的前向（固化的前提）
    q_model(x)
quantizer.export_xmodel(deploy_check=True)   # 写 .xmodel（dump_xmodel=True 触发）
quantizer.export_onnx_model(dynamic_batch=True)  # 写 .onnx（dump_onnx=True 触发）
```

注意 `deploy_check=True`：导出时会比对「deployable 定点模型」与「可训练量化模型」的输出是否一致，是一道安全闸——若两者输出对不上，说明量化折叠出了问题，导出的图就是错的。

#### 4.1.3 源码精读

**导出命令**（本模块最关键的一行）：

> [software/quantization/README.md:56-60](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L56-L60) —— 导出命令。逐参数拆解：
> - `yolo detect val`：仍是 **val 子命令**——导出发生在一次「带量化的验证推理」过程中（不是单独的 export 子命令）。
> - `model=<path-to-quantized-model.pt>`：输入是**已经量化过的模型**（PTQ/QAT 产出的 `.pt`，内部已带 quant_info）。
> - `nndct_quant=True`：打开 nndct 量化通路（与校准同）。
> - `quant_mode=test`：**用 scale 做推理/导出**（区别于校准的 `calib`）——这是导出能成功的前提。
> - `batch=1`：**按 batch=1 固化图**。DPU 编译通常针对固定 batch 生成指令，batch=1 是边缘部署的默认约定（KV260 单图推理）。导出时定了 batch=1，后续编译与板上推理都必须用 batch=1。
> - `imgsz=800`：与切片/训练/校准严格一致（u1-l3 暗线）。
> - `dump_xmodel=True`：触发写出 `.xmodel`（主线产物）。
> - `dump_onnx=True`：触发写出 `.onnx`（辅线产物）。
> - 两个 `--nndct_convert_*` 标志：激活替换，与校准/QAT **逐字一致**（u4-l1、u4-l3）。

**这条命令背后调用的函数**，在 `validator.py` 的改动概述里写得清清楚楚：

> [software/quantization/modifications.md:48-52](https://github.com/alan-turing-institute-sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L48-L52) —— 这段说明在 `validator.py` 里：xmodel/ONNX 导出时分别调用 `quantizer.export_xmodel(output_dir=..., deploy_check=True)` 与 `quantizer.export_onnx_model(output_dir=..., verbose=True, dynamic_batch=True)`。这把命令行上的 `dump_xmodel`/`dump_onnx` 两个开关，落实到了两个具体的 `export_*` 函数调用上。

由此可以建立命令参数 ↔ 代码函数的对应关系：

| 命令参数 | 触发的代码（modifications.md） |
| --- | --- |
| `quant_mode=test` | `torch_quantizer("test", ...)` 创建 quantizer（modifications.md:43-44） |
| `dump_xmodel=True` | `quantizer.export_xmodel(..., deploy_check=True)` |
| `dump_onnx=True` | `quantizer.export_onnx_model(..., dynamic_batch=True)` |

注意 ONNX 导出用了 `dynamic_batch=True`——这与 xmodel 的 `batch=1` 固化不同：ONNX 是给「别的工具链对比」用的，允许动态 batch 更方便；而 xmodel 要喂给 DPU 编译器，必须固定 batch=1。这种差异恰好印证了两种格式定位不同。

> 与 u4-l1/u4-l2 的衔接：导出命令读取的 scale，PTQ 路线来自 `quant_info.json`（u4-l1 的 `task.py` 改动写出），QAT 路线来自 checkpoint 的 `qat_ema_quant_info`（u4-l2 的 `convert_to_deployable` 产出）。无论哪条路线，到导出这一步都被统一成「`quant_mode=test` 读 scale → 固化」。

#### 4.1.4 代码实践

**实践目标**：对照 README 原文与 modifications.md，建立「命令参数 → 导出函数 → 产物」的完整映射，并论证 `batch=1` 与 `deploy_check=True` 为何不可省。

**操作步骤**：

1. 打开导出命令原文 [software/quantization/README.md:58-60](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L58-L60)，把命令里每个参数抄进下表的左列。
2. 打开 `validator.py` 改动概述 [software/quantization/modifications.md:48-52](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/modifications.md#L48-L52)，把每个参数对应的 `export_*` 函数与关键 flag 填进中列。
3. 在右列写出「若去掉这个参数会怎样」。

参考答案（填表结果）：

| 命令参数 | 对应函数 / flag | 去掉会怎样 |
| --- | --- | --- |
| `quant_mode=test` | `torch_quantizer("test",...)`，读取 scale | 退化成无 scale 的浮点推理，导出失败或图无量化 |
| `dump_xmodel=True` | `export_xmodel(deploy_check=True)` | 不会生成 `.xmodel`，下一步无法编译 |
| `dump_onnx=True` | `export_onnx_model(dynamic_batch=True)` | 不生成 `.onnx`（不影响上板，只影响跨工具链对比） |
| `batch=1` | 固化图时的 batch 维 | 编译器按其他 batch 生成指令，板上单图推理时形状不匹配 |
| 两个 `--nndct_convert_*` | 量化图里激活算子的替换 | 与校准/QAT 图拓扑不一致，scale 失效（u4-l3） |

**需要观察的现象 / 预期结果**：你会看到「`dump_xmodel` 和 `dump_onnx` 这两个开关，一一对应两个 `export_*` 函数」——这说明导出不是一个神秘黑盒，而是 quantizer 在验证推理完成后、按开关调用对应的序列化函数。`deploy_check=True` 是 `export_xmodel` 专属的安全检查（ONNX 那条没有），因为 xmodel 才是上板主线，必须确保定点折叠无误。

> 说明：实际运行导出命令需要 Vitis AI 3.5 Docker 环境与 `pytorch_nndct`（本仓库不含）。本实践产出是一份**参数-函数映射表**，命令可运行性标注为「待本地（Vitis AI 环境）验证」。

#### 4.1.5 小练习与答案

**练习 1**：导出命令里 `quant_mode=test` 能不能改成 `quant_mode=calib`？为什么？

> **参考答案**：不能。`calib` 是「算 scale」模式，`test` 是「用 scale 做固化推理并导出」模式。导出需要读取已算好的 scale 并把它烙进图里，这正是 `test` 模式的职责；若用 `calib`，则是在重新统计激活范围、不会固化导出量化图。必须先 calib 后 test（u4-l1 的状态机顺序）。

**练习 2**：导出的 xmodel 文件名是 `DetectionModel_0_int.xmodel`，这个 `_int` 后缀意味着什么？它和后面编译产物（也叫 `.xmodel`）有何本质区别？

> **参考答案**：`_int` 表示这是**整型（int8）量化后的计算图**——权重和激活都已是定点，但图本身仍是**与 DPU 型号无关的 IR**。它和编译产物的区别：导出的 xmodel 描述「算什么」（抽象 int8 图，喂给编译器），编译后的 xmodel 描述「在 KV260 这款 DPU 上具体怎么算」（机器指令 + 内存布局，板上运行）。两者同名却异构，是本讲最容易踩的坑。

### 4.2 vai_c_xir DPU 编译

#### 4.2.1 概念说明

这个模块回答：**导出的 xmodel（IR）为什么还不能上板，还要再「编译」一次？**

答案藏在第 2 节建立的「三种模型形态」里：导出的 xmodel 是**硬件无关的 IR**，它只说「这个卷积用 int8 算」，却没说「在 `DPUCZDX8G` 这款 DPU 上，这个卷积该拆成哪几条指令、数据怎么在片上 BRAM 与 DDR 之间搬、用几个 MAC 阵列」。**编译**就是完成这层「抽象图 → 具体指令」的翻译。README 一句话说清了这一步的必要性：

> [software/quantization/README.md:70-72](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L70-L72) —— 这段话说明：导出的 `xmodel` 必须针对目标硬件上的**特定 DPU 架构**编译，才能运行。

编译的关键输入是 `arch.json`——它描述目标 DPU「长什么样」。这正是 u1-l3 强调的那个判断：**`arch.json` 是阶段 ④（编译）与阶段 ⑤（硬件平台构建）反向耦合的唯一硬件依赖**。同一份量化 xmodel，配上 KV260 的 `arch.json` 编译，就只能跑在 KV260 的 DPU 上；换一块板子（如 ZCU104），就得换 `arch.json` 重新编译。这条耦合是双向的：硬件侧（u5）把 DPU 配成 `DPUCZDX8G/KV260/arch.json` 所描述的样子，软件侧（本讲）才能用它编译。

#### 4.2.2 核心流程

编译流程（对应 README 第 3 节）：

1. **输入**：导出阶段产出的 IR `xmodel`（如 `nndct_quant/DetectionModel_0_int.xmodel`）。
2. **读 `arch.json`**：编译器加载目标 DPU 的硬件规格（算力、频率、可用指令、片上内存……）。
3. **图编译**：把每个量化算子映射到 DPU 指令，规划数据流（DDR↔片上缓冲），做指令调度。
4. **输出**：在 `-o` 指定目录写出编译产物三件套。

抽象地看，编译做的是「按硬件规格把算子落地」：

\[
\text{IR xmodel（硬件无关）} \xrightarrow[\text{arch.json}]{\text{vai\_c\_xir}} \text{DPU 指令包（硬件专属）}
\]

`arch.json` 里的核心字段（通用 Vitis AI 约定，本仓库不含其内容）大致包括：DPU 架构名（`DPUCZDX8G`）、算力（本项目 B4096，即 4096 个 MAC）、运行频率（325MHz）、是否集成 softmax 等特殊算子单元。这些参数决定了编译器能把图「压」到多紧——u5-l1 会从硬件侧详细讲解。

#### 4.2.3 源码精读

**编译命令**：

> [software/quantization/README.md:74-81](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L74-L81) —— `vai_c_xir` 编译命令。逐参数拆解：
> - `vai_c_xir`：Vitis AI 的 XIR 编译器（**c**ompiler for **xir**）。
> - `-x nndct_quant/DetectionModel_0_int.xmodel`：输入，即 4.1 导出的量化 IR xmodel（注意 `_int`）。
> - `-a /opt/vitis_ai/compiler/arch/DPUCZDX8G/KV260/arch.json`：目标 DPU 的硬件规格文件。路径里 `DPUCZDX8G/KV260` 明确了「DPU 型号 + 开发板」，与 u5-l1 讲的 DPU IP 一一对应。
> - `-o vai_c_output`：输出目录。
> - `-n my_yolov8_model`：编译产物的命名前缀（决定输出 `.xmodel` 的文件名）。

**编译产物**，README 紧接着说明了三件套：

> [software/quantization/README.md:83-83](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L83-L83) —— 这句说明：该命令生成一个 `vai_c_output` 目录，里面有编译后的 `.xmodel`、一个 `meta.json` 文件，以及一个 checksum（校验和）。

三个产物的作用：

| 产物 | 作用 |
| --- | --- |
| `my_yolov8_model.xmodel` | **DPU 指令包**——这才是板上真正运行的模型（形态③）。与输入的 IR xmodel（形态②）同名后缀、内容完全不同。 |
| `meta.json` | 模型元数据：输入/输出张量的形状、名字、布局等，供 Vitis AI 运行时库（`vitis_ai_library`）正确地喂输入、取输出。 |
| `checksum` | 编译产物的校验和，用于校验部署到板上的 `.xmodel` 没有在传输/加载中损坏。 |

注意一个易错点：**输入 xmodel 与输出 xmodel 是两个完全不同的文件**。输入是 `nndct_quant/DetectionModel_0_int.xmodel`（量化 IR），输出是 `vai_c_output/my_yolov8_model.xmodel`（DPU 指令）。下文 4.3 要找输出层名时，**必须用编译后的那份 xmodel**（因为张量名在编译后才会最终确定）。

#### 4.2.4 代码实践

**实践目标**：把 `vai_c_xir` 命令的四个参数与「三种模型形态」对应起来，并解释 `arch.json` 路径里 `DPUCZDX8G/KV260` 这一段为何决定了产物的可移植性。

**操作步骤**：

1. 打开编译命令原文 [software/quantization/README.md:77-81](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L77-L81)。
2. 填下表，把每个参数对应到「形态①/②/③」或「硬件规格」类别：

| 参数 | 取值（README 原文） | 类别 |
| --- | --- | --- |
| `-x`（输入） | `nndct_quant/DetectionModel_0_int.xmodel` | 形态 ② 量化 IR |
| `-a`（架构） | `.../DPUCZDX8G/KV260/arch.json` | 硬件规格 |
| `-o`（输出目录） | `vai_c_output` | 产物落盘位置 |
| `-n`（命名） | `my_yolov8_model` | 形态 ③ 的文件名前缀 |

3. **论证可移植性**：回答「若把同一份输入 xmodel 改用 `DPUCZDX8G/ZCU104/arch.json` 编译，产物能在 KV260 上跑吗？」

**需要观察的现象 / 预期结果**：

- `-x` 吃的是**形态②**（量化 IR），`-n` 决定的产物是**形态③**（DPU 指令）——一条命令完成了 ②→③ 的跃迁。
- 可移植性论证（预期答案）：**不能**。`arch.json` 不同，意味着编译器针对另一款 DPU 的算力/指令/内存布局编排了指令，搬到 KV260 上指令集与资源都对不上，无法运行。这正是 u1-l3 所说的「arch.json 是阶段 ④⑤ 唯一硬件耦合点」——换板必换 arch.json 重编译。

> 说明：`vai_c_xir` 由 Vitis AI 3.5 工具链提供，本仓库不含。本实践产出是**参数-形态映射与可移植性论证**，命令可运行性标注为「待本地（Vitis AI 环境）验证」。

#### 4.2.5 小练习与答案

**练习 1**：编译产物的 `.xmodel` 和导出阶段的 `.xmodel`，哪一个才是板上 Vitis AI 运行时真正加载的？为什么？

> **参考答案**：**编译产物的**那份（`vai_c_output/my_yolov8_model.xmodel`）。因为它是针对 `DPUCZDX8G` 编排好的 DPU 指令包（形态③），运行时库能直接喂给 DPU 执行。导出阶段那份（形态②）是硬件无关 IR，只是编译器的输入，DPU 看不懂、运行时也不会加载它。

**练习 2**：为什么 `meta.json` 对运行时不可或缺？没有它运行时会出什么问题？

> **参考答案**：`meta.json` 记录了模型输入/输出张量的形状、名字与布局。运行时库需要据此：(a) 把预处理后的输入张量按正确形状/布局喂进 DPU；(b) 知道有几个输出张量、各叫什么名、什么形状，才能正确取回结果做后处理。没有它，运行时就不知道该怎么对接 DPU 的输入输出，推理无法进行。它和 4.3 的 `detect_layer_name` 正好衔接——后者就是从编译后 xmodel 里查到的输出张量名。

### 4.3 prototxt 与输出层定位

#### 4.3.1 概念说明

这个模块回答：**编译后的 xmodel 已经能跑了，为什么还要配一个 `model.prototxt`？**

原因是：编译后的 xmodel 只包含「前向计算」本身——它把输入图像跑成一组输出特征图张量，**仅此而已**。但 YOLOv8 的输出是「原始的类别 logit + DFL 距离分布」，要变成「最终检测框」，还需要一整套**后处理**（解码、置信阈值、NMS……）。Vitis AI 运行时库需要一个配置文件来告诉它**后处理的参数**，以及**该从 DPU 输出里取哪几个张量**来后处理——这个配置文件就是 `model.prototxt`。README 第 4 节开头点明了这一点：

> [software/quantization/README.md:87-91](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L87-L91) —— 这段说明：部署前需要创建一个模型配置文件并识别输出层。`model.prototxt` 要与编译后的 `.xmodel` **同名**，里面指定后处理参数。

`model.prototxt` 里最关键、也最容易出错的一个字段是 `detect_layer_name`：它列出 DPU 输出张量的**名字**，运行时据此取出对应的特征图去做后处理。这个名字**不能手写、不能猜**——必须从编译后的 xmodel 里查出来。原因有二：

1. **张量名是编译器生成的，不等于 PyTorch 里的层名**。量化（nndct）和编译（vai_c_xir）会重构计算图、给张量重新命名。你脑子里的「检测头第三层输出」在编译后的图里可能叫 `"DetectionModel_0_relu_..._output"` 之类，不查根本不知道。
2. **取错名字，运行时取不到张量，后处理直接失败**。`detect_layer_name` 与实际输出张量名必须**逐字符一致**，是硬约束。

因此本模块的核心操作就是：用 `xir subgraph` 工具把编译后 xmodel 的 DPU 子图打印出来，从中挑出**标记为 `O`（output）**的张量名，抄进 `prototxt`。

> 与 P2 头的呼应：标准 YOLOv8 在 P3/P4/P5 三个尺度输出，有 3 个检测输出张量；本项目额外加了 P2 高分辨率头（u3-l1、u6-l3），输出张量个数会多一个。`detect_layer_name` 的**条数必须与实际输出头个数一致**——这是本讲新引入的一致性约束（输出头数量一致）。

#### 4.3.2 核心流程

定位输出层名并配 `prototxt` 的流程（对应 README 第 4 节）：

1. **确保 `prototxt` 与编译产物同名**：如编译产出 `my_yolov8_model.xmodel`，配置文件就叫 `my_yolov8_model.prototxt`，放在同一目录。运行时按这个同名约定去配对加载。
2. **查 DPU 子图输出**：运行 `xir subgraph <编译后的.xmodel> | grep DPU`，打印出 DPU 子图；其中输入张量标 `I`、输出张量标 `O`。
3. **抄名字**：把标 `O` 的张量名（通常每个检测尺度一个）复制进 `prototxt` 的 `detect_layer_name` 字段，**逐字符一致**。
4. **核对条数**：`detect_layer_name` 的条数 = 模型输出头个数（本项目含 P2，会比标准 YOLOv8 多一条）。
5. **部署上板**：把含 `.xmodel` + `.prototxt`（+ `meta.json` + `checksum`）的模型目录用 `scp` 拷到板上约定路径。

`xir subgraph` 的输出大致形如（**示意，非本仓库真实输出，张量名为占位**）：

```
... # DPU subgraph ...
  input:  ...__DataTransfer__...        I   <- 输入张量（标 I）
  output: ...__DetectionModel_0_...__output  O   <- 输出张量（标 O），抄这个
  output: ...__DetectionModel_0_...__output  O   <- 另一个尺度的输出
  ...
```

`| grep DPU` 的作用是只看 DPU 子图（编译后的 xmodel 里既有跑在 DPU 上的子图，也有跑在 CPU 上的前后处理子图），缩小排查范围。

#### 4.3.3 源码精读

**查输出层名的命令**：

> [software/quantization/README.md:93-99](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L93-L99) —— 这段给出两步：(a) 用 `xir subgraph yolov8.xmodel | grep DPU` 打印 DPU 子图；(b) 把输出列表里**标 `O`** 的张量名，复制进 `.prototxt` 的 `detect_layer_name` 字段。

这两步合起来回答了本模块的核心问题——`detect_layer_name` 为何必须来自 `xir subgraph` 输出：因为张量名是编译器在重构计算图时生成的，既不等于训练侧的层名、也无法事先预测，唯一可靠的来源就是去问编译后的图本身。

**部署命令**：

> [software/quantization/README.md:101-105](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L101-L105) —— 这段说明：用 `scp -r model/ <user>@<IP>:~/usr/share/vitis_ai_library/models/` 把整个模型目录（含 `.xmodel` + `.prototxt`）拷到板上 Vitis AI 模型库的约定路径。

注意目标路径 `/usr/share/vitis_ai_library/models/`：这是 Vitis AI 运行时库默认查找模型的目录。`scp -r model/` 拷的是**整个模型目录**（不是单个 xmodel）——因为运行时同时需要 `.xmodel`、`.prototxt`、`meta.json`，缺一不可。这部分的实际板载加载与 `xmutil` 验证，留待 u5-l4（固件部署）与 u7（板载推理应用）展开。

> 关于 `prototxt` 模板：README 把 `model.prototxt` 的具体字段指向了一个外部示例（仓库内不含该文件）。`detect_layer_name` 是其中最关键的字段；其余字段（类别数、阈值、anchor/stride 等）属 YOLOv8 后处理通用配置，会在 u6（框架补丁的后处理优化）里与软件后处理一并讲。本讲只聚焦「输出层名怎么来、为什么必须查」。

#### 4.3.4 代码实践

**实践目标**：把「`xir subgraph` 输出 → `detect_layer_name`」这条链路走一遍，并用一个最小 Python 脚本模拟「从子图文本里抽取标 `O` 的张量名」的过程，理解为什么这个名字不能手写。

**操作步骤**：

1. 阅读原文 [software/quantization/README.md:93-99](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L93-L99)，确认「标 `O` 的才抄」这条规则。
2. 运行下面**示例代码**（非仓库代码，仅依赖标准库），模拟从 `xir subgraph` 的文本输出里抽取输出张量名：

```python
# 示例代码：模拟从 xir subgraph 输出中抽取标 O（output）的张量名
import re

# 模拟一段 `xir subgraph yolov8.xmodel | grep DPU` 的输出（占位文本，非真实张量名）
subgraph_text = """
  input:   DataTransfer_x_input                 I
  output:  DetectionModel_0_Conv_245_output      O
  output:  DetectionModel_0_Conv_271_output      O
  output:  DetectionModel_0_Conv_297_output      O
  output:  DetectionModel_0_Conv_323_output      O
"""

def extract_output_tensors(text):
    # 每行形如：  output:  <name>  O
    names = []
    for line in text.splitlines():
        if line.strip().startswith("output:") and line.strip().endswith("O"):
            # 抠出中间的张量名
            m = re.search(r"output:\s+(\S+)\s+O", line.strip())
            if m:
                names.append(m.group(1))
    return names

out_names = extract_output_tensors(subgraph_text)
print("detect_layer_name 应填：")
for n in out_names:
    print("  -", n)
print(f"输出头个数 = {len(out_names)}")
```

3. **思考题**：上面抽到 4 个输出张量名。对照 u3-l1 / u6-l3，本项目用了 P2 多尺度头，比标准 YOLOv8（P3/P4/P5 共 3 个头）多一个。这 4 个张量名是否对得上「P2/P3/P4/P5」？若你漏抄了一个（只抄了 3 个），运行时会怎样？

**需要观察的现象 / 预期结果（待本地验证，规律确定）**：

1. 脚本抽出的张量名都是**编译器生成的长串**（如 `DetectionModel_0_Conv_xxx_output`），完全无法靠记忆手写——这直观印证了「必须查图」。
2. 抽到的名字个数 = 输出头个数。本项目含 P2，应为 4 个（P2/P3/P4/P5 各一）。若漏抄，运行时取不到对应尺度的特征图，该尺度的检测框全部丢失，小目标（靠 P2 高分辨率头捕捉，u6-l3）召回率会明显下降。
3. `detect_layer_name` 必须与图里 `O` 张量名**逐字符一致**——多一个下划线、大小写错一个字母都会导致运行时找不到张量。

> 说明：`xir` 工具由 Vitis AI 工具链提供，本仓库不含；上面的 `subgraph_text` 是为演示解析逻辑构造的**占位文本**，真实张量名需在自己的 Vitis AI 环境里实际运行 `xir subgraph` 获得。脚本本身可在任意 Python3 环境运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `detect_layer_name` 必须从 `xir subgraph` 的输出里取，而不能直接用训练代码里检测头的层名（比如 `Detect.head.0`）？

> **参考答案**：因为量化（nndct）和编译（vai_c_xir）会**重构计算图并重命名张量**。训练代码里的层名是 PyTorch 模块的属性路径，到了编译后的 xmodel 里，张量名变成编译器生成的形式（如 `DetectionModel_0_Conv_xxx_output`），两者没有简单的对应关系。运行时库只认编译后图里的张量名，所以必须以图为准。唯一可靠的来源就是 `xir subgraph` 打印出来的、标 `O` 的名字。

**练习 2**：`model.prototxt` 为什么要和编译后的 `.xmodel` **同名**（仅后缀不同）？运行时是怎么找到它的？

> **参考答案**：因为 Vitis AI 运行时库按「同名约定」配对加载：给定模型名 `my_yolov8_model`，它会去模型目录里找 `my_yolov8_model.xmodel`（DPU 指令）和 `my_yolov8_model.prototxt`（后处理配置），两者必须同基名。若不同名，运行时按模型名找不到对应的 `prototxt`，后处理参数缺失，无法正确解码输出。这是工具链的目录约定，不是可选项。

## 5. 综合实践

**实践目标**：把本讲三个模块串成一条完整的「导出 → 编译 → 部署」命令链，并解释每一步产物的含义与 `detect_layer_name` 的来源——这正是规格里要求的三步序列。

**背景设定**：假设你已完成 u4-l1 的 PTQ 校准（或 u4-l2 的 QAT），手握一个带量化信息的 `best-quantized.pt`，现在要把它一路推到 KV260 板上可加载的模型目录。

**操作步骤**：

1. **第一步：导出**（4.1）。写出导出命令，标注每个关键参数的作用。参考 [software/quantization/README.md:58-60](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L58-L60)：

   ```bash
   yolo detect val data="xview3-vitis.yaml" model=best-quantized.pt \
       nndct_quant=True quant_mode=test batch=1 imgsz=800 \
       dump_xmodel=True dump_onnx=True \
       --nndct_convert_sigmoid_to_hsigmoid --nndct_convert_silu_to_hswish
   ```
   - 产物：`nndct_quant/DetectionModel_0_int.xmodel`（形态②量化 IR）。

2. **第二步：编译**（4.2）。写出 `vai_c_xir` 命令。参考 [software/quantization/README.md:77-81](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L77-L81)：

   ```bash
   vai_c_xir -x nndct_quant/DetectionModel_0_int.xmodel \
             -a /opt/vitis_ai/compiler/arch/DPUCZDX8G/KV260/arch.json \
             -o vai_c_output \
             -n my_yolov8_model
   ```
   - 产物：`vai_c_output/` 目录（形态③）。

3. **解释 `vai_c_output/` 三件套的作用**（参考 [software/quantization/README.md:83-83](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L83-L83)）。填写下表（答案见下方）：

   | 文件 | 作用 |
   | --- | --- |
   | `my_yolov8_model.xmodel` | ? |
   | `meta.json` | ? |
   | `checksum` | ? |

4. **第三步：配 `prototxt` 并定位输出层**（4.3）。
   - 查输出层名：参考 [software/quantization/README.md:93-99](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L93-L99)，执行 `xir subgraph vai_c_output/my_yolov8_model.xmodel | grep DPU`，把标 `O` 的张量名抄进与 `.xmodel` 同名的 `my_yolov8_model.prototxt` 的 `detect_layer_name` 字段。
   - **说明 `detect_layer_name` 为何必须从 `xir subgraph` 输出获取**：写出两条理由（提示：张量名是编译器重构图后生成的、不等于训练侧层名；取错名字运行时取不到张量）。

5. **第四步：部署上板**。参考 [software/quantization/README.md:101-105](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/quantization/README.md#L101-L105)：

   ```bash
   scp -r vai_c_output/ <target-user>@<target-IP>:~/usr/share/vitis_ai_library/models/my_yolov8_model
   ```

**预期结果（参考答案要点）**：

- 三件套作用：
  - `my_yolov8_model.xmodel` —— **DPU 指令包**，板上真正运行的模型（形态③，区别于导出的形态② IR）。
  - `meta.json` —— 模型输入/输出张量的形状、名字、布局元数据，运行时据此正确喂输入、取输出。
  - `checksum` —— 编译产物校验和，校验部署/加载过程中文件未损坏。
- `detect_layer_name` 必须来自 `xir subgraph` 的两条理由：(1) 量化与编译会重构计算图、重命名张量，输出张量名是编译器生成的、既不等于训练侧层名也无法预测；(2) 运行时按这些名字从 DPU 输出里取张量做后处理，名字错一个字符就取不到、后处理失败。所以唯一可靠来源是直接问编译后的图（标 `O` 的张量）。
- 一致性核对：导出与编译用的输入尺寸 `imgsz=800`、batch=1 必须与训练/切片/板上推理一致；`detect_layer_name` 条数必须等于输出头个数（本项目含 P2，比标准 YOLOv8 多一条）。

> 说明：本实践产出是一份**完整命令链 + 产物解释文档**。所有命令的真实可运行性依赖 Vitis AI 3.5 环境与一块 KV260 板（本仓库不含），标注为「待本地验证」。部署后的板载加载（`xmutil`、`xdputil query`）见 u5-l4；推理应用如何消费这个模型目录见 u7。

## 6. 本讲小结

- **模型有三种形态，别混淆**：浮点 `.pt`（①）→ 量化 `xmodel`（②，硬件无关 IR）→ 编译 `xmodel`（③，KV260 专属 DPU 指令）。② 和 ③ 都叫 `.xmodel` 却是两样东西。
- **导出 = 固化量化图**：`yolo detect val ... quant_mode=test batch=1 dump_xmodel=True dump_onnx=True`，用校准算好的 scale 把模型固化成 IR；背后是 `validator.py` 的 `export_xmodel(deploy_check=True)` 与 `export_onnx_model(dynamic_batch=True)`。
- **编译 = 把 IR 翻译成 DPU 指令**：`vai_c_xir -x <IR> -a <arch.json> -o <dir> -n <name>`；`arch.json`（`DPUCZDX8G/KV260`）是阶段④⑤唯一的硬件耦合点，换板必换 arch.json 重编译。
- **编译产物三件套**：`.xmodel`（DPU 指令，板上运行）、`meta.json`（输入输出张量元数据）、`checksum`（校验和）。
- **`detect_layer_name` 必须查图获取**：编译器重构图后重命名张量，输出层名既不等于训练侧层名也无法预测，必须用 `xir subgraph <xmodel> | grep DPU` 取标 `O` 的张量名，逐字符填进与 `.xmodel` 同名的 `model.prototxt`。
- **一致性暗线再次显现**：导出仍需 `imgsz=800` + 两个 `--nndct_convert_*` 标志与校准/QAT 一致；且新增「输出头数量一致」——本项目 P2 头使 `detect_layer_name` 比标准 YOLOv8 多一条。

## 7. 下一步学习建议

本讲走完了「量化模型上板」的最后三跳，软件工具链到此结束。接力点如下：

- **如果你想知道硬件侧怎么把 DPU 配成 `arch.json` 描述的样子** → 进入 **u5-l1（KV260 MPSoC 与 DPU 硬件架构）**：讲 `DPUCZDX8G` IP、B4096 算力、325MHz 频率与资源利用率，从硬件侧印证本讲 `-a arch.json` 的含义。
- **如果你想看板载怎么加载这个模型目录** → 进入 **u5-l4（固件制作与板载部署）**：讲 `xmutil loadapp` 加载固件、`xdputil query` 验证 DPU，以及模型目录在板上的最终落位。
- **如果你关心 `prototxt` 里后处理参数怎么在 C++ 侧落地** → 进入 **u6（Vitis AI 推理框架补丁）**：尤其是 u6-l3（YOLOv8 后处理优化与 P2 架构），它会讲 `yolov8_post_process` 如何处理 P2 多输出头，与本讲的 `detect_layer_name` 条数直接对应。
- **如果你想用这个模型目录真正跑出预测** → 进入 **u7（板载推理应用）**：`xview3_benchmark.cpp` / `xview3_performance.cpp` 会加载本讲产出的 `.xmodel` + `.prototxt`，跑出 JSON 预测与 FPS。

建议阅读顺序：u5-l1 → u5-l4 → u6 → u7。本讲产出的「编译后 `.xmodel` + `model.prototxt`」是 u6/u7 在板上消费的直接输入——把这两份文件喂给打过补丁的 Vitis AI 运行时库，整条端到端流水线就贯通了。
