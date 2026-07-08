# 用 iverilog 跑仿真并看波形

## 1. 本讲目标

学完本讲，你应该能够：

- 说出本项目仿真所用的工具链：用 **iverilog** 编译、用 **vvp** 运行、用 **gtkwave** 看波形。
- 看懂 `SIM/` 目录里两个 `.bat` 脚本到底执行了哪三条命令，并能在 Linux 下手动敲出等价命令。
- 理解 `$dumpvars` 是如何把仿真过程中的信号变化写进 `dump.vcd` 波形文件的，以及为什么 `tb_svpwm.v` 里要写**两次** `$dumpvars`。
- 在 gtkwave 里把 `signed`（有符号）信号正确显示成**模拟（Analog）曲线**，从而直观看到正弦波、马鞍波。
- 解释**为什么本项目只仿真了 clark/park 和 svpwm 这几个子模块，却没有仿真整个 FOC**——答案是缺少电机的 Verilog 模型。

本讲是入门篇的最后一讲。它不深入 FOC 的数学（那是第 2 单元的事），而是教你**如何用免费工具把项目跑起来、如何用波形验证算法对不对**。这是一项贯穿后续所有源码精读的基础技能。

## 2. 前置知识

在开始前，你需要先建立以下几个朴素概念。如果还不太熟，不用担心，下面会结合源码再讲一遍。

- **什么是仿真（simulation）**：FPGA 代码（Verilog）描述的是硬件。但在把代码烧进芯片之前，我们可以用软件模拟器"假装"运行这段硬件，观察每个信号在每个时钟周期的取值，从而提前发现错误。这就叫仿真。
- **VCD 文件**：仿真过程中，信号随时间变化的全部记录会被写进一个文件，扩展名通常是 `.vcd`（Value Change Dump）。它就像一段"信号录像"。
- **波形查看器（waveform viewer）**：`gtkwave` 是一个免费工具，用来打开 VCD 文件，把"信号录像"画成一条条随时间变化的曲线（波形）。
- **有符号数（signed）**：Verilog 里一个 16 位信号既可以被当作无符号数（0~65535），也可以被当作有符号数（-32768~+32767）。本项目里电流、电压都用**有符号 16 位**表示（见 [u1-l2](./u1-l2-directory-and-hierarchy.md)）。在 gtkwave 里，如果忘了把信号设成"有符号"，正弦波的负半周就会显示成巨大的正数，曲线就完全不对了。
- **testbench（测试平台）**：一段只为仿真而写、不会被综合成真实硬件的 Verilog 代码。它负责产生时钟、复位、激励信号，并例化被测试的模块（Design Under Test, DUT）。本项目的 testbench 都放在 `SIM/` 目录。

> 名词小贴士：本讲里出现的 **DUT** = Design Under Test = 被测模块（例如 `clark_tr`）；**tb** = testbench 的常用缩写。

## 3. 本讲源码地图

本讲涉及的关键文件全部在 `SIM/` 目录下，共 4 个：

| 文件 | 作用 | 是否综合成硬件 |
| --- | --- | --- |
| [SIM/tb_clark_park_tr.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v) | 测试 Clark 变换 `clark_tr` 和 Park 变换 `park_tr` 的 testbench | 否（仅仿真） |
| [SIM/tb_clark_park_tr_run_iverilog.bat](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr_run_iverilog.bat) | 在 Windows 下用 iverilog 编译并运行上面这个 testbench 的脚本 | 否 |
| [SIM/tb_svpwm.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v) | 测试直角转极坐标 `cartesian2polar` 和 SVPWM 调制器 `svpwm` 的 testbench | 否（仅仿真） |
| [SIM/tb_svpwm_run_iverilog.bat](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm_run_iverilog.bat) | 用 iverilog 编译并运行上面这个 testbench 的脚本 | 否 |

这两个 testbench 在仿真时还会把 `RTL/foc/` 下的真实算法模块**一起编译进来**作为被测对象，包括：

- `RTL/foc/sincos.v`：计算正弦/余弦。两个 testbench 都"借用"它来产生正弦波激励（当作信号源用）。
- `RTL/foc/clark_tr.v`、`RTL/foc/park_tr.v`：Clark/Park 变换（tb_clark_park_tr 的被测对象）。
- `RTL/foc/cartesian2polar.v`、`RTL/foc/svpwm.v`：直角转极坐标、SVPWM（tb_svpwm 的被测对象）。

