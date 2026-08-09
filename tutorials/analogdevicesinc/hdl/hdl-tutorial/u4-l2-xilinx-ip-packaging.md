# Xilinx IP 打包：adi_ip_xilinx.tcl 与 *_ip.tcl

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「一段 Verilog 是如何变成一个可被 Vivado 拖拽复用的 IP」的标准流水线，并指出 `adi_ip_create` / `adi_ip_files` / `adi_ip_properties` 三者各自的职责。
- 区分接口的「自动推断」与「显式声明」两种机制，看懂 `adi_ip_infer_mm_interfaces`、`adi_add_bus`、`adi_add_bus_clock`、`adi_if_define` 等原语的分工。
- 理解 `*_ip.tcl` 在打包时只是「注册」了 `bd.tcl` 与 `*.ttcl`，而它们的真正逻辑分别运行在「块设计时」和「IP 综合时」，并能解释二者如何形成一个反馈环。
- 读懂任意一个 ADI library 模块的 `*_ip.tcl`，预测它打包出了哪些接口与参数。

## 2. 前置知识

本讲默认你已掌握 [u4-l1](u4-l1-library-structure.md) 的核心结论：库侧用 `make xilinx` 触发 `library.mk` 中的 `component.xml` 目标，而该目标的命令就是

```makefile
$(VIVADO) $(LIBRARY_NAME)_ip.tcl
```

也就是 `vivado -mode batch -source axi_dmac_ip.tcl`（见 [library/scripts/library.mk:L116-L125](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L116-L125)）。所以 **`*_ip.tcl` 是库侧打包的「主入口脚本」**，本讲要拆的就是它内部到底做了什么。

还需几个术语铺垫：

- **IP-XACT / component.xml**：一种 XML 标准，用来描述一个 IP 的「元数据」——厂商/库/名字/版本（VLNV）、源文件清单、参数、对外接口、地址映射、GUI 布局。Vivado 的 IP 目录（IP Catalog）认的就是这份描述。Verilog 本身只是其中的「实现」，`component.xml` 才是「身份证」。
- **VLNV**：Vendor : Library : Name : Version 的缩写，例如 `analog.com:user:axi_dmac:1.0`，是 IP 在仓库里的唯一编号。
- **总线接口（bus interface）**：把一组零散端口（如 `m_axis_valid`、`m_axis_data`、`m_axis_ready`）按某种标准（如 AXI-Stream）捆绑成一个有名字的「接口」（如 `m_axis`），这样在块设计里就能整根连线，而不是逐根连信号。
- **块设计（Block Design, BD）**：Vivado 里用方框图拼系统的环境。本讲会涉及 BD 在放置 IP 时调用的回调钩子。

> 一句话直觉：`*_ip.tcl` 的工作，就是把一堆 `.v` 文件 + 一份「这些端口属于哪个接口、哪些参数控制哪些端口」的说明，翻译成一份 `component.xml`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [library/scripts/adi_ip_xilinx.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_xilinx.tcl) | **打包原语库**。定义 `adi_ip_create`、`adi_ip_files`、`adi_ip_properties`、`adi_add_bus` 等所有 `adi_*` 过程，是对 Vivado `ipx::*` 底层命令的封装。所有 Xilinx 库共用它。 |
| [library/axi_dmac/axi_dmac_ip.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl) | **打包主入口脚本**。`source` 上面那个原语库后，依次调用各原语把 axi_dmac 打包成 IP。本讲的「样本」。 |
| [library/axi_dmac/bd/bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/bd/bd.tcl) | **块设计钩子脚本**。定义 `init`/`post_config_ip`/`propagate`/`post_propagate` 四个回调，在 IP 被拖入块设计时由 Vivado 自动调用。 |
| [library/axi_dmac/axi_dmac_constr.ttcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_constr.ttcl) | **参数化约束模板**（ttcl）。在 IP 综合时被求值，按参数生成一份 `.xdc` 时序约束。 |
| library/scripts/library.mk | （承自 u4-l1）定义 `xilinx` 目标，用 `vivado *_ip.tcl` 产出 `component.xml`。 |

## 4. 核心概念与源码讲解

### 4.1 打包三步走：adi_ip_create / adi_ip_files / adi_ip_properties

#### 4.1.1 概念说明

把 Verilog 打包成 IP，可以归纳为一个固定的「三步走」：

1. **建工程**：`adi_ip_create` 创建一个空的 Vivado 工程，并把整个 ADI library 目录注册成 IP 仓库，让本工程能「看到」并引用其他 ADI 子 IP；顺带做一次工具版本校验。
2. **加文件**：`adi_ip_files` 把 `.v/.vhd` 倒进综合源文件集、把 `.xdc` 倒进约束文件集，并声明顶层模块名。
3. **写身份证**：`adi_ip_properties` 调用 `ipx::package_project` 把工程「升格」为 IP，并自动推断出每个 ADI IP 都必备的 AXI4-Lite 寄存器从接口（`s_axi`）及其时钟、复位与地址映射。

这三个过程是**所有** Xilinx 库 `*_ip.tcl` 的固定起手式——你会在几乎每一个 `*_ip.tcl` 的开头看到一模一样的三行。理解了它们，就理解了 80% 的打包套路。

