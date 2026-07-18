# 随机序列生成与自检 rand_gen.v

## 1. 本讲目标

本讲是「架构取舍、综合与扩展」单元的收尾篇，主题是一个**独立于解码主链路**的小工具：伪随机序列发生器 `rand_gen.v` 及其自检测试台 `rand_gen_tb.v`。

它不参与 802.11 包的正常解码，而是项目里**「如何写测试激励」的范例**：当你想给 `descramble`、`crc32`、`bits_to_bytes` 这类模块喂一段足够长、足够「乱」的输入，又不想手写一张大表时，就用一个 LFSR 现场造。

学完后你应当能够：

- 说清 LFSR（线性反馈移位寄存器）产生伪随机序列的原理，并能把它和 u3-l6 讲过的 802.11 扰码器（`descramble.v`）对照起来理解。
- 逐行读懂 `rand_gen.v`：128 位移位寄存器、4 抽头反馈、按位拼装成字节输出。
- 看懂 `rand_gen_tb.v` 的「时钟 + 复位 + 使能 + 落盘」四件套写法，并知道它**不在 Makefile 里**、必须手动用 `iverilog` 跑。
- 把「LFSR 造激励」的思路迁移到 `descramble` 的边界自检上，自己写一个带参考模型（reference model）的最小自检测试台。

## 2. 前置知识

阅读本讲前，你需要具备（对应前置讲义摘要）：

- **LFSR 与扰码器**：u3-l6 已讲过 `descramble.v` 用 7 级 LFSR、生成多项式 \(S(x)=x^7+x^4+1\)、反馈 `state[6]^state[3]`、并用「直装法」把接收前 7 位当种子装入状态寄存器。本讲的 `rand_gen.v` 是同一族思想的放大版（128 级），务必先回忆 u3-l6。
- **仿真三件套与测试台角色**：u1-l2 讲过 `iverilog`/`vvp`/`gtkwave`；u5-l3 讲过 `dot11_tb.v` 作为「激励发生器 + 探针」的双重角色、`$readmemh` 加载样本、`$fwrite` 落盘、`$dumpvars` 出波形。本讲的 `rand_gen_tb.v` 是一个**更小、更纯**的测试台，正好用来复习这套套路。
- **「数据 + strobe」握手**：u1-l4/u3-l2 反复强调的全项目统一风格。注意 `rand_gen` 是个**例外**——它只有 `enable`，没有 strobe，因为它不是流水线的一环，而是自由运行的激励源。

几个本讲要用到的术语：

- **LFSR（Linear Feedback Shift Register，线性反馈移位寄存器）**：把若干寄存器位经 XOR 后回送进输入端的移位寄存器；给定种子后产生的序列完全确定，但统计上「看起来随机」，故名「伪随机」。
- **抽头（tap）**：参与 XOR 反馈的那些位。
- **本原多项式（primitive polynomial）**：若抽头位置取自一条本原多项式，LFSR 可遍历除全 0 外的所有状态，周期达到最大值 \(2^n-1\)，称为 m 序列（最大长度序列）。
- **种子（seed）**：LFSR 的初值。全 0 种子会让 LFSR 永远停在 0，故必须非零。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 是否在编译清单里 |
|---|---|---|
| [verilog/rand_gen.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rand_gen.v) | 128 位 LFSR 伪随机字节发生器（被测设计 DUT） | **否**（独立工具） |
| [verilog/rand_gen_tb.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rand_gen_tb.v) | 例化 `rand_gen`、产生时钟/复位/使能、把结果落盘的测试台 | **否**（Makefile 默认编的是 `dot11_tb.v`） |
| [verilog/descramble.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/descramble.v) | 7 位 LFSR 解扰器，作为「最小的 LFSR 范例」与综合实践的对接对象 | 是（`dot11_modules.list` 第 25 行） |
| [verilog/ofdm_decoder.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v) | 例化 `descramble` 的上层，说明真实场景下解扰器如何被驱动 | 是 |
| [verilog/Makefile](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/Makefile) | 默认只编译/仿真 `dot11_tb.v`，故 `rand_gen_tb` 需手动跑 | —— |

一句话定位：`rand_gen` + `rand_gen_tb` 是项目自带的「LFSR 教学/工具」小样，和主干解码流水线**完全解耦**，可以单独编译、单独跑。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先讲 LFSR 通用原理（用 `descramble.v` 当最简样例），再讲 `rand_gen.v` 这个 128 位放大版，最后讲 `rand_gen_tb.v` 的测试台写法。

