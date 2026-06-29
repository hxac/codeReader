# 控制计数器与时钟门控

## 1. 本讲目标

前面三讲（u2-l1 的 PE、u2-l2 的阵列互联、u2-l3 的行/列矩阵变换）解决了「数据怎么算」「数据怎么连」「数据怎么错峰喂进去」三个问题。但还有一个关键问题没回答：**谁来告诉整个阵列「现在开始算」「算到第几拍了」「算完了，请采样结果」？**

这就是本讲要讲的 `topSystolicArray.sv` 里的**控制段（Control 段）**。学完本讲，你应该能够：

1. 解释 `MULT_CYCLES = 3N-2` 的含义，并算出给定 N 下的乘法周期数与计数器位宽 `MULT_CYCLES_W`。
2. 看懂 `counter_q` 计数器是如何被 `doProcess` 驱动、又如何反过来决定 `doProcess` 何时结束的。
3. 画出一次完整乘法过程中 `doProcess_q` / `counter_q` / `validResult_q` 的时序图，并说清楚 `o_validResult` 为什么是一个**单拍脉冲**。
4. 理解编译期参数检查 `N_VALID` + `$error` 是如何充当一道「护栏」的。

---

## 2. 前置知识

本讲会用到下面几个概念，不熟悉的可以先放慢节奏：

- **寄存器（flip-flop）与组合逻辑**：`always_ff @(posedge clk)` 描述的寄存器在时钟上升沿把 `_d`（下一拍值）锁存为 `_q`（当前值）；`always_comb` 描述的组合逻辑在输入变化时立刻算出 `_d`。本工程统一用 `_d` / `_q` 后缀区分「下一拍要写入的值」和「当前持有的值」。
- **`localparam` / `parameter`**：编译期常量。`parameter` 可以在实例化时被覆盖（本工程的 `N` 就是 parameter），`localparam` 是模块内部派生出的常量（如 `MULT_CYCLES`），不能再被外部覆盖。
- **`$clog2(x)`**：向上取整的对数，返回「表示 0 到 x-1 这些值至少需要多少位」。例如 `$clog2(11) = 4`，因为 \(2^3=8 < 11 \le 16 = 2^4\)。
- **`$error`**：SystemVerilog 的诊断系统任务，在**精化（elaboration）阶段**触发时会报告错误并终止编译，常用于做编译期断言。
- **门控（clock / process gating）**：这里不是物理上关时钟，而是用一个使能信号（`doProcess`）决定寄存器要不要更新、MAC 要不要累加。PE 的累加与输入寄存都受 `i_doProcess` 控制（见 u2-l1）。

> 承接提示：u1-l3 已经建立了「一次乘法耗时 `3N-2` 拍、`o_validResult` 为单拍脉冲」的结论；u2-l3 讲清了行/列移位寄存器每拍右移一个元素。本讲就回答「这 `3N-2` 拍到底是哪个计数器数出来的、`o_validResult` 到底在哪一拍亮」。

---

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开：

| 文件 | 作用 |
| --- | --- |
| [rtl/topSystolicArray.sv](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv) | 顶层模块。本讲只看其中三段：`Check matrix dimension size is valid`（参数检查）、`Control counter`（计数器与 validResult）、`Systolic array clock gate`（doProcess 门控）。|
| rtl/pe.sv | （承接 u2-l1）只用到一点：PE 的累加与输入寄存都靠 `i_doProcess` 门控，用来理解「doProcess 一拉低，全阵列同时清零/冻结」。|
| tb/Makefile | 提供 `make lint` 目标，用来在本讲的实践中触发编译期参数检查。|

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **参数检查 `N_VALID`**——编译期的护栏，挡住非法的 N。
2. **控制计数器 `counter_q` 与 `MULT_CYCLES`**——数出一次乘法要多少拍。
3. **`doProcess` 门控与 `validResult` 生成**——用计数器决定「开始/保持/结束」并点亮完成脉冲。

### 4.1 参数检查 N_VALID（编译期护栏）

#### 4.1.1 概念说明

