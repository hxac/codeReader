# u3-l3 _ecos 单元 LEF：电源轨道与高层引脚

## 1. 本讲目标

上一讲（u3-l2）我们解剖了普通版单元 LEF 的 MACRO 结构，并留下一个关键事实：**普通版所有引脚几何都在 MET1 上**。本讲解决三个问题：

1. **电源轨道 pin 与 ABUTMENT**：`SHAPE ABUTMENT` 的 VDD/VSS 轨道引脚在两版 LEF 中到底是什么状态？——我们会用数据纠正一个常见误判（包括本手册大纲最初的猜测）：普通版**本来就有**完整的电源轨道引脚，ecos 版并没有"新增"它们。
2. **高层引脚可达性（pin accessibility）**：ecos 版给全部 3499 个信号引脚补上 MET2 竖条和 VIA1 过孔形状，这为什么是开源布线器的"及时雨"？
3. **普通版/ecos 版 diff 方法**：如何用脚本把两份 8~9 万行的 LEF 之间的差异量化成三张表（矩形增量表、层分布表、方向差异表），并用 git 历史（提交 e5c881b）交叉验证结论。

学完本讲，你应该能独立回答："如果我在 OpenROAD 里误用了普通版 LEF，布线器会遇到什么困难？ecos 版改了哪三件事来缓解？"

## 2. 前置知识

- **引脚可达性（pin accessibility）**：详细布线器（detailed router）要把金属线接到单元引脚上。它只能在引脚已有形状的层上"落笔"，且落点必须满足 DRC 间距、避开 OBS 障碍。一个引脚若在高层金属没有形状，布线器就只能通过下层（如 MET1）访问它。单元排成行之后 MET1 资源非常拥挤，可达性差会导致布线失败或 DRC 违例。
- **过孔栈（via stack）**：信号从 MET1 爬到 MET2 需要一个 VIA1 过孔。LEF 允许同一个 PIN 的 PORT 里声明多层的 RECT——布线器把所有矩形都视为该引脚的可用形状，跨层矩形之间的连接由工具按过孔规则处理，或由库直接给出 VIA1 形状"示意"此处可以打孔。
- **ABUTMENT（对接形）**：u3-l2 讲过，`SHAPE ABUTMENT` 表示引脚形状在单元边界上与相邻单元直接对接，VDD/VSS 轨道矩形的 y 区间越过 SIZE 边界（±0.08 μm），单元拼行后自然连成贯穿整行的电源轨。本讲只回顾，不重复推导。
- **轨道（track）**：u2-l1/u2-l3 讲过，布线层按 `PITCH` 和 `OFFSET` 划分等间距轨道，布线器尽量让线中心落在轨道上。tech LEF 中 MET2 是垂直方向、PITCH 0.2、WIDTH 0.1；这一点在本讲会反复用到。
- **视图职责差异**：LEF 是物理抽象视图，必须包含电源引脚；Verilog 是功能仿真视图，模块端口通常**不含** VDD/VSS；liberty 是时序功耗视图，通常**含** VDD/VSS。做跨视图核对时要按各自的职责来比。

