# Block Design 钩子与 AXI ID 宽度传播

## 1. 本讲目标

学完本讲后，你应当能够：

- 区分 `bd/bd.tcl` 里 `init`、`pre_propagate`、`propagate` 三个 Block Design 回调各自的**触发时机**、**处理的接口模式**（master 还是 slave）以及**数据流动方向**（向外推还是向内拉）。
- 读懂 `bd::mark_propagate_only` 与 `set_property` 这两条命令如何协作：前者声明某个参数「由传播决定、用户不可手改」，后者在回调里真正把参数值从一个对象搬到另一个对象。
- 看清 AXI4 的 `ID_WIDTH` 为什么需要在主从之间自动匹配，以及当某个 AXI 主机的 `ID_WIDTH`（例如 4）与本 IP 默认值（1）不一致时，`propagate` 回调如何把对端协商出来的宽度拉回 `C_S00_AXI_ID_WIDTH`、再经由 generic 重排 `arid/rid/awid/bid` 四个端口的位宽。
- 指出 `bd.tcl` 是按 Xilinx「AXI ID 宽度传播」通用模板写的，因此虽然它包含 master 分支，但 fpga_base 只有 slave 接口 `S00_AXI`，`pre_propagate` 实际上是空转的——并理解这种「写了用不上的分支」为何不是冗余。

本讲承接 u4-l2（GUI 参数与可选端口），把镜头从「参数在 IP 自己的 GUI 内部静态联动」上升到「参数在 Block Design（BD）里跨多个 IP 动态传播」。

## 2. 前置知识

进入源码前，先建立三个直觉。

**第一，AXI4 的 ID 是什么，为什么宽度要协商。** AXI4 协议允许一个接口上多笔事务**乱序**（out-of-order）并发完成——主机先发出的读请求未必先返回数据。为了让接收方能把响应和请求重新配对，每笔事务都挂一个 **ID 标签**：读地址通道用 `arid`、读数据通道用 `rid`、写地址通道用 `awid`、写响应通道用 `bid`。`ID_WIDTH` 就是这个标签的位宽，它决定了「同时在飞」的唯一 ID 数上限（\(2^{\text{ID\_WIDTH}}\) 种）。当一个 AXI 主机和一个 AXI 从机直连或经互连（Interconnect）相连时，双方的 ID 宽度必须协调一致——互连通常会把所有主从里最宽的那个宽度传播给所有人，否则从机端口位宽装不下主机发出的标签。fpga_base 是个**从机**，它的 `ID_WIDTH` 不该由自己拍板，而应跟随所连主机。

**第二，Block Design 里一个参数住在「两个楼层」。** 与 u4-l2 讲的 `PARAM_VALUE`/`MODELPARAM_VALUE` 双房间类似，在 BD 里 `ID_WIDTH` 这种「总线参数」也分两层：一层挂在**接口引脚**（interface pin）上，即 `CONFIG.ID_WIDTH`，它代表「这根 AXI 总线对外宣称的宽度」，会被 BD 引擎沿着连线在主从之间传播；另一层挂在 **cell**（BD 里的这个 IP 实例）上，即 `C_S00_AXI_ID_WIDTH`，它最终流向 VHDL generic、决定端口物理位宽。BD 引擎会自动传播第一层，但「第一层和第二层如何同步」需要 IP 自己用回调参与——这正是 `bd.tcl` 存在的理由。

**第三，三个回调各有时机和方向。** Vivado 在校验/展开一个 BD 时，会按固定顺序调用 IP 提供的总线参数回调：

- `init`：IP 被放进 BD（或每次校验开始）时调用一次，做**一次性声明**——在这里告诉 Vivado「我的 `C_S00_AXI_ID_WIDTH` 不是给用户手填的，它将由传播决定」。
- `pre_propagate`：BD 引擎**开始跨连线传播参数之前**调用。此刻连线上还没有值流过来，适合让 IP 把自己手里的值**向外推**（cell → interface pin）。
- `propagate`：BD 引擎**传播完毕、值已经到达本 IP 的接口引脚之后**调用。此刻适合让 IP 把到达引脚的值**向内拉**（interface pin → cell）。

把这三点合起来就得到本讲的核心对称结构：**master 是 ID 宽度的源头，所以它在 `pre_propagate` 里向外推；slave 是 ID 宽度的汇，所以它在 `propagate` 里向内拉。** fpga_base 是 slave，因此真正干活的是 `init` 和 `propagate`。

带着这三点直觉，我们去读源码。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `bd/bd.tcl` | Block Design 回调脚本 | `init`/`pre_propagate`/`propagate` 三回调、`mark_propagate_only`、master/slave 两个分支 |
| `hdl/fpga_base_v1_0.vhd` | 顶层 RTL | `C_S00_AXI_ID_WIDTH` generic 如何决定 `arid/rid/awid/bid` 端口位宽；确认本 IP 只有 slave 接口 |
| `component.xml` | 打包产物（IP-XACT 清单） | 端口位宽对 `MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH` 的依赖、参数默认值 1 |

