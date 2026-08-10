# ADC 接口与 AD9215 时钟分频

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂 VHDL 写的 `read_adc`（文件 `custom_adc_ad9215.vhd`）模块，说清它的每一个端口在系统里扮演什么角色。
- 用一个 `generic` 参数 `M` 推导出「输入时钟被除以几」，并算出本系统最高 100 MSPS 采样率是怎么来的。
- 解释 `adc_enable` / `adc_pwdn` / `clk_adc` 三个控制信号的因果关系，理解「power-down（掉电）」对 ADC 芯片的意义。
- 追踪 AD9215 输出的 10 位并行数据 `adc_read[9:0]` 是怎样进入 FPGA、又怎样连到第一块 RAM 的——并发现一个容易被忽略的接线细节。
- 理解 VHDL `package`（包）是什么，以及本工程的 `defs` 包为何「定义了却几乎没用上」。

本讲是信号链「ADC → ram1」这一段的具体实现，承接 [u2-l1 时钟生成与多时钟域](u2-l1-clock-generation-and-domains.md) 讲过的时钟域，并为 [u2-l2 采样存储与三块 RAM](u2-l2-sram-storage-and-three-rams.md) 里 ram1 的写地址来源做铺垫。

## 2. 前置知识

在进入源码前，先用三段话把概念铺平。

**AD9215 是什么。** AD9215 是 Analog Devices 出的一款 10 位（10-bit）、最高 105 MSPS 的高速模数转换器（ADC）。它对外提供 10 根并行数据引脚（一次送出一个采样值，共 1024 个量化等级）和一根转换时钟引脚。FPGA 必须给它一个时钟（通常叫「采样时钟」或「转换时钟」），它才会在每个时钟边沿把模拟输入采成一个 10 位数字。给多快的时钟，它就采多快——所以「采样率」其实就是「喂给 ADC 的时钟频率」。

**power-down（掉电）引脚。** ADC 芯片一般有一个 `PWDN` 引脚：拉高时芯片正常工作，拉低时进入低功耗休眠、停止输出有效数据。本工程里 `adc_pwdn` 就是这根线（注释写明 active HIGH，即高电平有效 = 正常工作）。

**VHDL 的 generic（类属）。** Verilog 用 `parameter`，VHDL 用 `generic`，两者本质相同：在例化一个模块时，可以顺手传一些「配置常数」进去，同一个模块就能复用出不同行为。`read_adc` 用一个 `generic M` 决定时钟除以几，这正是本讲的核心。

> 术语速查：ADC（模数转换器）、MSPS（兆样本/秒，即每秒百万次采样）、parallel bus（并行总线，多位数据用多根线同时传，本工程是 10 位并行）、active HIGH（高电平有效）。

## 3. 本讲源码地图

| 文件 | 语言 | 作用 |
|------|------|------|
| [`vhdl files/custom_adc_ad9215.vhd`](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/custom_adc_ad9215.vhd) | VHDL | `read_adc` 模块：给 ADC 生成采样时钟、控制 power-down、读取 10 位并行数据。**本讲主角。** |
| [`vhdl files/defs.vhd`](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/defs.vhd) | VHDL | `defs` 包：定义一个共享数组类型 `slv8array`。本讲顺带说明它的角色与「几乎未用」的现状。 |
| [`verilog files/TOP.v`](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v) | Verilog | 顶层：例化 `read_adc`、把 `adc_read` 接到 ram1、用 `ADC_clock_mux` 选择 `read_adc` 的输入时钟。 |

> 命名陷阱（承接前几讲）：文件名 `custom_adc_ad9215.vhd` 里的实体名是 `read_adc`；`adc_read`（带下划线、读作「ADC 读数」）则是 TOP 顶层的一个 **10 位输入端口**，两者只差一个字母，含义完全不同，务必分清。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先看 `defs` 包（最简单），再深入 `read_adc` 的时钟分频、使能与数据通路，最后看 TOP 怎么把它接进系统。

### 4.1 defs 包：VHDL 的共享类型定义

#### 4.1.1 概念说明

