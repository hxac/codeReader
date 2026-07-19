# param_ram 双口参数 RAM

## 1. 本讲目标

学完本讲，读者应该能够：

- 说清 `psi_fix_param_ram` 是什么：一个**纯 VHDL、厂商无关的真双口 RAM**，并且是 psi_fix 库里少数「没有 Python 位真模型」的基础存储组件。
- 读懂它的四个 generic（`depth_g / fmt_g / behavior_g / init_g`）和 A/B 两个独立端口，并解释为什么 A、B 可以用**两个不同时钟**。
- 区分 **RBW（Read-Before-Write，读旧值）** 与 **WBR（Write-Before-Read，读新值）** 两种同地址读写行为，并能推断它们对综合时推断真实 BRAM 的影响。
- 掌握用 `init_g : t_areal` + `psi_fix_from_real` 在**综合期**把前 N 个定点系数固化进 RAM 的机制。
- 看懂 FIR 等可配置组件如何把它当成「运行时系数 RAM」：A 口写系数、B 口在数据时钟域里读系数做乘加。

---

## 2. 前置知识

本讲默认你已经学过 **u2-l1（psi_fix_pkg 类型与格式定义）**，因此下面三个概念不再重新展开，只复习要点：

- **定点格式三元组 `psi_fix_fmt_t := (s, i, f)`**：`s` 符号位、`i` 整数位、`f` 小数位，总位宽 `W = s + i + f`。
- **`psi_fix_size(fmt)`**：返回该格式占用的总位宽（slv 的位数）。
- **`psi_fix_from_real(a, fmt)`**：把一个 `real` 数在综合期量化成 `fmt` 格式的 `std_logic_vector`，是「把常数塞进硬件」的标准入口。

另外需要两个 VHDL/FPGA 常识：

- **真双口 RAM（True Dual-Port RAM, TDP）**：一块存储阵列同时暴露两个完全独立的访问端口（各有自己的时钟、地址、读/写控制），两个端口可同时读写同一块存储。
- **BRAM 推断**：FPGA 里真实的块存储器（如 Xilinx BRAM）对「同一时钟沿、同一地址同时读和写」有固定语义（通常返回**旧值**）。用 RTL 描述 RAM 时，若读写顺序与目标 BRAM 语义匹配，综合工具就能把它映射成一个硬件 BRAM，否则只能用寄存器堆实现（资源爆炸）。

> 名词速查：`t_areal` 是依赖库 `psi_common_array_pkg` 里定义的「`real` 数组」类型（即 `array of real`），所以 `init_g := (0.0, 0.0)` 是一个长度可变的实数列表。`log2ceil(x)` 来自 `psi_common_math_pkg`，返回 `⌈log₂x⌉`，用来算地址位宽。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|:-----|:-----|
| [hdl/psi_fix_param_ram.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_param_ram.vhd) | 本讲主角：纯 VHDL 真双口参数 RAM 的实体与架构。 |
| [hdl/psi_fix_pkg.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd) | 提供 `psi_fix_fmt_t`、`psi_fix_size`、`psi_fix_from_real`（初始化的底座）。 |
| [hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd) | 典型调用方：把 param_ram 当作「运行时可配置系数 RAM」使用。 |
| [testbench/psi_fix_param_ram_tb/psi_fix_param_ram_tb.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_param_ram_tb/psi_fix_param_ram_tb.vhd) | 自检测试台：验证初始内容、零填充、整片覆写三件事。 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl) | 把该测试台注册进回归（用默认 generics 跑一轮）。 |

---

## 4. 核心概念与源码讲解

### 4.1 双口 RAM 结构

#### 4.1.1 概念说明

`psi_fix_param_ram` 是一个**真双口、定点感知、带初始化**的存储器。文件头注释一句话点明它的定位：

> "This is a pure VHDL and vendor indpendent true dual port RAM."

它和 psi_common 里的通用 `psi_common_tdp_ram` 的区别在于：param_ram 是**为「定点参数（如滤波器系数）」量身定做**的——数据位宽直接由 `fmt_g : psi_fix_fmt_t` 决定（而非裸 `width_g`），并且能在综合期用一组 `real` 常数把前若干个系数固化进去。

