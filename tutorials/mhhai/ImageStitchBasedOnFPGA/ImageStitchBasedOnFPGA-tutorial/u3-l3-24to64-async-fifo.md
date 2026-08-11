# 24bit到64bit异步FIFO数据位宽转换

## 1. 本讲目标

学完本讲，你应当能够：

- 说清为什么七路摄像头采集的 **24bit** 像素流，必须经过一个**数据位宽转换的异步 FIFO**，才能喂给 **64bit** 接口的 DDR3 控制器（`mem_burst`）。
- 解释为什么这个 FIFO **不能用 Block RAM**，而要用**二维寄存器数组**（2D register array），以及由此带来的「写端 200MHz 出错、100MHz 可用」的时序代价。
- 用最大公约数（gcd）推导出 FIFO 的存储宽度应为 **8bit**，进而说明为什么两个指针「一次跳 3、一次跳 8」。
- 说清为什么「指针一次跳 3/8」会让经典异步 FIFO 的**格雷码（Gray code）方案失效**，以及 README 给出的替代策略——「用写端逻辑驱动读端逻辑」。
- 根据一次 DDR3 突发传输的长度，估算 FIFO 的 **depth**，并解释 depth 必须是 \(2^n\)、过大会拖慢编译。

> ⚠️ 重要提醒：本讲描述的 **24bit→64bit 异步 FIFO 模块的实现代码并未收录在本仓库中**（见 [u1-l2 仓库导航](u1-l2-repo-navigation.md)）。全讲唯一的直接源码依据是 [README.md](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md) 中的一段工程要点描述。因此本讲的所有 Verilog 代码片段都是**示例代码**（用来帮你看懂 README 在说什么），不是项目原代码；涉及「作者究竟怎么实现」的细节一律标注「待确认」。

## 2. 前置知识

本讲承接 [u3-l1 DDR3突发传输控制器 mem_burst](u3-l1-ddr3-mem-burst.md)（你要记得 `mem_burst` 的用户侧 `wr_burst_*` 接口长什么样）和 [u1-l1 项目总览](u1-l1-project-overview.md) 中画过的数据主线。下面先用大白话补几个本讲要用到的新术语。

- **异步 FIFO（asynchronous FIFO）**：一种先进先出缓存，**写端用一个时钟、读端用另一个时钟**，两个时钟频率/相位不同（跨时钟域，CDC）。它的核心难题是：如何在两个互不同步的时钟域之间，安全地传递「里面有多少数据」这件事，又不丢失数据、又不重复读。
- **数据位宽转换（data width conversion）**：写端进来的数据是一种宽度（如 24bit），读端出去是另一种宽度（如 64bit）。一个普通的等宽 FIFO 干不了这事，必须在内部把数据「拆开重装」。
- **Block RAM（BRAM）**：Xilinx FPGA 片内的专用存储块，容量大、能跑高频，但它对端口宽度有硬性约束（见 4.2）。
- **二维寄存器数组（2D register array）**：即 Verilog 里 `reg [7:0] mem [0:N-1];` 这种用触发器（flip-flop）堆出来的存储。它没有端口宽度约束、可以随意定义，但用的是通用逻辑资源、跑不了太高频率。
- **格雷码（Gray code）**：相邻两个数只有 1 个比特不同的编码。经典异步 FIFO 用它来传递指针，保证跨时钟域采样时「最坏也只差一」。
- **亚稳态（metastability）**：触发器在「建立/保持时间」被违反时，输出可能在 0 和 1 之间晃荡很久才稳定。跨时钟域信号若不加处理，亚稳态会导致采样到错误值。

一个关键直觉：**位宽转换 + 跨时钟域 = 两件难事叠在一起**。本仓库只给出了作者踩坑后的「结论」（README 一段话），没有给出实现代码。本讲的任务，是把这段结论拆成你能动手设计的工程知识。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [README.md](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md) | **本讲唯一直接依据**。第 6–9 行是作者对该异步 FIFO 的设计要点说明（为什么需要、用什么存储、width/depth、指针 +3/+8 与格雷码失效）。 |
| [DDR3控制/mem_burst.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v) | 本 FIFO 的**下游消费者**。它的用户侧 `wr_burst_*` 接口（`wr_burst_data`/`wr_burst_data_req`/`wr_burst_len`/`wr_burst_finish`）正是本 FIFO 读端要对接的对象（见 4.4、4.5）。 |
| [DDR3控制/mem_test.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_test.v) | `mem_burst` 的典型用户，里面 `wr_burst_len=128` 是本讲估算 depth 时的参考突发长度（见 4.5）。 |

> 说明：本 FIFO 模块的实现（.v 文件）**不在仓库内**，故「本讲源码地图」里没有它本身。后续出现 `待确认` 的地方，都是「README 提到了但仓库里看不到代码、无法核实」的设计细节。

