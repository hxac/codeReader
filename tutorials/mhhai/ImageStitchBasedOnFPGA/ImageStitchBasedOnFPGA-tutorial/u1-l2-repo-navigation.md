# 仓库结构与模块导航

## 1. 本讲目标

上一讲（u1-l1）我们只读了 `README.md`，建立了「项目在做什么、为什么用 FPGA」的全局认识。本讲我们要**真正打开仓库里的每一个文件**，把「地图」变成「实地」。

读完本讲，你应当能够：

1. **说出仓库的目录结构**：根目录有哪些文件、有哪些子目录、每个目录里放了什么。
2. **判断这是一个什么样的仓库**：它是一个「可直接编译运行的完整工程」，还是一个「按模块收集的源码片段集」？这决定了你阅读它的方式。
3. **说清每个模块在拼接流水线中的角色**：哪个做采集缓存、哪个做投影、哪个做缝合线、哪个做调试。
4. **建立「软件算法 → 硬件实现」的模块对应关系**：明白 `圆柱面投影.cpp`（软件）里的每一个关键概念，在 `圆柱面投影.v`（硬件）里对应到了什么。

本讲**不深入算法细节**（那是 u2 的事），只做「导航」——让你打开任何一个文件都不迷路。

---

## 2. 前置知识

本讲会用到上一讲（u1-l1）的几个结论，这里简要回顾（不重复展开）：

- **软件参考实现**：项目用 OpenCV（C++）先跑通整套拼接算法，作为「正确答案」。
- **硬件实现**：再把关键步骤用 Verilog 搬到 FPGA 上，追求实时。
- **数据流向**（来自 u1-l1）：采集 → 24→64bit 异步 FIFO → DDR3 → 圆柱面投影 → 缝合线 → 融合 → 输出。

此外，本讲会浏览 Verilog 和 C++ 文件的**头部**，需要你大概知道：

- **Verilog（.v）**：硬件描述语言。一个 `.v` 文件通常定义一个 `module`（模块），用 `input`/`output` 声明对外引脚，用 `always` 描述时序逻辑。本讲只看模块的「端口」和「它例化了谁」，不细究语法。
- **C++/OpenCV（.cpp）**：软件程序。本项目的 `圆柱面投影.cpp` 是一个带 `main()` 的可执行程序，调用 OpenCV 的拼接函数。
- **IP 核（IP core）**：FPGA 厂商（本项目用 Xilinx）提供的现成功能块，如 CORDIC（算三角函数）、MIG（驱动 DDR3）、时钟向导（生成时钟）。它们在 Verilog 里像「子模块」一样被例化（调用）。

> 本讲的定位是「带你看路牌」，遇到看不懂的语法不必纠结，后面专讲会逐个拆解。

---

## 3. 本讲源码地图

本讲会浏览仓库的**全部源码文件**，但只精读其中 4 个的「头部」，用以说明结构与对应关系。

| 文件 | 语言 | 行数 | 作用 | 本讲用法 |
| --- | --- | --- | --- | --- |
| `README.md` | Markdown | 17 | 项目自述（u1-l1 已精读） | 引用结论 |
| `圆柱面投影.cpp` | C++/OpenCV | 501 | 圆柱面投影的**软件参考实现** | ✅ 看头部 + `main` 流程 |
| `圆柱面投影.v` | Verilog | 119 | 圆柱面投影的**硬件实现** | ✅ 看端口 + IP 例化 |
| `DDR3控制/mem_burst.v` | Verilog | 233 | DDR3 突发读写控制器（封装 MIG） | ✅ 看端口 |
| `DDR3控制/mem_test.v` | Verilog | 126 | DDR3 读写自检 | 看角色 |
| `动态规划法寻找最佳缝合线/DynamicSeam.v` | Verilog | 271 | 动态规划找最佳缝合线 | 看角色 + IP 例化 |
| `UART串口通信/uart_rx.v` | Verilog | 170 | 串口接收 | 看角色（独立模块） |
| `UART串口通信/uart_tx.v` | Verilog | 160 | 串口发送 | 看角色（独立模块） |

> 提示：行数是用 `wc -l` 得到的真实值，不是估算。本讲后面给目录树时也会用到这些数字。

---

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 仓库结构**：仓库长什么样、有哪些文件、缺什么。
- **4.2 模块导航**：每个模块做什么、以及「软件 → 硬件」的对应关系。

