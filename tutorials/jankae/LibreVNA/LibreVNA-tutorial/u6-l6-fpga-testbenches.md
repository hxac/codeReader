# FPGA 验证文化：Testbench 全家福

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂仓库中任意一个 `Test_*.vhd`，说出它的三要素：时钟生成、复位序列、激励（stimulus）注入方式。
2. 识别仓库 testbench 的真实风格——**「激励生成＋人工波形观察」**，而不是教科书上的「激励＋自动断言」，并理解这种取舍的来龙去脉。
3. 区分两类 testbench 的仿真工具依赖：纯源码模块（可用 ISE ISim 或开源 GHDL 仿真）与 CoreGen IP 模块（只能用 ISE 仿真）。
4. 为 `Test_Windowing.vhd` 补充一个自定义激励场景（换窗类型）并加上自动断言，把「看波形」升级为「跑检查」。
5. 建立「改 VHDL 先跑仿真」的开发习惯：仿真只要秒级，综合要几十分钟，上板调试还要烧写固件。

## 2. 前置知识

**什么是 testbench（测试平台）？** 前几讲我们读的都是「会被综合成真实电路」的 VHDL——加法器、状态机、ROM。而 testbench 是一段**只存在于仿真器里、永远不会被烧进 FPGA** 的代码。它的职责是扮演「周围的世界」：给被测模块喂时钟、喂复位、喂输入信号，然后观察输出。

被测的那个模块，术语叫 **UUT（Unit Under Test，被测单元）**。

**激励（stimulus）与断言（assertion）** 是 testbench 的两大件：

- 激励：主动施加的输入，例如「第 100 ns 拉高 RELOAD 一个周期」。
- 断言：对输出的自动检查，例如「WINDOWING_DONE 必须在 272 个样本后变高，否则报错」。

**一个重要的诚实预告**：LibreVNA 仓库里的 10 个 testbench 全部由 Xilinx ISE 的向导自动生成骨架，作者只往里填了激励，**没有写任何一条断言语句**（你可以用 `grep -i assert FPGA/VNA/Test_*.vhd` 验证，唯一的命中 `INTERRUPT_ASSERTED` 只是一个信号名）。所以本讲标题里的「激励-断言模式」要分两步学：先读懂仓库现有的「激励-观察」模式，再在 4.3 节亲手把断言补上——这本身就是一次很好的练习。

**为什么 FPGA 项目特别需要仿真？** 对比一下三条验证路径的代价：

| 路径 | 耗时 | 反馈质量 |
|---|---|---|
| 行为仿真（本讲主题） | 秒级 | 可看任何内部信号波形 |
| ISE 综合＋实现＋生成 bitstream | 数十分钟 | 只有时序报告，看不到信号值 |
| 烧板＋配合 MCU 固件联调 | 小时级 | 只能靠有限的调试手段（如 u6-l2 提到的 `DEBUG_STATUS`） |

改一行 VHDL 就重跑一次综合，反馈周期太长；先在仿真里把逻辑跑对，再综合上板，是 FPGA 开发的标准节奏。这也是仓库为几乎每个功能模块都配一个 `Test_*.vhd` 的原因——单元六逐块精读的 Sweep、Sampling、Windowing、DFT、MAX2871、SPI 接口，全都配有对应 testbench。

**VHDL 仿真专用的语言特性**：testbench 里会出现一些「不可综合但可仿真」的写法，例如 `wait for 100 ns;`（时间等待）、无限循环 `while True loop`、以及初始值 `signal CLK : std_logic := '0';`。综合器遇到它们会报错，仿真器则完全支持——这正是 testbench 与可综合代码的分界线。

## 3. 本讲源码地图

本讲涉及的文件全部位于 `FPGA/VNA/` 下。仓库共有 10 个 testbench，一一对应单元六精读过的功能模块：

| 文件 | 被测模块（UUT） | 对应讲义 | 激励风格 |
|---|---|---|---|
| [Test_PLL.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_PLL.vhd) | PLL（CoreGen 时钟 IP） | u6-l1 | 最简：仅复位 |
| [Test_SinCos.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_SinCos.vhd) | SinCos（CoreGen IP） | u6-l3/u6-l4 | 逐值步进 |
| [Test_MCP33131.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_MCP33131.vhd) | MCP33131（ADC 接口） | u6-l3 | 无限周期脉冲 |
| [Test_MAX2871.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_MAX2871.vhd) | MAX2871（PLL 寄存器写入器） | u6-l5 | 一次性配置＋触发 |
| [Test_SPI.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_SPI.vhd) | spi_slave（SPI 从机） | u6-l5 | procedure 封装逐位发送 |
| [Test_SPICommands.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_SPICommands.vhd) | SPICommands（命令分发器） | u6-l5 | 命令序列 |
| [Test_Window.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Window.vhd) | window（窗系数 ROM） | u6-l4 | 索引扫描 |
| [Test_Windowing.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Windowing.vhd) | Windowing（三通道加窗器） | u6-l4 | 有限循环 272 脉冲 |
| [Test_Sampling.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Sampling.vhd) | Sampling（采样调度＋单 bin 解调） | u6-l3 | 握手响应式 |
| [Test_DFT.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_DFT.vhd) | DFT（96 bin 频谱核） | u6-l4 | 无限周期脉冲 |

> 注：上表 Test_SPI.vhd 的链接若有打不开的情况，请以仓库实际文件为准（本讲引用以行号链接为准，见 4.1.3）。

另外两个配套文件：

