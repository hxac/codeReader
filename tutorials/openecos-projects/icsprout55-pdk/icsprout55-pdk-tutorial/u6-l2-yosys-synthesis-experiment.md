# 综合实验：RTL 映射到 H7C 门级网表

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立写出一条完整的 yosys 命令序列（`read_liberty -lib` → `synth` → `dfflibmap -liberty` → `abc -liberty` → `stat`/`write_verilog`），把一段 RTL 映射到 H7C 标准单元库。
2. 说清楚为什么时序单元和组合逻辑要分两步映射（`dfflibmap` 与 `abc` 各管什么），以及 corner liberty 的选择会如何改变综合结果。
3. 把综合出的门级网表与 PDK 自带的 Verilog 单元模型（`ics55_LLSC_H7CR.v`）联合编译仿真，用 iverilog 对比综合前后的行为一致性。

本讲是「开源 EDA 全流程实战」的第二讲：u6-l1 解决了「把 PDK 装进工具」，本讲解决「让 RTL 变成这片工艺上的门级网表」。

## 2. 前置知识

- **逻辑综合（synthesis）**：把行为级 RTL（`always`、`assign`）转成由具体工艺单元（INV、NAND、DFF…）组成的门级网表的过程。可以分两段理解：前半段「工艺无关综合」把 RTL 变成通用布尔逻辑网络；后半段「工艺映射」把通用网络绑定到某个库的具体单元上。
- **yosys**：开源逻辑综合框架。本讲用到它的四个能力：读 liberty（`read_liberty`）、通用综合脚本（`synth`）、触发器映射（`dfflibmap`）、组合映射（`abc`，内部调用开源的 ABC 优化引擎）。
- **liberty 在综合中的双重身份**（承接 u3-l6）：对综合器而言，liberty 是一份「单元目录」——每个 cell 的面积、引脚、功能表达式、以查找表形式给出的延迟/功耗。`read_liberty -lib` 只抽取目录信息（单元名、端口、面积、function），忽略详细时序表；`dfflibmap -liberty` / `abc -liberty` 则要消费延迟表来挑单元。
- **corner（工艺角）**：tt/ff/ss 等工艺、电压、温度组合。u3-l6 已展示 IO 库同一位电容在 ff/ss 之间漂移约 −26%～−31%；对综合来说，corner 换了，延迟表就换，abc 选出的驱动强度配比和总面积也会变。
- **门级仿真（gate-level simulation）**：不再仿真 RTL，而是仿真「网表 + 库单元 Verilog 模型」。PDK 必须为每个单元提供仿真模型——这正是本讲主角 [ics55_LLSC_H7CR.v](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v) 的角色。
- **`celldefine` / `specify` / UDP**（承接 u3-l5）：`` `celldefine `` 把模块标记为标准单元；`specify` 块描述模块路径延迟和时序检查（`$setuphold`、`$width`）；UDP（用户自定义原语，`primitive ... table ... endprimitive`）用真值表精确描述触发器的边沿/复位行为。
- **SDF 反标（back-annotation）**：用真实延迟值（从 liberty + 布线寄生算出）替换占位延迟的机制。本讲的模型里延迟全是占位值，所以本讲做的是「功能对齐」的门级仿真，时序签核要靠 SDF。

一个必须先交代的事实：**标准单元库的 liberty 不在 git 内**。仓库只跟踪小体积文本，H7CR 的 liberty 需要按 u1-l3 讲过的方式从 GitHub Release 下载解压，且压缩包内的具体 `.lib` 文件名**待确认**（本讲会教你怎么用 `find` 找到它）。同样，仓库本身不提供 yosys/iverilog，需自行安装；本讲所有运行结果均标注「待本地验证」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Makefile](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile) | 下载并解压 H7CR liberty（`make unzip`），是综合流程的「原料供应线」 |
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v) | H7CR 全部 751 个单元的 Verilog 仿真模型 + 14 个 UDP，门级仿真的「模型库」 |
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/cell_list/ics55_LLSC_H7CR.txt](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/cell_list/ics55_LLSC_H7CR.txt) | 单元清单（748 项），用来核对网表里出现的单元名 |
| `IP/STD_cell/.../ics55_LLSC_H7CR/liberty/`（需下载生成） | 综合映射的时序库，目录由 Makefile 创建，内部文件名待确认 |

## 4. 核心概念与源码讲解

### 4.1 read_liberty/synth 流程：给 yosys 备好「单元目录」

#### 4.1.1 概念说明

综合器在映射阶段要回答的问题是：「实现这个与非操作，库里有 NAND2X0P5H7R、NAND2X1H7R、…、NAND2X12H7R 十几档，选哪一档？」要回答它必须知道每档的延迟随负载电容怎么变、面积多大——这些信息全在 liberty 里。所以综合流程的第一步永远是**把 liberty 喂给工具**。

`read_liberty -lib` 中的 `-lib` 表示「以库目录方式读入」：只抽取 cell 名、引脚方向、面积、功能表达式，把每个单元注册成一个黑盒模块。这样后面 `hierarchy -check` 才不会因为网表里出现工具不认识的 `DFFRX1H7R` 而报错。

而 liberty 从哪来？u1-l3 讲过「文件名即协议」：`ics55_LLSC_H7CR_liberty.tar.bz2` 这个 Release 资产名会被 `patsubst` 展开成解压目录。

#### 4.1.2 核心流程

```text
make unzip（或 make unzip RELEASE_TAG=<已发布版本>）
        │  下载 ics55_LLSC_H7CR_liberty.tar.bz2 → 解压到
        │  IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/liberty/
        ▼
