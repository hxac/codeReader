# 仓库目录结构：编号目录与模块地图

## 1. 本讲目标

上一讲（u1-l1）我们已经知道 FIR_x2 是一个把 PCM 音频 2 倍过采样的 FIR 滤波器，并认识了它的输入输出信号。本讲不再讲功能本身，而是回答一个更基础的问题：**这个仓库里的文件是怎么组织的？我该从哪里读起？**

学完本讲，你应当能够：

- 识别 `01_DPRAM_CONT` 到 `07_FIR_x2` 这 7 个核心 RTL 目录的职责。
- 区分「上层控制器」与「底层存储原语（SPROM/SDPRAM）」的分层关系。
- 说出 `08_hex`、`09_txt`、`10_Example`、`11_fir_gen` 这几个非 RTL 目录各自的用途。
- 对照编译脚本，画出 7 个核心模块之间的实例依赖草图。

掌握这张「模块地图」之后，后续每一讲深入某个模块时，你都能清楚它在整体中的位置。

## 2. 前置知识

阅读本讲前，建议你已经了解（来自 u1-l1）：

- **FIR_x2 的定位**：FPGA 上的 2 倍过采样 FIR 滤波器，把 44.1/48 kHz 升采样到 88.2/96 kHz。
- **I2S/PCM 信号**：输入 `MCLK_I`/`BCK_I`/`LRCK_I`/`DATA_I`，输出 `BCKx2_O`/`LRCKx2_O`/`DATA_O`。
- **单时钟域**：所有内部逻辑共用 `MCLK_I` 一个时钟。

本讲会用到两个通俗概念：

- **RTL（Register Transfer Level）**：用 Verilog 描述的、可被综合成硬件电路的代码，也就是这个项目的「源码」。
- **模块（module）**：Verilog 中的一个功能单元，类似软件里的「类」或「函数」。一个大模块可以在内部实例化（调用）若干小模块。
- **存储原语（primitive）**：最底层的 RAM/ROM 存储单元，通常应替换为各 FPGA 厂商的官方 IP 来获得最优资源占用。

## 3. 本讲源码地图

本讲主要阅读两份「地图」类文件，它们能帮你一次性看清整个仓库：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/README.md) | 项目总览：用法、已验证器件、示例工程清单、许可 |
| [07_FIR_x2/Questa/FIR_x2.bat](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/FIR_x2.bat) | Questa 仿真批处理脚本，里面的 `vlog` 编译列表列出了顶层仿真用到的全部 RTL 文件 |

配合阅读（用来印证模块实例化关系）：

| 文件 | 作用 |
|------|------|
| [07_FIR_x2/FIR_x2.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v) | 顶层模块，实例化 4 个子模块 |
| [02_DATA_BUFFER/DATA_BUFFER.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v) | 数据缓冲封装，内部再实例化控制器 + RAM 原语 |
| [04_FIR_COEF/FIR_COEF.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v) | 系数 ROM 封装，内部再实例化控制器 + ROM 原语 |

## 4. 核心概念与源码讲解

### 4.1 编号目录约定

#### 4.1.1 概念说明

打开仓库根目录，你会看到一串以两位数字开头的目录：`01_`、`02_`、……、`11_`。这不是随意的命名，而是一条**精心设计的阅读线索**——编号大致沿「音频信号在滤波器内部的流动方向」递进：

```
输入 PCM → 缓冲(RAM) → 取系数(ROM) → 乘法 → 累加 → 输出
   01/02      03/04       05      06      07
```

每个核心目录都遵循相同的内部结构：

```
0X_模块名/
├── 模块名.v          # RTL 实现（设计文件，DUT）
├── 模块名_TB.v       # 测试激励（testbench）
└── Questa/           # Questa 仿真脚本与所需数据文件
    ├── *.bat         # 编译 + 仿真批处理
    ├── run.do        # 波形 / 覆盖率 do 文件
    └── *.hex / *.txt # 存储初始化文件 / 测试信号文件
```

> 名词解释：**DUT（Design Under Test）** 指被测的设计本身；**testbench** 是为它提供激励、观察输出的「测试台」，不参与综合。

#### 4.1.2 核心流程

把 11 个目录按职责分类，可以得到下面这张总表（编号即目录名前缀）：

