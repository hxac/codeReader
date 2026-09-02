# 多视图一致性检查

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「同一单元在 cell_list、LEF、verilog、CDL、liberty 五种视图里各以什么身份出现」，并能画出 H7CH 库的「单元 × 视图」矩阵。
2. 解释为什么各视图的单元集合与引脚集合**并不完全相等**，并能列出真实存在的差异清单（谁多、谁少、多了谁）。
3. 编写一个不依赖任何商业 EDA 工具的 Python 脚本，自动完成「单元 × 视图」和「引脚 × 视图」两份一致性报告。
4. 掌握各视图的电源引脚约定（LEF 的 `USE POWER`、CDL 的 `VDD:B`、verilog 的「无电源端口」、liberty 的可选电源建模），知道做引脚核对时必须先剥离电源引脚再比较。
5. 对「CDL 比 cell_list 多出的子电路」结合命名规律推测用途，并养成标注「待确认」的习惯。

## 2. 前置知识

- **视图（view）**：同一份电路在不同 EDA 工具眼里的化身。u1-l2 讲过每库七个固定子目录；本讲只用到其中五个文本视图：`cell_list`（单元名单）、`lef`（物理抽象）、`verilog`（仿真模型）、`cdl`（晶体管网表）、`liberty`（时序功耗模型）。
- **金标准（golden）问题**：五个视图没有一个天然是「标准答案」——cell_list 最像目录，但我们会实测发现它既不是 LEF 的子集也不是 CDL 的子集。一致性检查的第一步就是选定参照系，通常是 cell_list ∪ LEF。
- **集合语言**：本讲大量用「交集 / 差集」描述视图关系，例如「CDL − cell_list = 38 个额外单元」。这只是一句话的集合运算，不是新语法。
- **需要回忆的旧知识**：LEF 的 `MACRO/PIN/DIRECTION/USE` 结构（u3-l2）；verilog 模块的 `input/output/inout` 端口声明与 `ifdef functional`（u3-l5）；liberty 的 `cell()/pin()/direction` 与 corner 命名（u3-l6）；CDL 的 `.SUBCKT` 与 `*.PININFO` 方向标记 `I/O/B`（u5-l1）。
- **为什么这件事重要**：多阈值库里 748 个单元 × 5 个视图 = 数千个可能对不上的点。综合器拿 liberty 认单元、布线器拿 LEF 认引脚、LVS 拿 CDL 认端子——任何一处名字对不上，流程就在那个工具手里断掉。人工逐个对是不可能的，所以「写脚本做核对」本身就是 PDK 使用者的必备技能。

## 3. 本讲源码地图

| 文件 | 视图 | 本讲关注点 |
| --- | --- | --- |
| `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt` | 单元名单 | 748 行、每行一个单元名，作为参照系 |
| `IP/STD_cell/.../ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef` | 物理抽象 | 785 个 MACRO、5069 个 PIN，含 VDD/VSS 电源引脚 |
| `IP/STD_cell/.../ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v` | 仿真模型 | 751 个 module，端口不含电源 |
| `IP/STD_cell/.../ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl` | 晶体管网表 | 1174 个 `.SUBCKT`（791 个唯一名），端口含 VDD/VSS |
| `IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib` | 时序库 | git 内唯一的 liberty 样本，12 个 cell |
| `IP/IO/.../lef/ICSIOA_N55_3P3_1P6M1TM.lef` / `verilog/icsIOA_N55_3P3.v` / `cdl/ICSIOA_N55_3P3.cdl` | IO 三视图 | 五视图全对照的最佳样本（liberty 不缺） |
| `Makefile` | 构建 | 标准单元 liberty 需 `make unzip` 下载，文件名待确认 |

> 提示：H7CH 还有 `_ecos.lef` 与 `_ant.lef` 两个变体文件。本讲实测三者 MACRO 数（785）、PIN 数（5069）、`SHAPE ABUTMENT` 数（1570）完全一致——变体只改几何与属性，不改单元名/引脚名集合，因此做名字级核对时任选其一即可。

## 4. 核心概念与源码讲解

### 4.1 多视图单元对照

#### 4.1.1 概念说明

「单元对照」回答的问题是：**每个视图里到底住着哪些单元？**

理想 PDK 里五个视图应该有完全相同的单元集合。现实是：每个视图由不同工具、不同流水线生成，生成范围由各自的需求决定——

- `cell_list` 是「手册目录」，倾向只列**用户可例化**的单元；
- LEF 服务布局布线，**物理上存在**的单元（包括填充、天线二极管）都得有；
- verilog 只需要**有逻辑行为**的单元，纯物理单元（FILLCAP 电容填充）可以没有；
- CDL 服务 LVS，**版图里画了的**都必须有网表；
- liberty 只需要**有时序/功耗意义**的单元。

于是「谁比谁多」不是错误，而是**视图职责不同的自然投影**——但多出来的具体名字必须能被解释，解释不了的就是真 bug。这就是一致性检查的价值：把「可解释的差异」和「不可解释的差异」分开。

#### 4.1.2 核心流程

对每个视图提取「单元名集合」，再做集合运算：

```text
S_cl  = { cell_list 每行去掉空白 }
S_lef = { LEF 中每条 "MACRO <名>" 的 <名> }
S_v   = { verilog 中每条 "module <名>" 的 <名> }
S_cdl = { CDL 中每条 ".SUBCKT <名>" 的 <名> }（注意要去重！）

报告 1：单元 × 视图矩阵
  每个视图：总数、∩cell_list 数、−cell_list 数（多出）、cell_list−视图 数（缺失）
报告 2：差集明细
  LEF−cell_list、verilog−cell_list、CDL−cell_list、cell_list−verilog …逐个列名
```