find 该目录 -name "*.lib"        # 确认真实文件名（待确认，勿凭空猜）
        ▼
yosys> read_liberty -lib <lib路径>   # ① 建立单元目录（黑盒）
yosys> read_verilog counter8.v       # ② 读 RTL
yosys> hierarchy -check -top counter8
yosys> synth -top counter8 -flatten  # ③ 工艺无关综合（proc/opt/fsm/memory/techmap）
        ▼
（4.2 继续：dfflibmap → abc → clean → stat → write_verilog）
```

#### 4.1.3 源码精读

Makefile 用三个变量把「资产名 → 解压目录」绑定起来。[Makefile:L10-L13](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L10-L13) 定义 H7CH/H7CL/H7CR 三份 liberty 压缩包名，并允许 `RELEASE_TAG` 固定版本：

```makefile
RELEASE_TAG ?= latest
RELEASE_FILE_LIB := ics55_LLSC_H7CH_liberty.tar.bz2 \
                    ics55_LLSC_H7CL_liberty.tar.bz2 \
                    ics55_LLSC_H7CR_liberty.tar.bz2
```

[Makefile:L22-L23](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L22-L23) 用 `patsubst` 从压缩包名抽出库名 `ics55_LLSC_H7CR`，拼出目标目录：

```makefile
DECOMP_DIR_LIB_P := IP/STD_cell/ics55_LLSC_H7C_V1p10C100
DECOMP_DIR_LIB   := $(patsubst %_liberty.tar.bz2, $(DECOMP_DIR_LIB_P)/%/liberty, $(RELEASE_FILE_LIB))
```

[Makefile:L62-L66](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L62-L66) 是解压模式规则：以目录为目标，依赖对应压缩包，`tar -xjvf` 原地展开（`touch $@` 让目录时间戳生效，保证幂等）：

```makefile
$(DECOMP_DIR_LIB_P)/%/liberty: %_liberty.tar.bz2
	@echo "\n[unzip] decompressing: $< -> $(DECOMP_DIR_LIB_P)/$*/"
	@mkdir -p $@
	@tar -xjvf $< -C $(DECOMP_DIR_LIB_P)/$*/
	@touch $@