### 4.1 LFSR 原理：用移位寄存器造伪随机

#### 4.1.1 概念说明

很多验证场景需要「大量、无规律」的输入：压测 CRC、烤扰码器、给数据通路灌随机激励。手写一张大表既笨又易错。LFSR 提供了一条近乎零成本的捷径：**几个触发器 + 一个 XOR 门**，就能源源不断地产出看起来随机的比特流。

LFSR 的核心思想：

- 维护一个 n 位的state。
- 每个时钟把 state 移一位。
- 移出去/移进来的那一位，由 state 中若干「抽头位」的 XOR 计算得出。
- 因为反馈是「线性的」（只用了 XOR），所以给定种子后，后续序列**完全确定**、可复现——这正是测试想要的：出 bug 时能精确定位到第几个输入。

它「伪随机」的来源是：当抽头选自一条本原多项式时，序列的周期可达最大值 \(2^n-1\)，且在统计上接近 0/1 各半、游程分布合理。

两种常见接法：

- **Fibonacci 型**（外接 XOR）：把若干高位抽头 XOR 后，作为新位送回最低位；state 整体左移。OpenOFDM 的 `rand_gen.v` 和 `descramble.v` 都是这一型。
- **Galois 型**（逐位 XOR）：每个抽头位置和反馈线就地 XOR；速度更快、常用于高速电路。本项目未使用。

#### 4.1.2 核心流程

一个 n 位 Fibonacci LFSR 每拍做的事（伪代码）：

```
feedback = state[t1] ^ state[t2] ^ ...        // 抽头位的异或
state    = {state[n-2:0], feedback}           // 左移 1 位，feedback 进 bit0
output   = feedback                           // 或输出 state[MSB]，看实现
```

最大周期（当抽头为本原多项式时）：

\[
T_{\max} = 2^n - 1
\]

直观理解：n 位 state 共 \(2^n\) 种组合，但全 0 是「死锁态」（反馈永远 0），所以能走的最长环路是 \(2^n-1\)。

#### 4.1.3 源码精读

先看项目里**最简的 LFSR**——`descramble.v` 的 7 位扰码器，用它建立直觉。反馈只有两个抽头：