VHDL 的 `package`（包）相当于 C 的头文件、Verilog 的全局 `` `include ``：把一些**类型（type）、子类型、常量、函数**集中声明在一个地方，别的文件用 `use work.包名.all;` 就能共享它们，避免到处重复定义。

本工程的 `defs` 包只定义了一个东西：一个数组类型 `slv8array`——元素是 8 位 `std_logic_vector` 的变长数组。设计者的意图大概是给串口模块（每个字节是 8 位）准备一种「字节数组」类型。

#### 4.1.2 核心流程

包的组成很简单：先声明 `package defs is ... end defs;`（放类型声明），再写 `package body defs is ... end defs;`（放函数实现，本工程为空）。

#### 4.1.3 源码精读

整个包只有一行实质内容：

```vhdl
package defs is
    type slv8array is array (natural range <>) of std_logic_vector(7 downto 0);
end defs;
```

`type slv8array is array (natural range <>) of ...` 定义了一个「下标范围未指定（`<>`）」的数组类型，每个元素是 8 位逻辑向量。完整定义见 [vhdl files/defs.vhd:13-17](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/defs.vhd#L13-L17) ——这段代码声明了 `slv8array` 这个共享类型。

值得留意的是它的**实际使用情况**：

- `custom_adc_ad9215.vhd` 第 10 行写了 `USE work.defs.ALL;`（见 [vhdl files/custom_adc_ad9215.vhd:10](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/custom_adc_ad9215.vhd#L10)），但 `read_adc` 内部**根本没有用到** `slv8array`——它只处理 10 位数据和整数计数器，`use` 这一行属于「多写的、没起作用」的引用。
- 串口发送模块 `serialt.vhd` 里对 `defs` 的 `use` 是**被注释掉的**（`--USE work.defs.ALL;`），也没真正使用。

结论：在本工程的当前版本里，`defs` 包是一个「定义了但实质未使用」的遗留（vestigial）组件。把它讲清楚，是为了让你读 VHDL 时知道 `use work.defs.all` 这类行意味着什么，而不是被它误导以为 `read_adc` 依赖了某种复杂类型。

#### 4.1.4 代码实践

实践目标：亲手验证 `slv8array` 在工程里到底有没有被用到。

1. 打开 `vhdl files/custom_adc_ad9215.vhd`，找到第 10 行的 `USE work.defs.ALL;`。
2. 在 `read_adc` 的 entity 和 architecture 里通读一遍，寻找任何形如 `slv8array` 的类型引用。
3. 用编辑器在整个 `vhdl files/` 目录里搜索 `slv8array` 关键字。
4. 观察现象：你会看到 `slv8array` 仅在 `defs.vhd` 的定义处出现，`read_adc` 没有使用它。

预期结果：确认 `defs` 包对本模块而言是「挂着但没用」的状态。这是一个**源码阅读型实践**，无需硬件。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `custom_adc_ad9215.vhd` 第 10 行的 `USE work.defs.ALL;` 删掉，`read_adc` 还能编译通过吗？为什么？
**答案**：能。因为 `read_adc` 没有引用 `defs` 包里的任何类型（`slv8array`），它只用 `STD_LOGIC`、`STD_LOGIC_VECTOR`、`integer` 这些 IEEE 标准库里的类型。删掉这行不影响编译。

**练习 2**：`slv8array` 的元素宽度是几位？为什么适合给「串口字节」用？
**答案**：元素是 `std_logic_vector(7 downto 0)`，即 8 位。串口一次收发一个字节正好是 8 位，所以这种「8 位数组」天然适合存一串字节。

---

### 4.2 read_adc 的时钟分频器：generic M 的数学

#### 4.2.1 概念说明

`read_adc` 第一项职责是**给 ADC 芯片生成采样时钟**。AD9215 需要一个外部时钟，本工程的做法是：拿现成的某一路系统时钟（200 MHz 或 50 MHz，由上一讲的 `ADC_clock_mux` 选出），用一个计数器把它**除以 M**，得到「慢一档」的方波，再送给 ADC。

之所以要分频，是因为板载 PLL 出来的时钟（200/50 MHz）对很多应用太快或不够灵活；用一个简单的计数器分频，就能得到 ADC 实际想要的采样率。这个 `M` 就是模块的 `generic` 参数。

#### 4.2.2 核心流程

分频器本质上是一个**模 M 计数器 + 方波合成**：

```
每个输入 clk 的上升沿：
    若计数器 cnt 还没数到 M-1：
        前 M/2 拍让 clk_div = 0
        后面      让 clk_div = 1
        cnt 加 1
    否则（cnt 数满一个周期）：
        clk_div = 1
        cnt 清零
