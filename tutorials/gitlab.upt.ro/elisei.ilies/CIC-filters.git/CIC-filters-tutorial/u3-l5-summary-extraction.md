# summary.xlsx 汇总与数据提取

> 前置讲义：[u3-l1 资源利用率横向对比分析](u3-l1-resource-comparison.md)、[u2-l3 报告元信息与文件格式](u2-l3-report-metadata-formats.md)。
> 本讲承接 u2-l3（`.txt` 是可靠文本数据源、`.rpx` 是二进制）与 u3-l1（跨方案对比需要把多份报告的指标摆到一张表里），把「读一份报告」升级为「批量提取 600 多份报告并汇总成表」。

## 1. 本讲目标

学完本讲，你应当能够：

- 说明 `vivado_reports/summary.xlsx` 在整套评估数据集里的定位（聚合后的「结果表」），并解释为什么不能像 `.txt` 那样直接用文本工具读取它。
- 写一段脚本，从某个方案、某个频率下的全部 `timing_impl` 报告里批量提取 WNS，生成 CSV。
- 识别批量汇总时的两类陷阱：`.rpx` 不可文本解析、`to_delete/` 重复目录会造成重复计数；并知道如何规避。

本讲的核心动作只有一个字：**提取（extract）**。原料是仓库里数百份 `.txt`，产物是一张可被表格软件或 pandas 处理的扁平表，而 `summary.xlsx` 正是这张表「已经做好」的成品。

## 2. 前置知识

- **Vivado 报告的两阶段与两类**：综合（synth）/ 实现（impl）两阶段，时序（timing）/ 利用率（utilization）两类。这些已在 [u1-l4](u1-l4-fpga-eval-basics.md) 讲过。本讲只处理「实现阶段」报告（`*_impl_*`），因为它们才含真实布线延迟，是论文的评估依据。
- **`.txt` vs `.rpx` vs `.xlsx`**：u2-l3 已经讲过 `.txt` 是纯文本、`.rpx` 是 Vivado GUI 专用的二进制报告数据模型。本讲再引入第三种格式 `.xlsx`——它同样是二进制（OOXML，本质是一个 zip 包），所以和 `.rpx` 一样**不能**用 `grep`、`cat`、`awk` 当文本处理。
- **Design Timing Summary 里的 WNS**：最差建立时间裕量（Worst Negative Slack），正数代表满足时序约束。u2-l1 已逐节讲过时序报告，本讲只关心「WNS 在报告的哪一行、用脚本怎么抠出来」。
- **实验矩阵**：频率（100/290/300 MHz）× 抽取率 R × 级数 N × 方案（CIC Compiler / MATLAB HDL Coder / Open-source CIC）的多维组合，见 [u2-l5](u2-l5-experiment-matrix.md)。汇总就是把这套矩阵「拍扁」成一张大表。

## 3. 本讲源码地图

本讲「源码」不是程序代码，而是数据文件本身——因为本仓库不含任何脚本（已确认：仓库内不存在 `.py`、`.sh`、`.tcl`、`.mk` 等脚本文件），所有提取逻辑都要读者自己写。

| 文件 | 作用 |
| --- | --- |
| `vivado_reports/summary.xlsx` | 论文作者预先做好的**汇总结果表**，是批量提取的「标准答案」，本讲的对照基准。 |
| `vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt` | 一份干净的「实现阶段时序报告」样本，用于讲解 WNS 提取的行结构。100 MHz 频率点没有 `to_delete/` 干扰。 |
| `vivado_reports/reports_at_290Mhz/CIC Compiler/to_delete/timing_impl_R16_N4.txt` | 重复计数陷阱的「罪证」：它与上级目录同名文件**逐字节相同**，递归遍历会被算两次。 |

## 4. 核心概念与源码讲解

### 4.1 summary.xlsx 汇总表的定位与格式

#### 4.1.1 概念说明