## 4. 核心概念与源码讲解

### 4.1 需求与定位：为什么要做 24bit→64bit 异步 FIFO

#### 4.1.1 概念说明

回顾 [u1-l1](u1-l1-project-overview.md) 的数据主线：摄像头采集像素 → 进入 FPGA → 存到 DDR3 → 做圆柱面投影 → ……

这里有两段「宽度」对不上：

- **采集侧**：每个像素是 **24bit**（通常 R/G/B 各 8bit）。摄像头像素时钟是它自己的时钟域。
- **DDR3 侧**：`mem_burst` / MIG 的应用接口数据线 `MEM_DATA_BITS` 是 **64bit**（见 [mem_burst.v:5](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L5)），跑在 DDR3 的 `ui_clk` 时钟域。

24bit ≠ 64bit，且两者不在同一个时钟域。你不能把 24bit 的线直接连到 64bit 的端口上。必须在中间放一个**位宽转换的异步 FIFO**：它一边一口一口吃 24bit 像素，一边攒够 64bit 后吐出一个个 64bit 字交给 `mem_burst`，同时顺带完成两个时钟域的隔离。

这正是 README 第一句要点交代的事。

#### 4.1.2 核心流程

```
摄像头像素流(24bit, wr_clk 域)                DDR3 字流(64bit, ui_clk 域)
        │                                              ▲
        ▼                                              │
  ┌──────────── 24bit→64bit 异步 FIFO ────────────┐
  │  写端：每像素写 24bit（拆成 3 个 8bit 单元）   │
  │  读端：每拍读 64bit（拼成 8 个 8bit 单元）     │
  │  跨时钟域：wr_clk ↔ ui_clk                      │
  └─────────────────────────────────────────────────┘
        │ 写端累计到「一次突发」的数据量
        ▼
  触发 mem_burst 的 wr_burst_req → 写入 DDR3
```

#### 4.1.3 源码精读

README 明确点出这个需求（也是本讲存在的理由）：

> 采集的像素数据是 24bit，DDR3 控制器的接口是 64bit，也就是说需要自己写一个 24bit 到 64bit 的异步 FIFO 转换模块。这部分网上资料很少……
>
> —— [README.md:6](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L6)

注意「这部分网上资料很少」一句——作者强调这是一个**非标准、非等宽**的异步 FIFO，现成 IP（如 Xilinx 的 `FIFO Generator`）默认写宽=读宽，或要求宽度比为 2 的幂，直接套用并不方便，所以才「自己写」。

下游接口的宽度事实，可以回 `mem_burst` 求证：数据位宽参数就是 64bit —— [mem_burst.v:5](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L5)，端口 `wr_burst_data[MEM_DATA_BITS-1:0]` 即 `[63:0]` —— [mem_burst.v:20](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L20)。这印证了「读端必须是 64bit」。

#### 4.1.4 代码实践

**实践目标**：把「24bit 与 64bit 对不上」这件直觉，落实到仓库里的两个数字上。

**操作步骤**：
1. 在 [mem_burst.v:5](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L5) 找到 `MEM_DATA_BITS` 的默认值，确认 DDR3 侧宽度。
2. 假设摄像头每像素 RGB888，确认采集侧宽度 = 8+8+8 = 24bit。
3. 写一句话：「采集侧 24bit @ 像素时钟域 → 异步 FIFO → DDR3 侧 64bit @ ui_clk 域」。

**需要观察的现象**：两侧宽度不等、时钟域不同，是「需要异步 + 位宽转换」的双重原因。

**预期结果**：能说清「为什么不能拿一根线直连」。

#### 4.1.5 小练习与答案

- **练习 1**：如果摄像头改成每像素 16bit（RGB565），还需要位宽转换吗？
  - **答案**：需要。16 ≠ 64，仍然对不上 DDR3 的 64bit 接口；只是宽度比从 24:64 变成 16:64，FIFO 的存储宽度会跟着变（变成 \(\gcd(16,64)=16\)bit，写端每次 +1 单元、读端 +4 单元）。本仓库固定是 24bit。
- **练习 2**：能不能直接把 24bit 像素塞进 BRAM 再慢慢读，省掉这个 FIFO？
  - **答案**：理论可行但要付出代价——既要做位宽转换又要跨时钟域，BRAM 的端口宽度约束（见 4.2）会卡住「24 进 64 出」，最终还是要绕回作者选的路子。

### 4.2 存储结构选型：为什么不能用 Block RAM

#### 4.2.1 概念说明

这是 README 给出的**第一个工程决策**：存储体用 Block RAM 还是二维寄存器数组？作者的结论是——**Block RAM 不能用，要用二维寄存器数组**。

原因是 Xilinx Block RAM 对**双端口宽度比**有硬约束：两个端口的宽度比必须是 **2 的幂**（如 1:1、1:2、1:4、1:8、1:16……）。这是因为 BRAM 内部按「位」均匀切分，每个端口一次读/写的位数必须能整除同一块存储阵列。