一句话关系：`bd.tcl` 把 BD 协商出的 `ID_WIDTH` 写回 cell 参数 `C_S00_AXI_ID_WIDTH` → 该值经 `MODELPARAM_VALUE` 流入 VHDL generic → 决定四个 ID 端口的物理位宽（`component.xml` 里这些端口的 `left` 边界用一个依赖表达式引用 `MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH`）。`bd.tcl` 是这条链路的「自动对齐器」。

## 4. 核心概念与源码讲解

### 4.1 Block Design 回调机制

#### 4.1.1 概念说明

Vivado 的 Block Design 在校验（validate）和展开（elaborate）时，会按固定时机调用 IP 提供的一组 TCL 回调。`bd.tcl` 里定义的 `init`、`pre_propagate`、`propagate` 三个 `proc`，就是其中专门用于**总线参数（bus parameter）传播**的三件套。它们的名字不是随便起的——Vivado 靠**名字约定**来识别这些回调：你在 `bd/bd.tcl` 里定义一个名为 `init` 的 proc，Vivado 就会在「初始化」时机调用它；名为 `pre_propagate` 就在「传播前」调用；名为 `propagate` 就在「传播后」调用。三个回调的签名完全一致：

```tcl
proc <名字> { cellpath otherInfo } { ... }
```

其中 `cellpath` 是本 IP 实例在 BD 里的路径（用 `get_bd_cells $cellpath` 拿到 cell 句柄），`otherInfo` 是 Vivado 传入的附加信息（本脚本未用到）。三个回调各自遍历本 IP 的所有接口引脚，但用不同的过滤器（`MODE` 或 `PROTOCOL`）挑出自己关心的那部分。

#### 4.1.2 核心流程

三个回调的触发顺序与各自职责可以用下面这张时序图概括：

```
IP 被放进 BD / 校验开始
        │
        ▼
   ① init(cellpath)
      ─ 遍历所有 interface pin
      ─ 过滤: MODE == "slave"
      ─ 对 S00_AXI: mark_propagate_only C_S00_AXI_ID_WIDTH
      ─ 作用: 声明该参数由传播决定,不可手改
        │
        ▼
   ② pre_propagate(cellpath)        ← BD 传播之前
      ─ 遍历所有 interface pin
      ─ 过滤: PROTOCOL=="AXI4" 且 MODE=="master"
      ─ 对每个 master: 把 cell 值推到 pin (cell → pin)
      ─ 作用: 让 IP 自己的值先摆到引脚上,供 BD 向外传播
        │
        ▼
   〔BD 引擎跨连线传播总线参数,值到达各 IP 的 pin〕
        │
        ▼
   ③ propagate(cellpath)            ← BD 传播之后
      ─ 遍历所有 interface pin
      ─ 过滤: PROTOCOL=="AXI4" 且 MODE=="slave"
      ─ 对每个 slave: 把 pin 值拉回 cell (pin → cell)
      ─ 作用: 把对端协商出的宽度写进 cell 参数
```

注意三个回调里重复出现的一行 `set axi_standard_param_list [list ID_WIDTH]`——它把「要传播的参数清单」写死成只有 `ID_WIDTH` 一项。如果将来要传播更多总线参数（如 `DATA_WIDTH`），只需往这个 list 里加元素，三个回调的主循环就会自动覆盖。

#### 4.1.3 源码精读

先看三个回调的签名与各自的过滤条件。

[bd/bd.tcl:2-22](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L2-L22) —— `init` 回调。它遍历所有接口引脚，用 `MODE == "slave"` 过滤，再进一步用本地变量 `full_sbusif_list`（值为 `S00_AXI`）限定只处理名为 `S00_AXI` 的从机接口，最后对构造出的参数名调用 `mark_propagate_only`。这一段会在 4.2 详讲。

[bd/bd.tcl:25-53](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L25-L53) —— `pre_propagate` 回调。关键过滤器在前面：先要求 `CONFIG.PROTOCOL == "AXI4"`（跳过 AXI Lite 等其它总线），再要求 `MODE == "master"`。也就是说它**只处理 AXI4 主机接口**。

[bd/bd.tcl:32-37](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L32-L37) —— `pre_propagate` 的双过滤器：协议必须是 `AXI4`，模式必须是 `master`，否则 `continue` 跳过。

[bd/bd.tcl:56-85](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L56-L85) —— `propagate` 回调。过滤结构同上，但第二个过滤器换成 `MODE == "slave"`，即**只处理 AXI4 从机接口**。

[bd/bd.tcl:63-68](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L63-L68) —— `propagate` 的双过滤器：协议必须是 `AXI4`，模式必须是 `slave`，否则 `continue`。

把三段过滤条件并排放，就得到本讲最核心的一张对照表：