提取的正则与去重逻辑是本讲脚本的核心，其中 CDL 的坑最多：模板子电路（`INV`、`TG`…）会在文件里**重复定义上百次**，直接数 `.SUBCKT` 会得到 1174 而不是真实的 791。

#### 4.1.3 源码精读

**cell_list：一行一个名字，无任何注释和字段。** 开头是加法器家族：

- [cell_list/ics55_LLSC_H7CH.txt:L1-L6](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L1-L6) —— 全文件 748 行非空记录，从 `ADDFX1H7H` 到 `XOR3X6H7H`。解析时只需 `strip()` 后丢弃空行。

**LEF：每个单元是一个 MACRO 块。**

- [lef/ics55_LLSC_H7CH.lef:L19-L25](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L19-L25) —— `MACRO ADDFX1H7H` 头部：`CLASS CORE`、`SIZE 4.8 BY 1.4`、`SITE core7`。全文件 785 个 MACRO。
- 用 `grep -c "^MACRO "` 实测：785 = 748（cell_list 全部在场）+ 37 个额外，额外者包括 [lef/ics55_LLSC_H7CH.lef:L3048](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L3048) 的天线二极管 `ANT2H7H`、FILLER 填充家族与一批触发器变体（明细见下面矩阵）。

**verilog：每个单元是一个 module，外面包着 `celldefine`。**

- [verilog/ics55_LLSC_H7CH.v:L17-L19](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L17-L19) —— `timescale` + `celldefine` + `module ANT2H7H ( A);`，全文件 751 个 module。
- 751 = 731（cell_list 中有模型的）+ 20 个额外（ANT/EDFF/MDFF/SDFF 变体），同时 cell_list 有 17 个单元**没有** verilog 模型。

**CDL：`.SUBCKT` 既是单元也是模板，且模板重复定义。**

- [cdl/ics55_LLSC_H7CH.cdl:L21-L25](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L21-L25) —— 文件最开头是库名 `ICSCORE` 的反相器模板 `.SUBCKT INV A VDD VSS Y`，形参 `nw/nl/pw/pl` 供单元传参展开（u5-l1 详述）。
- 实测 `.SUBCKT` 共 1174 条，**去重后 791 个唯一名**：`TG` 出现 184 次、`TSINV` 176 次、`NAND2` 22 次、`NOR2` 5 次（这些是各单元块内重复声明的本地模板），`INV` 只在文件头出现 1 次。791 = 748（cell_list 全部在场）+ 38 个额外单元 + 5 个模板名。

把上述实测数字汇成矩阵（本讲作者在 68d89ed 提交上用 `grep`/`sort`/`uniq` 等价命令逐一核对过，读者脚本应复现同样数字）：

| 视图 | 记录数 | 唯一单元数 | 比 cell_list 多 | 比 cell_list 少 |
| --- | --- | --- | --- | --- |
| cell_list | 748 | 748 | — | — |
| LEF | 785 MACRO | 785 | +37 | 0 |
| verilog | 751 module | 751 | +20 | **−17** |
| CDL | 1174 `.SUBCKT` | **791**（须去重） | +38 单元、+5 模板名 | 0 |
| liberty | — | 待下载（见 4.1.4） | 待确认 | 待确认 |

三个差集的明细（全部实测）：

- **LEF 独有的 37 个**：`ANT2/ANT4`；`FILLER1/2/4/8/16/32/64 + FILLTAP`（8 个填充）；27 个触发器变体——`DFFNSRQX1/2`、`EDFFQX0P5/1/2`、`MDFFQX0P5/1/2`、`MSDFFQX0P5/1/2/3`、`SDFFNQX1/2/3`、`SDFFNSRX0P5/1`、`SDFFNX1/2/3`、`SDFFRX1/2/3`、`SDFFSQX1/2/3`、`SDFFSRQX3`。
- **verilog 独有的 20 个**：`ANT2/ANT4`、`EDFFQX0P5/1/2`、`MDFFQX0P5/1/2`、`SDFFNQX1/2/3`、`SDFFNX1/2/3`、`SDFFRX1/2/3`、`SDFFSQX1/2/3`（是 LEF 独有 37 的子集，即「有 LEF 但 cell_list 没登记」的那批里，凡有逻辑行为的都有模型）。
- **cell_list 有、verilog 没有的 17 个**：`FILLCAP4/8/16/32`（电容填充，无逻辑行为）、`DFFNSRX0P5/1/2`、`DFFSRX0P5/1/2`、`DFFSRQX1/2`、`SDFFSRQX1/2`、`SDFFSRX0P5/1/2`（带置位复位的 DFF 家族缺失模型——这批**在 LEF/CDL 里都存在**，因此判断是 verilog 模型生成范围的问题，而不是单元不存在，用途待确认）。
- **CDL 独有的第 38 个**：`SDFFRQX3H7H` 只出现在 CDL（LEF、verilog、cell_list 都没有），见 [cdl/ics55_LLSC_H7CH.cdl:L19294](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L19294)。

#### 4.1.4 代码实践

**实践目标**：不写 Python，先用三条 shell 命令亲手复现上表中最关键的三个数字（785 / 751 / 791），确认「CDL 要去重」这个坑真实存在。

**操作步骤**：

