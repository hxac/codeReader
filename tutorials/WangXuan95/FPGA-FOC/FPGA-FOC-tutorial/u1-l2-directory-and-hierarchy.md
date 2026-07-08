# 目录结构与模块层次

## 1. 本讲目标

上一篇（u1-l1）我们已经建立了 FPGA-FOC 的系统全景图：知道了它是一个用 FPGA 实现的电机 FOC 电流环，并把系统分成了粉色（传感器控制器）、蓝色（FOC 固定算法）、黄色（用户逻辑）、淡橙色（FPGA 外部硬件）四个功能块。

本讲的目标是**把那张框图落到真实文件上**。读完本讲你应该能够：

1. 说出 `RTL/` 与 `SIM/` 两个目录的分工，以及 `RTL/` 与 `RTL/foc/` 这两级子目录分别装什么。
2. 把仓库里的 12 个 `.v` 源文件**逐一对应**到系统框图的粉色 / 蓝色 / 黄色三个 FPGA 内部区域。
3. 区分清楚哪些是 README 里标注的「固定功能，一般不需要改动」的核心模块，哪些是可以替换 / 移除的外设与用户逻辑。
4. 看懂顶层 `fpga_top.v` 如何把四个部分「总装」成一条数据通路，以及 `foc_top.v` 在蓝色区域内部又是如何用更小的子模块搭出来的。
5. 理解「平台无关」在本项目里有两层含义（跨 FPGA 厂商 vs 跨传感器型号），并据此判断移植时要改哪些文件。

---

## 2. 前置知识

本讲不展开 FOC 的数学（那是第 2 单元的事），但需要你具备下面这点 Verilog 常识。如果你已经熟，可跳过。

- **模块（module）与例化（instantiation）**：Verilog 用 `module ... endmodule` 描述一个电路块。一个模块可以在另一个模块里被「例化」使用，就像 C 语言里调用函数，只不过例化描述的是**硬件包含关系**——大电路里放进一个小电路。例如：

  ```verilog
  foc_top u_foc_top ( .clk(clk), .phi(phi), ... );  // 在 fpga_top 里例化了一个 foc_top
  ```

- **顶层模块（top module）**：FPGA 工程里最外层、不被任何模块包含的那个模块，它的端口就是 FPGA 芯片真实的物理引脚。本工程的顶层是 `fpga_top`。
- **模块层次（hierarchy）**：例化关系会形成一棵树。顶层在树根，被它例化的模块是子节点，子节点还可以继续例化更小的模块。本讲要理清的就是这棵树。
- **可综合（synthesizable）vs 仿真（testbench）**：`RTL/` 里的代码是可以综合成真实电路的；`SIM/` 里的 testbench 只用于仿真验证，不会被烧进 FPGA。

承接 u1-l1 已经建立的术语：机械角度 φ、电角度 ψ、极对数 N、d/q 轴、Clark/Park 变换、SVPWM、三相 PWM——这些在本讲只作为「某个模块的名字由来」出现，不重新解释。

---

## 3. 本讲源码地图

本讲只读三个文件，但它们足以撑起整棵模块树：

| 文件 | 在本讲的作用 |
| :-- | :-- |
| `README.md` | 给出 12 个源文件的功能表、颜色分区说明，以及「固定功能」标注——本讲的「标准答案」几乎都在这里。 |
| `RTL/fpga_top.v` | 工程顶层。例化了传感器控制器、ADC、FOC 核心、UART，并写了演示用的用户逻辑。 |
| `RTL/foc/foc_top.v` | 蓝色区域的顶层。内部例化了 clark/park/pi/cartesian2polar/svpwm 等子模块。 |

---

## 4. 核心概念与源码讲解

### 4.1 仓库的目录布局

#### 4.1.1 概念说明

一个 FPGA 工程通常分两类文件：**能综合进硬件的 RTL 源码** 和 **只在电脑上仿真用的 testbench**。本项目用两个顶层目录把它们干净地隔开：

- `RTL/`（Register Transfer Level）：综合可用的 Verilog 源码，是要烧进 FPGA 的东西。
- `SIM/`（Simulation）：仿真用文件，包括 testbench 和一键运行脚本，**不**加入 FPGA 工程。

而在 `RTL/` 内部又分两级，对应框图里「硬件相关 vs 硬件无关」的分界：

- `RTL/` 根目录：放顶层、外设控制器、用户逻辑——这些和具体硬件型号或 FPGA 引脚有关。
- `RTL/foc/` 子目录：放 FOC 核心算法——纯数学，和任何硬件型号都无关。

这个「核心算法单独抽一个子目录」的写法不是随手分的，它就是框图里**蓝色区域**的物理体现，也是整个库最值得复用的部分。

#### 4.1.2 核心流程：仓库目录树

把仓库的实际文件画成树（共 12 个 `.v` 源文件 + 4 个仿真文件）：

