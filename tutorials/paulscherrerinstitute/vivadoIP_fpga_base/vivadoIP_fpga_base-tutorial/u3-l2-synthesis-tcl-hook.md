# 综合阶段 TCL 钩子：把编译时间写入 INIT

## 1. 本讲目标

上一篇 u3-l1 我们搞清楚了硬件侧的事：fpga_base 用 160 个 `FDPE` 触发器当"一位 ROM"来存固件编译时间，靠 `dont_touch` 把它们从综合器的剪刀下救回来，并留了一条 `g_ngenerics`（默认）分支，让日期值来自 FDPE 的 `INIT` 初值。但 u3-l1 留了一个悬念：**这些 FDPE 的 `INIT` 到底是谁、在什么时候、按什么规律写进去的？** 本讲就把这条另一半的链路补完。

本讲精读根目录下的 [`fpga_base.tcl`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl)。学完后你应当能够：

- 说清楚这个脚本作为 Vivado 的 `tcl.pre` 钩子运行在流水线的哪个环节，为什么必须是那个环节。
- 读懂 `dec2bin` 辅助函数如何把一个十进制数变成定宽二进制字符串，并解释 `x` 从 31 递减、`y` 从 0 递增时**二进制位与 FDPE 实例下标如何一一对应**——也就是 u3-l1 末尾留下的"字符串最高位为何对应 `gen_year[31]`"这个问题。
- 掌握 `set_property INIT ... [get_cells <层次路径>]` 这种"综合后回写触发器初值"的手法，以及它依赖的可预测的 generate 实例命名（`gen_year[x].year_dfpe_inst`）。
- 判断：如果改用 generic 模式（`C_USE_INFO_FROM_SCRIPT = true`），这个 TCL 钩子还需要执行吗？

本讲依赖 u3-l1（FDPE 与双模式）。涉及寄存器偏移时回顾 u2-l3 即可。

## 2. 前置知识

### 2.1 什么是 Vivado 的 TCL 钩子（tcl.pre / tcl.post）

Vivado 把一次完整构建拆成两个大阶段：**综合（synthesis）** 把 VHDL 翻译成由基本单元（cell）组成的网表（netlist）；**实现（implementation）** 再对这张网表做优化、布局、布线、生成比特流。每个阶段又细分为若干 step（如 `synth_design`、`opt_design`、`place_design`、`route_design` 等）。

每个 step 都允许挂两段 TCL 脚本：

| 钩子 | 触发时机 | 典型用途 |
|------|----------|----------|
| `tcl.pre` | 该 step **开始之前** | 在工具动手前修改网表 / 属性 |
| `tcl.post` | 该 step **结束之后** | 事后检查、报表 |

本讲脚本的文件头注释写得很直白：它"把编译日期和时间写到包含 fpga_base 的设计里"，见 [`fpga_base.tcl:11-12`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L11-L12)；文件末尾还有一行点睛注释 "Execute tcl script tcl.pre in optimize design"，见 [`fpga_base.tcl:142`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L142)。

> **关键时机**：`opt_design`（优化设计）属于**实现阶段**，但它在 `synth_design` **之后**执行。也就是说，钩子跑的时候，综合已经完成，整张带层次名、带命名 cell 的网表已经摆在那里——这正是 `get_cells` 能按路径找到每一个 FDPE 的前提。如果挂在综合**之前**，cell 还不存在，脚本就无处下手。

### 2.2 这个脚本"挂"在哪里？

一个容易踩的坑：根目录的 `fpga_base.tcl` **并不在打包好的 IP 里**。我们查 `component.xml` 的 `xilinx_utilityxitfiles_view_fileset`（即 IP-XACT 的 "Utility XIT/TTCL" 文件集，用于登记工具型 TCL），它只登记了一个 logo 图片，见 [`component.xml:1289-1294`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1289-L1294)：

```xml
<spirit:fileSet>
  <spirit:name>xilinx_utilityxitfiles_view_fileset</spirit:name>
  <spirit:file>
    <spirit:name>doc/psi_logo_150.gif</spirit:name>
    <spirit:userFileType>LOGO</spirit:userFileType>
  </spirit:file>
</spirit:fileSet>
```

（`component.xml` 里出现的另一处 `drivers/fpga_base/data/fpga_base.tcl` 是**驱动目录下同名的另一个文件**，属于软件驱动文件集，与本讲的综合钩子无关。）