```bash
# 1. LEF 的 MACRO 数
grep -c "^MACRO " IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef
# 2. verilog 的 module 数
grep -c "^module " IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v
# 3. CDL 的 .SUBCKT 总数 与 去重后的唯一名数
grep -c "^\.SUBCKT" IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl
grep -o "^\.SUBCKT [^ ]*" IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl | sort -u | grep -c SUBCKT
# 4.（可选）看是哪些名字在重复
grep -o "^\.SUBCKT [^ ]*" IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl | sort | uniq -c | sort -rn | head
```

**需要观察的现象**：第 3 步两个数字相差 383；第 4 步头部是 `TG` 184 次、`TSINV` 176 次、`NAND2` 22 次、`NOR2` 5 次。

**预期结果**：785 / 751 / 1174 与 791。以上命令本讲作者已在仓库内实际运行并得到上述输出。

**关于标准单元 liberty**：`make unzip` 会把 [Makefile:L11-L13](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L11-L13) 列出的三个 `*_liberty.tar.bz2` 按 [Makefile:L22-L23](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L22-L23) 的 `patsubst` 规则解压到 `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/<库名>/liberty/`（解压规则见 [Makefile:L62-L66](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L62-L66)）。tar 包内部的 `.lib` 文件名**待确认**——下载前无法从仓库推知，下载后用 `ls IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/liberty/` 查看，再纳入第 5 节的脚本对比。

#### 4.1.5 小练习与答案

**练习 1**：为什么 verilog 视图比 cell_list 少的 17 个单元里，恰好包含全部 4 个 FILLCAP？而 FILLER 家族（LEF 独有）也全部没有 verilog 模型？

**答案**：FILLCAP 是纯电容填充、FILLER 是纯几何填充，二者都没有逻辑功能，功能仿真用不到模型；区别在于 FILLER 连 cell_list 都没登记（纯物理单元），而 FILLCAP 登记在 cell_list 里（可能是希望用户知道它可以例化），于是 FILLER 不会出现在「cell_list−verilog」差集中，FILLCAP 会。

**练习 2**：如果直接把 1174 当作 CDL 的单元数写进报告，会得出什么错误结论？

**答案**：会以为 CDL 比 cell_list 多出 426 个子电路（1174−748），并把 `TG`/`TSINV` 等**模板**误判为 426 个「神秘单元」中的重复项。去重后才能看到真实差异只有 38 个额外单元 + 5 个模板名。

**练习 3**：`SDFFRQX3H7H` 只在 CDL 出现，列出你要做的三步排查。

**答案**：① 用 `grep` 确认它确实不在 cell_list/LEF/verilog 三处（本讲已实测为 0 次）；② 在 CDL 里读它的端口与器件行，确认它是一个完整可用的子电路而非残缺记录；③ 对比同名 X0P5/X1/X2 档位（`SDFFRQX0P5/1/2` 在 cell_list 第 673-675 行且四视图齐全），推测 X3 档是「其他视图生成时漏掉的一档」——这是推测，交付原因待确认，应标注后向 PDK 维护方求证。

### 4.2 引脚名与方向核对

#### 4.2.1 概念说明

单元对上之后，第二步是**引脚级**核对：同一个单元，各视图给的引脚名集合、输入输出方向是否一致？

这里有一条必须先建立的预期，否则会把正常现象误报为错误：

- **信号引脚**（A、B、CI、CO、S…）在五个视图里应当同名、同方向——这是硬约束，违反即真 bug；
- **电源引脚**（VDD/VSS）的「出场方式」各视图不同：LEF 必有、CDL 必有、verilog 通常没有、liberty 视库而定（见 4.4）；
- **方向语义**有细微差别：LEF 的 `DIRECTION` 描述物理连接方向，liberty 的 `direction` 描述电学行为，verilog 的 `input/output/inout` 描述仿真端口——绝大多数单元三者可互译，但个别单元（下文的 ANT2、PAR）确实存在矛盾。

#### 4.2.2 核心流程

```text
对每个"四视图共有的单元" c：
  P_lef(c) = { PIN 名 }                     # 含 VDD/VSS
  P_v(c)   = { module 端口名 }              # 通常不含 VDD/VSS
  P_cdl(c) = { .SUBCKT 端口表 }             # 含 VDD/VSS，位于表尾
  比较 P_lef(c)−{VDD,VSS} 与 P_v(c)：应相等
  比较 P_lef(c) 与 P_cdl(c)：应相等（含电源）
  方向核对：LEF DIRECTION ↔ verilog in/out ↔ CDL *.PININFO 的 I/O/B
报告：引脚缺失 / 引脚多余 / 方向冲突 三张表
```

#### 4.2.3 源码精读

以 `ADDFX1H7H`（全加器）为标本走一遍：

**LEF 侧 7 个引脚**：信号脚 A、B、CI 为 `DIRECTION INPUT`（[lef:L26-L33](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L26-L33) 是 A），CO、S 为 `DIRECTION OUTPUT`；电源脚 [lef:L75-L87](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L75-L87) `PIN VDD` 为 `DIRECTION INOUT; USE POWER; SHAPE ABUTMENT`，[lef:L88-L100](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L88-L100) `PIN VSS` 同构。

**verilog 侧 5 个端口**：[verilog:L95-L97](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L95-L97) 声明 `module ADDFX1H7H (CO, S, A, B, CI)`，`output S, CO; input A, B, CI`。**端口顺序与 LEF 不同、无电源端口——两者都是合法差异**；名字集合与 LEF 剥离电源后完全一致，方向也一一对应。

**CDL 侧 7 个端子**：[cdl:L33-L34](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L33-L34) `.SUBCKT ADDFX1H7H A B CI CO S VDD VSS`，`*.PININFO A:I B:I CI:I CO:O S:O VDD:B VSS:B`——信号方向 I/O 与 LEF/verilog 一致，电源标 `B`（bidirectional）。电源排在端口表**尾部**是全库统一惯例，解析时可据此自动剥离。