```
FPGA-FOC/
├── README.md                       # 项目文档（功能表、调参、FAQ）
├── LICENSE
├── figures/                        # 文档配图（系统框图、原理图、波形截图）
├── gerber_pcb_foc_shield.zip       # 电机驱动板 PCB 制造文件
│
├── RTL/                            # ===== 综合可用的源码（工程主体）=====
│   ├── fpga_top.v                  # 顶层模块（总装 + 黄色用户逻辑）
│   ├── i2c_register_read.v         # 粉色：I2C 读 AS5600 磁编码器
│   ├── adc_ad7928.v                # 粉色：SPI 读 AD7928 ADC
│   ├── uart_monitor.v              # 黄色：UART 监视器（可移除）
│   └── foc/                        # ----- 蓝色：FOC 核心算法（硬件无关）-----
│       ├── foc_top.v               #   蓝色区域顶层
│       ├── clark_tr.v              #   Clark 变换
│       ├── park_tr.v               #   Park 变换
│       ├── sincos.v                #   sin/cos 计算器（被 park_tr、svpwm 调用）
│       ├── pi_controller.v         #   PI 控制器
│       ├── cartesian2polar.v       #   直角坐标转极坐标
│       ├── svpwm.v                 #   SVPWM 调制器
│       └── hold_detect.v           #   ADC 采样窗口检测
│
└── SIM/                            # ===== 仿真文件（不烧进 FPGA）=====
    ├── tb_clark_park_tr.v          #   clark/park 的 testbench
    ├── tb_clark_park_tr_run_iverilog.bat
    ├── tb_svpwm.v                  #   cartesian2polar/svpwm 的 testbench
    └── tb_svpwm_run_iverilog.bat
```

数一数：`RTL/` 根目录 4 个文件，`RTL/foc/` 子目录 8 个文件，合计 **12 个 `.v` 源文件**——这正是 README「设计代码详解」那张表里的 12 行（见 [README.md:433-446](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L433-L446)，旁边中文说明每个文件的作用）。

注意一个细节：`RTL/` 根目录和 `RTL/foc/` 并不严格等于「硬件相关 / 硬件无关」。准确的分界是——

- **`RTL/` 根目录**里既有硬件相关的（`i2c_register_read.v`、`adc_ad7928.v`），也有顶层 `fpga_top.v`（含 Altera 专属的 `altpll`）和用户逻辑 `uart_monitor.v`。
- **`RTL/foc/`** 里的 8 个文件则**全部**是硬件无关的核心算法。

所以「`foc/` 子目录 = 蓝色 = 可复用核心」这个等式最干净；根目录里的文件要逐个看。

#### 4.1.3 源码精读：用 README 的表给文件归类

README 用一张表给 12 个文件都标了功能和「备注」，这是归类时最权威的依据：

[README.md:433-446](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L433-L446) —— 中文版的「设计代码详解」文件表，其中 8 个蓝色模块都标注了「固定功能，一般不需要改动」，`uart_monitor.v` 标注「不需要的话可以移除」，两个外设控制器没有任何「固定功能」标注（说明它们是可替换的）。

紧跟着这张表，README 还给出了颜色分区的文字定义（这是本讲「映射」的依据）：

[README.md:453-460](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L453-L460) —— 明确说：粉色是传感器控制器（硬件相关，换型号要重写）；蓝色是 FOC 固定算法（硬件无关，核心代码，一般不改）；黄色是用户自定义逻辑（可改 user behavior，或改 uart_monitor 监测别的变量）；淡橙色是 FPGA 外部硬件（电机、驱动板、传感器）。

把这两段合起来，本讲的归类「标准答案」其实已经齐全。

#### 4.1.4 代码实践：数清楚 12 个文件

**实践目标**：亲手核对仓库的文件清单，确认「12 个源文件」这个数字，并区分哪些进 FPGA、哪些只在电脑上跑。

**操作步骤**：

1. 在仓库根目录用 `git ls-files` 或文件管理器列出所有 `.v` 文件。
2. 把它们分成两堆：路径以 `RTL/` 开头的（综合用）、以 `SIM/` 开头的（仿真用）。
3. 再把 `RTL/` 那一堆分成两组：直接在 `RTL/` 下的、在 `RTL/foc/` 下的。

**需要观察的现象**：`RTL/` 根目录有 4 个文件，`RTL/foc/` 有 8 个文件，`SIM/` 有 4 个文件（其中 2 个是 `.bat` 脚本）。

**预期结果**：综合用源码恰好 12 个，与 README 的功能表行数一致。

> 如果你在自己机器上跑命令结果与本讲不一致（例如将来仓库新增了文件），以你看到的实际结果为准——本讲的数字对应 HEAD = `3816c6c`。

#### 4.1.5 小练习与答案

**练习 1**：`SIM/tb_svpwm_run_iverilog.bat` 不是 `.v` 文件，它是什么？属于哪一类？