| 回调 | 协议过滤 | 模式过滤 | 处理谁 | 数据方向 |
| --- | --- | --- | --- | --- |
| `init` | （无，靠 `full_sbusif_list` 名单） | `slave` | `S00_AXI` | 声明（不搬运值） |
| `pre_propagate` | `AXI4` | `master` | （fpga_base 无 master） | cell → pin（向外推） |
| `propagate` | `AXI4` | `slave` | `S00_AXI` | pin → cell（向内拉） |

#### 4.1.4 代码实践

**实践目标**：从源码层面确认三个回调各自处理 master 还是 slave，并解释为什么 fpga_base 的 `pre_propagate` 是空转的。

**操作步骤**：

1. 打开 `bd/bd.tcl`，分别在 `init`（第 10 行）、`pre_propagate`（第 35 行）、`propagate`（第 66 行）找到 `MODE` 判断，记下各自要求的模式。
2. 打开 `hdl/fpga_base_v1_0.vhd` 的 entity（第 27–102 行），确认本 IP **只有** `S00_AXI` 一个 AXI 接口，且从信号方向看它是**从机**（`arvalid/awvalid/wvalid` 为 `in`、`arready/awready/wready` 为 `out`，见 [hdl/fpga_base_v1_0.vhd:70-71](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L70-L71)）。
3. 在 entity 里搜索 `M00_AXI` 或任何 master 接口——你将一无所获。
4. 结论：`pre_propagate` 的 `foreach` 循环虽然会执行，但每次都因 `MODE != "master"` 而 `continue`，所以它对 fpga_base **不做任何事**。

**需要观察的现象**：第 1–3 步是静态文本事实，可在编辑器里直接核对。

**预期结果**：`init` 与 `propagate` 处理 **slave**（`S00_AXI`），`pre_propagate` 处理 **master**，而 fpga_base 没有 master 接口，故 `pre_propagate` 空转。这一步可在源码里 100% 确认。至于三个回调在真实 Vivado BD 里的实际触发顺序与效果，本仓库未提供可一键运行的 BD 工程脚本，标注为「待本地验证」——建议在装有 Vivado 的机器上把本 IP 拖进一个 BD，连上一个 AXI 主机，用 `puts` 在三个回调里打印日志，观察调用顺序与 `ID_WIDTH` 的变化。

#### 4.1.5 小练习与答案

**练习 1**：`bd.tcl` 是靠什么机制让 Vivado 识别出这三个 `proc` 是 BD 回调的？如果我把 `propagate` 改名为 `propagate_id`，会发生什么？

**参考答案**：靠**名字约定**。Vivado 在 BD 校验时按固定名字（`init`/`pre_propagate`/`propagate`）去查找 IP 提供的回调并调用。改名为 `propagate_id` 后，Vivado 找不到名为 `propagate` 的 proc，就不会在「传播后」时机调用它——`C_S00_AXI_ID_WIDTH` 将不再被自动同步，slave 端口位宽会停留在默认值 1，可能与所连主机不匹配。

**练习 2**：三个回调里都有一行 `set axi_standard_param_list [list ID_WIDTH]`。如果删掉 `init` 里的这一行、只保留 `pre_propagate` 和 `propagate` 里的，会有影响吗？

**参考答案**：有影响。`init` 用这个 list 构造参数名 `C_S00_AXI_ID_WIDTH` 并调用 `mark_propagate_only`（[bd/bd.tcl:16-19](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L16-L19)）。删掉后该 list 为空，`foreach` 不执行，`mark_propagate_only` 不会被调用，于是 `C_S00_AXI_ID_WIDTH` 不再被标记为「传播决定」——后续 `propagate` 即使把值写回 cell，也可能与 Vivado 的预期冲突或被 GUI 当成普通可编辑参数。这条 list 是三个回调共享的「传播清单」，三处必须一致。

### 4.2 AXI 参数传播：mark_propagate_only 与 set_property 的协作

#### 4.2.1 概念说明

把参数值在 cell 与 interface pin 之间搬来搬去，靠两条命令分工：

- `bd::mark_propagate_only $cell_handle $param_list`：这是一条**声明**。它告诉 Vivado：「列表里的这些参数（这里是 `C_S00_AXI_ID_WIDTH`）的值由 `propagate` 回调负责设定，请勿让用户在 GUI 里手填，也别在传播时把它们当成 IP 的硬性输入」。它的作用是**解除歧义**——同一个参数既可能被用户设、又可能被传播设，`mark_propagate_only` 明确把决定权交给传播。注意它**不搬运任何值**，只改属性。
- `set_property CONFIG.<名字> <值> <对象>`：这是 Vivado 通用属性写入命令，真正把一个值写到一个对象上。对象可以是 cell（写 cell 参数）或 interface pin（写总线参数）。三个回调里，`init` 不用它，`pre_propagate` 用它把值写到 **pin**，`propagate` 用它把值写到 **cell**。

二者协作的完整逻辑是：`init` 先用 `mark_propagate_only` 把参数「登记」为传播驱动；随后 `pre_propagate`/`propagate` 在合适方向上用 `set_property` 真正搬运值。

#### 4.2.2 核心流程