所以这套机制的工作方式是：**仓库提供脚本，由"使用 fpga_base 的那个 Vivado 工程"把它登记为实现阶段的 `tcl.pre` 钩子**（例如挂到 `impl_1` 的 `STEPS.OPT_DESIGN.TCL.PRE`）。这也呼应文件头说的"the block design **containing** the fpga_base component"——它作用在"包含 fpga_base 的那个设计"上，而不是 IP 打包本身。具体登记命令属于消费方工程的事，本讲聚焦脚本自身的行为。

### 2.3 回顾：INIT 是什么、为什么要改它

`INIT` 是 FDPE 触发器的一个**属性**（不是端口）：FPGA 上电配置完成、全局复位释放的那一瞬间，Q 的值就是 `INIT`。u3-l1 已论证：因为本设计把 FDPE 的 `D/CE/PRE` 全接成常量，运行期 Q 永不翻转，所以 **Q 永久等于 INIT**。

那么"每次编译都更新固件时间"就等价于"每次编译都把当前年月日时分写进这 160 个 FDPE 的 INIT"。问题是 INIT 是网表属性，VHDL 源码里写不了"当前时间"——VHDL 不知道几点。于是让一个知道时间的外部脚本（TCL，能调系统时钟），在综合产物（网表）出现之后，**回写**这些属性。这就是本讲脚本的全部职责。

### 2.4 TCL 小抄

本讲脚本用到几个 TCL 要素，先混个脸熟：

- `clock seconds`：返回当前 Unix 时间戳（秒）。
- `clock format $t -format %Y`：把时间戳格式化，`%Y` 年、`%m` 月、`%e` 日（空格补齐）、`%k` 时（24 小时制，空格补齐）、`%M` 分。
- `scan $s "%d" v`：把字符串 `$s` 按十进制解析成整数存入 `v`（顺便剥掉 `%e/%k` 的前导空格）。
- `string index $s $i`：取字符串第 `$i` 个字符（0 基）。
- `get_cells <模式>`：Vivado 命令，按层次/通配模式返回网表里的 cell 对象。
- `set_property <属性> <值> <对象>`：给对象设属性。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [`fpga_base.tcl`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl) | 本讲主角。`tcl.pre` 钩子脚本。定义 `dec2bin` 与 `fpga_base` 两个过程，逐位把当前年月日时分写进对应 FDPE 的 `INIT`。 |
| [`hdl/fpga_base_date_package.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd) | 上一篇主角，本讲作"被改写方"。它的 `gen_year/month/...` generate 语句产生了脚本要寻址的那 160 个 FDPE cell，命名规律是脚本赖以工作的基础。 |
| [`hdl/fpga_base_v1_0.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd) | 顶层。实例化 `fpga_base_date_inst`，并注释说明日期"由 Vivado 在综合后跑的 tcl 脚本设定"。 |
| [`component.xml`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml) | IP-XACT 元数据。用于**反证**根目录 `fpga_base.tcl` 不在打包 IP 内（见 2.2）。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 TCL 综合钩子**（脚本整体结构、何时跑、如何取当前时间）、**4.2 INIT 属性回写**（`dec2bin` + 按位循环 + `set_property`）、**4.3 触发器实例路径**（`*/fpga_base_inst/U0/fpga_base_date_inst/gen_year[x].year_dfpe_inst` 这条层次串是怎么来的、为什么必须可预测）。三者是一条流水线：钩子负责"在对的时机被调起来"，回写负责"把每一位日期落到 INIT 上"，路径负责"让脚本能精确找到每一位"。

### 4.1 TCL 综合钩子

#### 4.1.1 概念说明

这个脚本要解决的问题是：**VHDL 不知道"现在几点"，但 TCL 知道；而能改 FDPE 初值的时机又必须在综合之后。** 把这两件事接起来的胶水，就是"挂在实现阶段 `opt_design` 之前的 `tcl.pre` 钩子"。

钩子的本质是一段"在工具流水线某个固定卡点被自动 source 的 TCL"。它和普通脚本的区别不在于语法，而在于**触发方式**：普通脚本要人手动 `source`，钩子由 Vivado 在跑到对应 step 时自动调用。本脚本文件末尾直接写了一句 [`fpga_base`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L144)——即"被 source 时立即执行 `fpga_base` 过程"，这正是钩子脚本的典型写法：**文件被 source → 顶层语句执行 → 调用干活的过程**。

#### 4.1.2 核心流程

脚本整体结构（两个 `proc` + 一句顶层调用）：