整个 `vivado_reports/` 目录有 **655 个被 git 跟踪的文件**，绝大多数是成对的 `.txt` 与 `.rpx`。任何一篇分析（例如 u3-l1 的三方案资源对比、u3-l2 的 fmax 估算）都不可能靠人眼翻这几百份报告来完成——必须先把关键指标从每份报告里抠出来，**摊平成一张大表**：每一行是一个「频率 + 方案 + R + N」组合，每一列是一个指标（WNS、LUT、寄存器、DSP……）。

`summary.xlsx` 就是这张大表的**成品**：作者已经替你做完了「遍历 + 解析 + 聚合」这三步，论文里的对比图表多半都建立在它之上。换句话说：

- 各份 `.txt` 是**原始数据（raw data）**；
- `summary.xlsx` 是**聚合数据（aggregated data）**，是下游分析（u3-l1、u3-l2、u3-l3）直接消费的对象。

理解了这层关系，你就理解了本讲在整套手册里的位置：**它把「读报告」与「做分析」连接起来**——summary.xlsx 是连接点，而本讲授你如何自己重做这个连接点（用来核对、或扩展到作者没汇总的指标）。

#### 4.1.2 核心流程

从原始报告到汇总表，是一条单向流水线：

```
数百份 *_impl.txt 报告（原始数据，文本格式）
        │  ① 遍历：按 频率/方案 目录树定位文件
        ▼
        │  ② 解析：从每份 .txt 抠出 WNS、LUT、寄存器……
        ▼
        │  ③ 装配：文件名拆出 R、N、阶段；头部拆出 Design、Device
        ▼
一张扁平表（每行一个 R/N 组合，每列一个指标）
        │  ④ 落盘：.csv / .xlsx
        ▼
   summary.xlsx（成品）  ←─── 下游分析消费
```

关键判断：`.xlsx` 处于流水线的**末端**，是给人/pandas 看的；`.txt` 处于**始端**，是给脚本读的。两者的「可机读性」完全不同，这是下一小节的重点。

#### 4.1.3 源码精读

先确认这个文件确实存在、且是二进制：

[vivado_reports/summary.xlsx](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/summary.xlsx)

用 `file` 命令查看它的真实类型（**待本地验证**：本环境只读，无法改文件，但可在你本地执行）：

```bash
$ file vivado_reports/summary.xlsx
vivado_reports/summary.xlsx: Microsoft Excel 2007+
$ ls -l vivado_reports/summary.xlsx
-rw-r--r-- ... 13795 vivado_reports/summary.xlsx
```

「Microsoft Excel 2007+」就是 OOXML 格式——**本质上是一个 zip 压缩包**，里面装着一堆 XML（`xl/worksheets/sheet1.xml`、`xl/sharedStrings.xml` 等）。这就带来三个直接结论：

1. **不能用 `grep` 搜它**：你以为表格里的 "6.274"，在 zip 里被拆成「单元格引用 + 共享字符串」分散存储，文本搜索会漏掉或命中无关 XML。
2. **能用 `unzip -l` 看到内部结构**：因为它就是 zip。
3. **必须用 `openpyxl` / `pandas` 等库读**：让库替你把 XML 还原成二维表。

> ⚠️ `summary.xlsx` 的**具体 sheet 名、列名、行数待确认**——它是二进制，本讲无法在不打开 Excel 库的前提下列出其内部结构。这正是本模块代码实践要你亲自打开它来填空的原因。

#### 4.1.4 代码实践

**实践目标**：用 pandas 打开 `summary.xlsx`，探明它的内部结构（几个 sheet、各 sheet 的列名与行数）。

**操作步骤**（以下为**示例代码**，仓库不含任何脚本）：