为什么要双口？在可配置 DSP 里有一个高频需求：**一套参数在「数据时钟域」里被高速读取参与运算的同时，另一套逻辑要在「配置时钟域」里低速地改写这些参数**。双口 RAM 正是为此而生——两个端口彼此独立，天然支持这种「边读边写、跨时钟域更新」的场景。

#### 4.1.2 核心流程

param_ram 的对外结构可以抽象成：

```
            ┌─────────────────────────── mem (depth_g × W bits) ───────────────────────────┐
            │                                                                              │
  Port A ──►│ ClkA, AddrA ──► ┌─ if WrA: mem[AddrA] := DinA                                                │
  (配置/读写)│                 └─ DoutA <= mem[AddrA]  (1 拍后出现在端口)                                 │
            │                                                                              │
  Port B ──►│ ClkB, AddrB ──► ┌─ if WrB: mem[AddrB] := DinB                                                │
  (运算/读) │                 └─ DoutB <= mem[AddrB]  (1 拍后出现在端口)                                 │
            └──────────────────────────────────────────────────────────────────────────────┘
```

要点：

1. **地址位宽** = `log2ceil(depth_g)`，数据位宽 = `psi_fix_size(fmt_g)`，二者都从 generic 推导，端口声明里直接写死。
2. **两个端口结构完全对称**：A、B 各有一个时钟、地址、写使能、数据输入、数据输出。A、B 可以接**不同的时钟**（真双口的灵魂）。
3. **读延迟固定 1 拍**：`DoutX <= mem(...)` 是时钟沿赋值，所以地址给出后，数据在下一个时钟沿才出现在 `DoutX`。
4. **同一块 `mem` 被两个进程共享**：用 `shared variable` 实现，使 A、B 进程都能修改同一存储阵列。

#### 4.1.3 源码精读

先看实体声明，四个 generic 决定一切：

