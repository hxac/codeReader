# 仓库目录结构：六大目录各司其职

## 1. 本讲目标

本讲承接上一讲对 FREE-TPU V3+ 的定位介绍，把视线从「它是什么」转向「它的代码长什么样」。

读完本讲，你应该能够：

1. 说出仓库顶层 `constr`、`doc`、`hardware`、`ip_repo`、`script`、`sdk` 六个目录各自的职责。
2. 区分哪些目录是「二进制交付物」（拿来即用、不可读），哪些是「可读源码」（可以打开学习与修改）。
3. 在后续学习中，根据自己要解决的问题，快速定位该进入哪个目录。

本讲只建立**整体地图**，不深入任何目录的内部实现——那是后续每一讲要做的事。

## 2. 前置知识

在开始看目录之前，先用三段话补齐几个嵌入式 FPGA 项目里常见的概念。如果你已经熟悉，可以跳过。

- **ZynqMP SoC**：FREE-TPU V3+ 跑在 Xilinx Zynq UltraScale+（ZynqMP）上。这是一颗「SoC」，把 **ARM 处理器（PS, Processing System）** 和 **FPGA 可编程逻辑（PL, Programmable Logic）** 集成在同一颗芯片里。TPU IP 放在 PL 里，ARM 上的软件通过地址映射去驱动它。
- **IP 核（IP Core）**：在 FPGA 世界里，「IP」指一段可复用的硬件设计模块，通常用 Verilog/VHDL 写成。FREE-TPU 本身就是一个 IP 核。商用 IP 往往以**加密**形式交付，使用者只能把它当成「黑盒」集成进自己的工程，看不到内部 RTL。
- **Bitstream / BOOT.BIN / xsa**：FPGA 需要一份「配置数据」才能变成你想要的电路，这份配置数据叫 **bitstream**。在 ZynqMP 上，把 bitstream、ARM 启动镜像等打包到一起的产物常叫 **BOOT.BIN**；而 **xsa** 是 Vivado 导出的「硬件设计包」，包含 PL 的 bitstream 和 PS 的地址映射，软件工程师据此编写驱动。

理解了这三点，下面六个目录为什么这样划分就一目了然了。

## 3. 本讲源码地图

本讲的核心依据是仓库根目录的 `README.md`，其中有一张「Directory Structure」表，是官方对六个目录的权威说明。此外我们会打开每个目录里的一个「代表文件」，让目录不再只是名字，而是落到具体文件上。

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目总说明，含六大目录的官方定义表 |
| `sdk/Readme.md` | SDK 目录内部的进一步说明（编译器/运行库/demo/standalone） |
| `constr/top.xdc` | FPGA 引脚与 bitstream 约束（constr 目录代表文件） |
| `script/create_prj.sh` | 创建 Vivado 工程的启动脚本（script 目录代表文件） |
| `ip_repo/EEP_DVP_Top_128B_v6p3.v` | 加密 IP 的 Verilog 顶层（ip_repo 目录代表文件） |

## 4. 核心概念与源码讲解

### 4.1 目录一览表：仓库的全局地图

#### 4.1.1 概念说明

FREE-TPU V3+ 仓库横跨「FPGA 硬件」与「软件 SDK」两个领域，但顶层只分了六个目录。这种划分不是随意的：它对应了一条从「拿到 IP → 集成进 FPGA 工程 → 上板启动 → 用软件驱动推理」的完整链路。先把这六个目录记住，后面所有讲义都能对号入座。

#### 4.1.2 核心流程

仓库顶层可以按职责粗分成三组：

```text
硬件侧：ip_repo(加密 IP) + script(建工程) + constr(约束)  → 产出可烧录的 hardware 交付物
上板侧：hardware(BOOT.BIN / image.ub / xsa)             → 直接烧到 ZynqMP 板卡
软件侧：sdk(编译器 + 运行库 + demo + standalone)         → 在 ARM 上驱动 TPU 推理
说明侧：doc(中英文使用手册)                             → 任何环节卡住都回这里查
```

也就是说：`ip_repo` + `script` + `constr` 是「造硬件」的输入，`hardware` 是它们的产物，`sdk` 是「用硬件」的软件，`doc` 是全程的说明书。

#### 4.1.3 源码精读

README 中对六个目录的官方定义集中在这一段：