```
┌─────────────────────────────────────────────┐
│  proc dec2bin {i {width {}}}                 │  十进制→定宽二进制字符串
│      ……                                      │
├─────────────────────────────────────────────┤
│  proc fpga_base {}                           │  主过程
│    1. clock seconds          取当前时间戳     │
│    2. 对 year/month/day/hour/minute 各：      │
│         clock format 抽出字段 → scan 成整数   │
│         dec2bin <int> 32     得 32 位字符串   │
│         for 每一位:                          │
│            get_cells <路径>                   │
│            set_property INIT <位值> <cell>    │
├─────────────────────────────────────────────┤
│  fpga_base        ← 文件被 source 时立即跑    │
└─────────────────────────────────────────────┘
```

取当前时间的核心几句（"年"为例）在 [`fpga_base.tcl:50-57`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L50-L57)：先用 `clock seconds` 拿时间戳，再用 `%Y` 抽出年份字符串，`scan` 转成整数 `c_date_int`，最后 `dec2bin $c_date_int 32` 得到 32 位二进制字符串 `binYear`。月/日/时/分只是把 `%Y` 换成 `%m`/`%e`/`%k`/`%M`，其余完全对称，见 [`fpga_base.tcl:70`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L70)、[`fpga_base.tcl:87`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L87)、[`fpga_base.tcl:104`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L104)、[`fpga_base.tcl:121`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L121)。

> **为什么是"每次编译都更新"**：因为脚本读的是 `clock seconds`（编译机的墙上时间），而它又挂在一个每次实现都会触发的钩子上。所以每次 `impl` 跑到 `opt_design` 之前，这 160 个 INIT 都被刷新成"当下"。这正是顶层注释里"it updates every time the code is compiled"的由来，见 [`hdl/fpga_base_v1_0.vhd:236-240`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L236-L240)。

#### 4.1.3 源码精读

文件头与主过程入口：

文件头注释点明用途——[fpga_base.tcl:11-12](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L11-L12) 说明"把编译日期时间写到包含 fpga_base 的 block design"。

主过程开头打印并取时间戳，见 [`fpga_base.tcl:46-57`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L46-L57)：

```tcl
proc fpga_base {} {
   puts "Compilation date and time is set to:"
   set date_raw     [clock seconds]
   set c_date [clock format $date_raw -format %Y]
   scan $c_date "%d" c_date_int
   set c_date_int [expr {$c_date_int}]
   puts "C_DATE_YEAR   : $c_date_int"
   set binYear [dec2bin $c_date_int 32]
   puts "$binYear"
   ...
```

这段中文注解：`clock seconds` 拿当前时间戳；`clock format ... -format %Y` 抽出四位年份字符串；`scan "%d"` 转整数；`dec2bin $c_date_int 32` 得到 32 位 MSB 在前的二进制字符串。随后进入逐位写 INIT 的循环（4.2 详讲）。

顶层这一句让脚本在被 source 时立刻执行主过程，见 [`fpga_base.tcl:144`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L144)：

```tcl
fpga_base
```

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认钩子的触发时机与"每次编译都更新"的关系。
2. **步骤**：打开 [`fpga_base.tcl`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl)，定位第 50 行的 `clock seconds` 与第 142 行的注释；再打开 [`hdl/fpga_base_v1_0.vhd:236-240`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L236-L240) 的注释。
3. **观察**：注意"时间来源"（编译机墙上时间）与"写入时机"（`opt_design` 的 `tcl.pre`）这两个要素分别落在哪一行。
4. **预期**：能用自己的话说清"为什么改了 HDL 不重新跑实现，日期就不会变"——因为 INIT 只在实现阶段那次钩子触发时才被回写。
5. 若要实证钩子确实被调，需在 Vivado 实现日志中查找脚本 `puts` 出的 `Compilation date and time is set to:` 字样；无 Vivado 环境时标注**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把这段脚本挂到 `synth_design` 的 `tcl.pre`（综合之前），会发生什么？

> **答案**：综合尚未跑，网表里还没有任何 FDPE cell，`get_cells */.../gen_year[*].year_dfpe_inst` 会返回空，`set_property` 找不到对象而报错（或静默无效）。所以它必须挂在综合**之后**的 step 上（本设计选 `opt_design` 的 `tcl.pre`）。

**练习 2**：脚本里 5 个字段（年月日时分）的写法高度重复。如果要把分辨率从"分"提升到"秒"，要改哪几处？

> **答案**：新增一组 `%S`（秒）的 `clock format` + `scan` + `dec2bin` + 逐位循环，循环里把 cell 路径换成 `gen_second[$x].second_dfpe_inst`；同时在 `fpga_base_date_package.vhd` 里加一组 `gen_second` generate 与一个 `o_second` 端口，并在顶层接出一个新寄存器。脚本本身只是"五个几乎一样的块"的堆叠，扩展点是机械的。