再给两个**真实的方向冲突**，这是本讲最想让你带走的发现：

- **ANT2H7H**：LEF 里引脚 A 是 `DIRECTION INPUT`（[lef:L3055-L3057](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L3055-L3057)）；verilog 里却是 `inout A`（[verilog:L19-L20](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L19-L20)）。天线二极管在物理上「接信号线、另一端接地」，按 INPUT 或 inout 各有道理，但两个视图不一致会让方向核对脚本报警——应当归档为「已知差异」而非视而不见。
- **IO 库 P65_1233_PAR**：liberty 中 A 与 PAD 都是 `direction : "inout"`（[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib:L145-L158](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L145-L158)）；普通版 IO LEF 中 A 是 `DIRECTION INPUT`（[ICSIOA_N55_3P3_1P6M1TM.lef:L53576-L53578](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L53576-L53578)），PAD 也标 `DIRECTION INPUT; USE SIGNAL`（[L53840-L53842](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L53840-L53842)）。u4-l2 提过 `_ecos` 版修正了 PBMUX 的 C、PWE 的 XC 两处方向，但 PAR 的这两处 INPUT 与 liberty 的 inout 仍不一致——正是「脚本要能抓出残留问题」的活例子。

#### 4.2.4 代码实践

**实践目标**：对 `ADDFX1H7H` 和 `ANT2H7H` 手工完成一次三视图引脚核对，体会「电源剥离」与「方向翻译」两个步骤。

**操作步骤**：

1. `grep -n "^MACRO ADDFX1H7H\|^MACRO ANT2H7H" .../lef/ics55_LLSC_H7CH.lef` 定位两个宏；
2. 分别读宏内所有 `  PIN <名>` 与其后的 `DIRECTION` 行；
3. `grep -n "^module ADDFX1H7H\|^module ANT2H7H" .../verilog/ics55_LLSC_H7CH.v`，读模块端口表与其下的 `input/output/inout` 声明；
4. `grep -n "^\.SUBCKT ADDFX1H7H\|^\.SUBCKT ANT2H7H" .../cdl/ics55_LLSC_H7CH.cdl`，抄下端口表与 `*.PININFO` 行；
5. 画三列对照表：LEF / verilog / CDL，电源引脚单独一行。

**需要观察的现象**：ADDFX1H7H 三视图信号脚完全一致（A,B,CI 入；CO,S 出）；ANT2H7H 的 A 在 LEF 是 INPUT、在 verilog 是 inout；CDL 两个单元的端口表都以 `VDD VSS` 结尾。

**预期结果**：得到与 4.2.3 相同的结论。以上 grep 命令均可在仓库直接运行（本讲作者已实测），无需任何 EDA 工具。

#### 4.2.5 小练习与答案

**练习 1**：verilog 模块端口顺序与 LEF 引脚顺序不同（ADDFX1H7H 一个是 `CO,S,A,B,CI` 一个是 `A,B,CI,CO,S`），一致性检查要不要报错？

**答案**：不要。Verilog 端口按位置/名字绑定，LEF 引脚按名字引用，两个视图都不依赖对方顺序；核对只看**名字集合**与**方向**。

**练习 2**：写出「LEF 方向 ↔ verilog 方向 ↔ CDL PININFO」的翻译表。

**答案**：`INPUT ↔ input ↔ I`；`OUTPUT ↔ output ↔ O`；`INOUT ↔ inout ↔ B`。电源脚在 CDL 标 `B`、LEF 标 `INOUT`，verilog 无对应端口，需先从比较集合中剔除。

**练习 3**：如果一个单元在 liberty 里有引脚 `CLK`，LEF 里却叫 `CK`，后果是什么？

**答案**：综合器（读 liberty）例化的实例引脚名与布线器（读 LEF）认识的引脚名对不上，布局布线阶段该实例的连接会丢失或报 unknown pin。这是必须在 PDK 发布前抓出的第一类错误——本讲 H7CH 的 liberty 尚未下载，无法实测，属待确认项；IO 库经抽查未发现此类问题。

### 4.3 自动检查脚本

#### 4.3.1 概念说明

前两节的对照都是「点」上的手工核对；真实 PDK 有 748+ 个单元、5000+ 个引脚，必须自动化。好消息是：五种视图都是**行导向的文本**，用「逐行正则 + 状态机」就足以提取结构，不需要真正的 LEF/CDL/liberty 解析器。

脚本设计的三个原则：

1. **提取器分离**：每个视图一个 `parse_xxx(path) -> dict[单元, dict[引脚, 方向]]`，主流程只消费统一的数据结构；
2. **报告分级**：`ERROR`（信号脚缺失/多余/方向冲突）、`WARN`（电源脚不一致、CDL 重复定义）、`INFO`（单元级差集，多数可解释）；
3. **白名单机制**：像 ANT2 的 INPUT/inout 这类「已知差异」写进豁免表，让报告聚焦新问题。

#### 4.3.2 核心流程

```text
读 cell_list → S_cl
解析 LEF     → S_lef, pins_lef{cell:{pin:dir}}
解析 verilog → S_v,   pins_v{cell:{pin:dir}}
解析 CDL     → S_cdl, pins_cdl{cell:{pin:dir}}（同名 .SUBCKT 取首次定义并记 WARN）
解析 liberty → S_lib, pins_lib（文件存在才做）

单元报告：每单元一行，五个布尔列（在哪个视图出现），
          按出现组合分组统计
引脚报告：对 ∩ 视图的每单元：
          sig = pins − {VDD,VSS}
          比较各视图 sig 的键集合 → 缺失/多余
          比较方向（经 4.2.5 翻译表）→ 冲突
输出：Markdown 表 + 退出码（有 ERROR 则非零，可挂 CI）
```