---

### 4.1 仓库结构

#### 4.1.1 概念说明

拿到一个陌生项目，第一步永远不是读代码，而是**看结构**。一个仓库的结构能告诉你很多信息：

- 它是「完整工程」还是「源码片段集」？
- 有没有构建系统（Makefile、CMakeLists、Vivado 工程 `.xpr`）？
- 文件按什么逻辑组织（按语言、按功能、按模块）？

理解仓库结构，就像拿到一张「楼层平面图」——之后进任何一间房（任何一个文件），你都知道自己在第几层、这间房属于哪个功能区。

本项目 `ImageStitchBasedOnFPGA` 的结构非常**小巧**：全部源码加起来不到 1600 行，核心源文件只有 8 个。但它的组织方式有一个重要特点，我们先看实物，再下结论。

#### 4.1.2 核心流程

用 `git ls-files`（列出 git 跟踪的所有文件）可以得到仓库的**真实文件清单**，据此画出目录树：

```text
ImageStitchBasedOnFPGA/
├── README.md                                  # 项目自述（u1-l1 精读）
├── .gitignore
├── 圆柱面投影.cpp                              # 软件参考实现（OpenCV）
├── 圆柱面投影.v                                # 硬件实现（Verilog）
├── UART串口通信.v                              # ⚠ 空文件（0 字节），见 4.1.3
├── DDR3控制/
│   ├── mem_burst.v                            # DDR3 突发读写控制器
│   └── mem_test.v                             # DDR3 读写自检
├── UART串口通信/
│   ├── uart_rx.v                              # 串口接收
│   └── uart_tx.v                              # 串口发送
└── 动态规划法寻找最佳缝合线/
    └── DynamicSeam.v                          # 动态规划找缝合线
```

观察这棵树，可以得出三条**结构性结论**：

1. **按「功能模块」分目录**。每个中文命名的目录就是一个独立功能：DDR3 控制、UART 串口、缝合线。圆柱面投影的 `.cpp`/`.v` 则平铺在根目录。
2. **软件与硬件并列存放**。`圆柱面投影.cpp`（软件）和 `圆柱面投影.v`（硬件）同名并列，作者显然是有意让你把它们对照着看——这正是本讲 4.2 要建立的关系。
3. **没有构建系统、没有顶层集成**。仓库里**没有** Makefile、CMakeLists、Vivado 工程文件（`.xpr`）、约束文件（`.xdc`），也没有一个「把所有模块连起来」的顶层 `top` 模块。这说明它是一个**按模块收集的源码片段集**，而不是一个「克隆下来就能编译/综合的完整工程」。

> 第三点很关键：阅读本仓库时，你要把每个文件当成**独立的参考实现**来读，而不是期望 `make` 一下就出全景图。后续讲义里凡是涉及「把模块连起来」的系统级话题，都会标注哪些部分（IP 核、顶层、约束）**不在仓库内**，属于「待确认」。

#### 4.1.3 源码精读

**(A) 文件清单：用 `git ls-files` 自己确认**

不要只信上面的目录树——你可以用只读 git 命令亲自核对。仓库跟踪的全部文件就是上面那 9 个（含 `.gitignore`）。命令输出会列出：

```text
.gitignore
README.md
DDR3控制/mem_burst.v
DDR3控制/mem_test.v
UART串口通信.v
UART串口通信/uart_rx.v
UART串口通信/uart_tx.v
动态规划法寻找最佳缝合线/DynamicSeam.v
圆柱面投影.cpp
圆柱面投影.v
```

注意根目录下有一个 `UART串口通信.v`，和一个同名的**目录** `UART串口通信/`。这两者重名很容易让人混淆。

**(B) 一个需要警惕的细节：空文件 `UART串口通信.v`**

根目录的 `UART串口通信.v` 是一个 **0 字节的空文件**。你可以亲自用 `wc -c` 核对：

```text
0 UART串口通信.v
```

真正有内容的串口代码在 `UART串口通信/` 目录下的 `uart_rx.v`（170 行）和 `uart_tx.v`（160 行）。所以导航时要注意：

- 读串口代码 → 进 `UART串口通信/` **目录**，看 `uart_rx.v` / `uart_tx.v`。
- 根目录那个同名的空文件 → 可以忽略，它既不是入口、也没有内容。