一个方法论提醒：本讲的核心姿态是"**先提出假设，再用 grep/脚本量化验证，最后用 git 历史佐证**"。你会看到假设被数据推翻的活例子。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef) | 普通版单元 LEF，79580 行，785 个 MACRO，全部引脚几何在 MET1 |
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef) | ecos 版单元 LEF，93576 行，同一批 785 个 MACRO 的"开源工具适配变体" |
| [prtech/techLEF/N551P6M.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef) | 工艺 LEF，本讲查 MET2/VIA1 的层规格（宽度过孔尺寸），与 ecos 引脚形状对照 |
| [IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v) | Verilog 仿真模型，实践任务里做端口对照（注意它没有电源端口） |
| 提交 [e5c881b](https://github.com/openecos-projects/icsprout55-pdk/commit/e5c881bf62a18236449c72ee7348960c00e1fd88) | "fix: correct direction for some pins in *_ecos.lef"，本讲 4.3 的历史佐证 |

提示：标准单元 liberty 不在 git 内，需按 u1-l3 的 `make unzip` 从 GitHub Release 下载后才能做 liberty 对照；本讲实践提供了未下载时的替代方案（与 Verilog 端口对照）。

## 4. 核心概念与源码讲解

先给出全讲最重要的一张实测数据表（HEAD 68d89ed，`grep -c` 逐项统计）：

| 指标 | 普通版 `ics55_LLSC_H7CH.lef` | ecos 版 `ics55_LLSC_H7CH_ecos.lef` |
| --- | --- | --- |
| 文件行数 | 79580 | 93576 |
| `VERSION` | 5.7 | 5.8 |
| `MACRO` 数 | 785 | 785 |
| `PIN` 总数 | 5069 | 5069 |
| `SHAPE ABUTMENT` | 1570 | 1570 |
| `USE POWER` / `USE GROUND` | 785 / 785 | 785 / 785 |
| `LAYER MET2 ;`（引脚内） | **0** | **3499** |
| `LAYER VIA1 ;`（引脚内） | **0** | **3499** |
| `DIRECTION INPUT` / `OUTPUT` / `INOUT` | 2584 / 915 / 1570 | 2591 / 908 / 1570 |

三个立刻能读出的结论：

- 电源引脚（ABUTMENT、USE POWER/GROUND）数量**完全相同**——ecos 版没有增删任何 VDD/VSS；
- 5069 − 1570 = 3499 个信号引脚，与 ecos 版新增的 3499 个 MET2、3499 个 VIA1 **一一对应**：每个信号引脚恰好补 1 个 MET2 矩形 + 1 个 VIA1 矩形；
- 方向统计相差 7（INPUT 2591 vs 2584，OUTPUT 908 vs 915）——这是 4.3 要讲的方向修正遗留。

### 4.1 电源轨道 pin 与 ABUTMENT：先纠正一个直觉

#### 4.1.1 概念说明

很多人（包括本手册大纲的最初规划）会猜："ecos 版为开源工具补上了 VDD/VSS 电源轨道引脚"。这个猜测听起来合理——开源工具确实依赖 `USE POWER/GROUND` 识别电源网络——但**对 H7CH 单元 LEF 不成立**：普通版从发布起就带完整的 ABUTMENT 电源引脚。

需要区分两件事：

- **电源引脚的有无**：两版相同，都有。开源布线器/电源网络综合工具在两版里都能找到 VDD/VSS。
- **电源引脚的排布位置**：普通版引脚大致按字母序排列（信号引脚在前，VDD/VSS 在后；TIEHIH7H 的 Z 排在 VDD/VSS 之后，因为它字母序更靠后）；ecos 版则把 VDD/VSS **统一放到每个 MACRO 的最前面**。排序不影响 LEF 语义，但统一的顺序方便解析脚本和人工 diff 快速定位电源轨道。

为什么 IO 库的 ecos 版（u4-l2 会讲）确实在"修"电源相关属性（USE GROUND 修正、IOSite 定义），而标准单元库的 ecos 版不需要？因为两类库的"原版质量"不同——这正是"不要把一个库的 ecos 结论平移到另一个库"的教学点，也是 diff 方法论的价值所在。

#### 4.1.2 核心流程

电源轨道如何靠 ABUTMENT 引脚拼成（回顾 u3-l2，补充坐标细节）：

1. 每个单元的 VDD 引脚含一条横贯全宽的 MET1 矩形，y 区间 \(1.32 \sim 1.48\)（顶部）；VSS 为 \( -0.08 \sim 0.08\)（底部）。
2. SIZE 高度 1.4 μm，而轨道 y 区间以行边界为中心上下各伸 0.08 μm。
3. 行内左右相邻单元的轨道矩形在 x 方向首尾相接（都是 `0 到 SIZE 宽度`），上下相邻行共享同一条边界轨道，于是拼出贯穿全芯片的电源网格。

#### 4.1.3 源码精读

普通版 ADDFX1H7H 的电源引脚——注意它**自带** SHAPE ABUTMENT：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L75-L100](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L75-L100)
这段是普通版的 VDD（USE POWER）与 VSS（USE GROUND）：两者都是 `DIRECTION INOUT` + `SHAPE ABUTMENT`；第一条 RECT 分别为 `0 1.32 4.8 1.48` 与 `0 -0.08 4.8 0.08`，即横贯 4.8 μm 全宽、越过 SIZE 边界的电源轨道；后面几条 RECT 是从轨道垂下/升起的"供电支干"，把轨道与单元内部电源节点接通。

ecos 版同一单元的电源引脚（矩形坐标逐字节相同，只是位置挪到了 MACRO 开头）：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L26-L51](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L26-L51)
ecos 版 MACRO 头部六字段之后**第一条就是 PIN VDD，第二条是 PIN VSS**，五个 RECT 与普通版 L75-L100 完全一致。版本号差异见两文件各自的 [第 15 行](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L15)：普通版声明 `VERSION 5.7`，ecos 版声明 `VERSION 5.8`（5.8 是 LEF/DEF 的最终版本号，对齐当前开源工具的解析器）。

再看 DFFNSRX1H7H 的 ecos 版头部，VDD/VSS 同样置顶：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L23170-L23200](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L23170-L23200)
这是 7.2 μm 宽触发器的电源对：VDD 轨道 `0 1.32 7.2 1.48` 加 7 条支干，VSS 轨道 `0 -0.08 7.2 0.08` 加 6 条支干。宽单元支干多，是因为内部触发器主从两级都要就近取电。

统计佐证（普通版只有 14 个宏"天然"以 VDD 开头，全是无信号引脚或 Z 字母序靠后的单元）：

```text
FILLCAP4/8/16/32H7H、FILLER1/2/4/8/16/32/64H7H、FILLTAPH7H、TIEHIH7H、TIELOH7H
```