而本 FIFO 要做的是 **24bit 进、64bit 出**，宽度比是：

\[
\frac{64}{24} = \frac{8}{3} \approx 2.67
\]

\(8/3\) **不是 2 的幂**（分母有个 3）。所以 BRAM 无法同时开一个 24bit 写口和一个 64bit 读口。于是作者改用**二维寄存器数组**——它本质是触发器堆，没有任何端口宽度约束，你爱怎么拆就怎么拆。

#### 4.2.2 核心流程

```
方案A：Block RAM（被否决）
  写口 24bit ─┐  宽度比 64/24 = 8/3，不是 2 的幂
  读口 64bit ─┘  → BRAM 物理上做不到，PASS

方案B：二维寄存器数组（采纳）
  reg [7:0] mem [0:DEPTH-1];   // 每个单元 8bit
  写端：把 24bit 拆成 3 个 8bit，写入 mem[p]、mem[p+1]、mem[p+2]
  读端：把 8 个 8bit 拼成 64bit，从 mem[q..q+7] 读出
  代价：用触发器实现，频率上不去（200MHz 出错，100MHz 可用）
```

#### 4.2.3 源码精读

README 原文（决策 + 实测频率上限）：

> a. 存储器是用 Block RAM 还是二维寄存器数组。Block RAM 不能用，因为输入输出的位宽需要满足 2^n 次方关系，因此好的办法是用二维寄存器数组了。用二维寄存器数组，在写入像素数据时时钟频率不能太高，否则写入的数据会出错。实测写端 200MHz 出错，100MHz 可以。
>
> —— [README.md:7](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L7)

「输入输出的位宽需要满足 \(2^n\) 次方关系」正是上面说的「BRAM 宽度比必须是 2 的幂」。24:64 不满足（含因子 3），所以 BRAM 被排除。

> 注意：作者在 [README.md:8](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L8) 紧接着说该 FIFO 的 **width 应该是 8bit**。也就是说，最终选择的存储单元宽度是 8bit（\(\gcd(24,64)=8\)，见 4.3），而不是直接拿 24bit 当写口——这样写端每拍写 3 个 8bit 单元、读端每拍读 8 个 8bit 单元。这也回过头解释了为什么 BRAM 即使想用也尴尬：BRAM 能做 8bit↔64bit（比 1:8，是 2 的幂），但那样写端就得拆成「3 拍各写 8bit」而不是「1 拍写 24bit」，写端吞吐被砍到 1/3，作者不愿接受，于是干脆上寄存器数组。**此处的具体权衡作者未展开，标注待确认。**

#### 4.2.4 代码实践

**实践目标**：动手验证「24:64 不满足 2 的幂」这个判据。

**操作步骤**：
1. 计算 \(64/24\)，化简为最简分数。
2. 判断分母（和分子）是否只含因子 2。
3. 再对比一组「能进 BRAM」的宽度比，如 8:64、16:64，看它们的比是否是 2 的幂。

**需要观察的现象**：8:64 = 1:8（\(=2^3\)）✅；16:64 = 1:4（\(=2^2\)）✅；24:64 = 3:8 ❌。

**预期结果**：亲手确认 24:64 因含因子 3 而被 BRAM 拒绝。

#### 4.2.5 小练习与答案

- **练习 1**：如果把 FIFO 的存储单元宽度定为 8bit，那「写端 1 拍写 24bit」需要往几个单元写？读端 1 拍读 64bit 呢？
  - **答案**：写端写 \(24/8=3\) 个单元；读端读 \(64/8=8\) 个单元。这正是 4.3 里指针 +3 / +8 的由来。
- **练习 2**：为什么二维寄存器数组「写端频率不能太高」？
  - **答案**：寄存器数组是用通用触发器 + 多路选择/译码逻辑堆出来的，写 3 个单元意味着「地址译码 + 选通 3 个目标触发器」要走一段组合逻辑，路径较长。频率高（周期短）时建立时间不够，触发器 latch 到错误数据。BRAM 有专用硬件写口能跑高频，但本设计用不了 BRAM。（详细分析见本讲综合实践第 3 题。）

### 4.3 存储宽度 8bit 与指针 +3/+8 的来由：gcd

#### 4.3.1 概念说明

README [第 8 行](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L8) 给出两个关键参数：

- **width = 8bit**（存储单元宽度）
- 两个指针「一次加 3、一次加 8」（[第 9 行](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L9)）

这两件事其实是**同一个数学结论的两面**，根源是最大公约数。

设写端一次进 \(W_{\text{写}}=24\)bit、读端一次出 \(W_{\text{读}}=64\)bit，FIFO 的存储单元宽度为 \(w\)。为了让「写」和「读」都能整数次地填满/抽干存储单元，\(w\) 必须同时整除 24 和 64。能取的最大宽度就是它们的最大公约数：

