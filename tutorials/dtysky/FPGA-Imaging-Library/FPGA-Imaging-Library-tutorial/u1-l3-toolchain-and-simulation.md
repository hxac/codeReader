# 工具链与仿真运行方式

## 1. 本讲目标

本讲要解决一个最实际的问题：**拿到 FPGA-Imaging-Library（以下简称 F-I-L）的源码后，怎么把它跑起来？**

读完本讲，你应当能够：

- 说清 Python、ModelSim、Vivado 这三种工具链各自负责哪一段工作。
- 对照 `ColorReversal/README.md` 把「配置图像 → 软件仿真 → 生成 dat → 功能仿真 → 转换比对」这五步完整走一遍。
- 看懂 `conf.json` 的配置约定，知道它为什么是一个数组、又如何从无参数的 `["default"]` 扩展到带参数的多组配置。
- 读懂 `sim.py` / `create.py` / `convert.py` / `compare.py` 这四个脚本各自的输入、输出和落盘路径，并理解「软件黄金模型」与「HDL 功能仿真结果」是如何自动配对、用 PSNR 打分的。

本讲承接 [u1-l1 项目总览](u1-l1-project-overview.md) 建立的「软硬一致性」理念和七大分类，以及 [u1-l2 目录结构](u1-l2-directory-structure.md) 讲过的单个 IP 标准目录布局。本讲不再重复目录约定，而是聚焦在这些目录之间**数据是如何流动的**。

## 2. 前置知识

在开始之前，用最通俗的话把几个术语说清楚：

- **PIL（Python Imaging Library）**：Python 处理图像的老牌库（现在的替代品是 Pillow）。本项目的软件仿真脚本用 `from PIL import Image` 来打开图片、逐像素运算、再存回图片。
- **黄金模型（Golden Model）**：一个「我们假定它永远正确」的参考实现。在 F-I-L 里，Python 脚本就是黄金模型——它用纯软件算出「正确结果」，用来给硬件实现打分。
- **testbench（测试平台）**：一段用来「驱动」被测硬件的 Verilog/SystemVerilog 代码。它负责产生时钟、复位、把图像数据喂给被测模块、再把模块输出收集起来。本讲的 `.do` 文件就是在 ModelSim 里启动 testbench 的脚本。
- **`.dat` 与 `.res`**：仿真用的两种纯文本中间文件。`.dat` 是喂给硬件的**输入激励**（像素写成二进制串）；`.res` 是硬件跑完后的**输出结果**（像素写成十进制）。它们都不入库，是运行时产物。
- **PSNR（峰值信噪比）**：衡量两张图像差异的指标，单位是分贝（dB），越大表示越相似。本讲第 4.4 节会给出公式。
- **Python 2.7**：本项目脚本写于 2015 年前后，使用 Python 2 语法（例如 `print` 语句风格、`xrange`）。运行时需要 Python 2.7 解释器，而不是 Python 3。

> 名词提示：README 里出现的 `creat.py`、`covert.py` 是作者笔误，磁盘上的真实文件名是 `create.py`、`convert.py`。本讲一律以真实文件名为准。

## 3. 本讲源码地图

本讲以最简单的点运算 IP `Point/ColorReversal`（颜色取反）为样板，涉及的关键文件如下：

| 文件 | 所在目录 | 作用 |
| --- | --- | --- |
| `README.md` | `Point/ColorReversal/` | 给出五步仿真的操作说明，是本讲的「说明书」 |
| `conf.json` | `Point/ColorReversal/ImageForTest/` | 仿真配置：声明要跑哪些参数组合 |
| `sim.py` | `Point/ColorReversal/SoftwareSim/` | 软件黄金模型，算出 `<name>-soft.bmp` |
| `create.py` | `Point/ColorReversal/HDLSimDataGen/` | 把图片转成硬件激励 `<name>.dat` |
| `Run.do` / `RunOver.do` | `Point/ColorReversal/FunSimForHDL/` | ModelSim 启动 testbench 的脚本，产出 `<name>.res` |
| `convert.py` | `Point/ColorReversal/SimResCheck/` | 把 `.res` 还原成 `<name>-hdlfun.bmp` |
| `compare.py` | `Point/ColorReversal/SimResCheck/` | 配对软、硬两张结果图，算 PSNR 写报告 |
| `.gitignore` | `Point/ColorReversal/` | 解释为什么 `.dat`/`.res` 等中间产物不入库 |