> 为什么会存在一个空的同名文件？仓库里没有说明，我们**不臆测作者意图**，只需要知道「它在、但为空」这个事实即可。阅读个人/毕业项目时，这类「遗留小文件」很常见，识别并跳过它们是一项实用的导航技能。

**(C) 没有 build / 没有顶层：再次确认「片段集」定位**

你可以在仓库里搜索常见的构建/工程文件，结果都会是空的：没有 `Makefile`、`CMakeLists.txt`、`*.xpr`（Vivado 工程）、`*.xdc`（约束）。而各个 Verilog 模块还**例化**了若干 IP 核（如 `cordic_0`、`clk_wiz_0`、`mig_7series_0`），这些 IP 的配置文件（`.xci`/`.xco`）也不在仓库里。

例如 `圆柱面投影.v` 末尾就例化了一个 `cordic_0`（CORDIC 三角函数 IP），参见 [圆柱面投影.v:L113-L118](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L113-L118)：

```verilog
cordic_0 uut(
    .s_axis_phase_tvalid(1'b1),        // 相位输入有效
    .s_axis_phase_tdata(phase_tdata),  // 相位输入（角度）
    .m_axis_dout_tvalid(dout_tvalid),  // 输出有效
    .m_axis_dout_tdata(dout_tdata)     // 输出（sin/cos）
);
```

而 `cordic_0` 这个 IP 的定义并不在仓库中。同理，`DynamicSeam.v` 里例化了 `clk_wiz_0`（时钟向导）和 `mig_7series_0`（DDR3 控制器 IP），参见 [DynamicSeam.v:L38-L47](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L38-L47)，它们也都不在仓库里。这进一步印证了「片段集」的定位。

#### 4.1.4 代码实践

这是一个**源码阅读 + 命令核对型实践**。

1. **实践目标**：亲自核实仓库结构，建立「东西在哪、缺什么」的第一手印象。
2. **操作步骤**：
   - 在仓库根目录执行只读命令 `git ls-files`，把输出和本讲 4.1.3 (A) 的清单对比。
   - 执行 `wc -l README.md 圆柱面投影.cpp 圆柱面投影.v DDR3控制/mem_burst.v` 等命令，核对每个文件的行数。
   - 执行 `wc -c UART串口通信.v`，确认它是 0 字节。
   - 尝试用 `ls *.xpr *.xdc Makefile CMakeLists.txt 2>/dev/null` 查找构建/工程文件（预期：无输出）。
3. **需要观察的现象**：你会看到仓库只有 9 个被跟踪的文件，没有构建系统，根目录有一个 0 字节的空 `.v` 文件。
4. **预期结果**：你得出与本讲一致的结论——这是一个「按模块收集的源码片段集」，需要带 IP 核和工程文件才能综合，但用来**阅读和学习**完全够用。
5. **待本地验证**：命令本身可在任何类 Unix 终端运行；如果你在 Windows 下，可用 `dir` 或 Git Bash 替代。

#### 4.1.5 小练习与答案

**练习 1**：仓库根目录下既有 `UART串口通信.v` 文件，又有 `UART串口通信/` 目录。要阅读串口收发代码，你应该打开哪个？

> **参考答案**：打开 `UART串口通信/` **目录**里的 `uart_rx.v` 和 `uart_tx.v`。根目录的 `UART串口通信.v` 是 0 字节空文件，没有内容。

**练习 2**：仓库里没有 Makefile、没有 Vivado 工程文件，这对你「使用」这个仓库意味着什么？

> **参考答案**：意味着不能「一键编译/综合出全景图」。它是一个供阅读和参考的源码片段集——每个模块可以单独学习、单独仿真，但要真正在 FPGA 上跑通，还需要你自己建工程、添加缺失的 IP 核（`cordic_0`/`clk_wiz_0`/`mig_7series_0`）和约束文件。

---

### 4.2 模块导航

#### 4.2.1 概念说明

知道了文件「在哪」，还要知道它们「干什么」以及「互相怎么配合」。模块导航要回答三个问题：

1. **每个模块在拼接流水线里扮演什么角色？**（采集缓存 / 投影 / 缝合线 / 调试）
2. **哪些模块是「软件版 vs 硬件版」的对照关系？**（核心是圆柱面投影）
3. **哪些模块是独立的、和拼接主线无关？**（UART）