\[
w = \gcd(24,64) = 8
\]

用辗转相除法验证：

\[
64 = 2\times 24 + 16,\quad 24 = 1\times 16 + 8,\quad 16 = 2\times 8 + 0 \;\Rightarrow\; \gcd=8
\]

于是每次写/读要搬动的单元数为：

\[
n_{\text{写}} = \frac{24}{8}=3,\qquad n_{\text{读}} = \frac{64}{8}=8
\]

这就是「一个指针一次 +3、另一个一次 +8」的全部数学来源。**因为存储单元取了 gcd，两边才都能整数次搬动**——这是非等宽位宽转换的标准套路。

#### 4.3.2 核心流程

```
选 width = gcd(24,64) = 8bit
  → 写端(24bit) 一次写 3 个单元  → 该侧指针每次 +3
  → 读端(64bit) 一次读 8 个单元  → 该侧指针每次 +8
两个指针都不是「+1」，这正是 4.4 里格雷码失效的导火索。
```

#### 4.3.3 源码精读

README 原文：

> b. 输入是 24bit，输出是 64bit，该异步 FIFO 的 width 应该是 8bit……
>
> —— [README.md:8](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L8)

> c. 读指针一次加 3，写指针一次加 8……
>
> —— [README.md:9](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L9)

下面这段是**示例代码**（仓库未收录实现，仅据 README 第 8–9 行的 width=8bit、+3/+8 描述示意）：

```verilog
// ⚠️ 示例代码：本仓库未收录该 FIFO 的实现，此处仅为帮助理解 README 的描述
reg [7:0] fifo_mem [0:DEPTH-1];   // 二维寄存器数组，width = 8bit（= gcd(24,64)）

// —— 摄像头侧（24bit）：每来 1 个像素，写 3 个 8bit 单元 ——
always @(posedge clk_cam) begin
  if (pixel_valid) begin
    fifo_mem[ptr_cam    ] <= din[ 7: 0];
    fifo_mem[ptr_cam + 1] <= din[15: 8];
    fifo_mem[ptr_cam + 2] <= din[23:16];
    ptr_cam <= ptr_cam + 3'd3;        // 该侧指针一次 +3
  end
end

// —— DDR3 侧（64bit）：每读 1 个字，拼 8 个 8bit 单元 ——
always @(posedge clk_ddr) begin
  if (rd_en) begin
    dout <= {fifo_mem[ptr_ddr+7], fifo_mem[ptr_ddr+6], fifo_mem[ptr_ddr+5], fifo_mem[ptr_ddr+4],
             fifo_mem[ptr_ddr+3], fifo_mem[ptr_ddr+2], fifo_mem[ptr_ddr+1], fifo_mem[ptr_ddr]};
    ptr_ddr <= ptr_ddr + 4'd8;        // 该侧指针一次 +8
  end
end
```

> 📌 **关于「读/写」命名的说明（待确认）**：README [第 9 行](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L9) 原文是「**读指针一次加 3，写指针一次加 8**」。但按物理数据流，24bit 的摄像头侧才是「往 FIFO 里写」（应 +3），64bit 的 DDR3 侧才是「从 FIFO 里读」（应 +8）——即写 +3、读 +8，与 README 字面相反。由于**实现代码不在仓库、无法核实**，作者究竟是按哪一侧命名「读/写」已不可考。本讲为避免误导，示例代码用 `ptr_cam`/`ptr_ddr`（按物理侧命名）而非 `wr_ptr`/`rd_ptr`。**唯一确定的不变量是：24bit 侧 +3、64bit 侧 +8，两边都不是 +1。**

#### 4.3.4 代码实践

**实践目标**：用 gcd 自己推出 width 和两个步长，不背 README 的数字。

**操作步骤**：
1. 用辗转相除法手算 \(\gcd(24,64)\)。
2. 写出 \(n_{\text{写}}=24/\gcd\)、\(n_{\text{读}}=64/\gcd\)。
3. 对照 [README.md:8-9](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L8-L9)，验证你算出的 width=8、步长 3/8 与作者一致。

**需要观察的现象**：三个数字（8、3、8）全部由 \(\gcd(24,64)\) 一个根推出。

**预期结果**：理解「为什么是 8/3/8，而不是别的数」——它们不是拍脑袋定的。

#### 4.3.5 小练习与答案

- **练习 1**：如果把采集改成 16bit 像素、DDR3 仍 64bit，width 和两个步长分别变成多少？
  - **答案**：\(w=\gcd(16,64)=16\)；写端 \(16/16=1\)（+1），读端 \(64/16=4\)（+4）。注意此时写端变成 +1，那一侧就能用格雷码了——但读端 +4 仍不行。
