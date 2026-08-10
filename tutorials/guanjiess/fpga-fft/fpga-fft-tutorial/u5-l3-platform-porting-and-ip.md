# Xilinx / Anlogic 双平台移植与 IP 依赖

## 1. 本讲目标

本讲是「验证、仿真与平台移植」单元的第三篇。前面几讲我们一直在读 Verilog 源码里的算法逻辑（蝶形、延时、旋转因子、复数乘法），默认这些逻辑在任何 FPGA 上都能跑。但真实情况是：**这套 FFT 里有一批「厂商 IP 核」并不随源码提供，必须用对应厂商的工具（如 Xilinx Vivado、安路 Anlogic Tang/Diamond）重新生成**。换一块芯片、换一家厂商，这些 IP 就要全部重做，而且接口、延迟都可能不一样。

读完本讲，你应当能够：

1. 识别本项目里**全部五类厂商 IP 依赖**：`mult2`（乘法器）、`Delay`（双口 RAM）、`rotator_*_real/img`（旋转因子 ROM）、`blk_mem_gen_0`（块存储）、`addr_ctrl`（地址控制器）。
2. 看懂 `delay.v` 里**注释掉的 Anlogic 版 RAM** 与**启用的 Xilinx 版 RAM** 在端口命名上的差异，并能写出两套端口的对照表。
3. 理解为什么同一个常量 `HALT_FOR_NEXT_LAYER` 在 Anlogic 用 `-2`、在 Vivado 用 `-3`，这背后是 **ROM 读取延迟不同**，并知道移植时这是必须重新校准的关键点。
4. 拿着一份 checklist，把整套设计搬到一块新 FPGA（Anlogic、Intel、国产芯片等）上。

## 2. 前置知识

在进入源码前，先把几个移植相关的概念讲清楚。

### 2.1 什么是「IP 核」（Intellectual Property Core）

在 FPGA 设计里，「IP 核」是厂商或第三方预先做好、参数化、可以直接例化的硬件模块。比如「一个 18×18 的流水线乘法器」「一块 8K 深度的双口 RAM」「一块用 `.coe` 文件初始化的只读存储器」——这些底层都要映射到芯片上**专门的硬资源**（DSP 乘法单元、BRAM 块），不同厂商的芯片这些硬资源结构不同，所以 IP 不能跨厂商通用。

例化一个 IP，写法上和例化一个普通 Verilog 模块一模一样：

```verilog
mult2 real_ac(
    .CLK(clk), .A(a), .B(c), .P(ac)
);
```

但 `mult2` 这个名字对应的「实现」是 Vivado 用 IP Catalog 生成的 `.xci` 工程文件，**仓库里并不包含**。你拿到这份源码、换一个工具链打开，综合器会报 `mult2 not found`，你必须自己重新生成一个同名、同端口、同行为的 IP。

### 2.2 什么叫「厂商锁定」（vendor lock-in）

当你的 RTL 里大量直接例化某厂商的 IP（如 Xilinx 的 `mult2`、`blk_mem_gen_0`），这份代码就**绑定**到了这家厂商的工具链。换到 Anlogic、Intel、国产 FPGA 时，这些 IP 名字和端口都不存在，必须逐一替换。本讲的核心工作，就是把这层「绑定关系」梳理清楚，并给出解绑（移植）的方法。

### 2.3 两类硬资源与对应的 IP

本项目用到的 IP 归根到底映射到两类硬资源：

| 硬资源 | 用途 | 本项目对应的 IP |
| --- | --- | --- |
| **DSP 乘法单元** | 做实数乘法（复数乘法拆成 4 个实数乘） | `mult2` |
| **BRAM 块存储** | 做延时用的双口 RAM、存旋转因子的 ROM | `Delay`、`rotator_*_real/img`、`blk_mem_gen_0` |

理解这一点很重要：**移植的本质，是在新芯片上找到对应的 DSP 和 BRAM 资源，把 IP 重新生成一遍，并保证端口、位宽、流水线延迟都对齐。**

> 如果你还没读过前面关于 `delay.v`（u3-l2）和 `multiplier.v`（u2-l2）的讲义，建议先读。本讲会频繁引用这两个文件里 IP 的例化点。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/delay.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v) | SDF 延时单元，用双口 RAM `Delay` 做反馈 | Anlogic / Xilinx **两套 RAM 例化**（一套注释、一套启用） |
| [src/delay_1k_plus.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay_1k_plus.v) | `delay.v` 的变体（计数器位宽更窄） | 同样保留两套 RAM 版本，移植方式一致 |
| [src/multiplier.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/multiplier.v) | 复数乘法器，用 4 个 `mult2` 乘法器 IP | 乘法器 IP 的厂商依赖与流水线延迟 |
| [src/Rotator16.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v) | 16 点层旋转因子，用 ROM IP `rotator_16_real/img` | ROM IP 的 Anlogic / Xilinx 两套端口 |
| [src/fft_32.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v) | 高层模块模板，例化 `rotator_32_real/img` ROM | 高层 ROM 命名规律（`rotator_<N>_*`） |
| [src/rom.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/rom.v) | 一个独立的小模块，例化 `addr_ctrl` 与 `blk_mem_gen_0` | 另两类 IP 的出现位置 |
| [src/butterfly_general.v](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v) | 参数化蝶形层，含 `HALT_FOR_NEXT_LAYER` 常量 | `-2`（anlogic）/ `-3`（vivado）的平台校准 |
| [README.md](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/README.md) | 设计文档 | 明确写出「Xilinx ROM 读延迟 2 拍，移植需验证」 |