其中第 2 点——**软件算法 → 硬件实现的对应关系**——是本讲最重要的产出。因为整个项目的方法论就是「先用 OpenCV 跑对（软件），再把关键步骤搬到 FPGA（硬件）」。理解了这张对应表，你以后看任何一个硬件模块，都能在软件里找到它的「正确答案」做参照。

#### 4.2.2 核心流程

先把每个模块的角色放进 u1-l1 给出的数据流向里，看看它们各守哪一段：

```text
7 路摄像头(24bit)
   │
   ▼
[采集]  ── 采集/白平衡模块：未收录（u1-l1 难点①）
   │
   ▼
[位宽转换] ── 24→64bit 异步 FIFO：README 描述，代码未收录（u1-l1 难点③）
   │
   ▼
[存储]  ── DDR3控制/mem_burst.v  ：突发读写控制器
   │       DDR3控制/mem_test.v   ：读写自检（验证 mem_burst）
   │
   ▼
[投影]  ── 圆柱面投影.v  ：硬件实现（CORDIC + 定点矩阵乘）
   │       圆柱面投影.cpp：软件参考实现（OpenCV）
   │
   ▼
[缝合线] ── 动态规划法寻找最佳缝合线/DynamicSeam.v：DP 找最佳切割线
   │
   ▼
[融合/输出] ── 融合模块：未独立收录（u1-l1 难点②）
   │
   ▼
全景输出

旁路（独立调试通道）：
   └─ UART串口通信/uart_rx.v、uart_tx.v ：串口收发，与拼接主线无直接关系
```

可以看到：

- **主线（采集→存储→投影→缝合线→融合）**：仓库收录了其中的「存储、投影、缝合线」三段；「采集、位宽转换、融合」三段**未收录**或仅 README 描述。
- **旁路（UART）**：串口模块是独立的通用收发器，和图像拼接没有数据关系，通常用于下板后调试、参数下发、结果回传。

> 一个导航经验：FPGA 工程里常有「旁路调试模块」。看到与主线无关的简单模块（如 UART），先怀疑它是「调试/通信通道」，而不是主计算通路。

#### 4.2.3 源码精读

下面分别打开每个模块的「头部」，确认它的角色。重点放在**软件→硬件的对应表**。

**(A) 软件参考实现：`圆柱面投影.cpp` 的 `main` 流程**

`圆柱面投影.cpp` 是一个带 `main()` 的 C++ 程序，它用 OpenCV 把整套图像拼接流水线跑了一遍，这就是「正确答案」。它的 `main` 按顺序调用了 OpenCV 的各个组件，参见 [圆柱面投影.cpp:L267-L497](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L267-L497)。流水线阶段与行号对照如下：

| 阶段 | OpenCV 组件 | 大致行号 |
| --- | --- | --- |
| 特征提取 | `OrbFeaturesFinder` | L278 |
| 特征匹配 | `BestOf2NearestMatcher` | L289 |
| 相机参数估计 | `HomographyBasedEstimator` | L296 |
| 光束平差 | `BundleAdjusterRay` | L310-L315 |
| 圆柱面投影（映射） | `CylindricalWarper` + 自定义 `warp` | L345、L355 |
| 曝光补偿 | `ExposureCompensator` | L369 |
| 缝合线查找 | `GraphCutSeamFinder` | L388 |
| 图像融合 | `FeatherBlender` | L449-L452 |

注意：仓库收录的 `圆柱面投影.cpp` 里 `main` 只处理了 `num_images = 2`（两幅图，见 [圆柱面投影.cpp:L271](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L271)），用于验证算法；真正的「七路」是在 FPGA 上并行重复这套流程。这条流水线的逐段精读留到 u2 单元。

**(B) 核心：软件 → 硬件对应表**

这是本讲最重要的内容。`圆柱面投影.cpp`（软件）里的关键概念，在 `圆柱面投影.v`（硬件）里都能找到对应物：