```

这样一来，`clk_div` 每经过 **M 个输入时钟周期**完成一个完整波形（一个 0、一个 1），所以输出周期是输入周期的 M 倍。写成频率关系：

\[
f_{out} = \frac{f_{in}}{M}
\]

当 \(M=2 \)：\(f_{out}=f_{in}/2\)，占空比约 50%。这是本工程的默认配置（`generic M: integer := 2`）。

逐拍追踪 \(M=2\) 时 `clk_div` 的取值（cnt 初值为 0）：

| 输入 clk 边沿 | cnt（处理前） | 判断分支 | clk_div | cnt（处理后） |
|---|---|---|---|---|
| 第 1 拍 | 0 | `0 < M-1=1` 且 `0 < M/2=1` | 0 | 1 |
| 第 2 拍 | 1 | `1 < 1` 不成立 | 1 | 0 |
| 第 3 拍 | 0 | 同第 1 拍 | 0 | 1 |
| 第 4 拍 | 1 | 同第 2 拍 | 1 | 0 |

可见 `clk_div` 的波形是 `0,1,0,1,...`，每 2 拍一个完整周期，频率减半。

#### 4.2.3 源码精读

分频计数器位于主进程内（[vhdl files/custom_adc_ad9215.vhd:39-49](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/custom_adc_ad9215.vhd#L39-L49)）——这段代码用 `cnt` 在 0 到 M-1 之间循环，根据 `cnt` 与 `M/2` 的大小关系合成出方波 `clk_div`：

```vhdl
if (cnt < M - 1 ) then
    if (cnt < M / 2) then
        clk_div <= '0';
    else
        clk_div <= '1';
    end if;
    cnt <= cnt + 1;
else
    clk_div <='1';
    cnt <= 0;