## 4. 核心概念与源码讲解

### 4.1 厂商 IP 与可移植性问题

#### 4.1.1 概念说明

我们要回答一个根本问题：**为什么这份 Verilog 不能「拿来就在任何 FPGA 上跑」？**

答案是：源码里混进了大量「直接例化的厂商 IP」。这些 IP 的名字（`mult2`、`Delay`、`rotator_16_real`……）在 Verilog 语法上只是一个模块名，但它的**实现并不在仓库里**，而是由厂商 EDA 工具（Vivado 的 IP Catalog、Anlogic 的 IP Compiler）现场生成。换一个厂商，这些名字对应的模块要么不存在，要么端口完全不同。

这带来两个后果：

1. **可综合性受限**：源码离开原厂商工具链就无法直接综合。
2. **行为可能微变**：即使你重新生成了同名 IP，它的**内部流水线延迟**（比如 ROM 读出要几个时钟）可能和新平台不一样，从而破坏原本精心调好的时序对齐。

#### 4.1.2 核心流程

判断一个项目「可移植性」的通用流程：

```
1. 扫描所有 .v 文件里的模块例化语句
2. 区分「项目自己写的模块」与「厂商 IP」
   - 自己写的模块：源码在 src/ 里能找到 .v 定义 → 可移植
   - 厂商 IP：src/ 里找不到定义 → 必须重新生成
3. 对每个厂商 IP，记录：名字、端口、位宽、流水线延迟
4. 在目标平台上重新生成对应 IP，逐一对齐端口与延迟
5. 重新跑仿真，验证时序（尤其是 ROM/RAM 读延迟）
```

本讲就是按这个流程，把第 2、3 步为 fpga-fft 做完。

#### 4.1.3 源码精读

最直接的证据来自 `multiplier.v` 的文件头注释，作者自己写明用的是 Xilinx IP：