```python
# 示例代码：探查 summary.xlsx 的结构
import pandas as pd

path = "vivado_reports/summary.xlsx"
xl = pd.ExcelFile(path)
print("sheet 名:", xl.sheet_names)          # ① 列出所有工作表
for s in xl.sheet_names:
    df = xl.parse(s)
    print(f"\n[{s}] 形状={df.shape}")        # ② 每个表的 (行数, 列数)
    print("列名:", list(df.columns))         # ③ 列名（这是判断它汇总了哪些指标的关键）
    print(df.head(3))                        # ④ 前三行预览
```

**需要观察的现象**：终端打印出 sheet 名清单、每个 sheet 的形状与列名。

**预期结果（待本地验证）**：你应当能看到至少一个 sheet，列名里大概率出现类似 `频率 / 方案 / R / N / WNS / LUT / 寄存器 / DSP` 这类字段——但**精确列名以你本机打开结果为准**，本讲不臆造。把你看到的列名记录下来，后续 4.4 的脚本输出要和它逐列对齐。

#### 4.1.5 小练习与答案

**练习 1**：同事说「我把 `summary.xlsx` 用 `grep -i wns` 搜了一遍，没搜到，所以这张表里没有 WNS」。这个结论对吗？为什么？

> **参考答案**：不对。`.xlsx` 是 OOXML 二进制（zip+XML），单元格里的数值不是明文，`grep` 搜不到不代表不存在。必须用 `pandas`/`openpyxl` 打开才能判断。

**练习 2**：`summary.xlsx` 与各份 `.txt` 报告，谁是「源」、谁是「派生」？如果两者对某个 WNS 数值不一致，应该信谁？

> **参考答案**：`.txt` 是源（由 Vivado 直接生成），`summary.xlsx` 是派生（人工/脚本聚合而来）。出现不一致时，**应以 `.txt` 为准**回去核对——因为聚合过程可能出错（例如漏读 `to_delete/` 之外的某种异常），而 `.txt` 是 Vivado 的原始输出。

---

### 4.2 从文本报告批量解析关键指标

#### 4.2.1 概念说明

上一模块说 `.xlsx` 不能文本解析；但 `.txt` **可以**，而且非常稳定——这正是 u2-l3 留下的伏笔：每份报告都由**固定的一条 Tcl 命令**产生（`report_timing_summary` 或 `report_utilization`），命令参数不变，文本布局就不变。这条「固定命令 → 固定布局」的规律，我称之为**解析契约（parse contract）**，它是批量提取能够可靠的前提。

契约的核心证据就在报告头部的 `Command` 字段里：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L1-L9](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L1-L9) —— 报告头部元信息块，`Command` 行（L6）回显了生成此报告的完整 Tcl 命令。

这条命令里 `-max_paths 10`、`-input_pins` 等开关决定了后续段落的行数，因此**只要所有报告的 Command 一致，关键段落就落在固定行号上**。

#### 4.2.2 核心流程

提取 WNS 的两步配方：

1. **定位**：找到 `| Design Timing Summary` 段落。
2. **取值**：该段落下有一行列出 12 个字段（WNS、TNS、…），紧接着是表头分隔线（一串 `-------`），**分隔线的下一行就是数据行，其第 1 个字段即 WNS**。

数据行（按空白拆分）的字段顺序是：

| 字段位 | 1 | 2 | 3 | 4 | 5 | 6 | … |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 含义 | **WNS** | TNS | TNS Failing Endpoints | TNS Total Endpoints | WHS | THS | … |

所以「取 WNS」=「数据行的第 1 个空白分隔字段」。

**两种实现策略**：

- **快捷法（行号硬编码）**：在这批报告里，数据行稳定在第 170 行，直接 `sed -n '170p' | awk '{print $1}'` 即可。它依赖「所有报告 Command 一致」，在本数据集成立（已交叉验证多份文件，WNS 均在 L170）。
- **稳健法（文本锚定，推荐）**：以 `Design Timing Summary` 为锚，跳过列头与分隔线后取下一行。不依赖行号，命令参数万一变了也能用。

#### 4.2.3 源码精读

