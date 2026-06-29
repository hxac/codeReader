# Verilator C++ 测试平台

## 1. 本讲目标

前面 u1-l2 跑通了 `cd tb && make all`，u2 系列又把 PE、阵列互联、行/列变换、控制时序逐个拆透。但我们一直没有正面回答一个问题：**那台「自动跑随机矩阵、喂进 DUT、再判断结果对不对」的机器，到底是怎么用 C++ 写出来的？** 这台机器就是 `tb/tb_topSystolicArray.cpp`，它是整个项目唯一的验证程序。

学完本讲，你应该能够：

1. 看懂 Verilator C++ 测试平台的**主循环结构**：时钟是怎么用 `i_clk ^= 1` 翻出来的、复位在哪几个拍生效、`i_validInput` 多久拉高一次。
2. 说出 Verilator 是如何把 SystemVerilog 里的 packed 多维数组端口（`i_a`/`i_b`/`o_c`）**压扁**成 C++ 里的若干个 32 位字的，并解释为什么输入要手动「4 个字节塞一个字」、而输出却能一个元素占一个字直接读。
3. 独立解释 `verifyOutputMatrix` 是如何在 `o_validResult` 脉冲亮起的那一拍采样 `o_c`、和软件手算的期望矩阵逐元素比对、出错就 `exit(EXIT_FAILURE)` 的。
4. 弄清楚 VCD 波形是怎么通过 `VerilatedVcdC` 一拍一拍 `dump` 出来的，以及它和 Makefile 里 `--trace` 选项的对应关系。

本讲属于专家层（advanced），默认你已经掌握了 u1-l2 的 Makefile 四阶段流程和 u2-l1 的 PE 乘加行为。

---

## 2. 前置知识

本讲是 C++ 代码精读，会用到下面几个概念，不熟悉的可以先放慢节奏：

- **DUT（Design Under Test，被测设计）**：指我们要验证的 RTL，这里就是 `topSystolicArray`。Verilator 会把这份 SV 翻译成一个 C++ 类 `VtopSystolicArray`，测试平台通过这个类的成员变量去驱动/观察端口。
- **Verilator 的「翻译成 C++」机制**：Verilator 不是事件驱动仿真器（不像 ModelSim/VCS），它把 RTL 编译成一个两态（0/1，没有 X/Z）的 C++ 模型，由测试平台手动调用 `dut->eval()` 来求值。这让它非常快，但也意味着测试平台要自己负责「造时钟、给复位、控制节拍」。
- **packed 多维数组**：SV 里 `logic [N-1:0][N-1:0][7:0]` 是一个**紧凑拼接**的位向量，最左维是最高位。整块内存连续，没有空洞。这一点直接决定了 Verilator 怎么把它映射到 C++。
- **宽信号在 C++ 里的表示**：C++ 原生整数最多 64 位。Verilator 对超过 64 位的信号，用一个 32 位字（`IData`/`uint32_t`）的数组来表示——低位在前（小端）。这是本讲最关键的「机制」之一。
- **VCD（Value Change Dump）**：一种文本格式的波形文件，记录每个时刻各信号的值变化，用 GTKWave 等工具打开看波形。
- **C++ 后缀约定**：和 RTL 里 `_d`/`_q` 不同，这里的 `matrixA`/`matrixB`/`matrixC` 只是普通 C 数组，分别存输入 A、输入 B、期望输出 C。

> 承接提示：u1-l2 讲过 `make sim` 实际执行的是 `./obj_dir/VtopSystolicArray +verilator+rand+reset+2`（见 `tb/Makefile:27`），这个可执行文件的 `main()` 就在本讲的 `tb_topSystolicArray.cpp` 里。u2-l1 讲过 PE 在 `i_doProcess` 高时累加、低时清零；本讲会看到测试平台如何通过 `i_validInput` 间接驱动这一整轮 `3N-2` 拍的计算。

---

## 3. 本讲源码地图

本讲围绕两个文件展开：

| 文件 | 作用 |
| --- | --- |
| [tb/tb_topSystolicArray.cpp](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp) | C++ 测试平台主体。包含主循环、复位/握手时序、随机矩阵生成、端口打包、期望结果计算、逐元素校验、VCD 波形 dump。本讲几乎全部内容都在这里。|
| [tb/Makefile](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/Makefile) | 构建脚本。本讲只看两处：verilate 阶段的 `--trace`（让 C++ 模型支持波形 dump，第 37 行）和 sim 阶段的 `+verilator+rand+reset+2`（给未初始化寄存器随机初值，第 27 行）。|