#### 4.1.4 代码实践

1. **实践目标**：用三条 grep 验证"两版电源引脚数量与语义完全相同"，体会"假设—验证"的流程。
2. **操作步骤**（仓库根目录执行）：

```bash
LEF=IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef
grep -c 'SHAPE ABUTMENT' $LEF/ics55_LLSC_H7CH.lef $LEF/ics55_LLSC_H7CH_ecos.lef
grep -c 'USE POWER'     $LEF/ics55_LLSC_H7CH.lef $LEF/ics55_LLSC_H7CH_ecos.lef
grep -A 7 '^MACRO ' $LEF/ics55_LLSC_H7CH_ecos.lef | grep -c 'PIN VDD'
grep -A 7 '^MACRO ' $LEF/ics55_LLSC_H7CH.lef     | grep -c 'PIN VDD'
```

3. **需要观察的现象**：前两条命令两文件各输出 1570、785、785；第三条输出 785（ecos 版每个宏都以 VDD 打头），第四条输出 14（普通版只有 FILLER/FILLCAP/FILLTAP/TIE 类满足这种排列）。
4. **预期结果**：确认 ecos 版对电源引脚"只挪位置、不改内容"。逐宏的矩形级核对（新增 VDD/VSS rect 数应为 0）由 4.3/综合实践的脚本完成。本小节的 grep 统计已在 HEAD 68d89ed 上实测；逐宏明细待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：既然两版都有电源引脚，为什么 u2-l3 说"开源流程应选 _ecos 版"？矛盾吗？
**答案**：不矛盾。u2-l3 的建议针对 **tech LEF** 的 RC 寄生参数（CAPACITANCE/OFFSET）；本讲证明的是**单元 LEF** 的电源引脚两版相同。ecos 的适配点是按文件类型分摊的：tech LEF 补 RC、单元 LEF 补高层引脚可达性、IO LEF 补 SITE 与 USE 修正。选版策略要逐类文件判断。

**练习 2**：VDD 轨道矩形 `0 1.32 4.8 1.48` 的中心 y 是多少？为什么不在 SIZE 范围 \(0 \sim 1.4\) 内？
**答案**：中心 \(y = (1.32+1.48)/2 = 1.4\)，恰是行顶边界。轨道中心设计在行边界上，上下两行单元的轨道在边界处重叠对接，拼行后形成连续电源网格；`SHAPE ABUTMENT` 就是在声明这种"越界对接"是有意为之。

**练习 3**：`DIRECTION INOUT` 用在电源引脚上是什么含义？改成 INPUT 行不行？
**答案**：INOUT 表示该引脚既是电流入口也是出口（单元可以从上方轨道取电、也向下方支干供电，网格是双向流动的）。语义上电源引脚不是信号，方向字段只是沿用语法骨架；改成 INPUT 会导致部分工具把它当作单向端口处理，没有必要也不符合惯例。

### 4.2 高层引脚可达性：MET2 竖条 + VIA1

#### 4.2.1 概念说明

这是 ecos 版单元 LEF 的**真正增量**。问题背景：

- 普通版每个信号引脚只有 MET1 形状。布线器想连接这个引脚，只能在 MET1 层"落线"。
- 标准单元排成行后，行内 MET1 同时被三样东西争抢：引脚自己的形状、单元内部布线（OBS 障碍）、水平布线通道（MET1 是水平方向层，见 u2-l1）。
- 密集单元（引脚多、OBS 多）的 MET1 可达点所剩无几，详细布线器可能找不到合法落点。

ecos 版的解法非常朴素：**给每个信号引脚"预制"一个 MET2 竖条，并附一个 VIA1**。等效于库告诉布线器："这个引脚保证能从 MET2 访问，VIA1 位置我都替你选好了。"全库 3499 个信号引脚，一个不落。

为什么选 MET2 而不是更高层？MET2 是 MET1 的直接上层，过孔栈只需一级 VIA1；且 MET2 垂直方向，正好穿过行高，竖条可以同时覆盖多个水平 MET1 轨道。

一个必须建立的认识：**LEF 是抽象视图，不是版图真相**。这些 MET2 竖条在真实 GDS 里未必逐形存在（GDS 由 FOREIGN 指向的同名单元给出）。抽象视图的职责是告诉布线器"哪里可以用"，ecos 的竖条是一张"访问许可证"——只要最终物理实现（打孔上 MET2 再接线）合法，抽象先行完全成立。

#### 4.2.2 核心流程

详细布线器对每个引脚的可达性判定（简化伪代码）：

```text
for each pin:
    candidates = []
    for (layer, rect) in pin.shapes:          # 引脚在各层的形状
        for point in on_track_positions(rect, layer):
            if drc_legal(point, spacing, OBS, neighbor_pins):
                candidates.append(point)      # 可作为 access point
    if candidates 为空 or 与邻引脚冲突:
        尝试通过上层访问 —— 要求 pin 在上层有形状（或允许 via-on-pin）
```