- [FPGA/VNA/VNA.xise](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/VNA.xise) —— ISE 工程文件，10 个 testbench 全部登记在册，每个都带四种仿真关联属性。
- [FPGA/VNA/window.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/window.vhd) —— 窗系数 ROM，用 `textio` 在**仿真与综合的初始化阶段**读取 `Hann.dat`/`Kaiser.dat`/`Flattop.dat` 三个系数文件，这个细节直接决定仿真时的工作目录（见 4.2）。

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **testbench 模式总结**——解剖 ISE 生成骨架的四段式结构，以及仓库演化出的三档激励风格。
2. **仿真工具**——ISE ISim 与开源 GHDL 两条路线，以及 CoreGen IP 和 `.dat` 文件带来的两条硬约束。
3. **自增用例实践**——给 `Test_Windowing.vhd` 换窗型、加断言，完成从「看波形」到「跑检查」的升级。

### 4.1 testbench 模式总结

#### 4.1.1 概念说明

打开任何一个 `Test_*.vhd`，你会发现它们长得几乎一模一样——因为骨架都是 ISE 的「New Source → VHDL Test Bench」向导生成的（每个文件头注释里都留着 `VHDL Test Bench Created by ISE for module: XXX` 的落款）。作者的工作方式是：生成骨架，然后只在 `-- insert stimulus here` 这一行注释下面填激励。

这个骨架可以概括为**四段式结构**：

1. **空实体**：testbench 是仿真世界的最顶层，没有任何对外端口。
2. **UUT 的 COMPONENT 声明与信号清单**：把被测模块的端口复制一份，并为每个端口配一个信号；输入信号带初始值，输出信号不初始化。
3. **时钟进程**：一个永不结束的进程，靠 `wait for` 翻转时钟。
4. **激励进程**：一个以 `wait;`（永久挂起）结尾的进程，按时间顺序施加复位和输入。

理解了这个骨架，读任何 testbench 都只需要问三个问题：**时钟周期是多少？复位怎么给？激励注入了什么？**

#### 4.1.2 核心流程

四段式骨架的伪代码：

```text
ENTITY Test_X IS              -- ① 空实体：仿真顶层无端口
END Test_X;

ARCHITECTURE behavior OF Test_X IS
    COMPONENT X ... END COMPONENT;      -- ② 声明 UUT 端口
    signal CLK : std_logic := '0';      --    输入信号带初始值
    signal 输出 : ...;                  --    输出信号不初始化
    constant CLK_period : time := ...;
BEGIN
    uut: X PORT MAP(...);               -- ② 例化 UUT

    CLK_process: process                -- ③ 时钟：无限翻转
    begin
        CLK <= '0'; wait for CLK_period/2;
        CLK <= '1'; wait for CLK_period/2;
    end process;

    stim_proc: process                  -- ④ 激励：顺序施加后挂起
    begin
        RESET <= '1'; wait for 100 ns; RESET <= '0';   -- 复位序列
        wait for CLK_period*10;                         -- 稳定
        -- insert stimulus here                         -- 作者填空处
        wait;                                           -- 进程永久挂起
    end process;
END;
```

激励进程的执行模型值得强调：VHDL 进程从第一句顺序执行到 `wait;` 后**停在原地不再动**，但时钟进程还在跑、UUT 还在被驱动。所以仿真器会在激励耗尽后自动判断「没有事件再发生了」而结束仿真（或在 ISim 里跑满设定时长）。这不是程序退出，而是「事件队列干涸」。

在统一骨架之上，仓库的激励按复杂度分三档：

- **一次性激励**：设一组输入、发一个触发脉冲、挂起。适合「配置类」模块（如 MAX2871 写寄存器）。
- **有限循环**：`for i in 0 to N loop` 发 N 个脉冲后结束。适合「一帧数据」类模块（如 Windowing 的 272 个样本）。
- **无限握手循环**：`while True loop` 永远发脉冲，或 `wait until 信号 = '1'` 做**响应式**激励。适合数据流类模块（如 DFT、Sampling）。

#### 4.1.3 源码精读

**（a）最简样本：Test_PLL.vhd**

先看最短的 testbench，把骨架看清楚。