> 一个关键区分：`sincos` 在真实 FOC 设计里是**被 `park_tr` 调用**来算 sinψ/cosψ 的；而在 testbench 里，它被"挪用"成了一台正弦波信号发生器，纯粹是为了给被测模块喂激励。源码里有注释专门提醒这一点，详见 [4.2.3](#423-源码精读)。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先讲**仿真工具链**（怎么把 sim 跑起来），再分别精读**两个 testbench**（它们喂了什么激励、应该看到什么波形）。

### 4.1 仿真工具链：iverilog + vvp + gtkwave

#### 4.1.1 概念说明

iverilog（Icarus Verilog）是一个开源、免费的 Verilog 仿真器。它把"编译"和"运行"分成两步，这一点和 C 语言用 `gcc` 编译、再运行可执行文件非常像：

| 阶段 | 工具 | 类比 C 语言 | 产出 |
| --- | --- | --- | --- |
| 编译 | `iverilog` | `gcc` | 一个可被仿真的中间文件（本项目命名为 `sim.out`） |
| 运行 | `vvp` | 运行 `./a.out` | 执行仿真，按 testbench 里的 `$dumpvars` 指示写出 `dump.vcd` |
| 看波形 | `gtkwave` | —— | 打开 `dump.vcd`，画曲线 |

为什么要分成两步？因为编译只需要做一次（语法检查、把多个 `.v` 文件连起来），而运行才真正模拟时间流逝。分开后，你可以反复运行、反复看波形，而不必每次重新编译。

> 名词小贴士：`-g2001` 这个编译选项告诉 iverilog "按 Verilog-2001 标准（IEEE1364-2001）来解析代码"。本项目所有 `.v` 文件头部都写着 `Standard: Verilog 2001`，所以仿真和综合用的是同一套语法标准。这一点很重要：它能保证"仿真看到的行为"和"烧进 FPGA 后的真实行为"一致。

#### 4.1.2 核心流程

把一个 testbench 跑起来并看到波形，标准三步：

1. **编译**：用 `iverilog -g2001 -o sim.out <testbench> <被测模块...>`，把 testbench 和它用到的所有真实模块一起编译成 `sim.out`。
2. **运行**：用 `vvp -n sim.out` 执行仿真。testbench 里的 `$dumpvars` 会把信号变化写进 `dump.vcd`；testbench 里的 `$finish` 会让仿真在某个时刻结束。`-n` 表示"跑完就退出，不要进入交互模式"。
3. **看波形**：用 `gtkwave dump.vcd` 打开波形文件，把感兴趣的信号加进去，并把 `signed` 信号调成有符号模拟显示。

用文字流程图表示：

```
  .bat 脚本                iverilog                vvp                   gtkwave
  (一键串起来)      ───▶  编译 .v 文件  ───▶  运行 sim.out    ───▶  打开 dump.vcd
                          生成 sim.out          生成 dump.vcd           画波形
```

#### 4.1.3 源码精读

这两个 `.bat` 就是把上面三步的前两步写成了 Windows 批处理脚本，双击即可运行。来看 clark/park 的脚本：

[SIM/tb_clark_park_tr_run_iverilog.bat:1-4](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr_run_iverilog.bat#L1-L4) —— 删旧文件 → 编译 → 运行 → 删中间文件：

```verilog
del sim.out dump.vcd
iverilog  -g2001  -o sim.out  tb_clark_park_tr.v  ../RTL/foc/sincos.v  ../RTL/foc/clark_tr.v  ../RTL/foc/park_tr.v
vvp -n sim.out
del sim.out
```

逐行说明：

- 第 1 行：删掉上一次运行残留的 `sim.out` 和 `dump.vcd`，保证干净。
- 第 2 行（核心）：编译。注意它把**一个 testbench + 三个真实模块**一起喂给 iverilog。这四个文件缺一不可——testbench 例化了 `clark_tr`、`park_tr`，而 `park_tr` 内部又例化了 `sincos`，所以这三个 RTL 文件都得参与编译，否则会出现"模块未定义"错误。`../RTL/foc/` 是相对路径，意味着**这个脚本必须在 `SIM/` 目录里执行**。
- 第 3 行：运行 `sim.out`，`-n` 让它跑完即退出。
- 第 4 行：仿真结束后 `sim.out` 已经没用了，删掉它（`dump.vcd` 要保留，因为那是结果）。

svpwm 的脚本结构完全一样，只是被测模块换成了 `cartesian2polar` 和 `svpwm`：

[SIM/tb_svpwm_run_iverilog.bat:1-4](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm_run_iverilog.bat#L1-L4)：

```verilog
del sim.out dump.vcd
iverilog  -g2001  -o sim.out  tb_svpwm.v  ../RTL/foc/sincos.v  ../RTL/foc/cartesian2polar.v  ../RTL/foc/svpwm.v
vvp -n sim.out
del sim.out
```

> 如果你在 Linux/macOS 上，没有 `.bat`，可以手动敲等价命令（**必须先 `cd` 进 `SIM/` 目录**，因为路径是相对的）：
>
> ```bash
> cd SIM
> rm -f sim.out dump.vcd
> iverilog -g2001 -o sim.out tb_clark_park_tr.v ../RTL/foc/sincos.v ../RTL/foc/clark_tr.v ../RTL/foc/park_tr.v
> vvp -n sim.out
> gtkwave dump.vcd &      # 打开波形
> ```
>
> （`del` 换成 `rm -f`；末尾的 `&` 让 gtkwave 在后台运行，不阻塞终端。）

#### 4.1.4 代码实践

**实践目标**：在本机把仿真工具链跑通，确认能生成 `dump.vcd`。

**操作步骤**：

1. 安装 iverilog 和 gtkwave（README 给出了参考链接 [iverilog_usage](https://github.com/WangXuan95/WangXuan95/blob/main/iverilog_usage/iverilog_usage.md)）。Linux 下一般是 `sudo apt install iverilog gtkwave`。
2. 进入 `SIM/` 目录。
3. 双击 `tb_clark_park_tr_run_iverilog.bat`（Windows），或执行上面给出的等价 Linux 命令。

**需要观察的现象**：

- 终端会先打印 iverilog 的编译信息（若有语法错会在这里报错）。
- 然后 `vvp` 执行，控制台**不应**出现 `unknown module` 之类的错误。
- 运行结束后，`SIM/` 目录下应出现一个非空的 `dump.vcd` 文件。

**预期结果**：`dump.vcd` 成功生成（文件大小不为 0）。若报 `sincos/clark_tr/park_tr` 未定义，说明编译时漏带了对应 RTL 文件或没在 `SIM/` 目录下执行。

> 本讲所有"运行结果"均为**待本地验证**：实际能否跑通取决于你的 iverilog 是否安装正确。本讲不会假装已经替你运行过。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `.bat` 里 `iverilog` 命令必须同时列出 testbench 和三个 `RTL/foc/*.v` 文件，少列一个会怎样？

**参考答案**：因为 testbench 例化了这些模块（例如 `park_tr` 内部例化 `sincos`），Verilog 编译需要把所有被例化的模块定义都找齐。少列一个，iverilog 会报"模块未定义"（`unknown module`），`sim.out` 无法生成。

**练习 2**：把 `.bat` 里的 `vvp -n sim.out` 改成 `vvp sim.out`（去掉 `-n`），运行表现会有什么不同？

**参考答案**：`-n` 表示运行结束后立即退出、不进交互模式。去掉后，vvp 在仿真结束后会进入一个命令行交互界面等待你输入命令（仿真已经结束、波形也已写出，所以对结果没影响，只是不会自动退出，需要手动 quit）。

---

### 4.2 testbench 一：tb_clark_park_tr（Clark 与 Park 变换）

#### 4.2.1 概念说明

这个 testbench 验证 FOC 里的两步坐标变换：

- **Clark 变换**：把三相电流 \(I_a, I_b, I_c\) 变换成两相正交电流 \(I_\alpha, I_\beta\)。直观地说，三相电是三条相位差 \(2\pi/3\) 的正弦波；Clark 变换把它们"压缩"成两条相位差 \(\pi/2\) 的正弦波。
- **Park 变换**：再把 \(I_\alpha, I_\beta\) 旋转到跟着转子转的坐标系，得到 \(I_d, I_q\)。如果旋转角度取得合适，原本旋转的正弦波会"坍缩"成**接近恒定的直流值**。

> 现在你只需要建立这个直觉：**三相正弦波 →（Clark）→ 两条正交正弦波 →（Park）→ 接近常数**。这正是本 testbench 要在波形里验证的现象。变换的数学推导留给 [u2-l3](./u2-l3-clark-transform.md) 和 [u2-l4](./u2-l4-park-and-sincos.md)。

#### 4.2.2 核心流程

`tb_clark_park_tr` 做了四件事：

1. 产生一个 **36.864 MHz** 的时钟 `clk` 和一个上电复位 `rstn`。
2. 用一个不断递增的角度 `theta`（0→4095 循环，代表 \(0 \to 2\pi\)），通过三个 `sincos` 实例"合成"出三相正弦波 `ia/ib/ic`（相位各自差 \(2\pi/3\)）。
3. 把 `ia/ib/ic` 喂给 `clark_tr`，得到 `ialpha/ibeta`；再把 `ialpha/ibeta` 喂给 `park_tr`，得到 `id/iq`。
4. 调用 `$dumpvars` 把全部信号录进 `dump.vcd`，循环 1000 次后 `$finish` 结束。

数据流示意：

```
   theta ──┬──► sincos(θ+4π/3) ──► ia ─┐
           ├──► sincos(θ+2π/3) ──► ib ─┼──► clark_tr ──► (ialpha, ibeta) ──► park_tr ──► (id, iq)
           └──► sincos(θ)      ──► ic ─┘
```

#### 4.2.3 源码精读

**(a) dump 开关与时钟复位**

[SIM/tb_clark_park_tr.v:12](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L12) —— 一上来就开 dump：

```verilog
initial $dumpvars(1, tb_clark_park_tr);
```

`$dumpvars(1, tb_clark_park_tr)` 的意思是：把 `tb_clark_park_tr` 这个作用域里的信号（深度 1 层）录进 VCD。这正是 `dump.vcd` 的来源。

[SIM/tb_clark_park_tr.v:15-18](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L15-L18) —— 时钟与复位：

```verilog
reg rstn = 1'b0;
reg clk  = 1'b1;
always #(13563) clk = ~clk;   // 36.864MHz
initial begin repeat(4) @(posedge clk); rstn<=1'b1; end
```

`always #(13563) clk = ~clk;` 让 `clk` 每 13563 个时间单位翻转一次（代码注释说这对应 36.864 MHz，与真实 FPGA 主时钟一致）。`rstn` 上电为 0，等 4 个时钟上升沿后再拉高——这模拟了"上电复位一段时间后开始正常工作"。

> 注意：testbench 里**没有**写 `timescale 指令，所以这里的"时间单位"取仿真器默认值。对波形观察而言，真正重要的是相对的时钟周期（半个周期 = 13563 单位），而**不是**绝对时间——逻辑波形关系不受影响。

**(b) 用 sincos 合成三相正弦波**

[SIM/tb_clark_park_tr.v:20-24](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L20-L24) —— 定义递增角度 `theta` 和两个相位常数：

```verilog
reg         [11:0] theta = 0;       // 当前电角度（简记为 ψ）。取值范围0~4095。0对应0°；1024对应90°；2048对应180°；3072对应270°。
localparam  [11:0] PI_M2_D3 = (2*4096/3);     // (2/3)*π
localparam  [11:0] PI_D3    = (  4096/3);     // (1/3)*π
```

这里的角度约定很关键：**整个一圈 \(2\pi\) 被映射到 0~4095**（所以 1024 = 90°，2048 = 180°）。于是 \(2\pi/3 \approx 1365\)，\(4\pi/3 \approx 2730\)，正好就是 `PI_D3` 和 `PI_M2_D3`。

[SIM/tb_clark_park_tr.v:36-65](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L36-L65) —— 三个 `sincos` 实例分别生成 `ia/ib/ic`：

```verilog
// ia ：输入 θ + (2/3)*π  →  初相位 (4/3)*π 的正弦波
sincos u1_sincos ( ... .i_theta ( theta + PI_M2_D3 ), .o_sin ( ia ) ... );
// ib ：输入 θ + (1/3)*π  →  初相位 (2/3)*π 的正弦波
sincos u2_sincos ( ... .i_theta ( theta + PI_D3    ), .o_sin ( ib ) ... );
// ic ：输入 θ             →  初相位 0 的正弦波
sincos u3_sincos ( ... .i_theta ( theta            ), .o_sin ( ic ) ... );
```

把同一个递增的 `theta` 加上不同的相位偏移，就得到了三条相位依次差 \(2\pi/3\) 的正弦波——这就是一组标准的三相电流。注意第 36 行的注释特别强调：**这里只是借用 `sincos` 当信号源**，真实设计里 `sincos` 并不喂给 `clark_tr`，而是被 `park_tr` 调用。

**(c) 被测模块：clark_tr 与 park_tr**

[SIM/tb_clark_park_tr.v:68-78](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L68-L78) —— 例化 `clark_tr`。注意输入做了 `/ 16'sd2`（除以 2），把振幅从 ±16384 缩小到 ±8192，正好落在 `clark_tr` 规定的输入范围 \(-8191\sim 8191\) 内（见 [RTL/foc/clark_tr.v:13](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L13)）：

```verilog
clark_tr u_clark_tr (
    .i_ia ( ia / 16'sd2 ),   // 振幅 ±8192，初相位 (4/3)*π
    .i_ib ( ib / 16'sd2 ),   // 振幅 ±8192，初相位 (2/3)*π
    .i_ic ( ic / 16'sd2 ),   // 振幅 ±8192，初相位 0
    .o_ialpha ( ialpha ),    // 应为初相位 (4/3)*π 的正弦波
    .o_ibeta  ( ibeta )      // 相位应比 Iα 滞后 π/2（即与 Iα 正交）
);
```

[SIM/tb_clark_park_tr.v:81-91](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L81-L91) —— 例化 `park_tr`，旋转角 `psi = theta + 512`（512 对应 \(\pi/4\)）：

```verilog
park_tr u_park_tr (
    .psi      ( theta + 12'd512 ),   // θ + (1/4)*π
    .i_ialpha ( ialpha ),
    .i_ibeta  ( ibeta  ),
    .o_id     ( id ),                // 应变为一个定值
    .o_iq     ( iq )                 // 应变为一个定值
);
```

注释说 `id/iq` 应"变为一个定值"——这就是 Park 变换把旋转量"拉直"成直流的直观效果。

**(d) 主循环：让 theta 递增**

[SIM/tb_clark_park_tr.v:96-106](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L96-L106)：

```verilog
initial begin
    while(~rstn) @ (posedge clk);
    for (i=0; i<1000; i=i+1) @ (posedge clk) begin
        en_theta <= 1'b1;
        theta <= theta + 12'd10;
        @ (posedge clk);
        en_theta <= 1'b0;
        repeat (9) @ (posedge clk);
    end
    $finish;
end
```

逻辑：先等复位释放；然后循环 1000 次，每次把 `theta` 加 10、给 `sincos` 一个单周期使能脉冲 `en_theta`，再空等 9 拍。`theta` 每次加 10，1000 次共加 10000，超过一圈（4096）两圈多，所以波形里能看到正弦波**反复走过多个周期**，足够看清相位关系。最后 `$finish` 结束仿真。

#### 4.2.4 代码实践

**实践目标**：通过波形验证 Clark 变换的"正交性"——即 \(I_\alpha\) 与 \(I_\beta\) 相位应相差 \(\pi/2\)。

**操作步骤**：

1. 按 [4.1.4](#414-代码实践) 跑出 `dump.vcd`，用 `gtkwave dump.vcd` 打开。
2. 在左侧信号树里把 `ia, ib, ic, ialpha, ibeta` 这 5 个信号加到右侧波形区。
3. **关键**：依次对每个 `signed` 信号右键 → `Data Format` → `Signed Decimal`（否则负半周会显示成大正数）。
4. 再对每个信号右键 → `Data Format` → `Analog` → `Step`，把它们变成模拟曲线。

**需要观察的现象**：

- `ia/ib/ic` 是三条相位依次差 \(2\pi/3\)（即 120°）的正弦波。
- `ialpha` 是一条正弦波；`ibeta` 也是一条正弦波，且**相位比 `ialpha` 落后（或超前）\(\pi/2\)（90°）**，二者构成一对正交信号。

**预期结果**：`ialpha` 与 `ibeta` 在波形上呈 90° 相位差的正弦曲线（README 的"图4"即是参考画面）。如果你看到 `ibeta` 在 `ialpha` 过零点时恰好达到峰值，就验证了正交关系。

#### 4.2.5 小练习与答案

**练习 1**：如果把主循环里 `theta <= theta + 12'd10` 改成 `theta <= theta + 12'd20`（步长翻倍），波形会发生什么变化？

**参考答案**：`theta` 递增更快，意味着正弦波的"电角度"跑得更快，所以 `ia/ib/ic/ialpha/ibeta` 的频率都会**变高**（波形更密）。但相位关系（\(2\pi/3\)、\(\pi/2\)）不会变，因为那是由 `PI_D3` 等常数决定的，与步长无关。

**练习 2**：为什么 `clark_tr` 的输入要写成 `ia / 16'sd2`，而不是直接用 `ia`？

**参考答案**：`sincos` 输出的振幅是 ±16384，而 `clark_tr` 规定的输入范围是 \(-8191\sim 8191\)（见 `clark_tr.v` 第 13 行注释）。`/2` 把振幅缩到 ±8192，避免输入超范围导致 `clark_tr` 内部的乘 2、求和等运算溢出，从而保证仿真波形正确反映算法行为。

---

### 4.3 testbench 二：tb_svpwm（cartesian2polar 与 SVPWM）

#### 4.3.1 概念说明

这个 testbench 验证 FOC 末尾两步：

- **cartesian2polar（直角转极坐标）**：把直角坐标 \((x, y)\) 转成极坐标 \((\rho, \varphi)\)，即"幅值 + 角度"。例如把一组旋转的 \((\cos\theta, \sin\theta)\) 输入，应得到幅值恒定、角度随 \(\theta\) 变化的输出。
- **SVPWM（空间矢量脉宽调制）**：把极坐标电压矢量 \((\rho, \varphi)\) 转换成三路 PWM 的**占空比**（duty）。七段式 SVPWM 的三路占空比在波形上呈现典型的**马鞍波（saddle wave）**形状。

> 直觉：\((x,y)\) 旋转矢量 →（直角转极坐标）→ 恒定幅值 + 线性增长的角度 →（SVPWM）→ 三路马鞍形占空比。SVPWM 的原理留给 [u2-l7](./u2-l7-svpwm.md)，本讲只关心"在波形里能不能看到马鞍波"。

#### 4.3.2 核心流程

`tb_svpwm` 的结构与 `tb_clark_park_tr` 几乎一样，但有两处关键不同：

1. **写了两次 `$dumpvars`**：除了 dump 顶层 `tb_svpwm`，还专门 dump 了 `svpwm` 这个实例 `u_svpwm`，因为 `pwma_duty` 等占空比信号**在 svpwm 模块内部**，不在顶层。
2. 主循环每次让 `theta += 25`，并 `repeat(2048)` 等满**一个完整 PWM 周期**（2048 个时钟），好让 SVPWM 完整地生成一周期波形。

数据流示意：

```
   theta ──► sincos ──► (x=cosθ, y=sinθ)
                            │  /5
                            ▼
                     cartesian2polar ──► (rho=ρ, phi=φ)
                                            │
                                            ▼  (+v_amp=384)
                                         svpwm ──► pwm_a, pwm_b, pwm_c   (内部还有 pwma_duty 等马鞍波)
```

#### 4.3.3 源码精读

**(a) 为什么要两次 `$dumpvars`**

[SIM/tb_svpwm.v:12-13](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L12-L13)：

```verilog
initial $dumpvars(1, tb_swpwm);
initial $dumpvars(1, u_svpwm);
```

第一行 dump 顶层 testbench 的信号（`theta/x/y/rho/phi/pwm_a/...`）。第二行专门 dump `u_svpwm` 这个 `svpwm` 模块实例——因为 `pwma_duty, pwmb_duty, pwmc_duty` 这三个关键的"马鞍波占空比"信号是 `svpwm` 内部的 `reg`（见 [RTL/foc/svpwm.v:34](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L34)），不写第二行就在波形里找不到它们。README 也专门提醒："pwma_duty、pwmb_duty、pwmc_duty 这三个信号不在顶层，你要在 svpwm 这个模块内才能找到"。

> 这是一个很有用的技巧：**想看某个子模块内部的信号，就对这个实例再写一句 `$dumpvars`。**

**(b) 用 sincos 生成正交的 (x, y)**

[SIM/tb_svpwm.v:33-41](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L33-L41)：

```verilog
sincos u_sincos (
    .i_en    ( 1'b1   ),
    .i_theta ( theta  ),   // 一个递增的角度值
    .o_sin   ( y      ),   // y = sinθ，振幅 ±16384
    .o_cos   ( x      )    // x = cosθ，振幅 ±16384
);
```

注意这里 `i_en` 恒为 1（持续使能），同时取了 `o_sin` 和 `o_cos`，得到一对正交的 \((x, y)=(\cos\theta, \sin\theta)\)。

**(c) 被测模块：cartesian2polar 与 svpwm**

[SIM/tb_svpwm.v:43-52](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L43-L52)：

```verilog
cartesian2polar u_cartesian2polar (
    .i_x    ( x / 16'sd5 ),   // 振幅 ±3277 的余弦波
    .i_y    ( y / 16'sd5 ),   // 振幅 ±3277 的正弦波
    .o_rho  ( rho ),          // ρ，应该一直等于或近似 3277
    .o_theta( phi )           // φ，应该是接近 θ 的角度值
);
```

输入除以 5，把振幅从 ±16384 缩到 ±3277。因为 \((\cos\theta, \sin\theta)\) 在直角坐标里到原点的距离恒为 1（缩放后恒为 3277），所以 `rho` 应近似恒等于 3277，`phi` 应近似等于 `theta`——这正是直角转极坐标应有的结果。

[SIM/tb_svpwm.v:54-64](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L54-L64)：

```verilog
svpwm u_svpwm (
    .v_amp   ( 9'd384 ),   // svpwm 的最大电压矢量幅值
    .v_rho   ( rho  ),     // 输入 ρ
    .v_theta ( phi  ),     // 输入 φ
    .pwm_en  ( pwm_en ),
    .pwm_a   ( pwm_a ),
    .pwm_b   ( pwm_b ),
    .pwm_c   ( pwm_c )
);
```

`v_amp=384` 是 SVPWM 的最大电压矢量幅值参数；`rho/phi` 是上一步算出的极坐标电压矢量。`svpwm` 内部据此算出 `pwma_duty/pwmb_duty/pwmc_duty`（马鞍波），再生成数字 PWM 输出。模块头部说明 PWM 频率 \(= \mathrm{clk}/2048\)，36.864 MHz 时即 18 kHz（见 [RTL/foc/svpwm.v:10](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L10)）。

**(d) 主循环：每 2048 拍推进一个 PWM 周期**

[SIM/tb_svpwm.v:69-77](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L69-L77)：

```verilog
initial begin
    while(~rstn) @ (posedge clk);
    for(i=0; i<200; i=i+1) begin
        theta <= 25 * i;               // 让 θ 递增
        repeat(2048) @ (posedge clk);  // 等满一个 PWM 周期
        $display("%d/200", i);
    end
    $finish;
end
```

`repeat(2048)` 故意等满一个 PWM 周期（2048 个时钟），这样每次 `theta` 变化后，`svpwm` 都有充足时间走完它内部 `cnt` 计数器的一个完整循环、重新生成一组占空比。循环 200 次，`theta` 从 0 走到 `25×199=4975`，超过一圈（4096），覆盖了所有角度，足够画出完整马鞍波。`$display` 只是把进度打到终端，方便你看仿真跑到哪了。

#### 4.3.4 代码实践

**实践目标**：在波形里看到 SVPWM 的三路**马鞍波占空比**，并理解它如何决定 PWM 的占空比。

**操作步骤**：

1. 跑 `tb_svpwm_run_iverilog.bat`（或等价 Linux 命令）生成 `dump.vcd`，用 gtkwave 打开。
2. 在信号树里展开 `u_svpwm` 实例，找到内部的 `pwma_duty, pwmb_duty, pwmc_duty`（它们是 `signed`/有符号相关，按需调成 `Signed Decimal` → `Analog` → `Step`）。
3. 同时把顶层的 `pwm_a, pwm_b, pwm_c`（数字信号，保持 0/1 显示）加进来。
4. 放大到一两个 PWM 周期观察。

**需要观察的现象**：

- `pwma_duty/pwmb_duty/pwmc_duty` 呈现典型的**马鞍波**（一段平、两段斜的形状），三条之间有相位差。
- 在单个 PWM 周期内放大，能看到 `pwm_a` 为高的时间段宽度与 `pwma_duty` 的数值对应：**`duty` 越大，`pwm_a` 高电平占空比越大**。

**预期结果**：三路占空比为马鞍波；数字 PWM 的高电平宽度随对应 `duty` 增大而增大（README"图5/图6"即参考画面）。

#### 4.3.5 小练习与答案

**练习 1**：如果不写第二句 `initial $dumpvars(1, u_svpwm);`，在 gtkwave 里会少看到哪些信号？为什么？

**参考答案**：会少看到 `pwma_duty, pwmb_duty, pwmc_duty`（以及 `cnt, pwm_act` 等 svpwm 内部信号）。因为第一句 `$dumpvars(1, tb_swpwm)` 只录了顶层作用域的信号，而 duty 信号是 `svpwm` 模块内部的 `reg`，必须单独对 `u_svpwm` 实例再 dump 一次才会被写进 VCD。

**练习 2**：主循环里 `repeat(2048)` 为什么是 2048，而不是随便一个数？

**参考答案**：因为 SVPWM 的一个完整 PWM 周期正好是 2048 个时钟周期（PWM 频率 \(=\mathrm{clk}/2048\)）。等满 2048 拍，才能让 `svpwm` 内部的 `cnt` 计数器走完一整圈、重新算出并锁存好一组新的占空比，看到完整的波形周期。等太少会看到不完整的周期，等太多则浪费时间但波形不变。

---

## 5. 综合实践

把本讲学的"跑仿真 + 看波形"完整地走一遍，验证 Clark 变换的正交性。

**任务**：运行 `tb_clark_park_tr` 仿真，在 gtkwave 中把 `ia/ib/ic/ialpha/ibeta` 都设成有符号模拟显示，截图，并验证 \(I_\alpha\) 与 \(I_\beta\) 相位相差 \(\pi/2\)。

**操作步骤**：

1. 安装 iverilog 与 gtkwave（参考 [iverilog_usage](https://github.com/WangXuan95/WangXuan95/blob/main/iverilog_usage/iverilog_usage.md)）。
2. 进入 `SIM/` 目录，运行 `tb_clark_park_tr_run_iverilog.bat`（Windows），或执行等价 Linux 命令：
   ```bash
   cd SIM
   rm -f sim.out dump.vcd
   iverilog -g2001 -o sim.out tb_clark_park_tr.v ../RTL/foc/sincos.v ../RTL/foc/clark_tr.v ../RTL/foc/park_tr.v
   vvp -n sim.out
   gtkwave dump.vcd &
   ```
3. 在 gtkwave 左侧把 `ia, ib, ic, ialpha, ibeta` 加入波形区。
4. 对这 5 个 `signed` 信号依次：右键 → `Data Format` → `Signed Decimal`，再 右键 → `Data Format` → `Analog` → `Step`。
5. 找一个 `ialpha` 过零点的时刻，观察此刻 `ibeta` 是否正好在峰值（或谷值）。

**需要观察的现象**：

- `ia, ib, ic` 是三条相位依次差 \(2\pi/3\) 的正弦波。
- `ialpha, ibeta` 是两条相位差 \(\pi/2\) 的正弦波：一个过零时另一个到极值。

**预期结果**：截图能清楚显示 `ialpha` 与 `ibeta` 的 90° 相位差，与 README"图4"一致。

> 若结果与本讲描述或 README 图示不符：先检查是否漏了"`Signed Decimal`"这一步（最常见错误，会导致负半周显示成大正数，曲线变形）；再检查编译时是否漏带了 `sincos.v`（`park_tr` 依赖它）。本讲不假装已替你运行，结果**待本地验证**。

## 6. 本讲小结

- 本项目用 **iverilog 编译 + vvp 运行 + gtkwave 看波形** 的三件套做仿真；两个 `.bat` 脚本就是前两步的封装，必须**在 `SIM/` 目录**执行（路径是相对的）。
- `iverilog -g2001` 指定 Verilog-2001 标准，编译时必须把 testbench 和它例化的**所有** RTL 模块一起喂进去；`vvp -n` 跑完即退出。
- `$dumpvars(1, <作用域>)` 决定了哪些信号会被写进 `dump.vcd`；想看子模块内部信号（如 `pwma_duty`），要对那个实例**再写一句** `$dumpvars`。
- 在 gtkwave 里看正弦/马鞍波，必须先把 `signed` 信号设成 **Signed Decimal**，再设成 **Analog → Step**，否则负半周会变形。
- `tb_clark_park_tr` 借用 `sincos` 当信号源合成三相正弦波，验证 Clark（得到正交的 \(\alpha\beta\)）和 Park（得到接近直流的 \(dq\)）；`tb_svpwm` 验证 cartesian2polar（恒定 \(\rho\)）和 SVPWM（三路马鞍波占空比）。
- **本项目只仿真了这些子模块、没有仿真整个 FOC**，是因为作者没有电机的 Verilog 模型，无法对完整闭环做仿真（见 [README.md:466](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L466)）。

## 7. 下一步学习建议

入门篇到此结束，你已经能"把项目跑起来、看懂波形"。接下来进入**第 2 单元（FOC 核心数据流）**，把本讲在波形里"看到"的几个变换，从源码层面逐行读懂：

- [u2-l1 foc_top 全景与控制环路](./u2-l1-foc-top-overview.md)：先俯瞰整个电流环的数据流。
- [u2-l3 Clark 变换](./u2-l3-clark-transform.md)：搞清本讲里 `clark_tr` 那几行移位加法为什么能近似 \(\sqrt{3}\)。
- [u2-l4 Park 变换与 sincos](./u2-l4-park-and-sincos.md)：理解 `park_tr` 的乘加和 `sincos` 的查表状态机——本讲里"借用"的那个模块，在这里讲清它的本职工作。
- [u2-l7 SVPWM 调制器](./u2-l7-svpwm.md)：搞清本讲看到的马鞍波 `pwma_duty` 是怎么按七段式算法算出来的。

如果想进一步练习仿真技能，可以参考 [u4-l3 仿真方法论与波形解读](./u4-l3-simulation-methodology.md)，那里会教你如何为 `pi_controller` 之类的新模块**自己写 testbench**。
