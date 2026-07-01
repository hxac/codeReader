# 寄存器地址映射自动生成 newad.py

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `newad.py` 解决了什么问题：把 Verilog 端口「标记」成软件可读写寄存器，自动分配地址、自动生成总线解码器，让样板代码降到接近零。
- 看懂 Verilog 里两类 **magic 注释**：`external`（这是一个寄存器）和 `auto`（递归进入这个实例去收集寄存器），以及 `single-cycle`/`we-strobe`/`plus-we` 等属性修饰。
- 对照源码讲清 `newad.py` 的输入（顶层 Verilog + 目录列表）与三类产物（`_auto.vh` 解码器头、`addr_map_*.vh` 地址表、`regmap_*.json` 寄存器表）。
- 掌握 `newad.py` 的主要 CLI 参数（`-i/-o/-d/-a/-r/-w/-b/-l/-m`）以及 `newad_top_rules.mk` 如何用模式规则把它们接进 Make。
- 读懂一份真实的 JSON regmap（以 `projects/ctrace` 为例），并理解地址分配背后「按位宽对齐」的算法。

本讲是「核心方法学」的第三讲，承接 [u2-l2 片上 localbus 总线](u2-l2-localbus.md)：localbus 给出了「24 位地址 / 32 位数据 / 无握手」的读写字句，而 newad 回答的是「这些地址具体分给谁、谁来产生 `lb_write`/`lb_addr` 命中信号」。

## 2. 前置知识

- **寄存器映射（register map）**：把一片地址空间切成小块，每一块绑定一个有意义的名字（如 `phase_step`、`ampstep`），软件通过读写地址来控制硬件。地址表通常要同时给硬件（解码器）和软件（一张名字→地址的表）使用。
- **样板代码（boilerplate）**：每加一个寄存器，手写就要同时改「地址宏、命中判断、写使能、寄存器声明、JSON 表」五六处，极易漏改。newad 的全部价值就是消除这种重复劳动。
- **localbus 信号**（见 u2-l2）：`lb_clk`/`lb_addr`/`lb_data`/`lb_write`/`lb_rdata`。newad 生成的解码器就是挂在这样一组总线上的。
- **递归下降解析**：从顶层 Verilog 出发，遇到被标记的实例就「钻进去」读它的端口，再把端口一路向上传播到总线控制器——可以类比编译器遍历语法树。
- **regex（正则）与 yosys JSON**：newad 默认用正则「土法」解析 Verilog（`CommentParser`），也可选用 yosys 把 Verilog 转成 JSON 再解析（`Parser` + `read_attributes`）。两种后端共享同一套端口建模与地址分配逻辑。
- **Verilog 宏 `\`define`**：newad 的产物大量使用 Verilog 宏（如 `\`define AUTOMATIC_demo ...`），由顶层在编译时展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [build-tools/newad.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.md) | 官方说明文档：工作流、magic 注释语法、时钟域管理、命名规则。本讲的「语法权威」。 |
| [build-tools/newad.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.py) | 主程序：CLI 参数解析、调度两种解析后端、地址分配算法、写出三类产物。 |
| [build-tools/parser.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/parser.py) | `Port` 端口模型 + `make_decoder_inner` 解码器生成 + yosys 后端的层次遍历。是「端口如何变成 Verilog 解码逻辑」的核心。 |
| [build-tools/comment_parser.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/comment_parser.py) | 默认后端：用一组正则从 Verilog 文本里识别 `external`/`auto` magic 注释，继承自 `Parser`。 |
| [build-tools/read_attributes.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/read_attributes.py) | yosys 后端的辅助：把 yosys `write_json` 输出重整成 newad 能消化的 `external_nets`/`automatic_cells` 结构。 |
| [build-tools/newad_top_rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad_top_rules.mk) | Make 集成：把 `newad.py` 的三种调用包成 `%_auto.vh`/`addr_map_%.vh`/`regmap_%.json` 模式规则。 |
| [projects/ctrace/wctrace_top_regmap.json](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace_top_regmap.json) | 一份真实（已提交入库）的 JSON regmap，用来讲解字段含义与地址布局。 |

辅助阅读：真实 magic 注释样本在 [dsp/feedforward/ff_pulser.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/feedforward/ff_pulser.v)、[dsp/feedforward/ff_driver.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/feedforward/ff_driver.v)、[dsp/hosted/etrig_bridge.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/hosted/etrig_bridge.v)；真实 `// auto` 实例化样本在 [cmoc/cryomodule.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v)。

## 4. 核心概念与源码讲解

### 4.1 newad 的工作流与要解决的问题

#### 4.1.1 概念说明

在一个带 localbus 的 FPGA 设计里，「软件可设置的寄存器」要做四件事：占一个地址、产生写使能、保存数值、把名字报给软件。手写时这四件事散落在不同文件里，加一个寄存器要改五六处，是典型的样板代码泥潭。

