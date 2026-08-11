# 仓库结构与 GOTCHA 测试数据

## 1. 本讲目标

上一讲（u1-l1）我们建立了对「SAR 反投影 + Versal ACAP 三引擎」的直觉，但还没有真正打开过仓库。本讲带你走进代码仓库本身，读完之后你应当能够：

1. 说出仓库里每个顶层目录**装的是什么**、分别属于 **AIE / PL / Host** 中的哪一个域（或属于工具/文档）。
2. 认识测试用的 **GOTCHA 数据集**：知道有 `slowtime` 与 `phdata`（距离压缩）两类 CSV，它们的列数、文件大小、文件名含义分别是什么。
3. 知道为什么这些大文件要用 **Git LFS** 管理，以及 `git lfs pull` 的正确流程。

本讲只看「目录与数据」，不深入任何一段算法代码。目录背后的源码细节会留到第 3～6 单元逐域展开。

## 2. 前置知识

阅读本讲前，你需要具备：

- **域（Domain）的概念**：在 u1-l1 中提到，Versal ACAP 是异构三引擎系统。本仓库把代码按引擎拆成了三块：
  - **AIE 域**：AI Engine 阵列上跑的核心反投影计算（C++/ADF）。
  - **PL 域**：可编程逻辑（FPGA）上跑的 HLS 内核，这里是一个 DMA 包路由器。
  - **Host 域**：ARM Cortex-A72 上跑的 Linux 控制程序。
- **CSV（逗号分隔值）**：一种纯文本表格格式，每行是一条记录，列与列之间用逗号隔开。本项目用它存雷达原始数据。
- **复数（complex number）**：形如 `a+bi` 的数，`a` 是实部、`b` 是虚部、`i=√-1`。SAR 回波是复信号，所以距离压缩数据以复数形式存储。
- **Git LFS（Large File Storage）**：Git 的一个大文件扩展。普通 Git 适合存文本代码，存几百兆的二进制或数据文件会让仓库膨胀、克隆变慢。Git LFS 把大文件的真实内容存在单独的存储服务器上，仓库里只保留一个很小的「指针文件」，需要时再用 `git lfs pull` 把真实内容拉下来。

> 小提示：如果你还没读过 README，建议先扫一眼它的目录表（下面会引用），那是本讲最好的导览。

## 3. 本讲源码地图

本讲涉及的「源码」其实大多是**配置与数据文件**，而不是算法实现。这正符合「先认路、再读码」的学习顺序。

| 文件 / 目录 | 作用 | 本讲用来讲什么 |
|---|---|---|
| `README.md` | 项目说明，含目录表与 Git LFS 流程 | 仓库目录的权威说明 |
| `design/common.h` | 全局配置头文件（跨三域共享） | 解释 `BC_ELEMENTS=4`、`RC_SAMPLES` 选项 |
| `design/test_data/*.csv` | GOTCHA 测试数据（slowtime + 4 种 phdata） | 数据集格式与规模 |
| `Makefile` | 构建脚本 | 展示如何按 `RC_SAMPLES` 选中对应的 phdata 文件 |
| `.gitattributes` | 告诉 Git 哪些文件走 LFS | 解释 LFS 规则 |

## 4. 核心概念与源码讲解

### 4.1 目录树与域归属

#### 4.1.1 概念说明

打开仓库第一件事，是搞清楚「东西都放在哪、归谁管」。这个项目把**设计代码（design/）**、**构建脚本（Makefile）**、**部署辅助脚本（helper_scripts/）**、**文档（doc/）**分得很清楚。而 `design/` 内部又按 Versal 的三个引擎域再次拆分。理解这种「按域分层」的组织方式，是后续阅读任何源码的前提——你看到 `design/aie/xxx`，就知道这是跑在 AI Engine 上的；看到 `design/host/xxx`，就知道这是跑在 ARM 上的。