[hdl/psi_fix_param_ram.vhd:L22-L43](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_param_ram.vhd#L22-L43) —— 实体声明：`depth_g`（深度，默认 1024）、`fmt_g`（定点格式，默认 `(1,0,15)` 即 16 位有符号 Q0.15）、`behavior_g`（`"RBW"`/`"WBR"`，默认 `"RBW"`）、`init_g`（实数数组，默认 `(0.0, 0.0)`）。注意端口位宽都由 generic 推导：地址用 `log2ceil(depth_g)-1 downto 0`，数据用 `psi_fix_size(fmt_g)-1 downto 0`。

架构里先定义存储类型并把它实例化为一个 `shared variable`：

[hdl/psi_fix_param_ram.vhd:L47-L59](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_param_ram.vhd#L47-L59) —— 定义 `mem_t`（深度 × 位宽的二维数组），用 `GetInit` 函数把前 `init_g'length` 项填成定点化的常数、其余填 0，最后 `shared variable mem : mem_t := GetInit`。这里用 `shared variable` 而非 `signal`，是因为两个进程（A、B）都要在时钟沿里**立即**读写同一块存储——`signal` 的更新要等到进程挂起，做不到双口同地址并发访问。

两个端口的进程结构完全对称（这里只看 Port A）：

[hdl/psi_fix_param_ram.vhd:L63-L79](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_param_ram.vhd#L63-L79) —— Port A 进程：先一句 `assert` 校验 `behavior_g` 合法，再在 `rising_edge(ClkA)` 里按 `behavior_g` 决定「先读后写」还是「先写后读」。Port B 进程与之**逐行对称**（仅时钟与端口名换成 B）。正因为它俩互不依赖、各看各的时钟，才构成「真双口」。

> 注意库的「一文件一实体」纪律（见 u1-l2）：`psi_fix_param_ram.vhd` 全文只有一个 entity，方便 hdl2md 自动生成文档与回归脚本按名索引。

#### 4.1.4 代码实践

**实践目标**：通过阅读测试台，确认「读延迟固定 1 拍」与「未初始化单元为 0」。

**操作步骤**：

1. 打开测试台 [testbench/psi_fix_param_ram_tb/psi_fix_param_ram_tb.vhd:L129-L140](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_param_ram_tb/psi_fix_param_ram_tb.vhd#L129-L140)。
2. 观察 `AddrA <= to_uslv(i, AddrA'length)` 之后，测试台 `wait until rising_edge(ClkA)` **两次**再去比对 `DoutA`：第 1 拍采样地址进 RAM，第 2 拍输出才稳定——这就是 1 拍读延迟。
3. 观察比对逻辑：`i < init_g'length` 时期望 `DoutA = psi_fix_from_real(init_g(i), fmt_g)`，否则期望 `DoutA = 0`（用 `StdlvCompareInt(0, ...)`）。

**需要观察的现象**：对于 `depth_g=8, init_g=(0.1,0.2,0.3)`，地址 0/1/2 输出 0.1/0.2/0.3 的定点表示，地址 3..7 输出全 0。

**预期结果**：所有 `StdlvCompare*` 不打印 `###ERROR###`，测试台正常结束。（若要亲自运行：在 `sim/` 下 `source ./run.tcl` 或 `runGhdl.tcl` 跑回归，该 TB 由 [sim/config.tcl:L427-L428](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L427-L428) 以默认 generics 注册。）

#### 4.1.5 小练习与答案

**练习 1**：`depth_g=8, fmt_g=(1,0,15)`，地址端口和数据端口各几位？
**答案**：地址 `log2ceil(8)=3` 位；数据 `psi_fix_size((1,0,15))=1+0+15=16` 位。

**练习 2**：为什么存储用 `shared variable mem` 而不是 `signal mem`？
**答案**：两个端口进程都要在同一个时钟沿里即时读写同一块存储。`signal` 在进程内赋值后要到进程挂起才更新，无法让 A/B 两进程在同一沿完成「读+写」的并发访问；`shared variable` 是立即生效的，所以双口 RAM 的 RTL 模型必须用它。

---

### 4.2 RBW/WBR 行为

#### 4.2.1 概念说明

`behavior_g` 描述的是**同一个端口、同一时钟沿、对同一地址「既读又写」时，输出应返回旧值还是新值**。这是双口 RAM 最容易踩坑、也最影响综合结果的参数：

| 取值 | 全称 | 同沿同地址 R/W 时 `Dout` | 直觉记忆 |
|:-----|:-----|:------------------------|:---------|
| `"RBW"` | Read-Before-Write | 返回**旧值**（写之前的内容） | 「读在前」，输出落后一拍才能看到新值 |
| `"WBR"` | Write-Before-Read | 返回**新值**（刚写入的内容） | 「写在前」，写穿透到输出 |

这两种行为对应 FPGA 真实 BRAM 的两种工作模式：

- **RBW ≈ BRAM 的「READ_FIRST / 旧值」模式**：Xilinx 等 FPGA 的双口 BRAM 在同地址并发读写时默认返回旧值。因此 RTL 写成 RBW，综合工具最容易把它直接推断成一个硬件 BRAM。
- **WBR ≈ BRAM 的「WRITE_FIRST / 写穿透」模式**：部分 BRAM 支持，但资源/时序代价更高；若工具不支持，可能退化成寄存器堆。

> 注意：RBW/WBR 只影响「**同端口、同地址、同时读+写**」这一种边界情形。不同地址、或只读/只写时，两者表现完全相同。本讲开头任务问的「同一地址先读后写」正是这一情形。

#### 4.2.2 核心流程

RBW 进程内的语句顺序（读在前，写在后）：

```
rising_edge(ClkA):
    if RBW:  DoutA <= mem[AddrA]     -- (1) 先读：抓的是写入前的旧内容
    if WrA:  mem[AddrA] := DinA      -- (2) 后写：更新存储
```

WBR 进程内的语句顺序（写在先，读在后）：

```
rising_edge(ClkA):
    if WrA:  mem[AddrA] := DinA      -- (1) 先写：更新存储
    if WBR:  DoutA <= mem[AddrA]     -- (2) 后读：抓的是刚写入的新内容
```

用时间线刻画同地址、`WrA='1'`、原内容为 `OLD`、写入 `NEW` 的情形（输出在下一沿稳定）：

```
RBW :  mem: OLD ──沿──► NEW      DoutA(下一沿): OLD   ← 读到旧值
WBR :  mem: OLD ──沿──► NEW      DoutA(下一沿): NEW   ← 读到新值
```

#### 4.2.3 源码精读

进程里用**两个独立 `if`**（而非 `if/else`）来排定读写顺序，这是关键：

[hdl/psi_fix_param_ram.vhd:L66-L79](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_param_ram.vhd#L66-L79) —— Port A 进程体。RBW 分支：先执行 `DoutA <= mem(...)`（读到的是此刻尚未被本沿写更新的旧值），再执行 `mem(...) := DinA`。WBR 分支：写在前、读在后，所以读回的是刚写入的新值。注意这两个 `if` 由字符串 `behavior_g` 在**综合期**静态选择，工具会把另一条分支优化掉，最终只剩一种行为。

校验语句保证不会被传错值：

[hdl/psi_fix_param_ram.vhd:L63](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_param_ram.vhd#L63) —— `assert behavior_g = "RBW" or behavior_g = "WBR" ... severity error`，传成别的字符串会在仿真/综合时立刻报错。

测试台的「整片覆写」段间接验证了写通路（注意它读用的是默认 `behavior_g="RBW"`，写后另起一轮读，并未在同沿同地址同时读写）：

[testbench/psi_fix_param_ram_tb/psi_fix_param_ram_tb.vhd:L155-L169](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_param_ram_tb/psi_fix_param_ram_tb.vhd#L155-L169) —— 先把每个地址写成 `i+10`（`WrA<='1'` 连续写一整轮），再 `WrA<='0'` 用纯读回读，期望 `DoutA = i+10`。这段是「写之后、下轮再读」，因此 RBW/WBR 都会得到新值——它验证写生效，不区分两种 behavior。

#### 4.2.4 代码实践

**实践目标**：亲手推断 RBW 与 WBR 在「同沿同地址既读又写」下的输出差异，并理解 FIR 为什么默认选 RBW。

**操作步骤**（源码阅读 + 思维实验，无需上板）：

1. 在 Port A 进程里定位 RBW 分支 [hdl/psi_fix_param_ram.vhd:L69-L74](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_param_ram.vhd#L69-L74)：设地址 `5` 原内容 `0.3`，本沿 `WrA='1'`、`DinA=0.7`。
2. 按 RBW 顺序推演：先 `DoutA <= mem[5]`（=0.3 的旧值），再 `mem[5] := 0.7`。所以**本沿之后** `DoutA` 显示 `0.3`，要再读一次地址 5 才看到 `0.7`。
3. 改成 WBR 推演：先 `mem[5] := 0.7`，再 `DoutA <= mem[5]`（=0.7 新值）。所以 `DoutA` 立即显示 `0.7`。
4. 打开 FIR 调用方 [hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:L37-L40](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L37-L40)，看 `ram_behavior_g : string := "RBW"` 这个 generic 的注释，理解为什么库把 RBW 设为默认。

**需要观察的现象（思维实验结论）**：RBW 下，写一个系数后到「能在 B 口运算读到新系数」之间存在固有的「旧值窗口」；WBR 则写穿透、立即可见。

**预期结果**：你应当得出文字结论——
- RBW：同一地址**先读后写**时，输出返回**写之前的旧值**；新值要等到下一次读才出现。
- 默认 RBW 是为了贴合 FPGA BRAM 的 READ_FIRST 语义，便于综合时推断成硬件 BRAM、节省资源。

> 待本地验证：若想看真实波形，可复制 `psi_fix_param_ram_tb`，在 A 口构造一个「同地址 `WrA='1'` 且同时读」的激励，分别用 `-gbehavior_g=RBW` / `WBR` 跑两轮，对比 `DoutA` 波形。

#### 4.2.5 小练习与答案

**练习 1**：把 `behavior_g` 设成 `"XYW"` 会怎样？
**答案**：`assert` 会以 `severity error` 报 `"psi_fix_param_ram: behavior_g must be RBW or WBR"`；综合期字符串静态匹配，两条读写分支都不会命中，行为未定义。

**练习 2**：为什么库把 RBW 而不是 WBR 设为默认？
**答案**：主流 FPGA（含 Xilinx）双口 BRAM 在同地址并发读写时的原生语义就是「返回旧值」（READ_FIRST），RBW 与之一致，综合工具能直接推断成单个 BRAM；WBR 需要写穿透，常需额外逻辑或退化为寄存器堆。

**练习 3**：如果一个应用**只在 A 口写、只在 B 口读**（FIR 系数 RAM 正是如此），RBW/WBR 还会影响行为吗？
**答案**：不会。RBW/WBR 只在「**同一端口**、同沿、同地址既读又写」时才有区别；读写分属不同端口时，两者完全等价。这也是 FIR 能放心用默认 RBW 的原因。

---

### 4.3 定点初始化

#### 4.3.1 概念说明

很多 DSP 组件（尤其 FIR）在「上电即工作」的场景下，需要 RAM **一启动就装着合法系数**，而不是全 0（全 0 系数会让滤波器在配置完成前静音）。`init_g` 就是这个「上电初值」入口：

- `init_g` 是一个 `t_areal`（`real` 数组），长度任意。
- 它的前 `init_g'length` 项会被**综合期量化**成 `fmt_g` 格式，写入 `mem(0..N-1)`。
- 其余地址（`N..depth_g-1`）保持全 0。

这里的关键是「**综合期**」：`GetInit` 是一个在 elaboration（细化）阶段运行的函数，`psi_fix_from_real(init_g(i), fmt_g)` 把浮点常数在综合前就转成定点 slv，等价于给 RAM 一个 `.mif`/`.coe` 初值。运行时改写则交给 A 口的写通路。

#### 4.3.2 核心流程

初始化在存储声明处一次性完成，可表述为：

```
function GetInit return mem_t:
    mem_v := (others => (others => '0'))          -- 先全填 0
    for i in 0 to init_g'length-1:
        mem_v(i) := psi_fix_from_real(init_g(i), fmt_g)   -- 前 N 项定点量化
    return mem_v

shared variable mem : mem_t := GetInit            -- 上电即生效
```

对应的「定点量化」就是 u2-l1 讲过的 `psi_fix_from_real`：

\[ \text{mem}[i] = \mathrm{quantize}\big(\text{init\_g}[i],\ (s,i,f)\big),\quad i=0,\dots,N-1 \]

其中量化包含**饱和与舍入**（默认 round/sat 在 en_cl_fix 内核侧），所以即便 `init_g` 给了超出 `fmt_g` 范围的值，也会被钳到 `[psi_fix_lower_bound, psi_fix_upper_bound]` 而不会溢出反转。

#### 4.3.3 源码精读

`GetInit` 函数与存储声明：

[hdl/psi_fix_param_ram.vhd:L50-L59](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_param_ram.vhd#L50-L59) —— 先把整个 `mem_v` 初始化为全 0（`others => (others => '0')`），再用 `for` 循环把前 `init_g'length` 项替换为 `psi_fix_from_real(init_g(i), fmt_g)` 的结果。最后 `shared variable mem : mem_t := GetInit` 让初值在仿真/综合启动时就绪。

它依赖的 `psi_fix_from_real` 来自定点包：

[hdl/psi_fix_pkg.vhd:L344-L352](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L344-L352) —— `psi_fix_from_real` 的实现：先 `assert` 拦住「无符号格式却给了负数」的用法，再委托 `cl_fix_from_real` 完成量化。这正是 u2-l1 介绍的「壳 + en_cl_fix 内核」结构。

测试台直接验证了「前 N 项 = 定点化的 init_g、其余 = 0」：

[testbench/psi_fix_param_ram_tb/psi_fix_param_ram_tb.vhd:L131-L140](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_param_ram_tb/psi_fix_param_ram_tb.vhd#L131-L140) —— 对每个地址 `i`，若 `i < init_g'length` 则期望 `DoutA = psi_fix_from_real(init_g(i), fmt_g)`（测试台自己用同一个函数算期望值），否则期望 `DoutA = 0`。注意测试台常量 `init_g : t_areal := (0.1, 0.2, 0.3)`（见 TB 第 41 行），所以地址 0/1/2 有初值、3..7 为 0。

#### 4.3.4 代码实践

**实践目标**：确认 `init_g` 的长度可以与 `depth_g` 不同，且超出的部分自动补 0。

**操作步骤**：

1. 读 TB 顶部常量声明 [testbench/psi_fix_param_ram_tb/psi_fix_param_ram_tb.vhd:L38-L41](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_param_ram_tb/psi_fix_param_ram_tb.vhd#L38-L41)：`depth_g=8`、`fmt_g=(1,0,15)`、`init_g=(0.1,0.2,0.3)`。`init_g` 长度 3 < 深度 8。
2. 对照 `GetInit` 的 `for i in 0 to init_g'length-1` 边界：只量化下标 0、1、2，地址 3..7 维持初值 `(others => '0')`。
3. 跟读 TB 的比对分支（上一步已引用 L135-L139）：`i < init_g'length`（即 i<3）比定点值，否则比 0。

**需要观察的现象**：`init_g` 越界访问不会发生——循环上界是 `init_g'length-1`；`init_g` 长度可任意小于 `depth_g`，多余单元自动为 0。

**预期结果**：8 个地址的读回值为 `{0.1, 0.2, 0.3, 0, 0, 0, 0, 0}` 的定点表示，比对全部通过。

> 待本地验证：可把 TB 的 `init_g` 改长（如补到 8 项）或将 `fmt_g` 改窄，观察 `psi_fix_from_real` 对越界/精度损失的钳位与量化行为。

#### 4.3.5 小练习与答案

**练习 1**：`init_g := (0.1, 0.2)`、`depth_g=4`，上电后 `mem` 内容是什么？
**答案**：`mem(0)=quantize(0.1)`、`mem(1)=quantize(0.2)`、`mem(2)=0`、`mem(3)=0`。前 2 项定点化，后 2 项为 0。

**练习 2**：为什么用 `psi_fix_from_real` 而不是让用户直接传 `std_logic_vector` 当初值？
**答案**：用 `real` + `fmt_g` 让用户用「人类可读的浮点系数」声明初值，量化交给库统一处理，避免手算定点比特；同时保证初值与运行时写入（也走 `fmt_g` 位宽）格式一致。

**练习 3**：`init_g := (2.0, ...)` 但 `fmt_g=(1,0,15)`（范围 ≈ \([-1, 1)\)），会发生什么？
**答案**：`2.0` 超出 `(1,0,15)` 上界，`psi_fix_from_real` 内核会饱和，`mem(0)` 实际存的是 `+0.99997`（即最大正数）而非 `2.0`。

---

## 5. 综合实践

**任务**：把本讲三个模块（双口结构 / RBW-WBR / 定点初始化）串起来，读懂 param_ram 在 FIR 里「运行时更新系数」的完整用法，并写出一段说明文字。

**步骤**：

1. **看调用现场**。打开 [hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:L332-L352](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L332-L352)，这是 `i_coef_ram` 的例化。注意它的端口映射：
   - **A 口接配置接口**：`ClkA=>coef_if_clk_i`、`AddrA=>coef_if_addr_i`、`WrA=>coef_if_wr_i`、`DinA=>coef_if_wr_dat_i`、`DoutA=>coef_if_rd_dat_o`。这是用户在**配置时钟域**里读写系数的通道。
   - **B 口接数据时钟域**：`ClkB=>clk_i`、`AddrB=>r.CoefRdAddr_2`、`WrB=>'0'`（只读）、`DoutB=>CoefRamDout_3`。FIR 在做乘加时从这里实时取系数。
   - `depth_g=>CoefMemDepthApplied_c`、`fmt_g=>coef_fmt_g`、`behavior_g=>ram_behavior_g`、`init_g=>coefs_g`：把 FIR 自己的 generic 透传给 RAM。
2. **看两种系数来源**。同一文件里 `g_nFixCoef`/`g_FixCoef` 两条 generate 分支（L332、L355）：`use_fix_coefs_g=false` 时用 param_ram（运行时可配），`true` 时改用 ROM（系数固定、不能改）。这说明 param_ram 的存在意义就是「系数可运行时改写」。
3. **解释跨时钟域**。A 口用 `coef_if_clk_i`、B 口用 `clk_i`，正是真双口「两个独立时钟」的价值：低速配置逻辑可以慢慢写新系数，同时 FIR 数据通路在高速时钟域里照常读旧系数做滤波，两者并行不阻塞。
4. **写出说明**（本讲开头的实践任务）。用 3–5 句话回答两件事：
   - 当 `behavior_g=RBW` 且对同一地址「先读后写」时，`Dout` 返回**写入前的旧值**，新值要到下一次读才出现；
   - FIR 用 A 口（配置时钟域）在运行时写新系数、用 B 口（数据时钟域、只读）在乘加时取系数，从而实现「不停机换滤波器系数」；默认 `RBW` 贴合 FPGA BRAM 语义，便于推断成硬件块 RAM。

**交付物**：一段说明文字 + 一张「A 口写系数 → mem 更新 → B 口下一拍读到新系数」的简易时序草图（手画或文字描述即可）。

> 参考答案要点：因 B 口是**只读**（`WrB='0'`）且与 A 口分属不同端口，故 RBW/WBR 对 FIR 实际运行**无差别**——读写从不发生在同一端口同一地址；`behavior_g` 在这里主要影响的是 A 口「先读旧值再写」的回读语义，以及综合时能否把整块系数存储推断成单个 BRAM。

---

## 6. 本讲小结

- `psi_fix_param_ram` 是 psi_fix 库里**纯 VHDL、厂商无关的真双口 RAM**，专为「定点参数（如滤波器系数）」设计：位宽由 `fmt_g` 决定，深度由 `depth_g` 决定。
- A、B 两个端口完全对称且可接**不同时钟**，存储用 `shared variable` 才能让两进程在同一时钟沿并发读写同一阵列。
- `behavior_g` 区分 **RBW（读旧值）** 与 **WBR（读新值）**，仅在「同端口、同沿、同地址既读又写」时有别；默认 `RBW` 贴合 FPGA BRAM 的 READ_FIRST 语义，便于综合推断。
- `init_g : t_areal` 经 `GetInit` + `psi_fix_from_real` 在**综合期**把前 N 个浮点系数量化成定点初值，其余补 0——等价于 RAM 的上电初值表。
- 它是 FIR 等可配置组件的「运行时系数 RAM」：A 口在配置时钟域写系数、B 口在数据时钟域只读取系数，实现不停机换系数。
- 它**没有 Python 位真模型**（纯存储组件，不涉及定点运算语义），故回归只用一个 VHDL 自检测试台覆盖初始内容、零填充与整片覆写。

---

## 7. 下一步学习建议

- **向应用走**：下一单元 u7（FIR 滤波器族）会大量复用本讲的 param_ram。建议先读 [hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd) 中 `coef_if_*` 配置接口如何与 A 口对接、`CoefRdAddr_2` 如何在乘加循环里扫描 B 口地址。
- **向对照走**：对比同文件里用作数据存储的 `psi_common_tdp_ram`（[hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:L376](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L376)），体会「定点 + 初始化」的 param_ram 与「裸位宽」的通用双口 RAM 的分工。
- **向方法论走**：回顾 u3-l2 的协同仿真流程，理解为什么 param_ram 这类纯存储组件不需要 preScript/位真模型，而 FIR/CIC 等运算组件必须配位真 Python 模型。
