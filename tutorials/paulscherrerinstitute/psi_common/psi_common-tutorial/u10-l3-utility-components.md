# 实用组件：看门狗/消抖/防优化/触发器/动态移位

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 **看门狗（watchdog）** 与 **消抖器（debouncer）** 各自解决什么问题，并能从「计数器 + 边沿/电平检测」的角度理解它们的工作原理。
- 理解 **dont_opt** 为什么能只用 4 个物理引脚就保住上百根信号不被综合工具优化掉，知道它在「超 I/O 资源的综合试验」中的用途。
- 掌握 **模拟触发（trigger_analog）** 与 **数字触发（trigger_digital）** 的阈值跨越/边沿检测机制，以及 arm/disarm、continuous/single 两种模式。
- 理解 **动态移位器（dyn_sft）** 如何用「对数级数分摊移位量」的多级桶形移位器实现良好时序。
- 把这六个组件都看作对前两讲（u6-l1 节拍生成、u7-l1 二进程 record 设计法）已建立范式的小型应用。

## 2. 前置知识

本讲是「杂项组件」单元的一篇，组件彼此独立，但都建立在前面已经建立的几条通用范式之上，阅读前请确认你已熟悉：

- **二进程 record 设计法（u7-l1）**：所有寄存器收敛进一个 record，组合进程 `p_comb` 用变量 `v` 算次态、时序进程 `p_seq` 只负责打拍与复位。本讲六个组件全部沿用此法。
- **节拍/计数就是分频（u6-l1）**：strobe_generator 把「频率」换算成「计数比」。本讲的 watchdog、debouncer 把「时间（秒）」换算成「时钟周期数」，是同一思想。
- **math_pkg 的编译期函数（u2-l1）**：`log2ceil` 推位宽、`choose` 做编译期条件选择、`ceil` 向上取整，会反复出现。
- **边沿检测**：把信号打一拍，再与原值比较，是本讲 trigger 系列与 watchdog 的核心小动作。

几个通俗概念先解释清楚：

| 术语 | 通俗解释 |
|:--|:--|
| 消抖（debounce） | 机械按键按下/松开时，触点会在几毫秒内反复通断，产生一串毛刺而非一次干净的跳变。消抖就是「输入必须稳定持续 N 时间，才承认它真的变了」。|
| 看门狗（watchdog） | 一条「必须在规定时间内活动一次」的看护狗。若被监视信号在限定周期内没有任何变化，就累计一次「缺失」，缺失过多就报警/报错。|
| 阈值触发（trigger） | 像示波器的触发一样：当某个模拟值跨过设定门限、或某个数字信号出现上升/下降沿时，产生一个单周期脉冲。|
| 动态移位（dynamic shift） | 移位量不是常数、而是每个样本运行时给定的移位。实现上要避免一根超长组合移位链，故拆成多级。|

## 3. 本讲源码地图

本讲涉及六个独立的单文件组件，均位于 `hdl/`，除 `dont_opt` 外都有对应自校验测试平台（位于 `testbench/<组件名>_tb/`）。

| 文件 | 作用 | 是否有 TB |
|:--|:--|:--:|
| [hdl/psi_common_watchdog.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_watchdog.vhd) | 监视信号是否按期活动，超期未活动则累计缺失并报警/报错 | 有 |
| [hdl/psi_common_debouncer.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_debouncer.vhd) | 消抖滤波器，输入需稳定持续可设时长才输出 | 有 |
| [hdl/psi_common_dont_opt.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dont_opt.vhd) | 用 4 个物理引脚保住任意数量信号不被综合优化 | **无** |
| [hdl/psi_common_trigger_analog.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd) | 模拟值跨阈值时产生单周期触发脉冲 | 有 |
| [hdl/psi_common_trigger_digital.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_digital.vhd) | 数字信号上升/下降沿产生单周期触发脉冲 | 有 |
| [hdl/psi_common_dyn_sft.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd) | 多级动态（运行时移位量）桶形移位器 | 有 |

> 说明：`doc/files/psi_common_dont_opt.md` 文末虽然写着一行指向 `psi_common_dont_opt_tb.vhd` 的链接，但仓库里实际不存在该测试平台，`sim/config.tcl` 也未注册它——这是一处过时文档，阅读时请注意。

## 4. 核心概念与源码讲解

本讲按四个最小模块组织：①watchdog/debouncer（时间监视与消抖），②dont_opt（防优化），③trigger 模拟/数字（触发生成），④dyn_sft（动态移位）。

### 4.1 看门狗 watchdog 与消抖器 debouncer

#### 4.1.1 概念说明

这两个组件都把「一段时间」换算成「若干个时钟周期」，再用一个计数器来判断「这段时间内是否发生了某件事」，但出发点相反：

