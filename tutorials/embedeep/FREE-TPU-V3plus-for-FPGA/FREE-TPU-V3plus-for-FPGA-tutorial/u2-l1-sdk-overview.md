# SDK 全景：编译器、运行库、demo 与 standalone

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `sdk/` 目录下 **eeptpu_compiler、libeeptpu_pub、demo、standalone** 四个部分各自的职责，以及它们之间的协作关系。
- 复述 SDK 的主线：**框架模型 → eeptpu_compiler 编译成 TPU bin → 运行库加载 bin → 完成推理**。
- 区分两条部署路线：**Linux demo 路线**（用 libeeptpu_pub 高层 API）与 **裸机 standalone 路线**（直接读写 TPU 寄存器），并知道各自适合什么场景。

本讲是「地图课」：不深挖任何一个模块的实现细节（那是后面 U3–U8 各讲的任务），而是先把整张 SDK 地图画清楚，让你知道后续每一讲落在地图的哪个位置。

## 2. 前置知识

在进入 SDK 之前，请先确认你已经理解了上一讲（u1-l2）建立的仓库地图，尤其是下面几个概念：

- **TPU bin**：TPU 硬件能直接「吃下去」执行的模型文件。深度学习框架（Caffe/Darknet/PyTorch/ONNX/NCNN/Keras）训练出来的模型，**不能**直接给 TPU 跑，必须先经过编译，变成 TPU 专属的二进制格式。
- **推理（inference）**：把一张图片送进已经编译好的网络，得到分类得分 / 检测框 / 分割掩码等结果的过程。和「训练」相对。
- **PS / PL**：ZynqMP 芯片里 ARM 处理器部分叫 PS，FPGA 可编程逻辑叫 PL。TPU IP 放在 PL，软件在 PS 上通过地址映射去驱动它（详见 u1-l3、u1-l4）。
- **Linux 路线 vs 裸机（bare-metal / standalone）路线**：PS 上既可以跑一个完整的 Linux 操作系统，也可以「什么都不跑」，上电后直接执行一个 ELF 程序。前者开发方便、有文件系统和库；后者延迟更低、对硬件控制更直接。SDK 同时提供了这两条路线的代码。

如果你对「为什么模型要编译」「PS 怎么访问 PL」还模糊，建议回头复习 u1-l2 和 u1-l4 再继续。

## 3. 本讲源码地图

本讲主要围绕下面几个文件建立 SDK 的整体认知：

| 文件 | 作用 |
| --- | --- |
| `sdk/Readme.md` | SDK 的总说明书，用四段话点明四个部分的职责。本讲的「骨架」。 |
| `sdk/demo/classify/main.cpp` | Linux 路线下最简单的分类 demo，展示了 libeeptpu_pub 的典型调用顺序。 |
| `sdk/demo/classify/compile.sh` | demo 的交叉编译脚本，揭示 libeeptpu_pub 如何被链接进 demo。 |
| `sdk/standalone/src/main.cc` | 裸机路线的入口，展示 standalone 工程与 Linux demo 的形态差异。 |
| `sdk/standalone/src/config.h` | 裸机路线的关键编译开关与硬件地址宏，体现「软件魔法地址由硬件定死」。 |

另外，了解这几个目录的存在有助于建立地图：`sdk/eeptpu_compiler/`（编译器可执行文件）、`sdk/libeeptpu_pub/`（运行库压缩包）、`sdk/demo/`（各 demo 子目录）、`sdk/standalone/`（裸机工程与编译脚本）。

## 4. 核心概念与源码讲解

在看四个部分之前，先用一张「主线图」把全局串起来。SDK 里所有代码都服务于同一条流水线：

```text
   框架模型                编译器                   运行库 / 裸机驱动
┌────────────┐  eeptpu_compiler  ┌──────────┐  libeeptpu_pub 或 standalone
│ .cfg/.weights│ ──────────────▶ │ TPU bin  │ ──────────────────────────▶  推理结果
│ .onnx/.caffemodel│             │( *.pub.bin)│   load_bin → set_input       (分类/检测/分割)
└────────────┘                   └──────────┘  → forward → 读结果
```

