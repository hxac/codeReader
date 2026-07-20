# GUI 参数与可选端口实现

## 1. 本讲目标

学完本讲后，你应当能够：

- 区分 fpga_base 在打包阶段定义的两类参数——**普通参数**（对应真实 VHDL generic）与**用户参数**（只存在于 GUI、不进入 HDL），并说出它们各自走哪条 TCL 回调。
- 读懂 `xgui/fpga_base_v1_4.tcl` 里的三类回调：`init_gui` 布局、`update_PARAM_VALUE`/`validate_PARAM_VALUE` 参数联动与校验、`update_MODELPARAM_VALUE` 把 GUI 值桥接到 VHDL generic。
- 追踪一个可选端口（如 `o_led`）从 `add_port_enablement_condition` 声明，落到 `component.xml` 的 `PORT_ENABLEMENT` 依赖，再到「端口不出现在 IP 符号上」的完整链路。
- 说清 `IMPL_LED=false` 时为什么 `o_led` 会被裁掉、而 HDL 里却没有对应的 `if generate` 守卫——也就是「打包级可选端口」与「RTL 级条件生成」的本质区别。

本讲承接 u4-l1（PsiIpPackage 打包 DSL），把镜头从「打包总流程」推进到「GUI 参数与端口使能」这一更细的命令族。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，Vivado IP 的参数有两个「房间」。** 当你在 Vivado 里打开一个 IP 的 GUI 并修改某个值时，这个值先住进 `PARAM_VALUE.<名字>` 这个房间——它是 GUI 层的参数值。而真正综合进硬件的 VHDL `generic`（或 Verilog parameter）住在另一个房间 `MODELPARAM_VALUE.<名字>`。两者并非自动同步，需要一段 TCL 把前者的值「搬运」到后者。这就是下面要讲的 `update_MODELPARAM_VALUE` 回调存在的根本原因。

**第二，不是所有 GUI 参数都对应一个 HDL generic。** 有些参数纯粹是给用户在界面上拨开关用的，它们的值不会被综合进电路，而是被别的参数或端口的「使能表达式」引用。这类参数叫**用户参数（user parameter）**。fpga_base 的 `IMPL_BLINK`/`IMPL_LED`/`IMPL_SWITCH` 就是典型——它们决定「某个端口要不要实现」，但它们自己并不出现在 VHDL entity 的 generic 列表里。

**第三，「可选端口」是打包层的概念，不是 RTL 层的。** 传统的条件端口做法是在 VHDL 里写 `if IMPL_LED generate ... end generate;`，靠一个 generic 在综合时裁剪逻辑。fpga_base 走的是另一条路：HDL 里**没有**这种 generate 守卫，端口是否暴露完全由 IP-XACT 元数据（`component.xml`）里的 `enablement` 表达式决定。这是 Xilinx Vivado IP 打包框架提供的一种「轻量级」可选端口机制，代价是端口的真正物理裁剪交给综合器去处理。

带着这三点直觉，我们去读源码。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `scripts/package.tcl` | 打包输入清单（手写） | GUI 参数定义命令族、可选端口使能声明 |
| `xgui/fpga_base_v1_4.tcl` | Vivado 自动生成的 GUI 回调脚本 | `init_gui`/`update_PARAM_VALUE`/`update_MODELPARAM_VALUE` 三类回调 |
| `gui/fpga_base_v1_0.gtcl` | 历史遗留的自动生成脚本 | 说明它是孤儿文件、不参与本 IP 的 GUI 逻辑 |
| `component.xml` | 打包产物（IP-XACT 清单） | 参数清单、端口使能依赖的最终落盘形态 |
| `hdl/fpga_base_v1_0.vhd` | 顶层 RTL | 证明 `IMPL_*` 不在 generic 列表、端口无 generate 守卫 |

一句话关系：`package.tcl`（输入）→ 经 PsiIpPackage 综合打包 → 产出 `component.xml`（含参数与端口使能）和 `xgui/*.tcl`（GUI 回调）。三者描述同一件事的不同侧面。

## 4. 核心概念与源码讲解

### 4.1 GUI 参数定义

#### 4.1.1 概念说明