- **debouncer（消抖）**：关心「输入是否**稳定**」。输入一发生跳变，就把计数器重灌满；只要输入还在抖（反复跳变），计数器永远到不了 0，输出就不更新。只有输入稳定持续满 `dbnc_per_g` 秒，计数器才归零、输出才承认这次变化。它过滤掉短于门限时间的毛刺。
- **watchdog（看门狗）**：关心「输入是否**还在动**」。输入一发生变化，就把活动计数器清零；如果输入长时间不变，活动计数器就会一路数到「一个事件周期」的上限，说明「该动的时候没动」，记一次「缺失（miss）」。缺失累计到警告门限就拉 `warn_o`，到故障门限就拉 `fault_o`。

一句话区分：**debouncer 防止「不该算数」的抖动被算数；watchdog 防止「该来」的活动没来。**

#### 4.1.2 核心流程

**debouncer** 主循环（组合进程视角）：

```
每个时钟上升沿：
  inp_dff   <= (经 bit_cc 同步后的) 输入     # 打一拍，用于比较
  if inp_dff /= 当前输入:        # 输入又变了 → 还在抖
      counter <= count_max_c     # 重灌满计数器
  else:                         # 输入稳定
      if counter /= 0:
          counter <= counter - 1 # 继续倒计数
  if counter == 0:              # 已稳定满门限时间
      output <= inp_dff (按极性可选取反)
```

消抖时长与参数的关系：

\[
N_{\text{count}} = \left\lceil \frac{T_{\text{dbnc}}}{T_{\text{clk}}} \right\rceil - 1
\]

即 `count_max_c = integer(ceil(dbnc_per_g / clk_period_c)) - 1`，计数器从该值倒数到 0 所需时间正好约为 `dbnc_per_g`。

**watchdog** 主循环（组合进程视角）：

```
事件周期上限 thld_c = integer(freq_clk_g / freq_act_g) - 1
每个时钟上升沿：
  dat_dff <= dat_i                       # 打一拍
  if dat_i /= dat_dff:                   # 本拍数据变了 = 有活动
      activ_count <= 0                   # 活动计数器清零
  else:                                  # 没变
      if activ_count >= thld_c:          # 已数满一个事件周期
          activ_count <= 0
          miss_count <= miss_count + 1   # 记一次缺失
      else:
          activ_count <= activ_count + 1 # 继续等
  # 缺失计数到 warn/fault 门限 → 拉标志
```

watchdog 还有两种「缺失统计口径」，由 `thld_fault_succ_g` 选择：

- `thld_fault_succ_g = 0`（默认）：**累计缺失**模式。`miss_count` 只增不减，统计整个时间段内的总缺失数。
- `thld_fault_succ_g > 0`：**连续缺失**模式。一旦出现一次正常活动，连续计数器 `succ_count` 清零、标志撤销；只有「连续缺失」达到门限才报警。适合「偶发缺失可以容忍、但连续缺失一定是故障」的场景。

#### 4.1.3 源码精读

**watchdog 的事件周期与位宽推导**（把频率换算成周期数，位宽由 math_pkg 自动推导）：