辅助理解（不精读）：

| 文件 | 作用 |
| --- | --- |
| rtl/topSystolicArray.sv | DUT 本体。本讲只引用它的端口声明（`i_a`/`i_b`/`o_c` 的位宽与 packed 维度），用来解释 C++ 端为什么这样打包。|
| README.md | 「Verification Outline」一节（第 100-124 行）用自然语言列出了本测试平台的 0~7 步流程，是本讲 C++ 代码的「设计说明书」。|

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应 README「Verification Outline」里的三组步骤：

1. **主循环、复位与 validInput 时序**——对应步骤 0、1、7：怎么造时钟、复位、周期性触发一轮乘法。
2. **随机矩阵生成与端口打包驱动（`driveInputMatrices`）**——对应步骤 2、3：怎么造随机 NxN 矩阵、怎么把它塞进 Verilator 的宽端口。
3. **期望结果计算与校验（`verifyOutputMatrix`）+ 波形 dump**——对应步骤 4、5、6：怎么在 `o_validResult` 亮起时采样、比对、出错终止，以及怎么把全过程录成波形。

### 4.1 主循环、复位与 validInput 时序

#### 4.1.1 概念说明

Verilator 翻译出来的 C++ 模型本身**不会自己走时间**。它就像一个静止的电路，必须由测试平台手动「拨时钟、按节拍调用 `eval()`」。所以测试平台的第一职责是当一个**时钟与节拍发生器**。

具体要解决三件事：

- **造时钟**：用一个整数 `sim_time` 当「仿真时间」，每一步把 `i_clk` 翻转一次，就同时产生了一个上升沿和一个下降沿。
- **给复位**：在仿真最开始拉高 `i_arst` 几拍，把所有寄存器清成已知状态。
- **周期性触发计算**：每隔足够多的拍数拉一拍 `i_validInput`，启动一轮新的矩阵乘法；两轮之间留够 `3N-2` 拍的间隔，让上一轮算完。

#### 4.1.2 核心流程

主循环（`main` 里 `while (sim_time < MAX_SIM_TIME)`）每个 `sim_time` 做的事，用伪代码描述：

```
while (sim_time < 1000):
    dut_reset(dut)              # 1. 决定本拍 i_arst 是 0 还是 1
    dut->i_clk ^= 1            # 2. 翻转时钟（产生一个沿）
    dut->eval()                # 3. 让 DUT 在新时钟/输入下求值
    if dut->i_clk == 1:        # 4. 只在上升沿做下面的事
        posedge_cnt += 1
        toggle_i_validInput(dut)     # 决定本拍 i_validInput
        driveInputMatrices(dut)      # 决定本拍 i_a / i_b
        verifyOutputMatrix(dut)      # 检查本拍有没有 o_validResult
    m_trace->dump(sim_time)    # 5. 把当前所有信号值写进波形
    sim_time += 1
```

几个要点先记在脑子里：

- `i_clk ^= 1` 让时钟在 0/1 之间交替，所以上升沿出现在**偶数** `sim_time`（0,2,4,…）。`posedge_cnt` 只在上升沿自增，它才是真正的「时钟周期」计数。
- 复位、握手输入（`i_validInput`、`i_a`、`i_b`）都是在 `eval()` **之后**才设置新值，意味着它们要到**下一次** `eval()`（下一个沿）才被 DUT 看见。这是「在周期内驱动、下个沿采样」的常见写法。
- `verifyOutputMatrix` 也在 `eval()` 之后调用，所以它读到的是 DUT 在**本沿刚算出来**的 `o_validResult`/`o_c`。

#### 4.1.3 源码精读

**全局参数与计数器**：

[tb/tb_topSystolicArray.cpp:12-25](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L12-L25) —— 定义仿真总长 `MAX_SIM_TIME=1000`、复位窗口边界 `RESET_NEG_EDGE=5`、校验起始时间 `VERIF_START_TIME=7`，以及矩阵规模宏 `N=4`、元素位宽 `WIDTH=8`；并派生两个常量：`maxValue=2^WIDTH=256`（元素取值上界）和 `assertValidInput=3*N+3=15`（每隔多少个上升沿触发一轮新乘法）。`sim_time` 与 `posedge_cnt` 是两个全局时间轴。

> 关于 `assertValidInput = 3*N+3`：一轮乘法本身耗时 `3N-2` 拍（见 u2-l4），这里多给 5 拍（`3N+3 - (3N-2) = 5`）作为两轮之间的「喘息间隔」，确保上一轮的 `o_validResult` 脉冲已经过去、阵列已清零，再启动下一轮。