`newad.py` 的思路是**单点标记、多点生成**：开发者只在 Verilog 端口旁边写一行 magic 注释，声明「这是一个 external 寄存器」，剩下全部交给 newad 自动推导（位宽、符号、地址、解码器、JSON 表）。文档开篇就点明了这一目标——「The amount of boilerplate code required is marginally zero」，见 [build-tools/newad.md:3-6](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.md#L3-L6)。

#### 4.1.2 核心流程

newad 的输入本质上是两样东西：**一个顶层 Verilog 文件**（`-i`）和**一组存放模块的目录**（`-d`）。它从顶层出发递归下降，期间做两件事：

1. **收集 external 端口**：遇到带 `external` 注释的 input/output 端口，就记成一个待分配地址的寄存器；位宽与符号直接从 Verilog 原生语法推断。
2. **沿 auto 实例下钻**：遇到带 `auto` 注释的模块实例化，就递归进入子模块继续收集，并把子模块里发现的寄存器端口一路向上传播，最终汇入顶层总线控制器。

文字流程图：

```
顶层 .v ──┬─发现 external 端口──> 记入端口表
          └─发现 // auto 实例 ──> 递归读 子模块.v ──> 其 external 端口
                                                        │
                  所有端口汇总到 gch / g_flat_addr_map ◄┘
                                        │
                  ┌─────────────────────┼─────────────────────┐
                  ▼                     ▼                     ▼
            _auto.vh (解码器)     addr_map_*.vh (地址表)   regmap_*.json
```

文档把这两步明确写成「两个主过程」，见 [build-tools/newad.md:53-92](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.md#L53-L92)。最经典的极简 external 标记是一个 12 位寄存器：

```verilog
        input [11:0] phase_step, // external
```

而 `auto` 实例化长这样（端口连线里塞了一个机器生成的宏 `\`AUTOMATIC_drive_couple`，由 newad 填充）：

```verilog
pair_couple drive_couple // auto
        (.clk(clk), .iq(iq),
        .drive(prompt_drive), .lo_phase(lo_phase_d),
        .pair(fwd_ref),
        `AUTOMATIC_drive_couple
);
```

#### 4.1.3 源码精读

文档对工作流的总述在 [build-tools/newad.md:53-70](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.md#L53-L70)（收集 external）和 [build-tools/newad.md:72-92](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.md#L72-L92)（沿 auto 下钻）。注意文档诚实地标注了当前实现的局限——它依赖 magic 注释、「有朝一日想改用 Verilog 属性 + 真正的 Verilog 解析器」，见 [build-tools/newad.md:8-10](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.md#L8-L10)。这正是后面「正则后端」与「yosys 后端」两条路并存的由来。

主程序里把这两步串起来的总调度是 `print_decode_header`：它先选后端解析顶层文件，再把结果写进一段以 `\`ifdef LB_DECODE_<mod>` 包裹的 Verilog 头，见 [build-tools/newad.py:234-277](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.py#L234-L277)。其中后端的选择只看一个布尔参数 `use_yosys`：

```python
if use_yosys:
    vfile_parser = Parser()
    vfile_parser.parse_vfile_yosys(...)
else:
    vfile_parser = CommentParser()
    vfile_parser.parse_vfile_comments(...)
```

#### 4.1.4 代码实践

**目标**：用眼睛走一遍真实设计里的 magic 注释，确认「单点标记」确实存在。

1. 打开 [dsp/feedforward/ff_pulser.v:19-26](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/feedforward/ff_pulser.v#L19-L26)，找到 `length`、`slew_lim`、`setp_x`、`setp_y` 四个端口，它们都带 `// external`。
2. 打开 [dsp/hosted/etrig_bridge.v:14-16](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/hosted/etrig_bridge.v#L14-L16)，看 `sel`/`period`/`delay` 三个连续的 external 寄存器。
3. 观察：开发者只写了 `// external` 这一行，没有任何手写的地址或解码逻辑——这就是 newad 要消除的样板代码。

**预期结果**：你能在一两个真实文件里数出至少 5 个 `// external` 标记，且它们旁边都没有配套的手写地址宏。

#### 4.1.5 小练习与答案

**练习 1**：文档说 newad 当前基于 magic 注释，未来想换成什么？为什么？
**答案**：换成「Verilog 属性（attributes）+ 真正的 Verilog 解析器」。因为正则匹配 magic 注释既脆弱又无法理解 Verilog 真实语法（见 4.2 的局限讨论）。

**练习 2**：newad 的两个必备输入是什么？
**答案**：顶层 Verilog 文件（`-i`）和模块搜索目录列表（`-d`）。

---

### 4.2 magic 注释语法与 Verilog 解析

#### 4.2.1 概念说明

magic 注释是一套「写给机器看的人话」——它本身是 Verilog 注释（编译器忽略），但 newad 的解析器会专门匹配它。语法分两类：

- **端口注释 `external`**：声明这个端口是个寄存器。可追加属性：`single-cycle`（写一拍即清零，适合 trigger/clear）、`we-strobe`（需要把写使能脉冲连同数据一起给用户，如 FIFO 推入）、`plus-we`（额外引出一根 `_we` 写使能线）、`strobe`（读选通）。
- **实例注释 `auto`**：声明这个实例要被递归解析。可带参数：`// auto clk1x`（覆盖该实例的默认时钟域）、`// auto(c_n,2) lb4[c_n]`（在 generate 循环里展开）。

#### 4.2.2 核心流程

默认后端 `CommentParser` 逐行扫描 Verilog，用四条正则识别四种结构（见源码精读）。识别到 `auto` 实例就递归；识别到 `external` 端口就构造一个 `Port` 对象塞进当前模块的端口表。两条关键细节：

- **输出端口尾缀 `_addr` 触发 RAM 模式**：若一个 output 端口名叫 `coeff_addr`，newad 会去掉 `_addr` 得到 `coeff`，把同名 input 寄存器升级成 DPRAM 接口（地址 + 数据），用来建模系数表/波形内存。
- **时钟域可在端口列表里「粘性」覆盖**：用 `// newad-force <域> domain` 注释切换其后所有端口的时钟域，直到下一次覆盖。

伪代码：

```
for 每一行 line:
    if 命中 INSTANTIATION_SITE 正则:        # // auto [clk] [(gvar,n)]
        递归解析子模块; 把子模块端口加前缀挂到本模块
    if 命中 PORT_WIDTH_MULTI 正则:           # input [h:l] name // external [attr]
        构造 Port(name, downto, sign, signal_type, clk_domain)
    if 命中 PORT_WIDTH_SINGLE 正则:          # input name // external [attr]   (1 位)
        构造 Port(name, (0,0), ...)
    if 命中 newad-force domain 正则:         # 切换 port_clock
        port_clock = 新域名
```

#### 4.2.3 源码精读

四条核心正则定义在 [build-tools/comment_parser.py:9-17](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/comment_parser.py#L9-L17)。注意 `external` 关键字后跟一个**可选**的属性组（`(single-cycle|strobe|we-strobe|plus-we)?`），这正是 4.2.1 里那些修饰词的落点：

```python
PORT_WIDTH_MULTI = r"^\s*,?(input|output)\s+(signed)?\s*\[(\d+):(\d+)\]\s*(\w+),?\s*"
PORT_WIDTH_MULTI += r"//\s*external\s*(single-cycle|strobe|we-strobe|plus-we)?"
PORT_WIDTH_SINGLE = r"^\s*,?(input|output)\s+(signed)?\s*(\w+),?\s*//\s*external\s*(single-cycle|strobe|we-strobe)?"
```

逐行扫描与递归的主体是 `parse_vfile_comments`，见 [build-tools/comment_parser.py:24-217](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/comment_parser.py#L24-L217)。其中 `newad-force` 域覆盖逻辑在 [build-tools/comment_parser.py:167-171](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/comment_parser.py#L167-L171)。

端口对象模型 `Port` 在 [build-tools/parser.py:10-48](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/parser.py#L10-L48)，它携带名字、位宽区间 `downto`、方向、符号、所属模块、`signal_type`、`clk_domain` 等字段——newad 对一个寄存器的全部认知都浓缩在这个对象里。

`_addr` 尾缀触发 RAM 模式的逻辑在 `consider_port`，见 [build-tools/parser.py:109-116](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/parser.py#L109-L116)。真实样本见 `ff_driver.v`：input `coeff` 是寄存器，output `coeff_addr` 是它的地址线，见 [dsp/feedforward/ff_driver.v:29-30](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/feedforward/ff_driver.v#L29-L30)。

文档对时钟域管理（含实例级 `// auto clk1x` 与端口级 `newad-force` 两种覆盖方式，以及用 `--` 前缀禁用匹配的小技巧）有完整示例，见 [build-tools/newad.md:126-183](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.md#L126-L183)。

> 正则后端的局限（诚实提示）：`PORT_WIDTH_MULTI` 等正则只能识别「单端口单行」的固定写法，无法理解 `input [15:0] a, b,` 这类多端口声明，也不真正做语法分析。yosys 后端用真解析器规避了这点，但需要安装 yosys。注释见 [build-tools/parser.py:353-356](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/parser.py#L353-L356)。

#### 4.2.4 代码实践

**目标**：用 `// auto clk1x` 体会「实例级时钟域覆盖」。

1. 打开 [cmoc/cryomodule.v:377](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v#L377)，看 `cav_mech ... // auto clk2x`，说明 `cav_mech` 内部的 external 寄存器会被分配到 `clk2x_clk` 域。
2. 对照 [build-tools/newad.md:134-141](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.md#L134-L141) 的 `prc_dsp ... // auto clk1x` 例子，确认机制一致。
3. 再看 [build-tools/newad.md:149-166](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.md#L149-L166)：在同一份端口列表里用 `newad-force` 反复切换 `lb`/`clk1x`/`clk2x` 域，注意 `trace_ack` 前面的 `--` 故意把 `external` 关键字挡掉，使其不被 newad 接管。

**需要观察的现象**：仅靠实例名后的一两个词（`clk1x`、`clk2x`），就能批量改写一大片寄存器的时钟域；而 `--` 前缀是「临时退出 newad」的逃生开关。

**预期结果**：你能向别人解释「为什么 `cic_period` 落在 `clk1x_clk` 域而 `buf_trig` 落在 `lb_clk` 域」。

#### 4.2.5 小练习与答案

**练习 1**：写一行 Verilog，声明一个「写一拍就清零」的 1 位触发寄存器 `start`。
**答案**：`input start, // external single-cycle`（1 位端口用 `PORT_WIDTH_SINGLE` 正则，可不写 `[0:0]`）。

**练习 2**：`// external address for coeff` 这条 output 注释会被 newad 怎么处理？
**答案**：`consider_port` 发现它是 output 且名字以 `_addr` 结尾，于是剥掉 `_addr` 得到 `coeff`，把同名 input 寄存器升级为 DPRAM 接口（生成地址线 + 数据线 + dpram 实例），用于系数表之类的小内存。

---

### 4.3 newad.py 主程序：CLI、地址分配与三类产物

#### 4.3.1 概念说明

`newad.py` 是整个工具的入口与调度中心。它做三件事：解析 CLI、调用后端解析 Verilog、按需写出**三类产物**。理解本节后你就明白 `-o/-a/-r` 三个开关各自对应什么文件。

| 开关 | 产物文件 | 内容 |
| --- | --- | --- |
| `-o` / `--output` | `<mod>_auto.vh` | 总线解码器：`\`AUTOMATIC_self`（自声明端口）+ `\`AUTOMATIC_decode`（每寄存器的写使能/寄存器/DPRAM 逻辑），并 `\`include "addr_map_<mod>.vh"` |
| `-a` / `--addr_map_header` | `addr_map_<mod>.vh` | 地址表：每个寄存器一条 `\`define ADDR_HIT_<name> (...)` 命中判断宏 |
| `-r` / `--regmap` | `regmap_<mod>.json` | 给软件用的名字→地址映射（JSON） |

#### 4.3.2 核心流程

地址分配的核心是一个「按位宽对齐」的贪心算法：每个寄存器根据它「占用几位地址」(\(2^{\text{bitwidth}}\)) 来决定对齐。设当前空闲基地址为 \(b\)，某寄存器占 \(k=2^n\) 个地址：

- 若 \(b\) 已经落在 \(k\) 的边界上（\((k-1)\,\&\,b == 0\)），直接用 \(b\)；
- 否则把 \(b\) 向上取整到下一个 \(k\) 的倍数：\(b' = \lceil (b+k)/k \rceil \cdot k\)。

数学上等价于：

\[
\text{next\_addr} = \left\lceil \frac{b+k}{2^n} \right\rceil \cdot 2^n = \left( \frac{b+k}{2^n} \,\text{向右移}\,n\,\text{位再移回} \right)
\]

源码里用位运算 `((base + k_aw) >> bitwidth) << bitwidth` 实现，见精读。占 \(n\) 位地址的寄存器（如一块 4096 深的内存，\(n=12\)）会被对齐到 4096 的倍数，因此会留下「空洞」——这正是 regmap 里地址不连续的原因。

主程序流程：

```
main():
  解析 argparse → 得 input_file/dir_list/lb_width/三个产物开关...
  modname = input_file 去路径去后缀
  print_decode_header(...)      # 永远执行：解析 + 写 _auto.vh（若 -o 给了路径）
  if -a: write_address_header(...)   # 写 addr_map_*.vh
  if -r: write_regmap_file(...)      # 写 regmap_*.json
```

#### 4.3.3 源码精读

CLI 参数定义在 [build-tools/newad.py:309-388](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.py#L309-L388)。几个关键参数：`-w/--lb_width`（默认 10）、`-b/--base_addr`（默认 0）、`-l/--low_res`（默认 False）、`-m/--gen_mirror`、`-y/--yosys`（切换后端）。

> 关于 `-w` 的历史怪癖：help 文本写的是「One less than the address width of the local bus」（比实际地址位宽少一），源码注释也反复出现「the historical, buggy definition of lb_width as one less than the actual address bus bit-width」，见 [build-tools/newad.py:373](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.py#L373) 与 [build-tools/newad.py:116-117](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.py#L116-L117)。计算 ADDR_HIT 模式时用 `2**(lb_width+1)-1` 当掩码来补偿。Makefile 里实际传的是 `-w $(LB_AW)`，由各工程给定。

主流程 `main` 在 [build-tools/newad.py:309-416](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.py#L309-L416)，三段调度清晰可见：先 `print_decode_header`，再按 `-a`/`-r` 分别调用两个 writer。退出码检查在 [build-tools/newad.py:419-425](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.py#L419-L425)：若有文件没找到，打印计数并 `exit(1)`。

地址分配算法本体是 `generate_addresses`，见 [build-tools/newad.py:71-181](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.py#L71-L181)。对齐逻辑的关键两行：

```python
register_array_size = 1 << gch[k][0]          # k_aw = 2^bitwidth
...
next_addr = ((base + k_aw) >> bitwidth) << bitwidth   # 向上对齐到 k_aw 的倍数
```

每写一个 ADDR_HIT 宏的格式串在 [build-tools/newad.py:148-167](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.py#L148-L167)，产物长这样（文档示例）：

```verilog
`define ADDR_HIT_..._(...) (lb4_addr[0][`LB_HI:11]==4096) // ... bitwidth: 11, base_addr: 8388608
```

见 [build-tools/newad.md:119-121](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.md#L119-L121)。三个 writer 分别是 `print_decode_header`（[newad.py:234-277](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.py#L234-L277)）、`write_address_header`（[newad.py:280-290](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.py#L280-L290)）、`write_regmap_file`（[newad.py:293-306](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.py#L293-L306)）。

#### 4.3.4 代码实践

**目标**：不跑综合，只跑 newad 主程序，亲眼看到三个产物。

1. 进入仓库根目录，先看帮助：

   ```bash
   python3 build-tools/newad.py -h
   ```

2. 选一个已有顶层（例如 `projects/ctrace/wctrace_top.v` 或 `dsp/feedforward/ff_driver.v`），同时生成地址表与 JSON（**待本地验证**：以下命令需在仓库根目录、且 `-d` 指向含子模块源码的目录）：

   ```bash
   python3 build-tools/newad.py \
     -i dsp/feedforward/ff_driver.v \
     -d dsp/feedforward,dsp \
     -a /tmp/addr_map_ff_driver.vh \
     -r /tmp/regmap_ff_driver.json -l
   ```

3. 打开 `/tmp/addr_map_ff_driver.vh`，找到 `\`define ADDR_HIT_...` 行，记下每个寄存器的 `base_addr`；再打开 `/tmp/regmap_ff_driver.json` 比对同名条目的 `base_addr` 是否一致。

**需要观察的现象**：`coeff` 与 `mem` 因带有 `_addr` 输出（RAM 模式）会占用多于一的地址，且其 `base_addr` 会按 \(2^n\) 对齐，可能与其他寄存器之间出现地址「空洞」。

**预期结果**：两个文件里的同名寄存器地址完全一致；占用多地址的寄存器 `addr_width > 0`，单寄存器 `addr_width == 0`。若 `python3` 缺少依赖或路径不对，会以 `files not found` 退出——此时检查 `-d` 目录列表。

#### 4.3.5 小练习与答案

**练习 1**：为什么一个 4096 深的内存寄存器会让 regmap 里出现地址空洞？
**答案**：它的 `bitwidth=12`，需占用 \(2^{12}=4096\) 个地址，且基地址要对齐到 4096 的倍数；当前地址若不在边界上会被向上取整，从而跳过若干未用地址，形成空洞。

**练习 2**：`-o`、`-a`、`-r` 分别产出什么？哪个是给软件用的？
**答案**：`-o` 产 `<mod>_auto.vh`（硬件解码器），`-a` 产 `addr_map_<mod>.vh`（硬件地址命中宏），`-r` 产 `regmap_<mod>.json`（给软件用的名字→地址表）。

---

### 4.4 解码器生成 parser.py：从一个 Port 到一段 Verilog

#### 4.4.1 概念说明

`newad.py` 负责「分地址」，`parser.py` 负责「把每个端口变成一段具体的 Verilog 解码逻辑」。同一个 `Port` 对象，按其 `signal_type` 不同，会展开成截然不同的硬件：普通寄存器、单脉冲寄存器、读选通、写使能选通、DPRAM 接口。这一节是理解「样板代码到底被替换成了什么」的关键。

#### 4.4.2 核心流程

`make_decoder_inner` 是分派中心，按 `signal_type` 走不同分支：

```
对每个端口 p（实例 inst、模块 mod）:
  sig_name = "<inst>_<port>"                     # 寄存器在顶层的唯一名字
  生成 we_<sig_name> = <clk>_write & (`ADDR_HIT_<sig_name>)   # 写使能
  if (mod, port) 在 use_ram 表里:                → DPRAM 模式
      生成地址线/数据线 wire + 一个 dpram 实例
  elif signal_type == "single-cycle":            → 单脉冲寄存器
      always @(posedge clk) sig <= we ? data : 0
  elif signal_type == "strobe":                  → 读选通
      wire sig = <clk>_read & (`ADDR_HIT_<sig>)
  elif signal_type == "we-strobe":               → 写使能直通
      wire sig = we_<sig>
  else (普通寄存器):                             → 保持型寄存器
      always @(posedge clk) if (we) sig <= data
```

每生成一段解码逻辑，同时往全局字典 `gch` 写一条元组，记录该寄存器的位宽、模块、符号、数据宽度、时钟前缀、描述——地址分配（4.3）就靠这个 `gch` 来决定占多少地址。

#### 4.4.3 源码精读

分派中心 `make_decoder_inner` 在 [build-tools/parser.py:117-265](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/parser.py#L117-L265)。普通保持型寄存器分支（最常见）会生成「写使能 + always 块」：

```python
reg_def = '%s always @(posedge %s_clk) if (we_%s) %s <= %s_data%s;\\\n'
self.decodes.append(decode_def + we_def + reg_def)
self.gch[sig_name] = (0, mod, sign, data_width, clk_prefix, cd_index_str, p.description)
```

注意 `gch` 元组第一位是 `0`——对单地址寄存器，`bitwidth=0`，对应 4.3 里「占 1 个地址」。`single-cycle` 分支的区别在于 else 分支清零而非保持，见 [build-tools/parser.py:194-212](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/parser.py#L194-L212)：

```python
reg_def = '... %s <= we_%s ? %s_data%s[%d:%d] : %d\'b0;\\\n'
```

DPRAM 分支会实例化一个 `dpram`，把写口接到 localbus、读口接到用户的 `coeff_addr`/`coeff` 线，见 [build-tools/parser.py:146-193](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/parser.py#L146-L193)。

`print_instance_ports` 负责把子模块端口「向上传播」：它一边写出 `\`define AUTOMATIC_<inst> .port(sig)...` 宏（供顶层展开实例时连线），一边把每个端口送进 `make_decoder` 生成解码逻辑，见 [build-tools/parser.py:279-324](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/parser.py#L279-L324)。

yosys 后端的层次遍历在 `parse_vfile_yosys`，见 [build-tools/parser.py:344-456](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/parser.py#L344-L456)；它读的不是文本而是 yosys 重整过的 `external_nets`/`automatic_cells`（见 [build-tools/read_attributes.py:4-31](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/read_attributes.py#L4-L31)），但端口建模与解码器生成与正则后端完全共用。

#### 4.4.4 代码实践

**目标**：阅读型实践——对照源码，预言一个 `single-cycle` 寄存器会展开成什么 Verilog。

1. 读 [build-tools/parser.py:194-212](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/parser.py#L194-L212) 的 `single-cycle` 分支。
2. 假设端口为 `input [7:0] brightness, // external single-cycle`，实例名 `demo`，时钟域 `lb`。
3. 写下你预期生成的两行 Verilog（`we_` 写使能 + `always` 块）。
4. 若本地装了 iverilog/python3，可按 4.3.4 的命令真的跑一遍 newad，再翻看 `<mod>_auto.vh` 里 `\`AUTOMATIC_decode` 宏的展开，核对你的预言。

**预期结果**：写使能为 `wire we_demo_brightness = lb_write&(\`ADDR_HIT_demo_brightness);`，寄存器为 `always @(posedge lb_clk) demo_brightness <= we_demo_brightness ? lb_data[7:0] : 8'b0;`（实际字段顺序与命名以本地产物为准）。

#### 4.4.5 小练习与答案

**练习 1**：`single-cycle` 寄存器与普通寄存器生成的 Verilog 有什么本质区别？
**答案**：普通寄存器 `if (we) sig <= data`（写入后保持）；`single-cycle` 是 `sig <= we ? data : 0`（只在写当拍为写入值，下一拍自动清零），适合 trigger/clear 这类「不需要保持状态」的脉冲。

**练习 2**：`gch[sig_name]` 元组的第一位为什么对单寄存器是 `0`？
**答案**：它表示该寄存器占用地址位数为 0，即 \(2^0=1\) 个地址；地址分配算法据此只给它分配 1 个地址。

---

### 4.5 JSON regmap 产物与下游消费

#### 4.5.1 概念说明

`-r` 产出的 `regmap_<mod>.json` 是 newad 唯一面向软件的产物。它的设计哲学在文档里说得很清楚：软件始终**按名字**访问寄存器，名字→地址的映射被压缩后存进 FPGA 内存，由应用软件加载——这样即使地址布局变了，软件也不用重写硬编码地址，见 [build-tools/newad.md:186-193](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.md#L186-L193)。

寄存器名的构造规则是「实例层次 + 端口名」，例如 `ssa_stim_ampstep` = 实例 `ssa_stim` + 端口 `ampstep`；若实例位于 generate 循环里，还会插入循环下标（如 `shell_1_dsp_fdbk_core_mp_proc_sel_thresh`），见 [build-tools/newad.md:194-210](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.md#L194-L210)。

#### 4.5.2 核心流程

JSON 由 `write_regmap_file` 写出：它跑一遍 `address_allocation`（不写文件、只填 `g_flat_addr_map`），再把字典 dump 成 JSON，见 [build-tools/newad.py:293-306](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.py#L293-L306)。每条记录由 `add_to_global_map` 构造，字段为：`base_addr`、`sign`、`access`、`addr_width`、`data_width`、`description`，见 [build-tools/newad.py:57-68](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.py#L57-L68)。其中 `access` 与 mirror（回读）机制挂钩：

\[
\text{access} = \begin{cases} \text{"rw"} & \text{addr\_width} \le 5 \text{（可被镜像回读）} \\ \text{"w"} & \text{否则（只写，太大无法镜像）} \end{cases}
\]

也就是说，只有「小」寄存器（数组规模 \(\le 32\)）在启用 `-m` 时才会被镜像、变为可读；大块内存默认只写。

下游消费的典型方式见 `projects/ctrace`：它的 Makefile 把一份已提交入库的 regmap JSON 当作「真相表」，用 `leep.build_rom` 把它编译成一块配置 ROM（`config_romx.v`），见 [projects/ctrace/Makefile:52-53](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/Makefile#L52-L53)。这是一个「JSON 进、硬件 ROM 出」的完整闭环。

#### 4.5.3 源码精读

`add_to_global_map` 的字段构造在 [build-tools/newad.py:57-68](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.py#L57-L68)。真实样本是 `projects/ctrace` 的 regmap，见 [projects/ctrace/wctrace_top_regmap.json:1-30](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace_top_regmap.json#L1-L30)。截取前两条：

```json
"ctrace_lb_dout": { "access": "r", "addr_width": 12, "base_addr": 0,      "data_width": 32 },
"ctrace_trigger":{ "access": "rw","addr_width": 0,  "base_addr": 4096,    "data_width": 1  }
```

读这两条就能验证 4.3 的地址算法：`ctrace_lb_dout` 是一块深 4096（\(2^{12}\)）的回读内存，`addr_width=12`，占满 `0..4095`；于是下一个寄存器 `ctrace_trigger` 的 `base_addr` 自然落到 `4096`。`ctrace_running`(4097)、`ctrace_pc_mon`(4098) 紧随其后，各占 1 个地址。

> 诚实提示：`add_to_global_map` 当前只产生 `"rw"`/`"w"` 两种 access；ctrace 这份 JSON 里出现的 `"r"`（只读监控寄存器，如 `ctrace_running`/`ctrace_pc_mon` 这类 output 端口）以及它不带 `description` 字段，说明该文件是**手工维护/早期产物**并已提交入库，而非由当前版本 newad 直接生成。它在此作为「regmap JSON 字段与地址布局」的真实样例，字段语义以 `add_to_global_map` 为准。

文档对命名规则与 generate 循环下标的讲解在 [build-tools/newad.md:186-210](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad.md#L186-L210)；Make 层把 newad 三类产物接成模式规则的文件是 [build-tools/newad_top_rules.mk:13-42](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad_top_rules.mk#L13-L42)。

#### 4.5.4 代码实践

**目标**：读懂 ctrace 的 regmap 地址布局，并验证你的理解与算法一致。

1. 打开 [projects/ctrace/wctrace_top_regmap.json](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace_top_regmap.json)，列出四个寄存器的 `base_addr` 与 `addr_width`。
2. 用 4.3 的对齐规则手算：一块 `addr_width=12` 的内存从 0 开始占 4096 个地址，下个寄存器应从几开始？
3. 打开 [projects/ctrace/Makefile:52-53](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/Makefile#L52-L53)，看这份 JSON 如何被 `leep.build_rom` 消费成 `config_romx.v`。
4.（可选）对照 [projects/ctrace/wctrace.v:12-22](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace.v#L12-L22) 的端口（`start`/`running`/`pc_mon`/`lb_out`），把它们与 JSON 里的 `ctrace_trigger`/`ctrace_running`/`ctrace_pc_mon`/`ctrace_lb_dout` 一一对应。

**预期结果**：手算的「下个寄存器 base_addr = 4096」与 JSON 中 `ctrace_trigger.base_addr` 完全吻合；`ctrace_lb_dout.data_width=32` 对应 wctrace 把任意位宽的 trace 数据统一读成 32 位的 `lb_out`。

#### 4.5.5 小练习与答案

**练习 1**：为什么 ctrace 把 `ctrace_lb_dout` 的 `addr_width` 设为 12？
**答案**：它是深度为 \(2^{12}=4096\) 的 trace 内存，需要 4096 个地址来逐字读出，所以占 12 位地址。

**练习 2**：软件为什么不直接硬编码地址，而要走 JSON regmap？
**答案**：因为地址布局会随设计变化；把「名字→地址」映射压缩存进 FPGA、由软件按名字访问，可以在地址重排时只更新映射，不动软件代码（见 newad.md 第 186-193 行）。

---

## 5. 综合实践

**任务**：自己写一个最小两文件设计，亲手走完 newad 的「标记 → 生成 → 解读」全流程。

**第 1 步：准备源码**（示例代码，非项目原有文件）。在工作目录建两个文件：

`led_demo.v`：

```verilog
// 示例代码：被 newad 解析的子模块
module led_demo (
    input clk,
    input [11:0] phase_step,  // external
    input [7:0]  brightness   // external single-cycle
);
endmodule
```

`top_led.v`：

```verilog
// 示例代码：顶层，用 // auto 让 newad 下钻
module top_led (input clk);
    led_demo demo // auto
        (.clk(clk),
        `AUTOMATIC_demo
    );
endmodule
```

**第 2 步：生成三类产物**（在仓库根目录执行，**待本地验证**：需 python3）：

```bash
mkdir -p /tmp/newad_lab && cp led_demo.v top_led.v /tmp/newad_lab
python3 build-tools/newad.py -i /tmp/newad_lab/top_led.v -d /tmp/newad_lab \
    -o /tmp/newad_lab/top_led_auto.vh \
    -a /tmp/newad_lab/addr_map_top_led.vh \
    -r /tmp/newad_lab/regmap_top_led.json -l
```

**第 3 步：解读产物**：

1. 打开 `regmap_top_led.json`，确认出现 `demo_phase_step` 与 `demo_brightness` 两条（名字 = 实例名 `demo` + 端口名，印证 4.5 的命名规则）。
2. 它们各占 1 个地址（`addr_width=0`），`base_addr` 分别为 0 和 1（先后顺序以本地输出为准）；`data_width` 分别为 12 和 8。
3. 打开 `addr_map_top_led.vh`，找到 `\`define ADDR_HIT_demo_phase_step (...)` 与 `\`define ADDR_HIT_demo_brightness (...)`。
4. 打开 `top_led_auto.vh`，在 `\`AUTOMATIC_decode` 宏里找到：`demo_phase_step` 是「保持型寄存器」（`if (we) ... <= data`），而 `demo_brightness` 是「单脉冲」（`<= we ? data : 0`），印证 4.4 的分派逻辑。

**第 4 步（进阶）**：把 `led_demo.v` 里 `brightness` 的注释从 `external single-cycle` 改成 `external`，重跑第 2 步，观察 `top_led_auto.vh` 里 `demo_brightness` 的 `always` 块从「写一拍清零」变成「保持型」，从而直观体会 magic 注释属性如何改变生成代码。

**预期结果**：你亲手验证了「一行 `// external` → 一个地址 + 一段解码器 + 一条 JSON 记录」的端到端闭环，这正是 newad 消除样板代码的全部价值。

## 6. 本讲小结

- `newad.py` 的核心价值是**单点标记、多点生成**：开发者在 Verilog 端口写一行 `// external`，工具自动推导地址、解码器、JSON 表，样板代码降到接近零。
- 两类 magic 注释：`external` 标记寄存器端口（可带 `single-cycle`/`we-strobe`/`plus-we`/`strobe` 属性），`auto` 标记需递归下钻的实例（可带时钟域覆盖与 generate 展开）。
- 主程序 `newad.py` 产出三类文件：`-o` 解码器 `_auto.vh`、`-a` 地址表 `addr_map_*.vh`、`-r` 软件用 `regmap_*.json`；地址分配用「按位宽 \(2^n\) 对齐」的贪心算法，大块内存会留下地址空洞。
- `parser.py` 的 `make_decoder_inner` 按 `signal_type` 把同一个 `Port` 展开成不同硬件（保持型寄存器 / 单脉冲 / 读选通 / 写使能 / DPRAM）；`_addr` 尾缀的 output 会把同名 input 升级为内存接口。
- 默认正则后端（`comment_parser.py`）轻量但脆弱，yosys 后端（`parser.py` + `read_attributes.py`）用真解析器规避局限，两者共享端口建模与地址逻辑。
- JSON regmap 让软件按名字访问寄存器；`projects/ctrace` 展示了「JSON → `leep.build_rom` → 配置 ROM」的真实下游消费闭环。

## 7. 下一步学习建议

- **顺着 localbus 往下走**：本讲生成的解码器都挂在 localbus 上，下一讲可进入 [u4-l2 存储网关与地址空间序列化](u4-l2-memory-gateway.md)，看 `mem_gateway.v` 如何在固定延迟下完成一次 localbus 读——它正是配合 newad 解码器工作的总线控制器。
- **看真实工程怎么把 newad 接进 Make**：阅读 [build-tools/newad_top_rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/newad_top_rules.mk) 与 [projects/ctrace/Makefile](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/Makefile)，理解 `%_auto.vh`/`addr_map_%.vh`/`regmap_%.json` 模式规则如何被工程引用。
- **深入一个重度使用 newad 的设计**：[cmoc/cryomodule.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cmoc/cryomodule.v) 与 [dsp/feedforward/](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/feedforward/) 里有大量 `external`/`auto`/`newad-force` 用法，是进阶阅读的最佳样本。
- **官方 HOWTO**：文档末尾给出了更详细的 wiki 链接 <https://gitlab.lbl.gov/hdl-libraries/bedrock/-/wikis/home/Newad-HOWTO>，可作为延伸阅读。