[README.md:L55-L63](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/README.md#L55-L63) —— 这张表是六个目录职责的权威说明：`constr` 是引脚约束、`doc` 是使用文档、`hardware` 是 xsa 与预编译 BOOTbin、`ip_repo` 是加密 FPGA IP、`script` 是建 Vivado 工程的脚本、`sdk` 是 standalone 与 linux 的 demo。

README 在更上方还列出了「IP package」包含的五样东西，与六个目录一一对应，可以互相印证：

[README.md:L27-L32](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/README.md#L27-L32) —— 这里明确：加密 IP 对应 `ip_repo`、Linux/Bare Metal demo 设计对应 `hardware`、SDK 与编译器对应 `sdk`。

把这两段对照看，目录划分的逻辑就清楚了。

#### 4.1.4 代码实践

1. **实践目标**：用只读 git 命令把仓库的真实文件清单打印出来，亲手确认六个目录里到底有什么。
2. **操作步骤**：在仓库根目录执行
   ```bash
   git ls-files | sed 's#/.*##' | sort | uniq -c | sort -rn
   ```
   这条命令会列出每个顶层目录（及根目录文件）的 tracked 文件数量。
3. **需要观察的现象**：`sdk` 目录的文件数会远多于其他目录（约占整个仓库的绝大多数），而 `constr` 只有 1 个文件、`hardware` 只有 3 个文件。
4. **预期结果**：你会得到一张「目录 → 文件数」的直方图，直观印证「软件 sdk 是主体，硬件交付物是少量大文件」的判断。
5. 若想看完整树形结构，可再执行 `git ls-files`（不带管道）逐行浏览全部路径。

#### 4.1.5 小练习与答案

**练习 1**：README 的目录表里没有单独列出 `LICENSE` 和 `README.md` 这两个根目录文件，为什么？

> **答案**：它们不属于任何一个功能目录，而是整个仓库层面的元信息——`LICENSE` 声明许可协议，`README.md` 是项目入口说明，因此直接放在根目录。

**练习 2**：如果只能记住一个目录名，后续学习软件驱动 TPU 推理时最该记住哪个？

> **答案**：`sdk`。它同时包含编译器、运行库、demo 和裸机工程，是「用 TPU」这条主线的全部软件入口。

### 4.2 硬件相关目录：constr / hardware / ip_repo / script

#### 4.2.1 概念说明

这四个目录共同回答一个问题：**怎么把 TPU IP 变成一块能启动的板子？** 它们分别提供：加密 IP 本体（`ip_repo`）、把 IP 集成进工程的脚本（`script`）、引脚与 bitstream 约束（`constr`），以及最终可以直接烧录的产物（`hardware`）。前三者是「输入」，`hardware` 是「产物」。

#### 4.2.2 核心流程

```text
ip_repo/EEP_DVP_Top_*.v  ──(被脚本例化)──►  Vivado 工程
script/create_prj.sh + system_rtl_*.tcl  ───┘
constr/top.xdc  ──(约束引脚/bitstream属性)──►  综合/实现
                                          ↓
                              hardware/BOOTbin/BOOT.BIN + image.ub + xsa/system_wrapper.xsa
```

注意：`ip_repo` 里的核心 TPU IP 是**加密**的，使用者看不到内部 RTL，只能当黑盒集成；只有 `EEP_DVP_Top_128B_v6p3.v` 这类外设包装层是可见的 Verilog。

#### 4.2.3 源码精读

`constr/top.xdc` 全文只有两行，却定了两项重要的 bitstream 属性：

[constr/top.xdc:L1-L2](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/constr/top.xdc#L1-L2) —— `BITSTREAM.READBACK.SECURITY level2` 关闭回读以保护设计，`BITSTREAM.GENERAL.COMPRESS true` 开启 bitstream 压缩减小体积。

`script/create_prj.sh` 同样只有一行，作用是调用 Vivado 以 TCL 模式跑后面的工程脚本：

[script/create_prj.sh:L1-L1](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/script/create_prj.sh#L1-L1) —— 它把 Vivado 安装路径下的可执行文件以 `-mode tcl -source $1` 方式启动，`$1` 就是同目录下的 `system_rtl_*.tcl` 工程描述脚本。

`ip_repo/EEP_DVP_Top_128B_v6p3.v` 文件开头是一串 Xilinx 加密指令，说明这个可见的 Verilog 文件本身也是受保护的 IP：

[ip_repo/EEP_DVP_Top_128B_v6p3.v:L1-L13](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/ip_repo/EEP_DVP_Top_128B_v6p3.v#L1-L13) —— ``pragma protect`` 系列指令表明该 RTL 被 Xilinx Encryption Tool 加密，禁止网表导出、禁止探测、仅允许生成 bitstream，是典型的商用 IP 黑盒交付。

`hardware` 目录下则是已经造好的产物：`BOOTbin/BOOT.BIN`（启动镜像）、`BOOTbin/image.ub`（Linux 内核与设备树）、`xsa/system_wrapper.xsa`（Vivado 硬件导出包）。没有源码可读，只能拿来用或烧录。

#### 4.2.4 代码实践

1. **实践目标**：解读加密 IP 的命名，体会「文件名即规格」的工程习惯。
2. **操作步骤**：查看 `ip_repo` 目录，会看到一个分卷压缩包 `EEPTPU_M1024_N1_C8_ef16int8_ZU15EG_FOREVAL1h.zip`（以及 `.z01`/`.z02`/`.z03` 分卷）。
3. **需要观察的现象**：把文件名按 `M1024_N1_C8_ef16int8_ZU15EG_FOREVAL1h` 拆开读。
4. **预期结果**：可解读出大致含义——`ef16int8` 表示支持 FP16 与 INT8 精度（与上一讲「免费版仅开放 FP16/INT8」一致），`ZU15EG` 对应目标器件 Zynq UltraScale+ ZU15EG，`FOREVAL1h` 暗示免费评估版的一小时停机限制。具体字段定义以 `doc` 手册为准，无法确认的标注「待确认」。
5. 再打开 `constr/top.xdc`，确认全文仅两行 `set_property`，对应上面讲的两条 bitstream 属性。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ip_repo` 里的核心 IP 看不到 RTL，却还要放进仓库？

> **答案**：因为 Vivado 工程在综合/实现时需要把这些加密 IP 文件作为输入参与流程；使用者虽看不到内部实现，但仍需提供文件本体让工具链使用。

**练习 2**：`script/create_prj.sh` 里写的是 `[Vivado install path]/...`，这说明什么？

> **答案**：脚本没有硬编码 Vivado 的绝对路径，使用者需要把 `[Vivado install path]` 替换为自己机器上 Vivado 2021.1 的实际安装路径后才能运行。

### 4.3 软件目录 sdk：编译器、运行库与 demo

#### 4.3.1 概念说明

`sdk` 是仓库里**可读源码最集中**的目录，也是本学习手册后续大部分讲义的工作场。它把「用 TPU 做推理」这件事拆成四个角色：把框架模型编译成 TPU bin 的**编译器**、加载 bin 并驱动推理的**运行库**、教你怎么调用的 **demo 示例**，以及不依赖 Linux 的**裸机 standalone** 方案。

#### 4.3.2 核心流程

`sdk` 内部对应一条清晰的数据流主线：

```text
框架模型(cfg/weights/onnx)
        │  eeptpu_compiler（编译器，二进制工具）
        ▼
    *.pub.bin（TPU 可执行模型）
        │  libeeptpu_pub（运行库 API）   ── Linux 路线
        │  或 standalone 工程的 EEPTPU_SA ── 裸机路线
        ▼
   demo（classify / yolo / icnet ...）做输入预处理、推理、后处理、可视化
```

- **Linux 路线**：用 `libeeptpu_pub` 提供的 `EEPTPU` 类，在 ARM Linux 上加载 bin 推理，代表是 `demo/classify`。
- **裸机路线**：不跑 Linux，直接在 ARM 上跑 `standalone/src` 里的 C++ 工程，通过寄存器驱动 TPU，代表是 `standalone/src/main.cc`。

#### 4.3.3 源码精读

`sdk/Readme.md` 用四段话点明了四个子目录的分工：

[sdk/Readme.md:L1-L20](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/Readme.md#L1-L20) —— 依次说明 `eeptpu_compiler`（支持 caffe/darknet/pytorch(onnx)/ncnn/keras，生成推理用 bin）、`libeeptpu_pub`（支持 ARM32/AARCH64/X86 的 API 接口库）、`demo`（分类/检测/分割等推理示例）、`standalone`（yolov4-tiny 模型与编译脚本 + 裸机 C++ 源码）。

注意这几个子目录的「可读性」并不相同：

- 可读源码：`demo/`（各 demo 的 `main.cpp`/`compile.sh`/`test.sh`）、`standalone/src/`（裸机 C++ 工程）、`standalone/net_model/scripts/`（编译脚本与 `setting.ini`）。
- 二进制工具：`eeptpu_compiler/eeptpu_compiler`、`standalone/net_model/compiler/eeptpu_compiler`、`standalone/net_model/eepBinCvt/eepBinCvt`（这三个是可执行工具，不可读）、`libeeptpu_pub/libeeptpu_pub_v0.7.1.tar.gz`（压缩包形式的库）。

#### 4.3.4 代码实践

1. **实践目标**：把 `sdk` 的四个子目录对应到「编译 → 加载 → 推理」主线上的阶段。
2. **操作步骤**：执行 `git ls-files sdk` 浏览 `sdk` 下的全部路径，重点找这几类文件：编译器可执行文件、`setting.ini` 编译配置、各 demo 的 `main.cpp`、`standalone/src/main.cc`。
3. **需要观察的现象**：你会看到 `sdk/demo/` 下有 `classify`、`yolo`、`icnet`、`multi_bins_test`、`nntpu_test` 五个 demo，以及一个公共的 `common/`（图像库 `eepimg_v0.2.6` 与 npy 加载库 `npy`）。
4. **预期结果**：能在脑中（或纸上）画出「`net_model/scripts` 编译 → 生成 bin → `demo/*/main.cpp` 加载推理」的箭头。
5. 若想确认某个文件是不是可读文本，可用 `git show HEAD:<路径> | head` 瞥一眼前几行（只读操作）。

#### 4.3.5 小练习与答案

**练习 1**：`sdk/eeptpu_compiler/eeptpu_compiler` 和 `sdk/standalone/net_model/compiler/eeptpu_compiler` 看起来是同名工具，为什么放两份？

> **答案**：一份服务于 Linux 路线（直接在主机上编译模型生成 bin 给 `libeeptpu_pub` 用），一份服务于裸机路线（配合 `standalone/net_model/scripts` 进一步用 `eepBinCvt` 转成 `eepnet.h`/`.mem`）。它们是同一工具在不同路线下的部署副本，具体版本以手册为准。

**练习 2**：如果你想学「TPU 寄存器到底怎么驱动」，应该进 `sdk` 下哪个子目录？

> **答案**：`sdk/standalone/src/eeptpu/`，尤其是 `eeptpu_sa.cpp` 与 `interface/eep_interface.cpp`，它们是裸机侧直接操作寄存器的代码（后续 u4-l2、u4-l3 会精读）。

### 4.4 文档目录 doc：使用手册与运行说明

#### 4.4.1 概念说明

`doc` 目录全是 PDF，没有源码，但它是整个项目的「说明书」。README 里反复出现「Please refer to the document」「More details, please visit」之类的指引，指的就是这里。当你在硬件或软件任何一环卡住，第一反应应是回 `doc` 查手册。

#### 4.4.2 核心流程

`doc` 下的 PDF 大致分三类，覆盖从上板到开发的全过程：

```text
上板运行类： demo_readme.pdf / demo_readme-English.pdf         —— 怎么烧录、怎么跑 demo
IP 评估类：  EEP-TPU FPGA IP evaluation.pdf (中英)             —— IP 能力与评估版说明
开发手册类： eep-ug050 编译器使用手册 (中英)                   —— 怎么编译模型
             eep-ug053 API 使用手册 (中英)                     —— 怎么写推理代码
```

中英文成对出现是本目录的一个显著特征。

#### 4.4.3 源码精读

README 中「Run steps」一行直接把上板流程指向了文档：

[README.md:L52-L53](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/README.md#L52-L53) —— 「Please refer to the document.」说明运行步骤不在 README 里展开，而是交给 `doc/` 下的手册（首推 `demo_readme.pdf`）。

仓库实际提供的文档（由 `git ls-files doc` 可得）包括：`demo_readme.pdf`、`demo_readme-English.pdf`、`EEP-TPU FPGA IP evaluation.pdf`、`EEP TPU FPGA IP评估版使用手册.pdf`、`eep-ug050 EEP-TPU Compiler User Manual_230201.pdf`（及中文版）、`eep-ug053 EEP-TPU Application Programming Interface (API) User Manual_pub230201.pdf`（及中文版）。这些就是上面三类手册的具体落点。

#### 4.4.4 代码实践

1. **实践目标**：为后续上板与开发建立「文档索引」。
2. **操作步骤**：执行 `git ls-files doc` 列出全部 PDF，把中英文版本两两配对。
3. **需要观察的现象**：每本核心手册几乎都有中英两个版本；编号 `ug050`/`ug053` 是 EEP 的用户手册序列号。
4. **预期结果**：得到一张「手册编号 → 主题 → 中文文件名 / 英文文件名」的对照表，例如 `ug050 → 编译器 → eep-ug050 ...编译器使用手册_230201.pdf / eep-ug050 ...Compiler User Manual_230201.pdf`。
5. 由于 PDF 内容需在本地打开阅读，具体章节标题标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：拿到板子后想第一步跑通 demo，应该先读 `doc` 下哪本？

> **答案**：`demo_readme.pdf`（或英文版 `demo_readme-English.pdf`），它是上板运行步骤的入口手册。

**练习 2**：`ug050` 和 `ug053` 分别对应 SDK 里的哪两个子目录？

> **答案**：`ug050` 是编译器手册，对应 `sdk/eeptpu_compiler`（及 `standalone/net_model/scripts` 的编译流程）；`ug053` 是 API 手册，对应 `sdk/libeeptpu_pub` 与 `sdk/standalone/src/eeptpu` 的接口调用。

## 5. 综合实践

把本讲所有内容串起来，完成一份「仓库地图说明书」。

1. **实践目标**：产出一棵标注完整的目录树，并给每个顶层目录打上两个标签——「二进制交付物 / 可读源码」和「所属链路（硬件 / 上板 / 软件 / 文档）」。
2. **操作步骤**：
   - 执行 `git ls-files` 获取真实文件清单。
   - 基于清单画出顶层目录树（展开到二层即可），形如：
     ```text
     FREE-TPU-V3plus-for-FPGA/
     ├── README.md, LICENSE          （根说明）
     ├── constr/   └─ top.xdc
     ├── doc/      └─ *.pdf (8 份，中英成对)
     ├── hardware/ ├─ BOOTbin/{BOOT.BIN, image.ub}
     │             └─ xsa/system_wrapper.xsa
     ├── ip_repo/  ├─ EEP_DVP_Top_128B_v6p3.v
     │             └─ EEPTPU_M1024_..._FOREVAL1h.zip (+ .z01/.z02/.z03)
     ├── script/   ├─ create_prj.sh
     │             └─ system_rtl_..._v202101.tcl
     └── sdk/      ├─ Readme.md, eeptpu_compiler/, libeeptpu_pub/
                   ├─ demo/{classify,yolo,icnet,multi_bins_test,nntpu_test,common}
                   └─ standalone/{net_model, src}
     ```
   - 为每个目录写一句中文说明，并标注标签。
3. **需要观察的现象**：二进制交付物集中在 `hardware/`、`ip_repo/`（压缩包）、`sdk/eeptpu_compiler/`、`sdk/libeeptpu_pub/`、`sdk/standalone/net_model/{compiler,eepBinCvt}`；可读源码集中在 `sdk/demo/`、`sdk/standalone/src/`、`script/`、`constr/`。
4. **预期结果**：一张表或一棵树，让人一眼看出「想学软件进 `sdk/demo` 或 `sdk/standalone/src`，想上板用 `hardware`，想建工程看 `script`+`constr`+`ip_repo`，卡住了查 `doc`」。
5. 把这份地图保存到自己的学习笔记里——后续每一讲开头都会指向其中某个目录。

## 6. 本讲小结

- 仓库顶层六个目录各司其职：`constr` 约束、`doc` 文档、`hardware` 上板交付物、`ip_repo` 加密 IP、`script` 建工程脚本、`sdk` 软件。
- `ip_repo` + `script` + `constr` 是「造硬件」的输入，`hardware` 是它们的产物，`sdk` 是「用硬件」的软件，`doc` 是全程说明书。
- 核心加密 IP 以 Xilinx ``pragma protect`` 加密形式交付，只能当黑盒集成，看不到内部 RTL。
- `sdk` 是可读源码最集中的目录，分编译器、运行库、demo、standalone 四部分，对应「编译出 bin → 加载 bin 推理」的主线。
- 区分「二进制交付物」与「可读源码」是高效学习的关键：读源码进 `sdk/demo` 与 `sdk/standalone/src`，用工具则直接调用 `eeptpu_compiler`、`libeeptpu_pub` 等。
- `doc` 下手册与 `sdk` 子目录一一对应：`ug050` 对编译器、`ug053` 对 API、`demo_readme` 对上板。

## 7. 下一步学习建议

有了全局地图后，下一步建议沿着「从能跑起来 → 到看懂原理」的顺序推进：

1. **先看上板**：进入 [u1-l3 硬件交付物与上板运行]，搞清楚 `hardware/` 里的 `BOOT.BIN`、`image.ub`、`xsa` 怎么烧到 ZynqMP 板卡并启动 Linux。
2. **再看工程构建**：进入 [u1-l4 FPGA 工程构建与 IP 集成]，了解 `script/` 与 `constr/` 如何把 `ip_repo` 里的加密 IP 组装成可综合的 Vivado 工程。
3. **然后转软件**：进入 [u2-l1 SDK 全景]，从 `sdk/Readme.md` 出发，正式开始软件侧的学习主线。

如果你手头暂时没有板卡，可以跳过 u1-l3，直接从 u1-l4 的脚本与约束阅读开始，再进入 u2 的软件 demo——后者只需交叉编译环境即可上手。