> 对照提醒：第 4 节会反复回到这张表。建议你先在编辑器里把 `Point/ColorReversal/` 目录打开，边读边对照。

## 4. 核心概念与源码讲解

### 4.1 三种工具链与各自分工

#### 4.1.1 概念说明

F-I-L 的仿真不是单一工具完成的，而是三种工具接力。理解「谁干什么」是看懂五步流程的前提：

- **Python 2.7 + PIL**：负责所有「纯软件」工作——算黄金模型、把图片切成激励、把硬件结果还原成图片、比对打分。它是闭环里跑在 CPU 上的那一半。
- **ModelSim 10.1+**：负责 RTL **功能仿真**，也就是真正跑 Verilog 代码、看波形。它跑闭环里跑在「虚拟 FPGA」上的那一半。
- **Vivado**：负责把算法 IP 打包成可复用的 IP 核、做综合与实现、以及上板（`TestOnBoard/`）。它还提供仿真所需的 Xilinx 器件库。

#### 4.1.2 核心流程

三者的衔接关系可以用一句话概括：**Vivado 提供器件库 → 编译进 ModelSim → ModelSim 跑 RTL → Python 在两端做数据转换与打分**。

特别要注意 ModelSim 与 Vivado 之间的依赖：ModelSim 自带只能仿真通用 Verilog，而 F-I-L 的 testbench 会例化 Xilinx 仿真原语，所以必须先把 Vivado 的器件库编译进 ModelSim，否则功能仿真会报找不到库的错误。这一点 README 写得很明确。

#### 4.1.3 源码精读

README 对工具链版本和库依赖的说明在仿真章节开头：

