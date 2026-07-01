# cdc_snitch：形式化跨域验证

## 1. 本讲目标

本讲讲解 Bedrock 自带的静态「时钟域跨越（CDC）分析」工具 `cdc_snitch`。读完本讲，你应当能够：

- 说清 `cdc_snitch` 为什么能不跑仿真、只看门级网表就判断 CDC 是否安全；
- 解释它把每一个寄存器分成的四类（OK1 / CDC / OKX / BAD）各自的判据；
- 读懂 `cdc_snitch_proc.ys` 这个 yosys 流程脚本把设计「压平」的每一步在干什么；
- 读懂 `cdc_snitch.py` 的核心数据结构与递归溯源算法；
- 理解 `magic_cdc` 属性如何标记「有意为之的跨域」，以及为何 `passthrough=1` 的 jit_rad 会被判为 BAD；
- 读懂 Make 里的 `%_yosys.json`、`%_cdc.txt` 两条模式规则如何把 CDC 检查接进 CI 回归。

本讲是 u4-l1（CDC 基础）的「验收层」：u4-l1 教你「应该怎么写」正确的跨域电路，本讲教你「怎么自动证明」自己（或别人）写的电路确实是那样。

## 2. 前置知识

- **时钟域跨越（CDC）**：当触发器 A 由时钟 `clk_a` 驱动、触发器 B 由另一个异步时钟 `clk_b` 驱动，而 B 的输入又依赖 A 的输出时，就发生了一次时钟域跨越。u4-l1 已讲过：单比特靠两级同步器，多位数据靠「源域稳定锁存 + 跨域 gate 资格认证」。本讲不重复原理，只关心「如何静态地把这种结构找出来」。
- **亚稳态与「赌寄存器不被改坏」**：跨域多位数据如果不做任何同步，直接用，表面往往「能用」，但会偶发数据撕裂。u4-l3 的 jit_rad 把这种坏做法称为「赌寄存器不被改坏」。`cdc_snitch` 的全部意义就是把这种「赌」变成「被机器抓住」。
- **yosys**：开源逻辑综合器。它能读 Verilog，把设计拆解成门级（与门、或门、触发器、存储器），并导出 JSON。本讲里 yosys 只做「拆解 + 导出」，不做真正下到某家 FPGA 的综合。
- **techmap 与 flatten**：yosys 的两个命令。`flatten` 把层次化实例拍平成一个大模块；`techmap` 把高抽象级的寄存器原语映射成名字里含 `DFF` 的门级单元——这一点很关键，因为 `cdc_snitch.py` 就是靠「单元类型名里有没有 `DFF` 字样」来认触发器的。
- **Make 的模式规则**：u2-l1 讲过 `%_tb`、`%_check` 等用 `%` 通配的模式规则。本讲里的 `%_yosys.json`、`%_cdc.txt` 是同一种机制，串起「yosys 导出 → python 分析 → 失败即退出码非零」的流水。
- **退出码与 CI**：Unix 程序返回 0 表示成功、非 0 表示失败；Make 规则里命令非 0 退出会让该 target 失败，CI 据此判红。`cdc_snitch.py` 设计成「只要有一个 BAD 就返回 1」，正是为了塞进 CI。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [build-tools/cdc_snitch.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/cdc_snitch.md) | 官方文档，讲四类拓扑、工具流、如何读输出，是本讲的权威来源。 |
| [build-tools/cdc_snitch_proc.ys](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/cdc_snitch_proc.ys) | yosys 流程脚本：读入 Verilog → 压平 → techmap 出 `DFF` 单元 → 导出 JSON 的中间步骤。 |
| [build-tools/cdc_snitch.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/cdc_snitch.py) | 分析器：吃 yosys 的 JSON，给每个寄存器分类，BAD 则返回非零退出码。 |
| [build-tools/top_rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk) | 定义 `%_yosys.json` 与 `%_cdc.txt` 两条模式规则，把上面三者接进任意子目录的 Make。 |

辅助理解（非本讲核心，但实践任务会用到）：

| 文件 | 角色 |
| --- | --- |
| [localbus/Makefile](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/Makefile) | 真实使用 `%_yosys.json`/`%_cdc.txt` 的子目录，把 `jit_rad_gateway_demo_cdc.txt` 放进 `all`。 |
| [localbus/jit_rad_gateway.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway.v) | `passthrough` 参数切换「正确 CDC（dpram 快照）」与「坏 CDC（直通）」，是最佳对照实验。 |
| [dsp/reg_tech_cdc.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/reg_tech_cdc.v) | 同步器原子，靠 `(* magic_cdc *)` 注解把自己登记为「有意跨域」。 |