**复位函数 `dut_reset`**：

[tb/tb_topSystolicArray.cpp:34-40](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L34-L40) —— 默认 `i_arst=0`；仅当 `sim_time` 落在 `(2, 5)` 区间（即 `sim_time` 为 3、4）时才拉高 `i_arst=1`。由于 RTL 里复位是「异步、高有效」（`always_ff @(posedge i_clk, posedge i_arst)`），这两个 `sim_time` 步里只要 `eval()` 被调用，复位就会生效，把 `counter_q`、`validResult_q` 等全部清零。注释里说「默认所有信号初始化为 0，所以其它输入不必显式驱动为 0」——这是 Verilator 两态模型的默认行为。

**validInput 触发函数 `toggle_i_validInput`**：

[tb/tb_topSystolicArray.cpp:43-49](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L43-L49) —— 默认 `i_validInput=0`；仅当 `posedge_cnt` 是 `assertValidInput`(15) 的整数倍**且**复位已结束（`sim_time >= RESET_NEG_EDGE`）时才拉高。所以有效触发发生在 `posedge_cnt = 15, 30, 45, …`，对应 `sim_time = 28, 58, 88, …`。整个 `MAX_SIM_TIME=1000` 内大约能跑 33 轮随机矩阵乘法。

**主循环本体 `main`**：

[tb/tb_topSystolicArray.cpp:197-216](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L197-L216) —— 就是 4.1.2 那段伪代码的真实实现：`dut_reset` → `i_clk ^= 1` → `eval()` → 仅上升沿做 `posedge_cnt++` 与三个驱动/校验调用 → `dump` → `sim_time++`。注意它**每个 `sim_time` 只 `eval()` 一次**，这是一种简化模型——对本设计足够，因为测试平台在每个沿都重新求值并立即消费结果。

#### 4.1.4 代码实践

**实践目标**：通过「源码阅读 + 手算」理解时钟与复位节拍，不实际跑仿真也能预测波形前若干拍的值。

**操作步骤**：

1. 打开 `tb/tb_topSystolicArray.cpp`，找到第 197-216 行的主循环。
2. 准备一张表，列分别是 `sim_time`、`i_clk`（翻转后）、`i_arst`、`posedge_cnt`、`i_validInput`。
3. 从 `sim_time=0`、`i_clk=0` 起逐拍填表，填到 `sim_time=10`。

**需要观察的现象**：

- `i_clk` 在 0,2,4,6,8,10 为 1（上升沿），在 1,3,5,7,9 为 0。
- `i_arst` 只在 `sim_time=3,4` 为 1。
- `posedge_cnt` 在 `sim_time=0,2,4,6,8,10` 处依次为 1,2,3,4,5,6。
- `i_validInput` 在前 10 拍**全程为 0**（因为最近一次触发在 `posedge_cnt=15`，远未到）。

**预期结果**：你应得出一张表，清楚看到「复位窗口」与「validInput 首次触发时刻」这两个关键节点。如果想确认，可在本地跑 `cd tb && make all` 后用 GTKWave 打开 `waveform.vcd`，把 `topSystolicArray.i_clk`、`i_arst`、`i_validInput` 拖出来对照（前若干拍）。若本地无 Verilator/GTKWave 环境，本实践标注为「源码阅读型实践」，结论以手算表为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `toggle_i_validInput` 里要加 `sim_time >= RESET_NEG_EDGE` 这个条件？去掉会怎样？

**参考答案**：防止在复位还没结束时就触发乘法。`posedge_cnt` 从 1 开始递增，理论上 `posedge_cnt % 15 == 0` 最早在 15，那时复位早已结束；这个条件是「双保险」，确保即使将来有人改小 `assertValidInput`，也不会在复位期间误触发 `i_validInput`，把未定义的寄存器状态送进阵列。

**练习 2**：把 `MAX_SIM_TIME` 从 1000 改成 100，大约还能完整跑完几轮随机矩阵乘法？

**参考答案**：`posedge_cnt` 到 100 时 `sim_time` 约为 200；触发发生在 `posedge_cnt=15,30,45,60,75,90`，共 6 轮（下一轮 105 已超出）。所以改成 100 大约只剩 6 轮验证，覆盖度明显下降——这正是 `MAX_SIM_TIME` 要给得足够大的原因。

---

### 4.2 随机矩阵生成与端口打包驱动（driveInputMatrices）

#### 4.2.1 概念说明

这一段是本讲**最容易踩坑**的地方，也是最有学习价值的部分：C++ 测试平台要怎么把一个 N×N 的矩阵「喂」进 Verilator 翻译出来的端口 `i_a`/`i_b`？