普通版：`pin.shapes` 只含 MET1 → 候选点全部挤在 MET1，与 OBS/邻引脚竞争。
ecos 版：`pin.shapes` 含 MET1 + MET2（+VIA1）→ 每个 pin 至少多出一个"专属竖条"上的候选点，竖条之间互不重叠，冲突大幅下降。

竖条的几何规律（后文源码可逐一对上）：

- 宽度 0.1 μm = tech LEF 中 MET2 的 `WIDTH 0.1`（最小线宽）；
- 中心 x 全部落在 \(0.1 + 0.2k\)（\(k\) 为整数）的网格上——即 \(\equiv 0.1 \pmod{0.2}\)；
- VIA1 为 0.09 × 0.09 = tech LEF 中 VIA1 的 `WIDTH 0.09`（单个最小过孔），中心与竖条同 x，叠在 MET1 引脚形状上方。

第 2 条暗藏一个跨文件的精妙配合：tech LEF **原版** MET2 的 `OFFSET 0 0`，垂直轨道中心在 \(0.2k\)；而 **_ecos 版**（u2-l3）把 OFFSET 改为 0.1，轨道中心移到 \(0.1 + 0.2k\)——恰好对准这些竖条的中心。也就是说：**ecos 单元 LEF 的竖条是按 ecos tech LEF 的轨道网格摆放的**。两份 _ecos 文件是配套设计，这解释了为什么它们要成套出现。

#### 4.2.3 源码精读

tech LEF 中 MET2 与 VIA1 的规格（对照基准）：

[prtech/techLEF/N551P6M.lef#L76-L95](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L76-L95)
这段定义了 VIA1（TYPE CUT、`SPACING 0.11`、`WIDTH 0.09`，即单个过孔 cut 是 0.09 μm 见方）和 MET2（TYPE ROUTING、`DIRECTION VERTICAL`、`PITCH 0.2`、`WIDTH 0.1`、`OFFSET 0 0`）。注意 OFFSET 0——这正是 u2-l3 指出、ecos tech LEF 改成 0.1 的那个参数。

普通版 ADDFX1H7H 的 A 引脚（只有 MET1）：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L26-L33](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L26-L33)
一个 RECT `0.425 0.555 0.545 0.78`，单层单形状——布线器只能在 MET1 上、以不与 OBS 冲突的方式接到这块 0.12 × 0.225 μm 的小矩形。

ecos 版同一个 A 引脚（MET1 + MET2 + VIA1 三层形状）：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L52-L63](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L52-L63)
MET1 原形状保留不动；新增 `LAYER MET2` 的 RECT `0.45 0.43 0.55 0.96`（宽 0.1 = MET2 最小线宽，x 中心 0.5，落在 \(0.1+0.2k\) 网格上）和 `LAYER VIA1` 的 RECT `0.455 0.655 0.545 0.745`（0.09 × 0.09 最小过孔，中心与竖条同轴，且落在 MET1 矩形 `0.425 0.555 0.545 0.78` 内部——保证三层形状连通）。

多矩形引脚也一样升级——CI 引脚原有 8 个 MET1 碎片，ecos 版追加一竖条一过孔：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L76-L94](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L76-L94)
CI 的 MET1 有 8 个碎片（普通版 [L42-L56](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L42-L56)），是典型的"难访问"引脚；ecos 版追加 MET2 `2.05 0.23 2.15 0.76` 与 VIA1 `2.055 0.455 2.145 0.545`，x 中心 2.1 同样对齐 \(0.1 + 0.2k\) 网格。

连"边缘单元"也照顾到了——天线二极管 ANT2H7H 和常 1 单元 TIEHIH7H：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L3592-L3630](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L3592-L3630)
ANT2H7H 是 0.4 μm 宽（两个 site）的天线二极管单元，唯一信号引脚 A 也获得 MET2 竖条（`0.25 0.225 0.35 0.76`，x 中心 0.3）与 VIA1。

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L88134-L88145](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L88134-L88145)
TIEHIH7H 的输出 Z 位于单元顶部（y 1.025~1.175，贴近电源轨），竖条相应变为 `0.25 0.84 0.35 1.22`——位置随引脚走，但宽度和网格约束不变。

最后看一个反例：FILLER/FILLCAP/FILLTAP 类填充单元**没有**信号引脚，因此没有任何 MET2/VIA1——它们在 ecos 版里与普通版的差异只剩引脚排序。这就是"3499 = 信号引脚总数"这一等式的另一面。

补充一个容易被忽略的细节：ecos 版**没有**给 OBS 追加 MET2 障碍（`LAYER MET2 ;` 的 3499 次全部出现在引脚内，OBS 仍是纯 MET1）。这意味着单元上方的 MET2 层对布线器完全开放，竖条之间还能穿线——可达性与可布性兼得。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证"3499 个信号引脚、每个恰好 1 个 MET2 + 1 个 VIA1"以及竖条的网格规律。
2. **操作步骤**：