Vivado IP 在 GUI 上展示的每一个可配置项，都对应打包阶段的一次「参数声明」。PsiIpPackage 框架把 Xilinx 原生繁琐的参数声明流程封装成两条简洁命令：

- `gui_create_parameter <名字> <显示标签>`：声明一个**普通参数**。它最终会对应一个 VHDL generic，因此既有 GUI 值（`PARAM_VALUE`）也有模型值（`MODELPARAM_VALUE`）。
- `gui_create_user_parameter <名字> <类型> <默认值> <显示标签>`：声明一个**用户参数**。它只在 GUI/参数空间里存在，**不**对应任何 HDL generic，常被用作「拨开关」去驱动其它参数或端口的使能表达式。

声明之后都要用 `gui_add_parameter` 把它真正加到当前 GUI 页面里。页面本身由 `gui_add_page <页名>` 创建。fpga_base 只有一个名为 `Configuration` 的页面，所有参数都挂在其下。

#### 4.1.2 核心流程

参数定义的调用顺序如下（伪代码）：

```
gui_add_page "Configuration"            # 建页
对每个参数:
    if 是普通参数:
        gui_create_parameter <名> <标签>
        (可选) gui_parameter_set_widget_checkbox   # 把布尔参数渲染成复选框
        gui_add_parameter
    else (用户参数):
        gui_create_user_parameter <名> <类型> <默认> <标签>
        gui_add_parameter
# 之后再声明端口使能条件（见 4.3）
```

需要特别留意：用户参数 `gui_create_user_parameter` 的第二个参数是**类型**（如 `boolean`），第三个是**默认值**（如 `true`），这与普通参数的「名字+标签」两参数形式不同。

#### 4.1.3 源码精读

先看 `scripts/package.tcl` 里的 GUI 参数区块。它先建页，再用普通参数命令声明所有「真实 generic」对应的参数：

[scripts/package.tcl:54-57](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L54-L57) —— 建立 `Configuration` 页，并声明 `C_VERSION` 普通参数（对应 VHDL generic `C_VERSION`）。

`C_USE_INFO_FROM_SCRIPT` 是个布尔型普通参数，额外用一行把它渲染成复选框，而不是默认的文本输入框：

[scripts/package.tcl:65-67](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L65-L67) —— 布尔参数 `C_USE_INFO_FROM_SCRIPT` 用 `gui_parameter_set_widget_checkbox` 指定为复选框控件，再 `gui_add_parameter`。注意它仍是**普通参数**（有对应 generic，见 u3-l3 讲的总闸）。

紧接着是三个用户参数——本讲的主角：

[scripts/package.tcl:75-82](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L75-L82) —— 用 `gui_create_user_parameter` 声明 `IMPL_BLINK`、`IMPL_SWITCH`、`IMPL_LED`，类型均为 `boolean`，默认均为 `true`。这三行是「端口可选」的全部源头。

最后用 `package_ip` 收尾，第二个参数 `false`（不开 Edit GUI）、第三个参数 `true`（跑综合），第四个是目标器件：

[scripts/package.tcl:95-97](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L95-L97) —— `package_ip $TargetDir false true xc7a200t`。综合标志为 `true`，意味着端口方向/位宽由综合反推后落盘进 `component.xml`（这点 u4-l1 已讲）。

#### 4.1.4 代码实践

**实践目标**：从源码层面确认「普通参数」与「用户参数」的差别不在 IP-XACT 的存储层，而在「是否对应 HDL generic」。

**操作步骤**：

1. 打开 `scripts/package.tcl`，数一下 `gui_create_parameter`（普通）与 `gui_create_user_parameter`（用户）各出现了几次。预期：普通 6 个（`C_VERSION`、`C_VERSION_MAJOR`、`C_VERSION_MINOR`、`C_USE_INFO_FROM_SCRIPT`、`C_FREQ_AXI_CLK_HZ`、`C_FREQ_BLINKING_LED_HZ`），用户 3 个（`IMPL_*`）。
2. 打开 `hdl/fpga_base_v1_0.vhd` 的 entity generic 区块：

[hdl/fpga_base_v1_0.vhd:28-40](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L28-L40) —— 顶层 entity 的 generic 列表。你会看到 7 个 generic：`C_VERSION`、`C_VERSION_MAJOR`、`C_VERSION_MINOR`、`C_FREQ_AXI_CLK_HZ`、`C_FREQ_BLINKING_LED_HZ`、`C_USE_INFO_FROM_SCRIPT`、`C_S00_AXI_ID_WIDTH`。

