# Tcl 流程自动化模式

## 1. 本讲目标

学完本讲，你应当能够：

- 看懂并改写 `Vpad.tcl` 这类「用 `for` 循环按 die 边界批量生成坐标」的脚本，理解坐标如何从 `get_core_area` 一路抽取出来。
- 理解 `createpathgroup.tcl` 把所有时序路径切成 f2f / i2f / f2o / i2o 四组的写法，并能自己新增路径组。
- 理解 `lef_layer_tf_number_mapper.tcl` 如何用纯 Tcl（`open`/`gets`/`regsub`/`proc`/状态机）把两份文本文件加工成映射表，体会「库数据脚本化」的套路。
- 掌握 Synopsys 工具里两类最常用的 Tcl 设施：**collection（集合）API** 与 **`expr` 算术**，并知道它们分别出现在哪些场景。

本讲是「自动化与脚本进阶」单元的第二讲，承接 [u8-l1 NDR 路由规则自动化](u8-l1-ndr-rule-automation.md)（用 Perl 自动生成 Tcl），把视角从「单点脚本」拉到「四类通用自动化模式」。

## 2. 前置知识

本讲默认你已经建立以下认知（若陌生，请先读对应讲义）：

- **Tcl 基础语法**：变量用 `$` 取值、命令以换行或 `;` 分隔、`[...]` 表示命令替换、列表用空格分隔。这是阅读所有 `.tcl` 脚本的前提。
- **ICC2 物理设计主流程**（U4）：尤其要知道 floorplan 阶段的 die/core 边界、电源网络（PG）的 mesh/ring/rail 结构。本讲的 `Vpad.tcl` 正是 [u4-l3 电源网络设计](u4-l3-power-network.md) 里「撒虚拟电源 pad 做 IR drop 分析」那段脚本，那里讲了它的**用途**，本讲专门拆它的** Tcl 写法**。
- **collection 概念**：Synopsys 工具里 `all_inputs`、`all_registers` 这类命令返回的不是普通列表，而是「集合」对象，需要专门的 `foreach_in_collection`、`get_attribute` 来操作。这一点在 [u6-l2 case analysis 追踪](u6-l2-primetime-case-propagation.md) 里已用过。
- **`.tf` / LEF 文件的层定义格式**：[u3-l3 LEF 到 FRAM 的层映射](u3-l3-lef-to-fram-mapping.md) 已经用 Perl 版脚本讲透了「派生掩模名」的推导规则（metalN / viaN / poly / polyCont）。本讲讲同逻辑的 Tcl 版，**推导规则不重复**，只讲 Tcl 实现差异。

> 阅读提示：本仓库的 EDA 脚本几乎都是「模板」，参数（库名、层名、坐标）要按真实工艺替换。学模式，不要背数值。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 | 本讲关注点 |
|---|---|---|---|
| `IC Compiler II/Vpad.tcl` | 22 | 沿 die 四边均匀放置虚拟电源 pad，再做 IR drop 分析 | 几何坐标循环 + `expr` + `format` |
| `mentor_scripts/createpathgroup.tcl` | 9 | 把时序路径分成 f2f/i2f/f2o/i2o 四组 | collection 运算 + `group_path` |
| `LEF2FRAM/lef_layer_tf_number_mapper.tcl` | 186 | 解析 `.tf` 与 LEF，生成层号映射 `.map` 与日志 `.log` | 文件 I/O + `proc` + 状态机 + 数组 |

三个文件分属三个工具链（ICC2、Mentor Nitro、LEF2FRAM 工具集），但抽取出的 Tcl 模式是通用的——这正是本讲的价值：**学会模式，换工具也能用**。

## 4. 核心概念与源码讲解

### 4.1 几何坐标循环生成 pad

#### 4.1.1 概念说明

电源网络的 IR drop（电压降）分析需要一个「电流从哪里注入」的模型。真实芯片的电源从封装的 pad/bump 灌进来；但在核级（core-level）设计阶段，我们还没有真实的 IO pad，于是用 `set_virtual_pad` 在 die 边界上「假装」摆一排电源注入点，让 `analyze_power_plan` 能解电阻网络、估出最坏压降（详见 [u4-l3](u4-l3-power-network.md)）。

问题是：一颗 die 的四条边上要摆几十上百个 pad，手写每一个坐标既笨又易错。自动化的办法是——**算出 die 的四个角，然后用两个 `for` 循环分别沿 x、y 方向「步进」地摆**。这就是「几何坐标循环」模式：把规则几何（等间距布点）翻译成循环。