[verilog/descramble.v:19-19](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/descramble.v#L19-L19) —— `wire feedback = state[6] ^ state[3];`：这就是 802.11 生成多项式 \(S(x)=x^7+x^4+1\) 的硬件实现。`state[6]` 对应 \(x^7\) 项，`state[3]` 对应 \(x^4\) 项，常数 1 隐含在「反馈本身」里。

[verilog/descramble.v:37-41](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/descramble.v#L37-L41) —— 初始化（直装法）完成后的稳态逻辑：

- `out_bit <= feedback ^ in_bit;`：输出 = LFSR 序列（feedback）⊕ 输入位，即「解扰」。
- `state <= {state[5:0], feedback};`：典型的 Fibonacci 左移——把 `state[5:0]` 整体左移 1 位，腾出的 bit0 填入 feedback。

这条 `state <= {state[5:0], feedback}` 就是上面伪代码的 Verilog 写法，请把它记作本项目的「LFSR 标准动作」——等会儿在 `rand_gen.v` 里会看到一模一样的模式，只是位宽换成 128。

> 关键点：`descramble` 用前 7 位「装种子」（直装法），装满前不出有效输出；这与 `rand_gen`「复位即给种子、随后自由运行」是两种不同的初始化策略，后面会对比。

#### 4.1.4 代码实践

**实践目标**：在纸上验证 `descramble` 的反馈多项式确实对应 \(x^7+x^4+1\)，并手算一个 7 位序列的前几拍。

**操作步骤**：

1. 打开 [verilog/descramble.v:19-19](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/descramble.v#L19-L19)，确认 `feedback = state[6] ^ state[3]`。
2. 设种子 `state = 7'b1010101`（即 state[6..0] = 1,0,1,0,1,0,1）。
3. 逐拍计算 feedback 与下一拍 state。

**需要观察的现象（手算）**：

- 第 1 拍：feedback = state[6] ^ state[3] = 1 ^ 0 = 1；新 state = `{state[5:0], 1}` = `7'b0101011`。
- 第 2 拍：feedback = 0 ^ 1 = 1；新 state = `7'b1010111`。
- 第 3 拍：feedback = 1 ^ 0 = 1；新 state = `7'b0101111`。

**预期结果**：feedback 序列以 `1,1,1,...` 开头；state 持续左移。若你把它和 `rand_gen_tb` 跑出来的随机字节对照，会发现「左移 + feedback 回填 bit0」是共同的骨架。

**待本地验证**：可用下文 4.3.4 的测试台跑出 `descramble` 在全 0 输入下的 `out_bit`，与你手算的 feedback 序列逐一比对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LFSR 的种子不能是全 0？

**参考答案**：全 0 时所有抽头都是 0，feedback = 0⊕0⊕… = 0，新 state 仍是全 0，序列永远停在 0，称为「死锁态」。所以 LFSR 的有效状态空间是 \(2^n-1\) 而非 \(2^n\)。

**练习 2**：`descramble` 的反馈是 `state[6] ^ state[3]`。若误写成 `state[6] ^ state[2]`，周期通常会怎样变化？

**参考答案**：`x^7+x^4+1` 是本原多项式，周期最大为 \(2^7-1=127\)；改成 `state[6]^state[2]`（对应 \(x^7+x^5+1\)）一般不是本原多项式，周期会显著变短、统计特性变差。这就是「抽头位置不能乱改」的原因。

---

### 4.2 rand_gen.v：128 位伪随机字节生成器

#### 4.2.1 概念说明

`rand_gen` 要解决的问题是：**给一个需要大量随机字节的场景，持续吐出 8 位伪随机数**。它的做法是把 LFSR 的位宽放大到 128 位——这样周期可达 \(2^{128}-1\)，是一个天文数字（约 \(3.4\times10^{38}\)），仿真里根本跑不完一圈，等价于「无限不重复」的激励源。

它与 `descramble` 的对比：

| 维度 | `descramble.v` | `rand_gen.v` |
|---|---|---|
| 用途 | 解扰（802.11 协议要求） | 造测试激励（项目工具） |
| LFSR 位宽 | 7 位 | 128 位 |
| 反馈抽头 | `state[6]^state[3]`（2 抽头） | `random[127/125/100/98]`（4 抽头） |
| 初始化 | 接收前 7 位直装种子 | 复位写固定种子 `0x55…55` |
| 输出 | 1 位 `out_bit` + strobe | 8 位 `rnd`，每 8 拍凑满一字节 |
| 握手 | 数据 + strobe（流水线风格） | 仅 `enable`，自由运行 |

注意最后一行：`rand_gen` 是全项目里少有的**没有 strobe** 的模块，因为它不是流水线里的一环，而是自由运行的激励源。

#### 4.2.2 核心流程

`rand_gen` 每个时钟（`enable` 有效时）做三件事：

```
feedback = random[127] ^ random[125] ^ random[100] ^ random[98]   // 4 抽头
random  <= {random[126:0], feedback}                               // 128 位左移
rnd[bit_idx] <= feedback                                           // 把这一位拼进输出字节
bit_idx  <= bit_idx + 1                                            // 0..7 循环
```

复位时：

```
random  <= {128{4'b0101}}    // 即 128'h5555...55，非零种子
bit_idx <= 0
rnd     <= 0
```

数学上，128 位本原 LFSR 的最大周期：

\[
T_{\max} = 2^{128} - 1 \approx 3.4 \times 10^{38}
\`

输出侧的细节：`rnd` 是 8 位寄存器，但每个时钟只改写其中 1 位（由 3 位 `bit_idx` 索引）。所以**每 8 个时钟才凑满一个全新的字节**；中途读 `rnd` 会看到一个「正在逐位更新」的半成品。这是 `rand_gen_tb` 落盘策略要注意的地方（见 4.3）。

#### 4.2.3 源码精读

[verilog/rand_gen.v:10-13](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rand_gen.v#L10-L13) —— 位宽与反馈定义：

- `localparam LFSR_LEN = 128;`：128 位 LFSR。
- `wire feedback = random[127] ^ random[125] ^ random[100] ^ random[98];`：4 抽头 XOR。这组抽头位置（127/125/100/98）按本原多项式选取，目的是拿到接近 \(2^{128}-1\) 的超长周期。注意它和 `descramble` 的「2 抽头」是同一套思想，只是位宽和多项式不同。

[verilog/rand_gen.v:17-20](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rand_gen.v#L17-L20) —— 复位分支：

- `random <= {LFSR_LEN{4'b0101}};`：把 `4'b0101` 复制 32 份拼成 128 位，即 `0x5555…55`（奇数位为 1）。这是一个精心选的**非零**种子，避免 LFSR 死锁。
- `bit_idx <= 0; rnd <= 0;`：输出位指针与输出字节清零。

[verilog/rand_gen.v:21-25](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rand_gen.v#L21-L25) —— 使能分支，三个动作一拍完成：

- `random <= {random[LFSR_LEN-2:0], feedback};`：即 `{random[126:0], feedback}`，128 位左移 1 位、bit0 填 feedback。与 `descramble` 的 `state <= {state[5:0], feedback}` 完全同构。
- `rnd[bit_idx] <= feedback;`：把当拍 feedback 写进 `rnd` 的第 `bit_idx` 位。`bit_idx` 是 3 位，取值 0..7，正好覆盖一个字节。
- `bit_idx <= bit_idx + 1;`：3 位计数器自增，到 7 后自然回绕到 0，开始拼下一个字节。

> 一个易被忽略的点：`feedback` 来自**当前** `random`（组合逻辑），而 `random` 的更新是**下一拍**才生效。所以「写出 feedback」和「移位」用的是同一组旧 state，逻辑自洽。

#### 4.2.4 代码实践

**实践目标**：手动跑通 `rand_gen_tb`，确认它能持续产出伪随机字节，并体会「`rnd` 每 8 拍才完整刷新一次」。

**操作步骤**：

1. 确认工具链：`iverilog -V`（本讲环境未安装，**待本地验证**）。
2. 进入目录并准备输出目录：

   ```bash
   cd verilog
   mkdir -p sim_out           # rand_gen_tb 把文件写到 ./sim_out/rand_gen.txt
   iverilog -o rand_gen.out rand_gen.v rand_gen_tb.v
   vvp rand_gen.out
   ```

   注意：`rand_gen_tb` **不在** `dot11_modules.list` 里，`Makefile` 的 `TESTBENCH` 也写死成 `dot11_tb.v`（见 [verilog/Makefile:18-19](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/Makefile#L18-L19)），所以 `make simulate` 不会跑它，必须像上面那样手动指定两个 `.v` 文件。
3. 跑完后看 `sim_out/rand_gen.txt`，每行是一个十进制字节。

**需要观察的现象**：

- 文件应有数百万行（测试台跑 `#10000000` 个时间单位，时钟半周期 `#1`，约 500 万拍）。
- 把前 16 行并排看：因为 `rnd` 是逐位更新的，**连续 8 行其实是同一个字节在被一位一位地改写**；第 9 行才是一个全新的字节。这是 `rand_gen` 「先拼装、后输出」的副作用。

**预期结果**：字节序列看不出明显短周期（128 位 LFSR 周期远超仿真长度），统计上 0/1 比例接近 1:1。

**待本地验证**：本讲环境未安装 iverilog，上述现象需你在本地实跑确认。

#### 4.2.5 小练习与答案

**练习 1**：`rand_gen` 复位后 `random` 的值是多少？为什么不能初始化为 0？

**参考答案**：复位值为 `{128{4'b0101}}` = `128'h5555…55`。若初始化为全 0，四个抽头都是 0，feedback 恒为 0，state 永远不变，`rnd` 也永远为 0——LFSR 死锁。

**练习 2**：若把 `LFSR_LEN` 从 128 改成 8，`feedback` 表达式还能用 `random[127]^...` 吗？

**参考答案**：不能。改成 8 位后，`random[127]` 等高位索引超出向量范围，综合/仿真会出错或返回 x。改位宽必须同步把抽头位置换成 8 位本原多项式的抽头（例如 `random[7]^random[5]^random[4]^random[3]`），并相应调整种子。

**练习 3**：`rnd` 每 8 拍才完整更新一次，但测试台每拍都 `$fwrite`。这会导致落盘文件里有什么特点？

**参考答案**：同一个「正在拼装中」的字节会被连续写 8 次，只是每次某一位不同；真正全新的字节每 8 行才出现一次。因此直接统计 `rand_gen.txt` 时要注意这个「8 倍冗余」，或改成只在 `bit_idx==7` 那拍落盘。

---

### 4.3 rand_gen_tb.v：自检测试台的写法

#### 4.3.1 概念说明

`rand_gen_tb.v` 是一个**极简但完整**的测试台范本：它不依赖任何 coregen IP、不依赖 `dot11_modules.list`、不需要 Xilinx 仿真库，只要有 iverilog 就能跑。它的角色就是 u5-l3 讲过的两件事——**激励发生器**（产生 clock/reset/enable）和**探针**（把 `rnd` 落盘）。

值得强调的是它的「诚实」：这个测试台**不做任何断言**，只把输出全量 dump 到文件，校验工作交给离线分析。这是一种合法但偏弱的验证策略——适合「先生成大量数据、再事后统计」的场景。本讲的综合实践会演示如何给它**加一个参考模型做在线自检**，把它升级成真正的 self-checking testbench。

它在项目中的定位也值得注意：`rand_gen` / `rand_gen_tb` 是**孤立**的两个文件，grep 全仓库只有它们互相引用，没有被 `dot11_tb.v` 或任何解码模块例化。它纯粹是一段「教学/工具」代码。

#### 4.3.2 核心流程

测试台的骨架（伪代码）：

```
initial:
    clock = 0; reset = 1; enable = 0;          // 上电复位
    fd = $fopen("./sim_out/rand_gen.txt", "w") // 打开输出文件
    #10  reset = 0; enable = 1;                // 释放复位、开始使能
    #10000000 $finish;                          // 跑足够久

always #1 clock = ~clock;                       // 周期 2 的时钟

always @(posedge clock)
    if (enable) $fwrite(fd, "%d\n", rnd);       // 每拍把字节落盘
```

复用了 u5-l3 总结的测试台套路：`$fopen` 开文件、门控 `$fwrite` 落盘、`always #1` 造时钟、`initial` 控制复位与仿真结束。

#### 4.3.3 源码精读

[verilog/rand_gen_tb.v:11-17](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rand_gen_tb.v#L11-L17) —— 例化被测设计 `rand_gen inst (...)`，端口一一对接。注意 DUT 名是 `rand_gen`、实例名是 `inst`，`rnd` 接成 `wire`（因为 DUT 内部是 `output reg`，测试台侧只读）。

[verilog/rand_gen_tb.v:20-31](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rand_gen_tb.v#L20-L31) —— `initial` 块，测试台的「控制中心」：

- `clock = 0; reset = 1; enable = 0;`：上电即复位，DUT 处于 `S_WAIT` 式的静止态。
- `fd = $fopen("./sim_out/rand_gen.txt", "w");`：以写方式打开输出文件，路径写死成 `./sim_out/`，所以**必须先建好 `sim_out/` 目录**，否则 `$fopen` 返回 0、后续 `$fwrite` 静默失败。
- `#10 reset = 0; enable = 1;`：第 10 个时间单位释放复位、拉高使能，DUT 开始移位产随机数。
- `#10000000 $finish;`：仿真跑 1000 万个时间单位后结束。

[verilog/rand_gen_tb.v:34-36](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rand_gen_tb.v#L34-L36) —— 时钟发生：`always #1 clock <= ~clock;`，半周期 1 个时间单位，整周期 2。注意它用非阻塞 `<=`，与 `initial` 里的阻塞赋值分工明确（激励用阻塞、时钟用非阻塞，是常见写法）。

[verilog/rand_gen_tb.v:38-42](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rand_gen_tb.v#L38-L42) —— 探针：`always @(posedge clock) if (enable) $fwrite(fd, "%d\n", rnd);`。和 u5-l3 讲的 `dot11_tb.v` 落盘手法一致——用 `%d\n` 每拍写一个十进制字节。如前所述，这会写出 8 倍冗余（同一字节被逐位改写 8 次）。

> 小结：这个测试台是「最小可运行」范本。它的不足是没有 self-check，下面实践里补上。

#### 4.3.4 代码实践

**实践目标**：给 `rand_gen_tb` 加一个**参考模型**做在线自检，验证 DUT 的 feedback 行为正确。

**操作步骤**：在测试台里再维护一份「影子 LFSR」`random_ref`，每拍用同样的反馈多项式推进，再把 DUT 每拍新写进 `rnd` 的位与参考值比对。

下面是**示例代码**（非项目原有代码，仅供你添加到自己的测试台副本）：

```verilog
// 示例代码：rand_gen_tb 的自检扩展（加在 rand_gen_tb.v 的基础上）
reg [127:0] random_ref;
wire feedback_ref = random_ref[127] ^ random_ref[125] ^ random_ref[100] ^ random_ref[98];
integer errors = 0;

initial begin
    random_ref = {128{4'b0101}};   // 与 DUT 复位种子一致
end

always @(posedge clock) begin
    if (reset) begin
        random_ref <= {128{4'b0101}};
    end else if (enable) begin
        // 1) feedback 必须一致
        if ((inst.feedback) !== feedback_ref)
            errors = errors + 1;
        // 2) 每拍写入 rnd 的那一位必须等于参考 feedback
        if (inst.rnd[inst.bit_idx] !== feedback_ref)
            errors = errors + 1;
        random_ref <= {random_ref[126:0], feedback_ref};
    end
end

initial begin
    #10000000 $display("SELF-CHECK errors = %d", errors);
end
```

**需要观察的现象**：仿真结束时应打印 `SELF-CHECK errors = 0`。

**预期结果**：若 DUT 与参考模型完全一致，`errors` 始终为 0；若你故意把 DUT 的某个抽头改错（例如把 `random[100]` 换成 `random[101]`），`errors` 会立刻飙升——这就是 self-checking testbench 的价值：**不用人眼看波形，机器自动报错**。

**待本地验证**：本讲环境未装 iverilog，请本地实跑。注意 `inst.feedback`、`inst.rnd`、`inst.bit_idx` 是层次化引用 DUT 内部信号，iverilog 支持。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `rand_gen_tb` 必须先 `mkdir -p sim_out` 才能跑？

**参考答案**：因为 `$fopen("./sim_out/rand_gen.txt", "w")` 不会自动创建父目录；若 `sim_out/` 不存在，`$fopen` 返回 0（空句柄），后续 `$fwrite` 写到句柄 0（通常是标准输出）或静默失败，拿不到结果文件。

**练习 2**：上面的自检扩展里，为什么比较的是「每拍写入的那一位」而不是「整个 `rnd` 字节」？

**参考答案**：`rnd` 每 8 拍才完整刷新，且其余 7 位是历史值；逐拍比较整个字节会因为「位指针位置」错位而误报。比较「当拍新写入的那一位（`rnd[bit_idx]`）」与参考 feedback，才是 apples-to-apples 的对照。

**练习 3**：`rand_gen_tb` 没有 strobe，而 `dot11_tb` 全是 strobe 握手。为什么这里可以省？

**参考答案**：`rand_gen` 是自由运行的激励源，每个使能时钟都产出一个有效位，没有「数据无效」的拍子，所以用 `enable` 选通即可，不需要 strobe 区分有效/无效数据；`dot11` 主链路是流水线，数据时有时无，必须用 strobe 标记有效拍。

---

## 5. 综合实践

把本讲三块知识串起来：写一个**产生伪随机 16 位 I/Q 样本的小生成器**，并把它接到 `descramble` 输入端做一个最小自检测试台，验证 LFSR 种子初始化与 feedback 的正确性。

> 之所以选 `descramble` 作为对接对象，是因为它是项目里**最简单、最纯粹**的 LFSR 模块（7 位、无依赖、单 bit 输入），最适合做自检演示。真实工程里 descramble 由 ofdm_decoder 驱动（见 [verilog/ofdm_decoder.v:93-103](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L93-L103)），这里我们绕开上层、直接喂数据。

**任务拆成三步**：

### 第 1 步：写一个 16 位伪随机生成器 `rand16`

仿照 `rand_gen.v`，把位宽缩到 16、抽头换成 16 位本原多项式。下面是**示例代码**：

```verilog
// 示例代码：rand16.v —— 16 位伪随机 I/Q 样本生成器（仿 rand_gen.v）
module rand16 (
    input  clock, enable, reset,
    output reg [15:0] rnd16,     // 16 位「样本」
    output reg        valid      // 凑满 16 位时拉高一拍
);
    localparam LFSR_LEN = 16;
    // 16 位本原多项式 x^16 + x^15 + x^13 + x^4 + 1 的抽头
    reg [LFSR_LEN-1:0] random;
    wire feedback = random[15] ^ random[14] ^ random[12] ^ random[3];
    reg [3:0] bit_idx;

    always @(posedge clock) begin
        if (reset) begin
            random  <= 16'hACE1;          // 任意非零种子
            bit_idx <= 0;
            rnd16   <= 0;
            valid   <= 0;
        end else if (enable) begin
            random         <= {random[LFSR_LEN-2:0], feedback};  // 左移 + feedback 回填
            rnd16[bit_idx] <= feedback;                            // 逐位拼装
            valid          <= (bit_idx == 4'hF);                   // 拼满 16 位
            bit_idx        <= bit_idx + 1;
        end
    end
endmodule
```

要点对照：`{random[14:0], feedback}` 与 [rand_gen.v:22](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rand_gen.v#L22) 完全同构；`rnd16[bit_idx] <= feedback` 与 [rand_gen.v:23](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rand_gen.v#L23) 同构，只是位宽和指针位数不同。

### 第 2 步：从 `rand16` 派生单 bit 流，喂给 `descramble`

`descramble` 的输入是单 bit `in_bit` + `input_strobe`（见 [verilog/descramble.v:7-8](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/descramble.v#L7-L8)）。最简单的做法：直接把 `rand16` 内部的 `feedback`（或某一位）拉出来当 `in_bit`，每个使能拍都给一个 `input_strobe`。这样 descramble 前 7 拍自动「直装种子」，之后持续解扰。

> 注意：`rand16` 的 `feedback` 是 `wire`，要对外暴露需在模块里加一个 `output wire bit_out = feedback;`，或干脆在测试台里用层次化引用 `dut_src.random[0]` 之类。下面测试台用层次化引用以避免改 `rand16`。

### 第 3 步：写自检测试台，验证种子初始化与 feedback

核心思路：测试台里跑一个**参考 7 位 LFSR**，与 `descramble` 内部 `state` 逐拍对照。下面是**示例代码**骨架（请自行补全时序细节）：

```verilog
// 示例代码：descramble_self_tb.v —— 最小自检测试台
module descramble_self_tb;
    reg clock, reset, enable, in_bit, input_strobe;
    wire out_bit, output_strobe;

    // 被测：descramble
    descramble dut_d (
        .clock(clock), .enable(enable), .reset(reset),
        .in_bit(in_bit), .input_strobe(input_strobe),
        .out_bit(out_bit), .output_strobe(output_strobe)
    );

    // 激励源：rand16（只用来产随机 bit）
    wire clk_feedback;
    rand16 dut_r (
        .clock(clock), .enable(enable), .reset(reset),
        .rnd16(), .valid()
    );
    // 层次化引用 rand16 的 feedback 当单 bit 激励（不改 rand16 端口）
    // 注意：iverilog 支持跨模块层次引用，语法为实例名.信号名
    // 此处需你在 rand16 里把 feedback 通过 output 引出，或在此引用 dut_r.random

    // 参考模型：7 位 LFSR，与 descramble.feedback 同构
    reg [6:0] state_ref;
    wire fb_ref = state_ref[6] ^ state_ref[3];
    integer errors = 0;
    integer i;

    initial clock = 0;
    always #1 clock = ~clock;

    initial begin
        reset = 1; enable = 0; input_strobe = 0; in_bit = 0; state_ref = 0;
        #10 reset = 0; enable = 1;

        // (A) 种子初始化自检：手工喂 7 位种子 7'b1010101
        //     descramble 把第 i 个 in_bit 装入 state[6-i]，故先发位落 state[6]
        for (i = 0; i < 7; i = i + 1) begin
            @(posedge clock);
            in_bit = 7'b1010101 >> (6 - i) & 1;  // 依次发 MSB..LSB
            input_strobe = 1;
            state_ref[6 - i] = in_bit;           // 参考模型同步装种子
        end
        @(posedge clock); input_strobe = 0;

        // 装完种子，dut_d.state 应等于 7'b1010101
        if (dut_d.state !== 7'b1010101)
            $display("FAIL: seed init, state=%b", dut_d.state);
        else
            $display("PASS: seed init");

        // (B) feedback 正确性自检：喂全 0，则 out_bit 应等于参考 feedback
        in_bit = 0;
        for (i = 0; i < 100; i = i + 1) begin
            @(posedge clock);
            input_strobe = 1;
            #1; // 让 out_bit 稳定
            if (output_strobe && (out_bit !== fb_ref))
                errors = errors + 1;
            state_ref = {state_ref[5:0], fb_ref};  // 参考模型推进
        end

        $display("SELF-CHECK feedback errors = %d", errors);
        $finish;
    end
endmodule
```

**需要观察的现象**：

- 打印 `PASS: seed init`——说明 descramble 的直装法把 7 位种子正确装入 `state`。
- 打印 `SELF-CHECK feedback errors = 0`——说明 feedback = `state[6]^state[3]` 与参考模型逐拍一致。

**预期结果**：两项都通过。若 (A) 失败，说明你对 `state[6-bit_count]` 的位序理解有误；若 (B) 失败，说明参考模型的移位方向或 feedback 抽头与 DUT 不一致。

**待本地验证**：上述为教学骨架，时序对齐（`#1` 采样点、阻塞/非阻塞搭配）需你在本地用 iverilog 调通；本讲环境未安装 iverilog，无法替你实跑。把 `rand16.v`、`descramble.v`、`descramble_self_tb.v` 放同一目录，用 `iverilog -o s.out rand16.v descramble.v descramble_self_tb.v && vvp s.out` 运行。

## 6. 本讲小结

- **LFSR 是最廉价的伪随机源**：几个触发器 + 一个 XOR，给定非零种子就能产出周期可达 \(2^n-1\) 的可复现序列；抽头必须取自本原多项式才能拿到最大周期，不能随手改。
- **`rand_gen.v` = 128 位 Fibonacci LFSR**：4 抽头 `random[127/125/100/98]`、左移 + feedback 回填 bit0、复位种子 `0x55…55`、用 3 位 `bit_idx` 把逐位 feedback 拼成 8 位 `rnd`，每 8 拍凑满一字节。
- **它和 `descramble.v` 同源**：`descramble` 的 `state <= {state[5:0], feedback}` 与 `rand_gen` 的 `random <= {random[126:0], feedback}` 是同一条「LFSR 标准动作」，只是一个 7 位、一个 128 位；区别在初始化（直装法 vs 固定种子）和握手（strobe vs enable）。
- **`rand_gen_tb.v` 是最小测试台范本**：`initial` 控复位/使能/`$finish`、`always #1` 造时钟、`$fopen` + 门控 `$fwrite` 落盘；但它**无断言**，只 dump，校验靠离线分析。
- **它孤立于主链路**：不在 `dot11_modules.list`、不被 `dot11_tb` 例化、`make simulate` 不会跑它，必须 `iverilog rand_gen.v rand_gen_tb.v` 手动编译，且要先建 `sim_out/` 目录。
- **升级成 self-checking 的套路**：在测试台里维护一个**参考模型**（同构 LFSR），逐拍用层次化引用比对 DUT 内部信号，把「人眼看波形」升级为「机器报 errors」，这也是给扰码器/解码器写边界测试的通用方法。

## 7. 下一步学习建议

- **回到真实激励链路**：本讲的 `rand_gen` 是孤立的「教学工具」。想看「随机/真实激励如何驱动整条解码流水线」，回头重读 u5-l3（`dot11_tb.v` 用 `$readmemh` 灌样本）和 u5-l5（`bin_to_mem`/`condense` 如何把 USRP 抓包变成测试输入），把「造激励」和「喂激励」连起来。
- **深入扰码器的协议侧**：本讲把 `descramble` 当 LFSR 样例用，它的协议语义（为何要跳过 service 字段、`skip_bit=9` 怎么来的）在 u3-l6 和 [verilog/ofdm_decoder.v:126-127](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L126-L127) 有详细讨论，建议对照阅读。
- **学习更多 self-checking 范式**：本讲的「参考模型 + 逐拍比对」是最基础的一种；更复杂的有「黄金向量比对」（如 u5-l2 `test.py` 用 Python `decode.py` 当黄金参考逐阶段 diff）和「断言库（SVA）」。可以拿 `crc32.v` 或 `bits_to_bytes.v` 做下一个练手对象，写带参考模型的 self-checking 测试台。
- **若你要扩展项目**：本单元 u6-l5 讲过新增 MCS/调制的影响面；当你新增模块时，配套写一个像 `rand_gen_tb` 这样独立、零依赖的小测试台，是验证单个模块最快的方式，避免每次都拉起整个 `dot11_tb`。