- **练习 2**：为什么作者强调 width「**应该**是 8bit」而不是 4bit 或 2bit？
  - **答案**：取 gcd=8 能让存储单元最大、单元数最少（depth 以 8bit 为单位计数最省资源）；取更小的公因子（如 4、2）也能整除，但会让单元数翻倍、寄存器数组更大、编译更慢，没有好处。

### 4.4 跨时钟域：格雷码失效与「写端驱动读端」

#### 4.4.1 概念说明

这是本讲最硬核的一点，也是 README [第 9 行](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L9) 的核心警告。

**经典异步 FIFO 怎么防亚稳态？** 它的招数是：

1. 把二进制指针转成**格雷码**指针；
2. 格雷码指针过「两级触发器同步器」到对方时钟域；
3. 在对方域里比较「读指针」和「写指针」判断空/满。

格雷码的好处是**相邻两个值只有 1 个比特不同**。所以跨时钟域采样时，最坏情况只是「采到旧值」，指针最多差 1，判断出来的空/满是**保守安全**的（最多让你晚一拍读/早一拍停，不会错乱）。

**但这一切的前提是：指针一次只 +1。**

本 FIFO 的两个指针一次 +3 和 +8。以 4 位二进制 0→3 为例（写端 +3）：

| 十进制 | 二进制 | 格雷码 |
|---|---|---|
| 0 | 0000 | 0000 |
| 3 | 0011 | 0010 |

从 0 跳到 3，格雷码 `0000`→`0010` 有 **2 个比特**翻转（bit1、bit2）。跨时钟域采样时，这两个比特可能「一个采到新值、一个采到旧值」，结果得到一个既不是 0 也不是 3 的**乱码值**（如 0001、0011 对应的格雷中间态）。空/满判断就可能错乱，丢数据或重复读。

更深一层：写指针总是 3 的倍数、读指针总是 8 的倍数，两者**永远不会相等**（除 0 外）——连经典「指针相等即空/满」的比较逻辑都天然不成立。所以 README 直接宣判：

> 转换成格雷码来降低亚稳态出现的概率已经不行了。

#### 4.4.2 核心流程

作者的替代策略是「**用写端逻辑控制读端逻辑**」，本质是把「跨域比较两个飞快变化的指针」换成「跨域传一个稳定的电平条件」：

```
经典方案（失效）：          本 FIFO 方案（README）：
  wr_ptr 二进制→格雷          写端用本地时钟累计 FIFO 里的数据量
  → 同步到读域                  （不跨域传飞变的指针）
  → 与 rd_ptr 比较 full/empty   只要数据量 ≥ 一次 DDR3 突发长度
                                → 拉高一个稳定的「突发就绪」电平
                                → 该电平同步到读域后触发一次突发写
```

关键点：触发条件是**「数据量达到一次突发」这个阈值**（一个缓变、稳定的电平），而不是「两个指针相等」这种对逐拍变化敏感的比较。电平信号跨时钟域只需一个普通的两级同步器，亚稳态风险可控。

#### 4.4.3 源码精读

README 原文（本讲最关键的一句）：

> c. ……读指针一次加 3，写指针一次加 8，因此转换成格雷码来降低亚稳态出现的概率已经不行了，需要根据写端逻辑来控制读端逻辑：**只要异步 FIFO 中的数据满足一次 DDR3 突发传输的长度，那么就将数据写入到 DDR3 中**。
>
> —— [README.md:9](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L9)

注意「满足一次 DDR3 突发传输的长度」——这里的「长度」指的是 `mem_burst` 一次 `wr_burst` 要的字数（`wr_burst_len`，见 [mem_burst.v:14](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L14)）。本 FIFO 的读端（DDR3 侧）正是直接对接 `mem_burst` 的 `wr_burst_*` 用户接口，触发关系如下：

```
本 FIFO 内部数据量 ≥ wr_burst_len × 8 个单元
   → 拉高 burst_ready（电平，写到本 FIFO 内部寄存器）
   → 两级同步到 ui_clk 域
   → 驱动 mem_burst 的 wr_burst_req（发起一次写突发）
   → mem_burst 在 MEM_WRITE 状态逐拍拉 wr_burst_data_req
        → 本 FIFO 每拍吐 1 个 64bit 字（ptr_ddr +8），共 wr_burst_len 个字
   → mem_burst 拉高 wr_burst_finish → 重新等待下一批攒满
```

> 这里的「burst_ready 电平 → 同步 → wr_burst_req」连线，以及「wr_burst_data_req → 本 FIFO rd_en」「dout → wr_burst_data」连线，都是根据 README 描述 + [u3-l1](u3-l1-ddr3-mem-burst.md) 讲过的 `mem_burst` 用户接口**推导**的对接方式，仓库内没有顶层连线代码，**标注待确认**。

#### 4.4.4 代码实践

