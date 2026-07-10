# 寄存器规格与 RDL/Ordt 生成

## 1. 本讲目标

本讲回答一个贯穿配置子系统（单元 2）的问题：那几十个引擎里、动辄上百个寄存器的 `*_reg.v` 文件，到底是从哪里来的？是手写的吗？

学完本讲，你应当能够：

- 说清 **SystemRDL** 是什么、为什么它是寄存器空间的「单一可信源（single source of truth）」。
- 读懂 `spec/manual/` 下的 `test.rdl`（RDL 源）、`test.parms`（生成参数）、`Makefile`（编排）三者如何驱动 `Ordt.jar` 一次性吐出 Verilog / SystemVerilog / UVM-RAL / C++ 多套后端模型。
- 拿真实生成的 `NV_NVDLA_GLB_CSB_reg.v` 做精读，看懂 Ordt 产出的 RTL 寄存器文件那套固定的「四段式模板」，并把第 4 模块讲过的中断字段（mask/set/status）的位布局逐一对应回源码。
- 诚实地分辨一件事：仓库里自带的 `test.rdl` 是 Ordt 的**演示样例**，并不直接生成 NVDLA 的生产寄存器；真正生成 19 个 `*_reg.v` 的那份 RDL 没有随仓库开源。理解「机制」与「数据」的边界，是本讲最重要的工程素养。

## 2. 前置知识

在进入源码前，先用大白话对齐几个概念：

- **寄存器（register）**：CPU 读写硬件的一扇扇「小窗」。每个窗有一个地址（offset）、若干位（field）。CPU 往窗里写值来配置引擎、读窗里的值来观测状态。本仓库用 32 位寄存器、字对齐地址，详见 u2-l1 / u2-l3。
- **单一可信源（single source of truth）**：一份东西如果既要给 RTL 用、又要给验证平台的 RAL（Register Abstraction Layer）用、还要给 C 参考模型（cmod）用，最怕三处各写一份、彼此漂移。正确做法是**只维护一份规格**，再由工具自动派生出各语言版本——这就是本讲的主题。
- **SystemRDL**：一种 IEEE 标准（IEEE 1685-2014）的寄存器描述语言，专门用来声明「有哪些寄存器、每个寄存器有哪些位、每位的读写权限、是否计数器、是否触发中断」等元信息。它是声明式的，本身不可综合。
- **Ordt**：一个开源的「Open Register Description Tool」，读入 SystemRDL，按命令行开关生成多种后端（Verilog、SystemVerilog、UVM RAL、C++、HTML 文档等）。本仓库随附的 `Ordt.jar` 就是它的可执行 jar 包。
- **RAL（Register Abstraction Layer）**：UVM 验证方法论里用 SystemVerilog 类把寄存器抽象成对象，方便验证平台用高层 API（`.read()/.write()/.peek()/.poke()`）驱动和检查寄存器，而不是裸发总线事务。
- **影子/影偶（shadow / dual group）**：见 u2-l3。一个操作寄存器被例化两份（d0/d1）轮换使用，CPU 写 producer 组、引擎用 consumer 组，实现不停顿配置切换。

本讲承接 **u2-l3（寄存器文件与影偶配置机制）**——那里讲的是「自动生成的 `*_reg.v` 长什么样、regfile 如何调度影偶」，本讲讲的是「这些 `*_reg.v` 究竟由什么、怎么生成出来的」。建议同时回忆 **u2-l4（GLB 全局配置与中断聚合）**，因为本讲的精读对象正是 GLB 的中断寄存器文件。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `spec/manual/README.md` | 一句话说明：装 Java、设 `JAVA`、`make` 即可生成 | 确认生成前提与四类后端 |
| `spec/manual/test.rdl` | **SystemRDL 演示源**（estats/erdr/simple1，含计数器/中断/宽寄存器/级联） | 学 RDL 的字段语义，看它如何对应到生成的 RTL |
| `spec/manual/test.parms` | 生成参数（各后端的开关与位宽等） | 理解 Ordt 的「输入参数 / 输出参数」模型 |
| `spec/manual/Makefile` | 编排：调 `java -jar Ordt.jar …` 一次产出多后端 | 学多后端命令行与输出目录 |
| `spec/manual/Ordt.jar` | Ordt 工具本体（约 1.1 MB 的 Java 可执行包） | 它是被调用的生成器，本讲不拆 jar 内部 |
| `vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v` | **真实生成的** GLB 寄存器文件（428 行） | 精读 Ordt 的 RTL 后端产物，对照中断字段 |
| `vmod/nvdla/glb/NV_NVDLA_GLB_csb.v` | GLB 的 CSB 适配层，例化 `u_reg` | 看 `_CSB_reg.v` 如何被接入引擎 |

一句话定位：`spec/manual/` 是「工具 + 演示」的打包，`vmod/nvdla/*/` 下那 19 个 `*_reg.v` 才是「用同一把工具、喂生产 RDL 后产出的真东西」。

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：(4.1) SystemRDL 源；(4.2) Ordt 多后端生成；(4.3) RTL 后端产物精读（连接 u2-l3）；(4.4) RAL 与 cmod 后端及「机制 vs 数据」的诚实边界。

---

### 4.1 SystemRDL 源：寄存器空间的单一可信源

#### 4.1.1 概念说明

SystemRDL 是一份**纯声明**的寄存器规格。它不描述时序、不描述算法，只回答四个问题：

1. 有哪些寄存器？它们在地址空间里如何排列（`@` 绝对地址、`%=` 相对地址、`+=` 步长）？
2. 每个寄存器多宽（`regwidth`，默认 32）？有哪些 field？每个 field 占哪些位（`[msb:lsb]`）？
3. 每个 field 的**访问权限**是什么？这是 RDL 最核心的语义——用 `sw=`（软件侧）和 `hw=`（硬件侧）两个维度组合，例如 `sw=rw; hw=r` 表示「CPU 可读可写、硬件只读」。
4. field 的**行为特性**：是否计数器（`counter`）、是否硬件可写使能（`we`）、读清（`rclr`）、溢出（`overflow`）、饱和（`saturate`）、触发中断（`intr`）、停机（`halt`）等。