[Point/ColorReversal/README.md#L7-L11](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/README.md#L7-L11) —— 强调本模块仿真只支持 RGB / 灰度 / 二值图，并要求 Python 2.7 + PIL。

[Point/ColorReversal/README.md#L27-L34](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/README.md#L27-L34) —— 功能仿真要求 ModelSim 10.1 以上，并明确「必须先把所有 Xilinx Vivado 库编译进 ModelSim」。这正是 Vivado 与 ModelSim 衔接的关键一句。

#### 4.1.4 代码实践

1. 实践目标：确认本机是否具备三件套。
2. 操作步骤：在命令行分别执行 `python --version`（或 `python2 --version`）、`vsim -version`（ModelSim）、`vivado -version`。
3. 需要观察的现象：能否正确打印各自版本号。
4. 预期结果：Python 显示 `2.7.x`、ModelSim 显示 `10.1` 或更高、Vivado 能正常启动。任一缺失都需先补齐。
5. 若本机没有这些工具，相关运行结果标注为「待本地验证」，可先只做源码阅读。

#### 4.1.5 小练习与答案

- **练习**：为什么即便我们只想跑功能仿真，也仍然需要安装 Vivado？
- **参考答案**：因为 F-I-L 的 testbench 依赖 Xilinx 仿真原语，这些原语的实现包含在 Vivado 的器件库里；ModelSim 自身没有这些库，必须由 Vivado 编译进去后才能仿真。

---

### 4.2 README 五步仿真流程

#### 4.2.1 概念说明

README 把一次完整仿真拆成五个阶段。这五步本质上是「喂同一张图，分别让软件和硬件算一遍，再看两边结果一不一致」。

#### 4.2.2 核心流程

五步与 README 小节的对应关系如下：

| 步骤 | 名称 | README 小节 | 产出 |
| --- | --- | --- | --- |
| 1 | 配置图像 | Preparing | `ImageForTest/` 里放好图片、改好 `conf.json` |
| 2 | 软件仿真 | Software simulation | `SimResCheck/<name>-soft.bmp`（黄金结果） |
| 3 | 生成 dat | Creat preparing data | `FunSimForHDL/<name>.dat`、`imgindex.dat`（硬件激励） |
| 4 | 功能仿真 | Functional simulation | `FunSimForHDL/<name>.res`（硬件输出） |
| 5 | 转换比对 | Comparing | `SimResCheck/<name>-hdlfun.bmp`、`compare_report.txt`（PSNR） |

数据在目录之间的流动可以画成下面这张图（实线箭头表示脚本把数据写到目标目录）：

```
ImageForTest/                 SoftwareSim/                 SimResCheck/
  *.bmp / *.jpg   ──sim.py──►   transform()      ──►       <name>-soft.bmp
  conf.json                                                      │
     │                                                           │（最终在此处比对）
     ▼                                                           │
HDLSimDataGen/                                                  │
  create.py                                                      │
     │                                                           │
     ▼                                                           │
FunSimForHDL/                                                    │
  <name>.dat  ──ModelSim(Run.do)──►  <name>.res                 │
  imgindex.dat                          │                       │
                                convert.py                      │
                                        ▼                       │
                                 <name>-hdlfun.bmp ──compare.py─┘──► compare_report.txt
```

读图要点：`-soft.bmp`（软件结果）和 `-hdlfun.bmp`（硬件结果）最终都落在 `SimResCheck/` 里，`compare.py` 就在这里把两张图配对、算 PSNR。

#### 4.2.3 源码精读

README 里五步的小节标题一一对应上面的流程表：

[Point/ColorReversal/README.md#L13-L16](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/README.md#L13-L16) —— 第 1 步 Preparing：打开 `ImageForTest`、放入图片、编辑 `conf.json`。

[Point/ColorReversal/README.md#L18-L21](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/README.md#L18-L21) —— 第 2 步 Software simulation：进入 `SoftwareSim` 跑 `sim.py`，到 `SimResCheck` 看结果。

[Point/ColorReversal/README.md#L23-L25](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/README.md#L23-L25) —— 第 3 步：进入 `HDLSimDataGen` 跑 `creat.py`（README 笔误，实为 `create.py`）。

[Point/ColorReversal/README.md#L36-L50](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/README.md#L36-L50) —— 第 4 步 Functional simulation：首次 `vlib work`，GUI 里 Compile All，再 `do Run.do`（看波形）或 `do RunOver.do`（只看最终结果）。

[Point/ColorReversal/README.md#L52-L55](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/README.md#L52-L55) —— 第 5 步 Comparing：跑 `covert.py`（README 笔误，实为 `convert.py`）把 `.res` 转成图，再跑 `compare.py` 出报告。

#### 4.2.4 代码实践

1. 实践目标：把 README 的五步与目录里的真实文件对上号。
2. 操作步骤：在 `Point/ColorReversal/` 下打开 `README.md`，把每一步提到的脚本名/目录名用荧光笔标出，再到磁盘上确认这些文件确实存在。
3. 需要观察的现象：README 提到的 `SoftwareSim`、`HDLSimDataGen`、`FunSimForHDL`、`SimResCheck` 四个目录是否都在；`creat.py`/`covert.py` 在磁盘上是否其实叫 `create.py`/`convert.py`。
4. 预期结果：四个目录齐全；磁盘上真实脚本是 `sim.py`、`create.py`、`convert.py`、`compare.py`，外加 `Run.do`/`RunOver.do`。
5. 这一步无需运行任何命令，是纯阅读型实践。

#### 4.2.5 小练习与答案

- **练习 1**：第 2 步（软件仿真）和第 4 步（功能仿真）各自的「输入图」分别是谁？
- **参考答案**：第 2 步 `sim.py` 直接读 `ImageForTest/` 里的原始图；第 4 步 ModelSim 不读图，而是读第 3 步生成的 `.dat` 激励文件。
- **练习 2**：为什么 `-soft.bmp` 和 `-hdlfun.bmp` 都要放进 `SimResCheck/`？
- **参考答案**：因为 `compare.py` 要把这两张图逐像素比对算 PSNR，必须放在同一目录下才能按文件名配对。

---

### 4.3 conf.json 配置约定

#### 4.3.1 概念说明

`conf.json` 是仿真的「参数表」。它的核心设计是：**用一个数组列出若干组配置，让一次仿真可以同时跑多个参数组合**。对于没有参数的 IP（如 ColorReversal，只是把颜色取反），它退化成一个占位符；对于有参数的 IP（如 Threshold 的阈值），它的每个元素就是一组完整的参数。

#### 4.3.2 核心流程

所有脚本读取 `conf.json` 的方式完全一致——取出 `"conf"` 这个数组，然后 `for c in Conf` 遍历每一个元素。每个元素 `c` 会作为参数传给处理函数，并决定一个输出文件名。流程是：

```
读 conf.json → 拿到 Conf 数组 → 对数组里每个 c：
    用 c 处理图片 → 用 c 拼出一个唯一的输出文件名 → 落盘
```

#### 4.3.3 源码精读

ColorReversal 的 `conf.json` 极简，只有一个字符串元素 `"default"`：

[Point/ColorReversal/ImageForTest/conf.json#L1-L5](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/ImageForTest/conf.json#L1-L5) —— `"conf" : ["default"]`。因为这个 IP 不需要任何参数，`"default"` 只是一个占位标签，处理时根本不会被用到。

脚本侧如何消费它：`sim.py` 在文件开头一次性加载，后面循环遍历。

[Point/ColorReversal/SoftwareSim/sim.py#L51-L58](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/SoftwareSim/sim.py#L51-L58) —— `Conf = json.load(open('../ImageForTest/conf.json', 'r'))['conf']` 取出数组；同时声明只接受 `.jpg`/`.bmp`。

而 ColorReversal 的 `name_format` 与 `transform` 都忽略 `conf`，所以输出文件名里不带任何参数：

[Point/ColorReversal/SoftwareSim/sim.py#L65-L73](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/SoftwareSim/sim.py#L65-L73) —— `name_format` 返回固定 `<name>-soft.bmp`；`transform` 直接 `im.point(lambda p : 255 - p)` 做取反，参数 `conf` 未被使用。

为了看清这套约定如何扩展到「带参数」的 IP，对照看一眼 `Threshold`（灰度转二值）的配置就一目了然：

[Point/Threshold/ImageForTest/conf.json#L1-L14](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/ImageForTest/conf.json#L1-L14) —— `conf` 数组里有两个对象，分别是 `Base` 模式（阈值 128）和 `Contour` 模式（阈值 50~200），一次仿真就会跑两组参数。

[Point/Threshold/SoftwareSim/sim.py#L65-L80](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/SoftwareSim/sim.py#L65-L80) —— Threshold 把 `conf['mode']`、`conf['th1']`、`conf['th2']` 同时写进算法逻辑和输出文件名（`<name>-<mode>-<th1>-<th2>-soft.bmp`），这样两组参数各产出一张图、互不覆盖。

> 结论：`conf.json` 数组的每个元素 = 一组参数 = 一个独立输出文件。这就是它能从 `["default"]` 扩展到多参数的统一约定。

#### 4.3.4 代码实践

1. 实践目标：体会 `conf.json` 数组长度与输出文件数量的一一对应。
2. 操作步骤：阅读 ColorReversal 和 Threshold 两个 `conf.json`，数一下各自数组里有几个元素，再去看 `SoftwareSim` 里 `for c in Conf` 这层循环。
3. 需要观察的现象：ColorReversal 的 `transform` 是否用到了 `c`；Threshold 的 `transform` 是否用到了 `c['mode']` 等。
4. 预期结果：ColorReversal 有 1 个元素、产出 1 张图；Threshold 有 2 个元素、产出 2 张图。
5. 待本地验证：若你想亲眼看到多张图，可在 Threshold 的 `conf.json` 里临时加第三组参数（不改源码，只改配置），观察是否会多出一张输出图。

#### 4.3.5 小练习与答案

- **练习**：如果把 ColorReversal 的 `conf.json` 改成 `"conf" : ["default", "default"]`，软件仿真会产出几张图？两张图内容会不同吗？
- **参考答案**：会产出两次 `<name>-soft.bmp`，但由于文件名不含参数、第二次会覆盖第一次，最终磁盘上仍只有一张图，且内容完全相同（因为取反逻辑不依赖参数）。这反过来印证了「文件名必须含参数」才能区分多组配置——这也是 Threshold 把参数写进文件名的原因。

---

### 4.4 四个仿真脚本入口与数据流

#### 4.4.1 概念说明

五步流程的「肌肉」是四个 Python 脚本加两个 `.do` 文件。这一节把每个脚本的**输入文件、输出文件、落盘路径**讲清楚，把第 4.2 节那张数据流图落实成代码。这里还藏着一个容易看错的细节：`.dat` 里每个颜色通道写成 **8 位二进制**（不是 10 位），下面会专门解释。

#### 4.4.2 核心流程

四个脚本的职责与数据流向：

```
sim.py     : ImageForTest/*.bmp  ──► SimResCheck/<name>-soft.bmp       （软件黄金结果）
create.py  : ImageForTest/*.bmp  ──► FunSimForHDL/<name>.dat + imgindex.dat （硬件激励，二进制）
Run.do     : <name>.dat          ──► <name>.res                         （ModelSim 跑 RTL，十进制输出）
convert.py : FunSimForHDL/*.res  ──► SimResCheck/<name>-hdlfun.bmp      （硬件结果还原成图）
compare.py : SimResCheck/*.bmp   ──► compare_report.txt / ...table.txt  （配对算 PSNR）
```

PSNR 的计算公式（compare.py 里 `get_psnr` 的实现）为：

\[ \text{PSNR} = 20 \cdot \log_{10}\!\left(\frac{\text{MAX}}{\text{RMS}}\right),\quad \text{MAX}=255 \]

其中 RMS 是两张图逐像素差值的均方根。当两张图完全一致时 RMS=0，代码用一个很大的数 \(10^6\) 来代表「无穷大」。

#### 4.4.3 源码精读

**(a) `sim.py` —— 软件黄金模型**

核心运算是 `transform`，把每个像素 `p` 变成 `255 - p`，存成 `-soft.bmp`：

[Point/ColorReversal/SoftwareSim/sim.py#L89-L102](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/SoftwareSim/sim.py#L89-L102) —— 遍历 `ImageForTest` 下所有 `.jpg`/`.bmp`，对每个配置 `c` 调用 `transform` 并保存到 `../SimResCheck/<name>-soft.bmp`。

> 平台提示：`sim.py` 第 53-55 行用 `windll.LoadLibrary('user32.dll')` 弹 Windows 消息框报错，这意味着脚本原始形态面向 **Windows**，在 Linux/macOS 上 `windll` 不存在，需自行改造或跳过弹窗逻辑。`create.py` 同理。

**(b) `create.py` —— 生成硬件激励 `.dat`**

`.dat` 的文件格式由主循环决定：先写宽、高，再写图像模式，最后是逐像素的二进制串：

[Point/ColorReversal/HDLSimDataGen/create.py#L52-L68](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDLSimDataGen/create.py#L52-L68) —— 对每张图写 `%d\n%d\n`（宽、高）、`%s\n`（模式 RGB/L/1），再写像素数据；最后把所有 `.dat` 的文件名汇总写到 `imgindex.dat`，供 testbench 依次读取。

每个颜色通道被编码成二进制串的关键函数：

[Point/ColorReversal/HDLSimDataGen/create.py#L22-L44](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDLSimDataGen/create.py#L22-L44) —— `color_format` 把一个通道值转成二进制。注意第 30 行 `for i in xrange(10 - len(bin(c)))`：`bin(c)` 会带 `'0b'` 前缀（占 2 个字符），所以 `10 - len(bin(c))` 实际等于「补到 **8** 位」所需的零个数。也就是说每个通道写成 **8 位二进制**，RGB 一行就是 24 个 0/1 字符；二值图（模式 `'1'`）则只写单个 `'0'`/`'1'`。

> 这里的 `10` 是个容易看错的「魔数」：它 = 目标 8 位 + `'0b'` 的 2 个字符，并非「10 位精度」。

**(c) `Run.do` / `RunOver.do` —— 启动 ModelSim**

[Point/ColorReversal/FunSimForHDL/Run.do#L1-L1](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/FunSimForHDL/Run.do#L1-L1) —— `vsim -voptargs=+acc -L unisims_ver work.ColorReversal_TB` 加载 testbench（`-L unisims_ver` 正是 4.1 节说的 Xilinx 库依赖）。

[Point/ColorReversal/FunSimForHDL/Run.do#L84-L84](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/FunSimForHDL/Run.do#L84-L84) —— 末尾 `run -all` 启动仿真；前面大量 `add wave` 行用于配置波形窗口。

[Point/ColorReversal/FunSimForHDL/RunOver.do#L1-L2](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/FunSimForHDL/RunOver.do#L1-L2) —— 精简版：只 `vsim` + `run -all`，不打开波形，适合只想要最终 `.res`。

testbench 读取二进制 `.dat` 喂给被测模块，模块输出则写成**十进制** `.res`。

**(d) `convert.py` —— 把 `.res` 还原成图**

`.res` 与 `.dat` 结构对称（行 0=宽、行 1=高、行 2=模式、行 3+=像素），但像素是十进制文本：

[Point/ColorReversal/SimResCheck/convert.py#L11-L34](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/SimResCheck/convert.py#L11-L34) —— 读取 `../FunSimForHDL/*.res`，按模式解析每个像素（RGB 用空格分三通道），重建 PIL 图像并存成 `<name>-hdlfun.bmp`。

**(e) `compare.py` —— 配对算 PSNR**

[Point/ColorReversal/SimResCheck/compare.py#L11-L20](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/SimResCheck/compare.py#L11-L20) —— `get_psnr` 用 `ImageChops.difference` 算两张图差值、`ImageStat.Stat` 取 RMS，再代入 PSNR 公式。

[Point/ColorReversal/SimResCheck/compare.py#L50-L72](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/SimResCheck/compare.py#L50-L72) —— 通过文件名后缀（`-soft` 与 `-hdlfun`）把软、硬两张图配成一对，再把每张图的 PSNR 写进 `compare_report.txt` 与 `compare_report_table.txt`。

> 为什么这些中间产物不入库？看 ColorReversal 自己的 `.gitignore`：[Point/ColorReversal/.gitignore#L4-L7](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/.gitignore#L4-L7) 用 `FunSimForHDL/*` 排除整个目录，再用白名单 `!*.mpf / !*.mti / !*.do` 把工程与脚本放回——于是 `.dat`、`.res`、生成的 `.bmp`、报告文件都被挡在版本库之外，只保留可复现的源码与脚本。

#### 4.4.4 代码实践

1. 实践目标：在不跑 ModelSim 的前提下，亲手验证「`.dat` 写出 8 位二进制」这一结论。
2. 操作步骤：进入 `Point/ColorReversal/HDLSimDataGen/`，先在 `../ImageForTest/` 放一张自己的 `.bmp`（见下方提示），再运行 `python create.py`（Windows 环境）。
3. 需要观察的现象：打开生成的 `../FunSimForHDL/<name>.dat`，看前三行（宽、高、模式）和随后的像素行；任取一个 RGB 像素行数一下字符数。
4. 预期结果：前三行依次是十进制宽、十进制高、`RGB`；每个 RGB 像素行恰好是 24 个 `0`/`1` 字符（即每通道 8 位）。
5. 待本地验证：本仓库 `ImageForTest/` 下**没有提交任何测试图片**（只有 `conf.json`），你必须自己放图；非 Windows 环境需先处理 `windll` 依赖。若无法运行，可改为纯阅读 `create.py` 第 22-44 行推导出「8 位」结论。

#### 4.4.5 小练习与答案

- **练习 1**：`.dat` 和 `.res` 都是纯文本，它们的像素表示有什么不同？
- **参考答案**：`.dat` 是硬件的**输入**激励，像素写成二进制串（每通道 8 位，便于 testbench 按位拼成向量）；`.res` 是硬件的**输出**结果，像素写成十进制（便于 `convert.py` 直接 `int()` 解析还原成图）。
- **练习 2**：如果某次仿真 PSNR 报告里出现 `1000000.000000`，说明什么？
- **参考答案**：说明该图软件结果与硬件结果完全一致，RMS=0，代码用 \(10^6\) 这个大数代表「无穷大」PSNR——这正是软硬一致性通过的信号。

---

## 5. 综合实践

把本讲内容串起来，完成一次「半自动」的软硬一致性核对（跳过需要 ModelSim 的第 4 步，重点跑 Python 侧闭环）：

1. **准备图像**：在 `Point/ColorReversal/ImageForTest/` 放入一至两张你自己的 `.bmp` 图片（仓库未提供），例如 `cat.bmp`。
2. **配置参数**：确认 `conf.json` 为 `["default"]`。
3. **软件仿真**：进入 `SoftwareSim/` 运行 `sim.py`，记录生成的 `../SimResCheck/cat-soft.bmp`。
4. **生成激励**：进入 `HDLSimDataGen/` 运行 `create.py`，记录生成的 `../FunSimForHDL/cat.dat` 和 `../FunSimForHDL/imgindex.dat`。
5. **核对清单**：用一张表把「脚本 → 输入路径 → 输出路径 → 输出文件名」填满，验证它和第 4.2 节的数据流图完全一致。
6. **(可选，需 ModelSim)** 执行 `do RunOver.do` 得到 `cat.res`，再进入 `SimResCheck/` 跑 `convert.py` 与 `compare.py`，查看 `compare_report.txt` 里的 PSNR 是否接近 \(10^6\)（即软硬一致）。

> 平台与版本提醒：上述 Python 脚本依赖 Python 2.7 + PIL，且原始代码用 `windll` 调 Windows 消息框；在没有这些条件的环境里，请把这一实践当作「源码阅读 + 数据流核对」来完成，相关运行结果标注「待本地验证」。

完成本实践后，你应该能闭着眼睛说出：同一张图，被 `sim.py` 算成软件结果、被 `create.py` 切成硬件激励、被 ModelSim 跑出硬件结果、最后被 `compare.py` 用 PSNR 判分——这就是 F-I-L 的软硬一致性闭环。

## 6. 本讲小结

- F-I-L 仿真由 **Python 2.7+PIL**（软件侧）、**ModelSim 10.1+**（RTL 功能仿真）、**Vivado**（IP 打包与器件库）三种工具接力完成，ModelSim 必须先编译进 Vivado 的器件库。
- README 把一次仿真分成五步：配置图像 → 软件仿真 → 生成 dat → 功能仿真 → 转换比对，每一步对应一个固定目录和脚本。
- `conf.json` 的 `"conf"` 是一个数组，每个元素代表一组参数，决定一个独立输出文件；无参数 IP 用 `["default"]` 占位，带参数 IP 把参数写进算法和文件名。
- 四个脚本的数据流是：`sim.py` 出 `-soft.bmp`，`create.py` 出 `.dat`，ModelSim 出 `.res`，`convert.py` 出 `-hdlfun.bmp`，`compare.py` 用 PSNR 配对打分。
- `.dat` 里每个颜色通道写成 **8 位二进制**（`create.py` 里的 `10` 是 `8 + '0b'` 的魔数，不是 10 位精度），`.res` 里则是十进制。
- `.dat`/`.res`/生成的 `.bmp` 都是运行时产物，被每个 IP 自己的 `.gitignore` 挡在版本库外；测试图片也需用户自行放入 `ImageForTest/`。

## 7. 下一步学习建议

- 下一讲 [u1-l4 标准化 IP 接口与两种工作模式](u1-l4-standard-interface-and-modes.md) 将打开 `ColorReversal.v` 本体，讲清 `clk/rst_n/in_enable/in_data/out_ready/out_data` 这套统一端口，以及 `work_mode`（Pipeline=0 / ReqAck=1）两种模式的时序差异——届时你会理解 testbench 里那些波形信号到底在驱动什么。
- 在继续之前，建议你顺手阅读 `Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv`，看看 testbench 是如何读取本讲生成的 `.dat`、又是如何把输出写成 `.res` 的，把第 4.4 节的数据流和真实 RTL 串起来。
- 如果想看带参数 IP 的完整闭环，可以对照 `Point/Threshold/` 重走一遍本讲的五步，体会 `conf.json` 多配置如何放大成多张输出图。