#### 4.3.3 源码精读

脚本要正确处理的真实格式样本（均已在前文出现，这里集中列出解析锚点）：

- LEF 引脚块结构：`PIN <名>` → `DIRECTION <方向> ;` → `USE <用途> ;`，如 [lef/ics55_LLSC_H7CH.lef:L26-L33](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L26-L33)；`MACRO` 与 `END <名>` 之间属于同一单元。
- verilog 端口声明可能挤在同一行（[verilog:L96-L97](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L96-L97) 的 `output S, CO;`），也可能如 [verilog:L20](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L20) 一行一个 `inout A;`——正则要兼容逗号分隔。
- CDL 的 `*.PININFO` 是全库唯一的方向权威（[cdl:L34](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L34)）；重复定义的模板只保留第一次（[cdl:L21](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L21) 的 INV）。
- liberty 的 `cell ("名")` 与 `pin (名) { direction : "x"; }` 缩进不固定——[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib:L141-L159](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L141-L159) 的 cell 块内 `direction` 用空格缩进（L1079），别把缩进写死成正则的一部分。

#### 4.3.4 代码实践

**实践目标**：把下面这份「最小可用版」脚本（示例代码，非项目自带）保存为 `check_pdk.py` 跑通，得到 H7CH 的两份报告。

**操作步骤**：

1. 在仓库根目录新建 `check_pdk.py`（这是你自己的练习文件，不要提交进仓库）；
2. 粘贴以下代码：

```python
#!/usr/bin/env python3
# 示例代码：H7CH 多视图一致性检查（单元×视图 + 引脚×视图）
import re, sys
from collections import defaultdict

BASE = "IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/"
VIEWS = ["cell_list", "lef", "verilog", "cdl"]      # liberty 存在时自动加入
POWER = {"VDD", "VSS"}
DIRMAP = {"INPUT": "in", "OUTPUT": "out", "INOUT": "bidir",
          "input": "in", "output": "out", "inout": "bidir",
          "I": "in", "O": "out", "B": "bidir"}

def parse_cell_list(p):
    return {n: {} for n in map(str.strip, open(p)) if n}

def parse_lef(p):
    cells, cell, pin = {}, None, None
    for line in open(p):
        t = line.split()
        if not t: continue
        if t[0] == "MACRO":  cell = t[1]; cells[cell] = {}
        elif t[0] == "PIN" and cell: pin = t[1]; cells[cell][pin] = None
        elif t[0] == "DIRECTION" and pin: cells[cell][pin] = DIRMAP[t[1]]
        elif t[0] == "END" and cell and t[1:] == [cell]: cell = None
    return cells

def parse_verilog(p):
    cells, cell = {}, None
    for line in open(p):
        m = re.match(r"\s*module\s+(\S+)\s*\(([^)]*)\)", line)
        if m: cell = m.group(1); cells[cell] = {}; continue
        m = re.match(r"\s*(input|output|inout)\s+(.+);", line)
        if m and cell:
            for n in m.group(2).split(","):
                cells[cell][n.strip()] = DIRMAP[m.group(1)]
    return cells

def parse_cdl(p):
    cells, cell, dup = {}, None, []
    for line in open(p):
        t = line.split()
        if t[:1] == [".SUBCKT"]:
            cell = t[1]
            if cell in cells: dup.append(cell)      # 模板重复定义，取首次
            else: cells[cell] = {n: None for n in t[2:]}
        elif t[:1] == ["*.PININFO"] and cell:
            for nv in t[1:]:
                cells[cell][nv.split(":")[0]] = DIRMAP[nv.split(":")[1]]
        elif t[:1] == [".ENDS"]: cell = None
    print(f"[WARN] CDL 重复定义 {len(dup)} 处（模板），已按首次定义去重")
    return cells

def parse_liberty(p):
    cells, cell, pin = {}, None, None
    for line in open(p):
        m = re.match(r'\s*cell\s*\("([^"]+)"\)', line)
        if m: cell = m.group(1); cells[cell] = {}; continue
        m = re.match(r"\s*pin\s*\(([^)]+)\)", line)
        if m and cell: pin = m.group(1); cells[cell][pin] = None; continue
        m = re.match(r'\s*direction\s*:\s*"(\w+)"', line)
        if m and pin: cells[cell][pin] = DIRMAP[m.group(1)]
    return cells

def main():
    views = {"cell_list": parse_cell_list(BASE+"cell_list/ics55_LLSC_H7CH.txt"),
             "lef":       parse_lef(BASE+"lef/ics55_LLSC_H7CH.lef"),
             "verilog":   parse_verilog(BASE+"verilog/ics55_LLSC_H7CH.v"),
             "cdl":       parse_cdl(BASE+"cdl/ics55_LLSC_H7CH.cdl")}
    try:
        import glob
        libs = glob.glob(BASE+"liberty/*.lib")
        if libs: views["liberty"] = parse_liberty(libs[0])
    except OSError:
        print("[INFO] 标准单元 liberty 未下载（make unzip），跳过该视图")

    print("\n== 单元 × 视图 ==")
    print(f"{'视图':<10}{'单元数':>8}")
    for v, d in views.items(): print(f"{v:<10}{len(d):>8}")
    union = set().union(*views.values())
    for v in views:
        extra = union - set(views[v]) if v != "cell_list" else set()
        miss  = set(views[v]) - set(views["cell_list"]) if v != "cell_list" else set()
        print(f"[{v}] 比 cell_list 多 {len(miss)}: {sorted(miss)[:6]}{'...' if len(miss)>6 else ''}")
        cl_only = set(views['cell_list']) - set(views[v])
        print(f"[{v}] 缺 cell_list 中的 {len(cl_only)}: {sorted(cl_only)[:6]}{'...' if len(cl_only)>6 else ''}")

    print("\n== 引脚 × 视图（信号脚，已剔除 VDD/VSS）==")
    common = set(views["lef"]) & set(views["verilog"]) & set(views["cdl"])
    bad = 0
    for c in sorted(common):
        refs = {v: {p for p in views[v][c] if p not in POWER}
                for v in ("lef", "verilog", "cdl")}
        base = refs["lef"]
        for v, ps in refs.items():
            if ps != base:
                bad += 1
                print(f"[ERROR] {c}: {v} 信号脚 {sorted(ps)} != LEF {sorted(base)}")
        if bad >= 20: print("...（截断）"); break
    if bad == 0:
        print(f"全部 {len(common)} 个共有单元信号脚一致（预期内：电源脚差异已剥离）")

if __name__ == "__main__":
    sys.exit(main())
```