```bash
LEF=IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef
grep -c 'LAYER MET2 ;' $LEF/ics55_LLSC_H7CH.lef $LEF/ics55_LLSC_H7CH_ecos.lef
grep -c 'LAYER VIA1 ;' $LEF/ics55_LLSC_H7CH.lef $LEF/ics55_LLSC_H7CH_ecos.lef
grep -c '^  PIN '     $LEF/ics55_LLSC_H7CH_ecos.lef
```

再用一条 awk 抽出全部 MET2 竖条的中心 x，检查网格（awk 单命令，直接在终端执行）：

```bash
awk '/LAYER MET2 ;/{m=1;next} m&&/^      LAYER/{m=0;next} m&&/RECT/{split($0,a," ");
     printf "%.3f\n",(a[3]+a[5])/2; m=0}' $LEF/ics55_LLSC_H7CH_ecos.lef | sort -n | uniq -c | head
```

3. **需要观察的现象**：前两条命令输出 `普通版 0 / ecos 版 3499`；第三条 5069（PIN 总数，减去 1570 个电源引脚 = 3499）。awk 输出的中心 x 直方图应只出现在 0.1、0.3、0.5、…、\(0.1+0.2k\) 上（每个值出现若干次）。
4. **预期结果**：MET2/VIA1 与信号引脚数三者相等（3499）；竖条中心 x 全部 \(\equiv 0.1 \pmod{0.2}\)，与 ecos tech LEF 的 `OFFSET 0.1` 轨道网格吻合。计数类结论已在 HEAD 68d89ed 实测；awk 直方图的具体分布待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：竖条宽 0.1、VIA1 边长 0.09，这两个数字分别来自哪里？为什么不另造尺寸？
**答案**：0.1 是 tech LEF MET2 的 `WIDTH`（最小线宽），0.09 是 VIA1 层的 `WIDTH`（单孔最小尺寸）。抽象视图里的形状最终要能被物理实现替换：细于最小线宽的竖条、小于最小尺寸的过孔在真实版图中不合法，布线器做 DRC 检查时也会报错。

**练习 2**：给引脚加 MET2 竖条会不会挤占布线资源、得不偿失？
**答案**：会有代价——竖条本身占据一段 MET2 通道。但（a）竖条只在引脚正上方一小段（长约 0.38~0.53 μm），（b）OBS 没有声明 MET2 障碍，竖条之间的空隙仍可穿线，（c）收益是消解 MET1 可达性瓶颈这一布线失败的主因。对开源布线器而言净收益为正；这也是开源 PDK 社区处理类似问题的常见手法。

**练习 3**：如果布线器读的是 ecos 单元 LEF + **普通版** tech LEF，会发生什么？
**答案**：普通版 tech LEF 的 MET2 `OFFSET 0 0` 使垂直轨道中心落在 \(0.2k\)，与竖条中心（\(0.1+0.2k\)）错开半节距（0.1 μm）。布线器要么强制把线拉到轨道上导致与竖条错位、DRC 间距紧张，要么承认 off-track 布线。这就是"ecos 文件必须成套使用"的几何原因。

### 4.3 普通版/ecos 版 diff 方法：从猜想到清单

#### 4.3.1 概念说明

面对两份 8~9 万行的平行文件，逐行 diff 输出会淹没在引脚重排的噪音里（每个宏的 VDD/VSS 挪到开头，`diff` 会把每个单元都标记为"大改"）。正确做法是**先解析、后比较**：

1. **骨架对齐**：只保留结构行（MACRO/PIN/DIRECTION/LAYER/USE），把几何数字先放一边，确认两版的"单元集合、引脚集合"一致；
2. **状态机解析**：把每份 LEF 解析成 `{宏 → {引脚 → {方向, 各层矩形数}}}` 的表；
3. **三维度对比**：集合维度（有没有多/少的单元和引脚）、计数维度（每个引脚每层矩形数的增量）、语义维度（方向、USE 属性的差异）。

这样得到的不是"一万行差异"，而是一份**变换清单**：ecos = 普通版 + 三类变换（版本号、引脚重排、信号引脚统一加 MET2+VIA1）+ 一处历史修正（方向）。