参数名的构造遵循一条固定公式（这是 BD hook 与 HDL generic 之间的契约）：

```
param_name = "C_" + <接口名> + "_" + <标准参数名>
           = "C_" + "S00_AXI" + "_" + "ID_WIDTH"
           = "C_S00_AXI_ID_WIDTH"
```

注意这个拼出来的名字必须与顶层 VHDL entity 里的 generic **逐字符相同**——它就是 generic `C_S00_AXI_ID_WIDTH`。这是 BD 回调能驱动 HDL generic 的根本原因。

三段搬运逻辑（去掉遍历框架后的伪代码）：

```
# init (slave)
mark_propagate_only $cell [list C_S00_AXI_ID_WIDTH]      # 声明: 由传播决定

# pre_propagate (master) —— 向外推 cell → pin
if {pin值 != cell值 且 cell值 != ""} {
    set_property CONFIG.ID_WIDTH $cell值 $pin            # pin ← cell
}

# propagate (slave) —— 向内拉 pin → cell
if {pin值 != cell值 且 pin值 != ""} {
    set_property CONFIG.C_S00_AXI_ID_WIDTH $pin值 $cell   # cell ← pin
}
```

留心两个非空守卫的**不对称**：`pre_propagate` 检查的是「cell 值非空」（IP 自己得先有值才能往外推），`propagate` 检查的是「pin 值非空」（得有对端传过来的值才能往回写）。这个不对称恰好反映了 master 是源、slave 是汇的角色分工。

#### 4.2.3 源码精读

先看 `init` 如何构造参数名并登记。

[bd/bd.tcl:11-19](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L11-L19) —— 对每个通过过滤的 slave 接口，构造 `busif_param_list`：对 `axi_standard_param_list`（只有 `ID_WIDTH`）里的每一项，拼出 `C_${busif_name}_${tparam}`（即 `C_S00_AXI_ID_WIDTH`），再调用 `bd::mark_propagate_only $cell_handle $busif_param_list` 把它登记为传播驱动。第 13–15 行的 `lsearch` 把处理范围限定在 `full_sbusif_list`（`S00_AXI`）里——名字不在名单里的从机接口会被 `continue` 跳过。

再看两个搬运方向。`pre_propagate`（master，向外推）：

[bd/bd.tcl:43-50](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L43-L50) —— 读 `val_on_cell_intf_pin`（pin 上的 `CONFIG.ID_WIDTH`）和 `val_on_cell`（cell 上的 `CONFIG.C_S00_AXI_ID_WIDTH`）。若两者不等且 cell 值非空，则 `set_property CONFIG.ID_WIDTH $val_on_cell $busif`，把 cell 的值**写到 pin** 上（方向 cell → pin）。注意写的是 `CONFIG.${tparam}`（即 `CONFIG.ID_WIDTH`），目标是 `$busif`（pin）。

`propagate`（slave，向内拉）：

[bd/bd.tcl:74-82](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L74-L82) —— 结构镜像 `pre_propagate`，但非空守卫换成 `val_on_cell_intf_pin != ""`，且 `set_property` 的目标是 `$cell_handle`、写的属性是 `CONFIG.${busif_param_name}`（即 `CONFIG.C_S00_AXI_ID_WIDTH`）。方向是 pin → cell：把对端协商后到达 pin 的宽度**写回 cell 参数**。第 78 行注释 `override property of bd_interface_net to bd_cell -- only for slaves` 直白说明了这一意图。

最后验证参数名契约：拼出来的 `C_S00_AXI_ID_WIDTH` 必须等于 VHDL generic。

[hdl/fpga_base_v1_0.vhd:38-40](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L38-L40) —— 顶层 generic `C_S00_AXI_ID_WIDTH : integer := 1`，与 `bd.tcl` 拼出的名字完全一致。这是 BD 回调写回的值能流入 HDL 的关键。

#### 4.2.4 代码实践

**实践目标**：亲手把「参数名拼接公式」在源码里走一遍，确认 BD hook 与 HDL generic 的命名契约，并解释 `mark_propagate_only` 与 `set_property` 的分工。

**操作步骤**：

1. 在 `bd/bd.tcl` 第 6 行看到 `set axi_standard_param_list [list ID_WIDTH]`、第 7 行看到 `set full_sbusif_list [list S00_AXI]`。
2. 在第 17 行看到拼接 `lappend busif_param_list "C_${busif_name}_${tparam}"`。代入 `busif_name=S00_AXI`、`tparam=ID_WIDTH`，得到 `C_S00_AXI_ID_WIDTH`。
3. 在 [hdl/fpga_base_v1_0.vhd:39](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L39) 确认 generic 名字正是 `C_S00_AXI_ID_WIDTH`——三者吻合。
4. 区分两条命令：第 19 行 `bd::mark_propagate_only` 只**声明**、不写值；第 48 行与第 80 行的 `set_property` 才**真正搬运**值。

**需要观察的现象**：全部为静态文本事实。