#### 4.1.2 核心流程

```
1. 取 die/core 的 bounding box（外接矩形）四个角坐标
        get_attribute [get_core_area] bbox  →  {{llx lly} {urx ury}}
        用两层 lindex 抽出 llx / lly / urx / ury
2. for 循环 A：沿 x 方向步进（步长 80）
        在底边 (y=lly) 与顶边 (y=ury) 各放一对 VSS/VDD pad
3. for 循环 B：沿 y 方向步进（步长 80）
        在左边 (x=llx) 与右边 (x=urx) 各放一对 VSS/VDD pad
4. analyze_power_plan 算 IR drop
```

关键参数的几何含义：

- **步长 80**：同一个网络（如 VSS）相邻两个 pad 沿边的间距。
- **偏移 40**：`i+40` 让 VDD 与 VSS **交错（interleaving）** 排布——VSS 在 `i`，VDD 在 `i+40`，于是 VDD 与 VSS 沿边交替出现，间距变为 40。
- **起点 `+20`、终点 `-40`**：给 die 的四个角留出安全边距，避免 pad 撞到角上的其他结构。

用数学语言描述底边的布点：对于步长 \(s=80\)、交错偏移 \(o=40\)，第 \(k\) 个 VSS pad 的 x 坐标为

\[
x_k = x_{\text{start}} + k\cdot s,\quad k=0,1,\dots,\left\lfloor\frac{x_{\text{end}}-x_{\text{start}}}{s}\right\rfloor
\]

对应 VDD pad 的 x 坐标为 \(x_k + o\)。

#### 4.1.3 源码精读