end if;
```

注意几个细节：

- `clk_div` 是一个 `signal`（见 [vhdl files/custom_adc_ad9215.vhd:28-29](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/custom_adc_ad9215.vhd#L28-L29)），它在 `rising_edge(clk)` 下赋值，所以是一个**寄存器化的分频输出**，本身已经是慢时钟域的信号。
- 整个 `if/else` 包在 `if (rising_edge(clk))` 里（[第 35 行](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/custom_adc_ad9215.vhd#L35)），说明这是同步逻辑。
- 计数器声明为 `signal cnt: integer range 0 to M+1 := 0;`（[第 28 行](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/custom_adc_ad9215.vhd#L28)），范围留了一点余量。

#### 4.2.4 代码实践

实践目标：手动算出本系统两种采样率。

1. 回顾 [u2-l1](u2-l1-clock-generation-and-domains.md)：`ADC_clock_mux` 在 `timebase==0` 时选 200 MHz、否则选 50 MHz，结果送到 `read_adc.clk`。
2. 对两种情况分别套用 \(f_{out}=f_{in}/M\)、\(M=2\)，填写下表第三、四列：

| timebase | read_adc.clk（\(f_{in}\)） | 分频比 \(M\) | clk_adc_out（\(f_{out}\)） | 等效采样率 |
|---|---|---|---|---|
| 0 | 200 MHz | 2 | ？ | ？ |
| ≠0 | 50 MHz | 2 | ？ | ？ |

3. 需要观察的现象/预期结果：上排应为 100 MHz（即 100 MSPS，正是项目名里 100 MSPS 的由来）；下排应为 25 MHz（25 MSPS）。
4. 这一档分频是「粗调」，更细的采样间隔由 TOP 里另一个机制（`Transcoder` 产生 `out_trans`，在 `state2` 子状态机里做软件分频）完成——那条链路在 [u5-l2](u5-l2-trigger-and-slope-subfsm.md) 详讲，本讲只需知道 `read_adc` 提供 ADC 的「基础心跳」即可。
5. 数值结果**待本地验证**：上述是据源码逻辑推导，实际波形需用 Vivado 仿真或板载示波器在 `clock_adc_out` 引脚上确认。

#### 4.2.5 小练习与答案

**练习 1**：若把 `generic M` 改成 4，`clk_adc` 的频率会变成原来的多少？占空比大致是多少？
**答案**：变成原来的 1/4（\(f_{out}=f_{in}/4\)）。占空比约 50%：`cnt` 取 0、1 时 `cnt < M/2=2` 输出 0；取 2 时输出 1；取 3（`cnt=M-1` 分支）也输出 1，所以 2 拍低、2 拍高。

**练习 2**：为什么不直接把 PLL 出来的 200 MHz 时钟喂给 ADC，而要先除以 2？
**答案**：两个原因。其一，AD9215 最高 105 MSPS，200 MHz 超出其额定转换速率，可能采不出有效数据；除以 2 得 100 MSPS 正落在芯片能力内。其二，把采样率做成「可被命令切换」（快档 100 / 慢档 25 MSPS）能适应不同信号，留出灵活性。

---

### 4.3 read_adc 的使能控制、power-down 与数据通路

#### 4.3.1 概念说明

`read_adc` 的第二项职责是**控制 ADC 芯片的工作状态**并**把 ADC 的 10 位并行数据接进来**。它有三个相关端口：

- `adc_enable`（输入）：来自顶层的「总开关」。为 1 时让 ADC 正常工作，为 0 时让 ADC 掉电休眠。
- `adc_pwdn`（输出）：直接连到 AD9215 的 `PWDN` 引脚（active HIGH，高电平 = 正常工作）。注意名字虽叫 `pwdn`，但在本工程里**高电平代表「不禁用」**，逻辑上等于「使能」。
- `bites_read`（输入，10 位）/ `data_read`（输出，10 位）：ADC 的 10 位并行数据进 `bites_read`，模块内部顺手把它锁存（寄存）一道再从 `data_read` 吐出。

关键直觉：`adc_enable` 是「总闸」——它一关，时钟停、芯片掉电、数据清零；它一开，分频后的时钟送给 ADC、芯片上电、数据被持续采样进来。

#### 4.3.2 核心流程

在分频计数器之后、同一个时钟进程里，做一次二选一：

```
if (adc_enable == '1'):
    adc_pwdn <= '1'          // 让 AD9215 正常上电工作
    clk_adc  <= clk_div      // 把分频后的时钟送出去给 ADC
    data_read<= bites_read   // 采样 ADC 的 10 位数据
else:
    clk_adc  <= '0'          // 停止送时钟
    adc_pwdn <= '0'          // 让 ADC 掉电
    data_read<= 0            // 数据清零
```

也就是说，`adc_enable` 同时控制了**时钟门控**（`clk_adc` 是否翻转）、**芯片电源**（`adc_pwdn`）和**数据有效性**（`data_read`）。三者是绑定的：要么全开，要么全关。

#### 4.3.3 源码精读

使能与数据通路的代码紧跟分频器之后（[vhdl files/custom_adc_ad9215.vhd:50-60](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/custom_adc_ad9215.vhd#L50-L60)）——这段代码在 `adc_enable=1` 时把分频时钟 `clk_div`、power-down 信号 `adc_pwdn=1`、采样数据 `data_read=bites_read` 同时打开：

```vhdl
if (adc_enable ='1') then
    adc_pwdn<= '1';
    clk_adc <= clk_div;
    --take data
    data_read<= bites_read; 
else
    clk_adc <= '0';
    adc_pwdn<= '0';
    data_read<= (others=> '0');
end if;
```

端口的完整声明在实体里（[vhdl files/custom_adc_ad9215.vhd:13-25](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/custom_adc_ad9215.vhd#L13-L25)）——这段声明了 `generic M` 与 6 个端口：`clk`（系统时钟入）、`adc_enable`（使能入）、`clk_adc`（ADC 时钟出）、`bites_read`（10 位数据入）、`adc_pwdn`（掉电控制出）、`data_read`（10 位数据出）：

```vhdl
entity read_adc is
    generic  ( M: integer := 2  --variable for dividing the clock
              );
    Port ( clk : in  STD_LOGIC;
           adc_enable: in STD_LOGIC;
           clk_adc: out STD_LOGIC;
           bites_read : in  STD_LOGIC_VECTOR(9 downto 0);
           adc_pwdn: out STD_LOGIC;
           data_read: out STD_LOGIC_VECTOR (9 downto 0)
           );