**预期结果**：命名契约可在源码里 100% 确认；`mark_propagate_only`（声明）与 `set_property`（搬运）的分工清晰。这一步不依赖运行 Vivado。

#### 4.2.5 小练习与答案

**练习 1**：`propagate` 回调里写回 cell 时，用的是 `CONFIG.${busif_param_name}`（即 `CONFIG.C_S00_AXI_ID_WIDTH`），而不是 `CONFIG.ID_WIDTH`。为什么目标属性名不同？

**参考答案**：因为两层参数名字不同。挂在 interface pin 上的总线参数叫 `ID_WIDTH`（不带前缀），挂在 cell 上的参数叫 `C_S00_AXI_ID_WIDTH`（带 `C_<接口名>_` 前缀，与 VHDL generic 同名）。`propagate` 是把 pin 的值搬到 cell，所以源属性是 `CONFIG.ID_WIDTH`（pin 上，[bd/bd.tcl:74](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L74)），目标属性是 `CONFIG.C_S00_AXI_ID_WIDTH`（cell 上，[bd/bd.tcl:80](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L80)）。

**练习 2**：如果把 `init` 里 `mark_propagate_only` 那一行注释掉，`propagate` 里的 `set_property CONFIG.C_S00_AXI_ID_WIDTH ...` 还能正常把值写回 cell 吗？两者是什么关系？

**参考答案**：`set_property` 本身仍可执行（它是通用写属性命令，不依赖 `mark_propagate_only`）。但缺少 `mark_propagate_only` 的声明后，Vivado 不再认为 `C_S00_AXI_ID_WIDTH` 是「传播驱动」参数，可能在 GUI 上把它显示为可手编辑、或在传播时与用户值发生冲突。`mark_propagate_only` 是**语义声明**，`set_property` 是**数值搬运**；前者让后者的结果被 Vivado 正确理解和接受，二者配合才是一个完整的传播方案。

### 4.3 主从 ID 宽度协商

#### 4.3.1 概念说明

现在把前两节合起来，回答本讲标题里的「主从 ID 宽度协商」。核心是一句对称的话：**master 是 ID 宽度的源，slave 是 ID 宽度的汇。**

- 一个 AXI 主机自己决定发出多宽的 ID（它知道自己要并发多少笔事务），所以主机是 ID 宽度的**生产者**。生产者的值需要先摆到自己的 interface pin 上，再由 BD 引擎沿着连线传播给下游——这就是 `pre_propagate` 在传播前「向外推」的理由。
- 一个 AXI 从机不该自作主张决定 ID 宽度，它该接受所连主机（或互连）带来的宽度，所以从机是 ID 宽度的**消费者**。消费者的值在 BD 引擎传播完毕后才到达自己的 interface pin，需要再从 pin 拉回 cell——这就是 `propagate` 在传播后「向内拉」的理由。

`bd.tcl` 把这套对称逻辑写成了一个**通用模板**：它同时包含 master 分支（`pre_propagate`）和 slave 分支（`propagate`），无论被放进 BD 的 IP 是主机、从机还是两者皆有，都能各取所需。fpga_base 是纯从机（只有 `S00_AXI`），所以 master 分支对它而言是「空转」的——但这并非冗余，而是模板为了覆盖所有 IP 形态而保留的对称结构。这也解释了为什么一个从机 IP 的 BD hook 里会出现处理 master 的代码。

#### 4.3.2 核心流程

用一个具体场景把链路走通：假设某 AXI 主机 `ID_WIDTH=4`，而 fpga_base 的 `C_S00_AXI_ID_WIDTH` 默认为 `1`，二者在 BD 里相连。

```
主机 ID_WIDTH = 4
        │ (主机的 pre_propagate 把 4 推到主机 pin)
        ▼
   主机 pin: ID_WIDTH = 4
        │ (BD 引擎跨互连传播)
        ▼
   fpga_base S00_AXI pin: ID_WIDTH = 4   ← 对端协商结果到达
        │ (本 IP 的 propagate 触发, pin→cell)
        ▼
   set_property CONFIG.C_S00_AXI_ID_WIDTH 4  $cell
        │ (cell 参数 → MODELPARAM_VALUE → VHDL generic)
        ▼
   generic C_S00_AXI_ID_WIDTH = 4
        │
        ▼
   arid/rid/awid/bid 位宽 = (4-1 downto 0) = 4 位   ← 端口物理位宽匹配主机
```

关键在于最上面那个「fpga_base 的默认 1」被 `propagate` **覆盖**成了「对端协商出的 4」。这就是「主机的 ID_WIDTH 与本 IP 不一致时，`propagate` 如何修正」的完整答案。

#### 4.3.3 源码精读

先确认 master/slave 两个分支的过滤对称性。

[bd/bd.tcl:35-37](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L35-L37) —— `pre_propagate` 的 master 过滤（`MODE == "master"`）。

[bd/bd.tcl:66-68](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L66-L68) —— `propagate` 的 slave 过滤（`MODE == "slave"`）。两者镜像，只差一个模式词。