- **左端**是你在 PC 上用 Darknet/Caffe/PyTorch 训练出的模型文件。
- **中间**是 `eeptpu_compiler`，它把模型翻译成 TPU 能执行的 `*.pub.bin`。
- **右端**有两种「驾驶员」：在 Linux 上是 `libeeptpu_pub` 这个高层 C++ 库；在裸机上是 `standalone` 工程里直接读写寄存器的 C++ 代码。两者都做同一件事——加载 bin、送输入、触发推理、读结果。

记住这条主线，下面四个小节就是把它逐段拆开。

### 4.1 eeptpu_compiler：把框架模型编译成 TPU bin

#### 4.1.1 概念说明

`eeptpu_compiler` 是 SDK 的「翻译官」。深度学习框架五花八门（Caffe、Darknet、PyTorch/ONNX、NCNN、Keras……），它们各自的模型格式 TPU 硬件都不认识。编译器的作用就是把任意一种框架模型，统一翻译成 TPU 硬件专属的执行二进制（`*.pub.bin`）。

为什么必须编译、不能直接跑框架模型？因为 TPU 是**固定数据流（dataflow）架构**（见 u1-l1），它需要预先知道：每一层算子的执行顺序、张量在片上/片外内存的存放地址、数据复用（Only Take Once）的调度方式、量化到 FP16/INT8 的缩放系数。这些信息必须在「编译期」全部确定并烧进 bin 里，运行时 TPU 才能按既定流程高速推进。所以 bin 不只是「权重」，它还包含了完整的**调度表和地址表**。

#### 4.1.2 核心流程

编译一次模型，大致经历：

1. **读入框架模型**：根据输入是 `.cfg/.weights`（darknet）、`.onnx`（pytorch）、`.caffemodel`（caffe）等，解析网络结构。
2. **算子映射与图优化**：把框架算子映射到 TPU 支持的上百种算子，做融合、消除等图优化。
3. **量化**：把浮点权重/激活按指定精度（默认 FP16，可选 INT8）量化定点化，同时记录 mean/norm 等预处理参数。
4. **地址与调度规划**：为每层张量分配 TPU 片上/片外内存地址，规划 dataflow 执行顺序、线程数（`--tpu_threads`）。
5. **输出 `*.pub.bin`**：把权重 + 调度表 + 地址表 + 输入输出 shape 等打包成一个 bin。

#### 4.1.3 源码精读

`sdk/Readme.md` 第一段就交代了编译器的定位与支持的框架：