| 编号 | 目录 | 类别 | 一句话职责 |
|------|------|------|-----------|
| 01 | `01_DPRAM_CONT` | RTL·控制器 | 双口 RAM 的读写地址控制器（环形缓冲） |
| 02 | `02_DATA_BUFFER` | RTL·封装+原语 | 输入数据缓冲：把 01 控制器与 SDPRAM 原语组合 |
| 03 | `03_SPROM_CONT` | RTL·控制器 | 单口 ROM 的读地址控制器（系数多相寻址 + 过采样时钟） |
| 04 | `04_FIR_COEF` | RTL·封装+原语 | 系数 ROM：把 03 控制器与 SPROM 原语组合 |
| 05 | `05_MULT` | RTL·运算 | 有符号乘法器（数据 × 系数） |
| 06 | `06_ADD` | RTL·运算 | 累加积分器（求卷积和） |
| 07 | `07_FIR_x2` | RTL·顶层 | 把 02/04/05/06 拼成完整滤波器 + 输出饱和 |
| 08 | `08_hex` | 数据 | 共享的存储初始化 `.hex` 文件 |
| 09 | `09_txt` | 数据 | 共享的测试 PCM 信号 `.txt` 文件 |
| 10 | `10_Example` | 示例 | 7 款开发板的完整工程（打包文件） |
| 11 | `11_fir_gen` | 工具 | Python/AWK 系数生成与十六进制转换脚本 |

7 个核心 RTL 目录（01–07）是本项目的「主菜」，后续讲义会逐个深入；08–11 是辅助资源。

#### 4.1.3 源码精读

最能体现「编号 = 阅读顺序」的，是 README 中给出的仿真步骤——它要求你**逐个编译每个模块及其 testbench**，并指出部分模块需要存储初始化文件与测试信号：