**第一步：抽四个角。** [Vpad.tcl:1-4](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Vpad.tcl#L1-L4) 这四行从 `get_core_area` 取出 bounding box，再用「两层 `lindex`」剥出四个标量坐标：

```tcl
set die_llx [lindex [lindex [ get_attribute [get_core_area] bbox] 0] 0]
set die_lly [lindex [lindex [ get_attribute [get_core_area] bbox] 0] 1]
set die_urx [lindex [lindex [ get_attribute [get_core_area] bbox] 1] 0]
set die_ury [lindex [lindex [ get_attribute [get_core_area] bbox] 1] 1]
```

`bbox` 属性返回形如 `{{100.0 100.0} {900.0 900.0}}` 的两层列表：外层第 0 个是左下角、第 1 个是右上角；内层第 0 个是 x、第 1 个是 y。所以 `[lindex [...] 0] 0` = 左下角 x（llx），以此类推。

> 阅读提示：命令名是 `get_core_area`（取核心放置区），但变量被命名为 `die_*`（die 外框）。两者在真实流程里不一定是同一矩形，这里作者把 core 边界当作 die 边界来用——这是模板的简化，真实项目要确认你想布点的是哪条边。

**第二步：沿 x 步进摆 pad。** [Vpad.tcl:6-12](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Vpad.tcl#L6-L12) 用 Tcl 的 `for {初始} {条件} {步进} {循环体}` 经典三段式：

```tcl
for {set i "[expr $die_llx + 20]"} {$i < "[expr $die_urx - 40]"} {set i [expr $i + 80]} {
    set_virtual_pad -net VSS -coordinate [format {%.1f %.1f} $i $die_lly]
    set_virtual_pad -net VDD -coordinate [format {%.1f %.1f} [expr $i + 40] $die_lly]
    ...
}
```

要点：

- `for` 的初始/条件/步进三段都用 `[expr ...]` 做算术，Tcl 不会自动把 `$i + 80` 当表达式，必须套 `expr`。
- `[format {%.1f %.1f} $i $die_lly]` 把两个数字格式化成 `"120.0 50.0"` 这样的坐标字符串，正是 `-coordinate` 想要的形式（C 风格 `printf` 格式串）。
- 循环体里对底边（`$die_lly`）和顶边（`$die_ury`）各放一对 pad，所以一个 x 循环同时覆盖上下两条边。

**第三步：沿 y 步进摆 pad。** [Vpad.tcl:13-19](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Vpad.tcl#L13-L19) 结构完全对称，只是把自变量换成 y、把固定坐标换成左（`$die_llx`）/右（`$die_urx`）。

**第四步：算 IR drop。** [Vpad.tcl:21](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Vpad.tcl#L21) 把刚摆好的 pad 当作电流注入点求解：

```tcl
analyze_power_plan -power_budget 250 -voltage 1.2 -nets {VDD VSS} -use_terminals_as_pads
```

`-use_terminals_as_pads` 让工具用上面循环摆下的虚拟 pad 做注入点。由 \(P=VI\) 反推总电流：

\[
I = \frac{P}{V} = \frac{250\,\text{mW}}{1.2\,\text{V}} \approx 208\,\text{mA}
\]

再沿电阻网络算最坏压降 \(\Delta V = IR\)。

#### 4.1.4 代码实践

**实践目标**：直观感受「步长」对 pad 数量的影响。

**操作步骤**：

1. 读 [Vpad.tcl:6-12](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Vpad.tcl#L6-L12)，假设某 die 的 `die_llx=0`、`die_urx=800`（其余边暂不考虑）。
2. 在循环体开头加一行日志，打印每个 pad 的坐标：

   ```tcl
   puts "place VSS @ [format {%.1f %.1f} $i $die_lly]"
   ```

3. 把步长从 `80` 改成 `40`（三个地方：循环条件里的步进、以及若想保持交错间距比例也要同步调 `+40`）。

**需要观察的现象**：

- 改步长前，底边 VSS pad 的 x 坐标序列。
- 改步长后，pad 数量大约翻倍。

**预期结果**：步长 80 时，x 序列为 `20, 100, 180, ..., <760`（即 20, 20+80, 20+160, …）；步长 40 时序列变为 `20, 60, 100, ...`，pad 数量约翻倍，IR drop 会因注入点更密而改善。

> 本实践为「源码阅读 + 手算」型，不需要 EDA 环境；若要在 ICC2 里真跑，**待本地验证**具体 IR drop 数值。

#### 4.1.5 小练习与答案

**练习 1**：为什么 VDD pad 的坐标用 `i + 40` 而不是 `i + 80`？

**答案**：用 `i + 40`（半个步长）让 VDD 与 VSS 在同一条边上**交错**排布，相当于把两种网络的 pad 交替穿插，避免某一侧电源注入点过疏；若用 `i + 80`，VDD 会和 VSS 在同一 x 上重叠成对，丧失交错带来的均匀性。

**练习 2**：脚本用了两个独立的 `for` 循环，能不能合并成一个？

**答案**：理论上可以再加一层判断（按边类型分发坐标），但会牺牲可读性。两个循环分别管「水平边」和「竖直边」，是「可读性优先于简洁」的常见工程取舍。

---

### 4.2 路径分组（f2f / i2f / f2o / i2o）

#### 4.2.1 概念说明

静态时序分析（STA）里，一条时序路径（timing path）有一个**起点**和一个**终点**。起点通常是输入端口或寄存器的时钟引脚，终点通常是输出端口或寄存器的数据引脚。一个设计里可能有成千上万条路径，把它们**按起终点类型分桶**，就能分别看哪一类路径最差、分别优化——这就是 **path group（路径组）**。

`createpathgroup.tcl` 把所有路径分成四组，命名规律是「起点类型 → 终点类型」，其中 `f` = flip-flop（寄存器）、`i` = input（输入端口）、`o` = output（输出端口）：

| 组名 | 起点 | 终点 | 典型关注点 |
|---|---|---|---|
| `f2f` | 寄存器 | 寄存器 | 片内寄存器到寄存器，最常见 |
| `i2f` | 输入端口 | 寄存器 | 外部数据进入第一拍 |
| `f2o` | 寄存器 | 输出端口 | 最后一拍送出外部 |
| `i2o` | 输入端口 | 输出端口 | 纯组合直通路径 |

为什么要分组？因为优化工具（以及 `report_timing`）可以按组分别报告 WNS/TNS，让你一眼看出「是片内逻辑慢，还是 I/O 路径慢」——和 [u4-l2](u4-l2-floorplan.md) 里用拥塞图区分「逻辑慢/布线慢」是同一种诊断思路。

#### 4.2.2 核心流程

```
1. unq_input_list = all_inputs()                 ;# 拿到所有输入端口 collection
2. 遍历 all_clocks：
       把每个时钟端口从 input_list 里剔除         ;# 时钟端口不是数据输入
3. group_path 四次：
       f2f : reg → reg
       i2f : input_list → reg
       f2o : reg → output
       i2o : input_list → output
```

#### 4.2.3 源码精读

**先剔除时钟端口。** [createpathgroup.tcl:1-5](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/createpathgroup.tcl#L1-L5)：

```tcl
set unq_input_list [all_inputs]
foreach_in_collection unq_clock_element [all_clocks] {
     set unq_clock_name [get_port  $unq_clock_element]
     set unq_input_list [remove_from_collection $unq_input_list $unq_clock_name]
}
```

这里藏着一个**易错点**：时钟端口（如 `clk`）在物理上也是一个输入端口，所以会出现在 `all_inputs` 里。但它是**时钟源**，不该被当成「数据输入」塞进 `i2f`/`i2o` 路径组，否则会把时钟路径误统计成数据路径。于是先用 `all_clocks` 拿到所有时钟对象，再 `get_port` 转成端口，最后 `remove_from_collection` 从输入列表里减掉。

注意这里的 collection 运算三件套：

- `all_inputs` / `all_clocks`：返回 **collection**（不是普通 Tcl 列表）。
- `foreach_in_collection`：遍历 collection 的**专用**循环（普通 `foreach` 不行）。
- `remove_from_collection`：collection 的「减法」，返回新 collection。

**再建四个路径组。** [createpathgroup.tcl:6-9](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/createpathgroup.tcl#L6-L9)：

```tcl
group_path -name f2f -from [all_registers]  -to [all_registers] -critical_range 0.7
group_path -name i2f -from $unq_input_list   -to [all_registers] -critical_range 0.7
group_path -name f2o -from [all_registers]  -to [all_outputs]    -critical_range 0.7
group_path -name i2o -from $unq_input_list   -to [all_outputs]    -critical_range 0.7
```

- `-from` / `-to` 接收的又是 collection：`all_registers`、`all_outputs`。
- `-critical_range 0.7`：把 slack 落在最差值 0.7ns 范围内的路径都算作「关键路径」一起优化/报告，避免只盯绝对最差那一条而忽略「一窝」近似违例的路径。
- `$unq_input_list` 是上面**剔除时钟后**的输入集合，体现了变量在多条命令间复用。

#### 4.2.4 代码实践

**实践目标**：仿照该脚本，自己写一段只分 `in2reg` 与 `reg2out` 两组的 Tcl（即把名字换成更直观的语义，且只保留两组）。

**操作步骤**：

1. 新建一个 `.tcl` 文件（示例代码，不写进仓库源码）。
2. 同样先把时钟端口从输入列表里剔除（直接复用 [createpathgroup.tcl:1-5](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/createpathgroup.tcl#L1-L5) 的写法）。
3. 只发两条 `group_path`：

   ```tcl
   # 示例代码
   group_path -name in2reg  -from $unq_input_list -to [all_registers] -critical_range 0.7
   group_path -name reg2out -from [all_registers] -to [all_outputs]   -critical_range 0.7
   ```

**需要观察的现象**：在 PrimeTime 或 DC 里 `source` 这段脚本后，用 `report_timing -group in2reg` 看是否只报告输入到寄存器的路径。

**预期结果**：能按组名分别出报告；若忘了剔除时钟端口，`in2reg` 组里可能出现以时钟端口为起点的奇怪路径。**具体数值待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把第 1–5 行的「剔除时钟」整段删掉，直接 `set unq_input_list [all_inputs]`，四条 `group_path` 还能跑吗？会有什么副作用？

**答案**：能跑（命令本身不报错），但 `i2f`、`i2o` 两组的 `-from` 里会包含时钟端口，于是时钟路径会被误当成数据路径进入这两组，导致时序报告里多出本不该有的「输入到寄存器」路径，干扰判断。

**练习 2**：`group_path` 的 `-from`/`-to` 能不能直接写成端口名字符串，而不用 `all_registers` / `all_outputs`？

**答案**：可以，但要用 `get_ports` / `all_registers` 等**命令返回的对象**，而不是裸字符串。Synopsys 工具期望的是 collection 或对象引用；裸字符串只在能被解析成对象名时才偶然生效，写脚本时应坚持用 collection 命令。

---

### 4.3 库数据脚本化

#### 4.3.1 概念说明

[u3-l3](u3-l3-lef-to-fram-mapping.md) 用 **Perl 版** 脚本讲过如何把 `.tf` 工艺文件和 LEF 物理库加工成「层名 → 掩模层号」映射表，喂给老的 Milkyway/FRAM 流程。仓库里还有一份**功能等价的 Tcl 版**：`lef_layer_tf_number_mapper.tcl`。

为什么用 Tcl 重写一遍同一个功能？因为 Synopsys 工具原生 shell 就是 Tcl——如果整个流程都在 ICC/ICC2 的 Tcl 环境里跑，用 Tcl 版可以**省掉切换到外部 Perl 解释器**的开销，直接 `source` 即可。这就是「库数据脚本化」的价值：**把原本要手工或外部工具做的数据加工，写成一个可在 EDA 环境内运行的 Tcl 程序**。

本节不讲推导规则（metalN/viaN/poly/polyCont 已在 [u3-l3](u3-l3-lef-to-fram-mapping.md) 讲透），只讲这份 Tcl 程序的**结构套路**：参数解析、文件 I/O、`proc`、状态机、数组。这套套路换到任何「读文本 → 加工 → 写文本」的 EDA 辅助脚本都通用。

#### 4.3.2 核心流程

```
1. 解析命令行 argv：期望 2 个参数（.tf 路径、.lef 路径），不足则报 Usage 退出
2. 用 regsub 把后缀替换，拼出输出 .map / .log 文件名
3. open 三个输入/输出文件句柄
4. 定义两个 proc：print_log（写日志）、print_map（写映射）
5. 状态机扫 .tf：逐行 gets，靠标志位收集 maskName/layerNumber，存进数组
6. 状态机扫 LEF：逐行 gets，按 TYPE 派生掩模名，查 .tf 数组拿到层号
7. for 循环把结果写进 .map，关闭文件
```

#### 4.3.3 源码精读

**参数解析与文件名派生。** [lef_layer_tf_number_mapper.tcl:9-21](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.tcl#L9-L21)：

```tcl
set EXPECTED_COMMAND_ARGS 2
if {[llength $argv] < $EXPECTED_COMMAND_ARGS} {
    puts "Usage: lef_layer_tf_number_map.tcl Tech_file_name.tf Lef_file_name.lef"
    exit 1
} else {
    set tech_file [lindex $argv 0]
    set lef_file  [lindex $argv 1]
    set tmp_s1 [regsub {.tf}  $tech_file {_tf}]
    set tmp_s2 [regsub {.lef} $lef_file  {_lef}]
    set lef_tf_map_file "${tmp_s2}_${tmp_s1}.map"
    set lef_tf_log_file "${tmp_s2}_${tmp_s1}.log"
}
```

- `llength $argv` 数命令行参数个数；`$argv` 是 `wish`/`tclsh` 内置变量。
- `regsub {.tf} $tech_file {_tf}` 把 `foo.tf` 里的 `.tf` 替换成 `_tf`，得到 `foo_tf`，再拼成输出名 `bar_lef_foo_tf.map`。这是 Tcl 里「由输入名派生输出名」的常见手法。

**文件 I/O。** [lef_layer_tf_number_mapper.tcl:23-45](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.tcl#L23-L45) 用 `[open ... r]` / `[open ... w]` 打开读写句柄，每步都判空报错——典型的防御式写法。

**proc 定义。** [lef_layer_tf_number_mapper.tcl:47-55](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.tcl#L47-L55)：

```tcl
proc print_log {message} {
    puts $message
    puts $::lef_tf_log_file_id $message
}
```

`print_log` 同时往**屏幕**和**日志文件**写——这样跑脚本时既能实时看进度，又留了底。`$::lef_tf_log_file_id` 里的 `::` 是访问**全局变量**的写法（proc 内部默认看不到全局作用域，必须用 `::` 或 `global` 声明）。`print_map` 同理，往屏幕和 `.map` 文件双写。

> 阅读提示：[lef_layer_tf_number_mapper.tcl:177-185](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.tcl#L177-L185) 在文件末尾把 `print_log`/`print_map` **又定义了一遍**，与 47–55 行完全相同。这是冗余代码（第二次定义无意义），读代码时要意识到：proc 重复定义会静默覆盖前一个，不影响运行但属代码异味。

**状态机扫 `.tf`。** [lef_layer_tf_number_mapper.tcl:59-105](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.tcl#L59-L105)：

```tcl
while {[gets $tech_file_id line] != -1} {
    ;# ... 一串 regsub 清洗掉 [] {} " ; 等字符 ...
    if     {[regexp {^[lL][aA][yY][eE][rR]...} $line match tf_layer_name]} { ... }
    elseif {[regexp {^[\s]*\}.*$} $line]}                                  { ... }
    elseif {[regexp {...layerNumber...} $line match tf_layerNumber]}       { ... }
    elseif {[regexp {...maskName...}     $line match tf_maskName]}         { ... }
}
```

- `[gets $id line] != -1`：逐行读到文件尾（读不到返回 -1）。这是 Tcl 读文件的标配循环。
- 一串 `regsub -all`（69–76 行）先把方括号、花括号、引号、分号清洗掉，降低后续正则的复杂度。
- 四个 `regexp` 分支构成状态机：见到 `Layer 名 {` 进层、见到 `}` 出层、行内遇 `layerNumber=` 记层号、遇 `maskName=` 记掩模名。出层时（83–95 行）才把收集齐的 `maskName`/`layerNumber` 提交进数组——这就是 [u3-l3](u3-l3-lef-to-fram-mapping.md) 讲过的「**延迟提交**」，容忍块内字段顺序不固定。
- 三个 `array set`（60–62 行）声明关联数组，Tcl 数组用字符串当 key，这里 key 是掩模名或下标。

**状态机扫 LEF + 派生掩模名。** [lef_layer_tf_number_mapper.tcl:109-167](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.tcl#L109-L167) 结构同上，只是分支换成 LEF 的 `LAYER`/`TYPE`/`END`。核心派生逻辑（132–147 行）与 Perl 版等价：

```tcl
if {($lef_layerTYPE eq "ROUTING")} {
    if {$metal_index == 0} {incr metal_index}
    else                   {incr metal_index}
    set lef_derivedmaskName "metal$metal_index"
}
```

> 阅读提示：137–138 行的 `if/else` **两个分支都执行 `incr metal_index`**，是一段无效分支——无论 `metal_index` 是否为 0 都自增。效果上首个 ROUTING 层从 0 自增到 1 得 `metal1`，恰好和 Perl 版「首层置 1、其后自增」的结果一致，所以行为正确但写法冗余。读代码时遇到这种「两分支同动作」要警觉。

**输出映射。** [lef_layer_tf_number_mapper.tcl:170-175](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.tcl#L170-L175) 用普通 `for` 循环把数组内容逐行写进 `.map`，最后 `close` 关文件。

#### 4.3.4 代码实践

**实践目标**：理解「延迟提交」为何能容忍字段顺序。

**操作步骤**：

1. 读 [lef_layer_tf_number_mapper.tcl:83-95](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.tcl#L83-L95) 的「出层提交」分支。
2. 假设某 `.tf` 片段如下（`maskName` 写在 `layerNumber` **前面**）：

   ```
   Layer M1 {
       maskName = "metal1";
       layerNumber = 14;
   }
   ```

3. 追踪脚本读到这三行的状态变化：进层 → `found_maskName=1` → 记 `tf_layerNumber=14` → 出层时（`found_maskName==1`）把两个值一起写进数组。

**需要观察的现象**：即便字段顺序反过来（`layerNumber` 在前），出层时仍能同时拿到两个值。

**预期结果**：因为提交发生在「块结束」而非「字段出现」时刻，脚本对块内字段顺序不敏感——这正是状态机 + 延迟提交的优势。纯源码阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：脚本顶部的 shebang 是 `#!/usr/bin/env wish`（[第 1 行](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.tcl#L1)）。`wish` 和 `tclsh` 有什么区别？这个脚本用到了 `wish` 的特性吗？

**答案**：`wish` = Tcl + Tk（图形工具包），`tclsh` = 纯 Tcl 命令行 shell。本脚本全程是文件 I/O、字符串、数组，**没有用到任何 Tk 图形功能**，所以用 `tclsh` 也能跑；shebang 写 `wish` 只是作者习惯，并非必要。

**练习 2**：为什么 `print_log` 里访问日志文件句柄要写成 `$::lef_tf_log_file_id` 而不是 `$lef_tf_log_file_id`？

**答案**：因为文件句柄是在脚本顶层（全局作用域）`set` 的，而 `proc` 内部默认看不到全局变量。`::` 前缀显式引用全局作用域，等价于在 proc 里先写 `global lef_tf_log_file_id`。不加 `::` 会读到空值，导致 `puts` 报错。

---

### 4.4 collection 与 expr 用法

#### 4.4.1 概念说明

读完上面三个脚本，你会发现 Synopsys Tcl 自动化反复出现两类设施，把它们单独拎出来总结：

1. **collection（集合）API**：工具里「对象」的容器。端口、寄存器、时钟、网、引脚……几乎所有 `get_*` / `all_*` 命令返回的都是 collection，不是普通列表。
2. **`expr` 算术**：Tcl 把一切当字符串，`$a + $b` 不会被自动求值，必须套 `[expr ...]`。

这两者在前三个脚本里各司其职：collection 管「找对象」，`expr` 管「算坐标」。

#### 4.4.2 核心流程

```
collection 侧：产生 → 过滤 → 遍历 → 取属性
    all_inputs / all_registers / all_clocks / get_port / get_core_area   (产生)
    remove_from_collection                                              (过滤/运算)
    foreach_in_collection                                              (遍历)
    get_attribute ... bbox                                             (取属性)

expr 侧：
    [expr $a + $b]            算术
    [expr $i + 40]            坐标偏移
    [format {%.1f %.1f} x y]  格式化字符串
```

#### 4.4.3 源码精读

**collection 的产生与运算**（来自 `createpathgroup.tcl`）：

- `[all_inputs]`、`[all_registers]`、`[all_outputs]`、`[all_clocks]`——四个最常用的「全量」collection 产生器（[createpathgroup.tcl:1-9](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/createpathgroup.tcl#L1-L9)）。
- `[get_port $unq_clock_element]`——把时钟对象转成端口对象（[第 3 行](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/createpathgroup.tcl#L3)）。
- `[remove_from_collection $a $b]`——集合减法，从 `$a` 里去掉 `$b`（[第 4 行](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/createpathgroup.tcl#L4)）。
- `foreach_in_collection`——**唯一**能遍历 collection 的循环（[第 2 行](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/createpathgroup.tcl#L2)）。

> 易错点：collection 不是列表，**不能**用 `foreach`、`lindex`、`llength` 直接处理。要数元素个数用 `sizeof_collection`，要取属性用 `get_attribute`，要遍历用 `foreach_in_collection`。

**`get_attribute` 取几何属性**（来自 `Vpad.tcl`）：

- `[get_attribute [get_core_area] bbox]`（[Vpad.tcl:1](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Vpad.tcl#L1)）——`get_core_area` 产生区域 collection，`get_attribute` 取它的 `bbox` 属性，返回的是普通两层列表 `{{llx lly} {urx ury}}`，**到这里已经脱离 collection、变成普通列表**，所以后面能用 `lindex` 剥。

**`expr` 与 `format`**（来自 `Vpad.tcl`）：

- `[expr $die_llx + 20]`（[第 6 行](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Vpad.tcl#L6)）——任何坐标运算都得套 `expr`。
- `[format {%.1f %.1f} $i $die_lly]`（[第 7 行](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Vpad.tcl#L7)）——把算出的数字格式化成工具认的坐标串。

**普通列表操作**（来自 mapper）：

- `[llength $argv]`（[lef_layer_tf_number_mapper.tcl:11](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.tcl#L11)）——`argv` 是普通列表，用 `llength` 数长度。
- `[lindex $argv 0]`（[第 15 行](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LEF2FRAM/lef_layer_tf_number_mapper.tcl#L15)）——按下标取元素。

> 关键区分：**对象集合 → collection API；坐标/参数 → 列表 + `expr`**。判断该用哪套，看数据来源是 `get_*`/`all_*`（collection）还是 `gets`/`lindex`/`$argv`（列表）。

#### 4.4.4 代码实践

**实践目标**：用 collection API 写一段「统计并报告」的小脚本。

**操作步骤**：

1. 读 [createpathgroup.tcl:1-5](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/createpathgroup.tcl#L1-L5)，理解 collection 的产生与减法。
2. 写一段示例 Tcl（不写进仓库），统计输入端口总数与时钟数：

   ```tcl
   # 示例代码
   set all_in [all_inputs]
   set n_in   [sizeof_collection $all_in]
   set n_clk  [sizeof_collection [all_clocks]]
   puts "inputs=$n_in clocks=$n_clk"
   ```

**需要观察的现象**：`sizeof_collection` 返回的整数能否直接用 `puts` 打印、能否喂给 `expr` 做减法（如 `$n_in - $n_clk`）。

**预期结果**：`sizeof_collection` 返回的是普通整数字符串，可被 `puts`/`expr` 直接使用；而 collection 本身不能被 `expr` 当数字。**具体数值待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：下面两段哪个能跑？为什么？

```tcl
# A
foreach pin [all_inputs] { puts $pin }
# B
foreach_in_collection pin [all_inputs] { puts [get_attribute $pin full_name] }
```

**答案**：B 能跑。`all_inputs` 返回 collection，必须用 `foreach_in_collection` 遍历；A 用普通 `foreach` 处理 collection 会出错或拿到无法直接 `puts` 的对象句柄。此外 B 里取端口名要用 `get_attribute`，不能直接 `puts $pin`。

**练习 2**：为什么 `[expr $die_llx + 20]` 里的 `expr` 不能省？

**答案**：Tcl 是「万物皆字符串」，`$die_llx + 20` 在 Tcl 看来只是三个被空格分隔的「单词」，不会自动求值；`expr` 才是把它们当算术表达式计算的命令。省掉 `expr`，`for` 的条件段会拿到字面字符串，循环无法正常工作。

---

## 5. 综合实践

**任务**：写一个名为 `report_io_pad_count.tcl` 的示例脚本，把本讲的**几何坐标循环**与 **collection 统计**两套模式合起来用。

**要求**：

1. 用 `get_attribute [get_core_area] bbox` 取 die 四角坐标（仿 [Vpad.tcl:1-4](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Vpad.tcl#L1-L4)）。
2. 用一个 `for` 循环（步长自定）沿底边算出「若按此步长布点，需要多少个虚拟 pad」，用 `[expr ...]` 算个数，用 `puts` 打印。
3. 用 `sizeof_collection [all_inputs]` 与 `sizeof_collection [all_outputs]` 统计真实 I/O 端口数，一并打印，对比「虚拟 pad 数」与「真实端口数」的量级差。
4. （进阶）仿 [createpathgroup.tcl:1-5](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/createpathgroup.tcl#L1-L5)，在统计输入端口前先把时钟端口剔除，观察数字是否变小。

**参考骨架（示例代码，需自行在 ICC2/PT 环境内 `source` 验证）**：

```tcl
# 示例代码：统计底边虚拟 pad 数与真实 I/O 端口数
set llx [lindex [lindex [get_attribute [get_core_area] bbox] 0] 0]
set urx [lindex [lindex [get_attribute [get_core_area] bbox] 1] 0]
set step 80
set n_pad 0
for {set i [expr $llx + 20]} {$i < [expr $urx - 40]} {set i [expr $i + $step]} {
    incr n_pad
}
puts "bottom-edge virtual pads per net: $n_pad"

set in_list  [all_inputs]
set clk_port [get_port [all_clocks]]
set in_list  [remove_from_collection $in_list $clk_port]
puts "data inputs: [sizeof_collection $in_list]"
puts "outputs:     [sizeof_collection [all_outputs]]"
```

**验收**：能说清「为什么虚拟 pad 数量远大于真实电源端口数量」（答：电源网络要沿整条边均匀密集注入以控 IR drop，与数据 I/O 端口的数量没有直接关系）。**具体数值待本地验证**。

## 6. 本讲小结

- **几何坐标循环**（`Vpad.tcl`）：用 `get_attribute [get_core_area] bbox` 取四角，两个 `for` 循环沿 x、y 步进摆虚拟 pad，把「等间距布点」翻译成循环——`expr` 算坐标、`format` 拼坐标串。
- **路径分组**（`createpathgroup.tcl`）：用 `all_inputs/all_registers/all_outputs` 产生 collection、`remove_from_collection` 做减法剔除时钟、`group_path` 把路径切成 f2f/i2f/f2o/i2o 四桶，`-critical_range` 圈定关键路径簇。
- **库数据脚本化**（`lef_layer_tf_number_mapper.tcl`）：纯 Tcl 实现「读 `.tf` + LEF → 写 `.map`」，套路是 `argv` 解析 + `open/gets` 文件 I/O + `proc` 封装 + 标志位状态机 + 数组延迟提交；推导规则与 Perl 版（u3-l3）等价。
- **collection 与 expr**：对象集合必须用 `foreach_in_collection`/`sizeof_collection`/`get_attribute`/`remove_from_collection`；坐标与计数用 `[expr ...]` 和普通列表。判断依据是数据来源——`get_*`/`all_*` 产 collection，其余多为列表。
- **代码阅读素养**：本仓库脚本含若干冗余/异味（mapper 末尾 proc 重复定义、`if/else` 两分支同动作的 `incr`、`get_core_area` 与变量名 `die_*` 不完全对应），读模板时要带着批判眼光，**不把模板当圣经**。

## 7. 下一步学习建议

- 想看更复杂的「几何 + collection」综合脚本，可读 [u9-l2 Cadence SKILL 版图脚本入门](u9-l2-skill-layout-scripting.md)——`Logo.pl` 用 SKILL 的 `for` 循环按 BMP 像素画矩形，思路与本讲 `Vpad.tcl` 的坐标循环同源，但落在版图数据库上。
- 想深入 path group 在时序优化里的实际效果，回到 [u6-l1 PrimeTime STA 基本流程](u6-l1-primetime-sta-flow.md) 配合 `report_timing -group` 实践。
- 想把本讲的自动化能力串进完整流程，读 [u10-l2 全流程实战：RTL 到 GDSII](u10-l2-rtl-to-gdsii-capstone.md)，体会这些小脚本如何嵌入端到端流水线。