3. 运行 `python3 check_pdk.py`。

**需要观察的现象**：单元×视图部分的四个数字（748 / 785 / 751 / 791）与 4.1.3 矩阵一致；CDL 去重 WARN 打印出 383 处重复；引脚×视图部分若打印 ERROR，注意它是不是 ANT2 的 INPUT/inout 类方向差异（上面最小版只比名字，方向比较可作为进阶练习加入）。

**预期结果**：`cell_list` 行「多 0 缺 0」；`lef` 多 37；`verilog` 多 20、缺 17；`cdl` 多 43（38 单元 + INV/NAND2/NOR2/TG/TSINV 五个模板名）。引脚报告对共有单元应全绿——因为名字级一致（方向冲突需扩展脚本才能抓到，正是练习 3 的任务）。这些数字本讲作者已用 grep 等价命令实测，若你的脚本输出不同，先检查 CDL 去重与 verilog 多端口行解析。

#### 4.3.5 小练习与答案

**练习 1**：给脚本加上「方向冲突」检测（提示：把 `DIRMAP` 后的三个视图方向对齐后比较，白名单豁免 ANT2H7H）。

**答案**：在引脚循环里对 `common` 单元的每个信号脚取 `views['lef'][c][p]`、`views['verilog'][c][p]`、`views['cdl'][c][p]` 三者比较；不等则打印三视图方向。ANT2H7H 的 A 会被抓出（lef=in, verilog=bidir, cdl=I），加入 `KNOWN_ISSUES = {("ANT2H7H","A")}` 豁免后再跑，报告应归零（其余单元未发现方向冲突——本讲实测）。

**练习 2**：把报告改成「按出现组合分组」：`LEF+CDL 有、verilog 无`的单元应该恰好落在哪一组？共几个？

**答案**：恰好是 4.1.3 的「cell_list 有、verilog 没有的 17 个」——因为 LEF 与 CDL 都包含 cell_list 全集，这 17 个的组合就是「cell_list+lef+cdl、无 verilog」，共 17 个。

**练习 3**：如何把脚本接入 CI，让它只在「新出现」的不一致时失败？

**答案**：把当前全部差异导出为 `baseline.json` 提交；CI 中运行脚本与 baseline 做差，`新增 ERROR → 退出码 1`，`差异减少 → 提示更新 baseline`。这利用了 4.3.1 的分级原则：历史差异是数据，新差异才是事件。

### 4.4 电源引脚约定

#### 4.4.1 概念说明

电源引脚是多视图核对里最容易产生**假阳性**的部分，因为四个视图有四种「出场方式」：

| 视图 | 电源引脚的写法 | 是否出现 |
| --- | --- | --- |
| LEF | `PIN VDD ... DIRECTION INOUT; USE POWER; SHAPE ABUTMENT` | 必有（H7CH 全部 785 个宏各一对，共 1570 个 INOUT 脚） |
| CDL | 端口表尾部 `VDD VSS`，`*.PININFO` 标 `VDD:B VSS:B` | 必有 |
| verilog | 无电源端口（功能模型不模拟电源） | 通常无 |
| liberty | 信号单元常省略；电源 pad 的**轨道本身就是 pin** | 视库而定 |

`SHAPE ABUTMENT`（对接形）表示电源脚的矩形骑在单元边界上，靠相邻单元拼行自然连成电源轨——这是 u3-l2 讲过的「越界矩形」设计的正式名称。

#### 4.4.2 核心流程

```text
引脚比较前预处理：
  LEF   ：从 PIN 集合剔除 USE POWER / USE GROUND 的脚（或名字 ∈ {VDD,VSS}）
  CDL   ：剔除端口表尾部的 VDD/VSS（或 PININFO 里 :B 且名字 ∈ {VDD,VSS}）
  verilog：无需处理（本来就没有）
  liberty：若 pin 名 ∈ 电源集合且带 is_pad/pad_cell 语境 → 属 pad 轨道，单独归类
核对规则：
  信号脚集合 → 必须三视图相等
  电源脚集合 → LEF 与 CDL 必须相等（一对 VDD/VSS）；verilog 必须没有；
               liberty 有则记录、无则不报错
```

#### 4.4.3 源码精读

**标准单元侧（H7CH）**：

