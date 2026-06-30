# 多电压域与 UPF 电源意图

## 1. 本讲目标

学完本讲，读者应能：

- 理解 UPF（Unified Power Format，统一功率格式）是什么、为什么需要它，以及它如何描述"电源意图"。
- 读懂 `low_power.upf` 中 TOP / PD2 / PD1 三个电源域的供电结构与电压关系。
- 掌握 `create_power_domain`、`create_supply_net`/`create_supply_port`、`set_domain_supply_net` 等核心命令。
- 理解端口状态表（`add_port_state`）与层次电源域（`-scope`）。
- 理解电平转换器（level shifter）为什么是跨电压域信号传输的必需品。

## 2. 前置知识

本讲承接 u4-l1（ICC2 设计初始化与 MCMM）。在那里我们学过：设计初始化阶段要读入网表、库、TLU+ 寄生，并用 MCMM 描述"多角多模"的**时序意图**。本讲补上硬币的另一面——**电源意图**。两者都在 setup 阶段加载，互为补充。

先建立三个直觉。

**(1) 动态功耗与电压的平方成正比。** 一个 CMOS 电路的动态功耗近似为：

\[ P_{dynamic} = \alpha \cdot C \cdot V_{DD}^{\,2} \cdot f \]

其中 \(C\) 是负载电容，\(f\) 是翻转频率，\(\alpha\) 是活动因子。电压 \(V_{DD}\) 是平方项——把电压从 1.2V 降到 0.8V：

\[ \left(\frac{0.8}{1.2}\right)^{2} \approx 0.44 \]

即相同电路降压后动态功耗降到约 44%。这就是"多电压设计"（multi-voltage design）的动机：**让不追求极致性能的模块跑低电压，省下的功耗相当可观**。

**(2) 电源意图 vs. 时序意图。** RTL/网表只描述"逻辑和时序"，却不告诉工具"这个模块接多少伏电源、跨域信号要不要电平转换"。这些信息由一份独立的文本文件——**UPF（Unified Power Format，IEEE 1801 标准）**——以 Tcl 命令的形式描述。Synopsys / Mentor / Cadence 都支持，因此同一份 UPF 可在不同工具间复用。

**(3) 电源域（power domain）= 一组共用同一套电源的单元。** 把设计切成若干电源域，每个域有自己的电源/地网络；域之间电压不同时，靠电平转换器衔接信号。

> 本讲只读 `low_power.upf` 讲结构与语法，不要求在本地运行 EDA。若要真实加载 UPF，需 ICC2 或 Mentor Nitro 许可与配套工艺库，本仓库未提供运行环境。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `low_power.upf` | 本讲主角，68 行。定义一个 RISC 内核（RISC_CORE_1）的三个电源域（TOP / PD2 / PD1）及其供电网络、端口状态、电平转换器。 |
| `IC Compiler II/Scripts/03_PnR_setup.tcl` | 展示 ICC2 setup 阶段如何用 `load_upf`/`commit_upf` 把 UPF 读入设计（本仓库里被注释掉）。 |
| `mentor_scripts/0_import.tcl` | 展示 Mentor Nitro 在 import 阶段用 `source $MGC_UPF_File` 读 UPF。 |

## 4. 核心概念与源码讲解

### 4.1 电源域与供电网络

#### 4.1.1 概念说明

电源意图的"骨架"是三件事：

1. **电源域（power domain）**：把若干实例划成一个组，组内共用一套电源。
2. **供电网络（supply net）**：电源/地的"逻辑线"（如 VDD、VSS），只描述连接关系，不涉及具体金属走线。
3. **供电端口（supply port）**：供电网络与外界的接口（顶层电源焊盘，或层次模块的电源引脚）。

把它们绑起来的是 `set_domain_supply_net`——它声明"某域的主电源网是哪条、主地网是哪条"。注意 supply net 只描述"逻辑连接"，真正画成金属是后端 PnR 的事（参见 u4-l3 电源网络设计）。

#### 4.1.2 核心流程