**实践目标**：体会「指针 +3/+8 让格雷码不再单比特变化」。

**操作步骤**：
1. 列一张 4 位二进制 ↔ 格雷码对照表（0~15）。
2. 找出「+1」「+3」「+8」三种步长下，格雷码每次翻转的比特数。
3. 统计：+1 时每次翻几比特？+3、+8 时翻几比特？

**需要观察的现象**：+1 永远只翻 1 比特；+3、+8 经常一次翻 2~3 比特。

**预期结果**：亲眼看到「只有 +1 才满足格雷码单比特跳变前提」，从而理解作者为什么说格雷码「不行了」。

#### 4.4.5 小练习与答案

- **练习 1**：能不能把 +3 的指针也用格雷码传，只是「容忍更大误差」？
  - **答案**：不行。格雷码的「安全」完全建立在「单比特跳变→最坏采到旧值」上。多比特跳变时，同步器可能采到一个既非旧也非新的乱码指针，直接导致空/满判错、丢数据，不是「误差大」而是「可能错」。所以作者放弃格雷码，改用阈值电平触发。
- **练习 2**：为什么「数据量达到一次突发长度」这个条件适合跨时钟域？
  - **答案**：因为它是一个**缓变的电平**（攒满才拉高，一拉高就持续到突发被启动），不是逐拍翻转的脉冲。缓变电平过两级同步器非常稳，亚稳态概率低；即便采晚一拍，也只是突发晚一点启动，不会错乱数据。

### 4.5 容量设计：width、depth 与 DDR3 突发长度的关系

#### 4.5.1 概念说明

README [第 8 行](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L8) 给出 depth 的两条规则：

1. depth **根据一次突发传输的长度**自己定；
2. depth **必须是 \(2^n\)**（2 的幂），且「太高编译时间会大大加长」。

为什么是这两条？

- **「根据一次突发长度」**：因为本 FIFO 的使命就是「攒够一次突发就吐给 `mem_burst`」。一次突发 = `wr_burst_len` 个 64bit 字 = `wr_burst_len × 8` 个 8bit 单元。所以 FIFO 至少要装得下一次突发：

\[
\text{depth} \;\ge\; \text{wr\_burst\_len} \times \frac{64}{8} \;=\; L \times 8 \quad (\text{个 8bit 单元})
\]

其中 \(L\) 是 `wr_burst_len`。

- **「必须是 \(2^n\)」**：FIFO 指针要回卷（wrap），地址取模运算 `ptr mod depth` 在 depth=\(2^n\) 时只需截低位（`ptr[k-1:0]`），不需要除法器，硬件极简。

- **「太高编译会慢」**：因为存储体是**二维寄存器数组**（触发器）。depth 越大，触发器越多、连线越密，综合工具要花更长时间布局布线。这是 4.2「用寄存器数组」决定的连带代价。

#### 4.5.2 核心流程

```
已知 mem_burst 的一次突发字数 = wr_burst_len（记为 L）
  → 一次突发占用 FIFO 单元数 = L × 8
  → depth 最小取 L×8，再向上取到最近的 2^n
  → 若 depth 太大（如上万），综合时间显著变长
```

举几个例子（**突发长度取决于顶层配置，仓库内 mem_test 用 128 仅作参考，实际值待确认**）：

| 突发字数 \(L\) | 突发占用的 8bit 单元数 \(L\times8\) | depth 取整（≥且为 \(2^n\)） | 寄存器数（depth×8bit） |
|---|---|---|---|
| 8（MIG BL8 级别） | 64 | \(64=2^6\) | 512 |
| 64 | 512 | \(512=2^9\) | 4096 |
| 128（mem_test 用值） | 1024 | \(1024=2^{10}\) | 8192 |

可见即使突发只 128 字，寄存器数组就要 8192 个触发器，相当吃资源——这正是 README 说「depth 不能太高」的现实原因。

#### 4.5.3 源码精读

README 原文：

> b. ……depth 根据一次突发传输的长度可以自己定，depth 太高的话，编译时间会大大加长，而且 depth 肯定是 2^n 次方；
>
> —— [README.md:8](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L8)

「一次突发传输的长度」在仓库里能找到参考：`mem_test` 驱动 `mem_burst` 时把长度设为 128 —— 见 [u3-l1 综合实践](u3-l1-ddr3-mem-burst.md) 引用的 [mem_test.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_test.v) 里 `wr_burst_len=128`。但这是**自检模块**用的值，真实图像采集链路用多大突发、FIFO 实际取了多大 depth，**仓库里都没有，待确认**。

#### 4.5.4 代码实践

**实践目标**：给定一个突发长度，自己算出合理的 depth。