```

[Makefile:L80-L81](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L80-L81) 的 `unzip` 目标按 `start → clean-dir → 解压 → clean-bz2` 串起全流程。执行后，H7CR 的 liberty 就落在 `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/liberty/` 下——注意压缩包内可能还有一层子目录，所以用 `find` 而不是猜路径。

#### 4.1.4 代码实践

1. **实践目标**：拿到 H7CR liberty 的真实路径与文件名，为 4.2 的综合做准备。
2. **操作步骤**：
   ```bash
   make unzip                 # 或 make unzip RELEASE_TAG=<某个已发布 tag>
   find IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/liberty -name "*.lib"
   ```
   对找到的每个 `.lib`，用 `head -40` 观察库头：确认 `library (...)` 的名字、`nom_voltage`/`nom_temperature`、`time_unit`、`capacitive_load_unit`。也可以只做 dry-run：
   ```bash
   make -n unzip    # 打印将执行的下载与解压命令但不真正执行
   ```
3. **需要观察的现象**：liberty 目录下 `.lib` 文件的个数与命名。若只有一份（例如典型角 1.2V/25℃，与 doc 目录里 `ics55_LLSC_H7CR_TYPICAL_V1P2_T25.pdf` 的命名风格相呼应——这只是推测，待确认），则 4.2 的 corner 对比实验要等更多 corner 发布；若有多份，记录 tt/ff/ss 的分布。
4. **预期结果**：得到一个或多个 `.lib` 的完整相对路径，把它代入 4.2 的 `<lib路径>`。本讲撰写时该目录尚未下载，文件名**待确认**。

#### 4.1.5 小练习与答案

1. **练习**：为什么 `read_liberty` 要加 `-lib`，不加会怎样？
   **答案**：不加 `-lib` 时 yosys 会尝试完整解析时序表并把单元当设计的一部分处理；加 `-lib` 只建黑盒目录，网表里引用单元名时能对上号即可，速度快且避免无关信息干扰。真正消费延迟表的是后面的 `dfflibmap -liberty` 和 `abc -liberty`。
2. **练习**：`make -n unzip` 输出里，H7CR 的 liberty 会被解压到哪个目录？这条路径由 Makefile 的哪两个变量拼出来？
   **答案**：`IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/liberty`；由 `DECOMP_DIR_LIB_P`（前缀）与 `patsubst` 从 `ics55_LLSC_H7CR_liberty.tar.bz2` 抽出的 `ics55_LLSC_H7CR`（`%/liberty`）拼出。

### 4.2 dfflibmap + abc 库映射：两步把逻辑绑到 H7C 单元上

#### 4.2.1 概念说明

`synth` 结束后，设计里是 yosys 的**内部通用单元**：触发器形如 `$_DFF_P_`（上升沿 D 触发器）、组合门形如 `$_AND_`、`$_NOT_`。把它们换成 `DFFRX1H7R`、`AND2X1H7R` 需要**两步，且顺序固定**：

- **`dfflibmap -liberty <lib>`——先映射触发器**。ABC 是纯组合逻辑优化引擎，不认识触发器；如果先跑 abc，`$_DFF_P_` 会原样留下，abc 无法穿透它做优化。`dfflibmap` 读 liberty 里每个 cell 的 `ff` 组（next_state/clocked_on 等属性），把内部 FF 类型一一映射到库里的 DFFX/DFFRX/DFFN*/ICG 等。RTL 写的是「异步低有效复位」，映射目标就应当是带 `RN` 的 DFFRX 系列。
- **`abc -liberty <lib>`——再映射组合逻辑**。ABC 把布尔网络重新综合，并在库里挑选单元与**驱动强度**：负载重就选 X4/X6 大驱动，负载轻就选 X0P5/X1 小驱动，依据正是 liberty 的延迟查找表与面积。挑得好不好，直接由「用了哪个 corner 的 liberty」决定。

映射完成后芯片面积按公式

\[ A_{chip} = \sum_{i \in \text{cell 类型}} n_i \times a_i \]

统计（\(n_i\) 为第 i 种单元的数量，\(a_i\) 为其 liberty 面积）。`stat -liberty <lib>` 会替你算好。

**corner 的影响**：换一个 corner 的 liberty，延迟表整体平移（u3-l6 实测 IO 库同位电容在 ff/ss 间漂移约三成），abc 在「面积 vs 延迟」之间的折衷点随之移动——快角库里小驱动显得更慢、慢角库里大驱动更贵，最终网表的驱动档配比与总面积都会不同。这正是「综合要在与签核一致的 corner 上做」的原因。

#### 4.2.2 核心流程

```text
① read_liberty -lib <lib>          # 单元目录
② read_verilog counter8.v；hierarchy -check -top counter8
③ synth -top counter8 -flatten     # 通用综合：proc→opt→fsm→memory→techmap→opt
④ dfflibmap -liberty <lib>         # $_DFF_P_ 等内部 FF → DFFRX1H7R 等（时序映射）
⑤ abc -liberty <lib>               # 组合网络 → INV/AND/XOR/AO…（组合映射+驱动选择）
⑥ clean                           # 去冗余
⑦ stat -liberty <lib>              # 面积/单元构成报告
⑧ write_verilog -noattr counter8_netlist.v
```

注意两个常见坑（都会在 yosys 日志里以 `unmapped` 类警告出现）：

- RTL 意外推断出**锁存器**（组合 `always` 里分支不全）时，`dfflibmap` 对锁存的映射支持与 yosys 版本相关，优先改 RTL 消除锁存。
- 网表里允许出现的只有 liberty 里**有功能模型的单元**。像 ANT 天线单元、FILLCAP 这类无逻辑单元不会被 abc 选中，不必担心。

#### 4.2.3 源码精读

映射的「目标长相」由 PDK 的两份文件共同决定：liberty 说 `DFFRX1H7R` 的端口与面积，Verilog 模型说它仿真的行为——两者必须一致（u5-l2 的一致性检查正是保障）。

先看 cell_list 确认库里确实有异步复位触发器（以及它的邻居们）：[cell_list:L231](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/cell_list/ics55_LLSC_H7CR.txt#L231) 列出 `DFFRX1H7R`，前后还有 DFFQX/DFFRQNX/DFFSX/DFFSRX 等一大家族（负沿 DFFN*、时钟门控 ICG* 在 L15584 起也能找到），这是 `dfflibmap` 挑选的货架。

再看 Verilog 模型里 `DFFRX1H7R` 长什么样。[ics55_LLSC_H7CR.v:L14057-L14072](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v#L14057-L14072)：端口表 `D, RN, CK, Q, QN`——注意复位脚叫 `RN`（低有效），这与 RTL 里的 `rst_n` 语义对齐；行为体用 UDP 实例描述触发：

```verilog
module DFFRX1H7R (D, RN, CK, Q, QN);
  input D, RN, CK;
  output Q, QN;
  reg NOTIFIER;
  supply1 xSN;
  buf   XX0 (xRN,RN);          // 复位缓冲
  buf   IC (clk,CK);
  udp_dff I0 (n0,D, clk, xRN, xSN, NOTIFIER);  // 核心：UDP 真值表
  buf   I1 (Q, n0);
  not   I2 (QN, n0);
