# RTL 编码规范

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 Bedrock **为什么**需要一份统一的 RTL 编码规范，它解决的是什么痛点。
- 记住 `rtl_guidelines.md` 里 **A–E 五大类规则**：接口命名、信号命名、模块声明与参数、模块实例化、注释与杂项。
- 拿到任何一个 `.v` 文件（比如 `dsp/mixer.v`、`cordic/cordic_wrap.v`），能够**对照规范逐条判断**哪些写法合规、哪些是偏离，并能给出改名建议。
- 理解「规范」与「历史代码现状」之间的差距——Bedrock 是多年积累的库，并非每行代码都完美符合规范，学会区分「该怎么做」与「现在长什么样」。

本讲是入门层的最后一讲。它不要求你懂任何 DSP 或电路原理，只要求你看得懂 Verilog 的基本语法（`module`、`input/output`、`parameter`、`always`、`reg/wire`）。

## 2. 前置知识

- **Verilog 基础语法**：知道一个模块由端口列表（`input/output/inout`）、参数（`parameter`）和内部逻辑（`always` 块、`assign`、`reg/wire` 声明）组成。
- **接口（interface）的概念**：一组「配合使用才能完成一次数据搬运」的信号。例如要读一个寄存器，往往需要同时给出「地址 + 读/写指示 + 写数据」，并收回「读数据」——这些信号天然构成一组接口。
- **时钟域（clock domain）**：由同一个时钟驱动的所有触发器属于同一个时钟域。当一组接口信号属于某个特定时钟域时，把它们用同一个前缀标出来，能极大降低阅读成本。
- **承接前两讲**：你已经知道 Bedrock 是一个大型 Verilog 库（见 [u1-l1 项目总览](u1-l1-project-overview.md)），并且知道 `*.v` 是模块定义、`*_tb.v` 是测试台（见 [u1-l3 目录结构与代码导航](u1-l3-directory-structure.md)）。本讲我们聚焦「这些 `.v` 内部该怎么写才整齐」。

> 一个贯穿全讲的直觉：**规范的全部目的，是让你不用逐行逆向工程，光看名字就能猜出一个模块的接口和用途**。记住这句话，后面每条规则都是它的推论。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它来 |
| --- | --- | --- |
| `guidelines/rtl_guidelines.md` | Bedrock 官方 RTL 编码规范全文，A–E 五大类 | 规则的唯一权威来源 |
| `dsp/mixer.v` | 一个实数混频器模块 | 作为命名示例：既展示合规写法，也展示历史偏离 |
| `cordic/cordic_wrap.v` | CORDIC 核的 I/O 包装模块 | 作为「反面教材」：实例化方式偏离规范 |
| `dsp/cic_multichannel.v`（辅助） | 多通道 CIC 滤波器 | 作为「正面教材」：规范的实例化写法 |
| `localbus/localbus.vh`（辅助） | localbus 总线仿真任务 | 作为 `lb_` 接口前缀的活样本 |

## 4. 核心概念与源码讲解

`rtl_guidelines.md` 把规则分成 A（接口）、B（信号命名）、C（模块声明）、D（模块实例化）、E（杂项）五大类，每条都有一个编号（如 `A.1`、`D.3`），方便在代码评审里引用。下面按这五大类拆成五个最小模块来讲。

### 4.1 接口信号命名（A 类规则）

#### 4.1.1 概念说明

**接口（interface）** 是「一组配合使用、共同完成某个数据搬运协议的信号」。例如 localbus 总线要完成一次读操作，需要 `lb_clk`（时钟）、`lb_addr`（地址）、`lb_rnw`（读否）、`lb_strobe`（选通）等多个信号协同。

规则 `A.1` 要求：**同一接口的信号共用一个前缀**，让它们的「同伙关系」一目了然。当前缀同时标明时钟域（如 `lb_` 暗示 localbus 时钟域）时，价值更大。

规则 `A.2` 进一步要求：**尽量复用标准接口模式**，不要每个模块都自创一套。能用 `valid/ready/wdata/wstb` 这种约定俗成的命名，就不要发明新词。如果要偏离，必须写超出常规的详细文档。

#### 4.1.2 核心流程

给一组接口起名时，按下面的顺序判断：