**参考答案**：它是一个 Windows 批处理脚本，里面写了用 iverilog 编译 + vvp 运行 testbench 的命令。它属于仿真类（在 `SIM/` 目录），不会综合进 FPGA。

**练习 2**：为什么 `foc/` 要单独成一个子目录，而不是和 `fpga_top.v` 平级放在一起？

**参考答案**：因为 `foc/` 里的 8 个文件是硬件无关的纯算法核心，是整个库最值得复用、最不该动的部分。单独成目录既在物理上隔离了「核心算法」和「硬件相关 / 用户逻辑」，也方便移植时整目录拷贝。它是框图里蓝色区域的直接对应。

---

### 4.2 顶层模块 fpga_top

#### 4.2.1 概念说明

`fpga_top` 是整棵模块树的**根**。它的端口就是 FPGA 芯片对外暴露的真实引脚（晶振、I2C、SPI、PWM、UART）。它的职责只有两个：

1. **总装**：把传感器控制器（粉色）、FOC 核心（蓝色）、UART 监视（黄色）这几部分例化进来，用 `wire` 把它们的端口连成一条完整的数据通路。
2. **承载用户逻辑**（黄色）：演示程序里「电机顺时针 / 逆时针交替运行」的行为，就写在这个文件里几个简单的 `always` 块中。

理解了 `fpga_top`，就理解了框图里 FPGA 内部三个区域是怎么「接上线」的。

#### 4.2.2 核心流程：顶层的数据通路

把 `fpga_top` 内部的连线画成数据流（箭头表示信号流向）：

```
        clk_50m ──► [altpll] ──► clk (36.864MHz), rstn
                                        │ (驱动下面所有模块)

   AS5600 ◄─I2C─► [i2c_register_read] ──phi──┐
                                              ▼
                                         [foc_top] ──► pwm_a/b/c, pwm_en ──► 电机驱动板
                                              ▲                                ▲
   AD7928 ◄─SPI─► [adc_ad7928] ◄──sn_adc──────┤                                │
                       ▲ └─en_adc,adc_a/b/c────┘                                │
                       │                                                        │
                       │      sn_adc/en_adc/adc_* 是 foc_top 与外设之间的握手   │
                       │                                                        │
                  (黄色用户逻辑)                                                 │
                  id_aim=0, iq_aim=±200 ──id_aim/iq_aim──► [foc_top]            │
                                                                              │
                  [uart_monitor] ◄──id,iq,id_aim,iq_aim──┤                     │
                       │                                                     │
                       └──────────────── uart_tx ──────────────────────────────┘ (到电脑串口)
```

关键认知：`foc_top` 是中心枢纽，左侧吃进角度（`phi`）和电流采样（`sn_adc`/`en_adc`/`adc_*`），下侧吃进用户给的目标（`id_aim`/`iq_aim`），上侧吐出 PWM 驱动电机，再把监测量（`id`/`iq`）送给 UART。粉色外设为它服务，黄色逻辑给它下达目标和取走监测量。

#### 4.2.3 源码精读

**端口 = FPGA 引脚**。[RTL/fpga_top.v:11-28](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L11-L28) 定义了顶层的全部对外信号：`clk_50m`、3 路 PWM + `pwm_en`、SPI 4 线、I2C 2 线、`uart_tx`。这些就是 README「引脚约束」一节列出的物理连接。

**内部连线（wire）**。[RTL/fpga_top.v:31-46](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L31-L46) 声明了一堆 `wire`，它们就是上图里连接各模块的「导线」，例如 `phi`（机械角度）、`sn_adc`/`en_adc`/`adc_value_a|b|c`（ADC 握手与结果）、`id`/`iq`/`id_aim`/`iq_aim`（监测量与目标值）。

**总装一：I2C 读角度（粉色）**。[RTL/fpga_top.v:60-73](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L60-L73) 例化了 `i2c_register_read`，配置 `SLAVE_ADDR=0x36`、`REGISTER_ADDR=0x0E` 读 AS5600，把 16 位结果的高 4 位丢进 `i2c_trash`、低 12 位接到 `phi`。注意 `start=1'b1` 表示持续不断地读。

**总装二：SPI 读 ADC（粉色）**。[RTL/fpga_top.v:78-100](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L78-L100) 例化 `adc_ad7928`，用 `CH_CNT=2`+`CH0/1/2` 指定只采 AD7928 的通道 1/2/3（对应 A/B/C 相）。它接收 `i_sn_adc` 脉冲启动采样，结束后在 `o_en_adc` 上回送一个脉冲并同时提交三通道结果。

**总装三：FOC 核心（蓝色）**。[RTL/fpga_top.v:105-132](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L105-L132) 例化 `foc_top`，这里是全库最重要的例化——所有参数（`INIT_CYCLES`/`ANGLE_INV`/`POLE_PAIR`/`MAX_AMP`/`SAMPLE_DELAY`）和 PI 参数（`Kp`/`Ki`）都在这一处设定。注意 `phi`、`sn_adc`、`en_adc`、`adc_a|b|c` 这些端口和上面两个外设的端口是同一组 `wire`，这正是「连线」把三部分接在一起的体现。