- [lef/ics55_LLSC_H7CH.lef:L75-L87](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L75-L87) —— ADDFX1H7H 的 `PIN VDD`：`DIRECTION INOUT; USE POWER; SHAPE ABUTMENT`，矩形 `RECT 0 1.32 4.8 1.48` 上边界越出 `SIZE` 高度 1.4（顶到 1.48），与相邻行单元的 VSS 矩形（[L88-L100](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L88-L100)，底到 −0.08）搭接成轨。实测全库 `USE POWER` 785 处、`USE GROUND` 785 处、`DIRECTION INOUT` 1570 处，即**每个宏恰好一对**电源脚。
- [cdl:L33-L34](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L33-L34) —— CDL 的同单元端口表以 `VDD VSS` 收尾、PININFO 标 `B`；连最简单的填充单元也遵守，如 [cdl:L8001](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L8001) `.SUBCKT FILLTAPH7H VDD VSS`——只有电源两个端口，强证据表明它是「井接触/电源抽头」填充（用途待确认）。
- [verilog:L95](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L95) —— verilog 端口表只有 5 个信号脚，无 VDD/VSS；但注意 TIEHI 这类单元（[verilog:L57-L60](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L57-L60) `assign Z = 1'b1`）在功能上就是「接电源」，只是不写电源端口。

**IO 库侧（电源域更多）**：