难点在于端口位宽。RTL 里 `i_a` 是 `logic [N-1:0][N-1:0][7:0]`，对 N=4 来说是 128 位。C++ 原生没有 128 位整数类型，Verilator 的处理规则是：

> **任何超过 64 位的 packed 信号，Verilator 都拆成一个由 32 位字组成的数组，小端排列（bit 0 在第 0 个字）。**

也就是说，`i_a` 在 C++ 端变成了一个数组 `i_a[0], i_a[1], …, i_a[numArrays-1]`，每个元素是 32 位，拼起来才是完整的 128 位总线。所以测试平台必须**自己把 8 位的矩阵元素 4 个一组塞进每个 32 位字**。

这里有一个反直觉但极其重要的细节：**输出端口 `o_c` 的元素正好是 32 位**，和 Verilator 的字宽完全对齐，于是每个元素独占一个字、可以直接按下标读；而**输入端口 `i_a`/`i_b` 的元素是 8 位**，4 个才凑满一个字，必须手动移位打包。这正是本模块要讲清楚的「输入打包 vs 输出直读」的不对称。

#### 4.2.2 核心流程

`driveInputMatrices` 每拍都执行，但只有在「触发拍」才真正装载数据：

```
driveInputMatrices(dut):
    numArrays = ceil(N² / 4)            # N² 个 8 位元素 → 多少个 32 位字
    先把 i_a[0..numArrays-1]、i_b[0..numArrays-1] 全清 0   # 每拍先清空

    if 本拍是「触发拍」(posedge_cnt % 15 == 0 且已过复位):
        initializeInputMatrices()       # 随机生成 matrixA / matrixB
        displayMatrix('A'); displayMatrix('B')

        # 把二维矩阵按行展开成一维
        singleArrayA = [matrixA[0][0], matrixA[0][1], ..., matrixA[N-1][N-1]]
        singleArrayB = [matrixB[0][0], ..., matrixB[N-1][N-1]]

        # 4 个 8 位元素压进一个 32 位字，小端
        index = 0
        for i in 0..numArrays-1:
            for j in 0..3:
                i_a[i] |= singleArrayA[index] << (8*j)
                i_b[i] |= singleArrayB[index] << (8*j)
                index++
```

字数 `numArrays` 的推导：总位数为 \(N^2 \times 8\)，每个 32 位字装 4 个元素，故

\[
\text{numArrays} = \left\lceil \frac{N^2}{4} \right\rceil = \left\lfloor \frac{N^2 + 4 - 1}{4} \right\rfloor
\]

代码里 `(std::pow(N, 2) + 4 - 1) / 4` 正是这个向上取整的整数写法。N=4 时为 4 个字（128 位），N=5 时为 7 个字（200 位，注释里专门举了这个例子）。

打包顺序（小端）：第 `i` 个字的第 `j` 个字节放的是展开后第 `4*i + j` 个元素。由于 SV packed 数组「最左维是最高位」，而 C 端按 `i_a[i][j]`（字内字节）小端填充，两者位序恰好对齐——这也是为什么本设计行/列变换里特意做了「元素反转」（见 u2-l3），就是为了配合这种小端移出顺序。

#### 4.2.3 源码精读

**随机矩阵生成 `initializeInputMatrices`**：

[tb/tb_topSystolicArray.cpp:92-99](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L92-L99) —— 双重循环给 `matrixA[i][j]`、`matrixB[i][j]` 各填一个 `rand() % maxValue`（即 0~255）的随机 8 位无符号数。`main` 开头的 `srand(time(NULL))`（[tb/tb_topSystolicArray.cpp:184](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L184)）保证每次运行种子不同、矩阵不同。

**端口打包主体 `driveInputMatrices`**：

[tb/tb_topSystolicArray.cpp:101-142](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L101-L142) —— 整个打包逻辑都在这里。关键三段：

- 第 103 行算 `numArrays`；第 105-108 行先把所有输入字清 0（这样非触发拍输入自然是全 0，DUT 也只在 `i_validInput` 高时采样，行为一致）。
- 第 110-114 行：只在触发拍才生成并打印矩阵。
- 第 116-140 行：先把二维矩阵按行展开进 `singleArrayA`/`singleArrayB`（`std::vector<uint8_t>`），再用双重循环 `i_a[i] |= (singleArrayA[index] << (8*j))` 把每 4 个字节压进一个 32 位字。注意是用 `|=` 而不是 `=`——因为前面已清 0，用或运算逐字节填入，逻辑等价但更清晰地表达「把字节拼进字」。