```
create_power_domain  <域名> [-scope <层次实例>] [-elements {实例列表}]
create_supply_net    <网名> -domain <域名> [-reuse]
create_supply_port   <网名> -domain <域名> -direction in
connect_supply_net   <网名> -ports {端口列表}
set_domain_supply_net <域名> -primary_power_net <网> -primary_ground_net <网>
```

顺序大致是：建域 → 建网/建端口 → 连网到端口 → 指定每个域的主电源/主地。

#### 4.1.3 源码精读

TOP 是顶层 always-on 域，先建立三组供电网络与端口：

[low_power.upf:L3-L17](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/low_power.upf#L3-L17)

```tcl
create_power_domain TOP

create_supply_net  VDDH -domain TOP
create_supply_port VDDH -domain TOP -direction in

create_supply_net  VDDL -domain TOP
create_supply_port VDDL -domain TOP -direction in
connect_supply_net VDDL -ports {VDDL}
...
set_domain_supply_net TOP -primary_power_net VDDH -primary_ground_net VSS
```

要点：

- TOP 域的主电源是 **VDDH**、主地是 **VSS**。`-direction in` 表示这些端口是设计对外的输入电源。
- TOP 还建了一条 VDDL 网与端口，但它只是"借道"——VDDL 的真正消费者是 PD2 域。`connect_supply_net VDDL -ports {VDDL}` 把外部 0.8V 引到芯片内部，TOP 充当了"电源分发枢纽"。

PD2 域（低电压域）则用 `-reuse` 复用 TOP 已经建好的网：

[low_power.upf:L26-L30](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/low_power.upf#L26-L30)

```tcl
create_power_domain PD2 \
         -elements {I_DATA_PATH I_CONTROL I_STACK_TOP I_REG_FILE I_PRGRM_CNT_TOP I_INSTRN_LAT}
create_supply_net VDDL -domain PD2 -reuse
create_supply_net VSS  -domain PD2 -reuse
set_domain_supply_net PD2 -primary_power_net VDDL -primary_ground_net VSS
```

- PD2 的 `-elements` 列出 6 个实例（数据通路、控制、栈顶、寄存器堆、程序计数器、指令锁存）——RISC 内核的"主体"全部归到 PD2，跑 **VDDL = 0.8V**，正是降压省功耗的域。
- `-reuse` 关键字很关键：VDDL、VSS 这两个名字在 TOP 已经建过，PD2 用 `-reuse` 复用同一条网，而不是新建一条重名的网。复用意味着 PD2 的 VDDL 与 TOP 的 VDDL 在电气上是同一条网。

#### 4.1.4 代码实践

**实践目标**：亲手把三个域的供电关系对上号。

**操作步骤**：
1. 打开 `low_power.upf`，找到 L4、L26、L38 三处 `create_power_domain`。
2. 对每个域，找到紧随其后的 `set_domain_supply_net`（L17、L30、L50），抄下它的 `-primary_power_net` 与 `-primary_ground_net`。
3. 先自己填表，再对照第 5 节综合实践的拓扑图核对：

| 域 | 主电源网 | 主地网 |
|---|---|---|
| TOP | VDDH | VSS |
| PD2 | ? | ? |
| PD1 | ? | ? |

**需要观察的现象**：PD2 复用了 TOP 的网名（带 `-reuse`），而 PD1 用的却是带层次前缀的全新网名（`I_ALU/PD1_VDD`）——为什么不同？带着这个问题读 4.3。

**预期结果**：TOP=VDDH/VSS、PD2=VDDL/VSS、PD1=I_ALU/PD1_VDD / I_ALU/VSS。电压值在 4.2 给出。

> 说明：以上为源码阅读型实践，无需运行 EDA；如要在 ICC2 中验证可启用 `03_PnR_setup.tcl` L87-L88 的 `load_upf`/`commit_upf`，再用 `report_power_domains` 查看，结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 TOP 域里要建 VDDL 网，明明 TOP 自己用 VDDH？
**答**：VDDL 是为 PD2 域准备的电源。它从顶层焊盘进来后在 TOP 域"借道"，最终喂给 PD2。TOP 域作为 always-on 顶层域，承担了电源分发枢纽的角色。

**练习 2**：删掉 PD2 的 `create_supply_net VSS -domain PD2 -reuse` 会怎样？
**答**：PD2 不会有可用的地网，`set_domain_supply_net PD2 ... -primary_ground_net VSS` 将找不到属于 PD2 的 VSS 而报错。`-reuse` 是让 PD2 共享 TOP 已建的 VSS 网的关键。

### 4.2 端口状态表

#### 4.2.1 概念说明

声明完网和端口，工具还不知道每条供电线"到底是几伏"。`add_port_state` 就是给每个 supply port 登记它的电压状态。在本仓库这个简单例子里，每个端口只有一个固定状态；但在带电源门控（power gating）/ 状态保持（retention）的设计里，一个端口可以有多种状态（如 ON=1.2V / RET=0.8V / OFF=0V），再由 `add_power_state` 组合成系统级功耗模式。

端口状态表的两大用途：

1. 让工具知道相邻域的电压差，从而判断**哪里需要插电平转换器**。
2. 把电压信息与 Liberty `.db` 的 PVT 角对应，做**状态相关的时序/功耗分析**（参见 u3-l1 标准单元库基础）。

#### 4.2.2 核心流程

```
add_port_state <端口名> -state {<状态名> <电压值>}
```

#### 4.2.3 源码精读

[low_power.upf:L20-L22](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/low_power.upf#L20-L22)

```tcl
add_port_state VSS  -state {state1 0.000000}
add_port_state VDDL -state {state1 0.800000}
add_port_state VDDH -state {state1 1.200000}
```

三个顶层电源的电压一目了然：

- **VSS = 0V**（地）
- **VDDL = 0.8V**（低压，喂 PD2）
- **VDDH = 1.2V**（高压，喂 TOP 与 PD1）

对照 4.1 的供电网络，工具由此得知：PD2（0.8V）与 TOP/PD1（1.2V）之间存在 **0.4V 电压差**——这正是 4.4 要插电平转换器的根因。PD1 的电压则由 L55 单独声明（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：体会电压差如何决定电平转换器需求。

**操作步骤**：
1. 读 L20-L22 与 L54-L55，记下 VDDH/VDDL/VSS 与 PD1_VDD 电压。
2. 计算 PD2 边界两侧的电压差：\(|\,VDDH - VDDL\,|\)。
3. 预测：信号从 PD2（0.8V）进入 PD1（1.2V）时，电平是被"抬升"还是"降低"？

**需要观察的现象**：电压差为 0.4V，信号从低压域到高压域需要 up-shift，反向需要 down-shift。

**预期结果**：两种方向都需要，所以 4.4 的电平转换器规则会写 `-rule both`。待本地验证：在真实 ICC2 中 `report_level_shifters` 应在 PD2/PD1 边界报告插入点。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `state1` 这个名字在三个端口上重复出现而不冲突？
**答**：`add_port_state` 的状态名是**每个端口内部**的局部标签，不是全局名字。VSS 的 state1、VDDL 的 state1、VDDH 的 state1 各自独立，只是凑巧同名。

**练习 2**：如果要支持"休眠模式（PD2 断电到 0V）"，应改哪条命令？
**答**：给 VDDL 端口再加一个 OFF 状态，例如 `add_port_state VDDL -state {off 0.000000}`，再用 `add_power_state` 定义一个把 PD2 置为 off 的系统功耗模式。本仓库未实现，属进阶话题。

### 4.3 层次电源域（scope）

#### 4.3.1 概念说明

TOP 和 PD2 都是在设计**顶层**创建的域（没有 `-scope`），靠 `-elements` 拉拢一批顶层实例。但 PD1 不一样——它落在层次子模块 `I_ALU`（ALU 实例）内部，用 `-scope I_ALU` 声明：**PD1 是相对于 I_ALU 这个实例来定义的**。

引入层次电源域后，所有对 PD1 的引用都要带层次前缀，UPF 用 `/` 作分隔符：`I_ALU/PD1`（域）、`I_ALU/PD1_VDD`（网/端口）。这与 Verilog 层次名（如 `top/u1/signal`）一脉相承，参见 u2-l2 的层次化设计。

为什么要分层次？常见原因是：ALU 是别人提供的软 IP，它的电源端口名可能与顶层冲突，或我们想"原样保留"它的内部电源连接。`-scope` 让我们不必改写 IP，直接在它这一层贴一张"电源意图"标签。

> 注意：文件里的 `#Scope: ...`（L1、L35）只是**注释**标签，不是可执行命令。真正的层次作用域是由 L38 的 `-scope I_ALU` 参数指定的。

#### 4.3.2 核心流程

```
create_power_domain <域名> -scope <层次实例> -elements {<实例>}
# 之后引用该域一律写成 <层次实例>/<域名>
```

#### 4.3.3 源码精读

[low_power.upf:L35-L50](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/low_power.upf#L35-L50)

```tcl
#Scope: I_ALU
create_power_domain PD1 -scope I_ALU \
         -elements {I_ALU}

create_supply_port PD1_VDD -domain I_ALU/PD1 -direction in
create_supply_net  PD1_VDD -domain I_ALU/PD1
connect_supply_net I_ALU/PD1_VDD -ports {I_ALU/PD1_VDD}

set_domain_supply_net I_ALU/PD1 -primary_power_net I_ALU/PD1_VDD -primary_ground_net I_ALU/VSS
```

要点：

- `-domain I_ALU/PD1`——凡是引用 PD1，都要写成 `I_ALU/PD1`，这是层次域的标志，与 PD2（无前缀）形成鲜明对比。
- PD1 自己"新买"了一条电源线 `PD1_VDD`（名字与顶层 VDDH 不同），由 L55 `add_port_state I_ALU/PD1_VDD -state {state1 1.200000}` 标 1.2V。
- 但 PD1_VDD 电气上从哪来？看 L60：`connect_supply_net VDDH -ports {VDDH I_ALU/PD1_VDD}`——把顶层 VDDH 网连到了 PD1 的 PD1_VDD 端口。所以 **PD1 实际供的还是 1.2V**，PD1_VDD 只是它在 I_ALU 内部的"小名"。

这个设计示范了层次域的一个常见模式：**子域可以有自己命名的电源端口，再在顶层用 `connect_supply_net` 与全局电源并接到一起**。

#### 4.3.4 代码实践

**实践目标**：分清 `-scope` 层次域与顶层域的命名差异。

**操作步骤**：
1. 对比 L26（PD2）与 L38（PD1）两处 `create_power_domain`，找出 PD1 多了哪个参数。
2. 在文件里数一下带 `I_ALU/` 前缀的名字（域、网、端口）共有几处。
3. 找 L60 `connect_supply_net VDDH -ports {VDDH I_ALU/PD1_VDD}`，解释它把哪两个对象并接了。

**需要观察的现象**：所有 PD1 相关引用都带 `I_ALU/` 前缀；PD2 则没有任何层次前缀。

**预期结果**：PD1 用了 `-scope I_ALU`，因此后续一律 `I_ALU/PD1`；L60 把顶层 VDDH（1.2V）与 PD1 的 PD1_VDD 并接，使 PD1 实为 1.2V。

#### 4.3.5 小练习与答案

**练习 1**：L42 建了端口 `VSS -domain I_ALU/PD1`，它和顶层 TOP 域的 VSS 是同一条网吗？
**答**：不是。它是 I_ALU/PD1 这个层次域自己建的端口，是局部的。两者是否同电位，要看 L61 `connect_supply_net VSS -ports {I_ALU/VSS}` 这条连接——它把顶层 VSS 与 PD1 的 VSS 并接，才使它们同电位。

**练习 2**：如果把 L38 的 `-scope I_ALU` 去掉会怎样？
**答**：PD1 会被当成顶层域创建，但 L42 起所有 `I_ALU/PD1`、`I_ALU/VSS` 引用都将找不到对象而报错——层次名与域的定义层次必须一致。

### 4.4 电平转换器（level shifter）

#### 4.4.1 概念说明

当一条信号从低压域（PD2，0.8V）跨进高压域（PD1/TOP，1.2V），或反向时：

- 0.8V 驱动的信号送进 1.2V 器件，可能不足以把 PMOS 完全关断，导致直通电流甚至功能错误，需要 **up-level shifter** 把电平"抬"到 1.2V。
- 反向则需要 **down-level shifter**。

`set_level_shifter` 就是告诉工具："在这个域的边界，凡是跨电压的信号，请自动插入电平转换器。"它只声明**意图**，真正插哪些、插哪行由工具在综合/布局时落实。

#### 4.4.2 核心流程

```
set_level_shifter <规则名> -domain {<域>} \
    -applies_to input|output|both \
    -location automatic \
    -rule up|down|both
```

#### 4.4.3 源码精读

[low_power.upf:L64-L65](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/low_power.upf#L64-L65)

```tcl
set_level_shifter shift_up -domain {PD2}       -applies_to both -location automatic -rule both
set_level_shifter shift_up -domain {I_ALU/PD1} -applies_to both -location automatic -rule both
```

要点：

- 两条规则的**名字都叫 `shift_up`**，但 `-rule both` 才是真实语义——同时覆盖 up-shift 与 down-shift。规则名只是个标识符（方便后续 `remove_level_shifter` 引用），不代表"只升不降"，初学者不要被名字误导。
- `-applies_to both` 表示该域的**输入和输出**边界都声明；`-location automatic` 让工具自行决定把 shifter 摆在哪个域一侧。
- `-domain {PD2}` 与 `-domain {I_ALU/PD1}` 两条结合，覆盖了 PD2↔PD1、PD2↔TOP 这两条最主要的跨电压边界。
- 为什么没给 TOP 域单独写 level shifter？因为 TOP 与 PD1 同为 1.2V，二者之间无电压差；TOP 与 PD2 的边界已被 PD2 那条规则覆盖，不必重复。

#### 4.4.4 代码实践

**实践目标**：根据电压拓扑验证电平转换器规则是否完备。

**操作步骤**：
1. 列出所有相邻域对：TOP↔PD2、TOP↔PD1、PD2↔PD1。
2. 对每一对，用 4.2 的电压值判断是否需要 level shifter（电压不同则需要）。
3. 检查 L64-L65 的两条规则是否覆盖了所有需要的方向。

**需要观察的现象**：TOP(1.2V)↔PD1(1.2V) 不需要；TOP(1.2V)↔PD2(0.8V) 与 PD1(1.2V)↔PD2(0.8V) 都需要。

**预期结果**：两条规则（PD2、PD1 各一条，均 `applies_to both` + `rule both`）正好覆盖两条跨电压边界。待本地验证：ICC2 `commit_upf` 后 `report_level_shifters` 应列出 PD2 与 PD1 边界上的 shifter。

#### 4.4.5 小练习与答案

**练习 1**：规则名 `shift_up` 与 `-rule both` 是否矛盾？
**答**：不矛盾。规则名只是给这条规则起的标识，真实语义完全由 `-rule both` 决定，即升、降都做。

**练习 2**：为什么 PD1 与 TOP 都是 1.2V，却仍要给 PD1 单设一个电源域？
**答**：因为 PD1 落在 I_ALU 这个层次子模块里、有自己的电源端口与命名（PD1_VDD），且与 0.8V 的 PD2 紧邻、需要 level shifter。即便电压与 TOP 相同，从电源意图管理（层次、端口、shifter 边界）角度仍值得单列一域。

## 5. 综合实践

**任务**：把 `low_power.upf` 中 TOP / PD2 / PD1 三个电源域的供电关系画成一张框图，并标注电压。

**操作步骤**：

1. 重新通读 `low_power.upf` 全文（68 行），按 4.1–4.4 的方法把每个域的 primary power/ground 网填出。
2. 用 `connect_supply_net`（L11、L15、L44、L48、L60、L61）梳理网与端口的并接关系，特别留意 L60（VDDH↔PD1_VDD）与 L61（VSS↔I_ALU/VSS）。
3. 画出类似下面的拓扑（读者自行誊清为框图）：

```
            顶层焊盘
   VDDH(1.2V)    VDDL(0.8V)    VSS(0V)
       │             │            │
       ├─────────────┼────────────┤
       │             │            │
   ┌───┴────┐    ┌───┴────┐       │
   │  TOP   │    │  PD2   │◄──────┘   PD2 地 = VSS (经 -reuse)
   │ 1.2V   │    │ 0.8V   │
   │ always │    │ 主体6块 │
   │  -on   │    │        │
   └───┬────┘    └────┬───┘
       │       PD2 边界│ ──► level shifter (L64)
       │              │
       │         ┌────┴─────┐
       └────────►│   PD1    │   PD1_VDD 经 L60 接到 VDDH → 1.2V
        (L60)    │  I_ALU   │◄── 边界 ──► level shifter (L65)
                 │  1.2V    │
                 └──────────┘
```

4. 在图上标出每条网/端口的电压（来自 4.2 与 L55），并用箭头标出 PD2↔PD1、TOP↔PD2 两条 level-shifter 边界。

**需要观察的现象与预期结果**：

- TOP 与 PD1 都是 1.2V（同电位，PD1 经 L60 并接到 VDDH），PD2 是 0.8V。
- 三条供电线中：VDDH 同时供给 TOP 与 PD1；VDDL 单独供给 PD2；VSS 是公共地。
- 电平转换器出现在 PD2 边界（L64）与 PD1 边界（L65），覆盖所有 1.2V↔0.8V 跨越。

> 说明：本实践为源码阅读 + 手绘拓扑，无需运行 EDA。如要在 ICC2 中验证，可在 setup 阶段启用 `03_PnR_setup.tcl` L87-L88 的 `load_upf low_power.upf` + `commit_upf`，再 `report_power_domains`、`report_level_shifters` 比对——这些命令的具体输出待本地验证。

## 6. 本讲小结

- UPF（IEEE 1801）用 Tcl 命令描述"电源意图"，与 RTL/网表的"逻辑意图"、SDC/MCMM 的"时序意图"互补，三者一起在 setup 阶段喂给后端工具。
- `create_power_domain` + `create_supply_net`/`create_supply_port` + `set_domain_supply_net` 三件套搭建电源域与供电网络；`-reuse` 用于跨域共享同名网。
- `add_port_state` 给每个 supply port 登记电压（VDDH=1.2V、VDDL=0.8V、VSS=0V），是电平判断与状态相关分析的基础。
- `-scope` 定义层次电源域（如 PD1 落在 I_ALU 内），后续引用一律带 `I_ALU/` 前缀；`connect_supply_net` 把子域局部电源并接到全局电源（如 PD1_VDD→VDDH）。
- 多电压省功耗的代价是跨电压边界必须插 level shifter（`set_level_shifter`），本仓库在 PD2 与 PD1 边界各声明一条。
- 本仓库 UPF 为教学模板，域名、实例名、电压都来自一个假设的 RISC_CORE_1 设计；真实项目需按工艺 PDK 调整。

## 7. 下一步学习建议

- 顺着 ICC2 主流程回到 setup：复习 u4-l1，理解 UPF 与 MCMM 如何在设计初始化阶段一起加载（参考 `IC Compiler II/Scripts/03_PnR_setup.tcl` L87-L88 的 `load_upf`/`commit_upf` 注释行）。
- 想看 UPF 在别的工具里怎么读：阅读 `mentor_scripts/0_import.tcl` 的 `source $MGC_UPF_File` 段，对比 ICC2 与 Mentor Nitro 的接入方式。
- 进阶低功耗话题（本仓库未深入）：电源门控（isolation cell）、状态保持（retention register）、power state table（`add_power_state`）——可结合 Synopsys low-power / UPF 1801 官方手册继续学习。
- 衔接后续自动化讲义：U8 的 Tcl 自动化模式与本讲的 UPF 同属"用脚本描述设计意图"的范畴，可对照体会。