end read_adc;
```

这里有个细节值得品味：`data_read <= bites_read` 是在 `rising_edge(clk)` 下赋值的，所以 `data_read` 是对 ADC 数据做了**一次寄存**（打一拍）。设计者大概是想让 ADC 数据与 `clk_adc` 对齐、更稳。但这一拍寄存在本工程里是否真的被用上？答案见下一节——里面藏着一个「容易被忽略的接线细节」。

#### 4.3.4 代码实践

实践目标：确认 `adc_enable` 在本系统里到底是常开还是会被关掉。

1. 在 `verilog files/TOP.v` 中搜索 `adc_state`（这是连到 `adc_enable` 的顶层寄存器）。
2. 你会发现它在 [第 34 行](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L34) 初始化为 `1'b1`，又在 `init_state` 里 [第 260 行](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L260) 被赋成 `1'b1`。
3. 通读全文件，看它有没有被赋成 0 的地方。
4. 预期结果：`adc_state` 在本工程里**恒为 1**，即 ADC 始终使能、`adc_pwdn` 始终拉高、采样时钟始终在送。换句话说，本工程的 `read_adc` 在运行时永远走 `if (adc_enable='1')` 的「开」分支，`else` 分支是为「能掉电」预留的接口，但顶层目前没去关它。
5. 这是一个**源码阅读型实践**，结论「待本地验证」仅指「是否真的恒为 1」可在仿真里抓波形确认。

#### 4.3.5 小练习与答案

**练习 1**：`adc_pwdn` 名字里有 `pwdn`（power-down），那它为 0 时芯片是「工作」还是「掉电」？
**答案**：掉电。虽然名字暗示「power-down 信号」，但代码里 `adc_enable=1`（正常工作）时把 `adc_pwdn` 设为 1，`adc_enable=0`（禁用）时设为 0。结合 AD9215 手册 `PWDN` 引脚 active HIGH 的特性，可推断「`adc_pwdn=1`→不休眠→正常工作」。读这种引脚时不能只看名字，必须结合赋值方向和芯片手册。

**练习 2**：为什么 `clk_adc`、`adc_pwdn`、`data_read` 三个输出要由同一个 `adc_enable` 统一开关，而不是各自独立控制？
**答案**：因为三者语义上绑定——ADC 掉电时既不该收时钟（`clk_adc` 停），也不会产出有效数据（`data_read` 清零）。统一开关能保证状态一致，避免「时钟还在转但芯片已掉电」这类不一致状态造成下游 RAM 写入垃圾数据。

---

### 4.4 在 TOP 中例化 read_adc：从 ADC 引脚到 ram1 的完整链路

#### 4.4.1 概念说明

把 `read_adc` 单独看懂还不够，必须看它在顶层 TOP 里**怎么被接进去**。这一步会回答两个关键问题：

1. **采样时钟是怎么一路传到 ADC 的？** 板载晶振 → PLL → `ADC_clock_mux` 二选一 → `read_adc.clk` → 分频 → `read_adc.clk_adc`（即 TOP 的 `clock_adc_out` 引脚）→ AD9215。
2. **AD9215 的 10 位数据是怎么进到第一块 RAM 的？** 这里有一个让人意外的接线——见 4.4.3。

#### 4.4.2 核心流程

采样时钟链（「谁能决定采样率」的完整路径）：

```
clk_in (100 MHz 板载晶振)
   └─ pll_loop ─产生→ clk (200 MHz) 与 clk_50 (50 MHz)
        └─ ADC_clock_mux ─由 adc_div_sel 二选一→ clock_adc_in
             └─ read_adc.clk ─除以 M=2→ clk_adc_out (= clock_adc_out)
                  ├─→ TOP 的 clock_adc_out 引脚 → AD9215 的转换时钟
                  └─→ 驱动 TOP 里的 state2 采样子状态机