| 概念 | 软件实现（圆柱面投影.cpp） | 硬件实现（圆柱面投影.v） |
| --- | --- | --- |
| 焦距尺度 scale | `float scale = 2707.47f;`（[L30](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L30)） | 定点常数 `coe`（[L30](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L30)） |
| 投影映射里的 sin/cos | `sinf(u)`/`cosf(u)`（[L56-L58](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L56-L58)） | CORDIC IP `cordic_0`（[L113-L118](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L113-L118)） |
| 相机矩阵 K·R⁻¹ | `k_rinv[9]` 浮点数组（[L116-L119](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L116-L119)） | 定点系数 `k_inv0~k_inv8`（[L62-L70](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L62-L70)） |
| 目标图像范围 | `detectResultRoi` 算出 dst_tl/dst_br（[L68-L91](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L68-L91)） | 寄存器 `dst_tl_x/dst_br_x/dst_tl_y/dst_br_y`（[L56-L59](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L56-L59)） |
| 双线性插值的 4 个权重 | `weight` 表（col*4，[L195-L206](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L195-L206)） | `weight_x00/weight_y00/...` 由 floor/ceil 生成（[L93-L100](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L93-L100)） |

举两个直观的例子，让你感受「同一件事，软件和硬件各做一遍」：

- **scale 的对应**：软件里就是一个普通浮点数 `float scale = 2707.47f;`（[圆柱面投影.cpp:L30](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L30)）；硬件里浮点数难做，于是把它转成**定点二进制** `coe = 24'b0_0_0000_0000_0001_1000_0011_01`（[圆柱面投影.v:L30](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L30)）。两个值代表的是同一个数，只是一个用十进制浮点、一个用定点二进制。
- **sin/cos 的对应**：软件里直接调数学库 `sinf(u)`/`cosf(u)`（[圆柱面投影.cpp:L56-L58](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp#L56-L58)）；硬件里没有「数学库」，于是用 **CORDIC 算法 IP** 迭代算出 sin/cos，输入相位 `phase_tdata`、输出打包的 sin/cos（[圆柱面投影.v:L113-L118](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L113-L118)）。

> 这张对应表请牢记。后面 u2 单元会逐行验证「软件的浮点算法如何变成硬件的定点电路」，而本讲只需要你「知道两边是对得上的」。

**(C) 存储子系统：`DDR3控制/`**

DDR3 子系统有两个文件，是「控制器 + 自检」的典型搭配：

- **`mem_burst.v`（控制器）**：封装 Xilinx MIG IP 的应用接口（`app_cmd`/`app_addr`/`app_en`/`app_wdf_*` 等信号），用状态机实现 DDR3 的**突发读写**。端口定义参见 [mem_burst.v:L3-L40](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L3-L40)，状态机的各个状态（`IDLE`/`MEM_READ`/`MEM_WRITE`/...）定义在 [mem_burst.v:L44-L51](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L44-L51)。它在系统里的角色是「给投影/缝合线模块提供一个好用的突发读写接口」。
- **`mem_test.v`（自检）**：先往 DDR3 写一组递增模式数据，再读回比对，用 `error` 信号报告是否一致，参见 [mem_test.v:L32-L38](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_test.v#L32-L38)。它本身**不是拼接主线**，而是用来验证 `mem_burst` 是否可靠——典型的「先验证积木，再搭房子」。

注意 `mem_burst.v` 把 MIG 的应用接口直接作为自己的端口暴露出来（`app_*` 那一排信号），也就是说**真正的 DDR3 驱动（MIG IP）不在仓库里**，`mem_burst` 只是 MIG 之上的「应用层封装」。这与 4.1 得出的「片段集」结论一致。

**(D) 缝合线：`动态规划法寻找最佳缝合线/DynamicSeam.v`**

这个模块负责在两幅图的**重叠区**里，用动态规划找一条「代价最小」的切割线（缝合线），让拼接痕迹最不明显。它的角色是流水线里「投影之后、融合之前」的那一段。

模块头部声明了存放重叠区两行数据和 DP 结果的数组，参见 [DynamicSeam.v:L30-L33](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L30-L33)：

```verilog
reg [31:0] row1 [OVERLAPWIDTH : 0];   // 第一幅图的重叠行
reg [31:0] row2 [OVERLAPWIDTH : 0];   // 第二幅图的重叠行
reg [31:0] cost [OVERLAPWIDTH : 0];   // DP 累积代价
reg [31:0] coordinate [OVERLAPWIDTH : 0]; // DP 路径坐标
```

它的状态机很简洁（`IDLE`/`READ`/`SeamFind`），参见 [DynamicSeam.v:L54-L56](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L54-L56)。但要注意文件开头有一句重要提示「外部代码不能综合」，参见 [DynamicSeam.v:L21-L23](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L21-L23)——意思是这个文件包含不能直接综合的写法（比如在 `always` 块里例化模块、`localparam` 误用等），它更像一份「算法思路草稿」。这一点 u4 单元会专门讲。

**(E) 旁路：`UART串口通信/`**

`uart_rx.v`（接收）和 `uart_tx.v`（发送）是标准的 UART 串口收发器，模块名分别是 `uart_rx`（[uart_rx.v:L29](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L29)）和 `uart_tx`（[uart_tx.v:L29](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_tx.v#L29)）。它们与图像拼接**没有数据流上的直接关系**，属于通用调试通道（下板后收发命令、回传状态）。

之所以把它们放在仓库里，是因为它们**独立、简单、能跑**，非常适合作为阅读复杂状态机前的「热身」——这正是下一讲 u1-l3 的用途。

#### 4.2.4 代码实践

这是本讲的主实践任务（对应大纲里的实践要求）。

1. **实践目标**：画一张**模块关系图**，标注数据从「摄像头采集 → DDR3 存储 → 圆柱面投影 → 缝合线 → 融合输出」的流向，并在每个环节标注对应的源码文件。
2. **操作步骤**：
   - 参考 4.2.2 的数据流向骨架，把它画成一张更完整的图（纸笔或任何画图工具均可）。
   - 对每个环节，标上「仓库里有没有对应文件」「文件名是什么」。
   - 在「圆柱面投影」这一格，额外标出它的「软件版/硬件版」双文件（`圆柱面投影.cpp` / `圆柱面投影.v`）。
   - 用虚线把 UART 模块画成「旁路调试通道」，表明它不接入主线。
3. **需要观察的现象**：画完你会发现，主线里「采集 / 位宽转换 / 融合」三段没有对应文件（或仅有 README 描述），而「存储 / 投影 / 缝合线」三段有完整文件。
4. **预期结果（示例骨架）**：

   ```text
   [采集]      → (未收录)
   [24→64 FIFO]→ (README 描述，未收录)
   [DDR3 存储] → mem_burst.v + mem_test.v
   [圆柱面投影]→ 圆柱面投影.cpp(软件) / 圆柱面投影.v(硬件)
   [缝合线]    → DynamicSeam.v
   [融合输出]  → (未独立收录)

   旁路: uart_rx.v / uart_tx.v (调试)
   ```

5. **待本地验证**：本实践为画图与标注型，无需运行命令。若想进一步确认某文件的角色，可打开它的模块头部（端口列表）核对。

#### 4.2.5 小练习与答案

**练习 1**：`圆柱面投影.cpp` 里的 `float scale = 2707.47f`，在 `圆柱面投影.v` 里对应的是哪一个量？

> **参考答案**：对应硬件里的定点常数 `coe`（[圆柱面投影.v:L30](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L30)）。软件用浮点十进制表示焦距尺度，硬件把它转成了定点二进制，两者是同一个数值的不同表示。

**练习 2**：`mem_test.v` 属于拼接主线吗？为什么要单独写它？

> **参考答案**：不属于主线。它是一个**自检模块**，用来验证 `mem_burst.v` 控制器「写进去的数据能不能正确读回来」。先验证底层存储可靠，再让上层模块（投影、缝合线）放心使用 DDR3，这是典型的「积木验证」工程习惯。

**练习 3**：`DynamicSeam.v`、`圆柱面投影.v`、`mem_burst.v` 都例化了不在仓库里的 IP 核。请列出它们各自例化的 IP。

> **参考答案**：
> - `圆柱面投影.v` 例化了 `cordic_0`（CORDIC 三角函数 IP）。
> - `DynamicSeam.v` 例化了 `clk_wiz_0`（时钟向导）和 `mig_7series_0`（DDR3 控制 IP）。
> - `mem_burst.v` 本身**不直接例化** MIG，而是把 MIG 的应用接口（`app_*`）作为自己的端口暴露出来，等待上层（如 `DynamicSeam.v`）去例化 MIG 并对接。

---

## 5. 综合实践

把 4.1（结构）和 4.2（导航）串起来，做一份**「仓库导航手册」**，作为你后续阅读源码时的随身参考。

**任务：产出一张「文件 → 角色 → 软件/硬件对应 → 后续讲义」的总览表**

1. **实践目标**：检验你是否能 (a) 复述仓库结构；(b) 说出每个模块的角色；(c) 标出软件-硬件对应关系；(d) 知道每个模块的精读安排在后面哪一讲。
2. **操作步骤**：
   - 第一列：列出仓库全部 8 个源码文件。
   - 第二列：一句话写清它在流水线中的角色（采集/存储/投影/缝合线/调试/自检）。
   - 第三列：若是投影相关，标出它的「软件版/硬件版」对应文件。
   - 第四列：标注它是否依赖未收录的 IP 核。
   - 第五列：标注后续精读讲义（提示：UART→u1-l3、投影软件→u2-l1~l3、投影硬件→u2-l4、DDR3→u3、缝合线→u4、定点/IP/系统→u5）。
3. **需要观察的现象**：填完表后，你会对「每个文件什么时候读、为什么读」一目了然，再也不会在仓库里迷路。
4. **预期结果（示例片段）**：

   | 文件 | 角色 | 软件/硬件对应 | 依赖外部 IP | 后续讲义 |
   | --- | --- | --- | --- | --- |
   | `圆柱面投影.cpp` | 投影（软件参考实现） | 软件版 | 否 | u2-l1~l3 |
   | `圆柱面投影.v` | 投影（硬件实现） | 硬件版 | cordic_0 | u2-l4、u5-l1 |
   | `DDR3控制/mem_burst.v` | DDR3 突发读写 | — | （暴露 MIG 接口） | u3-l1 |
   | `DDR3控制/mem_test.v` | DDR3 自检 | — | 否 | u3-l2 |
   | `DynamicSeam.v` | 缝合线（DP） | — | clk_wiz_0、mig_7series_0 | u4-l1 |
   | `uart_rx.v` / `uart_tx.v` | 串口调试（旁路） | — | 否 | u1-l3 |

5. **待本地验证**：本实践为制表型，无需运行命令。

---

## 6. 本讲小结

- 仓库是一个**按模块收集的源码片段集**：9 个跟踪文件、约 1600 行代码，按功能分目录（`DDR3控制/`、`UART串口通信/`、`动态规划法寻找最佳缝合线/`），圆柱面投影的 `.cpp`/`.v` 平铺在根目录。
- 仓库**没有构建系统、没有顶层集成模块、IP 核未收录**；不能一键综合，但非常适合逐模块阅读学习。
- 根目录有一个 **0 字节的空文件 `UART串口通信.v`**，真正的串口代码在同名的 `UART串口通信/` 目录下。
- 模块角色：`mem_burst.v`（DDR3 突发控制器）+ `mem_test.v`（自检）管存储；`圆柱面投影.v`/`.cpp` 管投影；`DynamicSeam.v` 管缝合线；`uart_rx/tx.v` 是独立调试旁路。
- 最关键的产出是**「软件 → 硬件」对应表**：`scale↔coe`、`sin/cos↔CORDIC`、`K·R⁻¹↔k_inv`、`dst_tl/br↔寄存器`、`weight 表↔weight_x/y`。后续讲义会逐对验证。
- 主线「采集/FIFO/融合」三段未收录，「存储/投影/缝合线」三段有文件——阅读时要清楚哪些是「有代码可读」、哪些只能依赖 README 描述（待确认）。

---

## 7. 下一步学习建议

1. **下一讲 u1-l3《Verilog 状态机热身：UART 串口收发》**：用最独立、最简单的 `uart_rx.v`/`uart_tx.v` 练手，学习 Verilog 模块端口、参数化、波特率分频和状态机。这是进入复杂状态机前的最好热身。
2. **想直接看算法主线**：可以跳到 u2-l1《图像拼接算法全景：OpenCV 拼接流水线》，从 `圆柱面投影.cpp` 的 `main` 开始，但建议先有 u1-l3 的 Verilog 基础再读 u2-l4（硬件版）。
3. **暂时不建议**：直接读 `DynamicSeam.v`（u4）。它包含不能综合的写法，需要先理解 DDR3 控制器和定点投影，否则容易误读。

> 一句话：本讲帮你把「文件 ↔ 角色 ↔ 软件/硬件对应」对上了号；下一步用 u1-l3 的 UART 热身，再正式进入算法与硬件的精读。