**操作步骤**：
1. 假设图像链路一次突发写 \(L=128\) 个 64bit 字到 DDR3。
2. 算出突发占用 \(128 \times 8 = 1024\) 个 8bit 单元。
3. 向上取到 2 的幂：1024 已是 \(2^{10}\)，所以 depth=1024。
4. 估计资源：\(1024 \times 8 = 8192\) 个触发器。
5. 思考：若想把 depth 砍半到 512（\(2^9\)），能否还撑住一次 128 字突发？为什么？

**需要观察的现象**：depth 必须 ≥ 一次突发量，否则还没攒满就被读走，触发条件永远不稳。

**预期结果**：建立「depth ≥ L×8、取 \(2^n\)、别太大」的三条取舍意识。（第 5 题：512 < 1024，撑不住一次 128 字突发，会把一次突发拆成两次，时序更复杂——所以 depth 不能随便砍。）

#### 4.5.5 小练习与答案

- **练习 1**：depth 为什么必须是 \(2^n\)？
  - **答案**：让指针回卷用「截低位」实现（`ptr[k-1:0]`），避免取模除法器；同时 4.4 的阈值比较也更好做。非 \(2^n\) 的 depth 需要真正的比较/减法来回卷，硬件更贵。
- **练习 2**：如果 depth 远大于一次突发量（比如 depth=8192、突发只用 1024），有什么坏处？
  - **答案**：纯浪费。寄存器数组翻倍 → 综合时间变长、片上资源占用升高，而突发只用其中一小部分，没有任何吞吐收益。README「depth 太高编译时间大大加长」就是这意思。

## 5. 综合实践

把本讲四块知识（需求、存储结构、gcd 步长、跨时钟域、容量）串起来，做一次「**照着 README 把这个 FIFO 设计出来**」的纸面设计。这正是本讲的指定实践任务。

### 任务 1：写出该异步 FIFO 的端口列表与数据流说明

请根据本讲 4.1–4.5 的分析，**自己设计**一份端口表（参考答案在下方，先自己写再对照）。提示：端口要分「摄像头侧（写/24bit/像素时钟）」「DDR3 侧（读/64bit/ui_clk）」「控制/状态」三组，且读端要能对接 [mem_burst.v:8-23](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L8-L23) 的 `wr_burst_*` 用户接口。

> ⚠️ 以下端口表是**根据 README + u3-l1 推导的设计参考**，仓库内无实现代码，标注待确认。

| 组 | 端口 | 方向 | 位宽 | 说明 |
|---|---|---|---|---|
| 摄像头侧（clk_cam 域） | `clk_cam` | input | 1 | 写时钟，≤100MHz（见任务 3） |
| | `rst` | input | 1 | 复位（高有效，沿用工程约定） |
| | `pixel_valid` | input | 1 | 像素有效，拉高表示 `din` 上是 1 个有效像素 |
| | `din` | input | 24 | 24bit 像素（如 RGB888） |
| DDR3 侧（ui_clk 域） | `clk_ddr` | input | 1 | 读时钟 = MIG `ui_clk` |
| | `rd_en` | input | 1 | 读使能，接 `mem_burst` 的 `wr_burst_data_req` |
| | `dout` | output | 64 | 64bit 字，接 `mem_burst` 的 `wr_burst_data` |
| 控制/状态（跨域） | `burst_ready` | output | 1 | FIFO 内数据 ≥ 一次突发长度时拉高（电平），可作 `mem_burst` 的 `wr_burst_req` 来源 |
| | `burst_done` | input | 1 | 接 `mem_burst` 的 `wr_burst_finish`，一次突发结束后重新等待攒满 |

**数据流说明（请用自己的话复述一遍）**：

1. 摄像头在 `clk_cam` 域按像素流送来 24bit，每个 `pixel_valid` 把像素拆成 3 个 8bit 单元写入寄存器数组，`ptr_cam += 3`。
2. FIFO 内部用 `clk_cam` 计数已写入而未读出的单元数；当该数 ≥ `wr_burst_len × 8` 时拉高 `burst_ready`。
3. `burst_ready` 经两级同步器进 `clk_ddr` 域，触发 `mem_burst` 发起一次写突发（`wr_burst_req`，长度 `wr_burst_len`，起始地址由上层给定）。
4. `mem_burst` 进入 `MEM_WRITE` 后逐拍拉 `wr_burst_data_req`（即本 FIFO 的 `rd_en`），本 FIFO 每拍吐一个 64bit 字（拼 8 个单元，`ptr_ddr += 8`），共 `wr_burst_len` 个字。
5. `mem_burst` 写完拉高 `wr_burst_finish`（即 `burst_done`），本 FIFO 重新进入「攒满再发」循环。

### 任务 2：用一段 RTL 思路描述「写端逻辑控制读端逻辑」