3. 对照两个清单：6 个普通参数里有 5 个直接出现在 generic 列表；第 6 个 `C_USE_INFO_FROM_SCRIPT` 也在；外加一个 `C_S00_AXI_ID_WIDTH`（它由 BD 回调动态设置，见 u4-l3，未在 `package.tcl` 显式 `gui_create_parameter`）。而 3 个 `IMPL_*` 用户参数**一个都不在** generic 列表里。

**需要观察的现象**：`IMPL_LED`/`IMPL_BLINK`/`IMPL_SWITCH` 在 HDL 里完全找不到。

**预期结果**：这从源头证明了用户参数不进入 HDL——它们是 GUI 层的开关，只能通过端口使能表达式间接影响硬件形态（见 4.3）。

> 说明：第 3 步的「找不到」是静态文本层面的事实，可立即验证；它不依赖任何工具运行。

#### 4.1.5 小练习与答案

**练习 1**：如果想在 GUI 上再加一个「是否实现项目字符串寄存器」的开关，应该用 `gui_create_parameter` 还是 `gui_create_user_parameter`？为什么？

**参考答案**：用 `gui_create_user_parameter`。因为这个开关的目的是「裁剪/暴露某段硬件」，其值本身不需要综合进电路（不对应 VHDL generic），只需被某个端口或参数的使能表达式引用。这正是用户参数的用途。

**练习 2**：`C_USE_INFO_FROM_SCRIPT` 是普通参数却用复选框显示，而 `IMPL_LED` 是用户参数也显示成开关。两者在「勾选/取消」时的效果有何本质不同？

**参考答案**：`C_USE_INFO_FROM_SCRIPT` 的勾选会改变一个真实 VHDL generic 的值，进而通过 RTL 里的 `when ... else`（见 u3-l3）改变版本号/日期的数据来源——是电路行为层面的切换。`IMPL_LED` 的勾选只改变 GUI 参数空间里的布尔值，被端口使能表达式 `$IMPL_LED` 读取，决定 `o_led` 端口是否暴露——是打包/接口层面的切换，HDL 内部逻辑不变。

---

### 4.2 xgui 回调脚本

#### 4.2.1 概念说明

当 PsiIpPackage 跑完 `package_ip`（且开了综合），Vivado 会为这个 IP 自动生成一份 GUI 回调脚本，放在 `xgui/<IP名>_v<版本>.tcl`。文件名里的版本号会随打包版本走——fpga_base 当前是 1.4，所以是 `xgui/fpga_base_v1_4.tcl`（u4-l1 已指出这个版本传播现象）。这份脚本是**自动生成**的产物，但被打包者检入仓库随 IP 一起分发，这样下游用户打开 IP 时无需重新生成即可获得正确的 GUI 行为。

脚本里定义了三类过程（proc），由 Vivado 在不同时机回调：

| 回调类型 | 触发时机 | 作用 |
| --- | --- | --- |
| `init_gui` | 打开 IP 配置窗口时 | 创建页面、把控件摆到页面上 |
| `update_PARAM_VALUE.<X>` | 参数 `X` 的任何依赖变化时 | 联动修改 `X` 的值（留空表示无联动） |
| `validate_PARAM_VALUE.<X>` | 用户改完 `X` 失焦时 | 校验输入是否合法，返回布尔 |
| `update_MODELPARAM_VALUE.<X>` | 需要把 GUI 值送给 HDL 时 | 把 `PARAM_VALUE.X` 复制到 `MODELPARAM_VALUE.X` |

最后一类是「两个房间」之间唯一的桥梁——只有真实 generic 才需要它。

#### 4.2.2 核心流程

```
用户打开 IP GUI
  -> init_gui: 建页面、add_param 摆控件
用户修改某参数 X
  -> validate_PARAM_VALUE.X: 合法?
  -> 若 X 影响其它参数: update_PARAM_VALUE.<依赖者>
用户确认/综合
  -> 对每个真实 generic G:
       update_MODELPARAM_VALUE.G:
           set_property value [get_property value $PARAM_VALUE.G] $MODELPARAM_VALUE.G
  -> 综合读 MODELPARAM_VALUE.G 当作 generic 初值
```

