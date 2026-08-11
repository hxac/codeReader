# 消抖器 debouncer

## 1. 本讲目标

本讲是「从最简单的模块读起」单元的第一讲。我们将完整精读本库里最独立、最简单的一个时序逻辑模块——消抖器 `debouncer`。读完本讲后，你应该能够：

- 独立读懂一个完整的「单进程、时钟驱动」VHDL 时序模块，并能讲清每个信号的作用。
- 说清楚「用计数器判定输入稳定」的去抖原理，包括计数器在何时清零、何时自增、何时才把输入提交给输出。
- 掌握 generic `DEBOUNCE_SYNC_BITS` 与 `POLARITY` 的含义，并能算出给定时钟频率下消抖需要多长的稳定时间。
- 学会通过修改 generic 与测试台激励，观察毛刺输入下输出「何时才翻转」，并把现象和源码逻辑对应起来。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个直觉。

### 2.1 机械按键为什么会「抖动」

当你按下或松开一个机械按键时，金属触点在物理上会发生几次快速的弹跳（bounce），在电气上表现为：本该是一次干净的「0→1」跳变，却在几十微秒到几毫秒内出现了多次反复的 0/1 翻转。如果把这个原始信号直接送给数字电路（比如用来计数「按了几次」），一次按压可能被误识别成好几次。

**消抖（debounce）** 就是把这个带毛刺的原始信号，变成一个干净的、只在「输入真正稳定一段时间后」才变化的信号。

### 2.2 「稳定一段时间」该怎么量化

最朴素也最常用的做法是：用一个计数器持续监视输入。只要输入的电平和「上一拍」不一样（说明还在抖），就把计数器清零；只要输入连续保持不变，就让计数器累加；当计数器累加到一个阈值，就认定输入「真的稳定了」，把当前电平提交给输出。

阈值用「需要连续稳定多少个时钟周期」来表达，这正是本模块 generic `DEBOUNCE_SYNC_BITS` 控制的东西。

### 2.3 你需要带进来的旧知识

本讲依赖第 2 单元讲过的「同一实体多架构」模式（见 [u2-l1](u2-l1-multi-architecture-pattern.md)）。但这里有一个**重要对照**：并不是每个 IP 都需要 xilinx / intel / own 三套 architecture。`debouncer` 是一个纯粹的、与厂商无关的行为级模块——它不例化任何 `xpm` / `altera_mf` 原语，所以只有一套 `behavioural` 架构。这恰恰是它能成为「最适合先读的模块」的原因：没有厂商库的干扰，逻辑一目了然。

此外，源码顶部有一句 `use work.utils_pkg.all;`，它引入了工具函数 `to_bits`（我们在 [u3-l2](u3-l2-utils-pkg-and-submodule.md) 讲过它来自 `ip/vhdl_utils` 子模块，返回表示一个自然数所需的最少位数）。本讲会用到它，但不需要深入子模块细节。

## 3. 本讲源码地图

本讲只涉及一个 IP，共三个文件，全部在 `ip/debouncer/` 下：

| 文件 | 作用 | 设计/测试 |
|------|------|-----------|
| `ip/debouncer/debouncer.vhd` | 消抖器的设计源码（可综合），含一个 entity 和一套 `behavioural` 架构 | 设计 |
| `ip/debouncer/tb/tb_debouncer.vhd` | VUnit 测试台，用 OSVVM 随机化构造干净跳变、毛刺跳变、快速翻转等激励并校验输出 | 测试 |
| `ip/debouncer/tb/tb_debouncer.do` | ModelSim/QuestaSim 波形脚本，把信号按「接口/内部/测试台」分组显示 | 测试辅助 |

阅读建议：先看 `debouncer.vhd`（只有 49 行，非常短），建立整体印象；再看 `tb_debouncer.vhd` 里的 `test_bouncy_transition` 用例，理解毛刺是如何被构造和验证的；最后用 `.do` 脚本知道仿真时该重点看哪几个内部信号。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先看 `debouncer` 的整体面貌，再讲两个 generic，最后深入 `debounce_counter` 的稳定判定机制。

### 4.1 debouncer 模块全貌

#### 4.1.1 概念说明

`debouncer` 是一个三端口的小模块：吃一个时钟 `clk_in` 和一个原始输入 `input`，吐一个去抖后的 `output`。它只做一件事——**只有当输入连续稳定足够长时间后，才让输出跟随输入变化**。它是整座 IP 核库里依赖最少、逻辑最闭合的模块：没有复位端口、没有厂商原语、没有内部例化，只有一个时钟进程。