---

## 4. 核心概念与源码讲解

### 4.1 设计动机：把「赌寄存器不被改坏」变成机器能抓的错

#### 4.1.1 概念说明

u4-l1 已经建立了这条直觉：跨域多位数据如果直接连，仿真常常「看起来对」，但物理上会偶发撕裂，而且这种 bug 极难复现、可能要上板跑几小时才出现一次。靠人眼 review 找这种隐患，在大设计里几乎不可能。

`cdc_snitch` 的思路是：**不跑仿真，改做静态分析**。逻辑是——

> 如果某个触发器的输入，经过组合逻辑后，源头分别落在不同的时钟域，那么这个触发器的输出就可能在某个组合输入跳变瞬间被采到「撕裂的」中间值。这种结构在 FPGA 上**注定**不安全（因为 LUT 在输入变化时会有毛刺），应当被禁止。

反过来，安全的结构只有两种：

1. 触发器的所有输入源头都和它自己在**同一个时钟域**（正常逻辑 / 状态机）；
2. 触发器的输入是**另一个域里单个触发器的直连**（即「同步器」的核心一跳），中间没有组合逻辑。

`cdc_snitch` 把全设计的每一个触发器都套进这两条判据，凡是两都不沾的，就是「BAD」。

#### 4.1.2 核心流程：四种拓扑

`cdc_snitch.md` 用三张图定义了四种拓扑，我们用文字复述。设被检查的触发器叫 `DFF_out`，它的时钟域是 `clk_out`，把它的数据输入（D / CE / CLR 每个引脚独立分析）往回溯源到「源头触发器集合」，每个源头各有一个时钟域。于是有一个「源头域的多重集合」。

| 类别 | 拓扑判据 | 含义 | 退出码影响 |
| --- | --- | --- | --- |
| **OK1** | 源头域集合里**全部**等于 `clk_out` | 普通同域逻辑，绝大多数计算/状态机 | 不影响 |
| **CDC** | 源头域集合里**只有一个域、且不等于** `clk_out`，且该 DFF 带 `magic_cdc` 属性 | 「有意为之」的单跳跨域（同步器），作者已声明知道 | 不影响 |
| **OKX** | 源头域集合里只有一个域、且不等于 `clk_out`，**但没带** `magic_cdc` 属性 | 结构上像同步器，但作者没声明，可能 OK 也可能漏标 | 默认不影响；`--strict` 下判失败 |
| **BAD** | 源头域集合里有**多个不同的域** | 组合逻辑里混进了不同域的信号，必坏 | **失败（退出码 1）** |

注意一条容易被忽略的细节：**是否标了 `magic_cdc` 并不能把 BAD 救成 CDC**。因为判 BAD 的条件是「多域信号汇进同一组合云」，这种结构标不标属性都不安全。`magic_cdc` 只在「单域、单跳」时才有意义，它的作用是把 OKX「升格」成 CDC，表示「这一跳是故意的」。

#### 4.1.3 源码精读：四分类的判据就在这几行

四分类的全部判据集中在 `check_bit()` 里的一段 if-elif：