`N` 是这个设计里唯一一个可被外部修改的规模参数（默认 4）。它驱动了几乎所有东西：阵列是 N×N 个 PE、输入端口是 N×N×8 位、输出是 N×N×32 位、乘法周期是 `3N-2` 拍。如果有人随手把 N 设成一个荒唐的值（比如 1、0、或者 1000），设计要么算错、要么综合炸掉。

好的 RTL 习惯是：**在编译期就把非法参数挡掉，并给出一句人话错误信息**，而不是让错误潜伏到仿真波形里才被发现。这就是 `N_VALID` + `$error` 的作用。

#### 4.1.2 核心流程

```
1. 用 localparam 把「N 是否合法」算成一个 1 位常量 N_VALID。
2. N_VALID = (N > 2) 且 (N < 257)。   // 即合法范围 2 < N < 257
3. if (!N_VALID) 触发 $error，编译/精化在此终止。
```

注意这里的 `if ... $error` 写在模块体里、条件是编译期常量，所以它是一个**精化期（elaboration-time）判断**：工具在「把参数代进去、生成实际电路」这一步就会求值，根本不会等到仿真跑起来。

#### 4.1.3 源码精读

参数与检查的定义：

[rtl/topSystolicArray.sv:20-27](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L20-L27) —— 用 `&{...}` 把多个布尔条件「与」在一起得到 `N_VALID`，再在 `!N_VALID` 时 `$error`。

> 小知识：`&{ a, b }` 是对一个拼接向量做归约与（reduction AND）。因为 `a`、`b` 都是 1 位的，`&{a,b}` 等价于 `a & b`，但写成归约形式的好处是「以后想加第三个条件，只要再往花括号里塞一个」即可，扩展性更好。

关于合法范围的口径有两处说法，初学者容易混淆，这里点清楚：

- **RTL 的硬护栏**（这里）：`2 < N < 257`，对应 8 位输入、32 位输出下计数器/位宽都安全的理论上限。
- **README 的推荐范围**：`2 < N < 17`（即 3 到 16），这是作者实际验证过、且保证 32 位输出累加不溢出的范围（见 u1-l1）。

也就是说，RTL 允许更大的 N，但 README 只保证到 16；本讲的所有举例都用默认的 **N=4**。

#### 4.1.4 代码实践

**实践目标**：亲手触发一次编译期参数错误，确认这道护栏真的生效。

**操作步骤**：