一个关键认知：**这是一个多仓库项目的其中一个仓库**。README 顶部明确警告「不要直接克隆本仓库」，而应通过 [versal-manifest](https://github.com/nasa-jpl/versal-manifest) 仓库收集所有依赖（例如构建 Linux 系统的 Yocto 仓库）。本仓库只负责「SAR 反投影设计」本身，操作系统、工具链等都在别的仓库里。这解释了为什么本仓库里没有内核镜像、rootfs，只有「设计源码 + 把设计搬上板子的脚本」。

#### 4.1.2 核心流程

仓库自上而下可以分成四层：

```text
versal-sar-backprojection-design/
├── design/                 ← 设计源码（按域再拆分）
│   ├── aie/                ← 【AIE 域】AI Engine 反投影内核与图
│   ├── pl/                 ← 【PL 域】FPGA 上的 HLS 包路由器内核
│   ├── host/               ← 【Host 域】ARM 控制程序
│   ├── exec_scripts/       ← 运行脚本（打包后传到 ARM 执行）
│   ├── profiling_cfgs/     ← 性能剖析配置（xrt.ini）
│   ├── system_cfgs/        ← 系统连接配置（构建时生成，仅留 .gitkeep）
│   ├── test_data/          ← GOTCHA 测试数据 CSV
│   ├── vivado_metrics_scripts/ ← Vivado 资源/功耗度量脚本
│   └── common.h            ← 跨三域共享的全局配置
├── helper_scripts/         ← 环境配置 / 烧写 / 部署辅助脚本
├── doc/                    ← LaTeX 设计文档与图片
└── Makefile                ← 一键构建整个设计
```

域归属一句话总结：

- **AIE 域** → `design/aie/`
- **PL 域** → `design/pl/`
- **Host 域** → `design/host/`
- **跨域共享** → `design/common.h`
- **工具/部署** → `helper_scripts/`、`design/exec_scripts/`、`design/vivado_metrics_scripts/`
- **构建** → `Makefile`
- **文档** → `doc/`
- **测试数据** → `design/test_data/`

#### 4.1.3 源码精读

README 用一张表把目录职责讲得很清楚，这是最权威的导览：

[README.md#L16-L29](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md#L16-L29) —— README 的「目录表」，逐行说明 `design/aie`、`design/pl`、`design/host`、`design/exec_scripts`、`design/test_data`、`design/common.h`、`helper_scripts`、`Makefile`、`doc` 各自装什么。

对照仓库实际内容可以验证这张表：

- `design/aie/` 下确实是 AIE 相关文件：`backprojection.cc`（内核实现）、`graph.h` / `graph.cpp`（ADF 数据流图）、`custom_kernels.h`、`aiecompiler.cfg`。
- `design/pl/` 下是 PL 相关文件：`dma_pkt_router.cpp` / `.h`（HLS 包路由器内核）、`pkt_router_config.cfg`、以及 `tb/` 子目录里的 testbench。
- `design/host/` 下是 ARM 程序：`main.cpp`、`sar_backproject.cpp` / `.h`、以及一个独立的 `Makefile`。

`design/common.h` 是**唯一一个跨三域共享的头文件**，它定义了决定整个设计规模的宏。本讲只用到它的一小部分，先看两个与「数据」直接相关的定义：

[design/common.h#L40-L45](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L40-L45) —— `BC_ELEMENTS` 定义为 4，注释说明广播给其他 AIE 内核的 4 个元素是：天线 X 位置、Y 位置、Z 位置、到场景中心的参考距离（ref_range）。这正是 slowtime 数据每行的 4 列所对应的物理量。

[design/common.h#L19-L22](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L19-L22) —— `RC_SAMPLES` 定义为 512，注释明确「测试数据只为以下可选项生成：512, 256, 128, 64」。这与 `design/test_data/` 里 4 个 phdata 文件一一对应（见 4.2 节）。

#### 4.1.4 代码实践

**实践目标**：亲手核对仓库的目录结构，并把每个目录标注到正确的「域」上。

**操作步骤**：

1. 在仓库根目录运行 `git ls-files`（只看被 Git 跟踪的文件，过滤掉构建产物）。
2. 把输出按顶层目录归类，画一棵目录树（像 4.1.2 那样）。
3. 在每个 `design/<子目录>` 旁标注它属于 **AIE / PL / Host / 工具 / 文档 / 数据** 中的哪一类。

**需要观察的现象**：

- `design/aie/`、`design/pl/`、`design/host/` 三个目录的文件**互不重叠**，分别用不同的语言/框架（ADF C++、HLS C++、普通 C++ + XRT）。
- `design/common.h` 是**唯一**同时被三域 `#include` 的文件（后续读代码时会看到它被 host、aie、pl 都包含）。
- `design/system_cfgs/` 里只有一个 `.gitkeep`，说明真正的 `system.cfg` 是**构建时生成的产物**，不入版本控制。

**预期结果**：你会得到一张清晰的「域归属图」，明白后面每读一个文件，先看它在哪个目录、属于哪个域。

#### 4.1.5 小练习与答案

**练习 1**：`design/exec_scripts/` 里的脚本属于 AIE、PL 还是 Host 域？

> **答案**：严格说不属于「计算域」，而是**部署/运行工具**。但这些脚本（如 `run_script_hw.sh`）最终是被拷贝到 **ARM（Host）** 上去启动 `sar_backproject.elf` 的，所以它服务于 Host 域的运行。

**练习 2**：为什么 `design/system_cfgs/` 里只有一个 `.gitkeep` 而没有真正的配置文件？

> **答案**：因为 `system.cfg`（描述 AIE 与 PL 如何连接）是由 Makefile 在构建时根据 `common.h` 的宏**自动生成**的，属于构建产物，不应该入版本控制。用 `.gitkeep` 只是为了让 Git 保留这个空目录。

---

### 4.2 GOTCHA 测试数据集

#### 4.2.1 概念说明

反投影算法需要真实的雷达回波数据来验证。本项目用的是 **GOTCHA 数据集**——它是美国空军研究实验室（AFRL）用机载 SAR 在 Goleta, CA 上空做的一次 360° 圆周飞行采集的公开数据，是 SAR 成像算法验证的「标准考题」。文件名里的 `pass1_360deg_HH` 就表示「第 1 圈、360 度、水平发射水平接收（HH 极化）」。

数据分成两类，对应反投影算法的两路输入：

1. **slowtime（慢时间/方位）数据**：记录雷达平台在每个脉冲时刻的几何状态（天线位置、参考距离等）。它是「平台在哪、朝哪」的信息。
2. **phdata（phase history，相位历史 / 距离压缩数据）**：记录每个脉冲接收到的回波复数样本。它是「雷达听到了什么」的信息。

反投影就是用 slowtime 给出的几何关系，把 phdata 里的每个回波样本「投影」回地面像素上做相干累加。这两类数据缺一不可。

#### 4.2.2 核心流程

数据从 CSV 到内存的过程：

```text
design/test_data/*.csv
        │
        │  （打包到 SD 卡 / NFS，传到 ARM）
        ▼
  sar_backproject.elf <xclbin> <slowtime.csv> <phdata.csv> <out.csv> <iter>
        │
        │  host 的 fetchRadarData() 逐行解析
        ▼
  m_broadcast_data_array[]   ← slowtime：每个脉冲 4 个 float
  m_rc_array[]               ← phdata ：每个脉冲 RC_SAMPLES 个 cfloat
```

两类 CSV 的格式差异很大：

| 维度 | slowtime CSV | phdata CSV |
|---|---|---|
| 每行列数 | **4** 列（= `BC_ELEMENTS`） | **RC_SAMPLES** 列（512，对应 512 变体） |
| 数据类型 | 普通浮点数 | 复数，形如 `a+bi` |
| 列含义 | 天线 X/Y/Z + ref_range | 该脉冲的各个距离门回波复样本 |
| 每行代表 | 一个脉冲的几何状态 | 一个脉冲的全部距离压缩回波 |
| 文件大小 | ~2.3 MB | 规模随 RC_SAMPLES 变化（96 MB ~ 768 MB） |

phdata 的文件名遵循固定模式：`gotcha_phdata_<N>-out-of-424-rc-samples_pass1_360deg_HH.csv`，其中 `<N>` ∈ {64, 128, 256, 512}，与 `common.h` 里 `RC_SAMPLES` 的可选值一一对应；`424` 是原始 FFT 长度（即从 424 个原始样本中取出 N 个距离压缩样本）。

仓库里实际有 **5 个数据文件**：

| 文件 | 大小 | 列数 | 说明 |
|---|---|---|---|
| `gotcha_slowtime_pass1_360deg_HH.csv` | ~2.3 MB | 4 | 唯一的 slowtime 文件，含完整 360° 飞行 |
| `gotcha_phdata_512-out-of-424-...csv` | ~768 MB | 512 | 默认配置（`RC_SAMPLES=512`）使用 |
| `gotcha_phdata_256-out-of-424-...csv` | ~384 MB | 256 | `RC_SAMPLES=256` 时使用 |
| `gotcha_phdata_128-out-of-424-...csv` | ~192 MB | 128 | `RC_SAMPLES=128` 时使用 |
| `gotcha_phdata_64-out-of-424-...csv`  | ~96 MB  | 64  | `RC_SAMPLES=64` 时使用 |

> 注意：slowtime 文件实际有约 **42,208 行**（完整 360° 的全部脉冲），但 host 在解析时只会读取前 `PULSES`（默认 602）行——这对应一个小角度孔径的子集（详见 u3-l3）。也就是说，文件提供的是「全集」，运行时只消费一个子集。

#### 4.2.3 源码精读

先看 slowtime 数据的真实样子（每行 4 个浮点数，逗号分隔）：

[design/test_data/gotcha_slowtime_pass1_360deg_HH.csv#L1-L5](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/test_data/gotcha_slowtime_pass1_360deg_HH.csv#L1-L5) —— slowtime 数据前 5 行，每行 4 列，分别是天线 X 位置、（随脉冲递增的）方位/慢时间量、Y 位置、Z 位置这类几何量，对应 `BC_ELEMENTS=4`。

再看 phdata 数据的真实样子（每行 512 个复数，每个复数形如 `a+bi`，用科学计数法）：

[design/test_data/gotcha_phdata_512-out-of-424-rc-samples_pass1_360deg_HH.csv#L1-L1](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/test_data/gotcha_phdata_512-out-of-424-rc-samples_pass1_360deg_HH.csv#L1-L1) —— phdata 第一行，是 512 个 `a+bi` 复数，对应一个脉冲的 512 个距离压缩样本。

host 代码 `fetchRadarData()` 证实了「4 列 vs 复数列」的解析差异——slowtime 用普通 `std::stof` 直接读 4 列，phdata 用正则匹配 `a+bi`：

[design/host/sar_backproject.cpp#L164-L181](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L164-L181) —— 解析 slowtime：逐行读，用 `std::getline` 按 `,` 切出 4 个字段，`std::stof` 转成浮点，存入 `m_broadcast_data_array[BC_ELEMENTS*pulse_idx + 0..3]`。注意循环条件 `pulse_idx < PULSES`，所以只读前 `PULSES` 行（详细的解析逻辑在 u3-l3 讲）。

[design/host/sar_backproject.cpp#L197-L206](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L197-L206) —— 解析 phdata：对每个逗号分隔的字段，用正则 `complex_regex` 匹配 `a+bi` 形式，分别取出实部、虚部，组装成 `cfloat` 写入 `m_rc_array[pulse_idx*RC_SAMPLES + rc_samp_cnt]`。

最后看 Makefile 如何「按 `RC_SAMPLES` 自动选对 phdata 文件」——这是把 `common.h` 配置与数据文件绑定起来的关键一环：

[Makefile#L91-L108](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L91-L108) —— `package` 目标里，第 92 行先用 `grep` 从 `common.h` 读出 `RC_SAMPLES` 的值，第 108 行再用 `gotcha_phdata_$${RC_SAMPLES}-out-of-424-...csv` 拼出正确的 phdata 文件名并打包进 SD 卡镜像。这样改 `RC_SAMPLES` 就会自动换用对应的数据文件。

#### 4.2.4 代码实践

**实践目标**：亲手验证两类 CSV 的列数与格式，并理解文件大小为何差这么多。

**操作步骤**：

1. 统计 slowtime 每行列数：查看 slowtime 第一行被逗号切成了几段（应为 4）。
2. 统计 phdata_512 每行列数：查看 phdata_512 第一行被逗号切成了几段（应为 512，等于 `RC_SAMPLES`）。
3. 用 `ls -l design/test_data/*.csv` 查看 5 个文件的大小，注意 phdata 大小大致随 `<N>` 线性增长。
4. 算一笔账：512 列 × 602 行 × 8 字节（一个 `cfloat` 实部+虚部各 4 字节）≈ 2.4 MB——这只是「运行时实际消费」的部分；而 phdata_512 文件本身约 768 MB，因为它存的是**完整 360° 飞行**的所有脉冲，远多于 602 行。

**需要观察的现象**：

- slowtime 每行只有 4 个数，文件很小（~2.3 MB）。
- phdata 每行有数百个数，文件很大；`<N>` 越大，每列越多、文件越大。
- 两个文件的「行」都代表「一个脉冲」，但列的含义完全不同。

**预期结果**：你会直观理解为什么后续性能文档里「填充数据缓冲（Populating data buffers）」要花几十分钟——因为要从几百兆的文本 CSV 里逐字符解析数十万个复数。这一瓶颈的根因就在数据格式本身（详见 u8-l2）。

> 说明：以上命令需要本地已 `git lfs pull` 拉到真实数据后才能验证文件内容；若只拉到 LFS 指针，看到的会是几十字节的指针文本而非真实数据。

#### 4.2.5 小练习与答案

**练习 1**：为什么 slowtime 只有一个文件，而 phdata 有四个？

> **答案**：因为 slowtime 记录的是平台几何状态，与距离采样数无关，一份就够；而 phdata 的列数 = `RC_SAMPLES`，`RC_SAMPLES` 有 64/128/256/512 四种可选值，每种对应不同规模，所以为每个值各存了一份。

**练习 2**：如果我把 `common.h` 里的 `RC_SAMPLES` 从 512 改成 128，构建/打包时会发生什么？

> **答案**：Makefile 的 `package` 目标会从 `common.h` 读到 `RC_SAMPLES=128`，于是打包 `gotcha_phdata_128-out-of-424-...csv`（而不是 512 那个）到 SD 卡镜像；host 程序也会按 128 列去解析。整个数据通路会自动对齐到新的列数。

---

### 4.3 Git LFS 拉取流程

#### 4.3.1 概念说明

前面看到，phdata_512 有 **768 MB**，五个数据文件加起来超过 **1.4 GB**。如果把这种大文件直接塞进 Git，每次克隆都要下载全量历史，仓库会迅速膨胀到无法维护。Git LFS（Large File Storage）就是解决这个问题的标准做法：

- 仓库里**只存一个很小的「指针文件」**（约一百多字节，里面是文件的哈希和大小）。
- 真实内容存在 LFS 专用的存储服务器上。
- 执行 `git lfs pull` 时，才按指针去把真实内容下载下来、替换掉指针。

所以**克隆完仓库，数据文件并不会自动可用**——你看到的可能是「指针」，必须额外执行 LFS 拉取。这是新手最容易踩的坑：以为克隆完了就能跑，结果程序读到一个百来字节的指针文本而不是 768 MB 的数据。

#### 4.3.2 核心流程

Git LFS 的工作链路：

```text
.gitattributes            ← 声明「哪些文件类型走 LFS」
        │
        ▼
git lfs install           ← 在本机启用 LFS 钩子（每台机器一次）
        │
        ▼
git clone / git pull      ← 拉到的是「指针文件」
        │
        ▼
git lfs pull              ← 按指针下载真实内容，替换指针
        │
        ▼
design/test_data/*.csv    ← 变成可被程序读取的真实数据
```

关键认知：`.gitattributes` 决定了**哪些后缀走 LFS**。本项目把 CSV、PDF、图片（png/jpg/svg）、Office 文档、压缩包等都配置成了 LFS 对象。

#### 4.3.3 源码精读

README 给出了完整的 LFS 安装与拉取步骤：

[README.md#L31-L41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md#L31-L41) —— README 的「Git LFS Installation」小节，四步：①用包管理器装 `git-lfs`；②进入仓库目录；③`git lfs install`；④`git lfs pull`。

具体哪些文件走 LFS，由 `.gitattributes` 规定。本项目对**所有 CSV 文件**统一启用了 LFS：

[.gitattributes#L22-L23](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/.gitattributes#L22-L23) —— `.gitattributes` 的 CSV 规则：`*.[cC][sS][vV] filter=lfs diff=lfs merge=lfs -text`。`filter=lfs` 表示这类文件在提交/检出时经过 LFS 过滤器；`-text` 表示把它当作二进制处理（不做行尾转换）。同文件里还有 PDF、png/jpg/svg 等类似规则。

这就是为什么 `design/test_data/*.csv` 这几个大文件不会让仓库变大的原因——它们在 Git 里只是指针。

#### 4.3.4 代码实践

**实践目标**：确认本机的 GOTCHA 数据已被 LFS 真正下载，而不是停留在指针状态。

**操作步骤**：

1. 进入仓库目录，确认已安装 git-lfs：`git lfs version`。
2. 首次使用先初始化：`git lfs install`。
3. 拉取大文件：`git lfs pull`。
4. 验证数据已真实下载，任选一种：
   - 看文件大小：`ls -lh design/test_data/gotcha_phdata_512-out-of-424-rc-samples_pass1_360deg_HH.csv` —— 真实数据应为约 **768 MiB**；若只有一百多字节，说明还是指针。
   - 看文件开头：用 `head -c 200` 查看前 200 字节。LFS 指针文件以 `version https://git-lfs.github.com/spec/v1` 开头；真实 phdata 则以 `5.84543704463e-06-1.3672051864e-06i,...` 这样的复数开头。

**需要观察的现象**：

- 拉取前（仅指针）：文件很小，内容是 `version https://git-lfs.github.com/spec/v1\noid sha256:...\nsize ...`。
- 拉取后（真实数据）：文件变成几百兆，内容是真实的复数 CSV。
- slowtime 文件较小（~2.3 MB），拉取很快；几个 phdata 文件较大，拉取需要较长时间和稳定网络。

**预期结果**：`ls -lh` 显示 phdata_512 约 768M、phdata_64 约 96M 等，且 `head` 能看到真实的复数/浮点数据。此时数据才真正可用于构建和运行。

> 待本地验证：上述大小与耗时取决于本机是否已配置好 git-lfs 以及网络是否能访问 LFS 存储服务器；在受限/离线环境下 `git lfs pull` 可能失败，需要配置 LFS 端点或改用 manifest 仓库统一拉取。

#### 4.3.5 小练习与答案

**练习 1**：克隆仓库后直接 `head design/test_data/gotcha_phdata_512-...csv`，看到的却是 `version https://git-lfs.github.com/spec/v1 ...`，这是为什么？

> **答案**：因为该 CSV 走 Git LFS（见 `.gitattributes` 的 CSV 规则），克隆默认只拉指针。需要执行 `git lfs install` + `git lfs pull` 才会把真实 768 MB 内容下载并替换指针。

**练习 2**：为什么本项目要把 CSV 也放进 LFS，而不是只放图片和 PDF？

> **答案**：因为本项目的 CSV 是**雷达原始数据**，单个文件最大近 800 MB，五个文件合计超过 1.4 GB。这种体积远超 Git 普通文件管理的合理范围；放进 LFS 可以保持仓库本体小巧、克隆快，只在需要时才下载真实数据。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「仓库认路 + 数据就绪」的全流程：

1. **画目录树并标注域归属**：在本地用 `git ls-files` 把 `design/` 下的文件按 `aie/pl/host/exec_scripts/test_data/...` 归类，画一棵树，在每个叶子目录旁标注「AIE / PL / Host / 工具 / 文档 / 数据」。要求能一眼看出 `design/aie/backprojection.cc` 属于 AIE 域、`design/host/main.cpp` 属于 Host 域。

2. **建立「配置 → 数据」的映射**：打开 `design/common.h`，找到 `RC_SAMPLES` 和 `BC_ELEMENTS` 两个宏；再到 `design/test_data/` 找到与之对应的数据文件，写出一句话说明「`RC_SAMPLES=512` 决定了用哪个 phdata 文件，`BC_ELEMENTS=4` 决定了 slowtime 每行几列」。

3. **把数据拉到可用状态**：执行 `git lfs install && git lfs pull`，用 `ls -lh` 和 `head -c 200` 确认 phdata_512 已从「指针」变成约 768 MB 的真实复数数据。

4. **追踪一条「数据绑定」链路**：阅读 Makefile 第 92 行与第 108 行，用自己的话写清楚：为什么改 `common.h` 里的 `RC_SAMPLES` 后，`make package` 会自动打包正确的 phdata 文件？（提示：`grep` 读宏 → 字符串拼接文件名 → `--package.sd_file`）。

完成这套实践后，你就具备了进入后续讲义的基础：知道代码在哪、数据是什么、数据从哪来。下一单元我们会开始读 AIE 域和 Host 域的真正实现。

## 6. 本讲小结

- 仓库按 Versal 的三个引擎域分层组织：`design/aie/`（AIE）、`design/pl/`（PL）、`design/host/`（Host），`design/common.h` 是三域共享的唯一配置头。
- `design/` 之外还有工具/部署层（`helper_scripts/`、`design/exec_scripts/`）、构建层（`Makefile`）、文档层（`doc/`），本仓库只是多仓库项目中的一个设计仓库。
- 测试数据是 **GOTCHA** 公开数据集，分两类：**slowtime**（4 列浮点，平台几何）和 **phdata**（RC_SAMPLES 列复数 `a+bi`，距离压缩回波）。
- phdata 按 `RC_SAMPLES` 的四个可选值（64/128/256/512）各存一份，Makefile 会根据 `common.h` 自动选对文件。
- 这些大文件用 **Git LFS** 管理，仓库里只存指针；必须 `git lfs install && git lfs pull` 才能拿到真实数据，否则程序读到的是指针文本。
- slowtime 文件含完整 360° 飞行（约 4.2 万行），但运行时 host 只读前 `PULSES`（默认 602）行。

## 7. 下一步学习建议

接下来可以按两条线推进：

- **想先理解「整体怎么跑起来」** → 继续按入门层顺序，学 **u1-l3（构建系统与 Makefile 目标）** 和 **u1-l4（全局配置中心 common.h）**，把「目录、构建、配置」补齐，这是入门层的最后两块拼图。
- **想直接进入代码阅读** → 可以先看 **u2 单元（Versal 平台与 AIE 编程模型）** 补前置知识，再进入 **u3 单元（主机应用）**；届时本讲提到的 `fetchRadarData()`（解析 slowtime/phdata）会在 **u3-l3** 得到逐行讲解。

无论走哪条线，建议先把本讲的「综合实践」做完，确保本地数据已就绪、目录结构已心中有数，再开始读源码。