方向修正的主角是提交 [e5c881b](https://github.com/openecos-projects/icsprout55-pdk/commit/e5c881bf62a18236449c72ee7348960c00e1fd88)（"fix: correct direction for some pins in \*_ecos.lef"，2026-07-26）：它把三个库的 ecos LEF 里共 356 处 `DIRECTION OUTPUT` 改为 `DIRECTION INPUT`，其中 H7CH 一个文件就改了 119 个引脚，修复对象集中在 DFFNSR/DFFNS/DFFSQ/DFFSR 等触发器族。在 HEAD 上两版的方向统计仍差 7（普通版 INPUT 2584 vs ecos 版 2591）——也就是说普通版尚未同步这批修正。方向字段错误的实际危害：时序工具会把输出当输入、算不了对应的时序弧，综合/形式验证的库一致性检查（liberty ↔ LEF）会报错。e5c881b 的修复方向（OUTPUT→INPUT，与 liberty 的引脚方向对齐）表明 ecos 版以 liberty 为基准做了核对——这正是 u5-l2 多视图一致性检查的预告。

#### 4.3.2 核心流程

完整的 diff 方法可以总结成一棵决策树：

```text
两版 LEF 对比
├── 单元集合不同？          → 先解决"多了/少了哪些宏"（本例：785 = 785，跳过）
├── 引脚集合不同？          → 逐宏比对 pin 名集合（本例：逐宏相同，跳过）
├── 引脚属性不同？
│     ├── DIRECTION        → 方向差异表（本例：7 处，DFF 族）
│     └── USE / SHAPE      → 电源语义差异（本例：无）
├── 几何形状不同？
│     ├── 每引脚各层矩形数  → 增量表（本例：信号引脚 MET2 +1、VIA1 +1）
│     └── 矩形坐标         → 逐形 diff（本例：MET1 原形不变）
└── 排版/版本号             → VERSION、引脚顺序（本例：5.7→5.8、VDD/VSS 置顶）
```

执行顺序有讲究：先看集合、再看属性、最后看几何。集合不一致时谈几何没有意义；属性差异（如方向）常常是几何差异之外独立的"语义补丁"，要分开归档。

#### 4.3.3 源码精读

被 e5c881b 修复的那类引脚长什么样？看 DFFSQX1H7H（带异步置位的 D 触发器）两版当前的 SN 引脚（修复后两版已一致，均为 INPUT）：

普通版：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L23230-L23270](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L23230-L23270)
这是 DFFSQX1H7H 的完整骨架：CK（USE CLOCK）、D 输入、Q 输出、SN 输入，全部只有 MET1 形状。修复前 ecos 版曾把这类置位/复位引脚标成 OUTPUT——对一个"输入激励"引脚来说是明显的方向错误。

ecos 版（同单元，SN 已是 INPUT 且带 MET2/VIA1）：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L27710-L27721](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L27710-L27721)
SN 引脚：`DIRECTION INPUT`、MET1 原形 + MET2 竖条 `5.45 0.425 5.55 0.96` + VIA1 `5.455 0.655 5.545 0.745`。注意中心 5.5 仍然 \(\equiv 0.1 \pmod{0.2}\)。