#### 4.1.2 核心流程

整体数据流可以这样画：

```
            ┌─────────────────────────────────────┐
clk_in ───► │  时钟进程                            │
input  ───► │  ① 记录 input 上一拍 (input_sync_d)  │ ──► input_sync ──► output
            │  ② 检测 input 是否发生变化            │
            │  ③ 没变就让计数器累加，变了就清零      │
            │  ④ 计数器满 → 把 input 提交给 input_sync│
            └─────────────────────────────────────┘
```

一句话概括：**输入必须「连续 N 拍不变」才会被采信**，中途任何一拍的变化都会让计数器归零、重新计时。

#### 4.1.3 源码精读

先看 entity 的端口契约——只有三个信号，非常干净：

[ip/debouncer/debouncer.vhd:16-26](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd#L16-L26) 定义了 entity，含两个 generic（下一节细讲）和三个端口：时钟 `clk_in`、原始 `input`、去抖 `output`，均为 `std_ulogic`。注意它**没有复位端口**——上电初值由信号声明里的 `:= not POLARITY` 给出（见 4.2.3）。

再看架构的整体骨架，只有一个进程加一行并发赋值：

[ip/debouncer/debouncer.vhd:33-48](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd#L33-L48) 是整个模块的核心：一个对 `clk_in` 敏感的时钟进程（细节留到 4.3），加上第 48 行的 `output <= input_sync`——输出直接、组合地跟随内部信号 `input_sync`，没有任何额外延迟。也就是说，**`input_sync` 才是真正的「去抖结果」，`output` 只是把它引到端口上**。

#### 4.1.4 代码实践

**实践目标**：建立「这个模块到底有几个进程、几个内部信号」的整体印象。

**操作步骤**：

1. 打开 [ip/debouncer/debouncer.vhd:28-32](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd#L28-L32)，数一下 `architecture behavioural` 的声明区有几个 `signal`。
2. 确认整个文件只有 **一个** `process`，并且它只对 `clk_in` 敏感。

**需要观察的现象 / 预期结果**：架构里恰好有 3 个内部信号（`debounce_counter`、`input_sync`、`input_sync_d`），且进程列表里只有 `clk_in` 一个敏感信号——这印证了它是一个「单时钟域、单进程」的纯时序模块。

#### 4.1.5 小练习与答案

**练习 1**：`debouncer` 没有 `reset` 端口，那它上电后输出是什么值？由什么决定？

**参考答案**：输出初值由 `input_sync` 的声明初值 `:= not POLARITY` 决定，再经 `output <= input_sync` 透传出来，所以上电时 `output = not POLARITY`（即「未按下」的静止电平）。测试台的 `test_initial_state` 也正是断言这一点。

**练习 2**：为什么说 `debouncer` 是「最适合先读的模块」？

**参考答案**：因为它不依赖任何厂商库（没有 `xpm`/`altera_mf`/`unisim`），只有一套 `behavioural` 架构；端口只有三个、没有复位、没有内部例化、只有一个时钟进程。对照第 2 单元的多架构模式，它是「厂商无关行为级实现」的最纯粹样本，读起来没有任何外部干扰。

---

### 4.2 DEBOUNCE_SYNC_BITS 与 POLARITY 通用量

#### 4.2.1 概念说明

entity 有两个 generic，它们是本模块仅有的「可调旋钮」：

- `DEBOUNCE_SYNC_BITS`：决定「输入要连续稳定多少个时钟周期」才被采信。它本质上是**计数器的位宽**，也是所需稳定周期数的「以 2 为底的对数」。
- `POLARITY`：定义按键的「有效电平」。它没有默认值，例化时必须显式给出（如 `'1'` 表示高有效按键，`'0'` 表示低有效按键）。

#### 4.2.2 核心流程

设 \( N = \text{DEBOUNCE\_SYNC\_BITS} \)，\( f_{clk} \) 为时钟频率，则输入需要连续稳定的周期数为：

\[ 2^{N} \]

对应的稳定时间门槛为：

\[ T_{stable} = 2^{N} \times T_{clk} = \frac{2^{N}}{f_{clk}} \]

举几个数（以 \( f_{clk} = 100\,\text{MHz} \)、\( T_{clk} = 10\,\text{ns} \) 为例）：

| \(N\) | 需要稳定的周期数 \(2^N\) | 稳定时间门槛 |
|------|------------------------|-------------|
| 4（测试台取值） | 16 | 160 ns |
| 10（源码默认值） | 1024 | 约 10.24 µs |
| 20 | 1 048 576 | 约 10.49 ms |

> 说明：源码头注释里写「10 ms is a good value for DEBOUNCE_SYNC_BITS」，这是把「希望的去抖时长」和「generic 取值」混在一起的经验说法。真实关系如上式：在 100 MHz 下，默认值 10 给出的是约 10 µs，而非 10 ms；要得到约 10 ms，应把 `DEBOUNCE_SYNC_BITS` 提到约 20。以源码逻辑（计数器）为准。

`POLARITY` 不参与「要不要采信」的判定（输出始终如实反映去抖后的电平），它的作用是**定义静止/初值电平**：所有内部寄存器上电初始化为 `not POLARITY`，保证模块一上电就处于「未按下」状态，不会误报一次有效输入。这一点让同一个模块既能接高有效按键，也能接低有效按键。

#### 4.2.3 源码精读

看 generic 的声明：

[ip/debouncer/debouncer.vhd:17-20](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd#L17-L20) 声明了两个 generic。`DEBOUNCE_SYNC_BITS` 的取值范围是 `natural range 0 to to_bits(natural'high)`，默认 `10`。这里的上界 `to_bits(natural'high)` 表示「能装下 `natural` 最大值所需的位数」，在 32 位实现上约为 31（因为 `natural'high` 即 \(2^{31}-1\)，需要 31 位）——这是用工具函数给 generic 设了一个安全的、与具体平台无关的上限。`POLARITY` 是 `std_ulogic` 且**无默认值**，强制例化者必须明确按键极性。

再看 `POLARITY` 是如何影响初值的：

[ip/debouncer/debouncer.vhd:30-31](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd#L30-L31) 把 `input_sync` 和 `input_sync_d` 都初始化为 `not POLARITY`。因为 `output <= input_sync`，所以上电瞬间输出就是 `not POLARITY`。这就是 `POLARITY` 的全部作用：决定「静止」长什么样。

测试台里也是用同样的两个常量来例化 DUT：

[ip/debouncer/tb/tb_debouncer.vhd:46-48](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.vhd#L46-L48) 把时钟设为 100 MHz，`DEBOUNCE_SYNC_BITS` 取 **4**（注释说明「为了缩短仿真时间取小值」），`POLARITY` 取 `'1'`。所以测试台里只需 16 个稳定周期就能触发输出变化，仿真跑得很快。

#### 4.2.4 代码实践

**实践目标**：亲手算一次「给定去抖时长，该填多大的 `DEBOUNCE_SYNC_BITS`」。

**操作步骤**：

1. 假设你的系统时钟是 50 MHz（\(T_{clk} = 20\,\text{ns}\)），你希望按键去抖门槛约 5 ms。
2. 用公式 \(2^{N} = T_{stable} \times f_{clk}\) 反解 \(N\)：\(2^{N} \approx 0.005 \times 50\,000\,000 = 250\,000\)，\(N \approx \lceil \log_2(250000) \rceil = 18\)。
3. 验证：\(2^{18} = 262\,144\) 个周期 × 20 ns ≈ 5.24 ms，满足要求。

**需要观察的现象 / 预期结果**：填 `DEBOUNCE_SYNC_BITS => 18` 即可在 50 MHz 下得到约 5 ms 的去抖门槛。

> 待本地验证：上述数值为按公式推导的结果，建议在仿真中实测输出翻转时刻确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `POLARITY` 没有默认值、而 `DEBOUNCE_SYNC_BITS` 有？

**参考答案**：去抖时长有一个合理的通用经验值（默认 10），所以给了默认值方便快速例化；而按键极性完全取决于硬件接法（高有效还是低有效），没有「安全默认」，故强制例化者显式指定，避免接错极性导致行为反常。

**练习 2**：把 `DEBOUNCE_SYNC_BITS` 设为 0 会怎样？

**参考答案**：代入公式，\(2^{0} = 1\)，计数器范围变成 `0 to 0`。此时几乎不再有去抖效果（输入只要保持一拍就会被提交），毛刺会被放过。这个边界值在工程上没有意义，但有助于理解 generic 与稳定周期的关系。

---

### 4.3 debounce_counter 稳定判定机制

#### 4.3.1 概念说明

这是整个模块的「大脑」。它要回答一个问题：**这一拍，输入算不算「发生了变化」？** 为此，模块用一个寄存器 `input_sync_d` 保存「输入在上一拍的值」，把「本拍 input」和「上一拍 input」做比较：

- 两者不等 → 输入刚刚发生了跳变（可能是一次新跳变，也可能是一次毛刺）→ **计数器清零**，重新计时。
- 两者相等 → 输入这拍没动 → **计数器 +1**；当计数器加到上限，说明输入已经连续稳定了足够久 → **把当前 input 提交给 `input_sync`**，并把计数器清零，开始为下一次跳变重新计时。

`debounce_counter` 就是那个累加「连续稳定拍数」的计数器。

#### 4.3.2 核心流程

每个 `clk_in` 上升沿，进程依次做四件事（伪代码）：

```
input_sync_d <= input              # 永远把 input 延迟一拍存起来（用于下一拍的比较）

if input /= input_sync_d:          # 本拍 input ≠ 上一拍 input  → 检测到「变化」
    debounce_counter <= 0          #   清零，重新计时
elif counter < counter'subtype'high:  # 没变化，且还没数满
    debounce_counter <= counter + 1    #   继续累加
else:                              # 没变化，且已经数满 → 输入已稳定够久
    input_sync <= input            #   采信！把 input 提交给输出寄存器
    debounce_counter <= 0          #   清零，准备监视下一次跳变
```

关于「数满」到底是多少拍：计数器范围是 `0 to 2**N - 1`，所以从 0 数到 `2**N - 1` 再遇到「没变化」就触发提交。综合算下来，**输入需要连续稳定 \(2^{N}\) 个时钟周期**才会被采信（详见 4.3.5 练习 2 的逐拍推导）。

一个关键性质：因为比较的是「本拍 vs 上一拍」，所以**哪怕只有一拍的窄毛刺，也会让计数器清零**——这正是去抖能滤掉毛刺的根本原因。

> 补充说明：`input_sync_d <= input` 实际上把异步输入寄存了一拍，对输入做了一次轻度调理；但它**不是**第 8 单元（[u8-l1](u8-l1-ff-synchroniser.md)）那种多级亚稳态同步链。`debouncer` 面向的是同一个时钟域里的慢速机械输入；若输入来自另一个时钟域，应先过同步器再消抖。

#### 4.3.3 源码精读

先看信号声明，理解计数器和两个寄存器的角色：

[ip/debouncer/debouncer.vhd:29-31](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd#L29-L31) 声明了三个信号：`debounce_counter`（范围 `0 to 2**DEBOUNCE_SYNC_BITS - 1`，即一个 N 位计数器）、`input_sync`（去抖结果，初值 `not POLARITY`）、`input_sync_d`（input 的「上一拍」副本，初值同为 `not POLARITY`）。

再看进程主体的关键四行：

[ip/debouncer/debouncer.vhd:36-43](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd#L36-L43) 是稳定判定的全部逻辑。第 36 行无条件执行 `input_sync_d <= input`——由于 VHDL 信号赋值在进程挂起时才生效，此刻 `input_sync_d` 仍是「上一拍」的值，于是第 37 行 `input /= input_sync_d` 就是在做「本拍 vs 上一拍」的变化检测；第 38 行清零；第 39–40 行在未数满时自增；第 42–43 行在数满时把 `input` 提交给 `input_sync` 并清零。整段逻辑紧凑，没有一行多余。

最后看输出如何接出：

[ip/debouncer/debouncer.vhd:48](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd#L48) 用一行并发赋值 `output <= input_sync` 把去抖结果送到端口。可见 `input_sync` 是唯一的「真相源」，输出只是它的镜像。

测试台用一个精心设计的用例验证了「毛刺会被滤掉」：

[ip/debouncer/tb/tb_debouncer.vhd:123-150](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.vhd#L123-L150) 的 `test_bouncy_transition` 先让 `input` 在 `POLARITY`/`not POLARITY` 之间逐拍反复跳（模拟弹跳），断言输出始终不变；随后等到「不足 \(2^N\) 拍」时输出仍然不变（第 143–144 行），再等够拍数后输出才翻转为 `POLARITY`（第 146–147 行）。这组断言精确刻画了「稳定窗口」的边界。

测试台还把「需要等多少拍」集中定义成一个带裕量的常量，便于各用例复用：

[ip/debouncer/tb/tb_debouncer.vhd:87](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.vhd#L87) 定义 `DEBOUNCE_WAIT_CYCLES := 2**DEBOUNCE_SYNC_BITS + 2`。`+2` 是给「变化检测那一拍」和「提交那一拍」留的裕量，保证 `wait_clk_cycles(DEBOUNCE_WAIT_CYCLES)` 之后输出一定已经翻转。

#### 4.3.4 代码实践

**实践目标**：在仿真层面亲眼看到「计数器随毛刺清零、随稳定累加、到顶提交」的过程。

**操作步骤**：

1. 在仿真器中加载 `tb_debouncer`，并用波形脚本 [ip/debouncer/tb/tb_debouncer.do:3-14](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.do#L3-L14) 添加信号。注意该脚本把 `DUT/debounce_counter`（无符号十进制）、`DUT/input_sync`、`DUT/input_sync_d` 都加进了波形——这正是你要盯的三个内部信号。
2. 运行 `test_bouncy_transition` 用例，聚焦 `input` 反复跳动的区段。
3. 观察 `debounce_counter`：每当 `input` 与 `input_sync_d` 不等的那一拍，计数器立刻归零；当 `input` 稳定下来，计数器从 0 开始一格一格往上走，直到 15（`2**4 - 1`）后的下一拍，`input_sync`（以及 `output`）才翻转。

**需要观察的现象 / 预期结果**：你会清楚看到「稳定窗口」——从 `input` 最后一次跳变、开始稳定的那一拍起，到计数器数满、`input_sync` 翻转为止，共约 16 个时钟周期。毛刺期间计数器反复在 0 附近，`input_sync` 纹丝不动。

> 待本地验证：以上为依据源码逻辑的预期现象；具体波形请在本机 ModelSim/QuestaSim 或支持 VHDL-2008 的仿真器中实测。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `input /= input_sync_d` 来判断「变化」，而不是直接比较 `input /= input_sync`（即和「已采信的输出」比）？

**参考答案**：`input_sync_d` 是 input 的「上一拍」副本，比较它等价于检测「输入这一拍相对于上一拍是否跳变」，能捕捉到**哪怕一拍的窄毛刺**并立即清零。如果改成和 `input_sync`（已采信的稳态值）比，那么从稳态 A 到稳态 B 的过程中，只要输入电平等于 A 或 B 之一就不会被判为「变化」，单拍毛刺可能被漏掉，计数器不会被及时清零，去抖效果会被削弱。源码选择「逐拍变化检测」是最严格的滤毛刺策略。

**练习 2**：设 `DEBOUNCE_SYNC_BITS = 4`，输入在第 k 拍发生一次跳变后一直稳定。请逐拍推导 `debounce_counter`，并指出 `input_sync` 在第几拍更新。

**参考答案**：第 k 拍检测到变化（`input /= input_sync_d`），计数器被置 0。此后每拍稳定：
- k+1 拍：counter 0→1
- k+2 拍：1→2
- ……
- k+15 拍：14→15（此时 15 已等于上界 `2**4 - 1`）
- k+16 拍：counter 不再 `< high`，进入 `else`，`input_sync <= input`，counter 归 0。

所以 `input_sync`（及 `output`）在第 **k+16** 拍更新，对应 \(2^{4} = 16\) 个连续稳定周期，与公式一致，也与测试台 `DEBOUNCE_WAIT_CYCLES = 2**4 + 2` 的裕量设定吻合。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个贯穿性小任务：**把消抖门槛调大、构造一个带毛刺的输入、画出波形并标注稳定窗口。**

### 步骤 1：修改 generic

编辑 `ip/debouncer/tb/tb_debouncer.vhd`，把测试台常量

```vhdl
constant DEBOUNCE_SYNC_BITS: natural := 4;
```

改成更大的值，例如 `6`（这样 \(2^{6} = 64\) 个稳定周期，更容易在波形上看清计数过程）。相应地，`DEBOUNCE_WAIT_CYCLES` 会自动变成 `2**6 + 2 = 66`，无需手改。

### 步骤 2：构造一个带毛刺的激励

参照 `test_bouncy_transition`，在 `checker` 进程里新增一个用例（示例代码，仅说明结构）：

```vhdl
-- 示例代码：新增一个带毛刺的用例
elsif run("test_my_glitch") then
    input <= not POLARITY;
    wait_clk_cycles(5);          -- 先静止在「未按下」
    input <= POLARITY;            -- 第一次按下
    wait_clk_cycles(1);
    input <= not POLARITY;        -- 毛刺：弹起一拍
    wait_clk_cycles(1);
    input <= POLARITY;            -- 再次按下，之后保持稳定
    -- 在这里持续 wait 并观察 debounce_counter 与 output
```

别忘了在 `test_suite` 循环顶部加一句 `info` 或直接让它被 `run("test_my_glitch")` 命中。

### 步骤 3：观察并画波形

仿真时用 `.do` 脚本添加 `input`、`output`、`debounce_counter`、`input_sync`、`input_sync_d`，重点看两段：

1. **毛刺段**：`input` 在两拍内来回跳时，`debounce_counter` 反复归零，`output` 始终是 `not POLARITY`。
2. **稳定段**：`input` 稳定在 `POLARITY` 后，`debounce_counter` 从 0 一路数到 63，再到下一拍 `output` 才翻成 `POLARITY`。

把观察到的波形画成下面这样的时序简图（示意，非真实数据），并用括号标出「稳定窗口」：

```
clk            _|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_|‾|_ ... |‾|_|‾|
input       0  ‾‾‾‾‾‾‾‾‾‾1___0___1‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾
                    ↑毛刺↑       ←───── 稳定窗口（约 2^N 拍）─────→
counter     0   0 0 0 0 0 0 0 1 0 1 2 3 4 ... 63 |(提交)
output      0   0 0 0 0 0 0 0 0 0 0 0 0 ... 0    1
```

### 预期结果

- 在稳定窗口结束前，`output` 不会翻转，无论 `input` 怎么抖。
- 一旦 `input` 连续稳定满 \(2^{N}\) 拍，`output` 才在紧随其后的一拍跟随 `input`。
- 把 `DEBOUNCE_SYNC_BITS` 从 4 调到 6 后，稳定窗口明显变长，这正对应公式 \(T_{stable} = 2^{N}/f_{clk}\)。

> 待本地验证：波形与翻转拍数请在本机仿真器中实测确认；本任务不修改设计源码 `debouncer.vhd`，仅改测试台常量与新增用例。

## 6. 本讲小结

- `debouncer` 是本库最简单的时序模块：三端口、无复位、无厂商原语、只有一套 `behavioural` 架构和单进程，是「厂商无关行为级实现」的纯粹样本。
- 去抖原理是「计数器稳定判定」：用一个 `input_sync_d` 保存输入上一拍的值，做逐拍变化检测——只要输入这一拍和上一拍不等，计数器就清零。
- 输入必须连续稳定 \(2^{N}\) 个时钟周期（\(N = \text{DEBOUNCE\_SYNC\_BITS}\)）才会被采信，稳定时间门槛为 \(T_{stable} = 2^{N}/f_{clk}\)；计数器数满后才把 `input` 提交给 `input_sync`，再由 `output` 镜像输出。
- `POLARITY` 不参与采信判定，只定义「静止电平」并决定所有寄存器的上电初值，使同一模块既能接高有效也能接低有效按键。
- 测试台用 `DEBOUNCE_SYNC_BITS = 4` 缩短仿真时间，并用 `test_bouncy_transition` 等用例精确验证了「毛刺被滤、稳定窗口满才翻转」的边界。
- `debouncer` 内部只有 `input_sync_d` 这一拍寄存调理，并非多级亚稳态同步链；跨时钟域输入应先经 [u8-l1](u8-l1-ff-synchroniser.md) 的同步器再消抖。

## 7. 下一步学习建议

你已经读完一个完整的单进程时序模块，接下来可以：

- 顺读本单元下一讲 [u4-l2 上电复位 reset_on_startup](u4-l2-reset-on-startup.md)，它同样基于计数器，但加入了复位信号直通、双极性合并与 `dont_touch`/`preserve` 防优化属性，复杂度上一级台阶。
- 想巩固「计数器 + 状态判定」这一套路，可对比阅读 [u5-l1 时钟使能与门控 clock_enable](u5-l1-clock-enable-gating.md)，看它如何用 `if generate` 在不同门控策略间裁剪。
- 如果你对本模块缺少多级同步器这一点感兴趣，直接跳到 [u8-l1 单比特同步器 ff_synchroniser](u8-l1-ff-synchroniser.md)，那里系统讲解亚稳态与同步链。

建议继续保留 `tb_debouncer.vhd` 作为「VUnit 测试台骨架」的参照样本——第 11 单元（验证方法学）会以它和 `tb_spi_tx` 为模板，系统讲解 VUnit 的 `test_runner_setup`/`run()`/`watchdog` 结构。