**黄色用户逻辑**。[RTL/fpga_top.v:136-154](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L136-L154) 写了演示行为：一个 24 位自增计数器 `cnt`；`id_aim` 恒为 0（`assign id_aim = $signed(16'd0);` 在第 144 行）；用 `cnt[23]` 作为开关让 `iq_aim` 在 +200 与 -200 之间切换，从而让电机顺逆交替。**这就是 README 说的「修改 user behavior 来实现各种电机应用」的落点**——想换成速度环、位置环，就是改这段。

**总装四：UART 监视（黄色）**。[RTL/fpga_top.v:159-170](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L159-L170) 例化 `uart_monitor`，以 `en_idq` 脉冲为节拍，把 `id`/`id_aim`/`iq`/`iq_aim` 四个有符号数以十进制字符串送出。

> 注意：`altpll`（[RTL/fpga_top.v:50-54](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L50-L54)）是 Altera Cyclone IV 专属原语，这也是整个库里**唯一**一处非纯 RTL 代码——它是「`fpga_top.v` 平台相关」的根源，后面 4.4 节会展开。

#### 4.2.4 代码实践：追踪一条顶层连线

**实践目标**：体会「顶层 = 用 wire 把模块连起来」这件事。

**操作步骤**：

1. 打开 `RTL/fpga_top.v`。
2. 找到 `phi` 这个 `wire`（第 34 行声明）。
3. 分别找出 `phi` 在哪两个例化里被用到：作为 `i2c_register_read` 的输出（第 72 行 `.regout({i2c_trash, phi})`）和作为 `foc_top` 的输入（第 116 行 `.phi(phi)`）。

**需要观察的现象**：`phi` 这个名字在顶层文件里既出现在「输出端口」又出现在「输入端口」的连接处，但 `phi` 本身只在第 34 行声明了一次。

**预期结果**：你会看到顶层并不「计算」`phi`，它只是把 `i2c_register_read` 产生的 `phi` 用同一根 `wire` 喂给了 `foc_top`。这就是 RTL 里「模块间连线」的本质。

#### 4.2.5 小练习与答案

**练习 1**：`fpga_top` 里 `id_aim` 是 `wire`，`iq_aim` 是 `reg`（见第 45-46 行声明）。为什么类型不同？

**参考答案**：因为 `id_aim` 用 `assign` 持续赋值为常量 0（组合逻辑，用 `wire`）；而 `iq_aim` 在 `always @(posedge clk)` 块里根据 `cnt[23]` 时序赋值（时序逻辑，必须用 `reg`）。Verilog 里 `reg` 不一定对应寄存器，但 `always` 块内赋值的左值必须是 `reg` 类型。

**练习 2**：如果完全不需要串口监视（只想让电机转），顶层要怎么改？

**参考答案**：把 `uart_monitor` 的例化（[RTL/fpga_top.v:159-170](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L159-L170)）整段删掉或注释掉，并可以删除 `uart_tx` 端口（或让它悬空）。README 也明确说 `uart_monitor.v`「不需要的话可以移除」。这正体现了它属于「可移除的黄色逻辑」。

---

### 4.3 FOC 核心模块 foc_top

#### 4.3.1 概念说明

如果说 `fpga_top` 是「整棵树的根」，那 `foc_top` 就是**蓝色区域这棵子树的根**。它在 `RTL/foc/` 目录里，端口全是抽象的 FOC 量（角度、电流、电压、PWM），完全不涉及 I2C/SPI/UART 这些具体协议——所以它硬件无关、可移植、是「固定功能，一般不需要改动」的核心。

`foc_top` 自己不做太多计算，它的主要工作是**把 FOC 数据流上的 7 个子模块串起来**，再加上两个自己写的 `always` 块（角度换算、电流重构 + 反 Park）。换句话说，它把框图里蓝色区域内部的小方块组织成了一条流水线。

#### 4.3.2 核心流程：蓝色区域内部的流水线

`foc_top` 内部沿电流环数据流自左向右展开（这也是第 2 单元要逐个精读的顺序，本讲只看「谁连谁」）：

```
phi ──►[角度换算 always]──────► psi ──────────────────────────────────────┐
                                                                            │ (反park用)
adc_a/b/c ──►[电流重构 always]──► ia,ib,ic                                  │
                                   │                                        │
                                   ▼                                        │
                            [clark_tr] ──► ialpha,ibeta                      │
                                   │                                        │
                                   ▼                                        │
                  psi ──►[park_tr] ──► id,iq                                │
                                   │                       ▲                │
              id_aim,iq_aim ───────────────────────────────┘                │
                                   │                                        │
                                   ▼                                        │
                     [pi_controller]×2 ──► vd,vq                            │
                                   │                                        │
                                   ▼                                        │
                         [cartesian2polar] ──► vr_rho,vr_theta              │
                                   │                                        │
                                   ▼                                        │
                     [反park always] ◄──────────────────────────────────────┘
                                   │
                                   ▼
                          vs_rho,vs_theta
                                   │
                                   ▼
                              [svpwm] ──► pwm_a,pwm_b,pwm_c,pwm_en
                                   │
                          pwm_a/b/c ──►[hold_detect] ──► sn_adc (回到外设)
```