[FPGA/VNA/Test_PLL.vhd:L35-L38](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_PLL.vhd#L35-L38) —— 空实体。testbench 没有端口，因为它是仿真的最顶层。

[FPGA/VNA/Test_PLL.vhd:L61-L61](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_PLL.vhd#L61-L61) —— 时钟周期常量定义为 20 ns（50 MHz）。各 testbench 按各自 UUT 的需要选周期，例如 Test_MAX2871 用 6.25 ns、Test_Windowing 用 10 ns；仿真时钟不必与真实晶振频率一致，够用即可。

[FPGA/VNA/Test_PLL.vhd:L74-L80](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_PLL.vhd#L74-L80) —— 时钟进程。`wait for CLK_IN1_period/2` 让电平各持续半个周期，两段加起来正好一个周期，无限循环。

[FPGA/VNA/Test_PLL.vhd:L84-L97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_PLL.vhd#L84-L97) —— 激励进程的全部内容：先拉高 RESET 100 ns，再放开，等 10 个周期，然后 `wait;` 挂起。它在 `-- insert stimulus here` 注释下**什么都没填**——因为时钟 IP 唯一需要的外界输入就是复位，观察对象是 `LOCKED` 信号多久变高、`CLK_OUT1` 输出频率是否正确。

**（b）一次性激励：Test_MAX2871.vhd**

[FPGA/VNA/Test_MAX2871.vhd:L107-L125](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_MAX2871.vhd#L107-L125) —— 复位后，给四个影子寄存器 REG4/REG3/REG1/REG0 各塞一个**位图案刻意交错**的 32 位常量（`1111111100000000...`、`1111000011110000...` 等），然后让 RELOAD 高电平一个时钟周期。这些图案设计得很妙：在 MOSI 波形上用肉眼就能分辨出正在移出的是哪一个寄存器，从而验证 u6-l5 讲过的写入顺序 R4→R3→R1→R0 与 LE 锁存脉冲的位置。这就是「无断言也能验证」的手工波形技巧。

[FPGA/VNA/Test_MAX2871.vhd:L81-L83](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_MAX2871.vhd#L81-L83) —— 例化时通过 `GENERIC MAP(CLK_DIV => 10)` 覆盖了 UUT 的类属参数。testbench 可以随意改 generic 来试不同配置，不用动 UUT 源码——这是自增用例的常用手段。

**（c）逐值步进：Test_SinCos.vhd**

[FPGA/VNA/Test_SinCos.vhd:L84-L102](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_SinCos.vhd#L84-L102) —— 把 `phase_in` 从 0 开始每次加 1，每个值保持 10 个时钟周期。观察 `sine`/`cosine` 输出，就能在波形图上看出一张「相位-幅度」查找表的轮廓，检查 Table 是否单调、对称、幅度是否达到满量程。

**（d）有限循环＋参数呼应：Test_Windowing.vhd（本讲主角）**

[FPGA/VNA/Test_Windowing.vhd:L108-L133](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Windowing.vhd#L108-L133) —— 激励进程的完整逻辑：

- [L116](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Windowing.vhd#L116) 选 `WINDOW_TYPE <= "10"`，对照 [window.vhd:L67-L72](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/window.vhd#L67-L72) 的 case 表，`"10"` 是 **Hann 窗**（`"00"` 矩形、`"01"` Kaiser、`"11"` Flattop）。
- [L117](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Windowing.vhd#L117) 设 `NSAMPLES <= "0000000010001"`，即十进制 17。
- [L118-L120](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Windowing.vhd#L118-L120) 三路输入给成 1:2:4 的直流常量（0x0080、0x0100、0x0200），肉眼好区分通道。
- [L125-L130](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Windowing.vhd#L125-L130) `for i in 0 to 271` 循环，每 111 个时钟周期发一个 `ADC_READY` 单周期脉冲。

这里的两个数字都不是随手写的，而是与硬件严格对齐：

- 循环 272 次 = 16 × NSAMPLES = 16 × 17。呼应 u6-l3/u6-l4 的结论「每个扫描点采 16×NSAMPLES 个样本」。
- 间隔 111 周期 = MCP33131 一次「转换＋串行移出」事务的时长（u6-l3 精读过：一次事务约 111 个主时钟）。testbench 用这个间隔逼真地复现 ADC 的节奏，因此 Windowing 的输出波形在时间轴上与真实硬件一致。

[FPGA/VNA/Test_Windowing.vhd:L42-L57](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Windowing.vhd#L42-L57) —— COMPONENT 声明完整复制了 Windowing 的端口表：三路 16 位原始样本进、三路 18 位加窗样本出（多出的 2 位容纳窗增益），外加 `WINDOW_TYPE`、`NSAMPLES`、`ADC_READY`、`WINDOWING_DONE` 四个控制信号。

**（e）响应式激励：Test_Sampling.vhd**

[FPGA/VNA/Test_Sampling.vhd:L132-L159](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Sampling.vhd#L132-L159) —— 这是三档中最精巧的一档：testbench 不是傻等固定间隔，而是用 `wait until ADC_START = '1';` **监听 UUT 的输出**——Sampling 模块想要一个样本时会拉高 `ADC_START`，testbench 扮演 ADC，等 110 个周期（模拟转换耗时）后用 `NEW_SAMPLE` 脉冲交付数据，如此往复。这已经不是单纯的激励，而是一个**行为级 ADC 模型**。参数上（[L142-L147](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Sampling.vhd#L142-L147)）三路输入同样给成幅度递增的常量，`SAMPLES <= 1` 把单点样本数压到最小以缩短仿真。

**（f）procedure 封装：Test_SPI.vhd**

[FPGA/VNA/Test_SPI.vhd:L109-L155](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_SPI.vhd#L109-L155) —— 发一个 16 位 SPI 命令字需要 16 次「移一位、翻一次 SPI_CLK」的操作。作者把这套动作封装成 `procedure SPI(data : std_logic_vector(15 downto 0))`，激励进程里就能一句 `SPI(x"0000")` 发一条命令。有趣的是过程体内部是 16 段复制粘贴的重复代码而非 for 循环——功能正确但不算优雅，读代码时不必模仿，理解意图即可。

**（g）命名陷阱：Test_SPICommands.vhd**

[FPGA/VNA/Test_SPICommands.vhd:L83-L83](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_SPICommands.vhd#L83-L83) —— UUT 端口里有一个 `INTERRUPT_ASSERTED`（中断已置位）信号。注意这只是**信号名**，与 VHDL 的 `assert` 断言语句毫无关系。用 grep 找断言时别被它骗到——这正是我们说「全仓库零断言」时需要专门澄清的地方。

#### 4.1.4 代码实践

**实践：给十个 testbench 做激励风格归类（纯阅读，无需任何工具）**

1. **实践目标**：不运行任何东西，仅凭阅读 `stim_proc` 就能对任意 testbench 分类，证明你已掌握「三档激励风格」的判别方法。
2. **操作步骤**：
   - 打开 `FPGA/VNA/` 下的 `Test_MCP33131.vhd`、`Test_DFT.vhd`、`Test_Window.vhd` 三个文件（本讲未精读的三个），跳到各自的 `stim_proc`。
   - 对每个文件回答三问：时钟周期多少？复位序列什么样？激励属于三档中的哪一档？
   - 把答案填进一张三列表格：文件名 / 时钟周期 / 激励风格。
3. **需要观察的现象**：三个文件的骨架与 4.1.3 精读过的完全一致；`Test_MCP33131` 的复位比较特别——它用 `wait for CLK_period*10.5;` 给出**非整数周期**的复位长度（[Test_MCP33131.vhd:L109-L110](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_MCP33131.vhd#L109-L110)），故意让复位释放沿与时钟沿错开，这是一种不刻意的「异步释放」演示。
4. **预期结果**：参考归类——`Test_MCP33131`：无限周期脉冲档（`while True loop` 每 111 周期一个 START，见 [Test_MCP33131.vhd:L114-L119](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_MCP33131.vhd#L114-L119)）；`Test_DFT`：无限周期脉冲档（每 79 周期一个 NEW_SAMPLE，见 [Test_DFT.vhd:L123-L128](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_DFT.vhd#L123-L128)）；`Test_Window`：逐值步进档（扫窗系数 ROM 的索引）。

#### 4.1.5 小练习与答案

**练习 1**：`Test_Windowing.vhd` 的激励循环为什么恰好是 272 次、间隔为什么恰好是 111 个周期？

**答案**：272 = 16 × NSAMPLES = 16 × 17，对应硬件上「每个扫描点采 16×NSAMPLES 个样本」的规则（u6-l3/u6-l4）；111 周期是 MCP33131 一次转换＋移出事务的主时钟数（u6-l3），testbench 按这个节奏发 `ADC_READY`，使加窗器看到的时间关系与真实硬件一致。

**练习 2**：testbench 的 entity 为什么是空的？综合器遇到它会怎样？

**答案**：testbench 是仿真顶层，激励全部来自内部进程、无需对外连接，所以没有端口。它包含 `wait for`、无限循环等不可综合结构，综合器无法把它变成电路——它本来就只属于仿真器。这也解释了为什么 ISE 工程里 testbench 文件只关联「BehavioralSimulation」等仿真属性而不参与 Implementation（见 [VNA.xise:L143-L148](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/VNA.xise#L143-L148)）。

**练习 3**：激励进程末尾的 `wait;`（不带 for）起什么作用？删掉它仿真器会怎样？

**答案**：不带参数的 `wait` 让进程**永久挂起**，激励到此结束；时钟进程仍在跑，事件队列最终干涸，仿真器据此判断可以收工。在 VHDL 里删掉末尾 `wait;` 的话，进程会立即回到进程体开头重新执行——复位会被再次施加、激励被无限重放（行为类似无限循环测试，通常并非本意；不过本仓库多个 `while True loop` 的激励本来就永不到达 `wait;`，效果等同）。

### 4.2 仿真工具：ISE ISim 与开源 GHDL 两条路线

#### 4.2.1 概念说明

testbench 写好了，用什么跑？仓库的「官方」答案是 Xilinx ISE 14.7 自带的 **ISim** 仿真器（u1-l4 讲过 ISE 是本工程的综合工具链），但 ISE 已停止维护且只在旧平台上好用。好消息是：这批 testbench 的主体是纯 VHDL-93 风格（只用 `std_logic_1164`），**开源仿真器 GHDL ＋ 波形查看器 GTKWave** 也能跑大部分用例。

但「大部分」不是全部。有两条硬约束决定了哪些 testbench 能走开源路线：

**约束一：CoreGen IP 依赖。** `Test_PLL` 与 `Test_SinCos` 的 UUT 不是手写 VHDL，而是 Xilinx CoreGenerator 生成的 IP 核（工程里只有 `ipcore_dir/PLL.xco`、`ipcore_dir/SinCos.xco` 配置，仿真模型要靠 ISE 环境的 UNISIM/unimacro 库展开）。GHDL 没有这些库，这两个 testbench 只能在 ISE/ISim 下仿真。

**约束二：`.dat` 文件依赖。** `Windowing`（以及 `Test_Window`、`Test_Windowing`、`Test_Sampling` 等间接依赖者）的窗系数 ROM 在初始化时用 `textio` 按**相对路径**读取系数文件：

[window.vhd:L44-L58](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/window.vhd#L44-L58) —— `InitWindowDataFromFile` 函数打开文件、逐行读入 128 个 16 位二进制数填进常量数组；`hann`/`kaiser`/`flattop` 三个常量分别绑定 `Hann.dat`、`Kaiser.dat`、`Flattop.dat`。因为是相对路径，**仿真器的启动目录必须是 `FPGA/VNA/`**（或该目录在文件搜索路径上），否则初始化直接失败。这个约束对 ISim 和 GHDL 一视同仁。

顺带一提：这三个 `.dat` 各 128 行、每行一个 16 位二进制串，例如 `Hann.dat` 首行是 `0000000000000001`。它们同时被仿真器和综合器读取——综合时 ROM 内容被「焊死」进 bitstream（u6-l4 讲过），仿真时则动态读入，因此改窗系数不需要改 VHDL。

#### 4.2.2 核心流程

两条工具路线的流程对比：

```text
路线 A：ISE 14.7 + ISim（仓库原生）
  1. 打开 FPGA/VNA/VNA.xise 工程
  2. 在 Sources 窗口切到 "Simulation" 视图
  3. 选中某个 Test_X.vhd，右键 "Set as Top Module"
  4. 双击 "Simulate Behavioral Model"（行为仿真）
  5. ISim 启动，在波形窗口加信号、跑 t 秒、观察
  （10 个 testbench 都可用，包括 CoreGen 的两个）

路线 B：GHDL + GTKWave（开源，无 ISE 时）
  1. cd FPGA/VNA            ← 必须在此目录，Hann.dat 等是相对路径
  2. ghdl -a Windowing.vhd window.vhd Test_Windowing.vhd   （分析）
  3. ghdl -e Test_Windowing                                （ elaborate）
  4. ghdl -r Test_Windowing --wave=wave.ghw                （运行）
  5. gtkwave wave.ghw &    （看波形）
  （仅限纯源码 UUT 的 testbench；Test_PLL/Test_SinCos 不可用）
```

GHDL 三步曲的含义：`-a` 分析（语法/语义检查并生成工作库）、`-e` 精化（把顶层及其引用的实体链接成可执行模型）、`-r` 运行（执行仿真并落波形文件）。`--wave` 产出 GTKWave 格式；若想给别的工具用可换成 `--vcd=wave.vcd`。

#### 4.2.3 源码精读

[FPGA/VNA/VNA.xise:L143-L148](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/VNA.xise#L143-L148) —— 工程文件对 `Test_Windowing.vhd` 的登记：四种仿真关联（Behavioral/PostMap/PostRoute/PostTranslate Simulation）都配上，但不关联 Implementation。也就是说这个文件永远不参与生成 bitstream，只在仿真时可见。其余 9 个 testbench 在同一文件里各占一条同构的登记（见 [L29](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/VNA.xise#L29)、[L85](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/VNA.xise#L85) 等）。

[FPGA/VNA/window.vhd:L62-L75](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/window.vhd#L62-L75) —— 窗 ROM 的读取与选择：索引 `INDEX` 转整数后，时钟上升沿按 `WINDOW_TYPE` 四选一输出系数；`"00"` 输出常数 `0001000000000000`（即 4096，矩形窗的「全通」系数，呼应 u6-l4「四种窗相干增益统一到 4096」的设计）。仿真时把 `INDEX` 从 0 扫到 127，输出的包络就是窗形状——这正是 `Test_Window.vhd` 的验证思路。

[FPGA/VNA/Test_SinCos.vhd:L42-L49](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_SinCos.vhd#L42-L49) —— COMPONENT 声明显示 UUT 名为 `SinCos`。注意它是 CoreGen IP：工程里没有 `SinCos.vhd` 源文件，只有 `ipcore_dir/SinCos.xco` 配置。这就是 4.2.1 约束一的直接证据——GHDL 找不到它的仿真模型。

[FPGA/VNA/Test_PLL.vhd:L42-L49](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_PLL.vhd#L42-L49) —— 同理，UUT `PLL` 对应 `ipcore_dir/PLL.xco`（u6-l1 讲过它把输入时钟倍频到 102.4 MHz 主时钟）。只能在 ISim 里仿。

#### 4.2.4 代码实践

**实践：用 GHDL 跑通 Test_Windowing（或用 ISim，二选一）**

1. **实践目标**：在没有 ISE 的机器上完成一次行为仿真，亲眼看 272 个 Hann 加窗样本的波形，体会「秒级反馈」。
2. **操作步骤**（GHDL 路线，以下命令为示例代码，**待本地验证**——不同版本 ghdl 参数略有差异）：
   ```bash
   cd FPGA/VNA
   ghdl -a window.vhd Windowing.vhd Test_Windowing.vhd
   ghdl -e Test_Windowing
   ghdl -r Test_Windowing --wave=wave.ghw
   gtkwave wave.ghw &
   ```
   如果你装的是 ISE 14.7，则按 4.2.2 路线 A：打开 `VNA.xise` → Simulation 视图 → `Test_Windowing.vhd` 设为 Top → Simulate Behavioral Model。
3. **需要观察的现象**：
   - 仿真必须能找到 `Hann.dat`——若你忘了在 `FPGA/VNA` 目录下运行，初始化会立即报文件打开错误，这正是约束二的活教材。
   - 波形中 `PORT1/PORT2/REF_WINDOWED` 三路输出幅度恒为 1:2:4（输入常量被窗系数缩放，比例不变）。
   - `WINDOWING_DONE` 在 272 个 `ADC_READY` 脉冲之后出现。
   - 由于输入是直流常量，输出包络形状直接就是 Hann 窗系数序列：从中途某处爬升、到峰值、再回落，周而复始。
4. **预期结果**：得到一张含时钟、`ADC_READY`、三路输入、三路加窗输出、`WINDOWING_DONE` 的波形图；数一数相邻 `ADC_READY` 脉冲间隔应为 111 个 `CLK` 周期。若你没有仿真环境，本实践退化为「源码阅读型」：在纸上按 4.1.3(d) 的参数推演 272 个样本期间的输出包络并手绘草图。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Test_DFT`、`Test_MAX2871` 原则上可以走 GHDL 路线，而 `Test_PLL`、`Test_SinCos` 不行？

**答案**：`DFT.vhd` 与 `MAX2871.vhd` 是手写的纯 VHDL 源文件，GHDL 可以直接分析、精化；而 PLL 与 SinCos 的实体来自 CoreGen 生成的 IP 核，仓库里只有 `.xco` 配置没有可移植的 VHDL 源，其仿真模型依赖 ISE 自带的 Xilinx 库，GHDL 没有。（注意 `Test_DFT` 的 UUT 若例化了 DSP 原语则另有变数，实际能否跑通以本地验证为准。）

**练习 2**：把工作目录切到仓库根目录再跑 GHDL，会发生什么？为什么？

**答案**：`window.vhd` 用相对路径 `"Hann.dat"` 打开系数文件，工作目录不对时初始化即失败、仿真无法开始。解决办法是 `cd FPGA/VNA` 后再运行，或把三个 `.dat` 复制/链接到运行目录。这也提醒我们：testbench 的可移植性不仅取决于代码，还取决于它的文件依赖。

**练习 3**：行为仿真（BehavioralSimulation）与 ISE 里另外三种仿真关联（PostMap/PostTranslate/PostRoute）有什么区别？仓库实际用哪一种？

**答案**：行为仿真跑的是「源码直译」的功能模型，最快、最常用；后三种分别在不同实现阶段之后，用带时延/布局信息的网表仿真，慢但能暴露时序问题。仓库日常只跑行为仿真（testbench 也是按功能验证写的，没有时序检查），工程文件里的四种关联只是 ISE 登记模板自带的默认值。

### 4.3 自增用例实践：从「看波形」升级到「跑检查」

#### 4.3.1 概念说明

仓库现有 testbench 的验证闭环靠**人眼看波形**——2020 年快速开发期这完全够用，但有两个代价：一是回归测试靠人，改一处代码没法一键确认没弄坏别处；二是「正确波形长什么样」的知识没有固化在代码里。

本模块做一次升级示范：给 `Test_Windowing.vhd` 增加**第二个激励场景**（换窗型），并引入 VHDL 的 `assert` 语句把预期写成自动检查。`assert` 的语法是：

```vhdl
assert 布尔表达式
    report "人能看懂的失败信息"
    severity warning | error | failure;
```

表达式为假时，仿真器把 report 信息打到控制台，severity 决定严重程度（`failure` 会直接终止仿真）。把关键预期全部写成 assert，仿真跑完没有报错本身就是测试通过的证明——这正是软件世界里单元测试的思路。

为什么选 `Test_Windowing` 而不是规格里点名的 `Test_PLL` 做主战场？因为 `Test_PLL` 的 UUT 是 CoreGen IP（4.2.1 约束一）：我们能改的只有输入时钟周期和复位时长，能检查的只有 `LOCKED` 与输出周期，用例空间很窄，且开源工具链下根本跑不了。规格建议的另一方向「不同 PLL 频率」对时钟管理 IP 并不十分适用。因此本模块以 `Test_Windowing` 为主、`Test_PLL` 的增补为辅（见 4.3.4 步骤五），用换窗型这个更有物理意义、且纯源码可仿真的维度来达成同一学习目标。

#### 4.3.2 核心流程

自增用例的五步法：

```text
1. 复制场景：在 stim_proc 里复制原激励块，改参数（WINDOW_TYPE := "01" Kaiser）
2. 加同步点：用 wait until WINDOWING_DONE = '1' 等待一帧完成，避免检查早于结果
3. 写预期：矩形窗输出 = 输入 × 4096 截位（可精确断言）；
          换窗型则断言「相对关系」：三通道比值恒为 1:2:4、DONE 必须出现
4. 加 assert + severity error，失败时 report 出实际值
5. 跑仿真：控制台无 assert 报错 = 用例通过（可写入脚本做回归）
```

时序上的一个要点：**检查必须晚于结果产生**。激励进程与 UUT 并发运行，检查语句紧跟激励之后不等待，读到的会是旧值。`wait until` 是 testbench 里做「同步」的标准手段，与 4.1.3(e) 的响应式激励同一族技巧。

#### 4.3.3 源码精读

先看清楚我们要扩展的原场景，再给出扩展写法。

[FPGA/VNA/Test_Windowing.vhd:L116-L124](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Windowing.vhd#L116-L124) —— 原激励的参数设定与复位序列：设 Hann 窗、NSAMPLES=17、三路常量输入，`RESET` 高一个周期后放开，随后进入 272 脉冲循环。

[FPGA/VNA/Test_Windowing.vhd:L125-L130](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Windowing.vhd#L125-L130) —— 272 次 `ADC_READY` 脉冲。注意循环结束、执行到 [L132](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Windowing.vhd#L132) 的 `wait;` 之间没有任何检查语句——这正是「激励-观察」模式的空档，也是我们插入断言的位置。

下面的扩展代码是**示例代码**（仓库中不存在，供读者照抄实验），插入到原 `for` 循环之后、`wait;` 之前：

```vhdl
-- ===== 自增用例（示例代码）：Kaiser 窗场景 + 自动断言 =====
-- 场景 2：换 Kaiser 窗重跑一帧，并检查相对关系
WINDOW_TYPE <= "01";                    -- Kaiser（见 window.vhd L67-L72 的编码表）
RESET <= '1';
wait for CLK_period;
RESET <= '0';
for i in 0 to 271 loop
    wait for CLK_period*111;
    ADC_READY <= '1';
    wait for CLK_period;
    ADC_READY <= '0';
end loop;
wait until WINDOWING_DONE = '1';        -- 同步点：等一帧真正完成
wait for CLK_period*2;

-- 断言 1：三通道增益一致，比值应保持 1:2:4
assert PORT2_WINDOWED = PORT1_WINDOWED * 2
    report "通道2增益异常: PORT2=" & integer'image(to_integer(unsigned(PORT2_WINDOWED)))
    severity error;

-- 断言 2：Kaiser 与 Hann 的峰值系数不同，输出不应与上一帧完全相同
-- （具体比较逻辑取决于两窗峰值系数，需查阅 Kaiser.dat/Hann.dat 后确定）
```

更严格的检查可以借「矩形窗」做锚点（示例代码）：矩形窗系数是常数 4096，输入输出关系完全确定，可精确断言：

```vhdl
-- 场景 3（示例代码）：矩形窗，输出应严格等于输入 × 4096 的截位结果
WINDOW_TYPE <= "00";
-- ……重复复位与 272 脉冲循环……
assert PORT1_WINDOWED = std_logic_vector(
           unsigned(PORT1_RAW) * 4096 / 2**14)
    report "矩形窗增益不符合 4096/16384 缩放"
    severity error;
```

（截位系数需对照 `Windowing.vhd` 内部乘法与 u6-l4 讲过的「相干增益统一到 4096」设计确认后再定，此处标注**待确认**——这正是写断言前必须回到数据通路源码求证的例子。）

最后是 `Test_PLL` 的辅助增补（示例代码）：它虽不能换「频率」，但可以把复位后 `LOCKED` 置位的时延写成检查，作为该 testbench 唯一有意义的自动化断言：

```vhdl
-- Test_PLL.vhd 增补（示例代码，只能在 ISim 下运行）
wait until LOCKED = '1' for 10 us;      -- 限时等待锁定
assert LOCKED = '1'
    report "PLL 在 10us 内未锁定"
    severity error;
```

#### 4.3.4 代码实践

**实践：为 Test_Windowing 增加 Kaiser 场景并加上自动断言**

1. **实践目标**：亲手完成一次「自增用例」全流程——复制场景、改参数、加同步点、写断言、跑仿真、记录结论，把本讲三个模块的知识串成一个动作。
2. **操作步骤**：
   - **不要直接改仓库文件**。先复制：`cp FPGA/VNA/Test_Windowing.vhd /tmp/Test_Windowing_my.vhd`（或在 ISE 里新建 testbench）。以下以 `/tmp` 副本为实验对象。
   - 对照 4.3.3 的示例代码，在激励 `for` 循环之后插入场景 2（`WINDOW_TYPE <= "01"`）与断言 1（通道比值检查）。文件头部需要补 `use ieee.numeric_std.all;`（原文件里这行是被注释掉的，见 [Test_Windowing.vhd:L31-L33](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_Windowing.vhd#L31-L33)），否则 `to_integer`/`unsigned` 不可用。
   - 若两个场景放在同一个 stim_proc 里先后执行，注意第二个场景开始前给系统留足复位间隔；更清晰的做法是把两个场景拆成两个 stim 进程或干脆复制成两个 testbench 文件。
   - 按 4.2.4 的命令跑仿真（GHDL 或 ISim 均可，此 UUT 无 CoreGen 依赖）。
   - 记录：控制台输出、`WINDOWING_DONE` 前后三路输出波形截图、Kaiser 与 Hann 两帧输出包络的并排对比。
3. **需要观察的现象**：
   - Kaiser 帧（`"01"`）与 Hann 帧（`"10"`）的输出包络形状不同：Hann 是「余弦钟形」，Kaiser 依 β 参数呈更陡或更缓的肩部；峰值处的缩放也不同（两窗的 128 点系数表来自 `Kaiser.dat` 与 `Hann.dat`，可先 `head` 这两个文件比较数值）。
   - 无论哪种窗，`PORT1:PORT2:REF` 三路输出始终维持 1:2:4——断言 1 应当通过。
   - 故意把断言 1 写错（例如 `* 3`）再跑一次，控制台应立即打出 report 信息——这一步验证你的断言真的「在工作」，而不是形同虚设。
4. **预期结果**：控制台无 assert 报错、波形截图两帧包络可区分、`WINDOWING_DONE` 每帧恰好一次。若你本地没有仿真环境：完成代码修改与「纸上推演」，并在笔记里标注「待本地验证」，同时把两个 `.dat` 文件的前几行抄下来对比、预测两帧包络差异。
5. **波形的量化预期**：相邻 `ADC_READY` 间隔 111 个 `CLK`；一帧长度约 \(272 \times 111 \approx 30192\) 个时钟周期，在 10 ns 周期下约 \(3.0 \times 10^{5}\,\text{ns} = 302\,\mu s\) 的仿真时间——仿真器实际墙钟时间通常只需几秒，再次印证「仿真秒级、综合十分钟」的验证经济学。

#### 4.3.5 小练习与答案

**练习 1**：为什么在 assert 之前要加 `wait until WINDOWING_DONE = '1';`？如果直接在 272 脉冲发完后立刻断言，会出什么问题？

**答案**：VHDL 进程并发执行，激励发完不代表 UUT 已把结果算完；直接断言读到的是旧值（甚至初始值），检查结果不可信。`wait until` 把检查点同步到「一帧完成」事件之后。更稳妥时还可加 `wait until ... for T` 限时版，超时本身就是一个「DONE 没来」的失败信号。

**练习 2**：想把「Kaiser 帧与 Hann 帧输出不同」写成一条严格断言，需要哪些额外信息？

**答案**：需要两窗 128 点系数表（`Kaiser.dat`、`Hann.dat`）以及 `Windowing.vhd` 内部「样本 × 系数 → 18 位输出」的确切截位规则，才能推出常量输入下两帧输出的期望数值。这说明了写严格断言的普遍前提：预期必须来自数据通路源码或独立计算，而不是「看着差不多」。截位规则本讲标注**待确认**，读者可回读 u6-l4 与 `Windowing.vhd` 求证。

**练习 3**：规格中建议「为 Test_PLL 增加不同 PLL 频率的用例」，为什么本讲把它降级为辅助？

**答案**：`Test_PLL` 的 UUT 是 CoreGen 时钟管理 IP，不是可改参数的手写模块——「频率」由 IP 配置（`.xco`）决定而非激励决定，激励侧能变的只有输入时钟周期与复位时序；且该 testbench 依赖 ISE 仿真库，开源工具跑不了。相比之下 `Test_Windowing` 的 `WINDOW_TYPE` 是真实的运行时输入端口，换窗型是纯粹由激励驱动的正交场景，教学价值更高。

## 5. 综合实践

**综合任务：给仓库补一份「仿真优先」回归清单，并新增两个自增用例。**

把本讲全部内容串成一次交付物：

1. **建清单**：新建一份个人笔记（不要提交到仓库），列出 10 个 testbench 的三栏信息：UUT 类型（纯源码/CoreGen）、可否走 GHDL、激励风格（三档归类）。这张表就是你的「仿真资产台账」。
2. **跑基线**：任选一条工具路线（GHDL 或 ISim），跑通 `Test_Windowing` 原始版本，确认你能复现 4.2.4 列出的现象。记录耗时，对比「综合一次 top.vhd」的耗时（如无法综合，引用 u1-l4 的构建时间即可）。
3. **增用例一（Windowing 换窗）**：完成 4.3.4 的 Kaiser 场景与断言。
4. **增用例二（MAX2871 换配置）**：复制 `Test_MAX2871.vhd` 到实验目录，参照 4.1.3(b) 的做法写第二个场景——利用 `GENERIC MAP(CLK_DIV => ...)` 换一个分频值重跑，并在 MOSI/LE 波形上验证：移位时钟变慢了、四个寄存器的移出顺序（R4→R3→R1→R0）与 LE 脉冲位置不变。给「LE 只在四字全部移完后出现」写一条断言（提示：统计两次 LE 之间 SPI_CLK 的翻转数）。
5. **形成习惯**：在笔记末尾写下你的三条「改 VHDL 前后」守则，例如：改任何 `.vhd` 前先确认它有没有对应 testbench；改完先跑行为仿真再谈综合；新功能模块提交时必须附带至少一个带断言的 testbench。

预期产出：一张台账表、两份改过的 testbench 副本（不进仓库）、两帧波形截图与断言运行记录。全部工具操作标注实际结果；无法本地运行的部分明确写「待本地验证」。

## 6. 本讲小结

- 仓库 10 个 `Test_*.vhd` 全部由 ISE 向导生成统一骨架——空实体、UUT 例化、时钟进程、激励进程四段式；读任何 testbench 只需问三问：时钟周期、复位序列、激励内容。
- 激励演化出三档风格：一次性激励（Test_PLL/Test_MAX2871）、有限循环（Test_Windowing 的 272 脉冲）、无限/响应式循环（Test_Sampling 的 `wait until ADC_START` 行为级 ADC 模型）。
- 一个必须诚实面对的事实：**全仓库没有一条 assert 断言语句**，验证闭环靠人眼看波形；`INTERRUPT_ASSERTED` 只是信号名，别被它误导。
- testbench 的参数与硬件严格对齐是 LibreVNA 的一贯作风：272 = 16×NSAMPLES、111 周期 = MCP33131 一次事务，testbench 因此能逼真复现真实时序。
- 工具路线由两条硬约束决定：CoreGen IP（PLL、SinCos）只能在 ISE/ISim 下仿真；`window.vhd` 以相对路径读 `Hann.dat` 等系数文件，仿真必须在 `FPGA/VNA` 目录下启动。
- 自增用例五步法：复制场景 → 改参数 → `wait until` 同步 → 写 assert（预期须来自源码求证）→ 跑仿真看控制台；「故意写错断言验证它真的在工作」是防自欺的关键一步。

## 7. 下一步学习建议

本讲是单元六（FPGA 设计）的收官，也宣告「由浅入深」的源码精读阶段基本完成。接下来有三条路：

1. **闭环 MCU 一侧**：FPGA 的结果经中断交给 MCU（u6-l1 的数据流水线终点），建议带着「testbench 思想」回读 [Software/VNA_embedded/Application/FPGA.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/FPGA.cpp)，观察 MCU 如何读取仿真中我们断言过的那些结果寄存器——两侧对同一份 [FPGA_protocol.tex](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex) 文档负责。
2. **进入单元七（GUI 三大测量模式）**：如果你更关心数据的最终去向，下一站是 `Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp`，看 FPGA 算出的 I/Q 如何变成屏幕上的 S 参数。
3. **深挖验证方法论**：若你对「无实物验证」主题意犹未尽，可以对比仓库的另一套测试体系——单元十一将讲解的 `Software/PC_Application/LibreVNA-Test/` C++ 单元测试，比较软件单测与硬件 testbench 在「激励-预期-判定」结构上的同与不同。

无论选哪条路，请把本讲的习惯带在身上：**改硬件描述先跑仿真，改软件先跑测试，两条红线之外不动手。**