看真实的时序报告段落，确认上述行结构：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L164-L173](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L164-L173) —— `Design Timing Summary` 段落，含列头（L168）、分隔线（L169）、WNS 数据行（L170）、收敛结论（L173）。

第 170 行原文是：

```
      6.274        0.000                      0                  386   ...
```

即 **WNS = 6.274 ns**（满足约束，因为 > 0），`TNS Failing Endpoints = 0`（无违例端点），与 u2-l1 的解读一致。第 173 行 `All user specified timing constraints are met.` 是文本结论，同样可被脚本当作「收敛标志」抓取。

#### 4.2.4 代码实践

**实践目标**：用最短的命令，从单份报告抠出 WNS。

**操作步骤**（**示例代码**）：

```bash
# 快捷法：直接取第 170 行第 1 字段（本数据集成立）
sed -n '170p' "vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt" | awk '{print $1}'
```

**需要观察的现象**：终端只打印一个数字。

**预期结果**：打印 `6.274`，与上一小节源码精读完全一致。

**进阶（稳健法，推荐写入正式脚本）**：

```bash
# 稳健法：用 Design Timing Summary 锚定，不依赖行号
awk '
  /Design Timing Summary/ {flag=1}
  flag && /^ *[-]+/ && !done {getline; print $1; done=1}
' "vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt"
```

> 这段逻辑：遇到 `Design Timing Summary` 后置标志位；遇到第一行纯分隔线（`-------`）就读下一行并打印第 1 字段。预期同样输出 `6.274`。**待本地验证**：不同 awk 实现的 `getline` 行为略有差异，若结果不符请改用 4.4 的 Python 版本。

#### 4.2.5 小练习与答案

**练习 1**：为什么「直接取第 170 行」在本数据集里是可靠的？它依赖什么前提？

> **参考答案**：依赖「所有 `*_impl.txt` 报告由同一条参数相同的 `report_timing_summary` 命令生成」，因此头部各段（Timer Settings、Report Methodology、check_timing）长度一致，关键段落落在固定行号。前提一旦破坏（如有人换了 `-max_paths`），行号就会漂移，此时应改用文本锚定法。

**练习 2**：如果想同时判断「是否收敛」，除了 WNS≥0 还能用报告里的哪个文本信号？

> **参考答案**：第 173 行的 `All user specified timing constraints are met.`（出现即收敛）。脚本里可对它做存在性匹配作为第二道交叉验证。

---

### 4.3 to_delete 重复目录陷阱

#### 4.3.1 概念说明

批量提取最大的敌人不是「读不懂报告」，而是「**把同一份数据算了两遍**」。本数据集里就埋着这么一个雷：`reports_at_290Mhz/CIC Compiler/` 下有一个名叫 `to_delete/` 的子目录，里面装着 11 份 `timing_impl` 报告，**与上级目录里的 11 份同名报告逐字节相同**。

这是 u2-l5 提到的「卫生型缺口（hygiene gap）」的具体形态——不是数据缺失，而是数据冗余。它的危害完全取决于你遍历文件的方式：

- **非递归**遍历（只看当前目录）：只读到 11 份，正确。
- **递归**遍历（`**/*.txt` 或 `find`）：读到 11 + 11 = 22 份，每个 WNS 被算两遍。

#### 4.3.2 核心流程

先看证据链，再讲规避方法：

1. **定位**：`to_delete/` 在整个仓库里**只出现一次**——`vivado_reports/reports_at_290Mhz/CIC Compiler/to_delete/`。其它频率点（100/300 MHz）、其它方案（MATLAB HDL Coder、Open-source CIC）都没有。
2. **比内容**：`to_delete/timing_impl_R16_N4.txt` 与上级 `timing_impl_R16_N4.txt` 的 md5 完全相同，`diff` 无任何输出，确系同一份文件的副本。
3. **数数量**：上级目录 11 份 `timing_impl`，`to_delete/` 也是同样的 11 份。递归遍历必得 22。
4. **规避**：在提取脚本里加一条路径过滤——凡是路径含 `to_delete` 一律跳过；或干脆只遍历到方案目录这一层、不递归进子目录。