要点：`foc_top` 把框图蓝色区域里所有小方块都包含进来了——`clark_tr`、`park_tr`（内部还调 `sincos`）、两个 `pi_controller`、`cartesian2polar`、`svpwm`、`hold_detect`，外加它自己实现的「角度换算」「电流重构」「反 Park」三段逻辑。

#### 4.3.3 源码精读

**参数与端口**。[RTL/foc/foc_top.v:11-45](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L11-L45) 定义了 5 个 parameter（`INIT_CYCLES`/`ANGLE_INV`/`POLE_PAIR`/`MAX_AMP`/`SAMPLE_DELAY`）和端口。注意它的端口都是 FOC 抽象量：`phi`、`sn_adc`/`en_adc`/`adc_a|b|c`、`pwm_*`、`id`/`iq`/`id_aim`/`iq_aim`——**没有任何 I2C/SPI/UART 引脚**。这就是它硬件无关的根本原因。

**内部信号**。[RTL/foc/foc_top.v:47-64](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L47-L64) 声明了流水线各级的中间量：`psi`（电角度）、`ia/ib/ic`、`ialpha/ibeta`、`vd/vq`、`vr_rho/vr_theta`、`vs_rho/vs_theta`。这些就是上图各模块之间的连线。

**自己实现的三段逻辑**（不是子模块，是 `always` 块）：
- 角度换算：[RTL/foc/foc_top.v:68-88](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L68-L88)，用 \(\psi = N\cdot(\varphi-\Phi)\) 把机械角度换成电角度（含 `ANGLE_INV` 反向）。
- 电流重构：[RTL/foc/foc_top.v:92-109](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L92-L109)，由 ADC 原始值算出 `ia/ib/ic`。
- 初始化 + 反 Park：[RTL/foc/foc_top.v:213-237](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L213-L237)，初始化阶段标定初始角度 Φ；之后持续做反 Park（`vs_theta <= vr_theta + psi`）。

**7 个子模块例化**（蓝色区域的小方块）：
- `clark_tr`：[RTL/foc/foc_top.v:119-129](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L119-L129)
- `park_tr`：[RTL/foc/foc_top.v:140-150](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L140-L150)（其内部调用 `sincos.v`）
- 两个 `pi_controller`：[RTL/foc/foc_top.v:160-170](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L160-L170) 和 [RTL/foc/foc_top.v:180-190](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L180-L190)
- `cartesian2polar`：[RTL/foc/foc_top.v:200-209](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L200-L209)
- `svpwm`：[RTL/foc/foc_top.v:246-256](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L246-L256)
- `hold_detect`：[RTL/foc/foc_top.v:264-271](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L264-L271)

数一数例化：`clark_tr`、`park_tr`、`pi_controller`(×2)、`cartesian2polar`、`svpwm`、`hold_detect`——加上 `park_tr` 内部还藏着 1 个 `sincos`、`svpwm` 内部也用到 `sincos`，蓝色区域总共 8 个文件（与 `RTL/foc/` 目录的 8 个文件完全吻合）。

#### 4.3.4 代码实践：列全 `foc_top` 的子模块

**实践目标**：从源码验证「蓝色区域 = 8 个文件」，并体会 `foc_top` 作为「蓝色顶层」的角色。

**操作步骤**：

1. 打开 `RTL/foc/foc_top.v`，找出所有 `xxx u_yyy (` 形式的例化（即模块名 + 例化名）。
2. 把它们列出来：`clark_tr u_clark_tr`、`park_tr u_park_tr`、`pi_controller u_id_pi`、`pi_controller u_iq_pi`、`cartesian2polar u_cartesian2polar`、`svpwm u_svpwm`、`hold_detect u_adc_sn_ctrl`。
3. 注意 `park_tr.v` 和 `svpwm.v` 内部还各自例化了 `sincos`（可在 `SIM/tb_clark_park_tr_run_iverilog.bat`、`SIM/tb_svpwm_run_iverilog.bat` 的编译文件列表里看到 `sincos.v` 同时出现）。

**需要观察的现象**：`foc_top` 自己例化了 7 个模块（其中 `pi_controller` 用了两次），加上子模块内部用到的 `sincos`，蓝色区域正好覆盖 `RTL/foc/` 下的 8 个 `.v` 文件。

**预期结果**：你会确认 `foc_top.v` 没有任何「越界」依赖——它只引用同目录下的 7 个伙伴文件，这就是蓝色区域「自洽、可整体复用」的体现。