```

而 `adc_div_sel` 又由 `timebase` 决定（在 `wait_state` 里）：`timebase==0 → sel=1 → 200 MHz`；否则 `sel=0 → 50 MHz`。把整条链乘起来就是 4.2.4 里的那张采样率表。

数据链（10 位样本怎么落盘到 ram1）：

```
AD9215 10 位并行输出 → TOP.adc_read[9:0] (FPGA 物理引脚)
   ├─→ read_adc.bites_read (被锁存进 data_read，但 data_read 未被接出！)
   └─→ SRAM ram_adc.data_in  ← 这才是真正写入 ram1 的路径
```

#### 4.4.3 源码精读

**先看例化。** TOP 里 `read_adc` 被例化为 `adc_conf`（[verilog files/TOP.v:119-125](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L119-L125)）——这段代码把 `read_adc` 的 6 个端口接进顶层，注释也写明了它的三大职责（生成时钟、使能、设置掉电）：

```verilog
read_adc adc_conf(   .clk(clock_adc_in),     // 来自 ADC_clock_mux 的被选时钟
                     .adc_enable(adc_state), // 恒为 1
                     .clk_adc(clock_adc_out),// 分频后送 ADC 芯片
                     .bites_read(adc_read),  // ADC 的 10 位引脚数据
                     .adc_pwdn(adc_pwdn)     // 掉电控制引脚
                     // 注意：.data_read 这个 10 位输出没有出现在这里！
                    );
```

> **关键细节（容易看漏）**：实体 `read_adc` 一共有 **6 个端口**（见 4.3.3 的实体声明），但这里的例化**只映射了 5 个**——输出端口 `data_read` **没有被连接**。Verilog 允许例化时不连某个输出端口，它会悬空（floating）。也就是说，`read_adc` 内部辛辛苦苦锁存的 `data_read <= bites_read`（4.3 节那一拍寄存），在顶层**根本没人用**。

**那 ram1 到底从哪里拿 ADC 数据？** 答案是：直接从顶层输入端口 `adc_read` 拿，绕过 `read_adc` 的数据输出。看 ram1 的例化（[verilog files/TOP.v:127-134](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L127-L134)）——`data_in` 直接接 `adc_read`：

```verilog
SRAM ram_adc(   .clk(clk),          // 200 MHz 系统时钟
                .addr(ADR),         // 写地址（来自 state2）
                .we(we),            // 写使能
                .data_in(adc_read), // ★ ADC 10 位数据直接进 RAM，没经过 read_adc.data_read
                .data_out(buffer),
                .addr_r(ram_read),
                .carry(carry));