1. 这组信号是否共同完成一个协议？是 → 它们是「一个接口」。
2. 给这个接口起一个**短前缀**（如 `lb_`、`gmii_`、`spi_`）。
3. 该接口的所有信号都加上这个前缀。
4. 控制和数据信号尽量用通用名（`valid`、`ready`、`wdata`、`wstb`），而不是 `my_enable_x` 这种自创名。

#### 4.1.3 源码精读

最典型的合规样本是 localbus。在仿真头文件里，总线信号全部带 `lb_` 前缀：

[localbus/localbus.vh:9-11](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/localbus.vh#L9-L11) —— `lb_addr`、`lb_wdata`、`lb_rdata` 三个信号同属 localbus 接口，共享 `lb_` 前缀；紧接着的任务里驱动它们用的也是 `lb_clk`：

[localbus/localbus.vh:19-21](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/localbus.vh#L19-L21) —— 时钟 `lb_clk`、地址 `lb_addr`、写数据 `lb_wdata` 同前缀，一眼就能看出它们是「localbus 这一族」的信号。这正是 `A.1` 想要的效果。

规范的原文表述见：

[guidelines/rtl_guidelines.md:17-21](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/guidelines/rtl_guidelines.md#L17-L21) —— A.1 接口信号同前缀，并给出了 `lb_clk, lb_valid, lb_rnw, lb_wdata, lb_rdata` 的范例。

#### 4.1.4 代码实践

1. **实践目标**：体会「同前缀」如何让你秒懂一组信号的关系。
2. **操作步骤**：打开 `dsp/mixer.v`，看它的端口列表 [dsp/mixer.v:15-18](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L15-L18)。
3. **需要观察的现象**：端口只有 `clk`、`adcf`、`mult`、`mixout`，**没有**像 `lb_` 那样的统一前缀。
4. **预期结果**：这其实是合理的——`mixer` 是一个最底层的运算单元，它的端口本来就分属「时钟」「ADC 输入」「本振输入」「输出」几个不同用途，不属于同一个接口协议，所以不强求同前缀。**结论：A.1 只约束「属于同一接口」的信号，不是要求所有端口都同前缀。**
5. 待本地验证：无（纯阅读）。

#### 4.1.5 小练习与答案

**练习**：`cordic_wrap.v` 的端口是 `clk / data / strobe / osel / d_out`（见 [cordic/cordic_wrap.v:3-9](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cordic_wrap.v#L3-L9)）。`data`、`strobe`、`osel` 这三个是否算「一个接口」？是否应该加共同前缀？

**参考答案**：它们配合使用（`data` 给数据、`strobe` 指示写入哪个寄存器、`osel` 选择输出），语义上构成一个「加载/选择」接口。按 A.1，更规范的写法是加一个共同前缀，例如 `ld_data / ld_strobe / ld_osel`，让人一眼看出它们是一组。当前写法属于历史代码的轻微偏离。

---

### 4.2 普通信号命名（B 类规则）

#### 4.2.1 概念说明

B 类规则管的是「信号本身的名字后缀」，一共有四条：

| 规则 | 后缀 | 含义 | 例子 |
| --- | --- | --- | --- |
| B.1 | `_r` / `_d` | 寄存器输出 / 延迟一拍；多拍用 `_r1`、`_r2` | `valid_r`、`valid_r1` |
| B.2 | `_l` / `_i` | 仅用于在内部复制一份端口信号的本地线 | `data_in_l` |
| B.3 | （无后缀） | 全小写、snake_case，不用 camelCase | `valid_out`、`data_in` |
| B.4 | `_n` | 低有效信号 | `reset_n`、`ce_n` |

#### 4.2.2 核心流程

写一行 `reg`/`wire` 声明时，按下面判断该加什么后缀：

1. 全部小写、用下划线分词（B.3）。
2. 如果是某端口的「打了一拍」版本 → 加 `_r`（B.1）。
3. 如果是某端口的「内部走线副本」 → 加 `_l` 或 `_i`（B.2）。
4. 如果是低有效 → 加 `_n`（B.4）。

#### 4.2.3 源码精读

`mixer.v` 同时提供了**合规**和**偏离**两种写法，是最好的教学样本。

合规示例——寄存器输出加 `_r`：

[dsp/mixer.v:21-24](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L21-L24) —— `mixout_r`、`mix_out_r` 严格遵循 B.1，名字直接告诉你「这是打了一拍的寄存器值」。

偏离示例——多拍延迟没用 `_r` 而是直接加数字：

[dsp/mixer.v:22-25](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L22-L25) —— `adcf1`、`mult1`、`mix_out1`、`mix_out2` 是「延迟若干拍」的信号，按 B.1 应写成 `adcf_r1`、`mix_out_r1`、`mix_out_r2`。当前写法省掉了 `_r`，属于历史偏离。

规范原文：

[guidelines/rtl_guidelines.md:34-36](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/guidelines/rtl_guidelines.md#L34-L36) —— B.1 寄存/延迟信号后缀；

[guidelines/rtl_guidelines.md:38-40](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/guidelines/rtl_guidelines.md#L38-L40) —— B.2 内部信号 `_l`/`_i`；

[guidelines/rtl_guidelines.md:42-46](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/guidelines/rtl_guidelines.md#L42-L46) —— B.3 全小写 snake_case 与 B.4 低有效 `_n`。

#### 4.2.4 代码实践

1. **实践目标**：在真实代码里把「打了一拍」的信号认出来。
2. **操作步骤**：阅读 [dsp/mixer.v:38-44](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L38-L44) 的 `always` 块。
3. **需要观察的现象**：`adcf1 <= adcf;` 把输入打了一拍；`mix_out2 <= mix_out1;` 再打一拍。
4. **预期结果**：你能数出从 `adcf` 到最终 `mixout`（[dsp/mixer.v:45](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L45)）一共经过了几个寄存器级，并指出若按 B.1 它们应改名为 `_r1/_r2`。
5. 待本地验证：无（纯阅读）。

#### 4.2.5 小练习与答案

**练习 1**：下面哪个名字符合 B.3？`resetN`、`reset_n`、`ResetN`。

**参考答案**：`reset_n`。它全小写、snake_case（B.3），同时 `_n` 表示低有效（B.4）。`resetN`/`ResetN` 既用了大写又没用下划线，违反 B.3。

**练习 2**：`mixer.v` 里的 `mixmulti`（[dsp/mixer.v:29](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L29)）末尾的 `i` 可能对应 B.2 的哪条规则？

**参考答案**：它很可能是 `mixmult`（乘法中间结果）+ `_i`（internal）的组合，即「仅供内部使用的中间量」。这正符合 B.2「内部信号用 `_i`」的用法。

---

### 4.3 模块声明与参数（C 类规则）

#### 4.3.1 概念说明

C 类规则管「模块怎么声明」，共两条：

- **C.1 参数命名**：参数应**全大写、snake_case**，且名字要足够自解释。常见缩写可以接受——规范明确点名 `DWI`（data width in）和 `AWI`（address width in）是合法缩写。
- **C.2 参数默认值**：**每个参数都要有合理默认值**，使得「不覆盖任何参数直接仿真」时，模块就能完成它的典型功能。

#### 4.3.2 核心流程

声明一个参数时：

1. 名字全大写、下划线分词（C.1）；若是位宽类缩写，`DWI/AWI` 这类可接受。
2. 给一个「开箱即用」的默认值（C.2）。
3. 习惯上：**行为开关**用全大写（如 `NORMALIZE`），**位宽/规模类**参数在历史代码里常用小写缩写（如 `dwi`）——记住这是「现状」，规范本身要求全大写。

#### 4.3.3 源码精读

`mixer.v` 的参数区同时展示了「合规」和「偏离」：

[dsp/mixer.v:3-13](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L3-L13) —— 看参数声明：

- `NORMALIZE`、`NUM_DROP_BITS`：**全大写 snake_case**，完全合规 C.1。它们是「行为开关」。
- `dwi`、`davr`、`dwlo`：**小写**，是位宽类参数。按规范本应写成 `DWI/DAVR/DWLO`，这里沿用了历史小写习惯，属于轻微偏离。

同时，每个参数都有默认值（`NORMALIZE=0`、`dwi=16`、`dwlo=18`…），完全符合 C.2：即使你不覆盖任何参数，模块也能直接仿真。规范的原文见：

[guidelines/rtl_guidelines.md:51-54](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/guidelines/rtl_guidelines.md#L51-L54) —— C.1 参数全大写、自解释；

[guidelines/rtl_guidelines.md:56-58](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/guidelines/rtl_guidelines.md#L56-L58) —— C.2 参数必须有默认值。

> 这是一个非常重要的「现实 vs 规范」差异：**Bedrock 的 dsp 子系统里，位宽参数大量使用小写**（`dwi/dwo/dw`），而规范要求全大写。读老代码时要心里有数：这是历史遗留，不是你记错了规范。

#### 4.3.4 代码实践

1. **实践目标**：验证 C.2「默认值可仿真」是否真的成立。
2. **操作步骤**：查看 [dsp/mixer.v:9-13](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L9-L13)，把 `dwi/davr/dwlo` 的默认值代入端口位宽，自己推算 `mixout` 的输出位宽。
3. **需要观察的现象**：`mixout` 声明为 `signed [dwi+davr-1:0]`（[dsp/mixer.v:18](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L18)），代入默认值 `dwi=16, davr=4` 后是 20 位。
4. **预期结果**：仅凭默认值，整个模块的位宽就完全确定、可综合、可仿真——这正是 C.2 的意义。
5. 待本地验证：无（纯计算）。

#### 4.3.5 小练习与答案

**练习**：你想新增一个参数「是否启用流水线寄存器」，起名为 `PipeReg`。它违反了哪条规则？该怎么改？

**参考答案**：违反 C.1（参数应全大写、snake_case）。应改为 `PIPE_REG` 或 `PIPELINE_REG`，并给一个默认值（如 `parameter PIPE_REG = 1`）以满足 C.2。

---

### 4.4 模块实例化（D 类规则）

#### 4.4.1 概念说明

D 类规则管「在一个模块里例化另一个模块」时的写法，是最容易写乱、也最影响可读性的部分：

- **D.1 实例名**：实例名必须以 `i_` 开头，包含被例化模块的名字（或其易识别缩写），可加后缀说明用途或区分多个同类实例。例：`i_sqrt`、`i_mixer_field`、`i_shortfifo_lb`。
- **D.2 未连接端口**：未用的输出端口**不要省略**，要显式写出（留空），并加注释说明为什么不用——这样读者就知道「不是写漏了，而是故意不用」。
- **D.3 按名连接**：所有端口和参数**必须按名连接**（`.clk(clk)`），不能用位置连接。未用的参数可以省略。

#### 4.4.2 核心流程

写一个实例时：

1. 实例名 = `i_` + 模块名（或缩写）+ 可选用途后缀（D.1）。
2. 参数用 `#(.参数名(值))` 按名传递；不用的参数可省略（D.3）。
3. 端口用 `.端口名(连线)` 按名连接；不用的输出端口也要写出并留空、加注释（D.2、D.3）。

#### 4.4.3 源码精读

**正面教材**——`cic_multichannel.v` 的实例化完全合规：

[dsp/cic_multichannel.v:86-95](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/cic_multichannel.v#L86-L95) —— 模块 `double_inte_smp` 被例化为 `i_double_inte`（D.1 的 `i_` 前缀 + 模块名缩写），参数 `.dwi/.dwo` 按名传递，端口 `.clk/.reset/.stb_in/.in/.out` 全部按名连接（D.3）。

[dsp/cic_multichannel.v:106-115](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/cic_multichannel.v#L106-L115) —— `serializer_multichannel` 例化为 `i_serializer_multich`，同样 `i_` 前缀 + 全按名连接。

多实例区分后缀的范例（在测试台里常见）见 `cic_multichannel_tb.v` 的 `i_mixer_a_cos` / `i_mixer_a_sin`、以及 `iq_trace.v` 的 `i_cic_multichannel_i` / `i_cic_multichannel_q`——同一个模块例化两份，用 `_cos/_sin` 或 `_i/_q` 后缀区分。

**反面教材**——`cordic_wrap.v` 的实例化有两处偏离：

[cordic/cordic_wrap.v:33-35](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cordic_wrap.v#L33-L35) ——

- 实例名叫 `dut`，**没有 `i_` 前缀，也没包含模块名**，违反 D.1。规范写法应是 `i_cordicg`。
- 参数 `#(18)` 是**位置连接**，违反 D.3。规范写法应是 `#(.???（对应位宽参数名）(18))` 按名传递。
- 端口部分 `.clk(clk), .opin(opin)...` 是按名连接，这一点合规。

规范原文：

[guidelines/rtl_guidelines.md:63-66](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/guidelines/rtl_guidelines.md#L63-L66) —— D.1 实例名 `i_` 前缀；

[guidelines/rtl_guidelines.md:68-73](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/guidelines/rtl_guidelines.md#L68-L73) —— D.2 未连接端口不省略、D.3 一律按名连接。

#### 4.4.4 代码实践

1. **实践目标**：把一段偏离规范的实例化改写合规。
2. **操作步骤**：阅读 [cordic/cordic_wrap.v:31-35](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cordic_wrap.v#L31-L35)，然后**在纸上**（不要改源码）把它改写成符合 D.1/D.3 的样子。
3. **需要观察的现象**：你需要在 cordicg_b22 的定义里查到那个 `#(18)` 对应的参数名（可用 `grep -rn "module cordicg_b22" cordic/` 找定义，再用 `grep` 看参数列表）。
4. **预期结果**：改写后形如 `cordicg_b22 #(.某参数名(18)) i_cordicg ( .clk(clk), ... );`，实例名带 `i_`、参数按名、端口按名。
5. 待本地验证：参数名待你查定义后确认（标注「待确认」）。

#### 4.4.5 小练习与答案

**练习 1**：为什么规范要求「未连接的输出端口也要写出来」而不是省略？

**参考答案**：为了让读者一眼看出「这个端口是故意不接的」，而不是「作者写漏了」（D.2）。省略会让两种情况无法区分，增加排查难度。

**练习 2**：在一个模块里例化了两个 `fifo`，分别给读通道和写通道用。按 D.1 该怎么命名？

**参考答案**：例如 `i_fifo_read` 和 `i_fifo_write`（或 `i_fifo_rd` / `i_fifo_wr`）。`i_` 前缀 + 模块名 + 区分用途的后缀，完全符合 D.1。

---

### 4.5 注释与杂项（E 类规则）

#### 4.5.1 概念说明

E 类是「杂项但重要」的规则：

- **E.1 TODO**：提交进仓库的代码尽量别留 TODO；若必须留，**只能用 `TODO` 这一个标签**，别用 `FIXME/HACK/XXX` 等别名，否则容易被人忽略。
- **E.2 注释**：注释要清晰简洁，且**不要包含问号、反问句或假设性语句**（比如「这样做对吗？」「也许可以更快？」）——这类注释会让后人不确定这是疑问还是事实。
- **E.3 运算符优先级**：当优先级不显然时，**用括号显式标出**，不要逼读者去背优先级表。

#### 4.5.2 核心流程

写注释或表达式时：

1. 表达式里出现 `& | ^ && ||` 等混用 → 加括号（E.3）。
2. 注释只陈述事实，不写问句和假设（E.2）。
3. 真有未完成的工作 → 用 `TODO` 标记并说明（E.1）。

#### 4.5.3 源码精读

`mixer.v` 的参数注释是 E.2「清晰、陈述事实」的好例子——它用整句解释每个参数的含义和取值理由：

[dsp/mixer.v:5-13](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L5-L13) —— 例如 `NUM_DROP_BITS` 的注释「Number of bits to drop at the output. The typical case is where mult is never -FS...」，是陈述句、没有问号、把「为什么」讲清楚，完全符合 E.2。

而 `mix_out_w[dwi+dwlo-1:dwlo-davr] + mix_out_w[dwlo-davr-1]`（[dsp/mixer.v:33](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L33)）这种位选择 + 加法，虽然简单，但括号把范围框得很清楚，是 E.3 的朴素体现。

规范原文：

[guidelines/rtl_guidelines.md:78-80](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/guidelines/rtl_guidelines.md#L78-L80) —— E.1 TODO 标签；

[guidelines/rtl_guidelines.md:82-83](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/guidelines/rtl_guidelines.md#L82-L83) —— E.2 注释不含问号；

[guidelines/rtl_guidelines.md:85-86](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/guidelines/rtl_guidelines.md#L85-L86) —— E.3 用括号显式标优先级。

#### 4.5.4 代码实践

1. **实践目标**：体会 E.2「注释陈述事实」与「注释带问号」的差别。
2. **操作步骤**：对比下面两段假设注释：
   - 合规（陈述句）：`// Drop 1 redundant sign bit because mult never reaches -FS.`
   - 偏离（带假设/问号）：`// Maybe drop a sign bit? Could be -FS sometimes?`
3. **需要观察的现象**：第二段让人无法判断「到底会不会到 -FS」「到底该不该 drop」，属于 E.2 要避免的写法。
4. **预期结果**：你能在 `mixer.v` 现有注释里找到的是第一类（陈述句），这说明老作者自觉遵守了 E.2。
5. 待本地验证：无。

> 上面的「假设注释」为**示例代码**（非项目原有内容），仅为对比说明。

#### 4.5.5 小练习与答案

**练习**：表达式 `a & b | c & d` 是否符合 E.3？该怎么改？

**参考答案**：不符合 E.3——`&` 与 `|` 混用时优先级不显然（虽然 Verilog 里 `&` 高于 `|`，但很多人记不准）。应改成 `(a & b) | (c & d)`，用括号把意图写明。

---

## 5. 综合实践

把本讲的五类规则串起来，做一次「规范巡检」。

**任务**：任选 `dsp/` 下一个模块（建议选 `mixer.v`、`ph_acc.v` 或 `complex_mul.v`），按 A–E 五类规则做一次合规性检查，产出一份**不超过 5 条**的「符合 / 不符合」清单；对每条不符合项，给出建议改名。

**操作步骤**：

1. 用你选定的模块，依次回答下面 5 个问题（每个问题对应一类规则）：
   - **A 接口**：属于同一接口的信号是否共用前缀？（若该模块是底层运算单元、端口分属不同用途，可记为「不适用」）
   - **B 信号**：寄存/延迟信号是否用了 `_r/_d`？内部副本是否用了 `_l/_i`？名字是否全小写 snake_case？
   - **C 参数**：参数是否全大写？是否都有默认值？
   - **D 实例化**：若模块内部例化了子模块，实例名是否带 `i_`？端口/参数是否按名连接？
   - **E 杂项**：注释是否为陈述句、不含问号？表达式是否用括号标优先级？
2. 每条写明：`[符合/不符合/不适用] 规则编号 — 现象 — （若不符合）建议改名`。

**参考答案（以 `mixer.v` 为例）**：

| # | 类别 | 判定 | 现象与建议 |
| --- | --- | --- | --- |
| 1 | A.1 | 不适用 | 端口分属时钟/输入/输出，非同一接口协议，不强求同前缀 |
| 2 | B.1 | 不符合 | `adcf1/mult1/mix_out1/mix_out2` 应改为 `adcf_r1/mult_r1/mix_out_r1/mix_out_r2`；`mixout_r/mix_out_r` 已合规 |
| 3 | C.1 | 不符合 | `dwi/davr/dwlo` 应改为 `DWI/DAVR/DWLO`；`NORMALIZE/NUM_DROP_BITS` 已合规 |
| 4 | C.2 | 符合 | 所有参数都有开箱即用的默认值 |
| 5 | E.2 | 符合 | 参数注释均为陈述句、无问号 |

（清单控制在 5 条以内，符合要求。）

## 6. 本讲小结

- Bedrock 的 RTL 规范全部写在 `guidelines/rtl_guidelines.md`，分 **A 接口 / B 信号 / C 声明 / D 实例化 / E 杂项** 五类，每条都有编号便于评审引用。
- **A 类**要求同一接口的信号共用前缀（如 `lb_`），并尽量复用标准接口而不是自创。
- **B 类**规定了信号后缀：`_r/_d`（寄存/延迟）、`_l/_i`（内部副本）、`_n`（低有效），且全小写 snake_case。
- **C 类**要求参数全大写且有默认值；但**历史 dsp 代码里位宽参数大量用小写**（如 `dwi`），这是现实与规范的差距，读代码时要分清。
- **D 类**要求实例名带 `i_` 前缀、参数和端口一律按名连接、未用输出端口显式留空并注释。
- 规范的全部目的只有一个：**让人不看实现、光看名字就能猜出模块的接口与用途**，从而降低跨模块理解成本、促进复用。

## 7. 下一步学习建议

- 接下来进入**第 2 单元（核心方法学）**。建议先读 [u2-l1 基于 Make 的 HDL 仿真测试方法](u2-l1-make-hdl-testing.md)，学会用 `make mixer_check` 真正跑起来一个模块的测试。
- 想看「标准接口」的最佳样本，直接读 `localbus/README.md` 和 `localbus/localbus.vh`（对应 [u2-l2 片上 localbus 总线](u2-l2-localbus.md)），它是 A 类规则在真实总线上的完整体现。
- 在之后阅读任何 `.v` 文件时，养成「先扫一遍端口和参数命名、判断它合不合规」的习惯——这是把本讲知识固化的最快方式。