[build-tools/cdc_snitch.py:51-68](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/cdc_snitch.py#L51-L68) —— 先把所有输入引脚往回溯源得到域列表 `dom_in`，然后：

- 第 51 行 `if all([d == clk for d in dom_in])`：全部同域 → **OK1**；
- 第 56 行 `elif len(dom_in) == 1`：只有一个异域源头；此时再看 `magic`（即该 DFF 是否带 `magic_cdc`），带则 **CDC**（第 57-60 行），不带则 **OKX**（第 61-64 行）；
- 第 65-67 行的 `else`：否则（多域）一律 **BAD**，「doesn't matter if they claim CDC or not」。

退出码逻辑在 `sift_file()` 末尾：

[build-tools/cdc_snitch.py:253-259](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/cdc_snitch.py#L253-L259) —— 第 255 行 `if bad_count != 0: rc = 1`，这就是「有 BAD 即失败」；第 257 行 `if strict and okx_count != 0: rc = 1` 是可选的严格模式，把「漏标的同步器」也算失败。

#### 4.1.4 代码实践：先读文档认四张图

1. **实践目标**：在动手跑工具前，先建立「四种拓扑」的视觉直觉。
2. **操作步骤**：打开 [build-tools/cdc_snitch.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/cdc_snitch.md)，依次看 `OK1`（第 12-19 行）、`OKX or CDC`（第 21-32 行）、`BAD`（第 34-44 行）三节，以及对应的 `cdc_OK1.svg`、`cdc_OKX.svg`、`cdc_BAD.svg` 三张图。
3. **需要观察的现象**：`cdc_BAD.svg` 里画了 4 个源头触发器，其中 3 个时钟标 `A`、1 个标 `B`，它们**共同**汇进一个「combinational logic」云，再驱动一个时钟为 `A` 的输出触发器——这就是「组合云里混了 A、B 两个域」。对比之下 `cdc_OK1.svg` 的所有源头都是同一个域、`cdc_OKX.svg` 是「一个异域触发器经直连（无组合云）驱动输出」。
4. **预期结果**：你能用自己的话讲清「BAD 与 OKX 的唯一区别在于组合云里有没有第二个域的信号混进来」。
5. 若无法渲染 SVG，直接看 `cdc_BAD.svg` 的文本（内含 `<text>...combinational...</text>` 与字母 A/B 的标注）也能理解，待本地用浏览器打开看图确认。

#### 4.1.5 小练习与答案

- **练习 1**：如果一个触发器的 D 输入只来自另一个域的一个触发器，但中间经过了一个与门，且与门的另一端是本域信号，它是哪一类？
  - **答案**：BAD。溯源后 `dom_in` 含两个域（异域那一个 + 本域那一个），落入 `else` 分支。这正是「同步器前面别加本域的组合」这条规则的机器化体现。
- **练习 2**：把一个 BAD 寄存器加上 `(* magic_cdc *)` 能让它通过检查吗？
  - **答案**：不能。`magic_cdc` 只在「单域单跳」时把 OKX 升格为 CDC；多域组合云无论标不标都判 BAD（见源码第 65-67 行注释）。

---

### 4.2 cdc_snitch_proc.ys：把设计压平成「寄存器 + 组合逻辑 + 存储器」

#### 4.2.1 概念说明

`cdc_snitch.py` 的判据全部建立在「触发器」「组合逻辑」「存储器」这三类门级单元上，而且它只认**一个**顶层模块（详见 4.3）。因此必须先用 yosys 把层次化、行为级的 Verilog 处理成一张「扁平的门级网表」，再用 `write_json` 导出。

这件事不是一句 `synth` 就够的——Bedrock 的设计里大量使用 `generate`、双端口存储器、`$meminit`（存储器初始化）等结构，必须按特定顺序处理，才能让 `cdc_snitch.py` 既能找到所有 `DFF`，又不被存储器内部「假性跨域」误报。`cdc_snitch_proc.ys` 就是这个「特定顺序」。

#### 4.2.2 核心流程

脚本按从上到下分四段：

```
# 第 1 段：定层级、删干扰
hierarchy -simcheck -auto-top      # 选出顶层，校验层次
select t:$meminit_v2 ; delete      # 删掉存储器初始化单元（不影响 CDC）

# 第 2 段：行为级 → RTL 级
proc                                # 把 always 块展成 MUX/触发器
opt -purge                          # 常量传播、死代码消除

# 第 3 段：保护存储器边界，再整体压平
select t:$memrd t:$memrd_v2 t:$memwr_v2
setattr -set keep_hierarchy 1       # 给存储器单元打 keep，防止被吸收
flatten -wb                          # 把整个设计拍成单一模块
opt -fast -purge
synth -run :coarse                  # 跑综合的「粗」阶段
techmap                             # 把高级寄存器原语映射成 *_DFF_* 单元

# 第 4 段：快速清理 + 终检
opt_expr / opt_merge / opt_reduce / opt_dff / opt_clean   # 等价于 opt -fast -purge，但更快
hierarchy -check ; stat ; check     # 结构合法性、统计、一致性检查
```

两条最关键的命令：

- **`flatten -wb`**（第 28 行）：`-wb` 表示「wire-buffered flatten」，把所有子模块实例合并进顶层。这是让最终 JSON 里只剩**一个**模块的前提——`cdc_snitch.py` 的 `sift_design()` 在模块数 ≠ 1 时会直接报错退出（见 4.3）。
- **`setattr -set keep_hierarchy 1`（针对存储器，第 24-26 行）+ techmap（第 31 行）**：双端口存储器如果被吸收、展开成普通触发器，就会因为「数据在写域、地址在读域」而**必然**被判 BAD（文档第 46-55 行专门讲了这点）。所以脚本刻意保留存储器边界，让 `cdc_snitch.py` 走「存储器专用分支」整段跳过其内部检查。

#### 4.2.3 源码精读

[build-tools/cdc_snitch_proc.ys:14-18](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/cdc_snitch_proc.ys#L14-L18) —— 定顶层、删 `$meminit_v2`。`-auto-top` 让 yosys 自动挑那个「没人实例化它」的模块当顶层。

[build-tools/cdc_snitch_proc.ys:24-29](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/cdc_snitch_proc.ys#L24-L29) —— 给三类存储器单元打 `keep_hierarchy`，紧接着 `flatten -wb`。注意顺序：**先打 keep 再 flatten**，否则存储器边界在压平时会被冲掉。

[build-tools/cdc_snitch_proc.ys:31-45](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/cdc_snitch_proc.ys#L31-L45) —— `techmap` 把寄存器变成名字含 `DFF` 的单元（`cdc_snitch.py` 靠 `"DFF" in type_name` 识别，见 4.3）；后面那一串 `opt_*` 是注释里说的「等价于 `opt -fast -purge` 但更快」的版本；最后 `hierarchy -check`/`check` 做合法性兜底。

脚本头部的注释也值得一看：它强调本脚本**只负责中间处理**，`read_verilog` 与 `write_json` 必须在脚本外面用 Make 完成（这正是 4.4 模式规则里 `-p "read_verilog ...; script ...; write_json ..."` 的来历）。

#### 4.2.4 代码实践：手动跑一遍 yosys 流程

1. **实践目标**：亲眼看到「层次化 Verilog → 单模块门级 JSON」的过程。
2. **操作步骤**：在 `localbus/` 下执行 `make jit_rad_gateway_demo_yosys.json`（依赖见 [localbus/Makefile:24](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/Makefile#L24)）。若想看每一步，可手动跑：
   ```
   yosys -p "read_verilog -DQF2 jit_rad_gateway_demo.v jit_rad_gateway.v \
     dpram.v flag_xdomain.v reg_tech_cdc.v jxj_gate.v shortfifo.v; \
     script ../build-tools/cdc_snitch_proc.ys; \
     write_json /tmp/demo.json"
   ```
3. **需要观察的现象**：`stat` 命令会打印 `Number of cells: ...`，里面有 `$_DFF_*`、`$memrd_v2`、`$memwr_v2` 等条目；最终只生成一个顶层模块。
4. **预期结果**：`/tmp/demo.json` 的 `modules` 字段下只有一个键（即扁平化后的顶层）。若看到多个模块，说明 `flatten` 没生效，`cdc_snitch.py` 后续会报 `too many modules`。
5. 缺 yosys 时无法运行，标记「待本地验证」。

#### 4.2.5 小练习与答案

- **练习 1**：为什么脚本要在 `flatten` 之前给存储器打 `keep_hierarchy`？
  - **答案**：双端口存储器天然「写在 A 域、读在 B 域」，若被压平/吸收成普通触发器会被误判 BAD；保留边界后，`cdc_snitch.py` 用存储器专用分支整体跳过其内部，只要求「同一字不被同时读写」（文档第 46-55 行）。
- **练习 2**：把 `techmap` 那行删掉会怎样？
  - **答案**：寄存器仍以高抽象原语（如 `$_DFFE_*` 之前的 `$dff`）存在，名字里未必含字符串 `DFF`；`find_dff()` 靠 `"DFF" in type_name` 认触发器，会漏掉它们，统计不全。

---

### 4.3 cdc_snitch.py：递归溯源与 magic_cdc 锚点

#### 4.3.1 概念说明

有了门级 JSON，剩下的事就是：对每个触发器，把它的输入往回溯源，直到遇到「源头」，收集源头的时钟域，再套用 4.1 的四分类。难点有两个：

1. **怎么知道一个网线（net）是哪个域的？** —— 往回找它的驱动者（driver）。如果驱动者是触发器，域就是该触发器的时钟；如果驱动者是组合逻辑单元，就继续往回找它的输入……直到遇到触发器、模块输入端口、或常量为止。这是一个**有向图的递归遍历**。
2. **怎么标记「有意跨域」？** —— yosys 会把 Verilog 里的 `(* magic_cdc *)` 属性透传到网表的 netname 上。脚本预先把这些属性收进 `magic_list`，判断时若触发器的输出位落在 `magic_list` 里，就认为它是「声明的同步器」。

文档里也老实承认 `magic_cdc` 是个**占位名**，他们还在找一个永久的属性名（可能对接 `ASYNC_REG`/`DONT_TOUCH` 这类工业标准，见 cdc_snitch.md 第 178-181 行）。

#### 4.3.2 核心流程

```
sift_design(json):                       # 入口
    要求 modules 恰好 1 个（否则报错退出）
    index_netnames(mod)                  # 1. 建 网线号→名字 映射；收 magic_cdc → magic_list
    index_drivers(mod)                   # 2. 建 网线号→驱动单元 映射；收 input/inout → module_input
    find_dff(mod)                        # 3. 遍历所有单元，对每个 DFF / memwr_v2 调用 check_bit

check_bit(dout, clk, magic, inputs):     # 单个触发器引脚的分类
    dom_in = []
    对 inputs 里每个网线 p：dom_in += list_domains(p)   # 递归溯源
    按 dom_in 与 clk 的关系分 OK1 / CDC / OKX / BAD    # 4.1 的判据
    写一行 "BAD 31049 ... clk lb_clk inputs ( 8 x lb_clk, 3 x dsp_clk ... )"
    若 BAD，再逐条写出每个源头 (tree ...)

list_domains(ix):                        # 递归核心
    if ix 是常量 (0/1/x/z): 返回 []
    if ix 已访问过:        返回 []     # 环路保护
    if ix 是模块输入:      返回 [ix]   # 每个输入端口自成「一个域」
    if ix 无驱动者:        记 driverless，返回 []
    driver = driver[ix]
    if driver 是 DFF:      返回 [它的时钟]      # 源头：触发器
    else (组合单元):        对它的每个输入引脚递归，合并结果
```

几个要点：

- **模块输入端口各自成一个域**（`list_domains` 第 154-157 行）。这呼应文档第 57-67 行的 I/O 规则：顶层输入默认「自成一体」，所以 Bedrock 的做法是给被测逻辑套一层 shell，用输入寄存器把每个 I/O 的域先定死（见 `jit_rad_gateway_demo.v` 里那一堆 `magic_cdc` 输入寄存器，4.5 会用到）。
- **常量不参与分类**（第 149-150 行），避免 `0`/`1` 把域列表污染。
- **环路保护**靠 `active_nets`（第 151-153 行），防止组合反馈导致死循环。
- **D / CE / CLR 分开检查**（`find_dff` 第 122-127 行）。复位引脚 R 的域一致性同样重要，但前提是 yosys 的综合结果与最终厂家综合一致（文档第 183-190 行的 Discussion 讨论了这条假设）。

#### 4.3.3 源码精读

**`magic_cdc` 的收集**——在 `index_netnames()` 里：

[build-tools/cdc_snitch.py:185-196](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/cdc_snitch.py#L185-L196) —— 第 194-196 行：若某个 netname 的 `attributes` 里含 `MAGIC_CDC`（常量值就是字符串 `"magic_cdc"`，定义在第 28 行），就把它的每一位塞进全局 `magic_list`。稍后 `find_dff()` 第 117 行用 `magic = dout in magic_list` 判断当前触发器是不是「声明的同步器」。

**递归溯源**——`list_domains()`：

[build-tools/cdc_snitch.py:148-178](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/cdc_snitch.py#L148-L178) —— 注意第 166-167 行：若驱动者是 `DFF`，直接返回 `[它的时钟]`（这是递归的「叶子」）；否则第 168-176 行对组合单元的所有输入引脚继续递归。第 154-157 行把模块输入端口当作「自成一个域」返回。

**触发器与存储器两个分支**——`find_dff()`：

[build-tools/cdc_snitch.py:101-144](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/cdc_snitch.py#L101-L144) —— 第 101 行 `if "DFF" in type_name` 认触发器；第 122-127 行对 `D`/`CE`/`CLR` 每个输入引脚分别调 `check_bit`（注意第 126 行 `use_magic = magic and conn == "D"`：`magic_cdc` 只对 D 引脚生效）。第 128 行 `elif "memwr_v2" in type_name` 是存储器写端口分支，把 `DATA + ADDR + EN` 合在一起当输入交给 `check_bit`——这是 4.2 里说的「存储器专用处理」。

**单模块约束**——`sift_design()`：

[build-tools/cdc_snitch.py:221-235](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/cdc_snitch.py#L221-L235) —— 第 228-230 行：模块数 ≠ 1 时打印 `too many modules; try yosys command "flatten -wb <top_module>"` 并返回 1。这就是 4.2 里 `flatten -wb` 必须先做掉的根本原因。

#### 4.3.4 代码实践：给 BAD 行「画像」

1. **实践目标**：学会读 `cdc_snitch.py` 的输出文件，定位一条 BAD 路径。
2. **操作步骤**：阅读 cdc_snitch.md 第 113-149 行的示例输出，然后在脑子里演练：拿到一行 `BAD 31049 dsp.reg_bank_2[0]:D clk lb_clk inputs ( 8 x lb_clk, 3 x dsp_clk, 1 x dsp.evr_rx_out_clk )`，再用 `grep` 在 `_cdc.txt` 里搜这行，看它下面跟的 `tree ...` 列表。
3. **需要观察的现象**：每条 `tree` 行给出一个源头的网线名和它所属时钟域，例如 `lb_addr_r[0]` 来自 `lb_clk`、`dsp.evr_live_pps_tick[0]` 来自 `dsp_clk`。
4. **预期结果**：你能指出「要让这条 BAD 消失，得把 `dsp_clk` 域的那几个信号先用 OK1 拓扑采进 `lb_clk` 域」，与文档第 151-157 行的修复建议一致。
5. 待本地验证：用真实 `jit_rad_gateway_demo_cdc.txt` 跑一次 `grep BAD`。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `module_input` 里每个输入端口「自成一家」？
  - **答案**：因为静态分析不知道外部信号来自哪个时钟；把它当作独立域是最保守的假设。这样如果某个输入未经输入寄存器就直接进组合云，就会触发 BAD，逼你在 shell 层先用寄存器把它的域定死（文档第 57-67 行的 I/O 规则）。
- **练习 2**：`use_magic = magic and conn == "D"`（第 126 行）这条限制意味着什么？
  - **答案**：`magic_cdc` 只对数据引脚 D 生效，不复位 / 时钟使能引脚无效。即「声明的同步器」只豁免数据跨域，CE/CLR 仍要满足域一致。

---

### 4.4 Make/CI 集成：%_yosys.json 与 %_cdc.txt 模式规则

#### 4.4.1 概念说明

让 `cdc_snitch` 真正「卡住坏提交」的关键，不是工具本身，而是把它接进 Make、再接进 CI。Bedrock 在 `top_rules.mk` 里写了两条**目录无关**的模式规则，任何子目录只要 `include` 了 `top_rules.mk`，并给出一个 `foo_shell.v`（或任意 `foo.v` 顶层），就能用 `make foo_cdc.txt` 跑 CDC 检查。

两条规则把 u2-l1 讲过的「按目标定制」和「依赖推导」思想又用了一遍：第一条规则用 yosys 把 Verilog 编译成 JSON（类似 `%_tb` 编译），第二条规则用 python 分析 JSON（类似 `%_check` 仿真自校验）。

#### 4.4.2 核心流程

```
foo_cdc.txt  ──依赖──>  cdc_snitch.py + foo_yosys.json
foo_yosys.json ──依赖──>  foo.v (+ 若干 .v) + cdc_snitch_proc.ys
```

执行 `make foo_cdc.txt` 时：

1. Make 发现缺 `foo_yosys.json`，先跑第一条规则：
   `yosys -p "read_verilog <各 .v>; script cdc_snitch_proc.ys; write_json foo_yosys.json"`；
2. 再跑第二条规则：
   `python3 cdc_snitch.py foo_yosys.json -o foo_cdc.txt`；
3. `cdc_snitch.py` 末行打印 `OK1: N  CDC: N  OKX: N  BAD: N`；若有 BAD，退出码 1，`make` 报错，CI 判红。

#### 4.4.3 源码精读

`top_rules.mk` 先在文件上半段定义了一组 yosys 相关变量（这些在 u2-l1 讲过的「公共配方变量」之列）：

[build-tools/top_rules.mk:25-31](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L25-L31) —— `YOSYS = yosys`、`YOSYS_QUIET = -q`、`YOSYS_JSON_OPTION = -DBUGGY_FORLOOP`（第 27 行，注释说明这是为绕开 `dpram.v` 里 for 循环让 yosys 太慢的问题，见第 28-29 行）、`YOSYS_READ_VERILOG = read_verilog`。

然后是两条模式规则本身：

[build-tools/top_rules.mk:159-165](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L159-L165) ——

- 第 159-162 行 `%_yosys.json: %.v $(BUILD_DIR)/cdc_snitch_proc.ys`：先用 `$(YOSYS_JSON_PRECHECK)`（即 `true`，第 30 行）做占位，再 `$(YOSYS) --version` 记录版本，最后一条 `-p "read_verilog ...; script ...; write_json $@"` 一气呵成，正是 `cdc_snitch_proc.ys` 头部注释要求的「外面负责 read/write」。
- 第 164-165 行 `%_cdc.txt: $(BUILD_DIR)/cdc_snitch.py %_yosys.json`：`$(PYTHON) $^ -o $@`，把依赖里的 `cdc_snitch.py` 和 `foo_yosys.json` 直接当参数传给 python3，输出重定向到 `foo_cdc.txt`。简洁到只有一行。

真实用例在 localbus：

[localbus/Makefile:11](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/Makefile#L11) —— `all:` 目标里直接列了 `jit_rad_gateway_demo_cdc.txt`，意味着 `make -C localbus`（甚至 `sh selftest.sh` 里对 localbus 的那次 `make`）会自动跑 CDC 检查。[localbus/Makefile:24-25](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/Makefile#L24-L25) 还用 `YOSYS_JSON_OPTION += -DQF2` 给 yosys 传宏定义（按目标定制变量，u2-l1 讲过同款机制），切到 QF2 配置。clean 也专门删这两个生成物（[localbus/Makefile:43](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/Makefile#L43)）。

#### 4.4.4 代码实践：跑一次真实的 CDC 检查

1. **实践目标**：在子目录里触发 `%_cdc.txt`，看末行的四类计数。
2. **操作步骤**：装好 yosys（≥0.23，jit_rad.md 第 99-102 行提醒：**必须 ≥0.38**，否则 `magic_cdc` 属性会被 `jxj_gate.v` 弄丢，输出误导），在仓库根执行：
   ```
   make -C localbus jit_rad_gateway_demo_cdc.txt
   ```
3. **需要观察的现象**：终端最后一行形如 `OK1: N  CDC: N  OKX: N  BAD: 0`；`jit_rad_gateway_demo_cdc.txt` 里 `grep BAD` 应为空（passthrough 默认 0，CDC 正确）。
4. **预期结果**：退出码 0，`make` 成功；CI 这一步绿。若 BAD 非 0，`make` 失败。
5. 缺 yosys 时无法运行，标记「待本地验证」；可退而求其次，直接 `python3 build-tools/cdc_snitch.py --help` 看它的 `-o` / `--strict` 参数（[cdc_snitch.py:267-273](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/cdc_snitch.py#L267-L273)）。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `%_cdc.txt` 规则把 `cdc_snitch.py` 写在依赖里、又用 `$^` 一并传给 python？
  - **答案**：`$^` 展开成全部依赖 = `cdc_snitch.py foo_yosys.json`，正好是 `python3 cdc_snitch.py foo_yosys.json -o foo_cdc.txt` 需要的两个位置参数（`argparse` 第 273 行的 `file`）。一行写完，无需手敲文件名。
- **练习 2**：把 `jit_rad_gateway_demo_cdc.txt` 放进 `all:` 有什么 CI 意义？
  - **答案**：`selftest.sh`（u1-l2）会逐目录 `make -C localbus`，于是 CDC 检查自动进入 GitLab CI 的 test 阶段；任何让 BAD 计数变非零的提交都会让 CI 变红，回归就建起来了。

---

## 5. 综合实践：用 passthrough 切换「正确 CDC」与「坏 CDC」，亲眼看 BAD

本综合实践把四块知识串起来：用 `jit_rad_gateway.v` 的 `passthrough` 参数做对照实验，验证 `cdc_snitch` 能把 u4-1/u4-3 讲过的「赌寄存器不被改坏」抓出来。

### 背景

[jit_rad_gateway.v:11-30](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway.v#L11-L30) 定义了一个 `passthrough` 参数（注意默认值是 `1`，即「坏」的那一支，第 13 行）。它的两个 generate 分支分别是两种做法：

- **直通分支（passthrough=1，BAD）**：[jit_rad_gateway.v:32-39](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway.v#L32-L39) —— `assign xfer_clk = lb_clk`、`assign lb_odata = xfer_odata`，把 app_clk 域的 `xfer_odata` 直接喂给 lb_clk 域读取，**中间没有任何同步**。这正是 u4-3 里说的「赌寄存器不被改坏」。
- **缓冲分支（passthrough=0，正确）**：[jit_rad_gateway.v:40-75](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway.v#L40-L75) —— 用 `flag_xdomain` 把 `lb_prefill` 跨到 app 域（第 47-49 行），app 域跑约 17 拍把 16 个字写进一块双端口 `dpram`（第 64-66 行：A 口 app_clk 写、B 口 lb_clk 读），随后 lb 域**同域**读回，CDC 问题消失。

注意：被测顶层是 `jit_rad_gateway_demo.v`，它在输入处用一堆 `(* magic_cdc = 1 *)` 寄存器（[jit_rad_gateway_demo.v:26](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L26)、[第 36 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L36)、[第 50-51 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L50-L51)）把外部输入先采进 lb_clk 域——这就是 4.3 说的「shell 定域」做法。

### 步骤

1. **先跑默认（passthrough=1，应失败）**：`make -C localbus jit_rad_gateway_demo_cdc.txt`。但注意 demo 里 `jit_rad_gateway` 实例化时传的是 `.passthrough(passthrough)`（[jit_rad_gateway_demo.v:73](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L73)），而 demo 顶层参数 `passthrough=0`（[第 4 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad_gateway_demo.v#L4)），所以**默认就是正确的那支**。先确认这一支 BAD=0。
2. **改成坏的那支**：按 [localbus/jit_rad.md:104-105](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/localbus/jit_rad.md#L104-L105) 的指示，把 `jit_rad_gateway.v` 第 13 行的 `parameter passthrough = 1` 与 demo 第 4 行的 `parameter passthrough=0` 配合，使最终综合走 `thru` 分支（最简单的做法：临时把 demo 顶层的 `passthrough` 改成 `1`，**仅用于本实验，验证后改回**）。
3. **重跑 CDC**：`make -C localbus clean && make -C localbus jit_rad_gateway_demo_cdc.txt`。

### 观察与预期

- 正确支：末行 `BAD: 0`，退出码 0；
- 坏支（jit_rad.md 原话「It will fail badly!」）：`BAD: N`（N 明显大于 0），退出码 1，`make` 报错；`grep BAD jit_rad_gateway_demo_cdc.txt` 能看到 `lb_odata` 相关的触发器，其 `inputs ( ... )` 里同时出现 `lb_clk` 与 `app_clk` 两个域——正是「组合云里混了两个域」。
- 对照 `cdc_BAD.svg`（多域汇进组合云）与 `cdc_OK1.svg`（全同域），你会看到工具画的图与你抓到的 BAD 行是同一回事。

> 注意：本实验需修改一处源码参数（仅 `passthrough` 默认值），属破坏性改动，**务必在验证后立即还原**，不要提交。

待本地验证：若 yosys 版本 < 0.38，按 jit_rad.md 第 99-102 行的提醒，`magic_cdc` 属性会因 `jxj_gate.v` 的属性设置被丢弃，导致「正确支也被误报」——这本身就是属性透传脆弱性的一个好例子。

---

## 6. 本讲小结

- `cdc_snitch` 是**静态** CDC 分析：不跑仿真，靠 yosys 把设计压成门级网表，再对每个触发器的输入溯源，按「源头域的多重集合」分类。
- 四分类：**OK1**（全同域，正常逻辑）、**CDC**（单域单跳 + 带 `magic_cdc`，声明的同步器）、**OKX**（单域单跳但未声明）、**BAD**（多域汇进同一组合云，必坏，失败退出）。
- `cdc_snitch_proc.ys` 的核心是 `flatten -wb`（压成单模块）+ 给存储器打 `keep_hierarchy`（防误判）+ `techmap`（造出名字含 `DFF` 的单元供 python 识别）。
- `cdc_snitch.py` 用 `index_netnames/index_drivers` 建两张映射，用 `list_domains` 递归溯源，`magic_cdc` 属性只对 D 引脚把 OKX 升格为 CDC，**救不了 BAD**。
- `top_rules.mk` 的 `%_yosys.json` / `%_cdc.txt` 两条模式规则把工具接进任意子目录；localbus 把 `_cdc.txt` 放进 `all`，于是 CDC 检查随 `selftest.sh` 进入 CI。
- `passthrough=1` 的 jit_rad 把 app 域数据直通给 lb 域，是「赌寄存器不被改坏」的活样本，会被 `cdc_snitch` 判 BAD 并让 `make` 失败——这正是该工具存在的意义。

## 7. 下一步学习建议

- **横向对比厂家工具**：阅读 cdc_snitch.md 第 192-198 行关于 Vivado `report_cdc` 的讨论，思考「开源 yosys 流 vs 厂家专用工具」各自的覆盖面与可移植性取舍。
- **回到源头修一个 BAD**：在 dsp 或 cmoc 子系统里找一个 `_cdc.txt` 目标跑出来，挑一条真实 BAD 行，按文档第 151-157 行的建议，用 u4-l1 的 `data_xdomain`/`flag_xdomain`/`reg_tech_cdc` 把它改成 OK1 或 CDC，再看 BAD 计数是否归零。
- **后续讲义**：u6-l2（rtsim）与 u6-l3（cmoc）会把本讲的 CDC 积木用到 RF 系统仿真与控制器里，你会看到 `cryomodule.v` 用 `data_xdomain` 把 localbus 跨到 `clk1x`——届时回头看本讲，便能理解那条 `data_xdomain` 之所以能通过 CDC 检查的底层原因。
- **深入属性语义**：若你对 `ASYNC_REG`、`DONT_TOUCH` 与 `magic_cdc` 的关系感兴趣，可追踪文档第 178-181 行留下的「永久属性名」开放问题，以及 yosys 不同版本对属性透传的差异（jit_rad.md 第 99-102 行的 0.37/0.38 分水岭）。