- [README.md:30-33](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/README.md#L30-L33)：说明每个模块都可单独仿真，Questa 目录里提供了批处理脚本。这印证了「每个编号目录自包含、可独立验证」的设计。

而顶层仿真脚本 `FIR_x2.bat` 用相对路径把分散在多个目录的 RTL 文件汇总编译，正好暴露了 7 个核心模块之间的归属关系：

- [07_FIR_x2/Questa/FIR_x2.bat:5-14](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/FIR_x2.bat#L5-L14)：这段 `vlog` 编译列表逐行列出顶层仿真所需的全部文件。注意路径前缀——`../FIR_x2.v` 指向本目录（07）的上层，`../../01_DPRAM_CONT/...` 指向仓库根目录下的其它编号目录。把这份列表读一遍，就能得到一张现成的「文件来源清单」。

注意第 5 行 `vlog +cover=bcs ../FIR_x2.v` 中的 `+cover=bcs` 表示开启分支/条件/语句覆盖率收集，而最后一行 [FIR_x2.bat:16](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/FIR_x2.bat#L16) 的 `vsim ... -coverage -do "do run.do"` 才是真正启动仿真并加载波形/覆盖率配置。

#### 4.1.4 代码实践

**实践目标**：把 `FIR_x2.bat` 的编译列表转译成一张「文件 → 所属编号目录」的对照表，熟悉每个文件住在哪个目录。

**操作步骤**：

1. 打开 [07_FIR_x2/Questa/FIR_x2.bat:5-14](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/FIR_x2.bat#L5-L14)。
2. 对每一行 `vlog <路径>`，去掉 `../` 或 `../../` 前缀，记录「文件名 + 所在目录」。
3. 用 `git ls-files`（或直接看仓库目录）核对每个文件确实存在。

**需要观察的现象**：列表里一共出现 9 个 `.v` 文件（8 个 RTL + 1 个 testbench），分布在 7 个编号目录中。

**预期结果**：应当得到如下对照（已为你整理）：

| bat 行 | 文件 | 所属目录 |
|--------|------|---------|
| L5 | `FIR_x2.v` | 07_FIR_x2 |
| L6 | `DPRAM_CONT.v` | 01_DPRAM_CONT |
| L7 | `SDPRAM_SINGLECLK.v` | 02_DATA_BUFFER |
| L8 | `DATA_BUFFER.v` | 02_DATA_BUFFER |
| L9 | `SPROM_CONT.v` | 03_SPROM_CONT |
| L10 | `SPROM.v` | 04_FIR_COEF |
| L11 | `FIR_COEF.v` | 04_FIR_COEF |
| L12 | `MULT.v` | 05_MULT |
| L13 | `ADD.v` | 06_ADD |
| L14 | `FIR_x2_TB.v` | 07_FIR_x2 |

可见 02 和 04 各贡献了「封装 + 原语」两个文件，这正是下一节要讲的分层。

#### 4.1.5 小练习与答案

**练习 1**：为什么 testbench 文件 `FIR_x2_TB.v` 出现在编译列表最后一行，而 RTL 文件排在前面？

> **参考答案**：testbench 例化了被测设计（DUT），编译器需要先看到 DUT 及其全部子模块的定义，才能解析 testbench 中的实例化语句。所以必须先编译所有 RTL，最后编译 testbench。

**练习 2**：如果新增一个第 12 个编号目录（例如 `12_sim_ref`），按照本仓库的命名约定，它应该放在哪个层级？

> **参考答案**：放在最外层、与 `01`~`11` 同级。仓库约定所有一级目录都用「两位数字 + 下划线 + 名称」的形式，数字表示阅读/引用顺序，因此新目录应紧接 `11_fir_gen` 之后编号为 `12`。

---

### 4.2 控制器与原语层级

#### 4.2.1 概念说明

只看编号目录会忽略一个关键的设计思想：**分层**。FIR_x2 把存储相关逻辑拆成了三层：

1. **底层存储原语（primitive）**：只关心「怎么把数据存进 RAM/ROM、怎么读出来」的纯存储单元。
   - `SDPRAM_SINGLECLK`（Simple Dual-Port RAM，单时钟）：双口 RAM，可写可读。
   - `SPROM`（Single-Port ROM）：单口 ROM，只读。
2. **上层控制器（controller）**：决定「在什么时刻、用什么地址读写」，产生写使能、读使能与地址。
   - `DPRAM_CONT`：驱动双口 RAM 的地址（环形缓冲）。
   - `SPROM_CONT`：驱动 ROM 的读地址（系数寻址 + 生成过采样时钟）。
3. **封装模块（wrapper）**：把「控制器 + 原语」绑成一个对外的整体。
   - `DATA_BUFFER` = `DPRAM_CONT` + `SDPRAM_SINGLECLK`。
   - `FIR_COEF` = `SPROM_CONT` + `SPROM`。

为什么要分层？因为存储原语与具体 FPGA 厂商强相关——README 明确建议**用各厂商官方 IP 替换这两个原语**。把它们隔离在最底层，移植时只需替换这一层，上层的控制器与封装逻辑完全不动。

#### 4.2.2 核心流程

整个项目的实例依赖关系（谁实例化谁）可以画成一棵树：

```
FIR_x2 (顶层, 07)
├── DATA_BUFFER (02)             ← 被顶层直接实例化
│   ├── DPRAM_CONT (01)          ← 控制器
│   └── SDPRAM_SINGLECLK (02)    ← RAM 原语
├── FIR_COEF (04)                ← 被顶层直接实例化
│   ├── SPROM_CONT (03)          ← 控制器
│   └── SPROM (04)               ← ROM 原语
├── MULT (05)                    ← 被顶层直接实例化（运算单元）
└── ADD (06)                     ← 被顶层直接实例化（运算单元）
```

由此得到一个重要结论：

- **被顶层 `FIR_x2` 直接实例化的有 4 个**：`DATA_BUFFER`、`FIR_COEF`、`MULT`、`ADD`。
- **不直接被顶层实例化、而是被封装模块二次实例化的有 4 个**：`DPRAM_CONT`、`SDPRAM_SINGLECLK`、`SPROM_CONT`、`SPROM`。

数据流向上，信号依次流经 `DATA_BUFFER →（取系数）FIR_COEF → MULT → ADD → 顶层输出饱和`，与目录编号 02→04→05→06→07 的顺序一致。

#### 4.2.3 源码精读

先看顶层如何直接实例化 4 个子模块。在 [07_FIR_x2/FIR_x2.v:101-165](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L101-L165) 中，顶层依次创建了 `u_DATA_BUFFER`、`u_FIR_COEF`、`u_MULT`、`u_ADD` 四个实例，并用内部 wire（如 `RDATA`、`COEF`、`MULT_DATA`、`ADD_DATA`）把它们串成数据通路。例如 [FIR_x2.v:103-115](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L103-L115) 实例化 `DATA_BUFFER`，把外部输入 `DATA_I` 接到它的 `WDATA_I`，把它的输出 `RDATA_O` 引到内部 `RDATA`。

再看「封装模块二次实例化原语」的典型——`DATA_BUFFER` 内部同时实例化了控制器与原语：

- [02_DATA_BUFFER/DATA_BUFFER.v:76-86](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v#L76-L86)：实例化控制器 `DPRAM_CONT`（实例名 `u_DPRAM_CONT`），它输出写使能 `WEN`、写地址 `WADDR`、读使能 `REN`、读地址 `RADDR`。
- [02_DATA_BUFFER/DATA_BUFFER.v:88-102](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v#L88-L102)：实例化 RAM 原语 `SDPRAM_SINGLECLK`（实例名 `u_SDPRAM_SINGLECLK`），把上面控制器产生的 `WEN/WADDR/REN/RADDR` 连到它的端口，完成「控制器指挥、原语存取」的分工。

系数通路是完全对称的结构：[04_FIR_COEF/FIR_COEF.v:82-92](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v#L82-L92) 实例化控制器 `SPROM_CONT`，[FIR_COEF.v:95-104](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/04_FIR_COEF/FIR_COEF.v#L95-L104) 实例化 ROM 原语 `SPROM`。这种「控制器 + 原语」成对出现的模式是理解整个仓库的钥匙。

#### 4.2.4 代码实践

**实践目标**：画出 7 个核心模块的实例依赖草图，并标注哪些是被顶层直接实例化的子模块。

**操作步骤**：

1. 打开 [07_FIR_x2/FIR_x2.v:101-165](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L101-L165)，找出 `FIR_x2` 内部出现的 4 个模块名与实例名。
2. 对其中两个封装模块（`DATA_BUFFER`、`FIR_COEF`），分别打开它们的 `.v` 文件，找出它们内部又实例化了哪些模块。
3. 用箭头画出依赖树（父模块 → 子模块），并在直接被顶层实例化的 4 个模块旁打勾。

**需要观察的现象**：`DPRAM_CONT`、`SDPRAM_SINGLECLK`、`SPROM_CONT`、`SPROM` 这 4 个名字只出现在封装模块内部，不出现在顶层 `FIR_x2.v` 中。

**预期结果**：依赖树与 4.2.2 节给出的树一致。直接被顶层实例化的是 `DATA_BUFFER`、`FIR_COEF`、`MULT`、`ADD`；而两个存储原语（`SDPRAM_SINGLECLK`、`SPROM`）与两个控制器（`DPRAM_CONT`、`SPROM_CONT`）位于第二层，由封装模块实例化。

#### 4.2.5 小练习与答案

**练习 1**：如果把项目移植到 Xilinx Vivado，需要替换哪些模块？替换后上层逻辑受影响吗？

> **参考答案**：只需替换两个存储原语 `SDPRAM_SINGLECLK` 和 `SPROM`（用 Vivado 的 Block Memory Generator / Distributed Memory 等 IP 替代）。上层的 `DPRAM_CONT`、`SPROM_CONT`、两个封装模块以及顶层、运算单元都不需要改动——这正是分层隔离原语的好处。

**练习 2**：`DATA_BUFFER` 和 `FIR_COEF` 都是「封装模块」，但它们各自封装的存储类型有何不同？

> **参考答案**：`DATA_BUFFER` 封装的是**双口 RAM**（`SDPRAM_SINGLECLK`，可写可读，存放不断更新的输入 PCM 数据）；`FIR_COEF` 封装的是**单口 ROM**（`SPROM`，只读，存放固定的滤波器系数）。一个存「变的数据」，一个存「不变的系数」。

---

### 4.3 数据文件与示例工程

#### 4.3.1 概念说明

除了 RTL，仓库还有 4 个非源码目录，提供运行与验证所需的「数据」和「工具」：

- **`08_hex/`**：存储初始化文件（`.hex`）。FIR 滤波器需要预先把系数写进 ROM，也需要给 RAM 一个初始值，这些 `.hex` 文件就是用十六进制写好的初值表，仿真时由 `$readmemh` 加载。
- **`09_txt/`**：测试 PCM 信号文件（`.txt`）。仿真时模拟一段音频输入（如 1 kHz 正弦、冲激、直流最大值），逐行写成一个采样点。
- **`10_Example/`**：7 款开发板的完整工程，打包成 `.qar`/`.zip`/`.gar` 等厂商归档格式，下载后可直接在对应工具链打开。
- **`11_fir_gen/`**：系数生成工具——用 Python 设计滤波器、用 AWK 把十进制系数转成十六进制。

为什么还需要 `08_hex`、`09_txt` 这两个集中目录？因为每个模块的 `Questa/` 子目录里也都放了一份仿真所需的数据副本（例如 [07_FIR_x2/Questa/](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/Questa/FIR_x2.bat) 目录下就有 `FIR512_x2_48000.hex`、`PCM_1kHz_44100fs_32bit.txt` 等）。`08_hex`、`09_txt` 是**集中维护的规范副本**，而各模块下的副本是为「就近仿真」准备的。

#### 4.3.2 核心流程

数据文件的命名遵循「`内容_参数`」约定，读懂名字就能猜出用途：

| 文件名模式 | 含义 |
|-----------|------|
| `FIR512_x2_48000.hex` | 512 抽头、2 倍过采样、目标 48 kHz 的**系数** |
| `BUFFER_INIT.hex` | 数据 RAM 的**初始值**（通常全 0） |
| `PCM_1kHz_44100fs_32bit.txt` | 1 kHz 正弦、44.1 kHz 采样、32 位字长的**测试音频** |
| `Impulse_44100Hz_32bit.txt` | 冲激信号测试源（可观察滤波器脉冲响应） |
| `DCMAX_44100Hz_32bit.txt` | 直流最大值测试源（可观察饱和行为） |

文件名中的 `512` 还呼应了上一讲的关键约束：**FIR 长度 = MCLK 频率 / 采样频率**。在 24.576 MHz MCLK、48 kHz 采样下，512 = 24.576 MHz / 48 kHz，所以用 `FIR512`。

`10_Example/` 下 7 个子目录对应 README 列出的 7 款已验证板卡，每个子目录放一个打包好的工程：

- [README.md:55-63](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/README.md#L55-L63)：列出了全部 7 个示例工程及其对应的开发板与工具版本。例如 `01_EK-10CL025U256` 用 Quartus Prime Lite v24.1，`05_Cmod-A7` 用 Vivado 2025.1，`07_TangPrimer20K` 用 Gowin FPGA Designer。

`11_fir_gen/` 内有三个文件：`fir_gen.py`（用 `scipy.signal.firwin` 设计低通 FIR 并量化为有符号整数）、`dec2hex.awk`（把十进制转成二补码十六进制）、`README.md`（说明生成流程）。

#### 4.3.3 源码精读

README 的 Notes 一节明确点出了 `.hex` 文件的用途与厂商差异：

- [README.md:42-44](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/README.md#L42-L44)：第 42 行说明 `SPROM` 与 `SDPRAM_SINGLECLK` 来自 AUDIY 的通用 IP 库，但建议改用厂商官方 IP；第 43 行重申滤波器长度等于 MCLK/采样频率；第 44 行给出一个关键移植提示——在 Vivado 下需把 `.hex` 改成 `.data` 文件。这解释了为什么系数会有两种存储格式。

`11_fir_gen/README.md` 给出了从浮点系数到硬件可用的十六进制文件的完整流程，其中的量化公式值得留意：

- [11_fir_gen/README.md:30-45](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/11_fir_gen/README.md#L30-L45)：先用 Python 生成有符号整数系数，再用 `dec2hex.awk` 转成定宽十六进制 `.data` 文件。这正是 `08_hex/` 中那些 `.hex` 文件的来源。

#### 4.3.4 代码实践

**实践目标**：把一个 `.hex` 文件名「翻译」成它的工程含义，并定位它在仓库中的所有副本。

**操作步骤**：

1. 在仓库中搜索 `FIR512_x2_48000.hex`，列出它出现的所有目录。
2. 对文件名做拆解：`FIR512` / `x2` / `48000` 分别代表什么。
3. 对照 [README.md:43](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/README.md#L43) 的「滤波器长度 = MCLK/采样频率」约束，验证 `512` 这个数字。

**需要观察的现象**：同一个 `.hex` 文件会同时出现在 `08_hex/`、`04_FIR_COEF/Questa/`、`05_MULT/Questa/`、`06_ADD/Questa/`、`07_FIR_x2/Questa/` 等多个位置。

**预期结果**：

- `FIR512` = 512 个抽头；`x2` = 2 倍过采样；`48000` = 目标输出采样率 48 kHz。
- 由约束 `长度 = MCLK/fs`，当 `fs = 48 kHz`、`长度 = 512` 时，`MCLK = 512 × 48 kHz = 24.576 MHz`，与项目典型 MCLK 一致。
- 多处副本存在的意义是让每个模块都能就近完成独立仿真。

> 待本地验证：你可以在本地用搜索工具确认 `.hex` 副本的数量，本讲基于仓库文件清单给出的结论。

#### 4.3.5 小练习与答案

**练习 1**：`PCM_1kHz_44100fs_32bit.txt` 和 `FIR512_x2_48000.hex` 这两个文件，哪个进 ROM、哪个进测试激励？

> **参考答案**：`FIR512_x2_48000.hex` 是**系数**，用 `$readmemh` 加载进 `SPROM`（ROM 原语）；`PCM_1kHz_44100fs_32bit.txt` 是**测试音频信号**，由 testbench 读取后作为 `DATA_I` 送进 `DATA_BUFFER`，并不预存进硬件存储。

**练习 2**：README 第 44 行说「Vivado 下要把 `.hex` 改成 `.data`」。这是文件内容变了，还是只是扩展名/格式变了？

> **参考答案**：主要是**文件格式与扩展名**变了。Questa 用 `$readmemh` 读取 `.hex`；Vivado 的存储 IP 读取 `.data`（即内存初始化文件 `.coe`/`.mem` 类格式，二者地址/数据排列规则不同）。`11_fir_gen/dec2hex.awk` 输出的就是 `.data` 扩展名，可见工具链已经考虑了这种差异。

---

## 5. 综合实践

把本讲三块内容串起来，完成一张**完整的「仓库模块地图」**：

1. 在一张纸上画出从 `FIR_x2`（顶层）出发的实例依赖树（参考 4.2.2），把 7 个核心 RTL 模块全部挂上去。
2. 在每个模块旁标注它的「类别」：控制器 / 原语 / 封装 / 运算 / 顶层。
3. 在树的一侧，用箭头标出一条输入 PCM 样点的数据通路：`DATA_I → DATA_BUFFER →（系数来自 FIR_COEF）→ MULT → ADD → 顶层饱和 → DATA_O`。
4. 在图的角落，列出本仓库的两类「外部数据」分别喂给谁：`.hex`（系数/RAM 初值）喂给原语，`.txt`（测试信号）喂给 testbench。
5. 最后标注一个移植要点：换 FPGA 厂商时，只动两个原语，并把 `.hex` 换成对应格式。

完成后，这张图就是你阅读后续每一讲的「导航图」——每讲深入某个模块时，回到这张图定位它的位置。

## 6. 本讲小结

- 仓库用 `01`~`11` 编号目录组织，编号大致沿音频信号流动方向递进；每个核心目录都自包含 RTL、testbench 与 Questa 仿真脚本。
- 7 个核心 RTL 目录是 `01_DPRAM_CONT`、`02_DATA_BUFFER`、`03_SPROM_CONT`、`04_FIR_COEF`、`05_MULT`、`06_ADD`、`07_FIR_x2`。
- 设计采用三层分层：底层存储原语（`SPROM`/`SDPRAM_SINGLECLK`）、上层控制器（`SPROM_CONT`/`DPRAM_CONT`）、封装模块（`FIR_COEF`/`DATA_BUFFER`）。
- 顶层 `FIR_x2` 直接实例化 4 个模块（`DATA_BUFFER`/`FIR_COEF`/`MULT`/`ADD`）；两个原语和两个控制器由封装模块二次实例化。
- `08_hex`/`09_txt` 是集中维护的数据文件副本，`10_Example` 提供 7 款板卡的完整工程，`11_fir_gen` 是系数生成与格式转换工具。
- 存储原语被刻意隔离在最底层，便于按厂商替换；Vivado 下还需把 `.hex` 改为 `.data` 格式。

## 7. 下一步学习建议

有了这张模块地图，下一步有两条可选路径：

- **先跑通仿真**：进入 u1-l3《如何运行仿真：Questa 仿真流程与测试激励》，亲手把 `FIR_x2.bat` 跑起来，在波形里看到过采样前后的 `LRCK` 与 `LRCKx2`。这是建立直观信心的最快方式。
- **先看顶层架构**：进入 u2-l1《顶层 FIR_x2 模块：端口、参数与实例化图谱》，深入 [07_FIR_x2/FIR_x2.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v)，把本讲画出的依赖树与真实端口、参数对应起来。

建议优先选择 u1-l3，先用一次成功的仿真确认环境，再带着「能看到波形」的底气进入 u2 的源码精读。