[tb/tb_topSystolicArray.cpp:127-129](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L127-L129) 的注释明确点出了这套机制：「Verilator input ports are represented as 32 bit arrays … Eg - 7 32 bit arrays are required to represent a 5x5 matrix」。

**对照输出端口的不对称**：

[tb/tb_topSystolicArray.cpp:83-85](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L83-L85) —— 在 `displayMatrix('R')`（打印收到的结果矩阵）里，读取用的是 `dut->o_c[(N * i) + j]`，即把 `o_c` 当成 N² 个 32 位字的一维数组、按 `i*N+j` 下标直接取。这里**不用移位**，因为 `o_c` 每个元素本来就是 32 位，正好一个字，Verilator 把它平铺成 N² 个字。这正是输入需要打包、输出可以直接读的根本原因。

#### 4.2.4 代码实践

**实践目标**：手算一个 4×4 矩阵打包成 4 个 32 位字的结果，验证自己对「4 字节塞一字、小端」的理解。

**操作步骤**：

1. 任取一个 4×4 的 8 位矩阵 A（例如第一行 `01 02 03 04`，第二行 `05 06 07 08`，第三行 `09 0A 0B 0C`，第四行 `0D 0E 0F 10`，十六进制）。
2. 按行展开成 `singleArrayA = [01,02,03,04,05,06,07,08,09,0A,0B,0C,0D,0E,0F,10]`。
3. 按 `i_a[i] |= (singleArrayA[index] << (8*j))`（`j=0..3`）算出 `i_a[0]`、`i_a[1]`、`i_a[2]`、`i_a[3]`。

**需要观察的现象 / 预期结果**：

- `i_a[0] = 0x04030201`（第 0 字节 01 在最低位，第 1 字节 02，… 第 3 字节 04 在最高字节）。
- `i_a[1] = 0x08070605`，`i_a[2] = 0x0C0B0A09`，`i_a[3] = 0x100F0E0D`。

**结论**：每个字内部是「小端字节序」，即最早进入 `singleArrayA` 的元素落在最低字节。如果手算结果与此一致，说明你已掌握这套打包规则。若想进一步确认，可在 `driveInputMatrices` 打包完成后加一行打印 `printf("i_a[0]=%08x\n", dut->i_a[0]);`（标注为「示例代码」），跑 `make all` 观察控制台——本步骤标注「待本地验证」（需要 Verilator 环境）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 RTL 的输入位宽从 8 位改成 16 位（每个元素 16 位），`numArrays` 和打包循环各要怎么改？

**参考答案**：每个 32 位字只能装 2 个 16 位元素，所以 `numArrays = ceil(N²/2)`；打包循环的内层从 `j=0..3`、移位 `8*j` 改成 `j=0..1`、移位 `16*j`。同时 `maxValue = 2^16`、`matrixA`/`matrixB` 的元素类型要改成 `uint16_t`、`singleArrayA` 改成 `std::vector<uint16_t>`。这是一个牵一发动全身的改动点。

**练习 2**：为什么打包循环用 `|=` 而不是 `=`？换成 `=` 会出什么问题？

**参考答案**：每个字 `i_a[i]` 要装 4 个字节，必须把 4 次移位结果「拼」在一起。若用 `=`，后一次赋值会覆盖前一次，最终每个字只剩最后一个字节，前 3 个丢失。`|=`（配合事先清 0）才能把 4 个字节正确地或到各自的位段上。

---

### 4.3 期望结果计算与校验（verifyOutputMatrix）+ 波形 dump

#### 4.3.1 概念说明

数据喂进去了，阵列也算了 `3N-2` 拍，接下来要回答最关键的问题：**算得对不对？**

测试平台的思路非常朴素、也非常通用——**软件参考模型（reference model）法**：

1. 用纯 C++ 按定义三重循环算出期望结果 `matrixC = A × B`。
2. 盯着 DUT 的 `o_validResult`：它是单拍脉冲（见 u2-l4），亮起的那一拍 `o_c` 才是有效结果。
3. 在那一拍把 `o_c` 和 `matrixC` 逐元素比对，任何一个元素不等就打印错误、收到的矩阵、当前 `sim_time`，然后 `exit(EXIT_FAILURE)` 终止仿真。

如果整个 `MAX_SIM_TIME` 跑完都没 `exit`，`main` 末尾的 `exit(EXIT_SUCCESS)`（[tb/tb_topSystolicArray.cpp:220](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L220)）就让进程以成功码返回，Makefile 据此认为仿真「通过」。