1. 打开 `rtl/topSystolicArray.sv`，把第 4 行的默认参数从 `N = 4` 改成 `N = 2`（落在非法区间，因为要求 `N > 2`）。
2. 在 `tb/` 目录下跑最快的自检目标（来自 [tb/Makefile:40-42](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/Makefile#L40-L42)，它执行 `verilator --lint-only`）：
   ```
   cd tb && make lint
   ```
3. 观察控制台输出。
4. **记得改回 `N = 4` 再继续后面的练习。**

**需要观察的现象**：Verilator 在精化阶段就报错并退出，错误信息里应包含第 26 行那句 `Matrix dimension size 'N' is invalid.`，且不会生成任何可执行文件。

**预期结果**：`make lint` 以非零状态码结束，根本走不到仿真阶段。这正是「把错误前置到编译期」的价值。**待本地验证**：不同版本 Verilator 的错误措辞与退出码可能略有差异。

#### 4.1.5 小练习与答案

**练习 1**：如果把 N 改成 `300`，`N_VALID` 是多少？会触发 `$error` 吗？
**答案**：`N > 2` 为真、`N < 257` 为假，所以 `N_VALID = 1 & 0 = 0`，`!N_VALID` 为真，会触发 `$error`。

**练习 2**：为什么用精化期 `$error` 而不是在 `always_ff` 里用运行期断言？
**答案**：N 是编译期常量，非法 N 在精化阶段就已知，没必要等到仿真。精化期拦截可以「立刻、确定地」挡住非法配置，且不占用任何运行期硬件（综合后这段 `if` 在 N 合法时会被优化掉，不产生任何电路）。

---

### 4.2 控制计数器 counter_q 与 MULT_CYCLES

#### 4.2.1 概念说明

知道了「乘法要花 `3N-2` 拍」（u1-l3 结论），下一步就是：**用一个计数器把这 `3N-2` 拍数出来**。计数器的值有两个用途——一是判定「数到头了，该点亮 `o_validResult` 了」，二是判定「再多数一拍，就可以关掉 `doProcess` 了」（见 4.3）。

这里有两个派生常量必须先理清：

- `MULT_CYCLES = 3*N - 2`：一次乘法需要的**周期数**（N=4 时为 10）。这直接对应 README 里那句「After, 10 (3N-2) clock cycles the multiplication is complete」（[README.md:91-93](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/README.md#L91-L93)）。
- `MULT_CYCLES_W = $clog2(MULT_CYCLES+1)`：存放计数值需要的**位宽**。

#### 4.2.2 核心流程

`3N-2` 这个数从哪来？它是二维脉动阵列做矩阵乘法的经典延迟公式。直觉上可以这样拆（精确的逐拍推导需要结合 u2-l3 的数据流）：

\[ \text{延迟} = \underbrace{(N-1)}_{\text{对角错峰，让最后一行/列的数据进网}} + \underbrace{(N-1)}_{\text{行内逐列传播}} + \underbrace{N}_{\text{每个 PE 累加 N 个乘积}} \;=\; 3N-2 \]

也就是说，最晚完成的那个 PE（右下角 `c[N-1][N-1]`）要等输入错峰进网、再横竖传到它、还要累加 N 个乘积，加起来正好 `3N-2` 拍。N=4 时即 10 拍。

计数器本身的逻辑很简单：

```
计数器 counter_q：
  - 复位时清 0。
  - 当 doProcess_d == 1（这一拍要处理）：counter_d = counter_q + 1   // 继续数
  - 否则：                              counter_d = 0                  // 回 0，准备下一次
```

> 注意一个**双向耦合**：`counter` 的递增由 `doProcess_d` 决定（4.2），而 `doProcess_d` 的清零又由 `counter_q` 是否数到 `MULT_CYCLES+1` 决定（4.3）。两者互相驱动，但因为是「`_d`（组合）→ `_q`（寄存器）」的结构，不会形成组合环路：`doProcess_d` 依赖 `counter_q`（寄存器输出），`counter_d` 依赖 `doProcess_d` 与 `counter_q`，最终都落在寄存器 `counter_q` / `doProcess_q` 上，环路被寄存器打断。

#### 4.2.3 源码精读

两个派生常量：

[rtl/topSystolicArray.sv:36-38](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L36-L38) —— `MULT_CYCLES = 3*N-2`；位宽 `MULT_CYCLES_W = $clog2(MULT_CYCLES+1)`，注释里的「`+1` to support `counter_q + 1`」是说：计数器要做 `counter_q + 1`，所以位宽得能装下「最大计数值 +1」而不溢出。

计数器寄存器与组合：

[rtl/topSystolicArray.sv:40-52](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L40-L52) —— `counter_q` 异步复位清 0，否则锁存 `counter_d`；`counter_d` 在 `doProcess_d=='1` 时自增、否则归 0。

#### 4.2.4 代码实践

**实践目标**：手算 N=4 下的 `MULT_CYCLES` 与 `MULT_CYCLES_W`，并验证位宽够用。

**推导**：

\[ \text{MULT\_CYCLES} = 3\times 4 - 2 = 10 \]

\[ \text{MULT\_CYCLES\_W} = \$clog2(\text{MULT\_CYCLES}+1) = \$clog2(11) = 4 \]

因为 \(2^3 = 8 < 11 \le 16 = 2^4\)，所以 4 位。`counter_q` 的类型是 `logic [MULT_CYCLES_W-1:0]` 即 `logic [3:0]`，能表示 0~15。4.3 会看到计数器最大要数到 `MULT_CYCLES+1 = 11`，4 位（最大 15）放得下。✓

**操作步骤**：

1. 翻到 [rtl/topSystolicArray.sv:36-38](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L36-L38)，确认 N=4 时这两个常量确实算出来是 10 和 4。
2. 在 4.1 的实践里你已经会跑 `make lint`。如果想「在不改源码」的前提下看这两个常量的值，可以在脑海里把 N 代进去（编译器就是这么做的）。

**预期结果**：N=4 → `MULT_CYCLES=10`，`MULT_CYCLES_W=4`，`counter_q` 是 4 位寄存器。

#### 4.2.5 小练习与答案

**练习 1**：N=8 时 `MULT_CYCLES` 和 `MULT_CYCLES_W` 各是多少？
**答案**：`MULT_CYCLES = 3×8-2 = 22`；`MULT_CYCLES_W = $clog2(22+1) = $clog2(23) = 5`（因为 \(2^4=16<23\le32=2^5\)）。计数器最大要放 `MULT_CYCLES+1 = 23`，5 位（最大 31）够用。

**练习 2**：为什么计数器的递增条件是 `doProcess_d == '1`，而不是 `doProcess_q == '1`？
**答案**：用 `_d`（组合下一拍值）可以让计数器在「启动那一拍」就开始数。`i_validInput` 一到，`doProcess_d` 立刻变 1（4.3），同拍的 `counter_d` 就 +1，下一拍 `counter_q` 就是 1。如果改用 `doProcess_q`（当前值），计数器会晚一拍启动，整个时序会错位。`'1` 是 SystemVerilog 里「全 1」的写法，对 1 位信号就是 1。

---

### 4.3 doProcess 门控与 validResult 生成

#### 4.3.1 概念说明

这是本讲最核心的部分。`doProcess`（do-process，「是否在处理」）是发给整个脉动阵列的**总使能信号**：在 u2-l1 里我们见过，PE 的累加 `mac_d = i_doProcess ? mac_q + mult : 0` 和输入寄存器的更新都由它门控。换句话说：

- `doProcess = 1`：全阵列在算，MAC 在累加，数据在 PE 间流动。
- `doProcess = 0`：全阵列冻结——MAC 清零、输入寄存器保持，准备下一次乘法。

而 `o_validResult` 是回给使用者的**「算完了」信号**：它是一个**单拍脉冲**，使用者应该在这一拍采样 `o_c`。

本模块要回答三个问题：
1. `doProcess` 什么时候被**置 1**（启动）？
2. `doProcess` 什么时候被**清 0**（结束），中间怎么**保持**？
3. `o_validResult` 在哪一拍亮、为什么只亮一拍？

#### 4.3.2 核心流程

`doProcess_d`（下一拍的 doProcess）是一个**三态优先级**逻辑：

```
doProcess_d:
  if (i_validInput)                      → 1        // ① 启动：新请求来了，立刻置 1（最高优先级）
  else if (counter_q == MULT_CYCLES+1)   → 0        // ② 结束：数到头再多一拍，关掉
  else                                   → doProcess_q  // ③ 保持：其余情况维持现状
```

三个分支的优先级很关键（源码注释也强调了）：**`if/else if/else` 在硬件里被综合成优先编码器**，所以「新请求（validInput）」能打断正在进行的清零/保持。这一点在「上一个乘法刚结束、新乘法立刻到来」时很重要——这也是 u2-l3 里「新请求优先于旧移位」三态逻辑的同款写法。

`o_validResult` 的逻辑：

```
validResult_q（寄存器，复位为 0）:
  if (counter_q == MULT_CYCLES)  → 1     // 计数器数到 MULT_CYCLES，下一拍拉高
  else                           → 0     // 其余拍为 0
o_validResult = validResult_q              // 组合直出
```

因为 `validResult_q` 是**寄存器**，它在 `counter_q` 数到 `MULT_CYCLES` 的**下一拍**才变 1；又因为下一拍 `counter_q` 已经变成了 `MULT_CYCLES+1`（不再等于 `MULT_CYCLES`），`validResult_q` 又立刻被拉回 0——所以 `o_validResult` 天然就是一个**单拍脉冲**。

那 `doProcess` 为什么要在 `counter_q == MULT_CYCLES+1`（而不是 `MULT_CYCLES`）时才关掉？因为 `validResult_q` 比计数器晚一拍：`counter_q` 在第 `MULT_CYCLES` 拍到达终点，但 `o_validResult` 要到下一拍（`counter_q == MULT_CYCLES+1` 那拍）才真正亮起。如果 `doProcess` 在 `MULT_CYCLES` 就关掉，那么「亮 `validResult` 的那一拍」`doProcess` 已经是 0，全阵列 MAC 会被清成 0。所以 `doProcess` 必须多撑一拍，**让门控开到 `o_validResult` 亮的那拍结束为止**。

> 细心者会问：`doProcess` 多撑的这一拍里 MAC 还在累加，会不会把结果算错？不会。结合 u2-l3，行/列移位寄存器每拍右移 8 位，N=4 时每行只有 `2N-1 = 7` 个字节，撑不到 `MULT_CYCLES+1 = 11` 拍——到 validResult 亮起时，移位寄存器里早就只剩 0 了，PE 算的是 `mac_q + 0`，结果不变。这也是「门控多一拍」安全的原因。**待本地验证**：可在波形里确认 `validResult` 亮起那拍，馈入 PE 的输入已为 0。

#### 4.3.3 源码精读

`doProcess` 门控（含寄存器与三态组合）：

[rtl/topSystolicArray.sv:73-87](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L73-L87) —— `doProcess_q` 异步复位为 0；`doProcess_d` 按 `validInput` → `counter==MULT_CYCLES+1` → 保持 的优先级生成。注意 `MULT_CYCLES_W'(MULT_CYCLES+1)` 是把整数 `MULT_CYCLES+1` 显式转型成计数器位宽，避免位宽不匹配告警。

`o_validResult` 生成：

[rtl/topSystolicArray.sv:56-67](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L56-L67) —— `validResult_q` 在 `counter_q == MULT_CYCLES_W'(MULT_CYCLES)` 时置 1，否则清 0，再组合直出为 `o_validResult`。

doProcess 如何传给阵列（承接 u2-l1/u2-l2）：

[rtl/topSystolicArray.sv:161-173](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L161-L173) —— 顶层把 `doProcess_q` 接到 `systolicArray` 实例的 `i_doProcess`，再由阵列隐式命名连接广播到每一个 PE（见 u2-l2）。

**N=4 的完整时序（实践任务的参考答案）**

下表是一次乘法过程中，**每个时钟上升沿之后**各寄存器的值（`i_validInput` 仅在第 1 拍为 1，模拟单拍触发）：

| 上升沿 # | 本拍 i_validInput | counter_q | doProcess_q | validResult_q | 说明 |
|:---:|:---:|:---:|:---:|:---:|---|
| 0（复位后） | – | 0 | 0 | 0 | 复位初值 |
| 1 | **1** | 1 | 1 | 0 | 启动：`doProcess_d=1`，计数器开始自增 |
| 2 | 0 | 2 | 1 | 0 | 计数中（保持） |
| 3 | 0 | 3 | 1 | 0 | |
| 4 | 0 | 4 | 1 | 0 | |
| 5 | 0 | 5 | 1 | 0 | |
| 6 | 0 | 6 | 1 | 0 | |
| 7 | 0 | 7 | 1 | 0 | |
| 8 | 0 | 8 | 1 | 0 | |
| 9 | 0 | 9 | 1 | 0 | |
| 10 | 0 | **10** | 1 | 0 | counter 到达 `MULT_CYCLES=10` |
| 11 | 0 | 11 | 1 | **1** | `o_validResult` 亮：本拍采样 `o_c` |
| 12 | 0 | 0 | 0 | 0 | counter==`11` → `doProcess_d=0`，全阵列清零/冻结，归位 |

读这张表的三个要点：

- `doProcess_q` 在上升沿 1~11 共 **11 拍**为 1（即 `MULT_CYCLES+1` 拍），第 12 拍才回 0。
- `o_validResult` 只在上升沿 11 之后那 **1 拍**为 1（单拍脉冲），对应 `counter_q` 刚刚越过 `MULT_CYCLES`。
- `counter_q` 数到 11 后被 `doProcess_d=0` 拉回 0，整机回到「等待下一次 `i_validInput`」的静止状态。

对应的简化波形示意（`_` 低电平，`█` 高电平）：

```
i_validInput : █________________________________...   (仅第1拍)
counter_q    : 0 1 2 3 4 5 6 7 8 9 10 11 0 ...        (0→11 再回0)
doProcess_q  : 0 1 1 1 1 1 1 1 1 1  1  1  0 ...        (高11拍)
validResult_q: 0 0 0 0 0 0 0 0 0 0  0  1  0 ...        (单拍脉冲)
                                  ↑      ↑
                          counter=10   采样o_c
```

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：自己推算 N=4 的时序表，再用波形验证。

**操作步骤**：

1. **先合上书本**：不看上面的参考表，在纸上为 N=4 推算 `MULT_CYCLES`、`MULT_CYCLES_W`，并逐拍填出 `counter_q` / `doProcess_q` / `validResult_q` 的值，直到三者全部回到 0。
2. **跑仿真拿波形**：
   ```
   cd tb && make all
   ```
   （流程见 u1-l2：verilate→build→sim→waves，会生成 `waveform.vcd`。）
3. **打开波形**：用 gtkwave 打开 `tb/waveform.vcd`，在 `topSystolicArray` 作用域下添加 `i_validInput`、`counter_q`、`doProcess_q`、`validResult_q`、`o_validResult` 这几个信号。
4. **对照**：把波形里一次乘法过程的电平变化，和你手画的时序表逐拍比对。

**需要观察的现象**：

- `i_validInput` 拉高后下一拍，`doProcess_q` 跟着拉高，`counter_q` 开始递增。
- `counter_q` 从 0 数到 11 再回 0；`doProcess_q` 在此期间一直为 1，共 11 拍。
- `validResult_q`（=`o_validResult`）只亮 1 拍，且发生在 `counter_q` 从 10 跳到 11 的那个上升沿之后。

**预期结果**：波形与上面 4.3.3 的表格完全吻合。若测试平台（[tb/tb_topSystolicArray.cpp:156-181](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L156-L181)）正是在 `o_validResult==1` 那拍去采样并校验 `o_c` 的，这也印证了「单拍脉冲」的设计意图。**待本地验证**：Verilator 对内部信号 `counter_q`/`doProcess_q` 的具体层级命名可能略有差异，在 gtkwave 里按 `topSystolicArray.` 前缀搜索即可找到。

#### 4.3.5 小练习与答案

**练习 1**：如果把 4.3 里 `doProcess_d` 的清零条件从 `counter_q == MULT_CYCLES+1` 改成 `counter_q == MULT_CYCLES`，`o_validResult` 还能采到正确的 `o_c` 吗？为什么？
**答案**：会出问题。改完后，`counter_q` 一到 `MULT_CYCLES`（10），`doProcess_d` 立刻变 0；而 `validResult_q` 恰恰是在「`counter_q == MULT_CYCLES` 的下一拍」才亮——也就是说，`o_validResult` 亮的那拍 `doProcess_q` 已经是 0，PE 的 `mac_d = 0`，`o_c` 会在那一拍之后被清成 0。即使采样发生在脉冲前沿、值还没被覆盖，时序也变得极其脆弱、依赖精确采样点。原设计的 `+1` 正是为了让门控「多撑一拍」，盖住 `validResult` 的整个有效窗口。

**练习 2**：`o_validResult` 为什么必然是单拍脉冲，而不需要额外加清零逻辑？
**答案**：因为它由 `counter_q == MULT_CYCLES` 触发置 1，但 `counter_q` 每拍都在变（在处理期间一直 +1）。`counter_q` 等于 `MULT_CYCLES` 只能维持一拍（下一拍就变成 `MULT_CYCLES+1`），所以「置 1 条件」只成立一拍，脉冲自然只有一拍宽。这是用「自增计数器」做单拍标志的经典技巧。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「换规模、全推算、波形对拍」的练习。

**任务**：把设计改成 **N=8**，重新推算并验证控制段的全部关键量。

**步骤**：

1. **算**：为 N=8 推算
   - `MULT_CYCLES = 3×8-2 = 22`；
   - `MULT_CYCLES_W = $clog2(23) = 5`；
   - `doProcess_q` 高电平持续多少拍？（答：`MULT_CYCLES+1 = 23` 拍）
   - `o_validResult` 在 `counter_q` 等于几时亮？（答：等于 `MULT_CYCLES = 22` 的下一拍）
2. **改**：参考 u3-l2 的方法，同步修改 RTL 参数 `N`（[rtl/topSystolicArray.sv:4](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L4)）和测试台宏 `N`（[tb/tb_topSystolicArray.cpp:16](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L16)），两处必须一致。
3. **跑**：`cd tb && make all`，打开波形。
4. **对**：在波形里量出 `doProcess_q` 的高电平拍数、`counter_q` 的最大值、`o_validResult` 的脉冲位置，与你的推算逐一核对。

**验收标准**：

- `counter_q` 数到 `MULT_CYCLES+1 = 23` 后回 0；
- `doProcess_q` 高电平 23 拍；
- `o_validResult` 是单拍脉冲，且测试台在该拍校验 `o_c` 通过（控制台无 `ERROR`）。

> 这个练习同时复习了：参数检查（N=8 合法）、计数器位宽（5 位够放 23）、门控与脉冲时序（`+1` 规则），是把本讲三模块打通的最佳练手。

---

## 6. 本讲小结

- **参数护栏**：`N_VALID = (N>2) & (N<257)`，非法 N 在精化期就被 `$error` 挡下，不耗任何运行期硬件。
- **乘法周期**：`MULT_CYCLES = 3N-2`（N=4 时为 10），源自二维脉动阵列的经典延迟；位宽 `MULT_CYCLES_W = $clog2(MULT_CYCLES+1)`。
- **计数器**：`counter_q` 仅在 `doProcess_d==1` 时自增、否则归 0；它与 `doProcess` 互相驱动，但被寄存器打断，无组合环路。
- **doProcess 门控**：三态优先级——`validInput` 启动 > `counter==MULT_CYCLES+1` 结束 > 否则保持；`if/else if/else` 综合成优先编码器，让新请求可打断。
- **validResult 脉冲**：`counter_q == MULT_CYCLES` 触发，但因计数器每拍自增，置 1 条件只成立一拍，天然单拍脉冲；`doProcess` 的 `+1` 让门控盖住整个有效采样窗口。
- **整机一次乘法**：`i_validInput` 一到启动 → 计数 `3N-2` 拍 → `o_validResult` 亮一拍供采样 → `doProcess` 关、计数器回 0，回到静止。

---

## 7. 下一步学习建议

至此，u2（核心 RTL 模块）的四块拼图已经集齐：PE（u2-l1）、阵列互联（u2-l2）、矩阵变换（u2-l3）、控制时序（本讲）。整个 `topSystolicArray.sv` 已经没有黑盒了。接下来进入 u3（专家层），建议按这个顺序：

1. **u3-l1 Verilator C++ 测试平台**：去看测试台是怎么在 `o_validResult` 那拍采样 `o_c` 并比对的——你会更深刻地理解本讲的「单拍脉冲」为什么必须被正确捕获。
2. **u3-l2 参数化设计与 SystemVerilog 编码风格**：把本讲的「改 N」做成系统化的清单，并理解 `default_nettype none` / `$error` 等编码约定。
3. **u3-l3 Quartus FPGA 综合**：看这些控制逻辑（计数器、门控）综合到 Cyclone V 后占用多少 ALM/寄存器。
4. **u3-l4 进阶实践**：尝试把无符号 MAC 改成有符号乘法——届时你需要回头确认 `doProcess` 的门控时序在有符号改造后仍然正确。

如果你想立刻巩固本讲，最好的办法就是去做**第 5 节的综合实践**：换 N、推算、对波形。能独立对上，说明控制段你已经真正吃透了。