为什么这是「单一可信源」？因为同一份 RDL 会被翻译成三处用途完全不同、但必须语义一致的产物：RTL 的寄存器文件（硬件实现）、验证平台的 RAL（高层驱动）、C 参考模型的寄存器类（软件模型）。任何一处手写都会漂移；只留 RDL 一份，由工具派生，就从根上消除了不一致。

#### 4.1.2 核心流程

一段 RDL 的组织层次是自顶向下的：

```
addrmap  (顶层地址图，整个寄存器空间的名字)
  └─ regfile (可选的中间分组)
       └─ reg (一个 32 位寄存器)
            └─ field (寄存器里的若干位段)
```

- `field { ... }` 先声明「字段类型」（权限 + 行为），可在多个寄存器里复用；
- `reg { ... } 名字` 把若干 field 装进一个寄存器，可带地址标注；
- `regfile` / `addrmap` 把寄存器组织成带地址的树；
- `external` 关键字声明「这个 regfile 的实现不在我这里，由别处提供」——后面会看到它和「to be implemented outside」的对应。

#### 4.1.3 源码精读

仓库自带的演示源 `spec/manual/test.rdl` 不是 NVDLA 的真寄存器，而是一份精心设计、几乎覆盖 Ordt 各类特性的「展示集」。先看三种基础字段类型：