关键点：`update_MODELPARAM_VALUE` 只为「对应 HDL generic 的参数」生成。用户参数没有这一步——这是从源码判断「某参数是不是真实 generic」的最快方法。

#### 4.2.3 源码精读

先看 `init_gui`——它逐行把控件加到 `Configuration` 页面上，顺序与 `package.tcl` 的声明顺序一致：

[xgui/fpga_base_v1_4.tcl:2-17](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/xgui/fpga_base_v1_4.tcl#L2-L17) —— `init_gui` 先加 `Component_Name`（实例名），再建 `Configuration` 页，最后依次 `ipgui::add_param` 把 `C_VERSION`、`C_VERSION_MAJOR`、…、`IMPL_BLINK`、`IMPL_SWITCH`、`IMPL_LED` 全部挂到该页。注意 `IMPL_*` 三个用户参数在这里和普通参数**一视同仁**地被摆上页面——GUI 层并不区分两者。

接着是大量的 `update_PARAM_VALUE`/`validate_PARAM_VALUE` 空壳。fpga_base 的参数之间没有复杂联动，所以 update 体都是空的，validate 都直接 `return true`：

[xgui/fpga_base_v1_4.tcl:82-89](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/xgui/fpga_base_v1_4.tcl#L82-L89) —— 以 `IMPL_BLINK` 为例：`update_PARAM_VALUE.IMPL_BLINK` 体为空（无联动），`validate_PARAM_VALUE.IMPL_BLINK` 直接返回 `true`（不做范围校验）。其余参数同样如此。

真正有实质内容的是 `update_MODELPARAM_VALUE` 这一组。它的模板是「把 PARAM_VALUE 的 value 取出，写到 MODELPARAM_VALUE」：

[xgui/fpga_base_v1_4.tcl:110-143](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/xgui/fpga_base_v1_4.tcl#L110-L143) —— 这段为 `C_VERSION`、`C_VERSION_MAJOR`、`C_VERSION_MINOR`、`C_FREQ_AXI_CLK_HZ`、`C_FREQ_BLINKING_LED_HZ`、`C_USE_INFO_FROM_SCRIPT`、`C_S00_AXI_ID_WIDTH` 七个参数各定义了一个 `update_MODELPARAM_VALUE` 桥接 proc。每个 proc 的体都是同一句 `set_property value [get_property value ${PARAM_VALUE.X}] ${MODELPARAM_VALUE.X}`。

**关键观察**：这 7 个名字与 [hdl/fpga_base_v1_0.vhd:28-40](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L28-L40) 里的 7 个 generic **一一对应**，而 `IMPL_BLINK`/`IMPL_LED`/`IMPL_SWITCH` 在这里**完全没有** `update_MODELPARAM_VALUE`。这就是「用户参数不进入 HDL」在生成代码里的铁证。

#### 4.2.4 代码实践

**实践目标**：用 `update_MODELPARAM_VALUE` 的「有/无」作为判据，反查每个 GUI 参数是否对应真实 generic。

**操作步骤**：

1. 在 `xgui/fpga_base_v1_4.tcl` 里搜索 `proc update_MODELPARAM_VALUE`，列出它出现的全部参数名。
2. 在同一文件里搜索 `proc update_PARAM_VALUE`，列出全部参数名。
3. 做集合差：在 `update_PARAM_VALUE` 里出现、但在 `update_MODELPARAM_VALUE` 里**不**出现的参数，就是「只活在 GUI」的用户参数。

**需要观察的现象**：第 3 步的差集应当恰好是 `{IMPL_BLINK, IMPL_LED, IMPL_SWITCH}`。

**预期结果**：差集 = `IMPL_*` 三个，与 `package.tcl` 里用 `gui_create_user_parameter` 声明的那三个完全吻合。这印证了 4.1 的结论——可用一条规则记住：「**没有 `update_MODELPARAM_VALUE` 的参数，就是不进 HDL 的用户参数。**」

> 说明：本实践是纯文本检索，可直接用编辑器查找完成，无需运行 Vivado。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `IMPL_LED` 有 `validate_PARAM_VALUE.IMPL_LED` 却没有 `update_MODELPARAM_VALUE.IMPL_LED`？

**参考答案**：`validate` 是 GUI 层的输入校验（用户勾选/取消时检查合法性），对所有参数都会生成，与是否进 HDL 无关。`update_MODELPARAM_VALUE` 的职责是把 GUI 值搬运进 VHDL generic；`IMPL_LED` 没有对应 generic（不在 entity 列表），自然不需要、也不会生成这条桥接 proc。

**练习 2**：假设你想让 `C_FREQ_BLINKING_LED_HZ` 只能取 1~10 之间的整数，应该改哪个 proc？大概怎么改？

**参考答案**：改 `validate_PARAM_VALUE.C_FREQ_BLINKING_LED_HZ`。当前它直接 `return true`（[xgui/fpga_base_v1_4.tcl:32-35](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/xgui/fpga_base_v1_4.tcl#L32-L35)），可改为先 `set value [get_property value ${PARAM_VALUE.C_FREQ_BLINKING_LED_HZ}]`，再 `return [expr {$value >= 1 && $value <= 10}]`。（属于示例改动，需本地在 Vivado 里验证生效。）

---

### 4.3 端口使能条件

#### 4.3.1 概念说明

「端口使能条件」（port enablement condition）是 PsiIpPackage 暴露的一条命令：`add_port_enablement_condition <端口名> <表达式>`。它的语义是——「**当且仅当 `<表达式>` 为真，该端口才出现在 IP 的对外接口上**」。表达式里可以用 `$` 引用任意 GUI 参数（包括用户参数）。

这条声明经打包后，会落进 `component.xml` 里对应端口的 `<xilinx:enablement>` 节点，成为 IP-XACT 层面「可选端口」的正式描述。Vivado 在绘制 IP 符号、做 Block Design 连线时，会先求值这个依赖表达式：为假就把端口从符号上隐去；为真才显示并允许连线。

fpga_base 把三个物理端口分别绑定到三个用户参数：

| 端口 | 使能条件 | 默认 | 关掉后的影响 |
| --- | --- | --- | --- |
| `o_blink` | `$IMPL_BLINK` | true | 心跳灯端口消失 |
| `o_led` | `$IMPL_LED` | true | LED 输出端口消失 |
| `i_sw` | `$IMPL_SWITCH` | true | DIP 开关输入端口消失 |

#### 4.3.2 核心流程

以「用户把 `IMPL_LED` 取消勾选」为例，端口的完整生命周期：

```
打包阶段(package.tcl):
  add_port_enablement_condition "o_led" "$IMPL_LED"
    -> 落盘进 component.xml: <isEnabled dependency="$IMPL_LED">
       (此时 dependency 值尚未求值，只是声明)

用户使用阶段(打开 IP GUI):
  IMPL_LED 默认 true  -> o_led 端口可见
  用户取消勾选 IMPL_LED
    -> PARAM_VALUE.IMPL_LED = false
    -> Vivado 重新求值 PORT_ENABLEMENT.o_led 依赖
    -> o_led 从 IP 符号上消失, 不能连线
    -> 综合时该端口被当作「不实现」处理
```

注意最后一步：因为 `IMPL_LED` 不是 generic，HDL 里没有 `if generate` 守卫，所以 `o_led <= reg_wdata(24)(...)` 这条赋值在 RTL 文本里依然存在。端口「消失」是 IP-XACT 层的事，物理上的逻辑裁剪由综合器在消费方工程里完成（输出端口失去外部负载 → 驱动逻辑被优化掉）。这正是「打包级可选端口」与「RTL 级条件生成」的区别。

#### 4.3.3 源码精读

先看 `package.tcl` 里的三条声明，就在 GUI 参数区块之后：

[scripts/package.tcl:87-89](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L87-L89) —— 三条 `add_port_enablement_condition`：`o_blink` 依赖 `$IMPL_BLINK`、`o_led` 依赖 `$IMPL_LED`、`i_sw` 依赖 `$IMPL_SWITCH`。表达式里的 `\$` 是 TCL 转义，保证 `$IMPL_LED` 作为字符串原样传给打包框架，而不是在 `package.tcl` 执行时被当作 TCL 变量展开。

再看产物 `component.xml`。以 `o_led` 为例，它的端口定义和使能依赖长这样：

[component.xml:437-463](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L437-L463) —— `o_led` 端口声明：方向 `out`、位宽 `7 downto 0`、类型 `std_logic_vector`。其 `<spirit:vendorExtensions>` 里嵌着使能节点。

关键的使能节点本身：

[component.xml:456-462](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L456-L462) —— `<xilinx:isEnabled xilinx:resolve="dependent" xilinx:id="PORT_ENABLEMENT.o_led" xilinx:dependency="$IMPL_LED">true</xilinx:isEnabled>`。`resolve="dependent"` 表示这个使能值不是固定的，而是「依赖」`$IMPL_LED` 动态求值；`id="PORT_ENABLEMENT.o_led"` 是这个使能项的唯一标识。`i_sw`、`o_blink` 也有结构完全相同的节点（[component.xml:483-489](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L483-L489) 与 [component.xml:506-512](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L506-L512)）。

最后，回到 HDL，确认「端口逻辑确实没有 generate 守卫」：

[hdl/fpga_base_v1_0.vhd:289-292](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L289-L292) —— LED 寄存器回读与 `o_led <= reg_wdata(24)(7 downto 0)` 的连续赋值，**直接**出现在 architecture 主体里，外面没有任何 `if IMPL_LED generate`。这就是「打包级可选端口」的标志：HDL 永远实现完整逻辑，是否暴露端口交给 IP-XACT 元数据决定。

补充一个容易被忽视的文件——`gui/fpga_base_v1_0.gtcl`：

[gui/fpga_base_v1_0.gtcl:1-8](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/gui/fpga_base_v1_0.gtcl#L1-L8) —— 这个文件首行就写明「automatically written. Do not modify.」，内容是几个 `gen_HDLPARAMETER_*`/`gen_USERPARAMETER_*` proc，引用的全是 `RST_POLARITY`、`CLOCK_TYPE`、`PULSE_PERIOD` 等**与 fpga_base 毫无关系**的参数。它来自最初的开源发布提交（`git log` 仅一条 `Open source release` 记录），是一个残留的模板/孤儿文件，并不参与本 IP 的 GUI 逻辑——本 IP 真正生效的 GUI 脚本是 `xgui/fpga_base_v1_4.tcl`。读源码时若在这里找不到熟悉的参数，不必困惑，跳过即可。

#### 4.3.4 代码实践

**实践目标**：完整追踪 `IMPL_LED=false` 时 `o_led` 端口被裁掉的链路，并亲手在源码里走一遍「声明 → 元数据 → HDL」三段。

**操作步骤**：

1. **声明段**：在 `scripts/package.tcl` 找到第 88 行 `add_port_enablement_condition "o_led" "\$IMPL_LED"`，记住它把 `o_led` 的命运绑定到用户参数 `$IMPL_LED`。
2. **参数段**：确认 `$IMPL_LED` 是用户参数——回到第 81 行 `gui_create_user_parameter "IMPL_LED" boolean true "Implement LED outputs"`，默认 `true`。再确认它无对应 generic（hdl 第 28-40 行无 `IMPL_LED`）、无 `update_MODELPARAM_VALUE`（xgui 第 110-143 行无 `IMPL_LED`）。
3. **元数据段**：在 `component.xml` 第 459 行找到 `<xilinx:isEnabled ... xilinx:dependency="$IMPL_LED">`，这就是 Vivado 求值的依据。
4. **HDL 段**：在 `hdl/fpga_base_v1_0.vhd` 第 46 行看到 `o_led` 端口声明、第 292 行看到 `o_led <= reg_wdata(24)(7 downto 0)`，确认无 generate 守卫。
5. **心智模拟**：假设用户在 GUI 取消勾选 `IMPL_LED` → `PARAM_VALUE.IMPL_LED=false` → Vivado 求值 `$IMPL_LED` 为假 → `PORT_ENABLEMENT.o_led` 关闭 → `o_led` 从 IP 符号消失 → 消费方工程综合时，`o_led` 这根输出无外部负载，其驱动（第 292 行赋值）被裁剪。

**需要观察的现象**：第 1-4 步是静态文本事实，可直接在编辑器里逐条核对；第 5 步是运行期行为。

**预期结果**：前三段（声明/参数/元数据）可在源码中 100% 确认。第 5 步「输出端口负载消失后驱动被综合器裁剪」是 Xilinx 综合的标准行为，但**本仓库未提供可一键运行的 Vivado 工程脚本**来直接演示该裁剪效果，因此第 5 步的现象标注为「待本地验证」——建议在装有 Vivado 的机器上实例化本 IP，取消勾选 `IMPL_LED`，综合后用 `report_utilization` 或查看网表确认 `o_led` 相关逻辑是否被移除。

**关于「用户参数 vs 普通参数」的总结（本实践的核心结论）**：

| 维度 | 普通参数（如 `C_FREQ_AXI_CLK_HZ`） | 用户参数（如 `IMPL_LED`） |
| --- | --- | --- |
| 声明命令 | `gui_create_parameter` | `gui_create_user_parameter` |
| 对应 VHDL generic | 是 | 否 |
| 有 `update_MODELPARAM_VALUE` | 是 | 否 |
| 进入综合后的电路 | 是（影响 RTL 行为） | 否（只影响 GUI/端口使能） |
| 典型用途 | 配置电路参数（时钟频率、版本号） | 当开关，驱动端口/参数使能表达式 |

#### 4.3.5 小练习与答案

**练习 1**：如果某天开发者把 `add_port_enablement_condition "o_led" "\$IMPL_LED"` 从 `package.tcl` 里删掉、但保留 `gui_create_user_parameter "IMPL_LED" ...`，重新打包后 `o_led` 端口的行为会变成什么？

**参考答案**：`o_led` 会变成一个**永远存在**的普通端口——因为没有任何使能条件绑定它，Vivado 默认暴露所有在 HDL entity 里声明的端口。`IMPL_LED` 仍会出现在 GUI 上，但勾选与否不再影响任何端口，沦为一个无作用的开关。

**练习 2**：为什么端口使能表达式里写的是 `$IMPL_LED`（一个用户参数），而不是某个真实 generic？如果改成一个真实 generic（比如 `$C_FREQ_AXI_CLK_HZ`）做使能条件，技术上可行吗？

**参考答案**：用用户参数是因为「端口是否实现」本就不该影响电路功能——它是一个纯粹的接口裁剪决策，与 HDL 行为无关，所以用不进 HDL 的用户参数最干净。技术上完全可以把使能条件绑定到一个真实 generic（Xilinx 框架允许），但那样会把「接口裁剪」和「电路配置」耦合在一起，使同一个参数同时承担两种职责，容易引起混乱。fpga_base 的做法把它们解耦：generic 管电路，用户参数管接口。

**练习 3**：`o_led` 被裁掉后，寄存器映射里偏移 0x60（寄存器下标 24）的 LED 寄存器是否还存在？为什么？

**参考答案**：寄存器本身仍然存在。端口使能只裁剪「物理端口 `o_led`」及其外部连线，而 [hdl/fpga_base_v1_0.vhd:291](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L291) 的 `reg_rdata(24)(7 downto 0) <= reg_wdata(24)(7 downto 0)` 这条回读赋值并不依赖 `o_led` 端口。也就是说，软件仍可通过 AXI 读写寄存器 24，只是写进去的值不再驱动物理 LED 引脚（输出端口已消失）。这进一步说明端口使能是「对外的物理接口裁剪」，而非「内部寄存器空间裁剪」。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「读源码 + 改设计」的小任务：

**任务**：假设要给 fpga_base 增加第四个可选功能——「是否实现项目字符串寄存器组」（即偏移 0x40 起的 `C_VERSION_MAJOR` 字符串寄存器）。请仅基于本讲学到的机制，在源码层面（不实际运行 Vivado）规划出需要改动的位置。

**要求产出**：

1. **新增用户参数**：在 `scripts/package.tcl` 第 82 行后，仿照 `IMPL_LED` 加一行 `gui_create_user_parameter "IMPL_STRING" boolean true "Implement project/facility string registers"`，并 `gui_add_parameter`。
2. **决定是否加端口使能**：思考字符串寄存器**不是物理端口**而是 AXI 寄存器空间里的一段——因此 `add_port_enablement_condition` 在这里**不适用**（没有端口可绑）。说明这一判断的依据（提示：回顾练习 3，端口使能只裁物理端口，不裁寄存器空间）。
3. **结论**：写出你的判断——仅靠本讲的「打包级端口使能」机制，能否实现「关掉字符串寄存器组」？如果不能，应该改用哪一种机制（提示：RTL 级条件生成，即在 HDL 里用 `if <某 generic> generate` 守卫那段寄存器赋值，并让该 generic 由一个普通参数驱动）？

**预期结果（自检）**：

- 步骤 1 的命令格式应与 [scripts/package.tcl:75-82](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L75-L82) 完全一致。
- 步骤 2 应得出「`add_port_enablement_condition` 不适用，因为没有物理端口」。
- 步骤 3 应得出「打包级端口使能做不到裁寄存器空间；要裁寄存器组必须走 RTL 级条件生成——加一个普通参数（如 `C_IMPL_STRING`）作为 generic，在 [hdl/fpga_base_v1_0.vhd:280-286](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L280-L286) 的两个 generate 循环外面再套一层 `if C_IMPL_STRING generate`」。

这个任务把「用户参数 vs 普通参数」「端口使能 vs RTL 条件生成」两个核心区别逼到台面上：**裁物理端口用打包级用户参数，裁内部逻辑用 RTL 级 generic。**

## 6. 本讲小结

- fpga_base 在 `package.tcl` 里用 `gui_create_parameter` 声明 6 个普通参数（对应真实 generic）、用 `gui_create_user_parameter` 声明 3 个布尔用户参数（`IMPL_BLINK/LED/SWITCH`），全部挂在唯一的 `Configuration` 页上。
- 普通参数与用户参数的根本区别：前者有 `update_MODELPARAM_VALUE` 桥接 proc 把 GUI 值送进 VHDL generic、会综合进电路；后者没有这条 proc、不进 HDL，只当 GUI 开关用。这是从生成代码判断参数性质的最快方法。
- `xgui/fpga_base_v1_4.tcl` 定义三类回调：`init_gui` 摆控件、`update_PARAM_VALUE`/`validate_PARAM_VALUE` 联动与校验（fpga_base 里基本是空壳）、`update_MODELPARAM_VALUE` 把 `PARAM_VALUE` 搬运到 `MODELPARAM_VALUE`（仅 7 个真实 generic 各一份）。
- 端口使能 `add_port_enablement_condition "o_led" "\$IMPL_LED"` 落盘为 `component.xml` 里的 `<xilinx:isEnabled ... dependency="$IMPL_LED">`，由 Vivado 在 IP 符号/Block Design 求值，决定端口是否暴露。
- fpga_base 的可选端口是**打包级**机制：HDL 里没有 `if generate` 守卫，端口逻辑始终存在，物理裁剪交给消费方工程的综合器（输出端口无负载 → 驱动被优化）。这有别于「RTL 级条件生成」。
- `gui/fpga_base_v1_0.gtcl` 是与 fpga_base 无关的孤儿模板文件，真正生效的 GUI 脚本是 `xgui/fpga_base_v1_4.tcl`。

## 7. 下一步学习建议

- 下一讲 **u4-l3 Block Design 钩子与 AXI ID 宽度传播** 会从「参数在 GUI 内的静态联动」上升到「参数在 Block Design 中跨 IP 的动态传播」——精读 `bd/bd.tcl` 的 `init`/`pre_propagate`/`propagate` 三回调，看 `C_S00_AXI_ID_WIDTH` 这个普通参数如何被 BD 回调自动协商。
- 若想更扎实理解本讲的「打包产物」侧，可回头对照 **u4-l1** 把 `package.tcl` 命令族与 `component.xml` 节点的镜像关系再过一遍。
- 建议继续阅读的源码：把 `scripts/package.tcl`、`xgui/fpga_base_v1_4.tcl`、`component.xml`（参数与端口两段）三份文件并排打开，逐条核对每一个 `gui_create_*`/`add_port_enablement_condition` 命令落到 `component.xml` 的哪个节点——这是检验你是否真懂「输入清单 ↔ IP-XACT 产物」对应关系的最佳练习。