#### 4.3.5 小练习与答案

**练习 1**：`foc_top` 的端口里为什么没有 `i2c_scl`、`spi_sck` 这些引脚？

**参考答案**：因为 `foc_top` 是硬件无关的核心，它只关心 FOC 的抽象量（角度 `phi`、ADC 结果 `adc_a|b|c`、PWM 输出）。具体怎么用 I2C/SPI 把这些量从芯片里读出来，是粉色外设控制器（`i2c_register_read`、`adc_ad7928`）的职责，由顶层 `fpga_top` 负责对接。这种抽象正是 `foc_top` 可移植的关键。

**练习 2**：`foc_top` 里 `pi_controller` 被例化了两次（`u_id_pi` 和 `u_iq_pi`），分别用在哪？

**参考答案**：`u_id_pi` 对 d 轴电流做 PI 控制（输入 `id`/`id_aim`，输出 `vd`）；`u_iq_pi` 对 q 轴电流做 PI 控制（输入 `iq`/`iq_aim`，输出 `vq`）。d、q 两轴各自独立闭环，所以同一个模块例化两次。这是「代码复用」的典型例子。

---

### 4.4 颜色分区、固定功能与可移植边界

#### 4.4.1 概念说明

把 4.1~4.3 串起来，本节回答三个递进的问题：

1. **颜色分区**：12 个文件各自属于框图的哪个颜色区域？
2. **固定功能**：哪些是 README 标注「一般不需要改动」的，哪些是可替换 / 可移除的？
3. **平台无关**：哪些可以原样搬到 Xilinx / Lattice，哪些要改？

这里有一个**容易混淆的关键点**：「平台无关」在本项目里有两层含义，必须分开：

- **跨 FPGA 厂商可移植**（Altera ↔ Xilinx ↔ Lattice）：README 明确说，除 `fpga_top.v` 里的 `altpll` 原语外，全库都是纯 RTL。所以从这一层看，**只有 `fpga_top.v` 一个文件不平台无关**（因为含 `altpll`），其余 11 个都平台无关。
- **跨传感器 / ADC 型号可移植**（AS5600/AD7928 ↔ 其它）：只有蓝色区域硬件无关；粉色外设（`i2c_register_read.v`、`adc_ad7928.v`）是绑死在具体芯片型号上的，换型号要重写。

所以同一句话「这个文件平台无关吗」会有两个答案，取决于你问的是哪一层。这正是 u1-l1 里「粉色 = 硬件相关、蓝色 = 硬件无关」的精确化。

#### 4.4.2 核心流程：三步分类法

对任意一个 `.v` 文件，按下面三步归类：

1. **看目录**：在 `RTL/foc/` 里 → 大概率蓝色核心；在 `RTL/` 根目录 → 顶层 / 外设 / 用户逻辑之一。
2. **看 README 表格的「备注」列**：标了「固定功能，一般不需要改动」的是蓝色核心；标了「可以移除」的是黄色用户逻辑；没有任何「固定功能」标注的粉色外设是可替换件。
3. **看是否含 `altpll` 等厂商原语**：含 → 跨厂商移植时要改；纯 RTL → 跨厂商直接搬。

#### 4.4.3 源码精读：归类对照表

综合 README 表格（[README.md:433-446](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L433-L446)）与颜色说明（[README.md:453-460](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L453-L460)），12 个文件的归类如下（这也是本讲综合实践的「标准答案」）：

| # | 文件 | 目录 | 颜色区域 | 是否「固定功能」 | 跨 FPGA 厂商可移植 | 跨传感器型号可移植 |
| - | :-- | :-- | :-- | :-- | :-- | :-- |
| 1 | `fpga_top.v` | `RTL/` | 顶层容器（含黄色用户逻辑） | 否（顶层，要按工程改） | **否**（含 `altpll`） | — |
| 2 | `i2c_register_read.v` | `RTL/` | 粉色（传感器控制器） | 否 | 是 | **否**（绑 AS5600） |
| 3 | `adc_ad7928.v` | `RTL/` | 粉色（传感器控制器） | 否 | 是 | **否**（绑 AD7928） |
| 4 | `uart_monitor.v` | `RTL/` | 黄色（用户逻辑） | 否（可移除） | 是 | 是 |
| 5 | `foc_top.v` | `RTL/foc/` | 蓝色（核心顶层） | **是** | 是 | 是 |
| 6 | `clark_tr.v` | `RTL/foc/` | 蓝色 | **是** | 是 | 是 |
| 7 | `park_tr.v` | `RTL/foc/` | 蓝色 | **是** | 是 | 是 |
| 8 | `sincos.v` | `RTL/foc/` | 蓝色（被 park_tr/svpwm 调用） | **是** | 是 | 是 |
| 9 | `pi_controller.v` | `RTL/foc/` | 蓝色 | **是** | 是 | 是 |
| 10 | `cartesian2polar.v` | `RTL/foc/` | 蓝色 | **是** | 是 | 是 |
| 11 | `svpwm.v` | `RTL/foc/` | 蓝色 | **是** | 是 | 是 |
| 12 | `hold_detect.v` | `RTL/foc/` | 蓝色 | **是** | 是 | 是 |