再确认 fpga_base 确实是 slave-only、且 `C_S00_AXI_ID_WIDTH` 真的控制四个 ID 端口。

[hdl/fpga_base_v1_0.vhd:62](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L62) —— `s00_axi_arid : in std_logic_vector(C_S00_AXI_ID_WIDTH-1 downto 0)`，读地址 ID，宽度由 generic 决定。

[hdl/fpga_base_v1_0.vhd:73](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L73) —— `s00_axi_rid`（读数据 ID）同样依赖 `C_S00_AXI_ID_WIDTH`。

[hdl/fpga_base_v1_0.vhd:80](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L80) —— `s00_axi_awid`（写地址 ID）。

[hdl/fpga_base_v1_0.vhd:97](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L97) —— `s00_axi_bid`（写响应 ID）。四个 ID 端口位宽全部由同一个 generic 控制，所以 `propagate` 改写 `C_S00_AXI_ID_WIDTH` 一次，四个端口同时跟着重排。

然后在打包产物侧确认这条依赖链确实落盘。

[component.xml:540-547](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L540-L547) —— `s00_axi_arid` 端口的位宽声明。`<spirit:left>` 用 `spirit:resolve="dependent"` 和 `spirit:dependency="(spirit:decode(id('MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH')) - 1)"` 表示左边界「依赖」`MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH`。这正是「cell 参数 → MODELPARAM → 端口位宽」链路在 IP-XACT 里的表达。`rid/awid/bid` 三个端口有完全相同的依赖节点（见 [component.xml:727](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L727)、[component.xml:820](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L820)、[component.xml:1087](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1087)）。

[component.xml:1177-1181](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1177-L1181) —— `MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH` 的模型参数，默认值 `1`，`resolve="generated"` 表示它由 `PARAM_VALUE`（即 GUI/BD 设的值）生成而来。

[component.xml:1351-1355](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1351-L1355) —— `PARAM_VALUE.C_S00_AXI_ID_WIDTH` 用户参数，默认值 `1`，`resolve="user"` 表示它可由用户/BD 设置。`bd.tcl` 的 `propagate` 写回的正是这一层。

最后是一条值得点出的诚实观察：`bd.tcl` 虽然按 Vivado BD 回调约定编写，但在本仓库当前的 `component.xml` 里**查不到对它的显式引用**——五个 fileSet（综合、仿真、xgui、utility/logo、驱动）都不包含 `bd/` 下的文件（见 [component.xml:1191-1318](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1191-L1318) 的全部 fileSet 条目），`scripts/package.tcl`（u4-l1 讲的打包输入）里也没有任何把 `bd/bd.tcl` 登记为 BD 钩子的命令。因此这个脚本**是否会被打包进分发产物并真正在消费方 BD 里触发**，取决于 Vivado/PsiIpPackage 是否通过其它机制（如 IP-XACT 的 BD-hook 注册）登记了它——这一点在本仓库静态源码里无法确认，标注为「待本地验证」。无论它是否被自动登记，理解 `bd.tcl` 的三回调逻辑本身就是理解 Xilinx AXI IP「ID 宽度自动协商」机制的关键。

#### 4.3.4 代码实践

**实践目标**：本讲对应的代码实践任务——区分三个回调处理 master 还是 slave，并解释当主机 `ID_WIDTH` 与本 IP 不一致时 `propagate` 如何修正。

**操作步骤（master/slave 区分）**：

1. 在 `bd.tcl` 里定位三个回调的 `MODE` 判断：`init` 要求 `slave`（[L10](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L10)）、`pre_propagate` 要求 `master`（[L35](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L35)）、`propagate` 要求 `slave`（[L66](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L66)）。
2. 结合 entity 确认 fpga_base 只有 `S00_AXI`（slave），无 master。
3. 结论：`init`、`propagate` 处理 slave（实际生效），`pre_propagate` 处理 master（空转）。

**操作步骤（mismatch 修正追踪）**：假设主机 `ID_WIDTH=4`、本 IP 默认 `C_S00_AXI_ID_WIDTH=1`。

1. `init` 先把 `C_S00_AXI_ID_WIDTH` 标记为 propagate-only（[bd/bd.tcl:19](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L19)），声明默认值 `1` 不是最终结论。
2. 主机的 `pre_propagate` 把 `4` 推到主机 pin；BD 引擎经互连把 `ID_WIDTH=4` 传播到 fpga_base 的 `S00_AXI` pin。
3. 本 IP `propagate` 读取 `val_on_cell_intf_pin`（pin 上的 `ID_WIDTH=4`）与 `val_on_cell`（cell 上的 `1`），二者不等且 pin 值非空（[bd/bd.tcl:77-79](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L77-L79)）。
4. 执行 `set_property CONFIG.C_S00_AXI_ID_WIDTH 4 $cell_handle`（[bd/bd.tcl:80](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L80)），cell 参数被改写为 `4`。
5. `4` 经 `MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH` 流入 VHDL generic（[component.xml:1180](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1180)），四个 ID 端口位宽变为 `(3 downto 0)`（[hdl/fpga_base_v1_0.vhd:62](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L62) 等），与主机匹配。