### 4.2 INIT 属性回写

#### 4.2.1 概念说明

知道"当前是 2026 年 7 月 20 日 14 时 30 分"还不够，要把这个信息**拆成位**，再**逐位塞进**对应 FDPE 的 `INIT` 属性。这一步有两难点：

1. **十进制→定宽二进制字符串**：TCL 没有现成的"给我 32 位二进制"运算符，需要手写 `dec2bin`，而且必须**左补零到 32 位**，保证字符串长度恒为 32、第 0 个字符恒是最高位（bit 31）。
2. **位与实例的对应**：二进制字符串是"从左到右、MSB 在前"的一维序列，而 FDPE 实例按"位编号"命名（`gen_year[x]` 存第 x 位）。把这两套编号对齐，就是那个 `x` 递减 / `y` 递增的双游标循环。

`set_property INIT <0或1> <cell>` 是 Xilinx 的标准网表改写命令：它直接改 cell 的 `INIT` 属性，等价于改这个触发器上电那一刻的初值。因为 FDPE 的 Q 永久等于 INIT（u3-l1 结论），所以改 INIT 就是改"这一位日期"。

#### 4.2.2 核心流程

`dec2bin` 做两件事：先用"除 2 取余、逆序拼接"得到二进制串，再按指定宽度左补零。设输入整数 \(n\)、目标宽度 \(w=32\)，先得到自然二进制串 \(s\)（MSB 在前，长度 \(L = \lceil\log_2(n+1)\rceil\)），再左补 \(w - L\) 个 '0'。结果是一个长度恰为 \(w\) 的串 \(B\)，满足：

\[
B[\,i\,] \;=\; \text{bit}_{w-1-i}(n), \qquad i \in [0,\,w-1]
\]

即"字符串下标 \(i\)"对应"第 \(w-1-i\) 位"。所以 \(B[0]\) 是 bit 31（MSB），\(B[31]\) 是 bit 0（LSB）。

逐位回写循环（"年"为例）维护两个游标：\(x\) 从 31 递减到 0，\(y\) 从 0 递增到 31，且全程 \(x + y = 31\)（即 \(x = 31 - y\)）。每次取 \(B[y]\) 写到 `gen_year[x]`：

\[
\mathrm{INIT}(\,gen\_year[x]\,) \;\leftarrow\; B[y] \;=\; \text{bit}_{31-y}(n) \;=\; \text{bit}_{x}(n)
\]

也就是说 **`gen_year[x]` 收到的是第 \(x\) 位的值**——与 VHDL 里 `Q => year(count)`（count 即 x，驱动 `year(x)`）完全一致。两个游标反向滑动，本质是在"MSB 在前的字符串"和"按位编号的实例数组"之间做对齐。

用伪代码概括：

```
binYear = dec2bin(year, 32)          # 32 字符，B[0]=bit31 … B[31]=bit0
x = 31; y = 0
while x >= 0:
    bit = binYear[y]                 # = bit_(31-y) = bit_x
    cell = get_cells(".../gen_year[x].year_dfpe_inst")
    set_property INIT bit cell       # gen_year[x] 的初值 ← bit_x
    x -= 1; y += 1                   # 保持 x + y = 31
```

#### 4.2.3 源码精读

先看 `dec2bin`，定义在 [`fpga_base.tcl:18-41`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L18-L41)：

```tcl
proc dec2bin {i {width {}}} {
   set res {}
   ...
   while {$i>0} {
      set res [expr {$i%2}]$res      ;# 取余并"前插"→ 自然得到 MSB 在前
      set i [expr {$i/2}]
   }
   if {$res eq {}} {set res 0}
   if {$width ne {}} {
      append d [string repeat 0 $width] $res    ;# 左边拼一串 0
      set res [string range $d [string length $res] end]  ;# 截取最右 width 位
   }
   return $sign$res
}
```

中文注解：`while` 循环用"除 2 取余、把余数前插"构造 MSB 在前的自然二进制串 `res`；`width` 分支先在左边拼上 `width` 个 '0'，再从"原 `res` 长度"位置截到末尾，正好留下宽度为 `width` 的定宽串（左补零）。注意截取起点用的是 `[string length $res]`（补零前的原长度），这一步保证结果恒为 `width` 位。

逐位写 INIT 的循环（"年"），见 [`fpga_base.tcl:59-68`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L59-L68)：