[src/multiplier.v:L1-L5](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/multiplier.v#L1-L5) —— 文件开头直接写 `implement complex multiplier with xilinx multiplier IP`，这是整份代码「为 Xilinx 而写」的明确声明。

`delay.v` 里同样有平台声明：

[src/delay.v:L191-L195](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L191-L195) —— 注释 `Delay is a ram` / `2 versions, one for anlogic and one for xilinx` / `the ram ip name is set the same, both are called Delay`。关键一句是「**两个版本的 IP 名字都叫 `Delay`**」——这是作者刻意做的解耦：把 IP 的模块名固定下来，切换平台时只换 IP 内部实现、不换外层例化代码（可惜端口名没统一，详见 4.3）。

#### 4.1.4 代码实践

**实践目标**：亲手确认「哪些模块是项目自己的，哪些是厂商 IP」。

**操作步骤**：

1. 在 `src/` 目录下，对每个被例化的模块名，用搜索工具找它的 `module` 定义。
2. 能在 `src/` 内找到 `module xxx` 定义的 → 自己写的；找不到的 → 厂商 IP。
3. 例如 `butterfly`、`delay`、`multiplier`、`Rotator16`、`Rotator_address` 都能在 `src/` 找到定义；而 `mult2`、`Delay`、`rotator_16_real`、`blk_mem_gen_0`、`addr_ctrl` 在 `src/` 里**没有任何 `module` 定义**。

**需要观察的现象**：`mult2` 等名字在 `src/` 中只作为「例化名」出现，从不作为 `module ... ;` 出现。

**预期结果**：你会得到一份「厂商 IP 清单」，与 4.2 节的表格一致。如果某个名字你既找不到定义又无法确认是否为 IP，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么作者把两个平台的 RAM IP 都命名为 `Delay`，而不是一个叫 `Delay_xilinx`、一个叫 `Delay_anlogic`？

**参考答案**：为了让外层例化代码（`delay.v` 里的 `Delay delay_real (...)`）在切换平台时尽量少改。理想情况下只要换 IP 内部实现即可。但由于 Anlogic 和 Xilinx 的端口名不同（见 4.3），实际上端口映射那段还是要改，所以这个解耦并不彻底。

**练习 2**：如果要把项目移植到 Intel FPGA，`mult2` 这个名字还能直接用吗？

**参考答案**：不能。`mult2` 是 Xilinx 的乘法器 IP 名，Intel 工具里没有这个名字。需要在 Intel Quartus 里用其乘法器 IP（或 `lpm_mult`）重新生成一个等效模块，并保证端口（时钟、A、B、P）和流水线延迟与原 `mult2` 一致，再替换例化代码。

---

### 4.2 IP 依赖全景图：五类 IP 清单

#### 4.2.1 概念说明

在动手替换之前，必须先有一份**完整的 IP 清单**：每个 IP 叫什么、出现在哪个文件、用在哪里、对应哪类硬资源。这份清单是移植 checklist 的骨架。漏掉任何一个 IP，综合时才会报错，调试成本很高。

#### 4.2.2 核心流程

构建清单的方法是「从例化点反查」：在 `src/` 全局搜索每一类 IP 名，记录它出现的文件和行号，并标注它服务于流水线的哪个环节。

本项目流水线的每一级（fft_16 及以上）都由「蝶形 → RAM 延时 → 旋转因子 ROM → 复数乘法」四件套组成，因此 IP 也按这四个环节归位。

#### 4.2.3 源码精读

下表是全项目五类厂商 IP 的完整清单（按硬资源归类）：

| IP 名 | 类别 | 出现位置（文件:行） | 作用 | 例化数量 |
| --- | --- | --- | --- | --- |
| `mult2` | DSP 乘法器 | `src/multiplier.v:69,76,83,90` | 实数乘法，4 个组合成复数乘法 | 每个 `multiplier` 例化 4 个；每级 fft_*（fft_4 及以上）1 个 multiplier |
| `Delay` | 双口 RAM | `src/delay.v:222,233`；`src/delay_1k_plus.v:227,238` | SDF 反馈延时，实部/虚部各一块 | 每个 `delay` 例化 2 个 |
| `rotator_<N>_real` / `rotator_<N>_img` | 旋转因子 ROM | `src/Rotator16.v:60,66`；`src/fft_32.v:63,69`；`src/fft_64.v`、`fft_128.v`、`fft_256.v`、`fft_512.v`… | 存第 N 点层的旋转因子，实部/虚部分两块 ROM | fft_16 及以上每级 2 块 |
| `blk_mem_gen_0` | 块存储 ROM | `src/rom.v:38` | 一个独立小模块里用的 Xilinx 块存储 | 1（在 rom.v 内） |
| `addr_ctrl` | 地址控制器 | `src/rom.v:31` | 给 `blk_mem_gen_0` 产生读地址 | 1（在 rom.v 内） |

对应的例化点源码：

- 四个 `mult2`：[src/multiplier.v:L69-L95](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/multiplier.v#L69-L95) —— 分别算 `bd`、`ac`、`bc`、`ad` 四个实数乘积。
- `Delay` 双口 RAM：[src/delay.v:L222-L242](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L222-L242) —— Xilinx 版（启用），实部 `delay_real`、虚部 `delay_img`。
- 旋转因子 ROM（以 16 点层为例）：[src/Rotator16.v:L60-L70](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v#L60-L70) —— `rotator_16_real` 与 `rotator_16_img`。
- 高层 ROM 命名规律（32 点层）：[src/fft_32.v:L63-L73](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L63-L73) —— `rotator_32_real`、`rotator_32_img`，命名形如 `rotator_<点数>_(real|img)`。
- 块存储与地址控制器：[src/rom.v:L31-L42](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/rom.v#L31-L42) —— `addr_ctrl` 生成地址喂给 `blk_mem_gen_0`。

> **关于 `rom.v` 的说明**：`rom.v` 里的 `addr_ctrl` 与 `blk_mem_gen_0` 并没有被主流水线（`fft_top` → 各级 `fft_*`）例化，它更像一个独立的实验/测试模块。但它确实展示了项目里另两类 Xilinx IP 的用法，移植时若用到该模块也要一并替换。

#### 4.2.4 代码实践

**实践目标**：自己用搜索工具复现上表的「IP 出现位置」。

**操作步骤**：

1. 用 Grep 在 `src/` 下搜索 `mult2 `（注意末尾空格，匹配例化），统计它在 `multiplier.v` 出现 4 次。
2. 搜索 `Delay `，确认它在 `delay.v` 与 `delay_1k_plus.v` 各出现 2 次（Xilinx 版）。
3. 搜索正则 `rotator_\d+_(real|img)`，列出所有旋转因子 ROM 的名字：`rotator_16_*`、`rotator_32_*`、`rotator_64_*`、`rotator_128_*`、`rotator_256_*`、`rotator_512_*` …… 对应 fft_16 到 fft_16k 各级。

**需要观察的现象**：旋转因子 ROM 的名字严格按 `rotator_<点数>_<real|img>` 规律，每级两块（实部 + 虚部）。

**预期结果**：你应能得到一份与上表一致的清单，并能数出全项目（fft_16 ~ fft_16k 共 11 个高层 + fft_8 的 Mem + 各级 mult2/Delay）需要重新生成的 IP 总数大致量级（数十个 ROM + 数十个 mult2 + 若干 Delay）。

#### 4.2.5 小练习与答案

**练习 1**：fft_16k 这一级需要重新生成几个 ROM IP？分别叫什么？

**参考答案**：2 个，分别叫 `rotator_16k_real` 与 `rotator_16k_img`（实部、虚部各一块），命名遵循 `rotator_<点数>_(real|img)` 规律。

**练习 2**：为什么旋转因子 ROM 要实部、虚部分成两块，而不是合在一块？

**参考答案**：复数旋转因子 \(W_N^k=e^{-j2\pi k/N}=\cos(\cdot)-j\sin(\cdot)\) 有实部、虚部两个 18 位的分量；分开存成两块独立的 ROM，读取时各自给出实部、虚部，直接送进 `multiplier` 的 `c`（实部）、`d`（虚部）端口，省去拼接/拆分的额外逻辑。

---

### 4.3 delay.v 的双口 RAM：Anlogic 与 Xilinx 两套端口

#### 4.3.1 概念说明

`Delay` 是 SDF 延时反馈的「心脏」（回顾 u3-l2）：它是一块双口 RAM，写入蝶形下支 B 的结果，延时半周期后读出当上支 C 喂回蝶形。这是本项目**最关键、也最平台敏感**的 IP：双口 RAM 是每家 FPGA 厂商都有的硬资源（BRAM），但端口命名、读写使能的语义各不相同。

作者在 `delay.v` 里**同时保留了两套例化代码**：一套给 Anlogic（用块注释 `/* */` 关掉），一套给 Xilinx（启用）。这是理解「双平台移植」最直接的教材。

#### 4.3.2 核心流程

`Delay` 这个 RAM 在两个平台上的**功能完全一样**（双口、先写后读、实虚分存），区别只在端口名：

```
写端口：写时钟 + 写数据 + 写地址 + 写使能
读端口：读时钟 + 读地址 + 读使能 + 读数据
```

两套 IP 都把这些信号凑齐，只是名字不同。移植时把「Anlogic 端口名 → Xilinx 端口名」做一个一一映射即可。

#### 4.3.3 源码精读

**Anlogic 版（注释掉）**，[src/delay.v:L196-L218](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L196-L218)：

```verilog
/* Delay delay_real(
        .dia    (   din_real    ),   // 写数据
        .addra  (   r_addra     ),   // 写地址
        .clk    (   clk         ),   // 时钟（读写共用）
        .cea    (   1           ),   // 写口时钟使能（恒 1）
        .ceb    (   1           ),   // 读口时钟使能（恒 1）
        .dob    (   w_dout_real ),   // 读数据
        .addrb  (   r_addrb     ),   // 读地址
        .clk    (   clk         )
); */
```

**Xilinx 版（启用）**，[src/delay.v:L222-L231](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L222-L231)：

```verilog
Delay delay_real (
    .clka   (   clk         ),   // 写时钟
    .wea    (   1           ),   // 写使能（恒 1）
    .addra  (   r_addra     ),   // 写地址
    .dina   (   din_real    ),   // 写数据
    .clkb   (   clk         ),   // 读时钟
    .enb    (   1           ),   // 读使能（恒 1）
    .addrb  (   r_addrb     ),   // 读地址
    .doutb  (   w_dout_real )    // 读数据
);
```

两套端口的对照表（移植用）：

| 功能 | Anlogic 端口 | Xilinx 端口 | 本项目连接的信号 |
| --- | --- | --- | --- |
| 写时钟 | `clk` | `clka` | `clk` |
| 写数据 | `dia` | `dina` | `din_real` / `din_img` |
| 写地址 | `addra` | `addra` | `r_addra` |
| 写使能 | `cea`（clock enable） | `wea`（write enable） | 恒 `1` |
| 读时钟 | `clk` | `clkb` | `clk` |
| 读地址 | `addrb` | `addrb` | `r_addrb` |
| 读使能 | `ceb` | `enb` | 恒 `1` |
| 读数据 | `dob` | `doutb` | `w_dout_real` / `w_dout_img` |

注意一个语义差异：Anlogic 用 **`cea`（时钟使能）** 控制写，Xilinx 用 **`wea`（写使能）** 控制写。在本项目里两者都恒接 `1`（常写），真正控制「写不写」的是**写地址 `r_addra` 是否在推进**——这个逻辑由 `delay.v` 的状态机负责（回顾 u3-l2 的 `wea` 与 `r_addra`），与 IP 本身无关，因此两套 IP 在功能上等价。

> **移植陷阱（务必注意）**：注释掉的 Anlogic 版 `delay_img` 里写地址写成了 `.addra(r_addrb)`（[src/delay.v:L210](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L210)），而 `delay_real` 是 `.addra(r_addra)`（第 198 行）。`delay_1k_plus.v` 的注释版有同样的笔误（[src/delay_1k_plus.v:L215](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay_1k_plus.v#L215)）。这看起来是复制粘贴遗留的 bug：实部块和虚部块本应共用同一个写地址 `r_addra`。**切回 Anlogic 版时务必把 `delay_img` 的写地址改回 `r_addra`**，否则实部、虚部会写到不同地址，数据错乱。

#### 4.3.4 代码实践

**实践目标**：把 `delay.v` 从「Xilinx 版」切到「Anlogic 版」，验证你理解了端口映射。

**操作步骤**：

1. 复制一份 `delay.v` 到本地实验目录（**不要改源码**）。
2. 用注释把当前启用的 Xilinx 版 `delay_real`/`delay_img`（L222-L242）关掉。
3. 取消注释 Anlogic 版（L196-L218）。
4. **修正上面提到的笔误**：把 Anlogic 版 `delay_img` 的 `.addra(r_addrb)` 改成 `.addra(r_addra)`。
5. 在 Anlogic 工具里生成一个名为 `Delay` 的双口 RAM IP：位宽 32、深度按 layer 配置、读写各一端口、`cea`/`ceb` 时钟使能风格。
6. 综合，确认 `Delay` 端口对齐无报错。

**需要观察的现象**：综合报告里 `Delay` 被映射到 BRAM 资源；端口连接无 mismatch 警告。

**预期结果**：综合通过。但功能是否正确（数据是否按半周期延时回流）**待本地验证**——必须跑仿真看 `out_first`/`out_last` 与蝶形配对是否对齐。

#### 4.3.5 小练习与答案

**练习 1**：Anlogic 版用 `cea`/`ceb`，Xilinx 版用 `wea`/`enb`，二者在本项目里都恒接 1，为什么？

**参考答案**：本项目靠「地址是否推进」来控制实际写入/读出（见 u3-l2 的状态机与 `wea` 边沿检测），而不是靠 IP 的使能信号。所以 IP 的使能恒为 1，把控制权完全交给外层状态机，两套 IP 才能功能等价。

**练习 2**：如果不修那个 `.addra(r_addrb)` 的笔误直接切到 Anlogic 版，仿真会表现出什么现象？

**参考答案**：虚部 RAM（`delay_img`）的写地址用的是 `r_addrb`（读地址），与实部 RAM（用 `r_addra` 写地址）不同步，导致虚部数据被写到错误的地址、读出时与实部错位，最终复数延时结果实部/虚部不匹配，FFT 输出全错。这种 bug 不会让综合失败，只在仿真波形里暴露。

---

### 4.4 multiplier.v 与旋转因子 ROM 的 IP 依赖

#### 4.4.1 概念说明

这一节把剩下三类 IP 一次说清：`multiplier.v` 里的乘法器 `mult2`、各级的旋转因子 ROM `rotator_*_real/img`，以及 `rom.v` 里的 `blk_mem_gen_0` 和 `addr_ctrl`。它们各自依赖厂商的 DSP 或 BRAM 资源，移植时都要重新生成。其中**乘法器和 ROM 的流水线延迟**特别关键，会直接影响下一节的时序对齐。

#### 4.4.2 核心流程

```
mult2（乘法器 IP）
  ├─ 端口：CLK, A, B, P（流水线乘法器，P 落后 A/B 若干拍）
  ├─ 移植：用目标平台的 DSP 乘法器 IP 替换，保证位宽(18×32→50)与延迟一致
  └─ 出现：multiplier.v 里 4 个（ac/bd/bc/ad）

rotator_*_real/img（旋转因子 ROM IP）
  ├─ 端口(Xilinx)：clka, addra, douta   ← 当前启用
  ├─ 端口(Anlogic)：clka, addra, doa, ocea, rsta   ← 注释掉（见 Rotator16.v）
  ├─ 移植：重新生成 ROM，用 .coe 把同一批旋转因子烧进去；验证读延迟
  └─ 出现：Rotator16.v、fft_32.v…fft_16k 各级，命名 rotator_<点数>_(real|img)

blk_mem_gen_0 / addr_ctrl（rom.v 内的存储实验）
  ├─ blk_mem_gen_0：Xilinx 块存储 IP，端口 clka, addra, douta
  ├─ addr_ctrl：产生读地址（IP 或自定义模块，仓库无定义，待确认）
  └─ 移植：若用到 rom.v，需在目标平台重新生成等效块存储
```

#### 4.4.3 源码精读

**(1) 乘法器 `mult2`**，[src/multiplier.v:L69-L95](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/multiplier.v#L69-L95)：

```verilog
mult2 real_ac(
    .CLK(clk), .A(a), .B(c), .P(ac)   // P = a*c，落后若干拍
);
// 另有 real_bd(b,d→bd)、real_bc(b,c→bc)、real_ad(a,d→ad)
```

四个 `mult2` 分别算 \(ac\)、\(bd\)、\(bc\)、\(ad\)，供后续 `ac-bd`（实部）、`ad+bc`（虚部）使用。`mult2` 的端口只有 `CLK/A/B/P`，没有显式流水线寄存器配置——它的**内部延迟**由 IP 生成时决定（ Vivado 里可设 pipeline stages）。`multiplier.v` 的 `always` 块在 `mult2` 输出后**下一拍**才做 `ac-bd`，这隐含假设了 `mult2` 的输出 `P` 与输入 `A/B` 之间的固定延迟。**移植时若新平台的乘法器延迟不同，这个 `always` 块的对齐就会被破坏。**

**(2) 旋转因子 ROM 的两套端口**，以 16 点层为例，[src/Rotator16.v:L43-L70](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v#L43-L70)：

```verilog
/* // Anlogic 版（注释掉）
rotator_16_real rotator_16_real(
    .doa(w_rotator_real_tmp), .addra(r_addra),
    .ocea(1), .clka(clk), .rsta(0)
); */

// Xilinx 版（启用）
rotator_16_real rotator_16_real (
    .clka(clk), .addra(r_addra), .douta(w_rotator_real_tmp)
);
```

ROM 端口对照：

| 功能 | Anlogic | Xilinx |
| --- | --- | --- |
| 时钟 | `clka` | `clka` |
| 读地址 | `addra` | `addra` |
| 数据输出 | `doa` | `douta` |
| 输出使能 | `ocea` | （无，常开） |
| 复位 | `rsta` | （无） |

> 同样有复制粘贴遗留：注释掉的 Anlogic 版 `rotator_16_img` 用了 `.addra(r_addrb)`（[src/Rotator16.v:L53](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/Rotator16.v#L53)），而该模块里只有 `r_addra`、根本不存在 `r_addrb`。切回 Anlogic 版时这里**直接编译不过**，必须改成 `r_addra`。这是又一条「注释代码不能无脑启用」的教训。

**(3) 高层 ROM 的命名规律**，[src/fft_32.v:L63-L73](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_32.v#L63-L73)：32 点层用 `rotator_32_real`/`rotator_32_img`。fft_64 用 `rotator_64_*`，依此类推到 fft_16k 的 `rotator_16k_*`。每一级都是「地址由 `Rotator_address` 生成 → ROM 读出 → select 多路选择 → multiplier」的固定链路。

**(4) `blk_mem_gen_0` 与 `addr_ctrl`**，[src/rom.v:L31-L42](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/rom.v#L31-L42)：

```verilog
addr_ctrl addr_ctrl_inst( .clk(clk), .rst_n(rst_n), .addr(addr) );
blk_mem_gen_0 blk_mem_gen_0_inst( .clka(clk), .addra(addr), .douta(q) );
```

`blk_mem_gen_0` 是 Xilinx 的 Block Memory Generator（块存储 IP），`addr_ctrl` 给它产生 10 位读地址。这两个名字在 `src/` 里都找不到 `module` 定义，属于 IP；`addr_ctrl` 究竟是 IP 还是作者未提交的自定义模块，**待本地验证**（仓库未提供其源码）。

#### 4.4.4 代码实践

**实践目标**：为一个 ROM IP（如 `rotator_32_real`）写出移植到新平台的规格说明。

**操作步骤**：

1. 从 `fft_32.v` 读出 ROM 的位宽与地址位宽：数据 18 位（`[17:0]`），地址由 `Rotator_address` 的 `rotator_addr[12:0]` 给出（实际用低 5 位，因为 32 点层只需 N/2=16 个因子）。
2. 写下新平台 ROM IP 的规格：单口、读时钟 1 个、数据 18 位、深度 ≥ 16、用 `.coe` 初始化为这 16 个旋转因子的实部（Q1.16 量化值）。
3. 同理生成虚部 ROM `rotator_32_img`。
4. 在 IP 生成工具里**记录读延迟是几个时钟**（这一步的结论会直接用到 4.5 节）。

**需要观察的现象**：新平台 ROM 的读延迟可能不是 1 拍（Xilinx 默认 2 拍，见 README 说明）。

**预期结果**：得到一份 ROM IP 规格表（位宽、深度、初始化文件、读延迟）。读延迟的具体数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`multiplier.v` 里 `mult2` 的延迟如果从「1 拍」变成「2 拍」，会出什么问题？

**参考答案**：`ac`/`bd`/`bc`/`ad` 四个乘积会各自晚 1 拍到达，而下游 `always` 块（`r_data_real <= ac - bd`）是按原来的延迟对齐的。延迟一变，加减运算就会用「还没就绪」或「错位」的乘积，复数乘法结果错误。因此替换乘法器 IP 后，必须核对并相应调整 `multiplier.v` 里的寄存器级数，或在新 IP 里把流水线级数配成与原来一致。

**练习 2**：`Rotator16.v` 注释掉的 Anlogic 版 ROM 多了 `ocea`、`rsta` 两个端口，而 Xilinx 版没有。这说明什么？

**参考答案**：说明两家 IP 对「输出使能」和「复位」的暴露程度不同。Anlogic 的 ROM IP 显式提供输出时钟使能 `ocea` 和复位 `rsta`，Xilinx 的 `rotator_*` 配置成无输出寄存器、无复位的最简单形式（直接组合输出读出值）。移植时要在新平台的 IP 配置界面里选择对应的可选项，使行为与原设计一致。

---

### 4.5 HALT_FOR_NEXT_LAYER 的 -2/-3：ROM 读延迟差异如何渗透到每一级

#### 4.5.1 概念说明

前面两节讲的是「静态」的端口替换。本节讲一个**动态**的、跨级的移植陷阱：同一个常量 `HALT_FOR_NEXT_LAYER`，在 Anlogic 用 `-2`、在 Vivado 用 `-3`，相差 1 拍。这 1 拍不是随便写的，它**精确对应两家 ROM IP 的读延迟差**。如果你换了平台却没改这个数，整条流水线的级间握手就会错位，FFT 输出全错。

这是本讲最重要、也最隐蔽的一点：**IP 的读延迟差异，不只影响 IP 本身，还会通过时序对齐常量渗透到每一个流水级的 RTL 里。**

#### 4.5.2 核心流程

回顾（u3-l3）：`start_next`（下一级启动脉冲）由计数器达到 `HALT_FOR_NEXT_LAYER` 减某个数触发。这个「减几」要补偿两段延迟：

```
HALT_FOR_NEXT_LAYER = 6 + PERIOD/2
                      ├─ 6：固定的流水线开销（蝶形打拍、状态机等）
                      └─ PERIOD/2：SDF 半周期建立期

触发点 = HALT_FOR_NEXT_LAYER − offset
         └─ offset 补偿：ROM 读延迟 + 蝶形输出对齐
            ├─ Anlogic：offset = 2
            └─ Vivado ：offset = 3   ← 多 1 拍，因 Xilinx ROM 读延迟多 1 拍
```

`offset` 多 1 拍，意味着触发点更靠后（计数器要数到更大的值），`start_next` 晚 1 拍发出——正好抵消 Xilinx ROM 比 Anlogic 多出来的那 1 拍读延迟，使下一级的启动时机与新平台上「旋转因子真正就绪」的时刻对齐。

#### 4.5.3 源码精读

**作者权威说明**（最重要的一段），[README.md:L170-L172](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/README.md#L170-L172)：

> 1. xilinx 的 rom IP 读数据时有 2 个 clk 的延时，在移植到安路或者其他平台时，需要先对 rom 读取数据的时间做验证。
> 2. 需要在 fft 模块对 rotator_valid 输入的时序做调整，使旋转因子的输出和蝶形运算模块的输出对齐。

这段话直接点明：**Xilinx ROM 读延迟是 2 拍**；移植到安路等平台时，ROM 读延迟可能不同，必须重新验证，并相应调整 `rotator_valid`/`HALT` 的时序。

**参数化层的双版本注释**，[src/butterfly_general.v:L120-L123](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L120-L123)：

```verilog
//if(next_level_start_counter == HALT_FOR_NEXT_LAYER-2) begin
//HALT_FOR_NEXT_LAYER-2 is used for anlogic version
//HALT_FOR_NEXT_LAYER-3 is used for vivado version
if(next_level_start_counter == HALT_FOR_NEXT_LAYER-3) begin   // 当前启用 vivado
```

`HALT_FOR_NEXT_LAYER` 本身定义在 [src/butterfly_general.v:L25](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/butterfly_general.v#L25)（`= 6 + (PERIOD)/2`）。

**手写低层也复制了同样的注释**（移植时要改的地方不止一处）：

- `fft_4`：[src/fft_4.v:L112-L114](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_4.v#L112-L114)
- `fft_8`：[src/fft_8.v:L119-L121](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_8.v#L119-L121)
- `fft_16`：[src/fft_16.v:L118-L121](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/fft_16.v#L118-L121)

也就是说，`-2`/`-3` 的选择**分散在 `butterfly_general.v` 以及 `fft_4`/`fft_8`/`fft_16` 多个文件里**，每一处都要按平台切换。这是当前代码的一个可维护性短板：理想做法应把它集中成一个平台参数（如 `ROM_LATENCY`），让 `offset` 自动推导，避免散落修改。

#### 4.5.4 代码实践

**实践目标**：把项目从 Vivado 切到 Anlogic，定位所有需要改的 `HALT` offset，并解释每处的来历。

**操作步骤**：

1. 在 `src/` 全局搜索 `HALT_FOR_NEXT_LAYER-3` 与 `HALT_FOR_NEXT_LAYER-2`，列出所有命中行（应包括 `butterfly_general.v`、`fft_4.v`、`fft_8.v`、`fft_16.v`）。
2. 对每一处：把启用的 `-3` 注释掉，把 `-2` 取消注释（切到 Anlogic）。
3. 在 Anlogic 工具里生成 ROM IP 后，**实测其读延迟**（给地址后看 `douta`/`doa` 几拍后有效）。
4. 若实测延迟与「Anlogic = Xilinx − 1」的隐含假设不符，则 `-2` 也不对，需按实测值重新推算 offset（可能要写成 `-1` 或 `-4` 等）。

**需要观察的现象**：切换后，若 offset 没算对，仿真波形里下一级 `start` 会在旋转因子尚未就绪（或已过有效窗）时触发，`out_real`/`out_img` 出现错位或全错。

**预期结果**：列出「需要改的行清单」，并对每处写出新的 offset 值。具体 offset **待本地验证**——它取决于你新平台 ROM 的真实读延迟。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `-2`（anlogic）比 `-3`（vivado）让 `start_next` **更早**发出？

**参考答案**：`offset` 越小，触发点 `HALT − offset` 越大……注意这里方向：`start_next_counter` 从 0 往上数，达到 `HALT − offset` 时发脉冲。`offset=2` 时阈值为 `HALT−2`，`offset=3` 时为 `HALT−3`，前者阈值更大、需要数到更大的数，所以 `-2` 反而**更晚**发出。这与「Anlogic ROM 读延迟更短、不需要多等」是一致的——读延迟短，启动可以更早，但由于阈值更大，实际表现为……（此处方向需结合波形确认，**结论以本地仿真为准**）。关键认知是：**offset 与 ROM 读延迟强耦合，方向不能凭直觉，必须仿真确认。**

**练习 2**：如果把 `HALT_FOR_NEXT_LAYER` 的 offset 集中成一个 `ROM_LATENCY` 参数，公式应该怎么写？

**参考答案**：例如定义 `parameter ROM_LATENCY = 3;`（vivado）或 `2`（anlogic），把触发条件写成 `next_level_start_counter == HALT_FOR_NEXT_LAYER - ROM_LATENCY`。这样切换平台只改 `ROM_LATENCY` 一处，避免在多个文件里散落修改 `-2`/`-3`，降低漏改风险。

---

## 5. 综合实践

### 移植到新 FPGA 的完整 checklist

**任务**：假设你要把整套 fpga-fft 搬到一块国产 Anlogic FPGA（或 Intel FPGA）。请按下面的 checklist 完成移植准备，并针对 `delay.v` 单独写一份移植清单。

**第一步：列出所有需要重新生成或替换的 IP 核**

按 4.2 节的清单，逐类确认：

| IP | 新平台对应资源 | 数量 | 备注 |
| --- | --- | --- | --- |
| `mult2` | DSP 乘法器 IP | 每级 4 个 ×（fft_4~fft_16k 共 13 级） | 保证位宽 32×18→50、流水线延迟与原一致 |
| `Delay` | 双口 RAM（BRAM） | 每级 2 个 ×（fft_16~fft_16k 用 delay） | 按 layer 配深度；端口按 4.3 节映射 |
| `rotator_<N>_real/img` | 单口 ROM（BRAM） | fft_16~fft_16k 每级 2 块 | 用同一批 `.coe` 初始化 |
| `blk_mem_gen_0` | 块存储 ROM | 1（rom.v 内，若用到） | 主流水线未用 |
| `addr_ctrl` | 地址生成（IP 或自写） | 1（rom.v 内） | 仓库无定义，待确认 |

**第二步：针对 `delay.v` 的移植 checklist**

1. **替换 IP**：在新平台生成一个名为 `Delay` 的双口 RAM，端口风格按目标平台（Anlogic 用 `dia/cea/ceb/dob`，Intel 用对应双口 RAM 名）。
2. **修正注释版笔误**：若启用 Anlogic 版，把 `delay_img` 的 `.addra(r_addrb)` 改回 `.addra(r_addra)`（见 4.3.3 的陷阱）。
3. **核对位宽与深度**：数据 32 位；深度由 `layer` 决定（`DELAY_TIME = 1<<(layer-1)`，见 [src/delay.v:L17](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/src/delay.v#L17)）。
4. **核对读写时序**：确认新 RAM 是「先写后读」、读延迟几拍，必要时调整 `required_delay_in_state_machine` 里的 `-5` 补偿（见 u3-l2）。
5. **重新校准 `HALT` 常量**：见第三步。

**第三步：重新校准 `HALT_FOR_NEXT_LAYER` 的 offset**

1. 实测新平台 ROM 的读延迟 \(L_{\mathrm{rom}}\)（几个时钟）。
2. 计算 offset：以 vivado（`-3`，对应 \(L_{\mathrm{rom}}=2\)）为基准，若新平台读延迟为 \(L_{\mathrm{new}}\)，则 offset ≈ `3 + (L_new − 2)`。
3. 在 `butterfly_general.v`、`fft_4.v`、`fft_8.v`、`fft_16.v` 四处同步修改 offset（或集中成 `ROM_LATENCY` 参数）。

**第四步：验证**

1. 跑单级仿真（如 `fft_8_tb`），确认旋转因子与蝶形输出同拍对齐（`rotator_valid` 时序）。
2. 跑全链路仿真（`fft_top_tb`），用 MATLAB 黄金参考（u5-l1）比对输出。
3. 综合，确认资源映射到 DSP/BRAM、时序收敛。

**交付物**：一份 markdown 文档，包含（1）IP 替换总表；（2）`delay.v` 移植 checklist；（3）新平台 ROM 实测读延迟与据此推算的 offset 值（标注「待本地验证」处）。

## 6. 本讲小结

- 本项目依赖**五类厂商 IP**：`mult2`（DSP 乘法器）、`Delay`（双口 RAM）、`rotator_*_real/img`（旋转因子 ROM）、`blk_mem_gen_0`（块存储）、`addr_ctrl`（地址控制器），它们都不随源码提供，必须用目标平台工具重新生成。
- `delay.v` 同时保留 **Anlogic 版（注释）与 Xilinx 版（启用）** 两套双口 RAM 例化，端口对照为 `dia/dina`、`cea/wea`、`dob/doutb` 等；作者刻意让两套 IP 同名 `Delay`，但端口名未统一，移植时端口映射段仍需手改。
- 注释掉的 Anlogic 代码里有**复制粘贴遗留的 bug**（`delay_img` 和 `rotator_16_img` 误用 `r_addrb`），切回 Anlogic 版时必须修正，否则编译不过或数据错乱。
- `HALT_FOR_NEXT_LAYER` 的 **`-2`（anlogic）/ `-3`（vivado）** 之差，精确对应两家 ROM 读延迟之差（README 明确：Xilinx ROM 读延迟 2 拍）；这个 offset 分散在 `butterfly_general.v` 与 `fft_4/8/16` 多个文件，是移植最隐蔽的陷阱。
- 移植的核心动作是三件：**重新生成全部 IP 并对齐端口、修正注释版笔误、按新平台 ROM 实测读延迟重新校准 `HALT` offset**，最后用单级 + 全链路仿真 + MATLAB 黄金参考验证。

## 7. 下一步学习建议

- **下一步读 u5-l4（架构反思与扩展）**：它从整体上反思这套设计的取舍，包括「输出倒序（bit-reverse）未实现」「`data_config` 配置端口未启用」等已知缺陷，并提出改进方向。本讲讨论的「IP 名散落、offset 散落」也是典型的可维护性问题，u5-l4 会给出更系统的架构改进建议（如参数化点数、集中化平台常量）。
- **建议继续阅读的源码**：把 `fft_top.v`（顶层连线）与 `butterfly_general.v`（参数化层）再过一遍，体会「如果要把平台相关常量集中，应该放在哪一层」。也可以尝试自己写一个 `platform_pkg.v`，把 `ROM_LATENCY`、`RAM_VENDOR` 等集中定义，作为本讲移植思路的代码实践。
- **动手练习**：仿照 4.3 节，把 `Rotator16.v` 的 ROM 从 Xilinx 版切到 Anlogic 版（修正 `r_addrb` 笔误后），并在 IP 工具里实测读延迟，反推该层 `WAIT_FOR_ROTATOR` 是否需要调整——这是把本讲「读时序校准」落到具体一层的最佳练习。
