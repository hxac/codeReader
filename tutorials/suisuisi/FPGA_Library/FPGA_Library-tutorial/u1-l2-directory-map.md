# 目录结构与模块地图

## 1. 本讲目标

上一讲（u1-l1）我们已经用「读 README + 看目录 + 看 git 历史」三步法，对 `suisuisi/FPGA_Library`（旧名 `Xilinx_Library`）建立了总体印象：它是一个聚合型 FPGA 设计合集，由三大顶层目录组成。

本讲把那张「印象地图」精确化为一张「施工地图」。学完后你应当能够：

1. **画出仓库的三层目录结构图**，并能说出 `HDL/`、`HLS/`、`ThreePart/` 各自承载哪一类设计。
2. **精确定位关键源码**：例如 AES 加密核心、HLS 中值滤波、projf Verilog 库分别放在哪里。
3. **区分「自研源码」与「第三方合集」**，并知道每一块各自的许可证来源与可复用边界。

本讲是后续所有讲义的「导航页」——后面读任何一篇，都可以先回到这张地图确认位置。

---

## 2. 前置知识

在进入目录之前，先用一句话厘清几个容易混的术语（更细的解释见 u1-l1）：

- **FPGA**：可现场编程的硬件芯片，本仓库的设计最终都要烧写（bitstream）到 Xilinx 7 系列、Lattice iCE40 这类 FPGA 上。
- **HDL**（硬件描述语言）：用 Verilog / VHDL 直接描述电路，是「传统手写」路线。
- **IP 核**（Intellectual Property core）：一段可复用的硬件设计模块。在 Xilinx 生态里，IP 通常被打包成可在 Vivado 里直接拖拽的「盒子里」组件（带 `component.xml` 描述）。
- **HLS**（高层综合）：用 C/C++ 写算法，由工具（Vivado HLS / Vitis HLS）自动生成 Verilog，是「软件思维写硬件」路线。
- **AXI**：ARM 提出的片上总线协议，处理器（如 MicroBlaze / ARM）通过它和硬件 IP 通信。

> 关键认知：同一个仓库里，`HDL/` 走「手写 HDL → 打包 IP」路线，`HLS/` 走「C 综合 → IP」路线，`ThreePart/` 则是把别人做好的东西直接收录进来。这三条路线的目录组织方式完全不同——这正是本讲要讲清楚的重点。

---

## 3. 本讲源码地图

本讲主要阅读以下说明性文件（它们各自描述了自己所在子项目的组织方式）：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 仓库根说明（极简，仅一句话） |
| `LICENSE` | 仓库根许可证（MIT，仅覆盖作者自研部分） |
| `HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/readme.md` | Vivado 工程模板的目录组织约定（最权威的「目录怎么分」文档） |
| `ThreePart/projf-explore/lib/README.md` | projf Verilog 库的功能分区说明 |
| `ThreePart/digilent_ip/README.md` | Digilent IP 库的安装说明 |
| `ThreePart/hardwarebee/README.md` | hardwarebee 合集的来源说明 |
| `ThreePart/ISOIEC18033-3StandardBlock/README.md` | ISO 标准密码 HDL 合集的来源说明 |

> 说明：本讲是「读目录」而不是「读算法」，所以「源码精读」部分引用的多是 **README / 目录树** 这类元信息文件。真正的算法源码精读放在 Unit 2 之后。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 顶层目录划分**：仓库的三层骨架从何而来。
- **4.2 自研源码目录组织**：`HDL/` 与 `HLS/` 两条自研路线如何摆放文件。
- **4.3 第三方合集目录组织**：`ThreePart/` 下四个子项目各自的来源与结构。

---

### 4.1 顶层目录划分：仓库的三层骨架

#### 4.1.1 概念说明

一个大型仓库往往包含「自己写的」和「别人写的」两类代码。把它们混在一起会让许可证管理和后续维护变得混乱。本仓库的做法是：**用一个目录区分一条「技术路线」**。

仓库根目录只有两个信息源：一份极简的 `README.md`，和一份 MIT `LICENSE`。我们先看它们：