- [sdk/Readme.md:L3-L5](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/Readme.md#L3-L5) —— 这段说明 `eeptpu_compiler` 支持 caffe、darknet、pytorch(onnx)、ncnn、keras 等框架，产出供 EEP-TPU API 做推理的 bin。

编译器本体是一个可执行文件，仓库里以二进制形式交付：

- `sdk/eeptpu_compiler/eeptpu_compiler`（顶层，供 Linux 路线使用）
- `sdk/standalone/net_model/compiler/eeptpu_compiler`（裸机路线自带一份）

裸机路线还附带了完整的编译脚本与配置，是观察编译器「怎么被调用」的最佳入口：

- [sdk/standalone/net_model/scripts/b_yolo4tiny.sh:L90-L100](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/net_model/scripts/b_yolo4tiny.sh#L90-L100) —— 这里把 darknet 的 `yolov4_tiny.cfg` / `.weights`、mean/norm、类别表（`--extinfo classes=...`）拼成一条编译命令，交给 `${compiler}` 执行。注意它还带了 `--input_folder`，说明编译器可以顺便用一批图做量化/仿真。

编译命令的各个开关（精度、线程数、是否量化仿真等）集中在配置文件里：

- `sdk/standalone/net_model/scripts/setting.ini` —— `b_yolo4tiny.sh` 用 `read_ini` 从这里读出 `compiler`、`model_root`、`global_cmd`、`bin_name`。`global_cmd` 这一行就是编译器的核心命令行参数（如 `--public_bin --hybp`、`--int8`、`--tpu_threads` 等）。这些参数的逐项解读是 U3 的内容，这里只需知道「编译器参数都集中在 setting.ini」。

#### 4.1.4 代码实践

**实践目标**：在不实际运行编译器的前提下，通过阅读脚本搞清楚一次编译需要哪些输入、产出什么。

**操作步骤**：

1. 打开 `sdk/standalone/net_model/scripts/b_yolo4tiny.sh`，定位到第 90–100 行。
2. 列出这条命令用到的全部「输入文件」：cfg、weights、单张图、输入文件夹、类别表。
3. 找出命令里出现的预处理参数 `--mean` 和 `--norm`，记下它们的值。
4. 结合脚本第 109–112 行的 `mv ./${bin_name}` 推断：编译成功后产物文件名是什么、被移动到哪个目录。

**需要观察的现象 / 预期结果**：

- 输入文件至少包含：`yolov4_tiny.cfg`、`yolov4_tiny.weights`、一张 `004545.bmp`、一个 `images/ssd/` 文件夹、一段 `classes=...` 类别表。
- `--mean '0.0,0.0,0.0'` 表示三通道均值都为 0；`--norm '0.003921569,...'` 注意 \(0.003921569 \approx 1/255\)，即把像素从 \([0,255]\) 归一化到 \([0,1]\)。
- 产物是 `setting.ini` 里 `bin_name` 指定的文件，被移动到 `${binDir}/${netName}/` 下。

> 待本地验证：如果你手头有 Linux 环境与编译器权限，可执行 `bash b_yolo4tiny.sh` 真正跑一次编译，观察是否生成 bin；没有环境则按上面做「源码阅读型实践」即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能把 Darknet 的 `.weights` 直接拷到板卡上让 TPU 跑？

**参考答案**：TPU 是 dataflow 架构，运行前需要预先确定算子调度、张量内存地址、量化系数等，这些都在编译期由 `eeptpu_compiler` 算好并烧进 bin。`.weights` 只有权重，缺少调度表和地址表，TPU 硬件无法直接执行。

**练习 2**：`--norm 0.003921569` 这个数字是怎么来的？

**参考答案**：\(1/255 \approx 0.0039216\)。它把 8 位像素值（0–255）线性映射到 0–1 的浮点区间，是常见的图像归一化系数。

### 4.2 libeeptpu_pub：跨平台推理 API 接口库

#### 4.2.1 概念说明

编译器产出 bin 后，谁来「开车」？在 Linux 路线下，这件事交给 `libeeptpu_pub`。它是一个封装好的 C++ 动态库，对外提供一个 `EEPTPU` 类，把「设地址、加载 bin、送输入、触发推理、读结果、测耗时」这一整套硬件操作，收拢成几个高层方法。用户不需要关心寄存器、不需要关心 AXI 总线，照着 demo 调方法即可。

它的关键卖点是**跨平台**：同一套 API，通过不同的编译器（`arm-linux-gnueabihf-g++` / `aarch64-linux-gnu-g++` / `x86 g++`）可以编出分别跑在 ARM32、ARM64、x86 上的程序，对应 ZynqMP 的 32 位/64 位 Linux，以及带 PCIe/XDMA 卡的 x86 主机。库本身以压缩包形式交付：

- `sdk/libeeptpu_pub/libeeptpu_pub_v0.7.1.tar.gz`

#### 4.2.2 核心流程

libeeptpu_pub 的典型使用顺序（也是 demo 里的顺序）：

1. `EEPTPU::init()` 拿到单例对象。
2. 配置接口与地址：`eeptpu_set_interface(SOC 或 PCIE)`、`eeptpu_set_tpu_reg_zones(...)`、`eeptpu_set_base_address(...)`（告诉库 TPU 寄存器区和数据内存在哪里）。
3. `eeptpu_load_bin(path)` 加载编译好的 bin（库会自动解析出输入输出 shape）。
4. `eeptpu_set_input(data, c, h, w, 0)` 送入一帧图像。
5. `eeptpu_forward(result)` 触发推理，结果回填到 `result`。
6. `eeptpu_get_tpu_forward_time()` 读取硬件纯计算耗时。
7. `eeptpu_close()` 收尾。

#### 4.2.3 源码精读

`sdk/Readme.md` 第二段定位了 libeeptpu_pub 的作用与平台支持：

- [sdk/Readme.md:L7-L10](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/Readme.md#L7-L10) —— 明确它就是 EEP-TPU 的 API 接口，用户通过加载编译器产出的 bin 来做推理，且支持 ARM32、AARCH64、X86。

最能体现这套 API 的，是 classify demo 的初始化与主循环。库对外头文件叫 `eeptpu.h`（demo 第一行 `#include "eeptpu.h"`），核心对象是一个 `EEPTPU *tpu`：

- [sdk/demo/classify/main.cpp:L29-L90](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L29-L90) —— `eeptpu_init()` 函数完整展示了配置流程：先 `tpu->init()`（第 33 行），再设 reg zone 与 base address，最后 `tpu->eeptpu_load_bin(path_bin)`（第 81 行）。注意第 58 行 `zone.addr = 0xA0000000` —— 这个寄存器基地址和裸机侧 `config.h` 里的 `EEPTPU_REG_BASE_ADDR` 完全一致，印证了 u1-l4 的结论：软件里的「魔法地址」全由硬件设计定死。
- [sdk/demo/classify/main.cpp:L113-L171](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L113-L171) —— 这是真正「推理一次」的片段：`eeptpu_set_input` 送图（第 113 行）、`eeptpu_forward(result)` 触发推理（第 165 行）、`eeptpu_get_tpu_forward_time()` 读硬件耗时（第 170 行）。`input_shape[1/2/3]` 分别是通道数/高/宽，由 `load_bin` 自动解析。

库如何被链接进 demo，看编译脚本最清楚：

- [sdk/demo/classify/compile.sh:L17-L42](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/compile.sh#L17-L42) —— 第 17–26 行按 `32/64/86` 选择交叉编译器；第 37–39 行 `-I../libs/${pf}/eep/include`（头文件）、`-L../libs/${pf}/eep/lib` 与 `-leeptpu_pub`（链接库）。这说明解压 `libeeptpu_pub_v0.7.1.tar.gz` 后会得到一个 `libs/<平台>/eep/` 目录，里面是头文件和动态库。

#### 4.2.4 代码实践

**实践目标**：仅看源码，把 libeeptpu_pub 的调用顺序排成一张「时序表」。

**操作步骤**：

1. 打开 `sdk/demo/classify/main.cpp`。
2. 在第 29–90 行的 `eeptpu_init` 中，按出现先后，依次记录每个 `tpu->...` 调用的方法名与所在行号。
3. 在第 124–194 行的 `main` 中，记录 `load_bin` 之后的「送输入 → 推理 → 读耗时」三步对应的方法与行号。

**预期结果**：你应该得到类似这样的时序表——

| 顺序 | 方法 | 行号 | 作用 |
| --- | --- | --- | --- |
| 1 | `tpu->init()` | 33 | 取单例 |
| 2 | `eeptpu_set_tpu_reg_zones` | 60 | 设寄存器区 |
| 3 | `eeptpu_set_base_address` | 63 | 设数据基地址 |
| 4 | `eeptpu_set_interface` | 74 | 选 SOC/PCIE |
| 5 | `eeptpu_load_bin` | 81 | 加载 bin |
| 6 | `eeptpu_set_input` | 113 | 送输入图 |
| 7 | `eeptpu_forward` | 165 | 推理 |
| 8 | `eeptpu_get_tpu_forward_time` | 170 | 读硬件耗时 |

> 待本地验证：若已在板上部署，可用 `bash test.sh` 实跑，对比打印里 `hw cost` 与 `forward ok, cost time(hw+sw)` 的差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `load_bin` 之后，demo 能直接用 `tpu->input_shape[...]` 知道网络要多大的输入图？

**参考答案**：因为 bin 里打包了输入输出的 shape 信息（编译器在编译期就确定了网络结构），`eeptpu_load_bin` 会把这些信息解析出来填到 `tpu->input_shape`，所以 demo 第 97、102 行可以直接据此决定按 BGR 还是 GRAY 加载图片、按多大尺寸 resize。

**练习 2**：同一份 `main.cpp`，怎么做到既能编出 ARM64 程序又能编出 x86 程序？

**参考答案**：源码与 API 完全一致，平台差异只由 `compile.sh` 第 17–26 行切换的编译器（`aarch64-linux-gnu-g++` vs `g++`）和第 37–38 行切换的 `libs/${pf}/...` 目录决定，库本身为每个平台都提供了对应的 `.so`。

### 4.3 demo：Linux 下的开箱即用示例集

#### 4.3.1 概念说明

光有库还不够，用户要能照着抄。`demo` 目录就是一组「开箱即用」的完整示例，覆盖了三种最常见的视觉任务：**分类（classify）、目标检测（yolo）、语义分割（icnet）**，外加两个进阶示例。每个 demo 都是一个可独立编译运行的小工程，包含 `main.cpp`、`compile.sh`、`test.sh` 和输入图片，是学习 libeeptpu_pub 用法最直接的教材。

#### 4.3.2 核心流程

每个 demo 的使用流程高度一致：

1. 先用 `eeptpu_compiler` 把对应模型编译成 `*.pub.bin`（或从网盘下载预编译 bin）。
2. `bash compile.sh 64`（或 `32`/`86`）交叉编译出可执行文件 `demo`。
3. 把 bin、可执行文件、输入图拷到板卡。
4. `bash test.sh` 运行，看推理结果。

README 特别说明：如果不想自己编译，可以直接从百度网盘下载预编译好的 demo 与 bin（地址见 README）。

#### 4.3.3 源码精读

- [sdk/Readme.md:L12-L15](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/Readme.md#L12-L15) —— 点明 demo 覆盖 classify、object detection、semantic segmentation 等任务，并给出百度网盘的预编译下载地址。

仓库里 `sdk/demo/` 下实际存在的子目录（见 `git ls-files`）正好对应这三类任务加两个进阶示例：

| 子目录 | 任务类型 | 说明 |
| --- | --- | --- |
| `classify/` | 分类 | 最简单，4.2 节已精读 |
| `yolo/` | 目标检测 | 输出每行一个检测框 |
| `icnet/` | 语义分割 | 输出逐像素类别 |
| `multi_bins_test/` | 进阶 | 同时跑分类+检测两个网络、用多核 |
| `nntpu_test/` | 进阶 | 多输入网络、从 `.npy` 加载输入 |
| `common/` | 共享工具 | `eepimg`（图像加载/缩放/画框）、`npy`（numpy 文件读取），被各 demo 复用 |

每个 demo 的运行方式以 `test.sh` 为准：

- [sdk/demo/classify/test.sh:L1-L3](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/test.sh#L1-L3) —— 一行命令就把 bin 和输入图喂给可执行文件：`./demo ${bins}eeptpu_s2_mobilenet_v1.pub.bin ${input}`。从文件名 `eeptpu_s2_mobilenet_v1.pub.bin` 可以看出 bin 命名规律：`eeptpu_<方案>_<网络名>.pub.bin`，这里的 `s2` 对应 setting.ini 里的一种编译方案（U3 会详解）。

#### 4.3.4 代码实践

**实践目标**：横向对比三个 demo，归纳出它们的共同结构。

**操作步骤**：

1. 分别打开 `sdk/demo/classify/main.cpp`、`sdk/demo/yolo/main.cpp`、`sdk/demo/icnet/main.cpp` 的开头（前 30 行）。
2. 对比三者的 `#include`、`eeptpu_init`、`main` 主结构。
3. 找出三者**完全相同**的部分（初始化、送输入、forward）和**各自不同**的部分（后处理：分类取 topk、检测画框、分割着色）。

**预期结果**：你会发现三者的前半段几乎一样，差异只集中在 `forward` 之后的「结果解读」——这正是后续 U6（后处理算法）要展开的内容。这条对比能帮你确认：**SDK 把「推理」和「后处理」解耦得很好**，换网络只动后处理。

> 待本地验证：若有板卡，可分别跑三个 demo 的 `test.sh`，观察输出形态（分类打印 top5、检测生成画框图、分割生成彩色叠加图）。

#### 4.3.5 小练习与答案

**练习 1**：`common/` 目录里的 `eepimg` 和 `npy` 两个子库，分别解决 demo 的什么需求？

**参考答案**：`eepimg` 负责图像侧——加载 jpg/bmp、resize 到网络输入尺寸、画检测框/分割色、保存结果图；`npy` 负责数据侧——把 Python/NumPy 存的 `.npy` 张量读进来当输入（`nntpu_test` 用到）。两者都被多个 demo 复用，所以放在 `common/` 下。

**练习 2**：为什么 README 要提供百度网盘的「预编译 bin」下载？

**参考答案**：因为编译需要 `eeptpu_compiler` 与原始模型文件，且编译耗时、有平台要求；提供预编译 bin 让没有编译环境的用户也能直接上板体验推理，降低上手门槛。

### 4.4 standalone：寄存器级裸机方案

#### 4.4.1 概念说明

`standalone` 是 SDK 的另一条路线——**不跑 Linux，上电后直接执行一个裸机 C++ 程序**。它和 demo 做的事最终一样（加载网络、推理、出结果），但实现层次完全不同：demo 调 `libeeptpu_pub` 的高层方法，standalone 则**自己直接读写 TPU 的寄存器和内存**，连「触发推理」都是往 `STARTUP` 寄存器写一个启动值、再轮询 `STATUS` 寄存器等完成。

为什么要提供裸机路线？因为裸机没有操作系统开销，能拿到更低、更稳定的延迟，也方便和外设（DVP 摄像头、DP 显示、SD 卡）做硬实时联动。代价是开发更底层、要自己处理缓存、中断、内存布局。standalone 工程用 Xilinx Vitis 构建，跑在 PS 的 ARM 核上。

standalone 目录分两块：

- `net_model/`：模型与编译脚本（含一份独立的 `eeptpu_compiler`、`eepBinCvt`、`setting.ini`、`b_yolo4tiny.sh`）——裸机侧用的不是 `.pub.bin`，而是用 `eepBinCvt` 再把 bin 转成 `eepnet.h` / `eepnet.mem` 这种可直接 `#include` 或从 SD 卡加载的形式（U3 详解）。
- `src/`：裸机 C++ 工程源码（`main.cc`、`eeptpu_sa.*`、平台/中断/摄像头/SD 等驱动）。

#### 4.4.2 核心流程

standalone 程序的整体形态是一个**串口菜单**：

1. 上电 → 平台初始化（cache、中断控制器、外设）。
2. 把网络数据（`eepnet`）加载到 DDR 指定地址。
3. 进入 `while` 菜单循环，等用户在串口输入数字：
   - `1`：采集/读取一帧图像
   - `2`：跑一次 forward，打印结果
   - `3`：把图像存到 SD 卡
   - `4`：读取测试图
   - `5`：连续 demo（采集→推理→显示）
   - `0`：退出

#### 4.4.3 源码精读

- [sdk/Readme.md:L17-L19](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/Readme.md#L17-L19) —— 点明 standalone 的 `net_model`（yolov4-tiny 模型与编译脚本）与 `src`（裸机 C++ 代码）两部分。

standalone 与 demo 最直观的差异，在于它直接持有一个**裸机版 TPU 对象** `EEPTPU_SA eepsa`（SA = Stand-Alone），而不是库里的 `EEPTPU`：

- [sdk/standalone/src/main.cc:L91-L98](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L91-L98) —— 全局声明 `EEPTPU_SA eepsa;`，以及 `eepnet`、`eepinput_addr` 等裸机侧直接操作的内存指针。这里没有 `#include "eeptpu.h"`，取而代之的是 `eeptpu/eeptpu_sa.h`、`net_data/eepnet.h`。

它的交互形态是串口菜单：

- [sdk/standalone/src/main.cc:L322-L341](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L322-L341) —— `while(exit_flag != 1)` 菜单循环，用 `xil_printf` 打印 1–5 选项，`inbyte()` 读串口输入，`switch(choice)` 分支处理。注意 `3: Save Image to SD Card` 被 `#ifdef SD_CARD_IS_READY` 包起来——是否支持 SD 卡由编译开关决定。

而这些编译开关与硬件地址，集中在 `config.h`：

- [sdk/standalone/src/config.h:L25-L45](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L25-L45) —— 第 25–26 行 `EEPTPU_MEM_BASE_ADDR 0x31000000`、`EEPTPU_REG_BASE_ADDR 0xA0000000`，与 demo 里 `classify/main.cpp` 第 58、63 行设的地址一脉相承（u1-l3、u1-l4 已讲过这两个地址的硬件来源）。第 40–45 行用 `NetType_*` 枚举和 `NET_TYPE` 宏选择网络类型——这正是裸机侧「换网络」的开关（U8 移植实践会用）。

#### 4.4.4 代码实践

**实践目标**：把 standalone 与 Linux demo 的「同一件事、两种做法」对照清楚。

**操作步骤**：

1. 在 `sdk/standalone/src/main.cc` 第 322–341 行，列出菜单的全部选项及触发的功能。
2. 对比 `sdk/demo/classify/main.cpp` 的 `main`（无菜单、命令行参数、一次性推理）与 standalone 的菜单循环，各写一句话概括两者的交互方式。
3. 在 `sdk/standalone/src/config.h` 第 25–45 行，找出三个「会因换板卡/换网络而改动」的宏。

**需要观察的现象 / 预期结果**：

- 菜单选项：1 取一帧、2 forward、3 存 SD（条件编译）、4 读测试图、5 连续 demo、0 退出。
- demo 是「命令行一次性运行」；standalone 是「串口交互式常驻菜单」。
- 可移植宏示例：`EEPTPU_MEM_BASE_ADDR`、`EEPTPU_REG_BASE_ADDR`（换板卡/改地址映射时改）、`NET_TYPE`（换网络类型时改）。

> 待本地验证：若有 ZynqMP 板卡与 Vitis 环境，可把 standalone 工程编译成 ELF 烧到板卡，在串口（115200）里输入数字观察菜单响应；无硬件则做源码阅读型实践。

#### 4.4.5 小练习与答案

**练习 1**：standalone 为什么要把「网络」也放在 `net_model/` 里，和 `src/` 分开？

**参考答案**：因为网络数据（`eepnet.h`/`eepnet.mem`）是由编译器+`eepBinCvt`**生成**的产物，体积大、可替换、不属于工程源码逻辑；和驱动源码 `src/` 分开，换网络时只动 `net_model/` 重新生成数据，不碰工程代码，结构更清晰。

**练习 2**：同样是「设 TPU 寄存器基地址」，demo 第 58 行写 `0xA0000000`，standalone `config.h` 第 26 行也写 `0xA0000000`，两者为什么一致？

**参考答案**：因为两者跑在同一块板卡、同一套硬件设计上，TPU 寄存器区在地址空间里的位置由 Vivado 工程的 `assign_bd_address` 决定（见 u1-l4），对软件是固定的契约。无论走 Linux 还是裸机，这个地址都必须一致，否则就访问不到 TPU。

## 5. 综合实践

**任务**：画一张完整的 **SDK 数据流图**，把本讲四个部分串起来。

要求：

1. 从一个**框架模型**出发（例如 Darknet 的 `yolov4_tiny.cfg` / `.weights`）。
2. 标出它经过 `eeptpu_compiler` 编译后变成什么产物（`*.pub.bin`）。
3. 画出**两条分支**：
   - **Linux 路线**：bin 被 `libeeptpu_pub` 的 `load_bin` 加载，在某个 `demo`（如 classify）里完成推理。
   - **裸机路线**：bin 先经 `eepBinCvt` 转成 `eepnet.h`/`eepnet.mem`，被 `standalone` 工程加载，在 `main.cc` 菜单里完成推理。
4. 在图的**每个节点**上标注它对应的**仓库目录**（例如编译器 → `sdk/eeptpu_compiler/` 或 `sdk/standalone/net_model/compiler/`；运行库 → `sdk/libeeptpu_pub/`；demo → `sdk/demo/classify/`；裸机 → `sdk/standalone/src/`）。
5. 在图的**每条边**上用一句话标注「这一步做了什么」（例如「编译：量化+调度+地址规划」「转换：bin → 可 include 的头/可加载的 mem」「加载 bin：解析出 input/output shape」）。

**提示**：可以参考本讲第 4 节开头的那张简化主线图，再把它扩展成带两条路线、带目录标注的完整版。画完后，你应该能用这张图回答「我想换一个网络，需要动 SDK 的哪些部分」——答案是：只动最左端的模型和编译器参数，中间产物重新生成，右端的 demo/standalone 代码基本不动（除非后处理逻辑不同）。

## 6. 本讲小结

- `sdk/` 由 **eeptpu_compiler（编译器）、libeeptpu_pub（运行库）、demo（Linux 示例）、standalone（裸机工程）** 四部分组成，README 用四段话点明了各自职责。
- 主线是：**框架模型 → eeptpu_compiler 编译成 `*.pub.bin` → 运行库/裸机驱动加载 bin → 推理**。
- `eeptpu_compiler` 支持 caffe/darknet/pytorch(onnx)/ncnn/keras，产出含权重+调度表+地址表+shape 的 TPU bin；调用方式可参考 `b_yolo4tiny.sh`。
- `libeeptpu_pub` 提供 `EEPTPU` 高层类（init/load_bin/set_input/forward），一套 API 跨 ARM32/ARM64/x86，链接方式见 `compile.sh`。
- `demo` 覆盖分类/检测/分割三类任务（classify、yolo、icnet）加两个进阶示例（multi_bins_test、nntpu_test），共享 `common/` 下的 eepimg/npy 工具。
- `standalone` 是不跑 Linux 的寄存器级方案，用 `EEPTPU_SA` 直接驱动硬件、串口菜单交互，网络数据放在 `net_model/`、工程代码在 `src/`，关键开关集中在 `config.h`。

## 7. 下一步学习建议

本讲建立了 SDK 的「四宫格」地图，接下来的学习建议：

- **想先跑通最简单的 Linux 推理** → 进入 **u2-l2（eepimg 图像工具库）** 和 **u2-l3（EEPTPU 运行库 API 与 classify demo）**，亲手读懂 demo 的每一行。
- **想搞清楚 bin 是怎么编译出来的** → 进入 **U3（模型编译链路）**，从 `setting.ini`、`b_yolo4tiny.sh`、`eepBinCvt` 逐层拆解。
- **想深入硬件层、玩裸机** → 进入 **U4（裸机 standalone 路径）**，看 `EEPTPU_SA` 如何通过寄存器协议驱动 TPU。

建议的阅读顺序是 u2-l2 → u2-l3（先把 Linux demo 路线走通），再按需选择 U3（编译）或 U4（裸机）深入。