字段类型声明：[spec/manual/test.rdl:7-20](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/manual/test.rdl#L7-L20)

```systemverilog
field swcfg_field { hw = r; sw = rw; desc = "SW Configuration field"; };  // CPU 可读可写，硬件只读
field hwsts_field { hw = w; sw = r;  desc = "HW Status field"; };          // 硬件写，CPU 只读
field hwsw_field  { hw = rw; sw = rw; desc = "HW/SW R/W field"; };         // 双向都可读写
```

这三行是理解生成行为的钥匙：`hw=r`（硬件读）的字段在 RTL 里是**寄存器输出**（CPU 写进去、硬件读出来）；`hw=w`（硬件写）的字段在 RTL 里是**模块输入**（硬件驱动、生成器不给它建触发器）。4.3 节会在 `GLB_CSB_reg.v` 里亲眼看到这条规则如何落地。

再看演示集里的「行为特性」字段——计数器与中断：

计数器/中断字段特性：[spec/manual/test.rdl:27-52](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/manual/test.rdl#L27-L52)

```systemverilog
field rollover_incr_32b_field { sw=rw; counter; we; fieldwidth=4; overflow; incrvalue=4'd1; reset=4'd0; };
field sat_incr_rclr_16b_field { rclr; counter; we; fieldwidth=16; saturate; incrvalue=16'd1; reset=16'd0; };
...
// 在 estats 里：带 intr / halt 的中断字段
field { sw=rw; intr;       hw=r; } l0_b0;
field { sw=rw; intr; halt; hw=r; } l0_b2;
```

这里出现了 `counter`、`we`（write-enable，硬件可写）、`rclr`（read-clear）、`overflow`/`saturate`、`intr`（中断）、`halt`（停机）。这些正是 NVDLA 中断寄存器（mask/set/status）背后用到的同类语义——`intr` 字段会被 Ordt 识别并参与中断聚合逻辑的派生。

最后看顶层地址图，它把三块东西摆进地址空间：

顶层 addrmap：[spec/manual/test.rdl:117-127](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/manual/test.rdl#L117-L127)

```systemverilog
addrmap {
    estats stats @0x1000;        // 绝对地址 0x1000 起
    erdr   rdr  @0x4000;         // 绝对地址 0x4000 起
    reg { name="extra_reg name"; swcfg_field value[31:0]; } extra_reg;
} simple1;
```

`@0x1000` / `@0x4000` 是绝对基地址；演示集里还出现 `%=0x10`（相对上一个寄存器偏移 0x10）、`+=0x80`（数组每元素步进 0x10、整个数组再步进 0x80）、`buffer[4]`（4 个寄存器的数组）。这些地址标注会被 Ordt 翻译成 RTL 里的地址译码比较值——4.3 节会看到 `GLB_CSB_reg.v` 用 `(32'h4 & 32'h00000fff)` 这样的比较来识别寄存器。

#### 4.1.4 代码实践

**实践目标**：把 `test.rdl` 当成一份「特性清单」，亲手给每个 RDL 特性找对应。

**操作步骤**：

1. 打开 [spec/manual/test.rdl](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/manual/test.rdl)。
2. 在文件里搜索这些关键字，各记一处出现的行号与所在寄存器：`counter`、`we`、`rclr`、`saturate`、`overflow`、`intr`、`halt`、`regwidth = 128`、`external`、`+=0x80`、`%=0x10`。
3. 重点看第 114 行的级联计数器：`rcnt_sat_log.count->incr = roll32_counter_reg.count->overflow;`——它声明「A 计数器的自增由 B 计数器的溢出事件驱动」。

**需要观察的现象**：你会注意到 `test.rdl` 几乎是一个「Ordt 能力体检表」，每种字段属性都摆了一个样本。这说明它的用途是**验证生成器本身**，而不是描述某个真实 IP。

**预期结果**：能口述「`hw=w` 的字段由硬件驱动、`hw=r` 的字段由 CPU 驱动」这一最关键区别。

**待本地验证**：RDL 的精确语法（如 `%=0x10` 与 `@0x10` 的差异）以 IEEE 1685 / Ordt 文档为准，本讲只对照仓库源码做语义解读。

#### 4.1.5 小练习与答案

**练习 1**：`swcfg_field` 的权限是 `hw=r; sw=rw`，`hwsts_field` 是 `hw=w; sw=r`。各自在生成的 RTL 里更可能是「触发器输出」还是「模块输入端口」？

> **答案**：`swcfg_field`（`hw=r`，硬件只读）→ CPU 写、硬件读 → 是触发器，RTL 里作为**输出端口**（flop 的 Q 端引出）。`hwsts_field`（`hw=w`，硬件写）→ 硬件驱动、CPU 只读 → 是**输入端口**，生成器不为之建触发器（4.3 节的「to be implemented outside」即此）。

**练习 2**：`addrmap simple1` 里 `estats stats @0x1000` 的 `@0x1000` 决定了什么？

> **答案**：`estats` 这组寄存器的**基地址**。它内部每个寄存器的最终地址 = `0x1000 + 该寄存器在 estats 内的偏移`。生成器据此推导每个 field 的绝对地址，进而写出地址译码逻辑。

---

### 4.2 Ordt 多后端生成

#### 4.2.1 概念说明

有了 RDL 源，还需要一个「翻译引擎」把它变成各语言产物——这就是 Ordt。它的核心设计是**一个解析器、多个后端（backend）**：同一次解析得到的内部寄存器模型，按命令行开关派生出不同语言/用途的输出。

本仓库涉及的四个主要后端：

| Ordt 开关 | 产物 | 用途 |
| --- | --- | --- |
| `-verilog` | `regs_v.v` | 可综合 RTL 寄存器文件（本讲重点） |
| `-systemverilog` | `sv/` 目录 | SystemVerilog 模型（带断言、覆盖率可选） |
| `-uvmregs` | `regs_ral.sv` | UVM RAL 类，供验证平台高层驱动 |
| `-cppmod` / `-cppdrvmod` | `cmod/` / `dmod/` | C++ 寄存器模型，供 cmod 参考模型与驱动 |

生成行为还可由一份 **parms 文件**（`-parms`）调节：它分「输入参数」（如 `resolve_reg_category`，让 Ordt 自动推断寄存器类别）和「各后端的输出参数」（如 systemverilog 后端的地址位宽、是否用门控时钟、是否生成覆盖率）。

#### 4.2.2 核心流程

```
test.rdl (RDL 源)  ─┐
test.parms (参数)  ─┼─▶  java -jar Ordt.jar  ─┬─▶  regs_v.v      (RTL)
                    │       (一次解析)         ├─▶  sv/           (SystemVerilog)
                    │                          ├─▶  regs_ral.sv   (RAL)
                    │                          ├─▶  cmod/         (C++ 模型)
                    │                          └─▶  dmod/         (C++ 驱动)
```

整个流程是**单进程、一次解析、多路派生**——这正是「单一可信源」在工程上的兑现：改一处 RDL，所有后端同步刷新。

#### 4.2.3 源码精读

先看编排这一切的 Makefile（注意第 8–9 行那条坦白的注释）：

Makefile 顶部的「FIXME」与变量定义：[spec/manual/Makefile:8-18](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/manual/Makefile#L8-L18)

```makefile
#====================================
# FIXME: change below test parameters and rdl input to real one
#====================================
REG_PARMS = test.parms
REG_RDL   = test.rdl

# several backends
REG_V = $(OUT_DIR)/regs_v.v
REG_S = $(OUT_DIR)/sv/
REG_U = $(OUT_DIR)/regs_ral.sv
REG_C = $(OUT_DIR)/cmod
REG_D = $(OUT_DIR)/dmod
```

第 9 行的 `FIXME` 是本讲「诚实边界」的铁证：仓库作者明确承认 `test.rdl` 只是测试参数，真正生成生产寄存器要用「real one」——而那份 real RDL 没有随仓库发布。

真正调用 Ordt 的命令行在默认目标里：

生成命令（一次产出四类后端）：[spec/manual/Makefile:19-21](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/manual/Makefile#L19-L21)

```makefile
default:
	@mkdir -p $(OUT_DIR)
	$(AT)$(JAVA) -jar Ordt.jar -parms $(REG_PARMS) \
	    -systemverilog $(REG_S) -verilog $(REG_V) \
	    -uvmregs $(REG_U) -cppmod $(REG_C) -cppdrvmod $(REG_D) $(REG_RDL)
```

读这条命令就能还原 Ordt 的用法：`-parms` 指参数文件；`-verilog/-systemverilog/-uvmregs/-cppmod/-cppdrvmod` 各跟一个输出路径，分别开启对应后端；最后位置的是 RDL 源文件。`$(JAVA)` 在模板 `tools/make/tree.make.vm` 里默认是 `/usr/bin/java`，README 提示需要 Java 1.7+ 并可在 `tools/make/tools.mk` 改。

参数文件 `test.parms` 则揭示了「输入/输出参数」的分层：

输入与 systemverilog 输出参数：[spec/manual/test.parms:2-14](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/manual/test.parms#L2-L14)

```
input rdl {
    resolve_reg_category = true    // 寄存器未标 category 时，让 Ordt 从 RDL 推断
}
output systemverilog {
    leaf_address_size = 40         // 叶子地址位宽
    base_addr_is_parameter = false // 不把基地址做成顶层参数
    use_gated_logic_clock = false  // 不为寄存器单独用门控时钟
    suppress_no_reset_warnings = true
    include_default_coverage = false
}
```

`leaf_address_size=40` 决定了生成的地址译码用多宽的比较；`include_default_coverage=false` 表明默认不在 RTL 里插覆盖率点（与 4.3 节看到 GLB_CSB_reg.v 里没有覆盖率插入一致）。

README 用一句话总结了前提与产物：

README 的前提与后端清单：[spec/manual/README.md:8-20](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/manual/README.md#L8-L20)

```
Make sure you have java 1.7 or later version installed
Update variable JAVA in make/tools.mk to point to your local java installation
then you can use below command to generate rtl, ral and cmod
make
Following backends will be generated:
* regs_v.v: verilog model
* regs_ral.sv: ral class
* cmod: c++ model
* sv: systemverilog model
```

#### 4.2.4 代码实践

**实践目标**：在不改动源码的前提下，亲手跑通「RDL → 多后端」的演示生成，得到真实产物。

**操作步骤**：

1. 确认本机有 Java：`java -version`（README 要求 1.7+）。如缺，按 README 改 `tools/make/tools.mk` 里的 `JAVA`。
2. 进入 `spec/manual/`，直接 `make`（即默认目标）。若想单独看产物路径，先 `make -n` 打印命令而不执行。
3. 在输出目录（`$(OUT_DIR)`，由 `common.make` 的 `TOT/OUTDIR/PROJECT/REL_PATH_FROM_TOT` 拼成）下查看 `regs_v.v`、`sv/`、`regs_ral.sv`、`cmod/` 是否都已生成。

**需要观察的现象**：生成的 `regs_v.v` 里应出现 `estats`、`erdr`、`simple1`、`extra_reg`、`disable_check`、`wide_reg` 等 `test.rdl` 里声明过的名字——**不会**出现 `NVDLA_GLB_S_INTR_MASK_0` 这类 NVDLA 寄存器名。

**预期结果**：成功生成四个后端；肉眼可见 `regs_v.v` 描述的是演示寄存器，而非 GLB。这正是判断「这是演示、不是生产」的直接证据。

**待本地验证**：本仓库沙箱内未实际执行 `make`（运行 `java` 需本地授权），上述流程据 README 与 Makefile 还原；是否一次跑通、是否有告警，以本地实跑为准。

#### 4.2.5 小练习与答案

**练习 1**：Makefile 里那条命令同时带了 5 个后端开关。如果只想生成 RTL，验证平台那边会缺什么？

> **答案**：缺 `-uvmregs` 产出的 `regs_ral.sv`（RAL 类）。没有 RAL，UVM 验证平台就无法用高层寄存器 API 驱动/检查，只能退回裸总线事务。这正是「多后端同源」的价值：RTL 与 RAL 必须由同一份 RDL 派生才能保证位定义一致。

**练习 2**：`test.parms` 里 `resolve_reg_category = true` 解决什么问题？

> **答案**：寄存器的「类别」（如配置类 CONFIG、状态类 STATE）会影响 RAL 与文档后端的组织，也会影响某些检查。若 RDL 源里没显式标 `category`，这个输入参数让 Ordt 依据读写权限等线索**自动推断**，避免生成时报「no category」警告。`test.rdl` 里 `erdr.reorder_window` 就显式标了 `category="STATE"` 作为对照。

---

### 4.3 RTL 后端产物精读：以 NV_NVDLA_GLB_CSB_reg.v 为例

#### 4.3.1 概念说明

本模块把镜头对准 `-verilog` 后端的真实产物。需要先澄清一个关键事实：

> 仓库里的 `NV_NVDLA_GLB_CSB_reg.v` **不是**由 `spec/manual/test.rdl` 生成的。它由 NVIDIA 内部那份未开源的生产 RDL、用同一把 `Ordt.jar` 生成后提交进仓库。`test.rdl` 只是教你看懂这把「刀」怎么用；`GLB_CSB_reg.v` 是「刀」砍出来的真物件。两者共用同一套生成机制与同一套 RTL 模板，所以拿 `GLB_CSB_reg.v` 来精读 Ordt 的 RTL 后端风格完全有效。

Ordt 的 Verilog 后端对每一个引擎寄存器页都产出结构高度一致的一个模块（在 NVDLA 里被命名为 `NV_NVDLA_<ENG>_CSB_reg.v` / `_dual_reg.v` / `_single_reg.v`，见 u2-l3）。它遵循固定的**四段式模板**：

1. **端口声明**：统一的读写接口（`reg_rd_data/reg_offset/reg_wr_data/reg_wr_en` + 时钟复位）加上「逐字段」的输入输出端口——`hw=r` 的 field 引成输出、`hw=w` 的 field 引成输入。
2. **地址译码**：把每个寄存器的字偏移与 `reg_offset` 比较，得到逐寄存器的写使能 `_wren`。
3. **输出拼装**：把字段触发器（或输入）按位拼接成 32 位寄存器读出值。
4. **读多路选择**：`case (reg_offset)` 选出当前读地址对应哪个寄存器值。
5. **触发器声明**：一个 `always` 块，按复位值初始化、按逐字段写使能更新。

这套模板正是 u2-l3 所说「统一读写接口」与「四段式」的来源——它不是某位工程师手写的风格，而是生成器固化输出的固定结构。

#### 4.3.2 核心流程

一次 CPU 写事务流经生成模块的过程：

```
CSB 写请求带 (reg_offset, reg_wr_data, write)
        │
        ▼
reg_wr_en = (CSB 写有效)                          // 顶层一次写使能
        │
        ▼
逐寄存器 _wren = (reg_offset == 该寄存器偏移) & reg_wr_en   // 地址译码，第 2 段
        │
        ▼
always 块：if (某寄存器_wren) 对应字段触发器 <= reg_wr_data[位段]  // 第 5 段
        │
        ▼
字段触发器 Q 端 ──拼装──▶ 32 位寄存器读出值 _out        // 第 3 段
        │
        ▼
CPU 读请求带 (reg_offset) ──case──▶ reg_rd_data = 命中寄存器的 _out  // 第 4 段
```

#### 4.3.3 源码精读

以 GLB 的寄存器文件为标本。先看模块端口——注意它如何把「字段」逐个暴露成端口：

模块声明与统一读写接口：[vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v:11-21](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L11-L21)

```verilog
module NV_NVDLA_GLB_CSB_reg (
   reg_rd_data
  ,reg_offset
  ,reg_wr_data
  ,reg_wr_en
  ,nvdla_core_clk
  ,nvdla_core_rstn
  ,bdma_done_mask0      // ↓ 下面是一长串「字段级」端口
  ...
```

这就是 u2-l3 所说的统一读写接口：`reg_offset`（12 位字地址）、`reg_wr_data`（32 位）、`reg_wr_en`、`reg_rd_data`（32 位）。GLB 只有 4 个寄存器（hw_version/mask/set/status），所以端口段相对短；CDMA/CSC 那种上百寄存器的引擎，这一段会非常长。

接着是地址译码（模板第 2 段）：

地址译码：逐寄存器写使能：[vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v:176-179](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L176-L179)

```verilog
wire nvdla_glb_s_intr_mask_0_wren   = (reg_offset_wr == (32'h4  & 32'h00000fff)) & reg_wr_en;
wire nvdla_glb_s_intr_set_0_wren    = (reg_offset_wr == (32'h8  & 32'h00000fff)) & reg_wr_en;
wire nvdla_glb_s_intr_status_0_wren = (reg_offset_wr == (32'hc  & 32'h00000fff)) & reg_wr_en;
wire nvdla_glb_s_nvdla_hw_version_0_wren = (reg_offset_wr == (32'h0  & 32'h00000fff)) & reg_wr_en;
```

这里把 u2-l4 讲过的中断寄存器地址**直接写死**：mask=0x4、set=0x8、status=0xc、hw_version=0x0，与 RDL 里的地址标注一一对应（`& 32'h00000fff` 是按 4KB 页取低位，呼应 u2-l2 的 4KB 地址译码）。`reg_offset_wr` 是把 12 位 `reg_offset` 零扩展成 32 位（见第 172 行 `assign reg_offset_wr = {20'b0, reg_offset};`）。

第 3 段——输出拼装，是把字段触发器按位拼回 32 位。看 mask 寄存器：

mask 寄存器的位拼装：[vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v:183](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L183)

```verilog
assign nvdla_glb_s_intr_mask_0_out[31:0] = { 10'b0,
    cacc_done_mask1, cacc_done_mask0, cdma_wt_done_mask1, cdma_wt_done_mask0,
    cdma_dat_done_mask1, cdma_dat_done_mask0, 6'b0,
    rubik_done_mask1, rubik_done_mask0, bdma_done_mask1, bdma_done_mask0,
    pdp_done_mask1, pdp_done_mask0, cdp_done_mask1, cdp_done_mask0,
    sdp_done_mask1, sdp_done_mask0 };
```

这一行就是 u2-l4「8 引擎 × 2 影偶组 = 16 个中断源」的硬件写真。把位拼接展开成表（每个引擎占相邻两位，bit0=组0、bit1=组1）：

| 位 | 引擎 | 位 | 引擎 | 位 | 引擎 |
| --- | --- | --- | --- | --- | --- |
| [1:0] | sdp | [3:2] | cdp | [5:4] | pdp |
| [7:6] | bdma | [9:8] | rubik | [15:10] | 保留 0 |
| [17:16] | cdma_dat | [19:18] | cdma_wt | [21:20] | cacc |
| [31:22] | 保留 0 | | | | |

set 与 status 寄存器（第 184、185 行）用的是**完全相同**的位布局，只是把 `*_mask*` 换成 `*_set*` / `*_status*`。这种「三寄存器同布局」正是中断 mask/set/status 的标准范式，由同一份 RDL field 列表保证三者不会错位。

第 4 段——读多路选择：

读多路选择：[vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v:194-216](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L194-L216)

```verilog
always @( reg_offset_rd_int or ... ) begin
  case (reg_offset_rd_int)
     (32'h4  & 32'h00000fff): reg_rd_data = nvdla_glb_s_intr_mask_0_out;
     (32'h8  & 32'h00000fff): reg_rd_data = nvdla_glb_s_intr_set_0_out;
     (32'hc  & 32'h00000fff): reg_rd_data = nvdla_glb_s_intr_status_0_out;
     (32'h0  & 32'h00000fff): reg_rd_data = nvdla_glb_s_nvdla_hw_version_0_out;
    default: reg_rd_data = {32{1'b0}};
  endcase
end
```

读时按地址选通对应 `_out`；未命中返回 0。这是纯组合读路径（无握手），与 u2-l3 描述的「固定延迟读」一致。

第 5 段——触发器声明，注意 `hw=r` 与 `hw=w` 两类字段的截然不同处理。先看 mask（CPU 写、`hw=r`）字段：生成器**为之建触发器**：

mask 字段的触发器与写入（CPU 可写）：[vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v:242-245](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L242-L245)

```verilog
// Register: NVDLA_GLB_S_INTR_MASK_0    Field: bdma_done_mask0
if (nvdla_glb_s_intr_mask_0_wren) begin
    bdma_done_mask0 <= reg_wr_data[6];   // bit6 = bdma 组0，与上表吻合
end
```

CPU 写 mask 寄存器时，按位把 `reg_wr_data[6]` 锁进 `bdma_done_mask0` 触发器。复位值在第 224–240 行统一初始化为 0（即默认不屏蔽，与 u2-l4「复位全 0 默认放行」一致）。

再看 set/status（`hw=w`，硬件写）字段：生成器**不建触发器**，留给外部实现：

set/status 字段「留给外部实现」：[vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v:322-324](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L322-L324)

```verilog
// Not generating flops for field NVDLA_GLB_S_INTR_SET_0::bdma_done_set0 (to be implemented outside)
```

这条注释对应 4.1 节的 `hw=w` 语义：set/status 由硬件（GLB 的中断控制器 `NV_NVDLA_GLB_ic`，见 u2-l4）驱动，是**模块输入端口**（端口名列在第 40–71 行，`input` 方向声明在第 111–143 行，如 `input bdma_done_set0;`）。这就是为什么 u2-l4 说 status 是「软硬件双源寄存器」、必须留在手写的 `ic` 里而非自动 reg 文件里——生成器只负责把它声明成输入并参与读拼装，真正的置位/清除逻辑在外部。

还有一类是常量字段，生成器直接给常量不给触发器，例如硬件版本号：

常量字段（hw_version）：[vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v:181-186](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L181-L186)

```verilog
assign major = 8'h31;                 // 版本号常量
assign minor = 16'h3030;
assign nvdla_glb_s_nvdla_hw_version_0_out[31:0] = { 8'b0, minor, major };
```

`major=0x31`、`minor=0x3030` 来自 RDL 里 field 的 `= 2'd2` 这类复位/默认值声明，生成器把它固化成常量连线——读 hw_version 永远返回固定值。

最后，生成器还附赠一段**仅仿真用**的写调试块（`synopsys translate_off` 保护，综合时剔除）：

仿真用写跟踪（arreggen）：[vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v:406-422](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v#L406-L422)

```verilog
always @(posedge nvdla_core_clk) begin
  if (reg_wr_en) begin
    case(reg_offset)
      (32'h4 & 32'h00000fff): if (arreggen_dump) $display("... reg wr: NVDLA_GLB_S_INTR_MASK_0 = 0x%h ...", $time, reg_wr_data, ...);
      ...
      default: if (arreggen_abort_on_invalid_wr) begin $display("ERROR: write to undefined register!"); $finish; end
```

靠 `+arreggen_dump` / `+arreggen_abort_on_invalid_wr` 两个仿真 plusarg 控制：前者打印每次寄存器写、后者在写到未定义寄存器时报错终止。`arreggen` 即「addressable register generator」——Ordt 的别名。这段是调试寄存器编程的利器，也是判断一个 `*_reg.v` 是否由 Ordt 生成的指纹之一。

那么这个模块如何被引擎接入？看 GLB 的 CSB 适配层如何例化它：

GLB_csb 例化 u_reg 并连上 CSB 请求：[vmod/nvdla/glb/NV_NVDLA_GLB_csb.v:257-266](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_csb.v#L257-L266)

```verilog
assign reg_offset = {req_addr[9:0],{2{1'b0}}};   // 字地址左移 2 位成字节地址
assign reg_wr_en  = req_vld & req_write;
assign reg_wr_data = req_wdat;
NV_NVDLA_GLB_CSB_reg u_reg (
   .reg_rd_data (reg_rd_data[31:0])
  ,.reg_offset  (reg_offset[11:0])
  ,.reg_wr_data (reg_wr_data[31:0])
  ,.reg_wr_en   (reg_wr_en)
  ,.nvdla_core_clk  (nvdla_core_clk)
  ,.nvdla_core_rstn (nvdla_core_rstn)
  ...
```

CSB 适配层把 CSB 请求（`req_addr/req_vld/req_write/req_wdat`）翻译成生成模块要的 `reg_offset/reg_wr_en/reg_wr_data`，并把字段输出（`bdma_done_mask0` 等）连到 GLB 内部。这就是「自动生成的 reg 文件」与「手写的引擎控制逻辑」的分界线：reg 文件只管存储与读写译码，影偶切换、中断聚合等行为逻辑留在手写的 `glb/ic/csb` 里（呼应 u2-l3、u2-l4）。

#### 4.3.4 代码实践

**实践目标**：用 4.1 节那套「字段权限 → 生成行为」的规则，反向审计 `GLB_CSB_reg.v` 的中断位布局，验证它与 u2-l4 的结论一致。

**操作步骤**：

1. 打开 [NV_NVDLA_GLB_CSB_reg.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v)。
2. 定位第 183 行（mask 拼装）。从最高位到最低位，列出每个字段名与它占据的 bit。
3. 画出一张「bit → 引擎 → 影偶组」表（参考本模块给出的表）。
4. 跳到第 242–320 行，找到 `bdma_done_mask0 <= reg_wr_data[?]`、`sdp_done_mask0 <= reg_wr_data[?]`、`cacc_done_mask1 <= reg_wr_data[?]`，确认 `[?]` 的数字与第 2 步表格吻合。
5. 跳到第 322 行起，确认 set/status 字段都是「Not generating flops … to be implemented outside」，再到模块端口段确认它们是 `input`（端口名见第 40–71 行，`input` 方向声明见第 111–143 行）。

**需要观察的现象**：第 3 步的位表应当显示 8 个引擎、每引擎 2 位、中间 `[15:10]` 与高 10 位为保留 0；第 4 步的写位索引应当与位表一一对应，毫无错位。

**预期结果**：mask/set/status 三个寄存器的位布局完全一致（都是同一份 field 列表派生），证明「单一可信源」消除了三者漂移的可能。

**待本地验证**：无须运行仿真；本实践为纯源码阅读与对账，结论可直接从源码得出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `bdma_done_status0` 是模块的 `input`，而 `bdma_done_mask0` 是 `reg`（触发器）？

> **答案**：两者 RDL 权限不同。mask 是 CPU 写、硬件读（`sw=rw; hw=r`），所以生成器建触发器存 CPU 写入值、作为输出。status 是硬件写、CPU 读（`hw=w; sw=r`），由 GLB 中断控制器 `ic` 驱动置位/清除（见 u2-l4），所以生成器只声明成输入、不建触发器，注释「to be implemented outside」即指 `ic`。

**练习 2**：读 `reg_offset=0x8`（set 寄存器）会返回什么？写它有意义吗？

> **答案**：读返回 `nvdla_glb_s_intr_set_0_out`——但 set 字段是 `input`，读到的值取决于外部（`ic`）当前驱动。在 u2-l4 里 set 是「只写、读回 0」语义，所以这里读到的多半是 0。写 set（`nvdla_glb_s_intr_set_0_wren` 拉高）的作用是软件主动置位中断，由生成器把 `sdp_done_set0_trigger = ..._set_0_wren`（第 188 行）这类触发信号引出，交给外部逻辑去驱动对应 status——注意 trigger 目前只引出了 sdp 一组，其余引擎的软件 set 路径需结合 `ic` 进一步确认。

**练习 3**：第 406 行那段 `always` 块综合时会不会被保留？为什么？

> **答案**：不会。它被 `// synopsys translate_off … translate_on` 包裹（第 394、425 行），综合工具据此忽略整段。它的用途是仿真期用 `+arreggen_dump` 打印寄存器写、用 `+arreggen_abort_on_invalid_wr` 抓「写未定义寄存器」的错误。

---

### 4.4 RAL 与 cmod 后端，及「机制 vs 数据」的诚实边界

#### 4.4.1 概念说明

本模块收尾两件事：简要交代 RTL 之外的后端（RAL、cmod）产出什么，以及把本讲最重要的「诚实边界」讲透。

- **RAL 后端（`-uvmregs`）**：把每个寄存器/字段变成 SystemVerilog 的 UVM 类（`uvm_reg`/`uvm_reg_field`），含地址、权限、复位值、覆盖率。验证平台用它做高层读写与预测比对，而不用手算地址。它是 RTL 寄存器的「软件镜像」。
- **C++ 后端（`-cppmod`/`-cppdrvmod`）**：为 cmod 参考模型（见 u7-l3）提供 C++ 寄存器类，让 SystemC/TLM 模型也能像 RTL 一样被「配置寄存器 → 运行」地驱动。它和 RTL、RAL 三者同源，是 cmod 能做位精确比对的根基之一。

**诚实边界**（务必记住）：仓库随附的 `Ordt.jar` + `test.rdl` + `test.parms` + `Makefile` 是一套**自洽的生成机制演示**。它教你会用这把刀，但：

1. `test.rdl` 描述的是 `estats/erdr/simple1` 这些**演示寄存器**，不是 NVDLA 任何一个引擎的真寄存器。
2. `vmod/nvdla/` 下 19 个 `*_reg.v`（bdma/cacc/cdma/cdp/cmac/csc/glb/cvif/mcif/pdp/rubik/sdp）是由 **NVIDIA 内部、未开源**的生产 RDL，用**同一把** `Ordt.jar` 生成后提交进仓库的。
3. Makefile 第 9 行的 `FIXME: change below test parameters and rdl input to real one` 是这一事实的白纸黑字。

因此，若你想在本仓库里**修改**某个 NVDLA 寄存器（加一个 field、改一个位），你**无法**通过改 `test.rdl` 再 `make` 来重生 `GLB_CSB_reg.v`——因为生成它所需的生产 RDL 不在仓库里。你能做的是：理解生成机制后，直接编辑那份已提交的 `*_reg.v`（并清楚这破坏了「单一可信源」的一致性，需同步改 RAL 与 cmod），或者向 NVIDIA 内部团队申请改生产 RDL 后重新生成。这条边界，是开源硬件项目「工具开放、规格部分封闭」的典型形态。

#### 4.4.2 核心流程

```
                          ┌── (本仓库未提供) ──┐
生产 RDL (内部)  ──┐      │                     │
                   ├──▶ Ordt.jar ──▶ 19 个 vmod/nvdla/*/*_reg.v   (已提交，可读不可重生)
                   │      │
test.rdl (演示)  ──┘      │
                   └──▶ Ordt.jar ──▶ regs_v.v / regs_ral.sv / cmod (演示产物，可本地重生)
```

两条路径共用同一个 `Ordt.jar`，差别只在喂进去的 RDL 不同。学机制用下路，看真产物看上路。

#### 4.4.3 源码精读

验证「19 个生产寄存器文件」的事实——它们确实散布在各引擎目录下、命名一致：

仓库中由 Ordt 生成的寄存器文件清单（节选）：用 `git ls-files` 可见 `vmod/nvdla/` 下共 19 个 `*_reg.v`，例如：

- 配置/状态页：`vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v`、`vmod/nvdla/nocif/NV_NVDLA_MCIF_CSB_reg.v`、`vmod/nvdla/nocif/NV_NVDLA_CVIF_CSB_reg.v`
- 影偶操作页：`vmod/nvdla/cdma/NV_NVDLA_CDMA_dual_reg.v` + `NV_NVDLA_CDMA_single_reg.v`、`vmod/nvdla/csc/NV_NVDLA_CSC_dual_reg.v` + `_single_reg.v`、`vmod/nvdla/cacc/NV_NVDLA_CACC_dual_reg.v` + `_single_reg.v`、`vmod/nvdla/rubik/NV_NVDLA_RUBIK_dual_reg.v` + `_single_reg.v`
- 单页：`vmod/nvdla/cmac/NV_NVDLA_CMAC_reg.v`、`vmod/nvdla/bdma/NV_NVDLA_BDMA_reg.v`、`vmod/nvdla/sdp/NV_NVDLA_SDP_reg.v` + `_RDMA_reg.v`、`vmod/nvdla/pdp/NV_NVDLA_PDP_reg.v` + `_RDMA_reg.v`、`vmod/nvdla/cdp/NV_NVDLA_CDP_reg.v` + `_RDMA_reg.v`

它们都带相同的「指纹」：模块名 `NV_NVDLA_*_reg`、统一读写接口 `reg_offset/reg_wr_en/reg_wr_data/reg_rd_data`、`arreggen_*` 仿真调试块、`// Not generating flops for field … (to be implemented outside)` 注释。这些指纹印证了它们同出一把生成器。

而演示产物则由 README 明示其名字与用途（已在 4.2.3 引用 [spec/manual/README.md:16-20](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/manual/README.md#L16-L20)）：`regs_v.v`（verilog）、`regs_ral.sv`（ral）、`cmod`（c++）、`sv`（systemverilog）。

#### 4.4.4 代码实践

**实践目标**：用「指纹」方法，自己判断仓库里任意一个 `*_reg.v` 是否由 Ordt 生成。

**操作步骤**：

1. 任选一个引擎寄存器文件，如 [vmod/nvdla/bdma/NV_NVDLA_BDMA_reg.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/bdma/NV_NVDLA_BDMA_reg.v)。
2. 检查四个指纹：(a) 模块名是否 `NV_NVDLA_*_reg`；(b) 是否有 `reg_offset/reg_wr_en/reg_wr_data/reg_rd_data` 四件套；(c) 是否有 `arreggen_dump`/`arreggen_abort_on_invalid_wr`；(d) 是否有 `// Not generating flops for field …` 注释。
3. 统计该文件的寄存器个数（数 `_wren` 信号或读 `case` 分支），与你在 u2 系列里学到的 BDMA 配置规模对账。

**需要观察的现象**：四个指纹全部命中，可判定为 Ordt 生成；寄存器数量与 BDMA 的描述符/启动/状态配置相符。

**预期结果**：确认「整仓库的引擎寄存器文件都是同一把 Ordt 生成的」，只是各自喂了对应引擎的生产 RDL。

**待本地验证**：具体寄存器清单以本地打开文件为准；本实践为阅读型，无须运行。

#### 4.4.5 小练习与答案

**练习 1**：同事说「我把 `test.rdl` 里加一个寄存器，`make` 后就能更新 GLB 的中断寄存器」。这句话哪里错了？

> **答案**：错在把演示 RDL 当成了生产 RDL。`test.rdl` 描述的是 `estats/erdr/simple1`，`make` 只会更新演示产物 `regs_v.v` 等；`GLB_CSB_reg.v` 由另一份未开源的生产 RDL 生成，改 `test.rdl` 对它毫无影响（Makefile 第 9 行 FIXME 即指此）。

**练习 2**：为什么 cmod 参考模型（u7-l3）能和 RTL 做位精确的寄存器级比对？

> **答案**：因为 cmod 的 C++ 寄存器类（`-cppmod` 后端）和 RTL 的 `*_reg.v`（`-verilog` 后端）由**同一份生产 RDL** 经同一把 Ordt 派生，字段定义、地址、复位值同源，天然一致。这正是「单一可信源」跨 RTL/验证/参考模型的价值。

---

## 5. 综合实践

把本讲四条主线串成一个完整任务：**还原一个 NVDLA 中断寄存器从 RDL 到 RTL 的全链路**。

任务背景：GLB 有一个 `INTR_MASK_0` 寄存器（地址 0x4），其中 bit6 是 `bdma_done_mask0`（BDMA 引擎影偶组 0 的中断屏蔽位）。

请完成：

1. **RDL 侧（4.1）**：假设要描述这个字段，写出它的 RDL field 声明草稿。提示：它是 CPU 可读可写、硬件只读的配置位，复位为 0。参考 `test.rdl` 里 `swcfg_field` 的写法。
2. **生成侧（4.2）**：写出会把它生成成 RTL 的 Ordt 命令行骨架（指出 `-verilog`、`-parms`、RDL 源三个关键参数）。
3. **RTL 侧（4.3）**：在 [NV_NVDLA_GLB_CSB_reg.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_CSB_reg.v) 里追踪 `bdma_done_mask0` 的三处出现：端口声明、地址译码写使能（`nvdla_glb_s_intr_mask_0_wren`）、触发器写入（`<= reg_wr_data[6]`），确认三者闭环。
4. **接入侧（4.3）**：在 [NV_NVDLA_GLB_csb.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/glb/NV_NVDLA_GLB_csb.v) 里找到 `bdma_done_mask0` 这个端口被连到 GLB 内部哪条线（进而送进中断控制器 `ic` 参与 `core_intr = OR(~mask & status)`，见 u2-l4）。
5. **边界反思（4.4）**：写一句话说明——如果你现在想给 `INTR_MASK_0` 再加一个 bit（比如某新引擎的中断屏蔽），在本开源仓库里你**实际能做**和**做不到**的分别是什么。

**参考要点**：

- 第 1 步草稿形如 `field { sw=rw; hw=r; reset=1'b0; desc="BDMA group0 done mask"; } bdma_done_mask0;` 并在 mask 寄存器里 `swcfg_field bdma_done_mask0 [6:6];`。
- 第 2 步：`java -jar Ordt.jar -parms <parms> -verilog <out.v> <src.rdl>`。
- 第 3 步：端口见第 22/92 行、写使能见第 176 行、触发器写入见第 243–245 行，bit 索引 `[6]` 三处一致。
- 第 5 步：能做的是直接编辑已提交的 `GLB_CSB_reg.v`（并同步改 GLB_csb 端口与 ic 逻辑、以及 RAL/cmod 对应文件，承担一致性风险）；做不到的是从「生产 RDL」重生——因为那份 RDL 不在仓库。

## 6. 本讲小结

- **SystemRDL 是寄存器空间的单一可信源**：用 `sw=/hw=` 权限、`counter/we/rclr/intr/halt` 等行为特性声明寄存器与字段，一份规格喂给所有下游。
- **Ordt 是「一解析、多后端」的生成器**：`-verilog/-systemverilog/-uvmregs/-cppmod` 一次产出 RTL、SV、RAL、C++ 模型，由 `test.parms` 调节各后端参数。
- **RTL 后端有固定四段式模板**：端口声明（字段级输入输出）→ 地址译码（`_wren`）→ 输出拼装（`_out`）→ 读多路（`case`）→ 触发器声明（`always`）。
- **字段权限决定生成行为**：`hw=r` 字段建触发器并作输出；`hw=w` 字段不建触发器、作输入（`// to be implemented outside`）；常量字段给常量连线。
- **`GLB_CSB_reg.v` 印证了 u2-l4 的中断布局**：mask/set/status 三寄存器位布局完全一致，8 引擎 × 2 影偶组 = 16 个有效位，由同一份 field 列表派生、天然不漂移。
- **诚实边界**：仓库的 `test.rdl` 是演示，19 个生产 `*_reg.v` 由未开源的内部 RDL 用同一把 Ordt 生成；Makefile 的 `FIXME` 注释是这条边界的铁证。改寄存器在本仓库只能直接编辑已提交文件，无法从生产 RDL 重生。

## 7. 下一步学习建议

- **横向对照更多引擎**：拿 [NV_NVDLA_CDMA_dual_reg.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cdma/NV_NVDLA_CDMA_dual_reg.v) 做同样的四段式精读，体会影偶（dual）寄存器文件如何比 GLB 的单组（CSB_reg）多出 producer/consumer 选通逻辑，衔接 u2-l3。
- **回到验证侧**：学完 u7-l1/u7-l2（trace-player 与 CSB 激励）后，回头看本讲的 RAL 后端——理解验证平台为何能用高层 API 驱动寄存器，而不必像 trace-player 那样裸发地址+数据。
- **回看综合侧**：结合 u8-l3（综合流程），注意 `*_reg.v` 里的 `// synopsys translate_off`、`spyglass disable` 等注释如何被综合工具识别——生成器在产物里预埋了这些 lint/综合锚点。
- **若要深入 Ordt 本身**：可查阅 Ordt 开源项目的文档，了解 `test.parms` 里未用到的更多后端（如 HTML 寄存器手册、Verilog header），以及 `external` regfile 的完整语义。