[psi_common_watchdog.vhd:41-43](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_watchdog.vhd#L41-L43) —— `thld_c` 就是「一个事件周期等于多少个时钟周期减一」，是整条逻辑的时间基准。

**watchdog 的活动检测与缺失计数**（组合进程核心）：

[psi_common_watchdog.vhd:67-84](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_watchdog.vhd#L67-L84) —— 注意判据是 `dat_i /= r.dat_dff`，即「本拍值与上一拍值不同」才算活动。这意味着一条**恒定电平**（例如一直为高的 level）**不会**被视作活动；只有**会跳变/翻转**的信号（脉冲串、计数器低位、toggle 标志）才适合喂给 watchdog。这是选型时容易踩的坑。

**watchdog 的两种统计口径**（编译期 `if` 分支）：

[psi_common_watchdog.vhd:99-117](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_watchdog.vhd#L99-L117) —— `thld_fault_succ_g = 0` 走 `miss_count` 累计分支，否则走 `succ_count` 连续分支；`warn_o`/`fault_o` 一旦置位便自锁（只有复位才清除），符合「故障锁存待处理」的语义。

**watchdog 的标志自锁与复位清除**：标志在 record 中是普通 `std_logic`，置位后不会被自动清零，只能由 `proc_seq` 的复位分支清零（[L128-L141](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_watchdog.vhd#L128-L141)）。`miss_o` 直接引出 `miss_count`（[L126](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_watchdog.vhd#L126)）。

> 小观察：record 里声明了 `evt_count` 字段（[L49](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_watchdog.vhd#L49)）并在复位中清零（[L135](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_watchdog.vhd#L135)），但组合进程从未真正使用它——属于历史遗留的死代码，阅读时不必纠结。

**debouncer 的参数与极性处理**：

[psi_common_debouncer.vhd:36-38](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_debouncer.vhd#L36-L38) —— `count_max_c` 由「消抖时长/时钟周期」推导；`pol_eq_c` 用 `choose` 在编译期判断「输入极性是否等于输出极性」，决定输出时是否取反。这是 math_pkg `choose` 充当端口声明区「编译期三元运算符」的典型用法。

**debouncer 的输入同步**（复用 bit_cc）：

[psi_common_debouncer.vhd:53-64](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_debouncer.vhd#L53-L64) —— `sync_g=true` 时例化 `psi_common_bit_cc`（见 u5-l2）做两级同步，把异步外部输入（如按键）先纳入本时钟域；`sync_g=false` 则直通。debouncer 处理的往往是异步外部输入，故默认 `true`。

**debouncer 的倒计数与输出更新**（组合进程核心）：

[psi_common_debouncer.vhd:75-90](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_debouncer.vhd#L75-L90) —— 输入一变就重灌 `count_max_c`；稳定才递减；归零才把 `inp_dff`（按极性）写入 `output`。复位初值用 `not in_pol_g`/`not out_pol_g`（[L46](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_debouncer.vhd#L46)），保证上电时输入/输出处于各自的「无效电平」。

#### 4.1.4 代码实践

**实践目标**：用 debouncer 处理一个按键输入，选定合适的消抖周期，并通过运行官方 TB 观察滤波行为。

**操作步骤**：

1. 打开测试平台 [testbench/psi_common_debouncer_tb/psi_common_debouncer_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_debouncer_tb/psi_common_debouncer_tb.vhd)，看清它的 generic 默认值：`dbnc_per_g = 20.0e-6`（20 µs，仅为缩短仿真时间）、`freq_clk_g = 100.0e6`、`len_g = 10`、`sync_g = true`，并注意 TB 内部固定 `in_pol_c='1'`、`out_pol_c='0'`（输入高有效、输出低有效，极性相反）。
2. 阅读 `proc_stim`（[L73-L109](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_debouncer_tb/psi_common_debouncer_tb.vhd#L73-L109)）：第一段循环每 `dbnc_per_g/2` 秒翻转一次输入——**抖动周期短于门限**，应被滤除；第二段每 `2*dbnc_per_g` 秒翻转——**稳定时间长于门限**，应被承认。
3. 在 `sim/` 下按 u1-l3 的方法跑回归（`run.tcl`，Modelsim）或单独跑 `psi_common_debouncer_tb`：
   ```tcl
   # sim 目录下
   source run.tcl
   ```
   若只想跑这一个 TB，可在 `config.tcl` 中临时只保留 `create_tb_run "psi_common_debouncer_tb"`。
4. **为真实按键重选参数**：机械按键抖动通常持续 5–20 ms。若你的系统时钟是 100 MHz，把 `dbnc_per_g` 改为 `20.0e-3`（20 ms）才是上板合理值；此时 `count_max_c = ceil(20e-3/10e-9)-1 = 1 999 999`，计数器位宽 `log2ceil(2 000 000) = 21` 位。

**需要观察的现象**：

- 第一段（半周期抖动）期间，`out_obs` 维持在初始的「输出有效电平」不变（因为短抖动被滤掉）。
- 第二段（稳定 ≥ 门限）后，`out_obs` 翻转并稳定，TB 末尾的 `StdlvCompareStdlv(test, out_obs, ...)` 自检通过。

**预期结果**：仿真日志不出现 `###ERROR###`，TB 正常结束。把 `dbnc_per_g` 改成 20 ms 后**逻辑结论不变**，只是计数器位宽变宽、仿真时间变长（建议仿真时仍用 20 µs）。

> 若本地暂无 Modelsim/GHDL 环境，本实践可降级为「源码阅读型」：手动用 `count_max_c` 公式验算 20 ms / 100 MHz 下的计数器位宽，并解释为何 TB 故意把 `dbnc_per_g` 设成 20 µs（答案：把真实毫秒级时间压缩到微秒级以缩短仿真时长，逻辑等价）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：watchdog 默认 `freq_clk_g=100 MHz`、`freq_act_g=100 kHz`，`thld_c` 等于多少？它代表什么物理含义？
**答**：代入公式 `integer(100.0e6/100.0e3)-1 = 1000-1 = 999`。它表示「期望每 1000 个时钟周期（=10 µs）输入至少变化一次」；若 1000 周期内输入毫无变化，就记一次缺失。

**练习 2**：为什么不能把一个**恒为高**的「电源正常」信号直接喂给 watchdog 来监测？
**答**：watchdog 的活动判据是 `dat_i /= dat_dff`（本拍与上拍不同）。恒定电平每拍都与上拍相同，永远被判为「无活动」，会立即开始累计缺失并很快报故障。要监测「电源正常」这类电平，应先把它转成一个会翻转的信号（例如用它门控一个计数器、把计数器低位喂给 watchdog）。

**练习 3**：debouncer 把 `dbnc_per_g` 从 20 µs 改为 20 ms，`count_max_c` 与计数器位宽各如何变化？
**答**：`count_max_c` 由 1999 变为 1 999 999；计数器位宽由 `log2ceil(2000)=11` 变为 `log2ceil(2 000 000)=21`。

---

### 4.2 防综合优化 dont_opt

#### 4.2.1 概念说明

综合工具会优化掉「对输出没有任何可观测影响」的信号。但有时我们恰恰需要做一次**综合试验**——评估一个还接不进真实芯片的设计的时序和资源——而这个设计的 I/O 数量可能远超任何现有芯片的引脚。直接综合会被工具以「引脚不足」拒绝，或大量端口被裁剪导致资源统计失真。

`psi_common_dont_opt` 就是为这种场景准备的「虚拟引脚（Virtual Pin）」：它**只用 4 个真实物理引脚**，通过移位寄存器串行搬运，把被测设计（DUT）任意数量的输入/输出都「挂」在这 4 个引脚上，且因为存在真实的、工具可见的数据依赖，这些信号都不会被优化掉。

```
       4 个物理引脚 pin_io(3:0)        CLK
              |                          |
--+-------------------------+            |
|  psi_common_dont_opt      |--- dat_o -->  DUT 的 N 个输入
|  (4 个引脚串行搬运)        |<-- dat_i ---  DUT 的 N 个输出
+---------------------------+
```

#### 4.2.2 核心流程

dont_opt 内部维护三组寄存器，靠 4 根 `pin_io` 双向引脚串行收发：

- **ToDutShiftReg / ToDutLatchReg**：把要送给 DUT 的数据（`dat_o`）逐位从 `pin_io(1)` 串行移入；`pin_io(0)` 为高时把移位结果锁存到 `ToDutLatchReg`，作为稳定的 DUT 输入。
- **FromDutShiftReg**：捕获 DUT 送回的数据（`dat_i`）；`pin_io(2)` 为高时装载 `dat_i`，否则每拍移入一个 `'0'`；最高位通过 `pin_io(3)` 串行送出，让工具「看见」DUT 输出确实流向了一个物理引脚。

四根引脚的方向：`pin_io(0/1/2)` 由外部驱动（组件端置 `'Z'` 高阻，当输入用），`pin_io(3)` 由组件驱动输出。

#### 4.2.3 源码精读

**移位装载与串行输出**（组合进程）：

[psi_common_dont_opt.vhd:64-73](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dont_opt.vhd#L64-L73) —— `pin_io(1)` 逐位左移进 `ToDutShiftReg`，`pin_io(0)` 为高时锁存；`pin_io(2)` 选择把 `dat_i` 装入 `FromDutShiftReg` 还是继续移入 `'0'`。

**引脚方向与输出**：

[psi_common_dont_opt.vhd:79-84](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dont_opt.vhd#L79-L84) —— `pin_io(0/1/2) <= 'Z'`（高阻，作输入），`pin_io(3) <= FromDutShiftReg 的最高位`（组件驱动，作输出），`dat_o <= ToDutLatchReg`（送给 DUT 的稳定值）。注意时序进程 [L86-L91](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dont_opt.vhd#L86-L91) **没有复位分支**——上电状态由 FPGA 触发器初值决定，这在该组件的应用场景（综合试验，非功能通路）里可以接受。

#### 4.2.4 代码实践

**实践目标**：理解 dont_opt 的「虚拟引脚」机制，并在源码层面推演一次数据搬运过程。

**操作步骤**：

1. 阅读 [hdl/psi_common_dont_opt.vhd:57-84](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dont_opt.vhd#L57-L84)，画出三组寄存器与四根引脚的数据流图。
2. 假设 `to_dut_width_g=8`，回答：要把一个 8 位字从外部送进 DUT，需要多少个时钟周期？外部应如何驱动 `pin_io(1)` 与 `pin_io(0)`？
3. 在你的顶层里，把一个 I/O 数超标的设计的所有端口连到 dont_opt 的 `dat_i`/`dat_o`，只把 `pin_io(3:0)` 与 `clk_i` 连到真实物理引脚，跑一次综合，对比「不挂 dont_opt」与「挂 dont_opt」两种情况下 DUT 的资源占用是否接近。

**需要观察的现象**：综合报告里 DUT 的 LUT/FF/BRAM 数量在两种情况下应基本一致（因为 dont_opt 保住了所有信号）；`pin_io` 只占用 4 个物理引脚。

**预期结果**：能复述「8 位字需 8 拍移入 + 1 拍锁存」的串行搬运过程；综合后 DUT 信号未被裁剪。本组件无官方 TB，综合行为需待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 dont_opt 只用 4 个引脚就能保住上百根信号？
**答**：因为所有 DUT 信号都被接进了 dont_opt 内部的移位寄存器，而这些寄存器又通过 `pin_io(3)` 形成了对物理引脚的真实数据依赖。综合工具只要顺着数据依赖分析，就会发现这些信号最终都「可达」一个真实引脚，故不会优化掉；N 位数据只需 1 根串行数据线 + 几根控制线即可分时搬运。

**练习 2**：dont_opt 的时序进程为什么没有复位分支？
**答**：它服务于综合试验（评估时序/资源），不是功能数据通路，初值不影响资源评估结论；省略复位可减少复位布线负担。生产功能逻辑不应照搬这种写法。

---

### 4.3 触发生成 trigger_analog / trigger_digital

#### 4.3.1 概念说明

trigger 系列像示波器的触发器：当满足某个「条件」时产生一个单周期脉冲，用来对齐采集、启动某段处理。两个组件条件不同：

- **trigger_analog**：条件是「一个（有符号或无符号）数值**跨过阈值**」。可配置为上升沿跨越、下降沿跨越或两者皆可。从多路模拟输入里用 `trg_anlg_src_cfg_i` 选一路，与阈值 `anl_th_trig_i` 比较。
- **trigger_digital**：条件是「一个数字信号出现**上升沿/下降沿**」。从多路数字输入里用 `trg_digital_source_cfg_i` 选一路做边沿检测。

两者共享一套**触发管理**机制：

- **arm/disarm**：`trg_arm_cfg_i` 的**上升沿**用来 toggle「装填（armed）」状态。只有 armed 时才会输出触发脉冲。
- **模式（continuous/single）**：`trg_mode_cfg_i(0)` = 0 为连续模式，每次满足条件都发脉冲；=1 为单次模式，发一次脉冲后自动 disarm，必须重新 arm 才能再触发。
- **外部 disarm（ext_disarm_i）**：当多个触发源同时装填、只允许其中一个真正触发时，被选中触发的那个会通过此信号把其余触发源一起 disarm。

文档里特别提醒的延迟：trigger_analog 满足条件后**延迟 2 拍**输出脉冲（因为内部要先把模拟值打一拍做比较），trigger_digital **延迟 1 拍**。需要在采集对齐时由用户外部补偿。

#### 4.3.2 核心流程

以 trigger_analog（有符号）为例：

```
每个时钟上升沿：
  在 armed 状态下：
    取出选中通道的本拍值 RegAnalogValue 与上拍值 RegAnalogValue_dff
    if (上拍 < 阈值) and (本拍 >= 阈值) and edge_rising_enabled:  OTrg <= '1'  # 上升跨越
    if (上拍 > 阈值) and (本拍 <= 阈值) and edge_falling_enabled: OTrg <= '1'  # 下降跨越
  single 模式下 OTrg 一出现 → 自动 disarm
```

trigger_digital 把「阈值跨越」换成「0→1 / 1→0」的边沿检测：

```
  if (上拍=0) and (本拍=1) and rising_enabled:  OTrg <= '1'
  if (上拍=1) and (本拍=0) and falling_enabled: OTrg <= '1'
```

#### 4.3.3 源码精读

**trigger_analog 的 arm/触发管理**（与 digital 完全同构）：

[psi_common_trigger_analog.vhd:69-77](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd#L69-L77) —— `InTrgArmCfg_dff` 是 `trg_arm_cfg_i` 打一拍，二者比较得到上升沿；上升沿 toggle `TrgArmed`；single 模式下触发或外部 disarm 时清零。

**trigger_analog 的有符号阈值跨越检测**：

[psi_common_trigger_analog.vhd:84-96](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd#L84-L96) —— 用「上拍值与阈值的关系」对照「本拍值与阈值的关系」判定跨越方向，`trg_edge_cfg_i(1)` 使能上升、`(0)` 使能下降。通道选择靠把 `trg_anlg_src_cfg_i` 当作字索引，从拼接的 `anl_trig_i` 里切出对应 `width_g` 位片段（[L84](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd#L84)）。无符号分支逻辑同构（[L100-L112](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd#L100-L112)），由 `is_signed_g` 的 `if` 在编译期二选一。

**trigger_digital 的单输入位宽兜底**：

[psi_common_trigger_digital.vhd:32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_digital.vhd#L32) —— 源选择端口宽度用 `choose(trig_nb_g > 1, log2ceil(trig_nb_g)-1, 0)`：只有 1 路输入时端口缩成 0 位（恒定选第 0 路），多于 1 路才需要选择位。这是 `choose` 处理「边界退化」的又一范例。

**trigger_digital 的边沿检测**：

[psi_common_trigger_digital.vhd:72-80](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_digital.vhd#L72-L80) —— `RegDigitalValue_dff` 是选中信号的上拍值，与本拍值比较得 0→1 / 1→0 边沿。digital 信号本身只有 1 位，比较比 analog 简单，故延迟只有 1 拍。

> 命名小提醒：trigger_analog 的「已装填」状态输出端口写成 `trg_is_armed_i`（带 `_i` 后缀但实为 `out`，[L38/L117](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd#L38)），与库的 `_o` 规范（见 u1-l4）不一致；trigger_digital 则规整地写作 `trg_is_armed_o`。这是历史遗留命名，使用时以方向而非后缀为准。

#### 4.3.4 代码实践

**实践目标**：跑通官方 trigger TB，观察 arm/触发/disarm 与 single/continuous 两种模式的波形差异。

**操作步骤**：

1. 打开 [testbench/psi_common_trigger_digital_tb/psi_common_trigger_digital_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_trigger_digital_tb/psi_common_trigger_digital_tb.vhd)，定位它如何驱动 `trg_arm_cfg_i`、`trg_mode_cfg_i`、`trg_edge_cfg_i` 与 `digital_trg_i`。
2. 在 `sim/` 跑回归或单独跑 `psi_common_trigger_digital_tb` 与 `psi_common_trigger_analog_tb`（`config.tcl` 第 [434-438](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L434-L438) 行）。
3. 在波形窗口观察：armed 之前 `trigger_o` 是否始终为 0；digital 在选中的输入出现上升沿后，`trigger_o` 是否在**下一拍**出现单周期脉冲；single 模式下脉冲后 `trg_is_armed_o` 是否立即掉到 0。

**需要观察的现象**：

- 连续模式下，输入反复跳变时 `trigger_o` 反复出脉冲；
- 单次模式下只出一次脉冲，必须再次 arm 才会继续触发。

**预期结果**：两个 TB 均不报 `###ERROR###`。digital 延迟 1 拍、analog 延迟 2 拍，可在波形上数拍确认。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：trigger_analog 用「上拍 < 阈值 且 本拍 ≥ 阈值」判定上升跨越，而不是单纯「本拍 ≥ 阈值」。为什么必须引入上拍值？
**答**：单纯「本拍 ≥ 阈值」只要信号停留在阈值上方就会每拍都触发。引入上拍值后，只有「从下到上真正跨过阈值的那一拍」才触发，等价于在阈值处做一个上升沿检测，保证每个跨越只产生一个脉冲。

**练习 2**：single 模式下，触发脉冲产生后会发生什么？想再次触发必须做什么？
**答**：`OTrg` 一出现，`TrgArmed` 立即被清零（[trigger_analog L73-L74](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_trigger_analog.vhd#L73-L74)），组件进入 disarm 态，不再产生新脉冲。要再次触发，必须给 `trg_arm_cfg_i` 一个上升沿重新 arm。

---

### 4.4 动态移位器 dyn_sft

#### 4.4.1 概念说明

「动态移位」指移位量不是一个编译期常数，而是每个样本都可能在变的运行时值 `shift_i`（例如可变增益、可变延时里按值移位）。朴素实现是直接 `dat_i sll shift_i`，但 `shift_i` 是变量时，综合器会展开成一根覆盖所有可能移位量的超长组合多路选择链，时序很差。

`psi_common_dyn_sft` 用经典的**对数级数桶形移位器（logarithmic barrel shifter）**解决这个问题：把 N 位的移位量拆成若干段，每段 `sel_bit_per_stage_g` 位，每段对应一级移位器，该级只移「0 或 2^k」位（k 随级数增长）。于是总移位量 = 各级移位量之和，级数为 `ceil(shift位宽 / sel_bit_per_stage_g)`，每级组合逻辑深度恒定且浅，整体时序良好。

#### 4.4.2 核心流程

设移位量端口位宽为 `B = log2ceil(max_shift_g + 1)`，每段取 `S = sel_bit_per_stage_g` 位，则级数：

\[
N_{\text{stages}} = \left\lceil \frac{B}{S} \right\rceil
\]

每级 `stg`（从 0 开始）：

```
StepSize = 2^(stg * S)                       # 本级「单位移位量」
Select   = 取 shift(stg) 的低 S 位           # 本级实际移 0..(2^S - 1) 个 StepSize
data_out = data_in 移位 (Select * StepSize) 位  # LEFT: 左移补0; RIGHT: 右移补符号或0
shift    = shift(stg) 逻辑右移 S 位          # 把剩下的高位交给下一级
vld      = vld(stg)                          # 有效随数据逐级下传
```

默认例（`max_shift_g=16`、`sel_bit_per_stage_g=4`）：`B = log2ceil(17) = 5`，`Stages_c = ceil(5/4) = 2` 级。第一级每步 1 位（可选移 0/1/…/15 位），第二级每步 16 位（可选移 0/16 位）。

方向 `direction_g`：`"LEFT"` 左移低位补 0；`"RIGHT"` 右移高位补 `sign_extend_g ? 符号位 : '0'`。移位量是否非法由开头 `assert` 检查（方向必须为 LEFT/RIGHT）。

#### 4.4.3 源码精读

**级数与类型推导**：

[psi_common_dyn_sft.vhd:41](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd#L41) —— `Stages_c = ceil(shift_i'length / sel_bit_per_stage_g)`，是「分摊移位量」的关键。`Data_t`/`Shift_t` 是各级数据与剩余移位量的数组类型（[L44-L45](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd#L44-L45)）。

**方向合法性与综合期断言**：

[psi_common_dyn_sft.vhd:57](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd#L57) —— 用 `###ERROR###` 前缀的 `assert` 校验方向（库统一的 TB/综合错误标记，见 u1-l3）。

**多级移位主循环**（组合进程核心）：

[psi_common_dyn_sft.vhd:77-100](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd#L77-L100) —— 每级先用 `Select_v * StepSize_v` 算出本级移位量，再在双倍宽度的临时向量 `TempData_v` 里「放置数据后取半」实现一次干净的移位（RIGHT 分支 [L83-L90](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd#L83-L90)，LEFT 分支 [L91-L94](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd#L91-L94)），然后 `shift_right(r.Shift(stg), sel_bit_per_stage_g, '0')` 把剩余位移交给下一级（[L98](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd#L98)，用的是 logic_pkg 的 `shift_right`，见 u2-l2）。

**输出与流水**：

[psi_common_dyn_sft.vhd:103-104](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_dyn_sft.vhd#L103-L104) —— 输出取最后一级 `Data(Stages_c)`；`vld_o` 同步随级数延迟 `Stages_c` 拍。注意 record 把 `Vld`/`Data`/`Shift` 都做成 `0 to Stages_c` 的数组，每一级都是一组寄存器——这是把「流水线每一级」直接映成 record 数组的写法。

#### 4.4.4 代码实践

**实践目标**：通过官方 TB 的多组 generic 组合，体会 `sel_bit_per_stage_g`（每级位数）对级数与延迟的影响。

**操作步骤**：

1. 看 `config.tcl` 为 `psi_common_dyn_sft_tb` 注册的 5 组运行（[L463-L470](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L463-L470)）：覆盖 LEFT/RIGHT、`sel_bit_per_stage_g=2/3`、`sign_extend_g=true/false` 的组合。
2. 在 `sim/` 跑该 TB 的全部组合。
3. 手算：默认 `max_shift_g=16`、`width_g=32`。
   - `sel_bit_per_stage_g=4` 时，`B=5`，级数 `ceil(5/4)=2`，输出延迟 2 拍；
   - `sel_bit_per_stage_g=2` 时，`B=5`，级数 `ceil(5/2)=3`，输出延迟 3 拍。

   体会「每级位数越小 → 级数越多 → 延迟越大，但每级组合逻辑越浅」的权衡。

**需要观察的现象**：`vld_i` 拉高后，`vld_o` 在 `Stages_c` 拍后跟随；`dat_o` 是 `dat_i` 按 `shift_i` 移位后的结果，LEFT 低位补 0，RIGHT 在 `sign_extend_g=true` 时高位补符号位。

**预期结果**：5 组运行全部通过、无 `###ERROR###`；手算的级数与延迟与波形一致。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`max_shift_g=16`、`sel_bit_per_stage_g=4`，移位量端口位宽与级数各是多少？
**答**：移位量端口位宽 `B = log2ceil(max_shift_g+1) = log2ceil(17) = 5`；级数 `Stages_c = ceil(5/4) = 2`。

**练习 2**：把 `sel_bit_per_stage_g` 从 4 改为 2，时序与延迟如何变化？
**答**：每级处理的移位位变少，单级组合逻辑更浅、时序更好；但级数从 2 增至 3，输出延迟增加 1 拍，寄存器数量也增加。这是「时序 vs 延迟/资源」的经典权衡。

**练习 3**：为什么 dyn_sft 用「在双倍宽向量里放数据再取半」的方式做移位，而不是直接写 `sll`/`slr`？
**答**：移位量是运行时变量时，`sll`/`slr` 会被综合成面向所有可能移位量的长多路选择链。用双倍宽缓冲「先按 `Select*StepSize` 偏移放置、再截取目标半段」，把每一级的移位限制为「若干个固定 StepSize 的倍数」，每级只需一个浅 MUX，再靠多级累加得到任意移位量，整体组合深度为对数级，时序可控。

---

## 5. 综合实践

**任务**：为一个 100 MHz 系统设计一个「按键触发采集」的小数据通路，把本讲至少三个组件串起来。

要求：

1. 用 **debouncer** 处理一个机械按键：输入高有效、输出高有效，消抖周期选 20 ms（写出 `count_max_c` 与计数器位宽的手算结果）。
2. 把消抖后的按键沿（你需要在 debouncer 之后自己做一次边沿检测，得到「按下」单周期脉冲）接到 **trigger_digital** 的 `digital_trg_i`，配置为 single 模式、上升沿触发，作为「采集启动」。
3. 用 **watchdog** 监视「采集启动」脉冲的活动：`freq_act_g` 设为 1 Hz（即期望每秒至少按一次键用于自检），`thld_fault_succ_g` 设为正数启用连续缺失模式，思考 `thld_c` 等于多少个时钟周期。
4. 画出这条「按键 → debouncer → 边沿检测 → trigger_digital → 启动脉冲 → watchdog 监视」的数据通路框图，并标注每级的延迟拍数。

**参考思路**：

- debouncer 参数：`count_max_c = ceil(20e-3/10e-9)-1 = 1 999 999`，计数器位宽 21 位；输入输出极性相同（都高有效），`pol_eq_c=true` 不取反。
- 边沿检测：把 debouncer 输出打一拍得 `d`，`按下脉冲 = output and not d`。
- watchdog：`thld_c = integer(100e6/1)-1 = 99 999 999`，即期望每 1 亿个时钟周期（1 秒）至少看到一次启动脉冲的变化；连续缺失模式下，若连续 N 秒没有按键，按 `thld_fault_succ_g` 报故障。注意 watchdog 看的是「信号是否变化」，单周期脉冲本身就会带来两拍变化，天然适合喂给它。
- 延迟：debouncer 内部有 bit_cc 同步 2 拍 + 倒计数（稳定后 1 拍输出更新）；trigger_digital 1 拍；watchdog 不在本关键路径。

> 这是一个设计型实践，没有唯一答案。重点是练习「按真实物理时间（毫秒/秒）反推 generic 参数」以及把多个小组件拼成数据通路。若本地有仿真环境，可把这条通路写成一个顶层并自检；否则以框图与参数手算作为交付。待本地验证。

## 6. 本讲小结

- **debouncer 与 watchdog 是一对镜像**：debouncer 要输入「稳定」才承认变化（滤除短毛刺）；watchdog 要输入「还在动」才算正常（检出该来没来的活动）。两者都把「时间」换算成「时钟周期计数」。
- **watchdog 的活动判据是 `dat_i /= dat_dff`**，只对会跳变/翻转的信号有效，恒定电平会被判为无活动；它有「累计缺失」与「连续缺失」两种口径，由 `thld_fault_succ_g` 切换。
- **debouncer 默认例化 bit_cc 做两级同步**，因为它的输入常是异步外部信号（如按键）；极性由 `in_pol_g`/`out_pol_g` 决定。
- **dont_opt 是「虚拟引脚」**，用 4 个物理引脚 + 移位寄存器保住任意数量信号不被综合优化，专用于 I/O 超标的综合试验；它无官方 TB、无复位分支。
- **trigger_analog / trigger_digital** 用「上拍 vs 本拍」做阈值跨越/边沿检测，共享 arm/disarm 与 continuous/single 模式；analog 延迟 2 拍、digital 延迟 1 拍。
- **dyn_sft 是对数级桶形移位器**，把运行时移位量拆成多级、每级移「2 的幂」位，以恒定浅组合深度换取良好时序；级数 = `ceil(移位量位宽 / sel_bit_per_stage_g)`。

## 7. 下一步学习建议

- 本讲的 watchdog/trigger/dyn_sft 都把「时间」或「移位」作为变量，下一讲 **u10-l4（统计与信号源：min_max/prbs/pwm/sample_rate_converter）** 会继续介绍流式数据上的统计与信号生成组件，可对照阅读 `psi_common_prbs`（伪随机激励源，常与本讲的 trigger 配合做采集自检）。
- 若你对 dyn_sft 的多级思想感兴趣，可回头看 **u7-l2（multi_pl_stage）** 的 `for generate` 级联写法，二者的「级数化」思路一致。
- 想系统练习「为这些组件写自校验 TB」，请进入 **u11-l1（编写自校验测试平台）**，本讲引用的 `debouncer_tb`、`dyn_sft_tb` 都是很好的入门范本。