```

所以本系统里 `read_adc` 真正起作用的只有两件事：**生成 ADC 采样时钟** 和 **控制 power-down**；它的「数据锁存」功能是冗余的、被旁路了。这是读这份代码时最值得记下的「坑」之一。

**采样时钟是 TOP 的哪个引脚？** 见顶层端口声明（[verilog files/TOP.v:23-26](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L23-L26)）——`adc_read` 是 10 位并行数据输入、`clock_adc_out` 是送给 ADC 的时钟、`adc_pwdn` 是掉电控制：

```verilog
input [9:0] adc_read,     //parallel data provided by adc
output adc_pwdn,          //adc power down pin(active HIGH)
output clock_adc_out,     //clock signal for the ADC converter
```

**`ADC_clock_mux` 怎么选时钟？** 它的实现只有一行（[verilog files/ADC_clock_mux.v:6-12](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/ADC_clock_mux.v#L6-L12)）：`sel=1` 选 200 MHz、`sel=0` 选 50 MHz，结果取名 `clock_adc_in` 喂给 `read_adc.clk`。其例化在 [verilog files/TOP.v:205-208](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L205-L208)。

**`adc_div_sel` 怎么被设置？** 在主状态机的 `wait_state` 里（[verilog files/TOP.v:267-268](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L267-L268)）——`timebase==0` 时 `adc_div_sel=1`（走 200 MHz），否则 `=0`（走 50 MHz）：

```verilog
if(timebase==4'b0000) adc_div_sel<=1'b1;
else adc_div_sel<=1'b0;
```

**采样时钟还驱动了谁？** 除了送给 AD9215 芯片，`clock_adc_out` 还在 FPGA 内部驱动 `state2` 子状态机——它正是「把 `adc_read` 按节拍写进 ram1」的那个进程（[verilog files/TOP.v:460-465](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L460-L465)）：

```verilog
//This state machine is just for storing data from adc and for slope computation
always @(posedge clock_adc_out) 
    begin 
    case(state2)
        s1: begin 
            trig1<=adc_read[9:2];   // 用高 8 位做触发电平/斜率比较
            ...
```

这正好呼应了 [u2-l2](u2-l2-sram-storage-and-three-rams.md) 留的那个悬念：ram1 的写地址 `ADR` 来自 ADC 时钟域。`state2` 跑在 `clock_adc_out`（分频后的采样时钟）上，每来一个采样边沿就可能把 `ADR` 加 1（具体怎么加、何时加，是 [u5-l2](u5-l2-trigger-and-slope-subfsm.md) 的内容）。

#### 4.4.4 代码实践

实践目标：完整画出「时钟链」和「数据链」两张图，并定位那个被旁路的 `data_read`。

1. **时钟链追踪**：从 `verilog files/TOP.v` 的 `clk_in` 出发，依次找到 `pll_loop`（第 95 行）、`ADC_clock_mux`（第 205 行）、`read_adc adc_conf`（第 120 行），确认 `clock_adc_in`（mux 输出）→ `read_adc.clk`、`read_adc.clk_adc` → `clock_adc_out`（TOP 引脚）的接法。
2. **数据链追踪**：找到 `adc_read` 这个顶层输入端口（第 23 行），分别确认它同时被接到了 `read_adc.bites_read`（第 123 行）和 `SRAM ram_adc.data_in`（第 131 行）。
3. **找悬空端口**：对照 `read_adc` 实体的 6 个端口（custom_adc_ad9215.vhd 第 17–23 行）与 TOP 第 120–125 行的例化，逐个打钩，找出唯一一个没有出现的输出端口。
4. 预期结果：那个端口是 `data_read`。请把这一发现用自己的话写下来——「`read_adc` 的数据锁存输出 `data_read` 在 TOP 中未连接，ram1 实际直接读取顶层 `adc_read` 端口」。
5. 观察现象（如可在 Vivado 中综合）：综合报告里 `data_read` 会被优化掉（无驱动负载），可作为上述结论的旁证。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：既然 ram1 直接从 `adc_read` 取数，那 `read_adc` 里 `data_read <= bites_read` 那一拍寄存是不是完全没用？
**答案**：在**本工程当前接线**下，是的——因为 `data_read` 没被连出去，这一拍寄存没有负载，会被综合工具优化掉。它更像是模块作者为「将来可能想把数据打一拍再用」预留的接口。这也提醒我们：读模块时不能只看模块内部逻辑，还要看它在顶层的实际接线。

**练习 2**：`clock_adc_out` 这个名字容易让人以为它「来自 ADC」，它的真实身份是什么？
**答案**：它是 **FPGA 送给 ADC 的转换时钟**，方向是 FPGA→AD9215，不是 ADC→FPGA。它由 `read_adc` 分频产生，对内还驱动 `state2` 子状态机。承接 [u2-l1](u2-l1-clock-generation-and-domains.md) 的命名陷阱提醒：本工程的时钟网线名常与方向直觉相反，读代码时要以赋值/例化关系为准，而不是以名字猜方向。

---

## 5. 综合实践

把本讲三块内容（时钟分频、使能控制、数据通路）串起来，做一次「端到端」的源码追踪与推演：

**任务：为一帧采样还原「时基命令 → 实际采样率 → 数据落盘」的完整故事。**

1. 假设上位机通过串口把 `timebase` 设成了 `4'b0000`。
2. 据本讲与 [u2-l1](u2-l1-clock-generation-and-domains.md)，写出此时：`adc_div_sel` = ? → `ADC_clock_mux` 选哪一路（200/50 MHz）→ `read_adc.clk` = ? → 经 `M=2` 分频后 `clock_adc_out` = ? → 等效采样率 = ?。
3. 再假设 `timebase` 被设成任意非零值（如 `4'b0100`），重做第 2 步。
4. 针对任一种情况，描述一个 ADC 采样值从 AD9215 的 10 根数据线，到 ram1 某个存储单元的完整旅程：经过哪些信号名（`adc_read` → `bites_read` / `ram_adc.data_in`）、由哪个时钟节拍写入（`state2` 在 `posedge clock_adc_out` 更新 `ADR`，ram1 在 `posedge clk` 写）。
5. 最后回答：在这一整条链里，`read_adc` 的 `data_read` 输出有没有参与？为什么？

**参考要点（用于自查）**：

- 第 2 步：`timebase=0` → `adc_div_sel=1` → 选 200 MHz → `read_adc.clk=200 MHz` → `clock_adc_out=100 MHz` → 100 MSPS。
- 第 3 步：非零 → `adc_div_sel=0` → 选 50 MHz → `read_adc.clk=50 MHz` → `clock_adc_out=25 MHz` → 25 MSPS。
- 第 4 步：AD9215 10 位 → `adc_read[9:0]` → 同时进 `read_adc.bites_read`（被锁存但 `data_read` 悬空）和 `ram_adc.data_in`（真正写入）；写地址 `ADR` 由 `state2` 在采样时钟 `clock_adc_out` 域更新，ram1 本身在 `clk`（200 MHz）域执行同步写。
- 第 5 步：`data_read` 未参与，因为 TOP 没连接它。

> 说明：上述采样率为据源码推导的理论值，**待本地验证**（用 Vivado 仿真 `clock_adc_out`，或在 Nexys 4 DDR 的 PMOD/IO 引脚上用示波器测量）。

## 6. 本讲小结

- `read_adc`（`custom_adc_ad9215.vhd`）是 AD9215 的接口模块，三职责：**生成采样时钟、控制 power-down、读入 10 位并行数据**。
- 时钟分频靠一个模 M 计数器：\(f_{out}=f_{in}/M\)，默认 `M=2`，配合 mux 的 200/50 MHz 两档，得到 100 MSPS（快）与 25 MSPS（慢）两档采样率——项目名里的 100 MSPS 即由此而来。
- `adc_enable` 是总闸：为 1 时同时打开 `clk_adc`（时钟）、`adc_pwdn=1`（上电）、`data_read=bites_read`（采数）；为 0 时三者全关。本工程里 `adc_state` 恒为 1，ADC 始终工作。
- **接线细节**：TOP 例化 `read_adc` 时，输出端口 `data_read` **未连接**；ram1 直接从顶层 `adc_read` 端口取数，`read_adc` 的数据锁存功能被旁路。
- `defs` 包定义了数组类型 `slv8array`，但 `read_adc` 虽 `use` 了它却没真正使用，属遗留代码；读懂 `package` 机制即可，别被 `use work.defs.all` 误导。
- 命名陷阱延续：`clock_adc_out` 是 FPGA 送给 ADC 的时钟（方向 FPGA→ADC），名字与直觉相反，认接线不认名字。

## 7. 下一步学习建议

- 想知道 `timebase` 这个 4 位是怎么被 PC 命令改写、又怎样进一步通过 `out_trans` 控制 `state2` 的逐拍采样间隔？进入 [u5-l2 触发与斜率子状态机](u5-l2-trigger-and-slope-subfsm.md)，那里详解 `state2` 在 `clock_adc_out` 域里如何写 ram1、如何判触发。
- 想搞清 ram1 的同步写、双端口读写时序、以及为什么 `carry` 满标志只在这里用？复习/深读 [u2-l2 采样存储与三块 RAM](u2-l2-sram-storage-and-three-rams.md)。
- 想了解整条 ADC→ram1 之后去往 FFT 的下一步（二进制偏移转补码）？继续 [u2-l4 二进制偏移与二进制补码转换](u2-l4-binary-offset-to-twos-complement.md)。
- 建议顺带精读的源码：`verilog files/ADC_clock_mux.v`（一行二选一）、`verilog files/TOP.v` 第 460–495 行的 `state2` 进程，把「采样时钟如何驱动写地址」彻底闭环。