#### 4.3.3 源码精读

[vivado_reports/reports_at_290Mhz/CIC Compiler/to_delete/timing_impl_R16_N4.txt:L164-L173](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_290Mhz/CIC%20Compiler/to_delete/timing_impl_R16_N4.txt#L164-L173) —— 这是 `to_delete/` 里那份副本的 `Design Timing Summary` 段落。

它的 WNS 数据行与上级目录那份**完全一样**。验证（**待本地验证**，本环境只读）：

```bash
# 以下命令本讲未在仓库内执行改动；只读校验
diff -q  "vivado_reports/reports_at_290Mhz/CIC Compiler/timing_impl_R16_N4.txt" \
         "vivado_reports/reports_at_290Mhz/CIC Compiler/to_delete/timing_impl_R16_N4.txt"
# 输出为空 = 两文件完全相同

md5sum "vivado_reports/reports_at_290Mhz/CIC Compiler/timing_impl_R16_N4.txt" \
       "vivado_reports/reports_at_290Mhz/CIC Compiler/to_delete/timing_impl_R16_N4.txt"
# 两行 md5 相同：1de94775f64ba5fa260e5609295eadc5（已在本讲核实）
```

> 目录命名 `to_delete` 本身就泄露了作者的意图——这些是「待删除」的副本，但它们**仍然被 git 跟踪**，所以对自动化脚本而言它们「真实存在」。靠文件名「望文生义」来跳过是不可靠的；必须写进过滤规则。

#### 4.3.4 代码实践

**实践目标**：亲眼看到重复计数的发生，并验证规避方法有效。

**操作步骤**（**示例代码**，已在本讲只读环境核实计数结果）：

```bash
cd "vivado_reports/reports_at_290Mhz/CIC Compiler"

echo "① 非递归（正确）："; ls timing_impl_*.txt | wc -l        # 期望 11
echo "② 递归（被污染）："; ls */timing_impl_*.txt timing_impl_*.txt | wc -l   # 期望 22
echo "③ 递归但排除 to_delete（正确）："
find . -name 'timing_impl_*.txt' -not -path '*/to_delete/*' | wc -l   # 期望 11
```

**需要观察的现象**：三行计数的对比。

**预期结果**：依次输出 `11`、`22`、`11`。第 ② 行就是陷阱——任何对 290 MHz 做全量统计的脚本，若不做路径排除，都会把每个 WNS/LUT 重复计入，导致均值、求和、资源总量全部失真。

> 把这条规则固化进脚本：**遍历报告时一律 `-not -path '*/to_delete/*'`（或等价的 `if "to_delete" in path: continue`）**。这是本讲最重要的一条工程习惯。

#### 4.3.5 小练习与答案

**练习 1**：如果你对「290 MHz CIC Compiler 的全部 timing_impl 报告」递归提取 WNS 再求**平均值**，结果会被 `to_delete/` 污染吗？为什么？

> **参考答案**：平均值**不会**被污染——因为重复的 11 份与原 11 份数值相同，`(11x + 11x)/22 = x`，均值不变。但**计数、求和、资源总量**会被翻倍污染。这提醒我们：不同统计量对重复数据的敏感度不同，不能因为「均值看着对」就掉以轻心。

**练习 2**：除了 `to_delete/`，本讲提到的另一类「不可文本解析、混进遍历会出问题」的文件是什么？怎么排除？

> **参考答案**：`.rpx` 文件（u2-l3 讲过，Vivado GUI 二进制报告）。遍历时若用 `*.txt` 通配就能天然排除 `.rpx`；若用 `*` 全收则要显式过滤扩展名。`.rpx` 和 `to_delete/` 是本数据集批量提取的两大陷阱。

---

### 4.4 批量提取脚本与去重（pandas）

#### 4.4.1 概念说明

把前三个模块拼起来：遍历 → 过滤（排 `to_delete/`、排 `.rpx`）→ 解析 WNS → 从文件名拆出 R/N → 从头部拆出 Design/Device → 装进一张表 → 落盘 CSV。用 Python + pandas 写最顺手，因为最后一步天然得到 DataFrame，可以直接和 `summary.xlsx` 对齐核对。

「pandas/脚本」在规格里标注**待确认**——因为仓库不含任何现成脚本，以下脚本都是本讲为读者写的**示例代码**，需要你在本地实际运行、并根据 `summary.xlsx` 的真实列名微调。

#### 4.4.2 核心流程

一条带去重的提取流水线：

```
方案目录（如 reports_at_100Mhz/CIC Compiler/）
   │  ① glob: timing_impl_*.txt        ← 天然排除 .rpx
   │  ② 过滤: 路径不含 to_delete        ← 排除重复副本
   ▼
对每个文件:
   │  ③ 文件名正则: timing_impl_R(\d+)_N(\d+) → R, N
   │  ④ 头部解析: Device 行 → 器件; Design 行 → 方案
   │  ⑤ 锚定 "Design Timing Summary" → 取数据行第 1 字段 → WNS
   ▼
list[dict]  →  pandas.DataFrame  →  wns_100_cic_compiler.csv
   │
   ▼ （可选）与 summary.xlsx 同 sheet 对齐，逐行核对 WNS
```

文件名的解析契约是 `timing_<阶段>_R<抽取率>_N<级数>.txt`，按下划线分段后倒数三段分别给出阶段、R、N——这是把文件名当结构化数据用的关键。

#### 4.4.3 源码精读

回顾两处依赖的源码事实：

- [timing_impl_R16_N4.txt:L1-L9](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L1-L9)：头部 `Design : cic_compiler_0`（L7）、`Device : 7a100t-csg324`（L8）——脚本据此自动填方案与器件列，无需手填。
- [timing_impl_R16_N4.txt:L164-L173](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L164-L173)：WNS 数据在 L170，取第 1 字段得 6.274。

#### 4.4.4 代码实践

**实践目标**：从「100 MHz / CIC Compiler」下的全部 `timing_impl` 报告提取 WNS，生成 CSV；再与 `summary.xlsx` 核对。

**操作步骤**（**示例代码**，仓库不含脚本，需你本地创建并运行）：

```python
# 示例代码：extract_wns.py —— 从单个方案目录批量提取 WNS
import re, glob, os
import pandas as pd

DIR = "vivado_reports/reports_at_100Mhz/CIC Compiler"
rows = []
for path in glob.glob(os.path.join(DIR, "timing_impl_*.txt")):   # ① 天然排除 .rpx
    if "to_delete" in path:                                       # ② 排除重复副本
        continue
    name = os.path.basename(path)
    m = re.search(r"timing_impl_R(\d+)_N(\d+)", name)             # ③ 文件名拆 R/N
    R, N = int(m.group(1)), int(m.group(2))
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    wns = design = device = None
    for i, ln in enumerate(lines):
        if "| Design :" in ln:    design = ln.split(":", 1)[1].strip()
        if "| Device :" in ln:    device = ln.split(":", 1)[1].strip()
        if "Design Timing Summary" in ln:                          # ⑤ 稳健锚定
            data = lines[i + 3].split()                           # 跳过 列头/空行/分隔线
            wns = float(data[0])
            break
    rows.append({"R": R, "N": N, "WNS": wns, "Design": design, "Device": device})

df = pd.DataFrame(rows).sort_values(["R", "N"]).reset_index(drop=True)
df.to_csv("wns_100_cic_compiler.csv", index=False)
print(df)
print("行数:", len(df))    # 期望 11（100 MHz 无 to_delete，故 11）
```

> 锚定取值的细节：`Design Timing Summary` 出现后，`lines[i+1]` 是空行、`lines[i+2]` 是列头 `WNS(ns) ...`、`lines[i+3]` 是分隔线 `-------`。注意：若 `i+3` 仍是分隔线，需改取 `i+4`——**待本地验证**后再固化偏移量。这正说明「文本锚定」比「硬编码行号 170」更值得做一次校准后长期使用。

**需要观察的现象**：终端打印一张 11 行的表，按 R、N 排序。

**预期结果（部分锚点，已在本讲只读环境核实）**：

| R | N | WNS（ns） |
| --- | --- | --- |
| 16 | 4 | 6.274 |
| 16 | 6 | 5.692 |
| 64 | 6 | 5.730 |
| … | … | （共 11 行，全为正数，均收敛） |

行数应为 **11**。若你得到 22，说明遍历误入了某子目录（在 100 MHz 不应发生，请检查 glob）。

**与 summary.xlsx 核对（可选）**：

```python
# 示例代码：把自提结果与作者汇总表对齐
summ = pd.read_excel("vivado_reports/summary.xlsx")     # sheet/列名待确认
# 选出 100MHz、CIC Compiler 的行，按 R/N 对齐后比较 WNS 列
# （列名以你 4.1.4 探查结果为准，本讲不臆造列名）
```

**关于 to_delete 是否造成重复计数**：在 100 MHz 这一档**不会**——因为 `to_delete/` 只存在于 290 MHz 下。本脚本已含 `if "to_delete" in path: continue` 这道防线，即便你把 `DIR` 改成 290 MHz 的 CIC Compiler 目录，行数也仍会是 11 而非 22。**这条 `if` 就是你对「to_delete 重复目录陷阱」的标准答案**。

#### 4.4.5 小练习与答案

**练习 1**：把脚本里的 `if "to_delete" in path: continue` 删掉，并把 `DIR` 指向 290 MHz 的 CIC Compiler 目录，行数会变成多少？为什么？

> **参考答案**：变成 **22**。因为 290 MHz CIC Compiler 上级目录有 11 份、`to_delete/` 里又有逐字节相同的 11 份，递归/通配会把两套都收进来。这正重复了 4.3 的实验，用脚本形式再现陷阱。

**练习 2**：脚本为什么用 `glob("timing_impl_*.txt")` 而不是 `glob("*")`？

> **参考答案**：限定 `timing_impl_*` 只取**实现阶段的时序报告**（排除 synth 综合阶段报告），限定 `.txt` 后缀天然排除同名的 `.rpx` 二进制文件。这两条约束同时保证了「取对阶段」和「不碰二进制」。

**练习 3**：如果作者之后给某个频率点新增了 `utilization` 报告，本脚本需要改动吗？

> **参考答案**：不需要。本脚本只 glob `timing_impl_*.txt`，与 utilization 报告无关，互不干扰。若要扩展到利用率指标，应另写一份针对 `utilization_impl_*.txt` 的解析（锚点改为 `Slice Logic` 段落的 `Slice LUTs` 行），但遍历、去重、落盘的骨架可完全复用。

---

## 5. 综合实践

把本讲四个模块串成一个**端到端的「自建汇总表」任务**，并和作者的 `summary.xlsx` 做一次对抗式核对。

**任务**：用一份脚本，从**整个仓库**（三个频率 × 三种方案，排除 `to_delete/` 与 `.rpx`）的全部 `timing_impl` 报告里提取 WNS，生成一张扁平表 `my_summary.csv`，列至少包含：`频率 / 方案 / R / N / WNS / 是否收敛`，然后回答三个问题。

**建议步骤**：

1. **遍历设计**：从 `vivado_reports/reports_at_*Mhz/<方案>/` 这一层的 `timing_impl_*.txt` 取文件；频率与方案直接从路径段解析（目录名 `reports_at_100Mhz` → 频率 100；`CIC Compiler`/`MATLAB HDL Coder`/`Open-source CIC` → 方案）。
2. **去重**：全程带 `to_delete` 过滤。
3. **收敛标志**：除 WNS 外，额外匹配 `All user specified timing constraints are met.` 作为「是否收敛」布尔列。
4. **核对**：用 `pandas` 读 `summary.xlsx`（先按 4.1.4 探明其列名），筛选出相同「频率×方案×R×N」的行，按 R、N 对齐后比较两份 WNS。
5. **填空**：把你在 4.1.4 探到的 `summary.xlsx` 真实列名补进本讲对应位置。

**要回答的三个问题**：

- 你的 `my_summary.csv` 总共多少行？是否覆盖了 u2-l5 给出的实验矩阵（100 MHz 三方案齐全、290 MHz 仅 CIC Compiler、300 MHz 缺 Open-source）？
- 你的 WNS 与 `summary.xlsx` 是否逐行一致？若有不一致，是落在哪个频率/方案？（提示：若不一致集中出现在 290 MHz，先怀疑 `to_delete/` 是否被算重。）
- 在你的表里，哪个「频率×方案×R×N」组合的 WNS 最接近 0（最危险、最接近时序失败）？（参考：u3-l2 指出 290 MHz R64_N6 仅余 0.163 ns。）

> 完成本任务后，你就拥有了**独立复现作者汇总表**的能力——这是后续 u3-l6（复现实验方法论）的数据层基础。

## 6. 本讲小结

- `summary.xlsx` 是流水线**末端**的聚合成品（给人/pandas 看），各份 `.txt` 是**始端**的原始数据（给脚本读）；两者「可机读性」不同，`.xlsx` 与 `.rpx` 同属二进制，不能 `grep`。
- `.txt` 报告由固定的 Tcl 命令生成，文本布局稳定，构成可靠的**解析契约**；WNS 在 `Design Timing Summary` 段落数据行的第 1 字段（本数据集稳定落在 L170）。
- 提取 WNS 有两种策略：行号硬编码（快捷，依赖 Command 一致）与文本锚定（稳健，推荐写入正式脚本）。
- **`to_delete/` 重复目录**是批量提取的最大陷阱：它只在 290 MHz CIC Compiler 下出现，含 11 份与上级逐字节相同的副本，递归遍历会把计数/求和翻倍；标准规避是路径过滤 `if "to_delete" in path: continue`。
- 第二大陷阱是 `.rpx`：用 `*.txt` 通配天然排除；切勿把二进制报告当文本解析。
- 一条合格的提取脚本 = 遍历（限 `timing_impl_*.txt`）+ 去重（排 `to_delete`）+ 文件名/头部解析 + WNS 锚定 + 落盘 CSV，最后可选地与 `summary.xlsx` 对齐核对。

## 7. 下一步学习建议

- **向利用率指标扩展**：把本讲的骨架（遍历+去重+落盘）复用到 `utilization_impl_*.txt`，锚点改为 `Slice Logic` 段落，提取 Slice LUTs / Registers / DSP / Block RAM。这会直接喂给 [u3-l1 资源利用率横向对比分析](u3-l1-resource-comparison.md) 的跨方案对比表。
- **走向复现**：本讲只做「读已有报告」。若你想自己重新生成这些报告，进入 [u3-l6 复现实验的方法论](u3-l6-reproduction-methodology.md)，那里讲 Vivado 综合/实现流程与 `report_timing_summary` / `report_utilization` 命令的完整用法——注意仓库不含 Tcl/工程文件，需自行补齐。
- **补全 summary.xlsx 结构**：本讲把 `summary.xlsx` 的 sheet/列名留作「待确认」。建议你用 4.1.4 的脚本亲自打开它，把结构记录下来，回头修订本讲里的占位描述——这是把「待确认」变为「已确认」的闭环练习。