```tcl
set x 31
set y 0
while {$x>=0} {
   set val [string index $binYear $y]                              ;# 取第 y 个字符
   set reg */fpga_base_inst/U0/fpga_base_date_inst/gen_year[$x].year_dfpe_inst
   puts "reg is $reg: x is $x : val is $val"
   set_property -verbose INIT $val [get_cells $reg]                ;# 回写 INIT
   set x [expr {$x - 1}]
   set y [expr {$y + 1}]
}
```

中文注解：`x` 从 31 递减、`y` 从 0 递增；`string index $binYear $y` 取二进制串第 `y` 位字符；`get_cells $reg` 按层次路径找到那个 FDPE cell；`set_property -verbose INIT $val` 把它初值设成该字符（'0' 或 '1'）。月/日/时/分四个循环结构完全相同，仅字段名与路径里的 `gen_xxx` 不同，见 [`fpga_base.tcl:76-85`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L76-L85)（月）、[`fpga_base.tcl:93-102`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L93-L102)（日）、[`fpga_base.tcl:110-119`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L110-L119)（时）、[`fpga_base.tcl:127-136`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L127-L136)（分）。

**实跑一遍 year = 2026**（印证 4.2.2 的公式）：`dec2bin 2026 32` 得到 `binYear = "00000000000000000000011111101010"`（前 21 个 '0' + `11111101010`，共 32 字符）。挑三次迭代看：

| 迭代 | x（实例下标/位号） | y（串下标） | `binYear[y]` | 写入 | 含义 |
|------|----|----|----|----|----|
| 第 1 次 | 31 | 0 | '0' | `gen_year[31].INIT = 0` | bit 31 = 0 |
| 第 22 次 | 10 | 21 | '1' | `gen_year[10].INIT = 1` | bit 10 = 1（2026 的 1024 位） |
| 第 32 次 | 0 | 31 | '0' | `gen_year[0].INIT = 0` | bit 0 = 0 |

回到 VHDL：`gen_year[count]` 的 `Q => year(count)`（见 [`hdl/fpga_base_date_package.vhd:102-117`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L102-L117)），所以 `year(10)` = `gen_year[10]` 的 INIT = 1，恰好是 2026 的第 10 位 ✓。这就回答了 u3-l1 末尾的问题：**二进制串的最高位（下标 0 = bit 31）配对的是 `x = 31`，所以它落到 `gen_year[31]`**——根因是串为 MSB 在前，而 `x` 恰好按位号从 31 倒数。

#### 4.2.4 代码实践（源码阅读 + 手算型）

1. **目标**：亲手验证 `dec2bin` 的定宽与位对齐。
2. **步骤**：
   - 任取一个字段值（例如 day = 20）。用纸笔或 `tclsh` 跑 `dec2bin 20 32`，应得到 `"00000000000000000000000000010100"`（前 27 个 '0' + `10100`）。
   - 模拟循环：`x=31,y=0 → val='0' → gen_day[31]=0`；找到第一个 '1'，它在串下标 `y=27`，此时 `x = 31-27 = 4`，即 `gen_day[4].INIT = 1`（20 的 bit 4 = 16 ✓）；末尾 `y=31,x=0 → val='0'`。
3. **观察**：每次"串下标 y 的字符"都等于"位号 x 的值"，且 `x + y = 31` 恒成立。
4. **预期**：能说清"为什么串里第一个 '1' 出现在 y = 32 − ⌈log₂(n+1)⌉ 的位置，而它对应的实例下标 x = ⌈log₂(n+1)⌉ − 1 = n 的最高有效位编号"。
5. 若本地有 `tclsh`，可 `source` 本文件再 `puts [dec2bin 2026 32]` 直接对照；否则按上面手算，标注**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`dec2bin` 里若不传 `width`（即 `width = {}`），返回什么？为什么本脚本一定要传 32？

> **答案**：不传 `width` 时返回"自然宽度"二进制串（不带前导零，长度随数值变化）。本脚本必须传 32，是为了让 `string index $binYear $y` 的下标 `y` 与"位号"有固定换算关系（位号 = 31 − y）。若长度不定，下标 0 就不一定对应 bit 31，整个对齐就会错位。

**练习 2**：把循环里 `set x [expr {$x - 1}]` 和 `set y [expr {$y + 1}]` 都改成"同时从 0 递增到 31"（即 `set reg .../gen_year[$y]...`、`set val [string index $binYear $y]`），结果会对吗？