与此同时，整个过程的每一拍信号都被录进 `waveform.vcd`，出错时可以回放定位。

#### 4.3.2 核心流程

**期望结果计算**（标准矩阵乘法定义）：

\[
\text{matrixC}[i][j] = \sum_{k=0}^{N-1} \text{matrixA}[i][k] \times \text{matrixB}[k][j]
\]

注意这里是无符号 8×8→16 位乘、32 位累加，与 RTL 的 PE 行为完全一致（见 u2-l1），所以参考模型与 DUT 在数学上等价，比对才有意义。

**校验时机**：`o_validResult` 由 DUT 在计数器计满 `MULT_CYCLES=3N-2` 时拉高一个 `i_clk` 周期。测试平台每个上升沿都查一次：

```
verifyOutputMatrix(dut):
    if o_validResult == 1 且 sim_time >= VERIF_START_TIME:
        calculateResultMatrix()        # 软件算期望
        displayMatrix('C')             # 打印期望矩阵
        incorrect = false
        for i in 0..N-1, j in 0..N-1:
            if o_c[(N*i)+j] != matrixC[i][j]:
                incorrect = true
        if incorrect:
            打印 ERROR；displayMatrix('R') 打印收到的矩阵；打印 sim_time
            exit(EXIT_FAILURE)
```

`VERIF_START_TIME=7` 是一道保险，避免仿真最初几拍（复位期间）任何瞬态被误判为错误。

**波形 dump**：在 `main` 开头一次性建好 VCD 写出对象，主循环每拍调一次 `dump(sim_time)` 把当前所有被 trace 的信号值追加写盘，仿真结束 `close()`。

#### 4.3.3 源码精读

**期望结果计算 `calculateResultMatrix`**：

[tb/tb_topSystolicArray.cpp:144-154](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L144-L154) —— 标准三重循环：`matrixC[i][j] += matrixA[i][k] * matrixB[k][j]`。这正是矩阵乘法的教科书定义，作为 DUT 的「黄金参考」。注意它只有在 `o_validResult` 亮起时才被调用，而不是每拍都算——因为输入矩阵在「触发拍」才更新，没必要重复计算。

**逐元素校验 `verifyOutputMatrix`**：

[tb/tb_topSystolicArray.cpp:156-181](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L156-L181) —— 进入条件是 `o_validResult==1 && sim_time>=VERIF_START_TIME`；进入后先算期望、打印期望矩阵，再用一个 `bool incorrect` 配合双重循环比对 `dut->o_c[(N*i)+j]` 与 `matrixC[i][j]`。这里再次出现 `o_c[(N*i)+j]` 这种「N² 个 32 位字」的直读访问（与 4.2 的输入打包形成对比）。一旦 `incorrect` 为真，就向 `std::cerr` 报错、调用 `displayMatrix('R')` 打印收到的矩阵、打印 `sim_time`，最后 `exit(EXIT_FAILURE)` 立即终止——这是一种「fail fast」策略：发现第一个错误就停，方便定位。

[tb/tb_topSystolicArray.cpp:160-162](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L160-L162) 的注释把输出端的 Verilator 表示点明：「Verilator represents the output matrix as n^2 bit arrays」（准确说是 n² 个 32 位字）。

**VCD 波形 dump 的建立**：

[tb/tb_topSystolicArray.cpp:188-195](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L188-L195) —— 四步：`Verilated::traceEverOn(true)` 打开 trace 基础设施；`new VerilatedVcdC` 建写出对象；`dut->trace(m_trace, 5)` 把 DUT 的所有信号挂到这个对象上（参数 `5` 是递归深度，足够覆盖顶层以下若干层）；`m_trace->open("waveform.vcd")` 开文件。

[tb/tb_topSystolicArray.cpp:212-213](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L212-L213) —— 主循环每拍 `m_trace->dump(sim_time)`，把当前所有信号值写入 VCD。

[tb/tb_topSystolicArray.cpp:218-220](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L218-L220) —— 仿真结束后 `m_trace->close()` 落盘关闭，`delete dut` 释放，`exit(EXIT_SUCCESS)` 以成功码返回。

**与 Makefile 的衔接**：