#### 4.1.2 核心流程

```
adi_ip_create <name>          ;# ① 版本校验 → create_project → 注册 IP 仓库
adi_ip_files <name> {files}   ;# ② 按 .xdc/.v 分流进 constrs_1 / sources_1，设 top
adi_ip_properties <name>      ;# ③ package_project → 清空旧接口 → 推断 s_axi + 建地址映射
```

注意第 ③ 步里有一个「清空」动作：`adi_ip_properties_lite` 会先 `ipx::remove_all_bus_interface`、移除内存映射，把工程自动推断出的乱七八糟的接口全部抹掉，**给后面手动/半自动声明接口留出干净的白纸**。这是 ADI 打包风格的关键——接口不由 Vivado 乱猜，而由 `*_ip.tcl` 精确控制。

#### 4.1.3 源码精读

**① `adi_ip_create` —— 建工程 + 版本校验 + 注册仓库**

[library/scripts/adi_ip_xilinx.tcl:L258-L302](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_xilinx.tcl#L258-L302)

关键三段：

```tcl
# 版本硬校验（承自 u1-l3 的 adi_env.tcl）
set VIVADO_VERSION [version -short]
if {[string compare $VIVADO_VERSION $required_vivado_version] != 0} {
    ... ERROR ... ; exit 2     ;# 设了 IGNORE_VERSION_CHECK 则降级为 CRITICAL WARNING
}
create_project $ip_name . -force
...
set_property ip_repo_paths $lib_dirs [current_fileset]
update_ip_catalog
```

- 版本校验读的是全局变量 `required_vivado_version` 与 `IGNORE_VERSION_CHECK`（由 `adi_env.tcl` 提供），不匹配直接 `exit 2`。这就是 u1-l3 所说「版本比对与拦截真正发生在打包脚本里」的落点。
- `set_property ip_repo_paths` + `update_ip_catalog` 把 `$ad_hdl_dir/library`（必要时加上 `$ad_ghdl_dir/library`）登记为 IP 仓库目录。**这一步至关重要**：它让正在打包的 axi_dmac 工程能够识别并实例化它依赖的子 IP（如 `util_axis_fifo`、`util_cdc`）。没有这步，后面声明子核依赖会找不到。

**② `adi_ip_files` —— 按扩展名分流**

[library/scripts/adi_ip_xilinx.tcl:L309-L327](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_xilinx.tcl#L309-L327)

```tcl
foreach m_file $ip_files {
  if {[file extension $m_file] eq ".xdc"} {
    lappend constraint_files $m_file      ;# .xdc 进 constrs_1
  } else {
    lappend design_source_files $m_file   ;# 其余进 sources_1
  }
}
...
set_property "top" "$ip_name" $proj_fileset
```

它只做一件事：把传进来的文件列表按后缀分流，并设顶层。注意 axi_dmac 在调用时把 `bd/bd.tcl` 也塞进了这个列表（见 [axi_dmac_ip.tcl:L47](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L47)），紧接着又用两行把它排除在仿真与综合之外：

```tcl
set_property used_in_simulation false [get_files ./bd/bd.tcl]
set_property used_in_synthesis  false [get_files ./bd/bd.tcl]
```
（[axi_dmac_ip.tcl:L49-L50](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L49-L50)）

原因见 4.3：`bd.tcl` 是块设计环境脚本，不该参与 IP 自身的综合/仿真。

**③ `adi_ip_properties` —— 真正「升格」为 IP**

先看其骨架 `adi_ip_properties_lite`：[library/scripts/adi_ip_xilinx.tcl:L333-L357](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_xilinx.tcl#L333-L357)

```tcl
ipx::package_project -root_dir . -vendor analog.com -library $VIVADO_IP_LIBRARY -taxonomy /Analog_Devices
set_property name $ip_name [ipx::current_core]
set_property AUTO_FAMILY_SUPPORT_LEVEL level_2 [ipx::current_core]
ipx::remove_all_bus_interface [ipx::current_core]   ;# 清空，留白纸
```

- `VIVADO_IP_LIBRARY` 默认为 `user`（[adi_ip_xilinx.tcl:L7-L11](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_xilinx.tcl#L7-L11)），故最终 VLNV 是 `analog.com:user:axi_dmac:1.0`；可用环境变量 `ADI_VIVADO_IP_LIBRARY` 覆盖（例如改成 `hdl`）。
- `AUTO_FAMILY_SUPPORT_LEVEL level_2` 表示允许该 IP 被用到它原生器件族以外的 AMD 器件族上（按需给出支持级别），而不是硬性锁死在某一族。

再看 `adi_ip_properties` 主体如何「免费」推断出寄存器接口：[library/scripts/adi_ip_xilinx.tcl:L363-L414](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_xilinx.tcl#L363-L414)

```tcl
ipx::infer_bus_interface { s_axi_awvalid ... s_axi_rready } \
  xilinx.com:interface:aximm_rtl:1.0 [ipx::current_core]   ;# 显式列出 s_axi 的全部信号 → AXI-Lite 从接口
ipx::infer_bus_interface s_axi_aclk    xilinx.com:signal:clock_rtl:1.0 ...
ipx::infer_bus_interface s_axi_aresetn xilinx.com:signal:reset_rtl:1.0 ...
...
ipx::add_memory_map {s_axi} [ipx::current_core]
set_property slave_memory_map_ref {s_axi} [ipx::get_bus_interfaces s_axi ...]
ipx::add_address_block {axi_lite} ...
set_property range $range ...                              ;# range 由地址线宽推算
```

它做了一件重要的事：**每个 ADI IP 都是 AXI4-Lite 从设备**（用于 CPU 读写寄存器，详见 [u4-l5](u4-l5-register-map-up-axi.md)）。所以 `adi_ip_properties` 把这件事固化成了通用步骤——只要你的 Verilog 里端口叫 `s_axi_*`，它就自动拼出一个 `s_axi` 接口并配上内存映射。地址段大小由 `s_axi_araddr`/`s_axi_awaddr` 的位宽推算：

\[ \text{range} = \begin{cases} 2^{W}, & W < 16 \\ 65536, & W \ge 16 \end{cases} \]

（`W` 为地址线位宽，见 [adi_ip_xilinx.tcl:L393-L405](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_xilinx.tcl#L393-L405)）

> 小结：`adi_ip_properties` 解决的是「寄存器通路」，而每个 IP 各不相同的数据通路（DMA 的 AXI-MM 主口、AXI-Stream、FIFO 口）则留给 4.2 的接口声明原语去逐个描述。

#### 4.1.4 代码实践

**实践目标**：确认「三步走」在真实样本里的位置与顺序。

**操作步骤**：

1. 打开 [library/axi_dmac/axi_dmac_ip.tcl:L12-L52](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L12-L52)。
2. 定位三行：`adi_ip_create axi_dmac`（L12）、`adi_ip_files axi_dmac [...]`（L13）、`adi_ip_properties axi_dmac`（L52）。
3. 再任选另一个库，例如 `library/util_axis_fifo/util_axis_fifo_ip.tcl`，比对它的开头是否也是同样的三行。

**需要观察的现象**：三个过程出现的先后顺序固定为 create → files → properties；`adi_ip_files` 与 `adi_ip_properties` 之间隔着 L49-L50 的两行 `set_property used_in_*`。

**预期结果**：你会看到 `util_axis_fifo_ip.tcl` 同样以 `adi_ip_create` → `adi_ip_files` → `adi_ip_properties` 起手，验证这是全库通用模板。

> 待本地验证：若你装有指定版本的 Vivado，可在 `library/axi_dmac/` 下执行 `make xilinx`，观察 `axi_dmac_ip.log` 中 `create_project`、`package_project` 的出现顺序。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `adi_ip_create` 里要 `set_property ip_repo_paths`？如果删掉这行，后面 `adi_ip_add_core_dependencies` 声明的 `util_axis_fifo` 依赖会怎样？

> **答案**：这行把 ADI library 目录注册为 IP 仓库，使工程能在 IP Catalog 中找到子 IP。删掉后，打包时无法解析对 `util_axis_fifo`、`util_cdc` 的子核引用，`update_ip_catalog` 之后这些 VLNV 不可达，打包会报找不到 subcore 的错误。

**练习 2**：`adi_ip_properties` 为什么要先 `ipx::remove_all_bus_interface` 再重新推断 `s_axi`？

> **答案**：`package_project` 会按端口命名自动猜出一堆接口，其中可能包含不完整或错误的捆绑。ADI 的风格是先清空成白纸，再由 `*_ip.tcl` 用 4.2 的原语精确、可控地声明每一个接口，避免 Vivado 误猜污染 `component.xml`。

---

### 4.2 接口与总线的自动推断与显式声明

#### 4.2.1 概念说明

一个 IP 对外的「接口」描述清楚后，才能在块设计里被整根连线。ADI 提供两套互补的机制：

- **自动推断（infer）**：端口语义靠「命名约定」让 Vivado 自己分组。省事，但要求端口名符合标准（如 AXI-Stream 的 `*_tvalid`/`*_tdata`）。
- **显式声明（add_bus）**：逐个列出「物理端口 ↔ 逻辑信号」的映射表。可控，适合命名不标准或自定义接口。

此外还有三类辅助原语：`adi_add_bus_clock`（给接口配时钟/复位）、`adi_set_bus_dependency`/`adi_set_ports_dependency`（按参数值控制接口/端口的显隐）、`adi_if_define`/`adi_if_infer_bus`（定义并实例化自定义总线类型）。

#### 4.2.2 核心流程

axi_dmac 同时用到了两套机制，分工如下：

```
# —— 自动推断 ——（命名标准，让 Vivado 猜）
adi_ip_infer_mm_interfaces     → m_src_axi / m_dest_axi / m_sg_axi  (AXI-MM 主口)

# —— 显式声明 ——（命名不标准或自定义，手写映射表）
adi_add_bus  s_axis  slave  axis  {s_axis_ready→TREADY, s_axis_valid→TVALID, ...}
adi_add_bus  m_axis  master axis  {...}
adi_add_bus  fifo_wr slave  fifo  {fifo_wr_en→EN, fifo_wr_din→DATA, ...}  ;# ADI 自定义接口

# —— 配套：时钟/复位、显隐依赖、自定义接口 ——
adi_add_bus_clock "s_axis_aclk" "s_axis"
adi_set_bus_dependency "m_src_axi" ... "(DMA_TYPE_SRC) = 0"
adi_if_infer_bus analog.com:interface:if_framelock master m_framelock ...
```

一个关键观察：**axi_dmac 的 AXI-Stream 接口必须手写声明**。因为它的 Verilog 端口叫 `s_axis_valid`/`s_axis_data`（不带 `t`），不符合 Xilinx 的 `_tvalid`/`_tdata` 约定，自动推断抓不到，所以 L68-L94 用 `adi_add_bus` 显式列出 9 对映射。

#### 4.2.3 源码精读

**A. 自动推断的两个过程**

[library/scripts/adi_ip_xilinx.tcl:L64-L82](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_xilinx.tcl#L64-L82)

```tcl
proc adi_ip_infer_streaming_interfaces {ip_name} {
  ipx::infer_bus_interfaces xilinx.com:interface:axis_rtl:1.0 [ipx::current_core]
}
proc adi_ip_infer_mm_interfaces {ip_name} {
  ipx::infer_bus_interfaces xilinx.com:interface:aximm_rtl:1.0 [ipx::current_core]
}
```

它们是对 `ipx::infer_bus_interfaces` 的薄封装：Vivado 扫描全部端口，按指定总线类型把命名匹配的端口归并成接口。axi_dmac 在 [axi_dmac_ip.tcl:L53](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L53) 调 `adi_ip_infer_mm_interfaces`，于是三个名为 `m_src_axi`/`m_dest_axi`/`m_sg_axi` 的 AXI-MM 主接口被「免费」识别出来（因为 Verilog 端口是标准的 `m_src_axi_awvalid` 等）。

**B. 显式声明：`adi_add_bus` 与 `adi_add_port_map`**

[library/scripts/adi_ip_xilinx.tcl:L113-L149](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_xilinx.tcl#L113-L149)

```tcl
proc adi_add_bus {bus_name mode abs_type bus_type port_maps} {
  set bus [ipx::add_bus_interface $bus_name [ipx::current_core]]
  set_property "ABSTRACTION_TYPE_VLNV" $abs_type $bus   ;# 抽象类型(rtl)
  set_property "BUS_TYPE_VLNV"         $bus_type $bus   ;# 总线类型
  set_property "INTERFACE_MODE"        $mode    $bus    ;# master/slave
  foreach port_map $port_maps { adi_add_port_map $bus {*}$port_map }
}
```

四个参数的含义：`mode`（master/slave）、`abs_type`（抽象层 VLNV，定义信号逻辑名如 `TVALID`）、`bus_type`（总线族 VLNV）、`port_maps`（物理端口名 ↔ 逻辑信号名的映射表）。

看 axi_dmac 如何用它声明 `s_axis`：[axi_dmac_ip.tcl:L68-L80](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L68-L80)

```tcl
adi_add_bus "s_axis" "slave" \
  "xilinx.com:interface:axis_rtl:1.0" \
  "xilinx.com:interface:axis:1.0" \
  [list {"s_axis_ready" "TREADY"} {"s_axis_valid" "TVALID"} {"s_axis_data" "TDATA"} ...]
adi_add_bus_clock "s_axis_aclk" "s_axis"
```

每对 `{"物理端口" "逻辑名"}` 把一个真实端口挂到 AXI-Stream 标准的某个逻辑信号上。这样在块设计里，`s_axis` 就是一个可整根连线的从接口。

**C. 配时钟/复位：`adi_add_bus_clock`**

[library/scripts/adi_ip_xilinx.tcl:L205-L239](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_xilinx.tcl#L205-L239)

它创建一个 `clock_rtl` 信号接口，并用 `ASSOCIATED_BUSIF` 把它绑定到指定接口（支持冒号分隔多接口，如 `"s_axis:m_axis"`）。一个巧妙细节——复位极性靠**名字自动判断**：

```tcl
if {[string match {*[Nn]} $reset_signal_name] == 1} {
  set_property value "ACTIVE_LOW" $reset_polarity    ;# 名字以 n/N 结尾 → 低有效
} else {
  set_property value "ACTIVE_HIGH" $reset_polarity
}
```

所以 `s_axi_aresetn`（n 结尾）自动判为低有效，无需手写。这解释了为什么全库的复位名都规整地以 `n` 收尾。

**D. 显隐依赖：`adi_set_bus_dependency` / `adi_set_ports_dependency`**

axi_dmac 高度可参数化：源/目标端可以是 AXI-MM、AXI-Stream 或 FIFO 三选一（`DMA_TYPE_SRC`/`DMA_TYPE_DEST` = 0/1/2）。IP 描述必须告诉 Vivado「参数取某值时，哪些接口/端口该出现」。

[axi_dmac_ip.tcl:L96-L105](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L96-L105)

```tcl
adi_set_bus_dependency "m_src_axi" "m_src_axi" \
  "(spirit:decode(id('MODELPARAM_VALUE.DMA_TYPE_SRC')) = 0)"   ;# MM 时才显示 m_src_axi
adi_set_bus_dependency "s_axis" "s_axis" \
  "(spirit:decode(id('MODELPARAM_VALUE.DMA_TYPE_SRC')) = 1)"   ;# Stream 时才显示 s_axis
```

`spirit:decode(id('MODELPARAM_VALUE.X'))` 是 IP-XACT 标准表达式，引用参数 `X` 解析后的值。当 `DMA_TYPE_SRC=0`（MM）时 `m_src_axi` 可见、`s_axis` 隐藏；`=1`（Stream）时反过来。这就实现了「同一份 RTL，按参数动态呈现不同接口面板」。

底层实现：[adi_ip_xilinx.tcl:L93-L111](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_xilinx.tcl#L93-L111)，`adi_set_ports_dependency` 用 `port_prefix*` 通配给一组端口批量设 `ENABLEMENT_DEPENDENCY`。

**E. 自定义接口：`adi_if_define` / `adi_if_ports` / `adi_if_infer_bus`**

当 Xilinx 标准接口不够用时（如 ADI 的 `fifo_wr`/`fifo_rd`/`if_framelock`），先用 `adi_if_define` **创造**一个新总线类型（生成抽象/总线定义 XML），再用 `adi_if_infer_bus` 把它**实例化**到 IP 上。

[adi_ip_xilinx.tcl:L583-L594](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_xilinx.tcl#L583-L594)（定义）与 [L642-L656](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_xilinx.tcl#L642-L656)（实例化）。

axi_dmac 用它声明 framelock 接口：[axi_dmac_ip.tcl:L247-L252](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L247-L252)

```tcl
adi_if_infer_bus analog.com:interface:if_framelock master m_framelock [list \
  "s2m_framelock       m_frame_in" \
  "s2m_framelock_valid m_frame_in_valid" ...]
```

注意 `fifo_wr`/`fifo_rd` 走的是 `adi_add_bus`（[L204-L232](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L204-L232)），因为这些总线类型的 XML 定义文件已在 Makefile 的 `XILINX_DEPS` 里声明（见 [Makefile:L46-L51](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L46-L51)），无需在脚本里现定义。

#### 4.2.4 代码实践

**实践目标**：验证「自动推断 vs 显式声明」的分工，并理解显隐依赖。

**操作步骤**：

1. 在 [axi_dmac_ip.tcl:L53](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L53) 看到 `adi_ip_infer_mm_interfaces`——它没有列出任何端口名。打开 [library/axi_dmac/axi_dmac.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac.v)，搜索 `m_src_axi_awvalid`，确认该端口名符合 AXI-MM 命名约定。
2. 对比 [L68-L94](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L68-L94) 的 `s_axis`/`m_axis`——它们用了显式 `adi_add_bus`。再到 `axi_dmac.v` 搜 `s_axis_valid`，确认它不带 `t` 前缀。
3. 读 [L96-L105](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L96-L105) 的六条 `adi_set_bus_dependency`，填写下表。

**需要观察的现象 / 预期结果**：完成下表（参数取值与可见接口的对应）：

| 参数条件 | 可见接口 |
|----------|----------|
| `DMA_TYPE_SRC = 0` | `m_src_axi` |
| `DMA_TYPE_SRC = 1` | `s_axis` |
| `DMA_TYPE_SRC = 2` | `fifo_wr` |
| `DMA_SG_TRANSFER = 1` | `m_sg_axi` |

> 待本地验证：若在 Vivado GUI 中打开打包好的 axi_dmac，切换 `DMA_TYPE_SRC` 参数，应能看到源端接口在 MM/Stream/FIFO 三种形态间切换。

#### 4.2.5 小练习与答案

**练习 1**：假如把 axi_dmac 的 Verilog 端口 `s_axis_valid` 改名为 `s_axis_tvalid`（其余同步改），`*_ip.tcl` 里的 `adi_add_bus "s_axis"` 还必须手写吗？

> **答案**：可以不手写。改名后符合 AXI-Stream 命名约定，改用 `adi_ip_infer_streaming_interfaces` 即可让 Vivado 自动推断出 `s_axis`。这正是自动推断与显式声明的取舍：命名标准 → 推断；命名自定义 → 手写。

**练习 2**：`adi_add_bus_clock "s_axis_aclk" "s_axis"` 这一行为什么必要？省掉会怎样？

> **答案**：它把时钟（及隐含的复位）信号接口与 `s_axis` 绑定（`ASSOCIATED_BUSIF`）。省掉后，Vivado 不知道 `s_axis` 由哪个时钟驱动，块设计里无法自动连时钟、时序分析也无法正确分组，综合会报接口缺时钟关联的警告/错误。

---

### 4.3 bd.tcl 与 ttcl 约束的关联

#### 4.3.1 概念说明

`*_ip.tcl` 在打包时还做了一件容易被忽略的事：**注册**两个「运行时资产」——块设计钩子 `bd.tcl` 与参数化约束模板 `*.ttcl`。要点在于：

- 注册（`adi_ip_bd` / `adi_ip_ttcl`）发生在**打包时**，只是把文件挂进 `component.xml` 的特定文件组。
- 这两个文件的**真正逻辑**分别在**块设计时**和 **IP 综合时**才执行，且二者通过 `ASYNC_CLK_*` 参数形成一个反馈环。

理解这一点，就能回答本讲实践任务的核心问题：`adi_ip_bd`（注册过程）与 `bd/bd.tcl`（钩子脚本）是两个不同时间点、不同性质的东西。

#### 4.3.2 核心流程

```
┌─────────────── 打包时（make xilinx → vivado *_ip.tcl）───────────────┐
│  adi_ip_ttcl  name "x_constr.ttcl"   → 挂进 synthesis 文件组(type=ttcl) │
│  adi_ip_bd     name "bd/bd.tcl"      → 挂进 blockdiagram 文件组(tclSource)│
│  （两者都只是登记，不执行钩子/模板逻辑）                                  │
└──────────────────────────────────────────────────────────────────────┘
            │ component.xml 产出后，IP 被别人拖进块设计
            ▼
┌─────────────── 块设计时（消费方工程）───────────────┐
│  Vivado 自动调用 bd/bd.tcl 的回调：                  │
│   init → post_config_ip → propagate → post_propagate│
│  propagate：检测时钟域拓扑 → 写 ASYNC_CLK_* 参数      │
└──────────────────────────────────────────────────────┘
            │ 用户对该 IP 跑综合
            ▼
┌─────────────── IP 综合时 ───────────────┐
│  Vivado 求值 x_constr.ttcl → 生成 x_constr.xdc │
│  ttcl 读取 ASYNC_CLK_* → 生成对应的 CDC 约束     │
└─────────────────────────────────────────────┘
```

反馈环：**bd.tcl 的 `propagate` 自动判定哪些时钟域彼此异步，把结果写进 `ASYNC_CLK_*` 参数；ttcl 读这些参数，仅为异步域生成跨时钟域（CDC）约束。** 二者一检一用，配套出现。

#### 4.3.3 源码精读

**A. `adi_ip_bd` —— 注册块设计钩子**

[library/scripts/adi_ip_xilinx.tcl:L45-L62](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_xilinx.tcl#L45-L62)

```tcl
proc adi_ip_bd {ip_name ip_bd_files} {
  set proj_filegroup [ipx::get_file_groups xilinx_blockdiagram -of_objects [ipx::current_core]]
  if {$proj_filegroup == {}} {
    set proj_filegroup [ipx::add_file_group -type xilinx_blockdiagram "" [ipx::current_core]]
  }
  foreach file $ip_bd_files {
    set f [ipx::add_file $file $proj_filegroup]
    set_property -dict [list type tclSource] $f     ;# 仅登记，type=tclSource
  }
}
```

它只是把 `bd.tcl` 放进 `xilinx_blockdiagram` 文件组。**它不执行 `bd.tcl` 里的任何 proc**——那是消费方工程在块设计时才做的事。axi_dmac 的调用见 [axi_dmac_ip.tcl:L56](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L56)。

**B. `bd/bd.tcl` —— 真正的块设计钩子**

[library/axi_dmac/bd/bd.tcl:L6-L44](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/bd/bd.tcl#L6-L44) 定义四个标准回调，Vivado 在 BD 生命周期的不同时刻自动调用：

| 回调 | 触发时机 | axi_dmac 里做的事 |
|------|----------|-------------------|
| `init` | IP 刚放入/参数初始化 | 标记参数为可覆盖传播；按器件族设默认 AXI 协议（Zynq-7000 → AXI3，其余 → AXI4）；新版本 Vivado 开 `ALLOW_ASYM_MEM` |
| `post_config_ip` | 用户改完参数后 | 按配置重算 AXI 接口属性（`PROTOCOL`、`MAX_BURST_LENGTH`、读写 outstanding 数） |
| `propagate` | 连线变化时 | **检测各时钟域对的同步/异步关系，写 `ASYNC_CLK_*` 参数**（L143-L177） |
| `post_propagate` | 传播完成 | 据连接的地址段算 `DMA_AXI_ADDR_WIDTH`；按 `CACHE_COHERENT` 设 `AXCACHE`/`AXPROT` |

其中 `propagate` 是反馈环的源头。看它的异步判定核心：[bd/bd.tcl:L111-L141](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/bd/bd.tcl#L111-L141)

```tcl
# 仅当能确证同步时才标同步，否则一律按异步处理（保守）
if {$clk_domain_a == $clk_domain_b && $clk_freq_a == $clk_freq_b && $clk_phase_a == $clk_phase_b} {
  set clk_async 0
} else {
  set clk_async 1
}
set_property "CONFIG.$param_name" $clk_async $ip   ;# 写回 ASYNC_CLK_*
```

它比较两个时钟的 `CLK_DOMAIN`/`FREQ_HZ`/`PHASE`，只有三者全同才判同步，否则判异步。结果写进 `ASYNC_CLK_REQ_SRC` 等参数——正是 ttcl 要读的。

**C. `adi_ip_ttcl` —— 注册参数化约束模板**

[library/scripts/adi_ip_xilinx.tcl:L13-L28](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_xilinx.tcl#L13-L28)

```tcl
proc adi_ip_ttcl {ip_name ip_constr_files} {
  set proj_filegroup [ipx::get_file_groups -of_objects [ipx::current_core] -filter {NAME =~ *synthesis*}]
  set f [ipx::add_file $ip_constr_files $proj_filegroup]
  set_property -dict [list type ttcl] $f          ;# 挂进 synthesis 文件组，type=ttcl
  ipx::reorder_files -front $ip_constr_files $proj_filegroup
}
```

axi_dmac 的调用：[axi_dmac_ip.tcl:L54](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L54) 注册 `axi_dmac_constr.ttcl`。

**D. `axi_dmac_constr.ttcl` —— 读参数、吐约束**

普通 `.xdc` 不支持 `if`，无法写「参数相关」的约束。ttcl 是 Tcl 模板，用 `<: ... :>` 标签嵌入 Tcl 逻辑，综合时被求值生成真正的 `.xdc`。看它如何读 bd.tcl 写下的参数：[axi_dmac_constr.ttcl:L11-L16](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_constr.ttcl#L11-L16)

```tcl
<: set async_dest_req [getBooleanValue "ASYNC_CLK_DEST_REQ"] :>
<: set async_req_src  [getBooleanValue "ASYNC_CLK_REQ_SRC"]  :>
<: set async_src_dest [getBooleanValue "ASYNC_CLK_SRC_DEST"] :>
...
```

随后仅当某对域被判异步时，才生成对应的 CDC 约束，例如（[L60-L65](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_constr.ttcl#L60-L65)）：

```tcl
<: if {$async_req_src || $async_src_dest || $async_dest_req || ...} { :>
set_property ASYNC_REG TRUE \
  [get_cells -quiet -hier *cdc_sync_stage1_reg*] \
  [get_cells -quiet -hier *cdc_sync_stage2_reg*]
<: } :>
```

接着为每个异步域对生成 `set_max_delay -datapath_only`（约束同步器路径延迟不超过源时钟周期）与 `set_false_path`（切断某些状态/控制路径）。这些正是 axi_dmac 内部跨时钟域同步器（`cdc_sync_stage1/2_reg`）赖以通过时序收敛的约束。

> 一句话总结本节：`adi_ip_bd` 与 `adi_ip_ttcl` 是「登记处」，`bd/bd.tcl` 与 `*.ttcl` 是「干活的」；前者打包时登记，后者分别在块设计时与综合时干活，且 bd.tcl 的检测结果喂给 ttcl 当输入。

#### 4.3.4 代码实践

**实践目标**：亲手验证「登记 vs 执行」的时间差，并理清 `adi_ip_bd` 与 `bd/bd.tcl` 的分工。

**操作步骤**：

1. 在 [axi_dmac_ip.tcl:L49-L56](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L49-L56) 找到三行：
   - L49-L50：把 `bd/bd.tcl` 标记为不参与仿真/综合；
   - L54：`adi_ip_ttcl`，登记约束模板；
   - L56：`adi_ip_bd`，登记块设计钩子。
2. 打开 [bd/bd.tcl:L6](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/bd/bd.tcl#L6)，确认 `init` 是一个 Tcl `proc`，参数为 `cellpath otherInfo`——这是 Vivado BD 回调的标准签名。
3. 追踪反馈环：`bd.tcl` 的 `propagate`（[L143](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/bd/bd.tcl#L143)）写 `ASYNC_CLK_*` → `axi_dmac_constr.ttcl` 的 [L11-L16](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_constr.ttcl#L11-L16) 读 `ASYNC_CLK_*`。

**需要观察的现象**：`adi_ip_bd` 的函数体里没有任何 `proc init`、`proc propagate` 之类的定义，只有 `ipx::add_file`；而 `bd/bd.tcl` 里恰好定义了这些 proc。

**预期结果**：你能用一句话回答实践任务——

- **`adi_ip_bd`**：打包时执行的注册过程，把 `bd.tcl` 作为 `tclSource` 登记进 IP 的 `xilinx_blockdiagram` 文件组，使 `component.xml` 知道「这个 IP 自带一个块设计钩子脚本」。
- **`bd/bd.tcl`**：消费方工程把该 IP 拖进块设计时，由 Vivado 自动调用其 `init`/`post_config_ip`/`propagate`/`post_propagate` 回调，完成参数默认值设定、AXI 接口属性派生、时钟域异步检测与地址位宽推算。

> 待本地验证：若打包后在 Vivado 中对 axi_dmac 跑 `report_property -regexp CONFIG.ASYNC_CLK_*`，再改连接的时钟源，应能看到这些参数被 `propagate` 自动改写。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `axi_dmac_ip.tcl` 要在 L49-L50 把 `bd/bd.tcl` 设为 `used_in_synthesis false` / `used_in_simulation false`？

> **答案**：`bd.tcl` 的回调只在块设计环境（BD）里有意义，它操作的是 BD 单元（`get_bd_cells`）而非 RTL。如果让它参与 IP 自身的综合或仿真，既无对应的 BD 上下文会报错，又会把 BD 专用 Tcl 误当成源码/约束。所以仅限块设计使用。

**练习 2**：如果用户在块设计里把 axi_dmac 的所有时钟都连到同一个时钟源，`propagate` 会把 `ASYNC_CLK_*` 设成什么？ttcl 会因此生成还是省略 CDC 约束？

> **答案**：所有时钟同域同频同相，`propagate` 判为同步，`ASYNC_CLK_*` 全部置 0。ttcl 中 `if {$async_req_src || ...}` 条件为假，于是省略绝大部分 `set_max_delay`/`set_false_path` 的 CDC 约束——因为同步域间不需要跨域约束。这正是参数化约束相对于静态 xdc 的价值：按实际拓扑「按需生成」。

---

## 5. 综合实践

**任务**：以 axi_dmac 为对象，画出「一个 IP 从源码到块设计里可复用」的完整生命周期，并把本讲三个模块串起来。

请完成以下四步，产出一份一页笔记：

1. **打包流水线**（对应 4.1）：从 [axi_dmac_ip.tcl:L12](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L12) 的 `adi_ip_create` 到 [L691](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L691) 的 `ipx::save_core`，按时间顺序列出 `adi_ip_create` 之后依次调用的关键打包原语（至少列出 8 个，标注每个的行号）。

2. **接口清单**（对应 4.2）：列出 axi_dmac 最终打包出的全部对外接口，分三列写明：「接口名 / master·slave / 是自动推断还是显式声明」。提示：包括 `s_axi`、三个 `m_*_axi`、`s_axis`、`m_axis`、`fifo_wr`、`fifo_rd`、`m_framelock`、`s_framelock`、`irq`。

3. **运行时反馈环**（对应 4.3）：画一张简图，标出 `bd/bd.tcl` 的 `propagate` 写 `ASYNC_CLK_*`、`axi_dmac_constr.ttcl` 读 `ASYNC_CLK_*` 生成 `.xdc` 的因果链，并注明这两个文件分别由哪个 `adi_*` 原语登记、在什么时间点执行。

4. **预测验证**：把 `DMA_TYPE_SRC` 设为 `2`（FIFO），预测 `m_src_axi`、`s_axis`、`fifo_wr` 三个接口哪些可见、哪些隐藏，依据是 [L96-L105](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L96-L105) 与 [L216-L217](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L216-L217) 的依赖表达式。

**参考要点**（步骤 4）：`DMA_TYPE_SRC=2` 时，`m_src_axi` 的条件 `= 0` 不成立 → 隐藏；`s_axis` 的条件 `= 1` 不成立 → 隐藏；`fifo_wr` 的条件 `= 2` 成立 → 可见。这与「源端选 FIFO 模式」的语义完全吻合。

## 6. 本讲小结

- 打包有固定「三步走」：`adi_ip_create`（建工程 + 版本校验 + 注册 IP 仓库）→ `adi_ip_files`（按后缀分流源码与约束、设顶层）→ `adi_ip_properties`（`package_project` 升格为 IP，并固化推断 AXI4-Lite 寄存器从接口 `s_axi` 与地址映射）。
- 接口描述有两条路：命名标准时用 `adi_ip_infer_mm_interfaces`/`adi_ip_infer_streaming_interfaces` 让 Vivado 自动推断；命名不标准或为自定义总线时用 `adi_add_bus` + `adi_add_bus_clock` 显式声明，用 `adi_set_bus_dependency` 按参数值控制接口显隐。
- `adi_add_bus_clock` 会据复位名是否以 `n`/`N` 结尾自动判定 `ACTIVE_LOW`/`ACTIVE_HIGH`，这就是全库复位名规整收尾于 `n` 的原因。
- `adi_if_define`/`adi_if_infer_bus` 用于创造并实例化 Xilinx 标准之外的自定义总线类型（如 `fifo_wr`、`if_framelock`）。
- `adi_ip_bd` 与 `adi_ip_ttcl` 只是「登记处」：打包时把 `bd.tcl`（块设计钩子）和 `*.ttcl`（参数化约束模板）挂进 `component.xml`；二者真正执行分别发生在块设计时（`init`/`propagate` 等回调）与 IP 综合时（ttcl 求值生成 xdc）。
- bd.tcl 的 `propagate` 自动判定时钟域异步关系并写 `ASYNC_CLK_*`，ttcl 读这些参数按需生成 CDC 约束——二者构成反馈环，使同一份 IP 能自适应不同的时钟连接拓扑。

## 7. 下一步学习建议

- **继续向下读数据通路**：本讲把 axi_dmac「打包好了」，[u5-l1](u5-l1-axi-dmac.md) 将拆开它的 RTL，讲 `data_mover`、src/dest 通道与 2D/SG 传输等内部架构。
- **对比另两家工具链**：[u4-l3](u4-l3-intel-lattice-ip-packaging.md) 会对照 Intel 的 `*_hw.tcl`（`adi_ip_intel.tcl`）与 Lattice 的 `*_ltt.tcl`，看同一份 RTL 在三家工具下打包方式的异同。
- **读寄存器侧**：`adi_ip_properties` 推断出的 `s_axi` 接口连到的是寄存器映射，[u4-l5](u4-l5-register-map-up-axi.md) 讲 `up_axi.v` 与 `*_regmap.v` 如何把 AXI4-Lite 转成寄存器读写。
- **动手建议**：挑一个结构简单的库（如 `library/util_axis_fifo/`），通读它的 `util_axis_fifo_ip.tcl`，用本讲的「三步走 + 接口声明 + 钩子登记」框架去复述它的打包过程，检验你是否真的掌握了这套套路。