> **答案**：会错位。那样 `gen_year[y]` 收到的是 `binYear[y] = bit_(31−y)`，即 `gen_year[y]` 存的是第 `31−y` 位而不是第 `y` 位，整条向量变成"按位倒序"。正确做法必须让"实例下标"等于"位号"，所以要么像现在这样 `x` 倒数、`y` 正数且 `x=31−y`，要么单游标但写成 `gen_year[31−y]`。原作者用双游标是为了"实例下标用 x、串下标用 y"各表一义，可读性更好。

### 4.3 触发器实例路径

#### 4.3.1 概念说明

`set_property` 要改一个 cell，必须先**找到**它。Vivado 网表里每个 cell 都有一条唯一的层次路径（hierarchical path），形如 `顶层/子实例/.../叶子cell`。本脚本依赖的路径长这样：

```
*/fpga_base_inst/U0/fpga_base_date_inst/gen_year[$x].year_dfpe_inst
```

这段串能成立，靠的是**可预测的命名**：每一级的实例名要么来自 HDL 里写死的 label，要么来自 generate 循环的下标。只要命名稳定，脚本就能用通配符 + 下标变量精确点名每一位。这也是 u3-l1 反复强调"generate 命名规律是 TCL 寻址基础"的落点。

#### 4.3.2 核心流程

把这条路径从右往左拆，对应到 HDL 的五个层次：

```
*/ fpga_base_inst / U0 / fpga_base_date_inst / gen_year[x] . year_dfpe_inst
①      ②           ③        ④                   ⑤            ⑥
```

| 段 | 含义 | 来源 |
|----|------|------|
| ① `*/` | 顶层容器名通配（block design / 顶层工程名各异，用 `*` 吃掉） | 消费方工程 |
| ② `fpga_base_inst` | fpga_base 这个 IP 在设计里的实例名 | 消费方工程（实例化时命名） |
| ③ `U0` | Vivado IP 外壳里把 `*_v1_0` 实体包一层，惯例把内层实例命名为 `U0` | Vivado IP 包装惯例 |
| ④ `fpga_base_date_inst` | 日期组件实例 | 顶层 HDL 写死的 label：[`hdl/fpga_base_v1_0.vhd:241`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L241) |
| ⑤ `gen_year[x]` | generate 循环的第 x 个副本 | HDL 的 `for count in 0 to 31 generate`：[`hdl/fpga_base_date_package.vhd:102`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L102) |
| ⑥ `year_dfpe_inst` | 该副本里的 FDPE 实例 label | HDL 写死的 label：[`hdl/fpga_base_date_package.vhd:107`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L107) |

整条链的"可寻址性"是分工协作的结果：HDL 负责 ④⑤⑥（写死 label + 规整的 generate 下标），让叶子 cell 的相对路径完全可预测；脚本只负责用 `*/` 吃掉不可控的顶层容器名，再用 `$x` 代入下标遍历 32 个副本。

> **和 `dont_touch` 的关系**（接 u3-l1）：光有可预测路径还不够——综合器若把"输入全常量"的 FDPE 当死逻辑删掉，路径就指向了一个不存在的 cell。所以必须靠 `dont_touch` 把这 160 个 FDPE 原样留在网表里，路径才有效。两条机制缺一不可。

#### 4.3.3 源码精读

日期组件用 generate 造 32 个 FDPE，命名 `gen_year` + `year_dfpe_inst`，见 [`hdl/fpga_base_date_package.vhd:102-117`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L102-L117)：

```vhdl
gen_year : for count in 0 to 31 generate
   attribute dont_touch : string;
   attribute dont_touch of year_dfpe_inst: label is "true";
begin
   year_dfpe_inst: FDPE
   port map (
      PRE => '0', CE => '0', C => i_clk, D => '0',
      Q   => year(count)
   );
end generate;
```

中文注解：`for count in 0 to 31` 配 label `year_dfpe_inst`，综合后产生 32 个 cell，路径形如 `gen_year[0].year_dfpe_inst … gen_year[31].year_dfpe_inst`；`Q => year(count)` 把第 count 个副本的输出接到 `year(count)`，即第 count 位——这正是 4.2 里"`gen_year[x]` 存第 x 位"的硬件依据。`dont_touch` 防止这批 FDPE 被优化删除。

顶层实例化日期组件，label 为 `fpga_base_date_inst`，见 [`hdl/fpga_base_v1_0.vhd:241-264`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L241-L264)：