同宏的 CK 引脚则展示了时钟引脚的升级方式（时钟引脚的可达性最关键，因为时钟树布线要求低偏斜、规则化）：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L27672-L27684](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef#L27672-L27684)
CK 的 MET2 竖条 `0.25 0.625 0.35 1.16` 覆盖了行内的中上部，配合 VIA1 落在原 MET1 形状 `0.225 0.855 0.41 0.945` 之内。

最后是跨视图对照的基准——Verilog 模型中同单元的端口声明：

[IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L95-L105](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L95-L105)
ADDFX1H7H 的端口表 `(CO, S, A, B, CI)`，`output S, CO; input A, B, CI;`——注意**没有 VDD/VSS**。与 LEF 对照时：信号引脚的名字与方向应一一对应；LEF 多出的 2 个电源引脚是视图职责差异，不是错误。（liberty 下载后则应含 VDD/VSS，u5-l2 会系统化这一检查。）

#### 4.3.4 代码实践

1. **实践目标**：写一个无第三方依赖的 Python 脚本，把两版 LEF 的差异量化成三张表，并验证"ecos 变换清单"。
2. **操作步骤**：把下面的脚本保存为 `icsprout55-pdk-tutorial/diff_lef.py`（示例代码，约 70 行），在仓库根目录运行 `python3 icsprout55-pdk-tutorial/diff_lef.py`。

```python
#!/usr/bin/env python3
# 示例代码：对比普通版与 ecos 版单元 LEF 的三维度差异
import re
from collections import defaultdict

LEF_DIR = "IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef"
NORM, ECOS = LEF_DIR + "/ics55_LLSC_H7CH.lef", LEF_DIR + "/ics55_LLSC_H7CH_ecos.lef"

def parse_lef(path):
    """解析成 {macro: {pin: {"dir": str, "layers": {layer: rect 数}}}}"""
    macros, cur, pin, layer = {}, None, None, None
    for line in open(path):
        s = line.strip()
        if s.startswith("MACRO "):
            cur = s.split()[1]; macros[cur] = {}
        elif s.startswith("PIN ") and cur:
            pin = s.split()[1]
            macros[cur][pin] = {"dir": None, "layers": defaultdict(int)}
        elif s == "OBS":
            pin = None                       # OBS 的形状不计入引脚
        elif s.startswith("DIRECTION ") and pin:
            macros[cur][pin]["dir"] = s.split()[1]
        elif s.startswith("LAYER ") and pin:
            layer = s.split()[1]             # 形如 "LAYER MET2 ;"
        elif s.startswith("RECT") and pin:
            macros[cur][pin]["layers"][layer] += 1
        elif pin and s == "END " + pin:
            pin = None
    return macros

norm, ecos = parse_lef(NORM), parse_lef(ECOS)
assert set(norm) == set(ecos), "两版 MACRO 集合不同！"

power_add = sig_met2 = sig_via1 = 0
dir_diff, per_macro = [], []
for m in sorted(norm):
    assert set(norm[m]) == set(ecos[m]), f"{m} 引脚集合不同！"
    for p in norm[m]:
        if norm[m][p]["dir"] != ecos[m][p]["dir"]:
            dir_diff.append((m, p, norm[m][p]["dir"], ecos[m][p]["dir"]))
        if p in ("VDD", "VSS"):              # 电源引脚：只看 MET1 矩形增量
            power_add += ecos[m][p]["layers"]["MET1"] - norm[m][p]["layers"]["MET1"]
        else:                                # 信号引脚：看 MET2/VIA1 增量
            d2 = ecos[m][p]["layers"]["MET2"] - norm[m][p]["layers"].get("MET2", 0)
            dv = ecos[m][p]["layers"]["VIA1"] - norm[m][p]["layers"].get("VIA1", 0)
            sig_met2 += d2; sig_via1 += dv
            per_macro.append((m, p, d2, dv))

print(f"电源(VDD/VSS) MET1 矩形总增量 : {power_add}")
print(f"信号引脚 MET2 矩形总增量       : {sig_met2}")
print(f"信号引脚 VIA1 矩形总增量       : {sig_via1}")
print(f"方向差异条数                    : {len(dir_diff)}")
for row in dir_diff:
    print("  方向差异:", row)
for name in ("ADDFX1H7H", "ANT2H7H", "TIEHIH7H"):   # 抽样打印明细
    print(name, [r for r in per_macro if r[0] == name])
```

3. **需要观察的现象**：四个汇总数（电源增量 / MET2 增量 / VIA1 增量 / 方向差异数）；`dir_diff` 列表里每一条的"普通版方向 → ecos 版方向"极性是否一致；三个抽样宏中 ANT2H7H、TIEHIH7H 各只有 1 个信号引脚（A、Z），而 FILLER 类宏在 `per_macro` 里的增量应为 0。
4. **预期结果**：汇总行应为 `0 / 3499 / 3499 / 7`（前三个数与讲义开篇的 grep 统计直接对应；电源增量按本讲抽验的三个单元推全库应为 0）。方向差异的具体 7 条内容与极性分布请以脚本输出为准——待本地验证；记下它们，u5-l2 的一致性检查会再用到。

#### 4.3.5 小练习与答案

**练习 1**：为什么不用 `diff ics55_LLSC_H7CH.lef ics55_LLSC_H7CH_ecos.lef` 直接了事？
**答案**：可以用，但没用透。ecos 版把每个宏的 VDD/VSS 挪到开头，逐行 diff 会把 785 个宏几乎全部标成"整段重写"，真正的语义差异（MET2/VIA1、方向修正）反而被淹没。先解析成结构化数据再按维度对比，才能得到"变换清单"而不是"噪音清单"。`diff` 适合做最后的抽查（例如确认某宏 MET1 原形确实没变）。

**练习 2**：脚本的 `assert set(norm) == set(ecos)` 两行为什么重要？如果它失败了，后面的统计还有意义吗？
**答案**：它保证两版讨论的是同一批单元/引脚。若失败（比如某单元只在一边存在），后面的"同名引脚矩形增量"就出现了语义漏洞——增量可能来自"新增整个引脚"而不是"给既有引脚补形状"，两种情况要分开报告。工程上一致性检查永远先比集合、再比内容。

**练习 3**：e5c881b 为什么只改 \*_ecos.lef 而不改普通版？说说你的推断和验证方法。
**答案**：合理推断：ecos 版是开源社区（ECOS Team）维护、面向开源工具链的"主动修正线"，而普通版尽量保持代工厂交付原貌，便于与官方数据对照；仓库的分工就是把适配性修改隔离在 \*_ecos 后缀里。验证方法：`git log --oneline -- '*ics55_LLSC_H7CH.lef'` 与 `git log --oneline -- '*ics55_LLSC_H7CH_ecos.lef'` 分别看两个文件的修改历史——普通版历史上是否从未有过方向类修复，一查便知（此推断待本地验证）。

## 5. 综合实践

把 4.3 的脚本扩展成一份**《H7CH ecos 变换质量报告》**，任务贯穿本讲三个模块：

1. **扩展脚本**：在 `diff_lef.py` 基础上增加三个输出——
   - 每宏新增 MET2/VIA1 矩形数的分布直方图（多少宏 +0、+N……预期 +0 的只有 FILLER/FILLCAP/FILLTAP 等 14 个无信号引脚单元，其余每宏增量 = 信号引脚数）；
   - 全部 MET2 竖条的中心 x 相对 \(0.1 \pmod{0.2}\) 网格的偏差统计（预期全部为 0，即完全对轨）；
   - VIA1 矩形边长统计（预期全部为 0.09）。
2. **跨视图验证**：任选三个单元（建议 ADDFX1H7H、DFFSQX1H7H、TIEHIH7H），把 ecos 版 LEF 的信号引脚（去掉 VDD/VSS）与 Verilog 模型端口对照。可以在脚本里加一个简易的 Verilog 端口解析器：

```python
def parse_v_ports(path):
    mods, cur = {}, None
    for line in open(path):
        m = re.match(r"module (\w+)\s*\(", line)
        if m:
            cur = m.group(1); mods[cur] = {"input": set(), "output": set()}
            continue
        d = re.match(r"(input|output|inout)\s+(.+);", line.strip())
        if d and cur:
            for p in re.sub(r"\[\d+:\d+\]", "", d.group(2)).replace(" ", "").split(","):
                if p:
                    mods[cur][d.group(1)].add(p)
    return mods
```

对照规则：LEF 的 `DIRECTION INPUT` 引脚集合应等于 Verilog 的 `input` 端口集合，`OUTPUT` 对 `output`；LEF 的 VDD/VSS（INOUT）在 Verilog 中**不应**出现（职责差异）。若已执行 `make unzip` 下载 liberty，再把三个单元与 liberty 的 pin 段对照（此时 VDD/VSS 应该出现）——liberty 解压后的具体 `.lib` 文件名待确认，可先 `ls IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/liberty/`。
3. **写结论**：用 200 字回答——"如果只能给开源布线器一份文件，ecos 版相比普通版多给了什么、少改了什么？"报告中区分"实测确认"与"待确认"两类结论。

预期产出：一份可直接复核的报告 + 一个可复用脚本（u5-l2 的多视图一致性检查会在它基础上扩展到 CDL 与 liberty）。

## 6. 本讲小结

- **电源引脚两版相同**：普通版本就有 785 对 `SHAPE ABUTMENT` 的 VDD/VSS MET1 轨道引脚（中心在行边界、越界 ±0.08 μm 拼轨）；ecos 版只是把它们统一挪到每个 MACRO 最前，并把 `VERSION` 从 5.7 升到 5.8——"ecos 补电源引脚"的流行猜测对单元 LEF 不成立。
- **ecos 的真正增量是可达性**：全部 3499 个信号引脚各补 1 个 MET2 竖条（宽 0.1 = MET2 最小线宽）+ 1 个 VIA1（0.09² = 单孔最小尺寸），普通版两者计数为 0。
- **竖条与 _ecos tech LEF 配套**：竖条中心 x 全部落在 \(0.1 + 0.2k\) 网格，恰与 ecos tech LEF `OFFSET 0.1` 的 MET2 轨道对齐——ecos 文件必须成套使用。
- **方向修正是独立的一条线**：提交 e5c881b 把三个库 ecos LEF 的 356 处 `OUTPUT` 改为 `INPUT`（H7CH 119 处，集中在 DFF 族）；HEAD 上两版方向统计仍差 7，普通版未同步。
- **diff 方法论**：先解析成 `{宏 → 引脚 → {方向, 层→矩形数}}`，再按"集合 → 属性 → 几何"的顺序对比，把 9 万行文件压缩成一张变换清单；`diff` 只用来做终检。
- **视图职责差异**：LEF 含电源引脚、Verilog 功能模型不含（如 ADDFX1H7H 端口表只有 `CO, S, A, B, CI`），跨视图核对要按职责比。

## 7. 下一步学习建议

- **u3-l4（天线效应与 ant LEF）**：同目录下还有第三份变体 `ics55_LLSC_H7CH_ant.lef`（79582 行），它相对普通版的差异集中在 `ANTENNAPARTIALMETALAREA` 属性。用本讲的 diff 方法先自己跑一遍，再去 u3-l4 对答案。
- **u6-l1（把 PDK 装进开源 EDA 工具）**：本讲解释了"为什么选 _ecos"，u6-l1 演示 `read_lef` 读入 ecos tech LEF + ecos 单元 LEF 并核对 site/层/单元数，是本讲结论的实操落地。
- **u5-l2（多视图一致性检查）**：本讲 4.3 的方向差异和综合实践的对照脚本，会在 u5-l2 扩展成 cell_list/LEF/Verilog/CDL/liberty 五视图的系统化核查。
- 源码阅读建议：精读 [ics55_LLSC_H7CH_ecos.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH_ecos.lef) 中 2~3 个你感兴趣的宏（比如最宽的 FILLER64H7H 与最小的 ANT2H7H），亲手核对"每个信号引脚一组 MET2+VIA1、FILLER 只有电源对"的规律。