要点（参考答案）：放弃「跨域比较两个 +3/+8 指针」的经典空满判断；改为在写端本地时钟域里维护一个**单元计数器**（每写一像素 +3、每被读走一字 -8，注意「-8」这个值要同步过来或改用「读走了几次」的同步脉冲来扣减）；当计数 ≥ 阈值时输出一个**缓变电平** `burst_ready`；该电平过两级同步去读域触发突发。这样跨域传递的是「是否够一次突发」这个布尔电平，而非飞快变化的指针，亚稳态风险可控。具体计数扣减的同步细节作者未给出，**待确认**。

### 任务 3：分析「写端 200MHz 出错、100MHz 可用」的可能原因

请先自己分析，再对照下面的参考要点。

**参考要点（可能原因）**：

1. **二维寄存器数组的写路径太长**：每次写要在 `clk_cam` 一拍内完成「地址译码 + 选通 3 个目标触发器 latch 数据」。寄存器数组（不同于 BRAM 的专用写口）这段是普通组合逻辑 + 高扇出连线，路径延迟较大。200MHz 周期仅 5ns，留给建立时间的余量不足 → 触发器 latch 到错误数据；100MHz 周期 10ns，余量翻倍 → 正常。
2. **读端 8 选 1 拼接的扇出/布线**：读侧一次要把 8 个单元拼成 64bit，多路选择器大、连线密，进一步挤压时序。但 README 明确说是「**写端**频率」受限，所以主因还是写路径。
3. **根本症结**：这正是 4.2「被迫放弃 BRAM、改用寄存器数组」的连带代价——BRAM 的专用存储口能轻松跑 200MHz+，而寄存器数组做不到。若要提速，可考虑：把写地址/写数据先寄存一拍（打断组合路径，流水化）、或降低单拍写入的单元数（如 24bit 分两次写，但会降吞吐）、或换器件/换实现。

> 待本地验证：若有该 FIFO 实现，可在 Vivado 里跑时序报告，看 `clk_cam` 域的 `WNS（最差建立时间裕量）`在 200MHz 下是否为负、100MHz 下是否为正，即可印证上述分析。

## 6. 本讲小结

- 本 FIFO 的存在理由：摄像头 **24bit** 像素流与 `mem_burst`/MIG 的 **64bit** 接口宽度不等、且分处不同时钟域，必须用一个「**位宽转换 + 跨时钟域**」的异步 FIFO 衔接（[README.md:6](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L6)）。
- 存储体不能用 **Block RAM**：BRAM 要求端口宽度比为 2 的幂，而 24:64 = 3:8 含因子 3，不满足；故改用**二维寄存器数组**（[README.md:7](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L7)）。
- 存储宽度取 \(\gcd(24,64)=8\)bit，于是写端一次 +3、读端一次 +8——三个数字由一个 gcd 统一推出（[README.md:8-9](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L8-L9)）。
- 指针 +3/+8 让经典**格雷码防亚稳态方案失效**（多比特翻转、指针永不相等），作者改用「**写端逻辑驱动读端逻辑**」——攒够一次突发量就用电平触发一次 DDR3 写（[README.md:9](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L9)）。
- depth ≥ 一次突发字数 ×8、向上取 \(2^n\)；因用寄存器数组，depth 太大会拖慢综合（[README.md:8](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/README.md#L8)）。
- ⚠️ 本 FIFO **实现代码不在仓库**，全讲基于 README 一段描述；所有示例代码仅为帮助理解，涉及作者实际实现细节处一律标注**待确认**。

## 7. 下一步学习建议

- **横向复习**：回看 [u3-l1 mem_burst](u3-l1-ddr3-mem-burst.md) 的 `wr_burst_*` 用户接口与 [u3-l2 mem_test](u3-l2-ddr3-mem-test.md) 的「写后读」自检，把本 FIFO 读端（`burst_ready`→`wr_burst_req`、`rd_en`↔`wr_burst_data_req`、`dout`→`wr_burst_data`、`burst_done`↔`wr_burst_finish`）的对接关系彻底吃透。
- **下一篇 u4-l1**：进入 [DynamicSeam 动态规划缝合线](u4-l1-dynamic-seam.md)，那里会例化 MIG 并从 DDR3 读重叠区数据——本讲讲清的「数据怎么进 DDR3」正是它的前置。
- **纵深选读 u5-l2**：[CORDIC 与 MIG/时钟 IP 集成](u5-l2-cordic-mig-ip-integration.md) 会把 `ui_clk`（本 FIFO 读时钟）的来源——`clk_wiz_0`/`mig_7series_0`——讲清楚，帮你补全「`clk_cam` 与 `ui_clk` 到底从哪来、差多少」的系统时钟图（具体 IP 配置待确认）。
- **动手延伸**：如果你想自己实现这个 FIFO，建议先用 \(\gcd\) 法写一个**等宽**异步 FIFO（指针 +1，能用格雷码）跑通仿真，再把它改造成 24→64 非等宽版本，体会 README 三条要点各自卡在哪。注意：这是你自己的练习产出，不是仓库代码。