把这张表压缩成三组（即综合实践要求的三组划分）：

- **传感器外设控制器（粉色，2 个）**：`i2c_register_read.v`、`adc_ad7928.v`
- **FOC 核心算法（蓝色，8 个）**：`foc_top.v`、`clark_tr.v`、`park_tr.v`、`sincos.v`、`pi_controller.v`、`cartesian2polar.v`、`svpwm.v`、`hold_detect.v`
- **用户逻辑（黄色，1 个独立文件 + 顶层内嵌逻辑）**：`uart_monitor.v`，外加写在 `fpga_top.v` 内部的 `cnt` 计数与 `iq_aim` 切换（[RTL/fpga_top.v:136-154](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L136-L154)）
- **顶层 `fpga_top.v`**：不属于三组中的任一组，它是「把三组 + 外部硬件总装在一起」的容器。

> 关于「固定功能」：README 给蓝色区域 8 个文件都标了「固定功能，一般不需要改动」；两个粉色外设和 `uart_monitor` 没有这个标注——这正符合「核心不动、外设可换、用户逻辑可改」的设计意图。

#### 4.4.4 代码实践：判断移植时要改哪些文件

**实践目标**：用「两层平台无关」的视角，判断两个常见移植场景要动哪些文件。

**场景 A：从 Altera Cyclone IV 换到 Xilinx Spartan（传感器和 ADC 都不变）。**

操作：按表查「跨 FPGA 厂商可移植」列。

- 只需改 `fpga_top.v`：把 `altpll` 原语（[RTL/fpga_top.v:50-54](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L50-L54)）替换成 Xilinx 的 Clock Wizard IP，保证输出仍是 36.864MHz。
- 其余 11 个文件原样照搬。

**场景 B：把 AS5600 换成另一种 I2C 磁编码器（FPGA 厂商不变）。**

操作：按表查「跨传感器型号可移植」列。

- 需要改/重写 `i2c_register_read.v`（改 `SLAVE_ADDR`、`REGISTER_ADDR`、可能的位宽处理，见 [RTL/fpga_top.v:60-73](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L60-L73) 的参数）。
- `fpga_top.v` 顶层例化处的参数要相应调整。
- 蓝色区域 8 个文件**完全不动**——这就是把核心算法隔离出来的好处。

**需要观察的现象**：两种场景要动的文件集合**互不重叠**（一个动 `fpga_top.v`，一个动 `i2c_register_read.v`），蓝色核心在两种场景下都不用改。

**预期结果**：你应能体会到 README「充分考虑了封装的合理性和代码重用」这句话——分层抽象让「换 FPGA」和「换传感器」这两类常见改动各自只影响一个文件。

#### 4.4.5 小练习与答案

**练习 1**：有人说「`fpga_top.v` 是纯 RTL，所以它平台无关」。这句话对吗？

**参考答案**：不对。`fpga_top.v` 里调用了 Altera Cyclone IV 专属的 `altpll` 原语（[RTL/fpga_top.v:50-54](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L50-L54)），所以它是全库**唯一**一个跨厂商不可直接移植的文件。README 明确说「除了 `altpll` 原语外，全库都是纯 RTL」。

**练习 2**：蓝色区域的 8 个文件「固定功能，一般不需要改动」，那是不是永远都不能改？

**参考答案**：不是。「一般不需要改动」是针对正常使用而言——这些是 FOC 的标准数学链路（Clark/Park/PI/SVPWM），原理稳定。但如果你要改算法（例如把 SVPWM 换成 SPWM、把 PI 换成模糊控制），改的就是这些蓝色文件。所以「固定功能」强调的是「换传感器 / 换 FPGA 时你不需要动它」，而不是「禁止修改」。

**练习 3**：为什么作者把 `hold_detect.v`（采样窗口检测）也放进蓝色 `foc/` 目录，而不是和 ADC 控制器 `adc_ad7928.v` 一起放粉色 `RTL/` 根目录？

**参考答案**：因为 `hold_detect.v` 解决的是「在 SVPWM 的三相下桥臂同时导通窗口里，延时后通知该采样了」这一**控制策略**问题，它只看 `pwm_a/b/c`、只关心采样时序抽象，和具体 ADC 芯片型号无关——任何 ADC 都适用。而 `adc_ad7928.v` 绑死在 AD7928 的 SPI 协议上。所以前者属硬件无关的蓝色，后者属硬件相关的粉色。这也是为什么 `foc_top` 直接例化 `hold_detect`，而 `adc_ad7928` 由顶层 `fpga_top` 例化。