```

时序模型在 [ics55_LLSC_H7CR.v:L14080-L14103](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v#L14080-L14103)：CK→Q 的时钟-输出弧、以及按 `if(CK===...)` 分条件列出的 RN→Q 异步复位弧，全部是 `(1.0,1.0)` 占位延迟：

```verilog
  specify
	// arc CK --> Q
	(posedge CK => (Q : D))  = (1.0,1.0);
	if(CK===1'b0 && D===1'b0)
	(negedge RN => (Q : 1'b0))  = (1.0,1.0);
	...
```

组合侧的目标单元以反相器为例：[ics55_LLSC_H7CR.v:L16074-L16089](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v#L16074-L16089) 的 `INVX1H7R`，一行 `not I0(Y, A);` 加一条占位路径延迟 `(A => Y) = (1.0,1.0);`。abc 选中的每一档驱动（X0P5~X20）在文件里都有对应的 module——网表里出现 `INVX6H7R` 也仿真得起来。

顺带一个量化事实：整个 44600 行的模型文件里，7439 处路径延迟**全部**是 `(1.0,1.0)`（可用 `grep -oE '\([0-9.]+,[0-9.]+\)' | sort | uniq -c` 验证）。这印证了 u3-l5 的结论：specify 延迟只是占位，真实时序来自 SDF 反标；所以门级仿真主要验证**功能与结构**，不能拿它做时序判断。

#### 4.2.4 代码实践

1. **实践目标**：把一个 8 位计数器综合映射到 H7CR，拿到 `stat` 面积报告与门级网表。
2. **操作步骤**：

   先写 RTL（**示例代码**，非仓库原有文件），存为 `counter8.v`：

   ```verilog
   `timescale 1ns/1ps
   module counter8 (
       input  wire       clk,
       input  wire       rst_n,   // 低有效异步复位 → 期望映射到 DFFRX 系列
       output wire [7:0] count,
       output wire       co       // 计满指示
   );
       reg [7:0] cnt;
       assign count = cnt;
       assign co    = (cnt == 8'hFF);
       always @(posedge clk or negedge rst_n) begin
           if (!rst_n) cnt <= 8'h0;
           else        cnt <= cnt + 8'd1;
       end
   endmodule
   ```

   再写 yosys 脚本 `synth_h7cr.ys`（**示例代码**，`<lib路径>` 用 4.1.4 找到的真实路径替换）：

   ```text
   # ① 单元目录
   read_liberty -lib <lib路径>
   # ②③ 读 RTL + 工艺无关综合
   read_verilog counter8.v
   hierarchy -check -top counter8
   synth -top counter8 -flatten
   # ④⑤ 两步映射：先触发器后组合
   dfflibmap -liberty <lib路径>
   abc -liberty <lib路径>
   clean
   # ⑦⑧ 报告与网表
   stat -liberty <lib路径>
   write_verilog -noattr counter8_netlist.v
   ```

   运行 `yosys synth_h7cr.ys`（或 `yosys -s synth_h7cr.ys`）。
3. **需要观察的现象**：
   - 日志中 `dfflibmap` 一节应显示内部 FF 已映射（期望出现 `DFFRX*`，与 RTL 的异步复位写法对应）；
   - `stat` 报告的单元清单（下面是 stat 报告的**字段格式示意**，具体单元与数值待本地验证）：

   ```text
   === counter8 ===
   Number of wires:                 ...
   Number of cells:                 ...
     DFFRX1H7R                       8
     INVX1H7R                        ...
     XOR2X1H7R                       ...
     ...
   Chip area for module '\counter8': ... 
   ```
4. **预期结果**：8 个异步复位触发器 + 若干组合门；`Chip area` 为按 liberty 面积加权的总面积。用 `grep -oP '^\s+\K[A-Z][A-Z0-9_]*H7R' counter8_netlist.v | sort | uniq -c` 统计网表单元，与 stat 报告互相对照；再把这些单元名逐一在 [cell_list](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/cell_list/ics55_LLSC_H7CR.txt) 里 `grep` 确认存在。yosys 与 liberty 均未随仓库提供，全流程**待本地验证**。
5. **corner 对比（若下载到多个 corner）**：复制脚本仅替换 `<lib路径>` 为另一 corner，重跑并比较两份 stat 的单元构成与总面积，观察驱动档配比变化；只有单一 corner 时记录现象，留待后续版本补做。

#### 4.2.5 小练习与答案

1. **练习**：把 RTL 的复位改成同步（`always @(posedge clk)` 内 `if (!rst_n)`），重跑综合，`dfflibmap` 的结果有什么变化？
   **答案**：库中没有带同步复位的触发器（cell_list 里 DFFR* 均为异步 RN/SN 结构，其 UDP 表在时钟无关行直接清零），yosys 会把同步复位折算成触发器 D 端前的组合逻辑（选择器/与门），映射成 DFFX/DFFQX 类无复位脚单元 + 额外的组合门。观察点：stat 里 DFFRX 消失、MUX/AND 类单元增加。
2. **练习**：为什么 `dfflibmap` 必须在 `abc -liberty` 之前？
   **答案**：ABC 是组合优化引擎，遇到不认识的时序单元会把网络切成多段、只在段内优化，甚至直接失败；先由 dfflibmap 把 FF 固定为库单元，abc 才能对 FF 之间的纯组合锥做整体重构与驱动选择。
3. **练习**：同样是 8 位计数，把 `co = (cnt == 8'hFF)` 改成 `co = cnt[7]`，stat 面积应如何变化？
   **答案**：`cnt[7]` 只是引出一根线，比较器的 8 输入与逻辑（约映射为 AND/AO 树）整体消失，组合单元数与面积明显下降；这也是「综合结果对 RTL 写法敏感」的最小实验。具体数值待本地验证。

### 4.3 门级网表后仿对接：让 PDK 的 Verilog 模型动起来

#### 4.3.1 概念说明

综合产出的 `counter8_netlist.v` 只是一张「单元名 + 连线」的表，本身没有任何行为。行为来自 PDK 的单元模型库 `ics55_LLSC_H7CR.v`。门级仿真 = **网表 + 模型库 + testbench** 三者一起编译运行。

理解这个模型库的三个钥匙（都可回溯 u3-l5）：

1. **`ifdef functional 双模式**：全文件 716 处 `` `ifdef functional ``。定义了这个宏，`specify` 块被跳过，得到零延迟的纯功能仿真；不定义，则占位延迟与时序检查生效。一种库文件，两种用法。
2. **specify 占位 + NOTIFIER 机制**：7439 处延迟全是 `(1.0,1.0)`，且触发器里有 `$setuphold`/`$width` 检查，违例时翻转 `NOTIFIER`，UDP 看到 NOTIFIER 变化就输出 x（见 [L15014-L15020](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v#L15014-L15020)）。因为 setup/hold 限值也是占位的 1.0ns，testbench 的时钟周期要留足裕量（例如 10ns 以上），否则会出现大面积 x。
3. **UDP 集中在文件尾部**：14 个 `primitive` 全部位于 [L44200-L44600](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v#L44200-L44600)，而引用它们的模块在前面（如 `DFFX1H7R` 在 L14987 就实例化了 `udp_dff`）。「先使用后定义」对多数仿真器可行，但个别工具/版本要求 UDP 先定义——下面实践给了不改动源文件的应对办法。

#### 4.3.2 核心流程

```text
A. RTL 仿真（参照基准）
   iverilog -o rtl.vvp counter8.v tb.v && vvp rtl.vvp
B. 门级仿真（功能模式，零延迟）
   iverilog -Dfunctional -o gate_func.vvp counter8_netlist.v tb.v ics55_LLSC_H7CR.v
   vvp gate_func.vvp
C. 门级仿真（占位延迟模式）
   iverilog -o gate_time.vvp counter8_netlist.v tb.v ics55_LLSC_H7CR.v
   vvp gate_time.vvp
D. 对比：A/B 的波形应当逐拍完全一致；
   C 的 count 比 B 晚 ~1ns（CK→Q 占位延迟）且组合路径再叠若干 1ns，
   每拍稳定值仍应一致；若出现 x，多半是撞上了占位 setup/hold。
```

#### 4.3.3 源码精读

文件骨架：每个单元模块都包着 `` `celldefine ``，时间单位统一为 1ns/1ps——见文件开头 [ics55_LLSC_H7CR.v:L17-L18](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v#L17-L18)（前面 L1-L15 是 Apache-2.0 头，u1-l1 讲过逐文件授权要求）。这也说明 specify 里的 `1.0` 是 1ns。

最简单的单元模型：TIEHI 在 [L57-L71](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v#L57-L71) 就是一句 `assign Z = 1'b1;`；天线二极管 ANT2 在 [L19-L33](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v#L19-L33) 是空模块——布线器为修天线效应插入 ANT 后，门仿不会因为缺模型而断。

组合单元看带条件路径延迟的样例 ADDFX1H7R（全加器）：[L95-L102](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v#L95-L102) 用 `xor`/`and`/`or` 门原语描述功能（计数器的进位链综合后就会落到这类单元）：

```verilog
module ADDFX1H7R (CO, S, A, B, CI);
output S, CO;
input A, B, CI;
  xor I0(S, A, B, CI);
  and I1(a_and_b, A, B);
  ...
```

其 specify 部分 [L107-L145](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v#L107-L145) 按 `if (B==1'b0 && CI==1'b1)` 等输入条件逐条列出 A/B/CI 到 S/CO 的弧——这正是 u3-l6 讲过的 liberty `when` 条件表在 Verilog 侧的镜像。

触发器的核心是 UDP：`DFFX1H7R` 全文见 [L14987-L15028](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v#L14987-L15028)，其中 udp_dff 的真值表在 [L44297-L44321](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v#L44297-L44321)。表里几行关键语义：

```verilog
// in  clk  clr_   set_  NOT  : Qt : Qt+1
   0  r   ?   1   ?   : ?  :  0 ; // 上升沿采 0
   1  r   1   ?   ?   : ?  :  1 ; // 上升沿采 1
   ?  ?   0   1   ?   : ?  :  0 ; // clr_=0：异步清零（与时钟无关 → 异步复位）
   ?  ?   ?   ?   *   : ?  :  x ; // NOTIFIER 变化（时序违例）→ 输出 x
```

第三行就是「DFFRX 的复位是异步的」在模型层面的证据；第四行是 setup/hold 违例传染为 x 的通路。UDP 段前还有 `$Id: udp_mux4.v` 风格的注释（[L44193-L44200](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v#L44193-L44200) 起），说明这批 UDP 取自成熟的通用 UDP 库，mux/锁存/带使能 DFF 各有对应原语（`udp_mux4`、`udp_edff`、`ipicg_latchsr` 等，供 ICG、锁存类单元使用）。

#### 4.3.4 代码实践

1. **实践目标**：同一 testbench 下对比 RTL 仿真与门级仿真，验证综合映射没有改变功能。
2. **操作步骤**：

   写 testbench `tb.v`（**示例代码**）：

   ```verilog
   `timescale 1ns/1ps
   module tb;
       reg clk = 0, rst_n = 0;
       wire [7:0] count;
       wire co;
       integer errors = 0, i;

       counter8 dut (.clk(clk), .rst_n(rst_n), .count(count), .co(co));
       always #5 clk = ~clk;              // 10ns 周期，给占位 setup/hold 留裕量

       initial begin
           $dumpfile("tb.vcd"); $dumpvars(0, tb);
           #12 rst_n = 1;
           for (i = 0; i < 300; i = i + 1) begin
               @(posedge clk);
               #2;                          // 等输出稳定后采样
               if (count !== ((i + 1) % 256)) begin
                   $display("MISMATCH @%0t count=%h expect=%h", $time, count, (i+1)%256);
                   errors = errors + 1;
               end
           end
           $display(errors == 0 ? "PASS" : "FAIL (%0d errors)", errors);
           $finish;
       end
   endmodule
   ```

   依次运行 4.3.2 的 A/B/C 三条命令。若 iverilog 报「Unknown module type: udp_dff」一类错误（UDP 先使用后定义所致，取决于工具版本），不改源文件，在自己的工作目录把模型库拆开编译（行号以 L44190 为界，即最后一个 `endmodule`/`` `endcelldefine `` 之后是 UDP 段）：

   ```bash
   sed -n '1,44189p' IP/.../ics55_LLSC_H7CR.v > h7cr_cells.v      # 模块段
   sed -n '44190,44600p' IP/.../ics55_LLSC_H7CR.v > h7cr_prims.v  # UDP 段
   iverilog -o gate.vvp h7cr_prims.v h7cr_cells.v counter8_netlist.v tb.v
   ```

3. **需要观察的现象**：
   - A（RTL）与 B（`-Dfunctional` 门级）：`PASS`，且波形中 count 与 RTL 完全同步翻转（零延迟）；
   - C（占位延迟门级）：count 的翻转比时钟沿滞后 1ns（CK→Q 占位延迟），再经组合逻辑逐级滞后 1ns/级，但在 `#2` 采样点处数值与 A 一致；若把时钟周期改小（如 `#2` 翻转），应能看到 x 从触发器冒出来（占位 `$setuphold` 的 NOTIFIER 机制）；
   - 用 GTKWave 打开两个 `.vcd` 对比：信号名从 RTL 的 `cnt` 变成了网表的中间连线名（`-flatten` 的效果），顶层端口名不变。
4. **预期结果**：A/B 波形逐拍一致、C 每拍稳定值一致 → 综合映射功能正确。iverilog 未随仓库提供，以上**待本地验证**；若 `$setuphold` 在你的 iverilog 版本上不支持或告警，以 B 模式结果为准并记录现象。
5. **可选延伸**：把网表里的任一单元（如某个 `INVX1H7R`）在 tb 里用层次名 `dut.<实例名>.Y` 强拉翻转做故障注入，观察 RTL 仿真与门仿的分歧——这是门级仿真「能看到物理实现」的价值演示。

#### 4.3.5 小练习与答案

1. **练习**：门级功能仿真（B）已经 PASS 了，为什么还要跑占位延迟模式（C）？它还能暴露什么问题？
   **答案**：C 让延迟非零，能暴露「仿真对延迟敏感」的结构性错误——组合环（零延迟下可能振荡或被优化掉，带延迟后必然振荡）、复位释放时刻的恢复/去除行为、以及 testbench 采样点是否依赖零延迟。至于真实时序，仍需 SDF 反标 + 真实 corner，占位 1ns 只提供「非零」这个语义。
2. **练习**：网表里出现 `ANT2H7R`（布线后插的天线二极管，综合阶段不会有）时，门仿为什么不会报错？
   **答案**：模型库在 [L19-L33](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v#L19-L33) 为它提供了空模块（端口 `inout A`，无逻辑），u3-l4 讲过这类单元无逻辑功能，空模型恰好匹配。
3. **练习**：试着用 yosys 做形式等价性检查（`equiv_make` + `equiv_induct` + `equiv_status`）代替仿真对比，可行吗？
   **答案**：思路可行——RTL 设计与门级网表分别读入，单元行为来自 liberty 的 function 属性（或以 `read_verilog -lib` 读模型库建黑盒），再构造等价比较点。但触发器映射、三态、UDP 等边界情况对流程要求较高，建议先在纯组合模块上尝试；具体命令组合与版本支持**待本地验证**。

## 5. 综合实践

把本讲三段串成一个可复用的 mini-flow 脚本 `run_syn.sh`（**示例代码**，放自己的工作目录，不写入仓库）：

```bash
#!/usr/bin/env bash
set -euo pipefail
LIB=$(find IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/liberty -name '*.lib' | head -1)
echo "using liberty: $LIB"
[ -n "$LIB" ] || { echo "先运行 make unzip 下载 liberty"; exit 1; }

yosys -p "
read_liberty -lib $LIB
read_verilog counter8.v
hierarchy -check -top counter8
synth -top counter8 -flatten
dfflibmap -liberty $LIB
abc -liberty $LIB
clean
stat -liberty $LIB
write_verilog -noattr counter8_netlist.v"

# 门级功能仿真
iverilog -Dfunctional -o gate.vvp counter8_netlist.v tb.v \
         IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/verilog/ics55_LLSC_H7CR.v
vvp gate.vvp
```

完成后再做两件事：

1. **单元溯源**：从 `counter8_netlist.v` 统计用到的单元（`grep -oP '^\s+\K[A-Z][A-Z0-9_]*H7R' | sort | uniq -c`），对每个单元回到本讲引用的模型源码定位它的 module（`grep -n "^module <单元名>" ics55_LLSC_H7CR.v`），确认 abc 选中的每一档驱动都有模型、都在 cell_list 里。
2. **换库重跑**：把 `<lib路径>` 换成 H7CH（或 H7CL）的 liberty，重跑并 diff 两份 stat——三阈值库单元名只差后缀，网表结构应当几乎相同（多阈值综合的意义在 u3-l1 讲过：关键路径换 LVT、松路径换 HVT 是更上层的策略，单一库内的 abc 折衷是它的底层机制）。

## 6. 本讲小结

- 综合流水线 = `read_liberty -lib`（单元目录）→ `synth`（工艺无关）→ `dfflibmap -liberty`（先映射触发器）→ `abc -liberty`（再映射组合并选驱动档）→ `stat -liberty`（面积）→ `write_verilog`；liberty 由 `make unzip` 从 Release 解压获得，内部文件名待确认，用 `find` 取真。
- corner 决定延迟表，延迟表决定 abc 的面积/延迟折衷点；换 corner 重跑，stat 的单元配比与总面积随之变化——综合必须用与签核一致的 corner。
- 面积公式 \( A_{chip} = \sum_i n_i a_i \) 由 `stat -liberty` 自动汇总；网表单元应能同时回溯到 cell_list（存在性）与 `ics55_LLSC_H7CR.v`（模型）。
- 门级仿真 = 网表 + PDK 模型库 + testbench；`-Dfunctional` 得零延迟功能仿真，默认模式启用占位延迟与 `$setuphold`/`$width`（NOTIFIER 违例输出 x），7439 处 `(1.0,1.0)` 占位延迟说明真实时序要靠 SDF 反标。
- 模型库的触发器行为由文件尾的 14 个 UDP 承载（`udp_dff` 真值表里的 clr_/set_ 行就是异步复位的证据）；若工具不接受「UDP 先使用后定义」，用 `sed` 按行号把模型段与 UDP 段拆开编译，无需改源文件。

## 7. 下一步学习建议

- **下一讲 u6-l3（_ecos 变体设计与二次开发贡献）**：本讲综合网表里出现的每个单元，在 _ecos 版 LEF 里都有对应的电源轨道与高层引脚适配——学完综合再回头看 _ecos 为开源工具链做了什么，闭环就完整了。
- **接续物理设计**：把本讲的 `counter8_netlist.v` + H7CR liberty + tech/cell LEF 交给 OpenROAD（`read_liberty`/`read_lef`/`read_verilog` + `initialize_floorplan`），延续 u6-l1 的装载实验走向布局布线。
- **继续阅读源码**：`ics55_LLSC_H7CR.v` 中 ICG 系列模型（L15584 起，用 `ipicg_latchsr`/`udp_dff` 组合实现时钟门控）与 SDFF 系列（扫描链触发器，L37903 起），对比普通 DFF 的模型差异，理解可测性设计（DFT）单元的建模方式。
- **工具文档**：yosys 手册中 `dfflibmap`、`abc`、`stat -liberty` 与 `write_verilog` 章节；iverilog 手册中 UDP 支持与 specify/时序检查支持范围。