**需要观察的现象**：第 1–2 组步骤是静态文本事实；mismatch 修正的运行期效果（步骤 3 里值是否真等于 4）依赖真实 Vivado BD。

**预期结果**：master/slave 区分与各步源码定位可 100% 在源码确认；mismatch 修正的**逻辑**可由源码完全推导（`propagate` 用非空守卫保证只在有对端值时覆盖、用 `set_property` 把 pin 值写回 cell、再由 generic 重排端口）。真实 BD 里的具体数值变化标注为「待本地验证」——建议在装有 Vivado 的机器上把本 IP 与一个 `ID_WIDTH=4` 的主机（如 AXI Interconnect 下游）相连，在校验前后用 `get_property CONFIG.C_S00_AXI_ID_WIDTH [get_bd_cells <本IP>]` 观察值是否从 `1` 变为 `4`。

#### 4.3.5 小练习与答案

**练习 1**：`propagate` 里向 cell 写值前有一个守卫 `if { $val_on_cell_intf_pin != "" }`（[bd/bd.tcl:79](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L79)）。如果删掉这个守卫，会发生什么？

**参考答案**：若 fpga_base 的 `S00_AXI` 还没连任何主机（pin 上 `ID_WIDTH` 为空），删掉守卫后会把空串写回 `C_S00_AXI_ID_WIDTH`，可能把一个合法的默认值 `1` 覆盖成空、引发 Vivado 报错或综合时端口位宽异常。守卫的意义是「只有当对端真的传来了一个非空宽度时，才用它覆盖本 IP 的值」——这是一种安全的「有值才覆盖」策略，避免在未连接时误伤默认值。

**练习 2**：fpga_base 是 slave-only，`pre_propagate`（master 分支）对它空转。为什么作者还要把 master 分支留在 `bd.tcl` 里？

**参考答案**：因为 `bd.tcl` 是 Xilinx 「AXI ID 宽度传播」的**通用模板**，原本就为「可能含 master、slave 或两者皆有的 IP」设计。把 master 分支删掉，对 fpga_base 当前行为没有影响，但会破坏模板的对称性和可复用性——如果将来这个 IP（或抄走这段模板的别的 IP）增加了 master 接口，没有 master 分支就失去向外推 ID 宽度的能力。保留它是为了模板的完整性与可移植性，而非当前功能需要。

**练习 3**：假设某主机的 `ID_WIDTH` 比本 IP 综合时支持的更宽（例如主机 8 位、而某些资源限制使从机不想超过 4 位）。`bd.tcl` 的 `propagate` 会拒绝这个不匹配吗？

**参考答案**：不会。`propagate`（[bd/bd.tcl:74-82](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L74-L82)）只做「pin 值非空就原样写回 cell」，没有任何范围校验（注释 `May check for supported values` 提示了可加但未加）。也就是说它会忠实地把 `8` 写进 `C_S00_AXI_ID_WIDTH`，端口位宽随之变成 8 位。是否允许这样宽、互连是否需要做适配，是 BD 引擎与互连 IP 的职责，不是这个回调管的。这正是 `init` 里 `mark_propagate_only` 的用意——本 IP 在 ID 宽度上完全顺从对端，不设自己的偏好。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「读源码 + 走链路」的小任务。

**任务**：假设你在一个 BD 里用了一个 AXI Interconnect，其下游主接口 `M00_AXI` 的 `ID_WIDTH=6`，连到 fpga_base 的 `S00_AXI`。请仅基于本讲学到的机制，在不实际运行 Vivado 的前提下，完整描述 `C_S00_AXI_ID_WIDTH` 与四个 ID 端口位宽的变化过程，并指出整个链路里 `bd.tcl` 的三个回调分别在哪一步起作用。

**要求产出**：

1. **起始状态**：写出 `C_S00_AXI_ID_WIDTH` 的默认值（提示：从 [component.xml:1354](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1354) 和 [hdl/fpga_base_v1_0.vhd:39](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L39) 确认）。
2. **三回调各自的角色**：用一句话说明 `init`、`pre_propagate`、`propagate` 在本场景里哪个真干活、哪个空转、为什么。
3. **修正后的最终值**：写出 BD 校验后 `C_S00_AXI_ID_WIDTH` 的值，以及 `arid/rid/awid/bid` 的最终位宽，并指出是 `bd.tcl` 的哪一行真正完成了 cell 参数改写。
4. **诚实性检验**：根据 4.3.3 末尾的观察，说明要让上述自动协商在消费方工程里真正生效，还需要满足什么前提（提示：`bd.tcl` 需被注册为 BD 钩子）。

**预期结果（自检）**：