[tb/Makefile:37](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/Makefile#L37) —— verilate 命令里的 `--trace` 是让 Verilator 生成支持 `VerilatedVcdC` 的 C++ 代码；没有它，上面 `dut->trace(...)`、`m_trace->dump(...)` 都无法编译。换言之，**测试平台里的波形代码与 Makefile 里的 `--trace` 是一对配套开关**。

[tb/Makefile:27](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/Makefile#L27) —— 运行命令里的 `+verilator+rand+reset+2` 是一个运行时 plusarg，配合 verilate 时的 `--x-initial unique`（同在第 37 行），让未初始化的寄存器在复位前后取「确定但随机」的初值，从而把潜在的 X 传播问题（在两态模型里表现为不可预测的值）暴露出来——这是一种增强验证鲁棒性的手段。

#### 4.3.4 代码实践

**实践目标**：人为制造一次「校验失败」，观察 fail-fast 的错误输出长什么样，从而理解校验逻辑。

**操作步骤**（**注意：这是「破坏性」的临时改动，仅供学习，做完请还原**）：

1. 在 `verifyOutputMatrix` 的比对循环里，临时把 `if (dut->o_c[(N * i) + j] != matrixC[i][j])` 改成 `if (true)`（即强制判错）。
2. 跑 `cd tb && make all`。
3. 观察控制台输出。
4. **务必还原**第 1 步的改动（改回 `!= matrixC[i][j]`）。

**需要观察的现象**：

- 控制台会先打印一组随机生成的 Matrix A、Matrix B、Expected result Matrix。
- 紧接着打印 `ERROR: output matrix received is incorrect.`、Received matrix、`simtime: <某个值>`、一行 `****` 分隔符。
- 进程立即退出（`exit(EXIT_FAILURE)`），不再继续后续轮次。

**预期结果**：你会清楚地看到「软件参考模型（Expected result Matrix）」与「DUT 实际输出（Received matrix）」被并排呈现，以及失败发生在哪个 `sim_time`。这正是出错时定位问题的第一手信息。本步骤需要本地 Verilator 环境，若无可改为「源码阅读型实践」：直接阅读第 156-181 行，预测强制判错时哪些行会被执行、输出顺序是什么。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `calculateResultMatrix` 只在 `o_validResult==1` 时调用，而不是每拍都算？

**参考答案**：一是省算力——期望结果只在输入矩阵改变（触发拍）后才变化，没必要每个上升沿重算；二是对齐时机——`o_validResult` 亮起意味着 DUT 已经算完当前这轮 A×B，此时用同一份 `matrixA`/`matrixB` 算期望，才能保证与 DUT 的 `o_c` 比的是同一轮结果。

**练习 2**：如果 DUT 里 `o_validResult` 因为某个 bug 变成了「持续高电平」而不是「单拍脉冲」，这个测试平台会发生什么？

**参考答案**：`verifyOutputMatrix` 会在**每一个** `o_validResult==1` 的上升沿都触发一次校验。由于 `matrixA`/`matrixB` 在两次触发之间不变，期望矩阵不变，而 DUT 的 `o_c` 在计算中途是中间值，于是很快就会出现 `o_c != matrixC`，触发 `exit(EXIT_FAILURE)`。也就是说，这个测试平台对「validResult 时序错误」这类 bug 也有一定捕获能力——前提是 `o_c` 在中途确实不等于最终结果。

---

## 5. 综合实践

把三个最小模块串起来，完成规格里要求的核心实践：**把随机输入改成固定的一对 3×3 矩阵，同步把规模改成 N=3，跑通并确认 `o_c` 与手算期望一致。** 这会同时检验你对「端口打包」「校验时机」「参数化」三处的理解。

**实践目标**：用一个可手算的小例子，端到端验证测试平台的打包、驱动、参考模型、校验四条链路都正确。

**操作步骤**：

1. **改 RTL 规模**：打开 `rtl/topSystolicArray.sv`，把第 4 行的 `parameter int unsigned N = 4` 改成 `N = 3`。
2. **改 TB 规模**：打开 `tb/tb_topSystolicArray.cpp`，把第 16 行的 `#define N 4` 改成 `#define N 3`。
3. **改随机为固定**：在 `initializeInputMatrices`（第 92-99 行）里，把 `rand() % maxValue` 换成你选定的固定 3×3 矩阵的元素值。例如令

   \[
   A = \begin{bmatrix}1&2&3\\4&5&6\\7&8&9\end{bmatrix},\quad
   B = \begin{bmatrix}1&0&0\\0&1&0\\0&0&1\end{bmatrix}
   \]

   （B 取单位阵，便于手算：C 应当等于 A。）你可以用一个 `static` 的固定数组或直接 `matrixA[i][j] = 固定值;` 赋值（**示例代码**，请替换实际数值）。
4. **手算期望**：因为 B 是单位阵，期望 `matrixC = A`，即 `C[0][0]=1, C[0][1]=2, …, C[2][2]=9`。也可让 `calculateResultMatrix` 自己算，作为交叉校验。
5. 跑 `cd tb && make all`（先 `make clean` 确保全量重编）。

**需要观察的现象**：

- 控制台打印的 Matrix A、Matrix B 是你设定的固定值（每次运行都一样，因为不再随机）。
- 打印的 Expected result Matrix 与你手算的 C 一致。
- 仿真**不报错**地跑完 `MAX_SIM_TIME`，进程以成功码退出（控制台没有 `ERROR: output matrix received is incorrect.`）。
- 用 GTKWave 打开 `waveform.vcd`，能看到 `o_validResult` 每隔 `assertValidInput=3*3+3=12` 个上升沿出现一个单拍脉冲，脉冲那拍 `o_c` 的 9 个字正是 `1,2,3,4,5,6,7,8,9`。

**预期结果**：固定输入下结果确定可手算，DUT 输出与软件参考模型一致，验证通过。

**若想加深难度（可选）**：把 B 换成一个非单位阵，例如

\[
B = \begin{bmatrix}1&2&3\\4&5&6\\7&8&9\end{bmatrix}
\]

此时 `C[i][j] = sum_k A[i][k]*B[k][j]` 需要逐项手算（例如 `C[0][0] = 1*1+2*4+3*7 = 30`），用来检验你对参考模型三重循环的理解。本综合实践需要本地 Verilator 环境；若本地无环境，标注为「待本地验证」，但步骤 1-4 的源码改动与手算部分可独立完成。

**做完记得还原**：把 N 改回 4、把随机生成改回 `rand() % maxValue`，避免影响后续讲义（u3-l2 会专门讲参数化改 N 的完整流程）。

---

## 6. 本讲小结

- Verilator C++ 测试平台的核心是一个**手动节拍发生器**：用 `i_clk ^= 1` 造时钟、用 `sim_time`/`posedge_cnt` 两条时间轴控制复位（`dut_reset` 在 `sim_time=3,4` 拉高 `i_arst`）与周期性触发（`i_validInput` 每 `3N+3=15` 个上升沿拉一拍）。
- **宽端口打包**是最容易出错的地方：Verilator 把超过 64 位的 packed 信号拆成若干 32 位字（小端），`i_a`/`i_b` 因为元素是 8 位，必须用 `i_a[i] |= (elem << (8*j))` 把 4 个字节塞进一个字；而 `o_c` 因为元素正好 32 位，可以按 `o_c[(N*i)+j]` 直接读——这种「输入要打包、输出可直读」的不对称是本讲的核心机制。
- 验证采用**软件参考模型法**：`calculateResultMatrix` 用三重循环算期望 `C=A×B`，`verifyOutputMatrix` 在 `o_validResult` 单拍脉冲亮起时逐元素比对 `o_c` 与 `matrixC`，发现不一致就 fail-fast `exit(EXIT_FAILURE)`，否则 `main` 末尾 `exit(EXIT_SUCCESS)`。
- VCD 波形靠 `VerilatedVcdC` 建立、每拍 `dump(sim_time)`、结束 `close()`；它与 `tb/Makefile:37` 的 `--trace` 选项是一对配套开关，运行时的 `+verilator+rand+reset+2`（`tb/Makefile:27`）则给未初始化寄存器随机初值以增强鲁棒性。
- 整个测试平台高度依赖宏 `N`：改规模必须同时改 RTL 参数 `N` 和 TB 宏 `N`，`numArrays`、`assertValidInput`、`MAX_CYCLES` 等派生量会自动适应，但端口打包/解包逻辑只对「元素位宽固定为 8/32」成立。

---

## 7. 下一步学习建议

- 下一讲 **u3-l2 参数化设计与 SystemVerilog 编码风格** 会把「改 N」这件事系统化，讲清楚参数 `N` 如何驱动整个阵列、packed 多维数组在端口里的用法、以及 `default_nettype none`/`` `resetall ``/`$error` 等编码约定，与本讲的「TB 宏 N ↔ RTL 参数 N 同步」紧密呼应。
- 如果你对验证方法学感兴趣，建议接着读 **u3-l3 Quartus FPGA 综合与资源利用**，了解同一份 RTL 从仿真走向真实器件时（Cyclone V）的 DSP 映射与资源代价。
- 想动手深挖的读者，可在本讲基础上尝试：把参考模型从「无符号」改成「有符号」（配合 u3-l4 的有符号 MAC 改造），或给测试平台加一个「覆盖率」计数（累计校验了多少轮、最大元素值是多少），作为二次开发练手。