- IO 的电源域不止 VDD/VSS：u4-l1 讲过数字域 `VDD/VDDIO/VSS/VSSIO`、模拟域 `VDDA/VSSA`。LEF 里 PAR 有 6 个引脚 `A, VDD, VDDA, VSS, VSSA, PAD`（[ICSIOA_N55_3P3_1P6M1TM.lef:L53595-L53625](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/lef/ICSIOA_N55_3P3_1P6M1TM.lef#L53595-L53625) 依次可见 VDD/VDDA，其后 VSS/VSSA/PAD）；verilog 端口同为 6 个（[icsIOA_N55_3P3.v:L201](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L201)）；CDL 端口同为 6 个（[ICSIOA_N55_3P3.cdl:L164](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cdl/ICSIOA_N55_3P3.cdl#L164)）——**IO 的 verilog 模型带电源端口**，与标准单元相反！因为 IO 的电平移位、ESD 结构在仿真里需要真实电源连接。
- liberty 对电源 pad 的建模：cell 名 `P65_1233_VDD1A`，pin 却叫 `VDDA1`（[ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib:L1083-L1091](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L1083-L1091)），direction 为 inout 且 `is_pad`——电源 pad 的「信号」就是电源轨道本身；对比 [L1074-L1082](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L1074-L1082) 的 VDD1。而同一个 pad 在 verilog 里的端口名也是 `VDDA1`（[icsIOA_N55_3P3.v:L107](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L107)）——跨视图引脚名一致，只是「单元名 ≠ 引脚名」容易让人误报，脚本要用引脚名而不是单元名片段去匹配。
- 一个纯电源模块的样例：[icsIOA_N55_3P3.v:L20-L27](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v#L20-L27) `module P65_1233_CUT` 六个端口全是 inout 电源域脚、无任何逻辑——物理切割单元在 verilog 里只是「让网表能连电源」的占位。

#### 4.4.4 代码实践

**实践目标**：实测 H7CH「每宏恰好一对电源脚」这一约定，并统计 IO 库的电源域引脚家族。

**操作步骤**：

```bash
# 1. H7CH LEF 三种计数，应得 785/785/1570
grep -c "USE POWER"  IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef
grep -c "USE GROUND" IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef
grep -c "DIRECTION INOUT" IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef
# 2. CDL 里 PININFO 标 B 的电源声明（抽样看 5 行）
grep -m 5 "VDD:B VSS:B" IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl
# 3. IO verilog 里出现的电源域名种类
grep -o "inout V[A-Z]*" IP/IO/ICsprout_55LLULP1233_IO_251013/verilog/icsIOA_N55_3P3.v | sort | uniq -c
```

**需要观察的现象**：第 1 步三个数两两相等且等于宏数（785 与 1570=2×785）；第 3 步出现 VDD/VDDIO/VSS/VSSIO/VDDA/VSSA 等多个家族。

**预期结果**：确认「LEF 每宏一对 VDD/VSS、CDL 尾部同名、verilog 无」的三段式约定；IO 侧电源域引脚在 verilog 中以 inout 出现。命令均为只读 grep，本讲作者已实测（785/785/1570）。

#### 4.4.5 小练习与答案

**练习 1**：为什么标准单元 verilog 模型省略电源，IO 单元的 verilog 模型却保留？

**答案**：标准单元的功能（布尔/时序）与电源状态无关，省略可让门级仿真不必连电源网；IO 单元含电平移位与 ESD 结构，其行为依赖 IO 电压域（3.3V）与核域（1.2V）的连接关系，端口必须保留才能建出有意义的模型（u4-l3 讲过其 specify 全为零延迟占位，真实延迟靠 SDF）。

**练习 2**：脚本里 `POWER = {"VDD","VSS"}` 对 IO 库会出什么错？怎么改？

**答案**：IO 库还有 VDDIO/VSSIO/VDDA/VSSA/VDD1/VSS1/VDDA1 等电源域脚，二元集合会把它们当信号脚，与 liberty（只有 PAD/A 等 2 脚）比较时报大量假 ERROR。改为「名字匹配 `^V(DD|SS)[A-Z0-9]*$` 且（LEF 侧 USE 为 POWER/GROUND，或 CDL 侧 PININFO 为 B）」的规则集合。

**练习 3**：`FILLTAPH7H` 只有 VDD/VSS 两个端口，它在流程里什么时候被插入？

**答案**：它是 well-tap/电源抽头类填充单元（推断依据：无信号端口、CDL 只有电源、不在 cell_list、LEF 中属 FILLER 族），通常在布局后的 filler 阶段与 FILLER 一起插入，为衬底/阱提供电源接触、满足 DRC 对阱接触间距的要求。具体插入策略本仓库未提供文档，待确认。

## 5. 综合实践

**任务**：把 4.3 的最小脚本扩展成完整的 `check_pdk.py`，产出规格要求的两份报告，并对 CDL 多出的子电路做抽样考古。

**步骤**：

1. **单元×视图报告**：在 4.3 脚本基础上，为每个单元输出五列布尔（cell_list/lef/verilog/cdl/liberty），并按「出现组合」分组计数。完成 liberty 下载（`make unzip`，`RELEASE_TAG` 用法见 u1-l3）后把 H7CH 的 `.lib` 加入解析——解压出的文件名待确认，用 `glob` 兜底即可。预期分组结果（未下载 liberty 前是四视图）：`全部在场` 731 个、`缺 verilog` 17 个、`cell_list 外的 LEF+CDL/verilog 组合` 若干组——与 4.1.3 差集互相印证。
2. **引脚×视图报告**：按 4.4.2 的预处理剥离电源脚，先比名字集合，再加方向比较与 ANT2H7H 白名单（4.3.5 练习 1）。
3. **CDL 考古**：从「CDL−cell_list 的 38 个额外单元」中抽样 5 个，抄录端口表并按命名推测用途，全部标注待确认。可直接使用本讲已定位的样本：
   - [cdl:L4894](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L4894) `DFFNSRQX1H7H CKN D Q RN SN VDD VSS`——下沿触发、带异步复位 RN 与置位 SN 的 DFF（NSRQ=negative Set/Reset Q，待确认）；
   - [cdl:L8001](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L8001) `FILLTAPH7H VDD VSS`——电源抽头填充（见 4.4.5）；
   - [cdl:L10337](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L10337) `MSDFFQX3H7H CK D0 D1 Q S0 SE SI VDD VSS`——带输入 2 选 1（S0 选 D0/D1）与扫描使能 SE/SI 的多路扫描 DFF（M=mux，S=scan，待确认）；
   - [cdl:L18367](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L18367) `SDFFNSRX0P5H7H CKN D Q QN RN SE SI SN VDD VSS`——下沿、双输出、带复位置位与扫描的 DFF；
   - [cdl:L19294](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L19294) `SDFFRQX3H7H CK D Q RN SE SI VDD VSS`——只在 CDL 出现的 X3 档扫描 DFF，疑似其他视图漏生成（待确认）。
4. **交叉验证**：任选 3 个抽样单元，在 u5-l1 讲过的模板展开规则下数一数晶体管数，验证「MSDFF 比 SDFF 多输入多路器所以管子更多」这类推断是否自洽。

**预期结果**：两份 Markdown 报告 + 一份 5 条目的考古记录；所有「多出/缺失」都能归入四类解释之一（纯物理无逻辑、手册未登记、模型生成范围、疑似遗漏），归不进的按练习要求标待确认并整理成 issue 素材。

## 6. 本讲小结

- 五视图单元集合不相等是常态：H7CH 实测 cell_list 748、LEF 785（+37）、verilog 751（+20 / −17）、CDL 去重后 791（+38 单元 +5 模板名）；`SDFFRQX3H7H` 只在 CDL 出现。
- CDL 的 1174 条 `.SUBCKT` 必须去重（TG×184、TSINV×176、NAND2×22、NOR2×5 是模板重复定义），否则差集被放大约 10 倍。
- 引脚核对要先剥电源再比名字：LEF 每宏恰好一对 `INOUT+POWER/GROUND+ABUTMENT` 电源脚（785/785/1570 实测），CDL 端口表尾 `VDD VSS` 标 `B`，标准单元 verilog 无电源端口，IO verilog 反而保留电源域端口。
- 方向翻译表 `INPUT/input/I → OUTPUT/output/O → INOUT/inout/B`；真实冲突存在（ANT2H7H 的 A 在 LEF 是 INPUT、verilog 是 inout；PAR 的 A/PAD 在 liberty 是 inout、普通版 LEF 是 INPUT），要用白名单管理而非无视。
- 一致性检查脚本的骨架是「每视图一个提取器 + 集合运算 + 分级报告 + baseline」，纯 Python 标准库即可覆盖本仓库全部文本视图；标准单元 liberty 需 `make unzip` 下载后才能入列，其内部文件名待确认。
- cell_list 不是全集，不能当唯一金标准；「cell_list ∪ LEF」才是给工具链的完整物理清单。

## 7. 下一步学习建议

- 下一讲 u6-l1「把 PDK 装进开源 EDA 工具」将用 OpenROAD 实际读入 tech LEF + cell LEF + liberty，届时工具报出的 site/层/单元数应与本讲的统计互相印证——把你本讲的脚本输出留着当对答案的参照。
- 想继续深挖差异：把本讲脚本推广到 H7CL/H7CR 两库，验证「三库除后缀外逐行对齐」（u3-l1 的结论）在 CDL/verilog 视图同样成立；任何不对称都值得怀疑。
- 阅读 [CONTRIBUTING.md](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/CONTRIBUTING.md) 与 [README.md](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md)，把你考古出的「疑似遗漏」整理成规范的问题报告，这是 u6-l3 贡献流程的实战预演。
- 若你负责维护内部 PDK：把本讲脚本升级为 CI 门禁（4.3.5 练习 3），任何新生成的视图文件先过一致性检查再入库。