```vhdl
fpga_base_date_inst: entity work.fpga_base_date
   generic map ( ... C_USE_GENERIC_DATE => C_USE_INFO_FROM_SCRIPT )
   port map ( i_clk => s00_axi_aclk,
              o_year  => reg_rdata( 1), ... o_minute => reg_rdata( 5) );
```

中文注解：实例 label `fpga_base_date_inst` 决定了路径里的第 ④ 段；它的输出 `o_year..o_minute` 直接驱动只读寄存器 `reg_rdata(1..5)`（即偏移 0x04~0x14，回顾 u2-l3）。注意 generic `C_USE_GENERIC_DATE` 被顶层接到总闸 `C_USE_INFO_FROM_SCRIPT`，这决定了双模式走向（见综合实践第 3 问）。

脚本里的寻址串（"年"），见 [`fpga_base.tcl:63`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L63)：

```tcl
set reg */fpga_base_inst/U0/fpga_base_date_inst/gen_year[$x].year_dfpe_inst
```

中文注解：`*/` 通配顶层；`$x` 是循环变量（31→0），逐个点名 32 个 FDPE 副本。其余四个字段只是把 `gen_year/year_dfpe_inst` 换成 `gen_month/month_dfpe_inst` 等，见 [`fpga_base.tcl:80`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L80)、[`fpga_base.tcl:97`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L97)、[`fpga_base.tcl:114`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L114)、[`fpga_base.tcl:131`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L131)。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：把"路径串的每一段"与"HDL 里的命名源"对上号。
2. **步骤**：把 [`fpga_base.tcl:63`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L63) 的路径拆成 ①~⑥ 六段；逐段在 HDL 里找命名来源（`fpga_base_date_inst` ↔ [`hdl/fpga_base_v1_0.vhd:241`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L241)；`gen_year`/`year_dfpe_inst` ↔ [`hdl/fpga_base_date_package.vhd:102-107`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L102-L107)）。
3. **观察**：注意哪些段是 HDL 写死的 label、哪些是 generate 下标、哪些是工具/工程决定（`U0`、`fpga_base_inst`、`*/`）。
4. **预期**：能解释"为什么把 label `year_dfpe_inst` 改名会导致脚本寻址失败"——脚本里的串和 HDL 的 label 是又一份"无编译期联动、必须人工同步"的契约（和 u2-l3 讲的 HDL/C 寄存器偏移契约同类）。
5. 无 Vivado 时为静态阅读；若要实证，可在 Vivado Tcl Console 综合 IP 后跑 `get_cells */.../gen_year[*].year_dfpe_inst`，应列出 32 个 cell。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：路径里 `*/` 为什么用通配符，而不写死顶层名？

> **答案**：因为这个 IP 会被不同的顶层工程/不同的 block design 实例化，顶层名字不固定。用 `*/` 吃掉顶层及以上的容器名，让脚本对不同工程都适用；真正的"身份信息"集中在后面几段（`fpga_base_inst/U0/fpga_base_date_inst/...`），这些是稳定的。

**练习 2**：如果某天有人把 HDL 里 `year_dfpe_inst: FDPE` 改名成 `year_ff_inst: FDPE`，但不改 TCL，会发生什么？

> **答案**：`get_cells */.../gen_year[$x].year_dfpe_inst` 找不到匹配的 cell（名字对不上），返回空，`set_property INIT` 因对象不存在而报错/无效；即便不报错，相应 FDPE 的 INIT 仍为默认值 0，`year` 向量对应位读出 0，固件日期会出错。这就是路径契约被破坏的后果——改 HDL 的 generate label 必须同步改 TCL 里的串。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**追踪 + 判断**任务（对应本讲规格里的实践任务）。

**任务**：以 [`fpga_base.tcl:59-68`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L59-L68) 的"年"写入循环为对象，回答三个问题。

1. **位映射**：解释 `x` 从 31 递减、`y` 从 0 递增时，二进制位与 FDPE 实例下标如何对应。
   - 提示：先确定 `binYear` 是 MSB 在前（`dec2bin` 的 `width=32` 左补零保证 `binYear[0] = bit31`），再用 `x + y = 31` 推出 `gen_year[x]` 收到的字符 = `binYear[y] = bit_(31−y) = bit_x`。
   - 结论：**实例下标 = 位号**，`gen_year[x]` 恰好存第 x 位；二进制串的最高位（下标 0）配 `x=31`，落到 `gen_year[31]`。

2. **寻址依据**：对照 [`hdl/fpga_base_date_package.vhd:102-117`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L102-L117) 的 generate，指出 `gen_year[x].year_dfpe_inst` 这个名字的每一部分分别由 HDL 的什么语法决定（`for ... generate` 给出 `gen_year[x]`，label 给出 `year_dfpe_inst`），并说明 `dont_touch` 在这里扮演的"保命"角色。