- 步骤 1：默认值是 `1`。
- 步骤 2：`init` 真干活（标记 propagate-only，[bd/bd.tcl:19](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L19)）；`pre_propagate` 空转（fpga_base 无 master 接口）；`propagate` 真干活（向内拉，[bd/bd.tcl:80](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L80)）。
- 步骤 3：`C_S00_AXI_ID_WIDTH` 变为 `6`，四个 ID 端口位宽变为 `(5 downto 0)`；完成改写的是 [bd/bd.tcl:80](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L80) 的 `set_property CONFIG.C_S00_AXI_ID_WIDTH $val_on_cell_intf_pin $cell_handle`。
- 步骤 4：前提是 `bd.tcl` 必须被打包进 IP 并被 Vivado 登记为 BD 钩子脚本——而本仓库 `component.xml` 的 fileSet 中并未显式包含 `bd/bd.tcl`，因此这一前提是否满足「待本地验证」。

这个任务把「回调时机」「传播方向」「主从角色」「BD hook 到 HDL generic 的命名契约」四件事逼到同一条链路上：**master 推、slave 拉，`propagate` 在传播后用非空守卫把对端宽度安全地写回 `C_S00_AXI_ID_WIDTH`，generic 再重排端口。**

## 6. 本讲小结

- `bd/bd.tcl` 定义三个 Block Design 回调，签名都是 `proc <名> { cellpath otherInfo }`，Vivado 靠**名字约定**（`init`/`pre_propagate`/`propagate`）在 BD 校验的固定时机调用它们。
- 三回调分工：`init` 处理 **slave**、做一次性声明；`pre_propagate` 处理 **master**、在 BD 传播前把 cell 值**向外推**到 pin；`propagate` 处理 **slave**、在 BD 传播后把 pin 值**向内拉**回 cell。触发顺序是 `init` → `pre_propagate` →〔BD 传播〕→ `propagate`。
- 两条命令分工：`bd::mark_propagate_only`（`init` 里，[bd/bd.tcl:19](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L19)）只**声明**「该参数由传播决定」；`set_property`（`pre_propagate`/`propagate` 里）才**搬运**值。非空守卫不对称（master 看 cell 非空、slave 看 pin 非空），反映 master 是源、slave 是汇。
- 参数名按 `C_<接口名>_<标准参数>` 拼接，得到 `C_S00_AXI_ID_WIDTH`，与顶层 VHDL generic 逐字符相同——这是 BD hook 能驱动 HDL 的契约。
- fpga_base 是 slave-only，只有 `S00_AXI`，所以 `init`/`propagate` 真干活、`pre_propagate`（master 分支）空转。master 分支是 Xilinx 通用模板为对称性与可复用性而保留的，并非当前冗余。
- 当主机 `ID_WIDTH` 与本 IP 默认 `1` 不一致时，`propagate` 用非空守卫把对端协商出的宽度安全写回 `C_S00_AXI_ID_WIDTH`，经 `MODELPARAM_VALUE` 流入 generic，重排 `arid/rid/awid/bid` 四端口位宽（[component.xml:545](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L545) 等四处依赖节点）。
- 诚实观察：`bd.tcl` 未出现在 `component.xml` 任何 fileSet 中，也未在 `package.tcl` 里被登记；它是否在打包产物中真正生效「待本地验证」。理解其逻辑本身仍是掌握 Xilinx AXI ID 宽度自动协商机制的关键。

## 7. 下一步学习建议

- 至此第 4 单元（Vivado IP 集成机制）结束：u4-l1 讲了 PsiIpPackage 打包 DSL，u4-l2 讲了 GUI 参数与可选端口，本讲讲了 BD 回调与跨 IP 参数传播。建议把这三篇并排重读，体会「参数」在三个层次的流动：打包时声明（`package.tcl`）→ GUI 内静态联动（`xgui/*.tcl`）→ BD 内跨 IP 动态传播（`bd/bd.tcl`）。
- 下一单元 **u5 软件栈与系统集成** 会从「硬件/IP 集成」上升到「软件如何使用这个 IP」：u5-l1 精读裸机 C 驱动如何用 `Xil_IO` 经 AXI 访问寄存器，u5-l2 讲 Vitis 驱动构建与 `xparameters.h` 生成，u5-l3 讲 JTAG-to-AXI 调试 TCL 与 EPICS 集成。本讲建立的「AXI4 接口与寄存器空间」认知是 u5 全部三篇的共同地基（可回看 u2-l3 的寄存器映射）。
- 建议继续阅读的源码：把 `bd/bd.tcl` 与 [hdl/fpga_base_v1_0.vhd:38-40](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L38-L40) 的 generic 声明、[component.xml:540-547](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L540-L547) 的端口位宽依赖三处并排打开，逐条核对「BD 回调写回的参数名 → generic → 端口位宽」这条链路——这是检验你是否真懂 AXI ID 宽度自动协商的最佳练习。若手边有 Vivado，把本 IP 拖进 BD、连上不同 `ID_WIDTH` 的主机、在三个回调里加 `puts` 日志，观察实际触发顺序与参数变化，是把本讲从「读源码」推进到「验证」的最直接方式。