README.md 内容（[README.md:L1-L2](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/README.md#L1-L2)）只说了一件事——「这是 Vivado IP 合集，包括图像处理等」：

> `# Xilinx_Library`
> ` Vivado诸多IP，包括图像处理等`

LICENSE（[LICENSE:L1-L3](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/LICENSE#L1-L3)）声明：

> `MIT License`
> `Copyright (c) 2023 suisuisi`

由于根 README 信息极少，**仓库的真实结构必须靠「列目录」来理解**——这正是 u1-l1 传授的方法论在本讲的落地。

#### 4.1.2 核心流程

仓库根目录的实体构成如下（实测）：

```
FPGA_Library/
├── README.md          极简说明
├── LICENSE            MIT（Copyright 2023 suisuisi）
├── HDL/               ① 自研 HDL 源码与 IP（手写硬件路线）
├── HLS/               ② 高层综合（C → RTL 路线）
└── ThreePart/         ③ 第三方合集（别人写好、收录进来）
```

三层骨架的划分逻辑可以总结成一张对照表：

| 顶层目录 | 技术路线 | 内容性质 | 谁写的 |
| --- | --- | --- | --- |
| `HDL/` | 手写 Verilog/VHDL + 打包 Vivado IP | 自研 | 仓库作者 suisuisi |
| `HLS/` | C/C++ 算法 → HLS 综合 → IP | 自研（算法源）+ 工具生成 | 仓库作者 suisuisi |
| `ThreePart/` | 混合（学术 / 厂商 / 博客） | 第三方收录 | 各原始作者 |

> 一个重要的许可证边界：**根目录的 MIT 许可证只覆盖作者自研部分**（主要是 `HDL/` 与 `HLS/` 里的算法源码）。`ThreePart/` 下每个子项目都有自己的来源和许可证，复用前必须逐个确认——这一点 u1-l1 已强调过，本讲会在 4.3 给出每个子项目的具体来源。

#### 4.1.3 源码精读

仓库的三层划分并非随手而为。`HDL/` 下的 AES 核心随附了一份非常正式的「Vivado 工程模板」文档，其中开篇就引用了 Xilinx 官方手册 UG892，说明作者有意把工程按 Xilinx 推荐的方式做最小化版本控制（[HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/readme.md:L5-L14](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/readme.md#L5-L14)）：

> 这张表把一个 Vivado 工程拆成若干「零件」（Project/Block design/Design sources/Constraints/SDK/Custom IP…），并规定**哪些纳入 Git 版本控制、哪些由脚本重建**。

这说明仓库的目录组织是有方法论支撑的，而不是随意堆放。理解了「工程按角色分目录」这个思想，4.2 节看到 AES 核心里那一长串 `proj/`、`src/`、`sdk/`、`ip_repo/` 时就不会犯晕。

#### 4.1.4 代码实践

**实践目标**：用只读命令亲自验证三个顶层目录的「体量分布」，建立感性认识。

**操作步骤**（在仓库根目录执行，均为只读命令）：

```bash
# 1. 统计三个顶层目录各自的文件数量
for d in HDL HLS ThreePart; do
  echo "$d: $(git ls-files "$d" | wc -l) 个文件"
done

# 2. 看每个顶层目录的直接子目录
ls -1 HDL
ls -1 HLS
ls -1 ThreePart
```

**需要观察的现象**：三个目录的文件数量通常会有明显差异（`ThreePart/` 因为收录了大量第三方工程，文件往往最多）。

**预期结果**：

- `HDL/` 下应有 5 个子目录：`AesCryptoCore_1.0`、`DVI_TX`、`axi_dynclk_v1_0`、`color_space`、`ov5640_cap_data`。
- `HLS/` 下应有 2 个子目录：`2D-median-filter-algorithm-HLS`、`edge_canny_detector`。
- `ThreePart/` 下应有 4 个子目录：`ISOIEC18033-3StandardBlock`、`digilent_ip`、`hardwarebee`、`projf-explore`。

> 若你得到与上面不同的子目录列表，说明本讲义所基于的 HEAD（`1e33525`）与你本地的版本不一致，请以本地实际结果为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么仓库作者要把自研代码（`HDL/`、`HLS/`）和第三方代码（`ThreePart/`）分开放，而不是都丢进一个 `src/`？

**参考答案**：主要为了**许可证隔离**与**维护边界清晰**。根 MIT 许可证只覆盖作者自研部分；第三方代码各有各的许可与来源，单独成目录后，复用者只需去 `ThreePart/` 下逐项确认，不会误以为整库都是 MIT。此外，第三方代码更新频率、提交规范都和自研不同，物理隔离可避免相互污染。

**练习 2**：根 `README.md` 只有一句话，你怎么快速搞清楚仓库到底有什么？

**参考答案**：遵循 u1-l1 的三步法——(1) 读根 README（已知极简）；(2) 用 `ls` / `git ls-files` 列目录，画出结构树；(3) 用 `git log --oneline` 看提交历史（例如本仓库最近的提交 `add projf-explore`、`AesCryptoCore` 就直接点出了主要模块）。本讲正是第 (2) 步的展开。

---

### 4.2 自研源码目录组织：HDL 与 HLS

#### 4.2.1 概念说明

`HDL/` 和 `HLS/` 都是作者自研，但两者的「目录长相」差别很大：

- **`HDL/`**：每个设计都是一个「Vivado 工程模板」——目录里既有 RTL 源码，也有 IP 封装、约束、SDK 软件工程、重建脚本。这是因为手写 HDL 最终要在 Vivado 里综合、上板。
- **`HLS/`**：每个设计的核心是 **C 源码 + 测试激励**；当算法被综合成 IP 后，才会出现一个标准 IP 目录（`component.xml`、`hdl/` 等）。

#### 4.2.2 核心流程

`HDL/` 下实测有 5 个子目录，其中 **AES 核心是全手册的教学主线**，另外 4 个共同构成一条「摄像头采集 → 颜色转换 → DVI 输出」的视频链路：

```
HDL/
├── AesCryptoCore_1.0/    AES-128 加解密核心（教学主线，封装为 AXI IP）★
├── ov5640_cap_data/      OV5640 摄像头采集 IP            ┐
├── color_space/          颜色空间转换（RGB↔HSV/YCbCr/RYB…）│ 视频链路
├── axi_dynclk_v1_0/      AXI 动态时钟 IP（给显示供像素时钟）│
└── DVI_TX/               DVI 发送器 IP（TMDS 编码 + 10:1 串化）┘
```

`HLS/` 下实测有 2 个子目录：

```
HLS/
├── 2D-median-filter-algorithm-HLS/   2D 中值滤波：C 源码 + 测试 CSV（Apache-2.0）★
└── edge_canny_detector/              Canny 边缘检测：已综合为成品 IP（IP/ + sorce/）
```

> 带 ★ 的两个是后续讲义（Unit 2-3、Unit 4）的精读对象，本讲只需知道它们的位置和角色。

#### 4.2.3 源码精读

**（1）AES 核心的目录组织——这是全仓库最值得学习的一份目录范本。**

它的 Vivado 工程模板文档直接画出了「理想目录树」（[HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/readme.md:L41-L96](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/readme.md#L41-L96)），核心思想是：

> `proj/` 只放 `create_project.tcl` 和清理脚本（重建工程的唯一版本化入口）；`src/hdl/` 放设计源码、`src/constraints/` 放约束、`sdk/` 放软件工程、`repo/` 放自定义 IP。

把这套约定映射到 AES 工程的实际目录（实测）：

```
HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/
├── readme.md              Vivado 工程模板说明（UG892 工作流）
├── proj/                  create_project.tcl + cleanup 脚本（重建工程的入口）
├── hdl/                   ★ 设计源码（读码的重点）
│   ├── src/               AES 顶层与各变换：aes_top.v / aes_s_box.v / aes_mix_columns.v …
│   ├── utils/             辅助运算：aes_function_g.v / x_times.v / aes_types.v …
│   ├── gf_s_box/          复合域 S-Box：gf_inv_8 / gf_mul_4 / gf_scl_4 …
│   ├── tb/                Verilog 单元 testbench（tb_s_box.v 等）
│   └── VE_sv/             SystemVerilog 验证环境（面向对象验证）
├── ip_repo/AesCryptoCore_1.0/   自定义 IP 封装
│   ├── component.xml      IP 清单（版本/接口/文件）
│   ├── hdl/               AXI 包装：AesCryptoCore_v1_0.v + S00_AXI + S_AXI_INTR
│   └── drivers/           裸机 C 驱动（AesCryptoCore.h/.c/selftest）
├── src/                   工程级约束 / 块设计 tcl
├── sdk/                   软件应用与 BSP
├── repo/                  本地 IP / 板级仓库
└── hw_handoff/            硬件交付（.hdf）
```

这张表回答了初学者最常问的两个问题：

- **「我该去哪里读 AES 的算法？」** → `hdl/src/`（核心变换）和 `hdl/gf_s_box/`（S-Box 的复合域实现）。
- **「AES 是怎么变成一个能被处理器调用的 IP 的？」** → `ip_repo/AesCryptoCore_1.0/`（AXI 包装 + C 驱动）。

注意 `hdl/` 与 `ip_repo/.../hdl/` 是两个不同的 `hdl/`：前者是**算法 RTL**，后者是 **AXI 接口包装**。这个区分是 Unit 2 与 Unit 3 的分界线。

**（2）HLS 目录的组织——以「C 源码」为中心。**

`HLS/2D-median-filter-algorithm-HLS/` 实测包含：`MedianFilter.c`（算法）、`MedianFilter.h`（类型定义）、`main_test.c`（测试激励）、`clean.csv` / `noisy.csv`（测试图像数据）、`vivado_hls.app`（HLS 工程文件）、`LICENSE`（Apache-2.0）。

这里没有 `proj/create_project.tcl` 那套 Vivado 工程模板，因为 HLS 工程由 `vivado_hls.app` 描述；它的许可证也独立为 **Apache-2.0**（与根 MIT 不同），再次印证「逐目录确认许可证」的必要性。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：把上面的「目录角色」对照表用到真实文件上，确认你已能区分算法 RTL 与 AXI 包装。

**操作步骤**：

```bash
# 1. 列出 AES 算法源码目录（hdl/src）
ls -1 HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src

# 2. 列出 AES 的 IP 包装目录（ip_repo/.../hdl）
ls -1 HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl
```

**需要观察的现象**：

- 第 1 条命令应列出 `aes_top.v`、`aes_s_box.v`、`aes_mix_columns.v`、`aes_key_schedule.v` 等纯算法文件。
- 第 2 条命令应只列出 3 个文件：`AesCryptoCore_v1_0.v`、`AesCryptoCore_v1_0_S00_AXI.v`、`AesCryptoCore_v1_0_S_AXI_INTR.v`。

**预期结果**：你能清楚说出 `aes_top.v` 属于「算法 RTL」，而 `AesCryptoCore_v1_0.v` 属于「AXI 包装顶层」。这正是 Unit 2（数据通路）与 Unit 3（IP 封装）的分界。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `HDL/AesCryptoCore_1.0/.../hdl/src/` 和 `ip_repo/.../hdl/` 都叫 `hdl/`，但内容完全不同？

**参考答案**：它们服务于不同阶段。前者是 **AES 算法本身的 RTL**（SubBytes、MixColumns 等数学变换）；后者是 **把算法包成 IP 时加的 AXI 外壳**（寄存器读写、中断）。算法 RTL 不关心总线，AXI 外壳不关心 AES 数学。两层分离让算法可以独立仿真、也方便换一种总线重新封装。

**练习 2**：`HLS/2D-median-filter-algorithm-HLS/` 用的是 Apache-2.0，而仓库根是 MIT，这说明什么？

**参考答案**：说明**仓库根的 MIT 许可证并不覆盖所有子目录**。HLS 这个子项目很可能本身借鉴了第三方代码（Apache-2.0 是常见开源协议），所以作者保留了它的原始许可。复用时若涉及该子项目，要遵守 Apache-2.0 的条款（例如保留 NOTICE、声明修改等），而不是简单套用 MIT。

---

### 4.3 第三方合集目录组织：ThreePart

#### 4.3.1 概念说明

`ThreePart/` 是「别人的成果收录区」。它不追求统一的目录风格——因为来源各异：有学术论文附带的 HDL、有厂商（Digilent）的官方 IP 库、有博客/网盘分享的杂项 IP、也有像 projf 这样成体系的教程库。**理解 `ThreePart/` 的关键是看每个子项目的 README 里写的「来源」**。

#### 4.3.2 核心流程

`ThreePart/` 下实测有 4 个子目录，性质各不相同：

```
ThreePart/
├── projf-explore/              Project F 教程与 Verilog 库（自述 MIT）★ 教学价值最高
├── ISOIEC18033-3StandardBlock/ ISO/IEC 18033-3 标准分组密码 HDL（东北大学学术核心）
├── digilent_ip/                Digilent 官方 Vivado IP 库（MIT）
└── hardwarebee/                杂项开源 IP（微信/网盘来源，.vhd 片段 + .zip 工程）
```

| 子目录 | 来源 | 内容形态 | 典型内容 |
| --- | --- | --- | --- |
| `projf-explore/` | projectf.io（自述 MIT） | 成体系的 SystemVerilog 库 + 教程 | lib/(clock/display/graphics/maths…)、graphics/、demos/、hello/ |
| `ISOIEC18033-3StandardBlock/` | 东北大学学术网站 | 学术 HDL + PDF 规格 + zip | AES/CAST128/Camellia/DES/MISTY1/SEED/RSA… |
| `digilent_ip/` | Digilent 官方（MIT） | 可直接装进 IP Catalog 的 IP | Pmods/、dvi2rgb、rgb2dvi、video_scaler… |
| `hardwarebee/` | 微信公众号 / Google 网盘 | 零散 .vhd 片段 + zip 工程 | spi_slave.vhd、fm.vhd、AES128.zip、dds_synthesizer.zip… |

其中 **projf-explore 是 Unit 5-7 的教学主线**（一个「迷你 FPGA 大学」），本讲先定位它的内部结构：

```
ThreePart/projf-explore/
├── lib/        Verilog 库：clock/display/essential/graphics/maths/memory/uart …（Unit 5 主角）
├── graphics/   图形教程：pong、framebuffers、hardware-sprites、lines-and-triangles …（Unit 6）
├── demos/      综合 demo：life-on-screen、mandelbrot、ad-astra、sinescroll …（Unit 7/8）
├── maths/      数学 demo（mandelbrot 在 demos/ 下，maths/demo 为入口）
├── hello/      最易上手：hello-arty、hello-nexys（三部曲）
└── doc/        文档
```

#### 4.3.3 源码精读

**（1）projf 库的分区——projf 内部组织最规范，可作为范例。**

[ThreePart/projf-explore/lib/README.md:L5-L15](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/README.md#L5-L15) 把库明确分成若干「领域（Areas）」：clock（时钟与跨时钟域）、display（显示时序与 DVI/HDMI）、essential（公用小模块）、graphics（画线与形状）、maths（除法/LFSR/开方）、memory（ROM/RAM/BRAM）、uart（串口收发）等。每个领域就是一个子目录，职责单一。

该 README 还声明了厂商中立与 SystemVerilog 风格（[ThreePart/projf-explore/lib/README.md:L19-L31](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/README.md#L19-L31)）：同时支持 Xilinx 7 系列（XC7）与 Lattice iCE40，并使用 `logic`/`always_comb`/`always_ff`/`$clog2`/`enum` 等少量 SystemVerilog 特性（[ThreePart/projf-explore/lib/README.md:L34-L44](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/README.md#L34-L44)）。

> 许可证说明：该 README 自述为「MIT licensed designs」并引用 `../LICENSE`，但在本讲义所基于的快照中，projf-explore 根目录下**未发现** `LICENSE` 文件（仅 `lib/res/fonts/unifont-licences/` 下有按文件声明的资源许可）。是否完整、以哪个版本为准，**待本地确认**。

**（2）Digilent IP 库——「装进 IP Catalog」式组织。**

[ThreePart/digilent_ip/README.md:L9-L13](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/digilent_ip/README.md#L9-L13) 说明它就是为 Vivado IP Catalog 设计的：把目录加进 IP Catalog 后，Pmod、视频类 IP 就会出现在列表里。其根 `License.txt` 为 MIT（Copyright (c) 2017 Digilent）。实测 `ip/` 下既有大量 `Pmods/` 外设 IP，也有 `dvi2rgb`/`rgb2dvi`/`video_scaler`/`axi_dynclk` 等视频 IP——注意它和 `HDL/axi_dynclk_v1_0` 同名，可能存在版本/来源差异（待本地对比）。

**（3）hardwarebee——「碎片化」收录的典型。**

[ThreePart/hardwarebee/README.md:L1-L4](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/hardwarebee/README.md#L1-L4) 给出的是一个 Google Drive 链接和一篇微信公众号「IP 介绍」文章。目录里既有裸的 `.vhd`/`.vhd.txt` 片段（`spi_slave.vhd`、`fm.vhd`、`seven_segment.vhd.txt` 等），也有完整的 `.zip` 工程（`AES128.zip`、`cic_core.zip`、`dds_synthesizer.zip`、`Floating-Point-Multiplier-32-bit.zip` 等）。**这类来源的许可证往往不明**，复用前务必回到原始链接核实。

**（4）ISO/IEC 18033-3——学术核心。**

[ThreePart/ISOIEC18033-3StandardBlock/README.md:L1-L7](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/ISOIEC18033-3StandardBlock/README.md#L1-L7) 说明这是「ISO/IEC 18033-3 标准分组密码 HDL 代码」，来源是日本东北大学的学术密码核心网站（`aoki.ecei.tohoku.ac.jp/crypto/`）。目录下每个算法（AES、Camellia、CAST128、DES、MISTY1、SEED、RSA…）自成一级子目录，且常带 PDF 规格书和 `.zip` 归档。**学术核心通常允许教学/研究使用，商用需查原始网站的授权条款。**

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：核对 projf 库的「声明分区」与「实际目录」是否一致；并给每个第三方子项目归类「来源类型」。

**操作步骤**：

```bash
# 1. projf lib README 声明了哪些分区？
#    对照 README 第 5-15 行，逐个检查 lib/ 下是否真的存在该目录：
ls -1 ThreePart/projf-explore/lib

# 2. 查看每个第三方子项目是否自带 README（即是否有来源声明）
for d in projf-explore ISOIEC18033-3StandardBlock digilent_ip hardwarebee; do
  test -f "ThreePart/$d/README.md" && echo "$d: 有 README" || echo "$d: 无 README"
done
```

**需要观察的现象**：

- 第 1 条命令列出的目录应包含 README 中点名的 clock/display/essential/graphics/maths/memory/uart（外加 null/res 等），说明「文档分区」与「实际目录」吻合。
- 第 2 条命令应显示 4 个子项目**都有** README，但内容详略天差地别：projf 与 digilent 较正式，hardwarebee 只是链接，ISO 给出来源网站。

**预期结果**：你能在脑子里把 `ThreePart/` 四个子项目按「来源可信度 / 文档完整度」排序——通常 projf ≈ digilent > ISO（学术）> hardwarebee（碎片）。这会影响你后续挑选学习对象时的优先级。

#### 4.3.5 小练习与答案

**练习 1**：`ThreePart/digilent_ip/` 和 `HDL/axi_dynclk_v1_0/` 都涉及「动态时钟」，它们是什么关系？

**参考答案**：从目录看，`HDL/axi_dynclk_v1_0` 是**仓库作者自研/自留**的一份（放在自研区），而 `ThreePart/digilent_ip/ip/axi_dynclk` 是 **Digilent 官方库收录**的一份（放在第三方区）。两者很可能源自同一设计但版本不同。要确认差异需要对比源码——这是「同功能、不同来源、不同许可证」的典型例子，复用时不能想当然地用错版本。

**练习 2**：如果要在项目里复用 `hardwarebee/spi_slave.vhd`，你首先该做什么？

**参考答案**：先**回溯来源与许可证**。hardwarebee 的 README 只给了 Google Drive 和微信链接，没有明确许可证声明。在商用或公开项目里使用前，应回到原始链接确认作者授权，或联系作者。这一点和复用 projf（自述 MIT）的处理方式截然不同。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一张**仓库模块地图**。这是本讲的核心交付物，也是你后续阅读任何一篇讲义时的「快速索引」。

**任务**：为仓库制作一张模块地图表，每个主要子项目占一行，包含 5 列：

| 路径 | 顶层分区 | 语言/范式 | 用途 | 许可证来源 |
| --- | --- | --- | --- | --- |
| `HDL/AesCryptoCore_1.0` | HDL（自研） | Verilog + AXI IP | AES-128 加解密核心 | 根 MIT（自研） |
| `HDL/DVI_TX` | HDL（自研） | Verilog IP | DVI 发送（TMDS + 10:1 串化） | 根 MIT（自研） |
| `HDL/color_space` | HDL（自研） | Verilog/VHDL 混合 | 颜色空间转换 | 根 MIT（自研） |
| `HDL/ov5640_cap_data` | HDL（自研） | Verilog IP | OV5640 摄像头采集 | 根 MIT（自研） |
| `HDL/axi_dynclk_v1_0` | HDL（自研） | Verilog IP | AXI 动态时钟 | 根 MIT（自研） |
| `HLS/2D-median-filter-algorithm-HLS` | HLS（自研） | C → HLS | 2D 中值滤波 | Apache-2.0（子项目自带） |
| `HLS/edge_canny_detector` | HLS（自研） | C → HLS（已综合） | Canny 边缘检测 | 待确认 |
| `ThreePart/projf-explore` | 第三方 | SystemVerilog 库 + 教程 | FPGA 图形/显示/数学教学 | 自述 MIT（LICENSE 待本地确认） |
| `ThreePart/ISOIEC18033-3StandardBlock` | 第三方 | Verilog（学术 HDL） | 标准分组密码核心 | 东北大学学术授权（待查网站） |
| `ThreePart/digilent_ip` | 第三方 | Vivado IP 库 | Pmod / 视频 IP | MIT（Digilent，2017） |
| `ThreePart/hardwarebee` | 第三方 | VHDL 片段 + zip | 杂项开源 IP | 不明（回溯网盘/微信确认） |

**建议步骤**：

1. 用 `ls -1 HDL HLS ThreePart` 复核行数和路径是否与你的本地版本一致。
2. 对每一行的「许可证来源」标注你**确证的**（如根 MIT、Digilent MIT、HLS Apache-2.0）和**待确认的**（如 projf 的 LICENSE 文件、hardwarebee 的授权）。
3. 在地图上用记号标出本手册的教学主线：`AesCryptoCore_1.0`（Unit 2-3）、`2D-median-filter`（Unit 4）、`projf-explore`（Unit 5-7）。

**预期结果**：你得到一张可打印、可长期维护的「仓库导航图」，并且清楚地知道哪些代码可以放心学（许可证明确）、哪些复用前要先查授权。

---

## 6. 本讲小结

- 仓库根由 `README.md`（极简）、`LICENSE`（MIT，仅覆盖自研）和三个顶层目录 `HDL/`、`HLS/`、`ThreePart/` 构成——**一个目录对应一条技术路线**。
- `HDL/` 是手写 HDL + Vivado IP 路线，含 AES 核心及一条摄像头→颜色转换→DVI 的视频链路；AES 工程遵循 Xilinx UG892 的最小化版本控制模板（`proj/` 重建 + `src/`、`sdk/`、`ip_repo/` 分目录）。
- AES 工程里有两个不同的 `hdl/`：`hdl/src/` 是**算法 RTL**（aes_top.v 等），`ip_repo/.../hdl/` 是**AXI 包装**（AesCryptoCore_v1_0 等）——这是 Unit 2 与 Unit 3 的分界。
- `HLS/` 以 C 源码为中心（中值滤波、Canny 检测），其中中值滤波子项目带独立的 Apache-2.0 许可证，说明根 MIT 并不覆盖全部子目录。
- `ThreePart/` 收录 4 个来源各异的第三方项目：projf（成体系教学库，自述 MIT）、ISO 密码（学术）、digilent_ip（厂商官方 MIT）、hardwarebee（碎片化网盘来源）。
- **许可证边界**：复用任何代码前，必须按子目录逐项确认授权，尤其是 `ThreePart/` 和 `HLS/` 下的项目。

---

## 7. 下一步学习建议

有了这张地图，接下来可以选一条主线深入：

- **想学硬件加密与 AXI 软硬协同**：直接进入 Unit 2（AES 数据通路），先读 `HDL/AesCryptoCore_1.0/.../hdl/src/aes_top.v`；进阶到 Unit 3 看 IP 封装。
- **想学「软件思维写硬件」**：进入 Unit 4（HLS 中值滤波），读 `HLS/2D-median-filter-algorithm-HLS/MedianFilter.c`。
- **想学 FPGA 图形/显示/数学**：从 Unit 5（projf 库基础）开始，先读 `ThreePart/projf-explore/lib/` 各分区。
- **想第一次点亮开发板**：可以跳到 u6-l6（Hello 示例），用 `ThreePart/projf-explore/hello/` 三部曲上手。

无论选哪条线，建议把本讲的「模块地图」常备手边——它会在你迷路时告诉你「现在在仓库的哪个角落」。