3. **模式判断**：若顶层 `C_USE_INFO_FROM_SCRIPT` 设为 `true`（即 generic 模式），这个 TCL 脚本还需要执行吗？为什么？
   - 线索 1：generic 模式下日期走 `g_generics` 分支，直接 `o_year <= to_unsigned(C_DATE_YEAR, 32)`，数据来自 `BuildYear_c` 等 generic（由 `update_version.py` 在综合前替换 `$$tag$$` 注入，u3-l3 详讲），见 [`hdl/fpga_base_date_package.vhd:187-194`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L187-L194)。
   - 线索 2：此时 `g_ngenerics` 不生效，FDPE 的 Q（内部信号 `year` 等）**未被消费**；但它们带 `dont_touch`，仍留在网表里（u3-l1 讲过的"被接受的轻微冗余"）。
   - 结论：**不需要执行**。日期已经从 generic 正确来，FDPE 的输出没人读，写它们的 INIT 没有任何功能效果（即便跑了也只写到"死单元"上，无害但也无益）。实际上 generic 模式走的是另一条注入路径（`update_version.py` 综合前改 HDL），与这条综合后写 INIT 的 TCL 钩子是**互为替代**的两套机制。

> **交付物**：一段话 + 一张表（x / y / `binYear[y]` / 目标 cell / 位号）。结论里务必点出"两条机制互斥、generic 模式下本脚本可省"这一关键判断。实证（在 Vivado 里分别以两种 `C_USE_INFO_FROM_SCRIPT` 打包并查日志）**待本地验证**。

## 6. 本讲小结

- 根目录 [`fpga_base.tcl`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl) 是一段 `tcl.pre` 钩子，挂在实现阶段的 `opt_design` 之前（综合之后），时机选在"网表已存在、cell 可寻址"的卡点上。
- 它由"使用 fpga_base 的工程"负责登记；打包出的 IP（`component.xml` 的 utility 文件集）里只带 logo，并不包含这个脚本。
- `dec2bin` 把十进制时间字段转成**定宽 32 位、MSB 在前**的二进制串；这是后续按下标对齐位的前提。
- 逐位循环用 `x`（31→0）和 `y`（0→31）双游标，满足 `x + y = 31`，使 `gen_year[x]` 恰好收到第 x 位——与 VHDL 的 `Q => year(count)` 完全一致；这也解释了"串最高位 ↔ `gen_year[31]`"。
- 写入靠 `set_property -verbose INIT $val [get_cells $reg]`，直接回写 FDPE 上电初值；依赖可预测的层次路径 `*/fpga_base_inst/U0/fpga_base_date_inst/gen_year[x].year_dfpe_inst` 与 `dont_touch` 保住的 cell。
- 这条"TCL 综合钩子写 INIT"与 u3-l3 将讲的"`update_version.py` 写 generic"是**两条互斥**的编译时间注入路径；generic 模式下本脚本无需执行。

## 7. 下一步学习建议

- **紧接着读 u3-l3**：去看另一条路径——`scripts/update_version.py` 如何用 gitpython 读 git hash、用正则把 `hdl/fpga_base_scripted_info_pkg.vhd` 里的 `$$tag$$` 占位符替换成版本/时间，并在脏仓库、`assume-unchanged` 上做特殊处理。读完后你就能完整对比"综合前改 HDL（generic 模式）"与"综合后写 INIT（本讲）"两条路径的取舍。
- **想巩固"属性回写"手法**：在 Vivado 文档里查 `set_property INIT` 与 `get_cells` 的通配语法，理解 `*/`、`[]`、层级分隔符 `/` 在网表寻址中的含义。
- **想往上走**：这套"用真实原语 cell + dont_touch + 综合 hook 回写属性"的套路，在需要把"编译期信息"或"一次性配置"烧进比特流的设计里很常见；可以留意 psi_common 或其他 PSI IP 是否有类似实践。
- **回顾闭环**：结合 u2-l3 的寄存器映射，确认本讲写的固件日期最终出现在偏移 `0x04~0x14`（`reg_rdata(1..5)`），并能在 u5-l1 的 C 驱动 `__DATE__/__TIME__` 回写的软件日期（偏移 `0x18~0x28`）旁边形成"固件日期 vs 软件日期"的对照——这正是 fpga_base 用来核对"软硬配套"的核心读数。