---

## 5. 综合实践

**任务**：亲手制作一张「12 文件分类总表」，把本讲所有认知串起来。这是本讲的综合交付物，也是后续阅读源码时的「地图」。

**要求**：列出 `RTL/` 下全部 12 个 `.v` 文件，按下表填写（建议你先**自己填**，再对照 4.4.3 节的答案订正）：

| 文件 | 所在目录 | 三组划分（外设/核心/用户逻辑） | 颜色区域 | 是否「固定功能」 | 跨 FPGA 厂商可移植？ | 一句话作用 |
| :-- | :-- | :-- | :-- | :-- | :-- | :-- |
| `fpga_top.v` |  |  |  |  |  |  |
| `i2c_register_read.v` |  |  |  |  |  |  |
| `adc_ad7928.v` |  |  |  |  |  |  |
| `uart_monitor.v` |  |  |  |  |  |  |
| `foc_top.v` |  |  |  |  |  |  |
| `clark_tr.v` |  |  |  |  |  |  |
| `park_tr.v` |  |  |  |  |  |  |
| `sincos.v` |  |  |  |  |  |  |
| `pi_controller.v` |  |  |  |  |  |  |
| `cartesian2polar.v` |  |  |  |  |  |  |
| `svpwm.v` |  |  |  |  |  |  |
| `hold_detect.v` |  |  |  |  |  |  |

**进阶小任务（可选）**：用纸笔或画图工具，把 4.2.2 与 4.3.2 两张数据流图合并成一张「完整模块层次图」——以 `fpga_top` 为根，画出它例化的 4 个模块（含 `altpll`），再在 `foc_top` 下方画出它例化的 7 个子模块。标注每个模块的颜色。这张图就是 README Figure1 在文件层面的具象化，做完后你会对「框图 ↔ 文件」的对应关系非常清晰。

**自检标准**：

- 三个组的人数对得上：外设 2 个、核心 8 个、用户逻辑 1 个独立文件（+ 顶层内嵌）。
- 「固定功能」标「是」的恰好 8 个（全在 `RTL/foc/`）。
- 「跨 FPGA 厂商可移植」标「否」的只有 `fpga_top.v` 一个。
- 能解释清楚为什么 `hold_detect.v` 是蓝色而 `adc_ad7928.v` 是粉色。

---

## 6. 本讲小结

- 仓库分 `RTL/`（综合用，12 个 `.v`）和 `SIM/`（仿真用，4 个文件）两大顶层目录；`RTL/` 内部又分根目录与 `foc/` 子目录，后者装的是硬件无关的 FOC 核心。
- `fpga_top.v` 是工程顶层，职责是「总装」：用 `wire` 把粉色外设、蓝色 FOC 核心、黄色 UART 连成数据通路，并承载演示用的用户逻辑（`iq_aim` 在 ±200 间切换）。
- `foc_top.v` 是蓝色区域的子树根，内部例化了 7 个子模块（`clark_tr`/`park_tr`/`pi_controller`×2/`cartesian2polar`/`svpwm`/`hold_detect`，加上子模块内的 `sincos` 共 8 个文件），自己还实现了角度换算、电流重构、反 Park 三段逻辑。
- 三组划分：粉色外设 2 个（`i2c_register_read.v`、`adc_ad7928.v`）、蓝色核心 8 个、黄色用户逻辑 1 个独立文件（`uart_monitor.v`）+ 顶层内嵌逻辑。
- README 给蓝色 8 个文件标了「固定功能，一般不需要改动」；两个粉色外设可换、`uart_monitor` 可移除。
- 「平台无关」分两层：跨 FPGA 厂商只有 `fpga_top.v`（含 `altpll`）不可直接移植；跨传感器型号只有蓝色区域无关，粉色外设绑死在 AS5600/AD7928 上。

---

## 7. 下一步学习建议

本讲建立了「框图 ↔ 文件 ↔ 模块层次」的地图。接下来有两条路：

- **想先动手跑一次仿真、看看真实波形**：直接跳到 u1-l4，用 iverilog 跑 `SIM/` 里的两个 testbench，在 gtkwave 里看 Clark/Park 把三相正弦「坍缩」成直流量、看 SVPWM 产生马鞍波。这能让你对蓝色区域里那些模块「到底在算什么」有直观感受。
- **想先读透顶层接线和示例行为**：去 u1-l3，逐段精读 `fpga_top.v`，重点是 `altpll` 时钟生成与 `foc_top` 的参数/端口含义，为第 2 单元（沿 `foc_top` 数据流逐模块精读）打好基础。

第 2 单元（u2）会正式进入蓝色区域，从 `foc_top.v` 全景开始，沿「角度换算 → 电流重构 → clark → park → PI → cartesian2polar → 反 park → svpwm → 采样时序」一路把每个子模块拆开讲。本讲理清的「谁连谁」关系，正是那一系列讲义的导航图。
