# 数据进出：Touchstone 与 CSV 导入导出

## 1. 本讲目标

学完本讲，你应该能够：

1. 徒手写出一份合法的 Touchstone S1P/S2P 文件（选项行、数据行、参数顺序、单位换算全部自己构造），并逐字段解释它每一列的含义。
2. 跟踪一次完整的导入链路：文件 → `Touchstone::fromFile` → `Trace::createFromTouchstone` → `TraceImportDialog` → `TraceModel`，说清楚"文件是怎么变成一条 Trace 的"。
3. 跟踪一次完整的导出链路：`TraceTouchstoneExport` / `TraceCSVExport` 对话框 → `Touchstone::toFile` / `CSV::toFile`，理解端口数选择、单位选择、小数位数与 S12/S21 交换这些关键细节。
4. 区分 Touchstone 与 CSV 两种格式各自"存得下什么、存不下什么"，为数据交换选择合适的格式与列组合。

本讲仍然完全不需要硬件——导入导出是纯 GUI 侧行为，用手工构造的文件就能完成全部实践。

## 2. 前置知识

- **S 参数回顾**（承接 u1-l1、u8-l1）：S 参数把器件当成"黑盒网络"，\( S_{ij} \) 表示"从端口 j 注入激励、在端口 i 测得的反射/传输波之比"，是**无量纲的复数**（含幅度与相位）。单端口器件只有一个 \( S_{11} \)，双端口器件有 \( S_{11}, S_{12}, S_{21}, S_{22} \) 四个。
- **Trace 数据模型回顾**（承接 u8-l1）：GUI 内部一条 Trace 就是"一列按 X 升序排列的 `(x, 复数y)` 样本"；文件导入的 Trace 来源标记为 `Source::File`。
- **dB 换算**：本代码库统一用 \( |S|_{dB} = 20\lg|S| \)（S 参数是无量纲比值，所以是 20 而不是 10），反过来 \( |S| = 10^{dB/20} \)。实现见 [Util/util.h:L30-L35](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/util.h#L30-L35)。
- **复数的三种"人读"写法**——这正是 Touchstone 格式选项里的三种 Format：
  - `RI`（Real/Imaginary）：实部 虚部，如 `0.1 0.2`；
  - `MA`（Magnitude/Angle）：模 相位(度)，如 `0.224 63.4`；
  - `DB`（dB/Angle）：20lg|S| 相位(度)，如 `-13.0 63.4`。
  三者只是同一个复数的不同记法：\[ S = a + jb = |S|\,e^{j\varphi},\quad |S|_{dB} = 20\lg|S|,\quad \varphi = \arctan\frac{b}{a} \]
- **CSV**：逗号分隔值的纯文本表格，第一行是表头。它比 Touchstone "俗"得多，但通用：任何表格软件都能打开。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [Software/PC_Application/LibreVNA-GUI/touchstone.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/touchstone.cpp) | Touchstone 文件的解析器（`fromFile`）与生成器（`toString`/`toFile`），以及端口裁剪、插值等工具 |
| [Software/PC_Application/LibreVNA-GUI/csv.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/csv.cpp) | 极简 CSV 读写器：列(表头+double 数组)的容器 |
| [Software/PC_Application/LibreVNA-GUI/CustomWidgets/touchstoneimport.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/touchstoneimport.cpp) | "选一个 Touchstone 文件并挑端口"的可复用控件（用在校准件编辑、Trace 编辑对话框里，**不是**主导入入口） |
| [Software/PC_Application/LibreVNA-GUI/Traces/tracetouchstoneexport.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracetouchstoneexport.cpp) | Touchstone 导出对话框：选端口数、把 Trace 填进 S 矩阵、选单位与格式 |
| [Software/PC_Application/LibreVNA-GUI/Traces/tracecsvexport.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracecsvexport.cpp) | CSV 导出对话框：选 Trace、选要导出的 Y 轴量（幅度/相位/实虚部…） |
| [Software/PC_Application/LibreVNA-GUI/Traces/tracewidget.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewidget.cpp) | 主导入入口 `importFile`：按扩展名分流到 CSV/Touchstone，再弹出 TraceImportDialog |
| [Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp) | `fillFromTouchstone`/`fillFromCSV`/`createFromTouchstone`/`createFromCSV`：把文件数据灌进 Trace |
| [Documentation/Measurements/Mini-circuits_VAT-6+.s2p](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/Measurements/Mini-circuits_VAT-6%2B.s2p) | 仓库自带的真实 S2P 示例，可当"标准答案"对照 |

## 4. 核心概念与源码讲解

### 4.1 Touchstone 解析：一个自描述的文本协议

#### 4.1.1 概念说明

Touchstone（扩展名 `.s1p`/`.s2p`/…）是射频行业事实标准的 S 参数交换格式，几乎所有 VNA 软件和仿真器都认它。它是一个纯文本格式，结构极简：

```
! 这是注释（感叹号开头，可出现在任何行的任何位置）
# GHZ S DB R 50          <- 选项行（option line），以 # 开头
0.001 -44.02 -4.54 ...   <- 数据行：频率 + 若干 S 参数对
```

选项行四个字段依次是：**频率单位**（HZ/KHZ/MHZ/GHZ）、**参数种类**（本代码库只支持 S）、**数据格式**（DB/MA/RI）、**参考阻抗**（`R 50` 即 50 Ω）。

为什么 GUI 需要自己写解析器而不是随便找个库？因为这个类不止做 I/O：校准件建模（u9-l1）要读标准件的 Touchstone、阻抗匹配网络要去嵌入端口数据，都需要把文件读成结构化的 `Datapoint` 数组并可裁剪端口。所以 `Touchstone` 是一个被校准、去嵌入、导入三大功能共用的底层数据结构。

#### 4.1.2 核心流程

`fromFile` 的解析流程（逐行状态机）：

1. **从文件名推端口数**：找最后一个 `.`，检查后续三个字符必须是 `s/S` + 数字 1–9 + `p/P`，端口数 = 那个数字。**文件内容再对，扩展名不规范也会被拒收**。
2. 逐行读取：
   - 删掉 `!` 及其后的注释；
   - 去掉行首空白；纯空白行跳过；
   - 若该行以 `#` 开头 → 解析选项行（全大写化后逐 token 匹配）；
   - 否则 → 数据行，从 token 流里逐个取数。
3. 数据行按"参数对计数器"拼装：每行先读频率（乘以单位系数归一到 Hz），再读 `parameters_per_line` 个复数；凑满 `ports²` 个参数就打包成一个 `Datapoint` 提交。
4. **两端口特例**：文件里四个参数的顺序是 S11 S21 S12 S22，而内部存储是矩阵行优先的 S11 S12 S21 S22，所以凑满时要 `swap(S[1], S[2])`。

三种格式读入时的换算：

\[ \text{RI}: S = p_1 + j\,p_2 \qquad \text{MA}: S = p_1\,e^{j p_2\pi/180} \qquad \text{DB}: S = 10^{p_1/20}\,e^{j p_2\pi/180} \]

写文件（`toString`）是逆过程：先写选项行，再逐点写"频率 + 各参数"，1/2 端口一行一个点，≥3 端口按矩阵行优先、每固定列数换行。

#### 4.1.3 源码精读

**① 端口数来自文件名，而非文件内容** —— [touchstone.cpp:L148-L154](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/touchstone.cpp#L148-L154)：取最后一个 `.` 后的字符，必须是 `s`+`1..9`+`p`，端口数直接由这个数字构造 `Touchstone(ports)`。这解释了实践里最容易踩的坑：把文件存成 `test.txt` 或 `test.sIp` 都会抛 `Invalid filename extension`。

**② 选项行解析：一个 token 循环 + R 的"占位等待"** —— [touchstone.cpp:L157-L231](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/touchstone.cpp#L157-L231)：

- 初始默认值 `Scale::GHz` + `Format::RealImaginary`（L157-158），构造函数里默认 `referenceImpedance = 50.0`（[L17-L22](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/touchstone.cpp#L17-L22)）——与规范缺省值一致；
- 遇到 `R` 时置 `last_R = true`，下一轮循环取到的 token 才是阻抗数值（L195-199），**取完直接 `break`**，意味着 `R` 必须是选项行最后一个选项（规范也如此约定）；
- `Y/Z/G/H` 参数直接抛异常（L211-218），说明本库是"S 参数专用"解析器；
- 数据行若出现在选项行之前，抛 `First dataline before option line`（L234-236）——比规范更严格：本解析器**要求**选项行必须存在。

**③ 复数解析 lambda：三种格式归一为 `complex<double>`** —— [touchstone.cpp:L237-L255](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/touchstone.cpp#L237-L255)：MA 用 `polar(part1, part2/180*π)`，DB 先 `pow(10, part1/20)` 再 polar，RI 直接构造。注意角度单位是**度**不是弧度，且解析失败时把行号和频率拼进错误消息——这是手工构造文件出错时最重要的调试线索。

**④ 每行参数对数与 S12/S21 交换** —— [touchstone.cpp:L267-L288](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/touchstone.cpp#L267-L288)：`parameters_per_line` 规则是 1 端口每行 1 对、3 端口每行 3 对、其余（含 2 端口和 ≥4 端口）每行 4 对。凑满 `ports*ports` 个后，若 `ports == 2` 则 `swap(point.S[1], point.S[2])`。这是 Touchstone 1.x 规范留下的历史包袱：**两端口文件中的物理顺序是 S11 S21 S12 S22**（传输项在前），而代码内部 `S[i*ports+j]` 是行优先矩阵序（S11 S12 S21 S22）。

**⑤ 写文件时的镜像交换** —— [touchstone.cpp:L104-L117](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/touchstone.cpp#L104-L117)：写两端口时按 S[0]、S[2]、S[1]、S[3] 的顺序输出（注释也写明 "swap S12 and S21"），与读取端的 swap 互为逆操作——读写对称，往返不变形。≥3 端口按矩阵序直接展开，每 4 个（3 端口每 3 个）换行（[L118-L133](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/touchstone.cpp#L118-L133)）。⚠️ 标准 Touchstone 对 ≥4 端口的排列有更细的规定，本实现是简化版，与其他软件交换 ≥4 端口文件时兼容性**待确认**。

**⑥ 小数位数与频率单位** —— [touchstone.cpp:L60-L80](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/touchstone.cpp#L60-L80)：`std::fixed << setprecision(12)`，即**固定 12 位小数**；频率按所选单位除以 10 的幂（L96-102）。12 位小数对 |S|≈1 的量意味着约 \(10^{-12} \) 分辨率，远低于测量噪声（典型 \(10^{-3} \) 量级），可以认为是无损写出。

**⑦ 端口裁剪——给校准件和导入向导用的工具** —— [touchstone.cpp:L339-L381](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/touchstone.cpp#L339-L381)：`reduceTo2Port`/`reduceTo1Port` 从 N 端口数据中抽出指定端口组合的子矩阵（注意 L348-350：如果本来就是 2 端口文件，取出的行列索引还要先经过一次 S12/S21 交换修正）。`interpolate`（[L313-L337](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/touchstone.cpp#L313-L337)）对频率做线性插值，端点外直接取首末点。

**⑧ 真实样例** —— [Documentation/Measurements/Mini-circuits_VAT-6+.s2p:L1-L3](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/Measurements/Mini-circuits_VAT-6%2B.s2p#L1-L3)：选项行 `# GHZ S DB R 50`，数据行第一点 `0.001 -44.025 -4.548 -5.632 -0.607 -5.569 0.138 -45.891 -8.410`——即 1 MHz 处 S11=-44.03 dB/-4.55°、S21=-5.63 dB/-0.61°、S12=-5.57 dB/0.14°、S22=-45.89 dB/-8.41°。这是一个 10 dB 衰减器（VAT-6+）：传输约 -5.6 dB，反射优于 -44 dB。

#### 4.1.4 代码实践

**实践目标**：不依赖任何仪器，手工构造一份两点 S1P 文件并用 GUI 验证解析结果与手算一致。

1. **操作步骤**：
   - 用文本编辑器新建 `myfirst.s1p`（扩展名必须是 `s1p`），内容如下：

     ```
     ! 我的第一份 Touchstone 文件
     # HZ S RI R 50
     1000000 0.1000 0.2000
     2000000 -0.3000 0.1000
     ```

   - 启动 GUI（按 u1-l3 的方式编译运行，无需设备），在 VNA 模式的 Trace 列表面板点 **Import** 按钮，选择该文件；
   - 在弹出的导入对话框确认参数名（默认带文件名前缀 `myfirst_S11`），点 OK；
   - 新建一个 XY 图，把 Trace 加进去，Y 轴选 Magnitude 与 Phase，再建一个 Marker 放到 1 MHz。
2. **需要观察的现象**：
   - 导入对话框不报错，Trace 列表出现新 Trace；
   - Marker 在 1 MHz 处读数应为 |S11| ≈ **-13.01 dB**，相位 ≈ **63.43°**；2 MHz 处 ≈ **-10.1 dB**、161.57°。
3. **预期结果验证**（手算）：
   \[ |S_{11}| = \sqrt{0.1^2+0.2^2} = \sqrt{0.05} \approx 0.2236,\quad 20\lg 0.2236 \approx -13.01\ \text{dB},\quad \varphi=\arctan\frac{0.2}{0.1} \approx 63.43^\circ \]
   与 GUI 读数一致即证明选项行（HZ、RI）、频率换算、复数解析全部按预期工作。
4. **附加实验**：把选项行改成 `# MHZ S MA R 50`、数据行频率改成 `1 0.2236 63.43`，再次导入——读数应完全相同。这验证了单位缩放（[touchstone.cpp:L260-L265](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/touchstone.cpp#L260-L265)）与 MA→RI 的换算。若把扩展名改成 `.txt`，导入应失败——端口数来自文件名。

以上运行现象为源码逻辑推导所得，具体操作结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：一份 `# GHZ S DB R 50` 的 S2P 文件某数据行为：
`3.0 -20.0 0.0 -3.0 45.0 -3.0 -45.0 -30.0 90.0`
请写出内部存储的 `S[0..3]`（行优先矩阵序）。

**答案**：文件顺序是 S11 S21 S12 S22，故文件给的四个参数为 S11=-20dB/0°，S21=-3dB/45°，S12=-3dB/-45°，S22=-30dB/90°。换算幅度：\( 10^{-20/20}=0.1 \)，\( 10^{-3/20}\approx0.708 \)，\( 10^{-30/20}\approx0.0316 \)。内部矩阵序为：
S[0]=0.1∠0°，S[1]=S12=0.708∠−45°，S[2]=S21=0.708∠45°，S[3]=0.0316∠90°。
（这正是 [touchstone.cpp:L281-L284](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/touchstone.cpp#L281-L284) 那次 swap 的效果。）

**练习 2**：如果不写选项行，直接从数据行开始，`fromFile` 会怎样？规范又是怎么说的？

**答案**：代码在 [touchstone.cpp:L234-L236](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/touchstone.cpp#L234-L236) 抛出 `First dataline before option line`——本实现把选项行当作必需。而 Touchstone 规范里选项行可省略（缺省按 `# GHZ S RI R 50` 处理）。给本软件写文件时请永远带上选项行。

**练习 3**：`AddDatapoint` 为什么要检查 `m_datapoints.back().frequency >= p.frequency`？

**答案**：见 [touchstone.cpp:L30-L39](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/touchstone.cpp#L30-L39)。Trace/绘图体系要求 X 轴单调升（u8-l1），若新点频率不大于末点频率，就把整个数组重排一次，保证无论文件里点序多乱，读入后都是升序。

### 4.2 导入/导出对话框：文件与 Trace 之间的桥

#### 4.2.1 概念说明

这一模块回答两个问题：**"文件怎么变成 Trace 进模型"** 和 **"Trace 怎么变回文件"**。仓库里其实有两条导入路径，初学者很容易混淆：

1. **主导入路径**（Trace 面板的 Import 按钮/拖放文件）：`TraceWidget::importFile`，把整个文件展开成一组 Trace（S2P → 4 条 Trace），经 `TraceImportDialog` 让用户改名、选颜色后加入 `TraceModel`。
2. **TouchstoneImport 控件**（`CustomWidgets/touchstoneimport.cpp`）：一个"选文件 + 挑端口 + 显示点数/频率范围/状态"的复合控件，用在**需要从多端口文件里挑出特定端口**的场合——校准件编辑（calstandard.cpp 四处）和 Trace 编辑对话框（traceeditdialog.cpp）。它不往 TraceModel 里加 Trace，只产出裁剪后的 `Touchstone` 对象。

导出侧则是两个对称的对话框：`TraceTouchstoneExport`（S 参数网格 → .sNp）和 `TraceCSVExport`（任意 Y 轴量 → .csv）。

#### 4.2.2 核心流程

**导入（主路径）**：

```
用户点 Import / 拖放文件
  └─ TraceWidget::importDialog  (按扩展名过滤: VNA 模式接受 csv,s1p..s4p; SA 模式只接受 csv)
      └─ TraceWidget::importFile
          ├─ .csv  → CSV::fromFile → Trace::createFromCSV
          └─ 其他  → Touchstone::fromFile → Trace::createFromTouchstone (生成 ports² 条 Trace, 命名 Sij)
          ├─ 文件名(去目录去扩展名) + "_" 作为 Trace 名前缀
          ├─ 弹 TraceImportDialog → 用户确认 → TraceParameterModel::import(model) 加入 TraceModel
          └─ 若导入的恰好是完整 S 参数方阵且有校准/去嵌入 → 询问是否套用 (cal->correctTraces / deembed->Deembed)
```

**导出（Touchstone）**：

```
用户点 Export → Touchstone
  └─ TraceTouchstoneExport 对话框
      ├─ setPortNum: 按名字 "Sij" 自动匹配默认 Trace
      ├─ on_buttonBox_accepted:
      │    选文件名(自动补 .sNp) → 逐采样点组装 Datapoint
      │    (缺的 Trace 用 0 填充) → 选单位(HZ..GHZ)/格式(DB/MA/RI)
      │    → Touchstone::toFile → 记住本次设置到 Preferences
```

#### 4.2.3 源码精读

**① 扩展名分流与格式白名单** —— [tracewidget.cpp:L232-L256](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewidget.cpp#L232-L256)：`importFile` 取最后一个 `.` 之后的扩展名查白名单。白名单是虚函数：VNA 模式 [`{"csv","s1p","s2p","s3p","s4p"}`](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/tracewidgetvna.h#L16)，频谱仪模式[只有 `{"csv"}`](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/tracewidgetsa.h#L12)——因为 SA 数据是绝对功率而非 S 参数比值。

**② 文件 → Trace 的实体转换** —— [trace.cpp:L1095-L1108](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1095-L1108)：`createFromTouchstone` 对每个参数建一条空 Trace，调 `fillFromTouchstone(t, i)` 灌数据，再按 `sink = i/ports+1, source = i%ports+1` 命名为 `S<sink><source>`。而 [trace.cpp:L228-L257](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L228-L257) 的 `fillFromTouchstone` 做三件关键事：`domain = Frequency`；逐点 `d.x = 频率, d.y = S[parameter]`；并用 `parameter == i*ports+i` 判定**是否反射参数**（S11/S22…，Smith 图等只收反射量，u8-l2 的"闸门"就靠这个标志）。参考阻抗也从文件带入（L254）。

**③ 导入确认对话框与改名前缀** —— [tracewidget.cpp:L258-L272](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewidget.cpp#L258-L272)：文件名去掉目录和扩展名后加 `_` 作前缀（`myfirst.s1p` → `myfirst_S11`），TraceImportDialog 让用户批量确认；确认后 [traceimportdialog.cpp:L26-L30](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceimportdialog.cpp#L26-L30) 才真正调 `tableModel->import(model)` 写入 TraceModel。

**④ 给完整 S 参数方阵的"加餐"** —— [tracewidget.cpp:L272-L315](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracewidget.cpp#L272-L315)：若导入的是全部 ports² 条 Trace 且当前存在校准或去嵌入配置，会弹对话框询问是否对**导入的**数据也套用 `cal->correctTraces` / `deembed->Deembed`。即导入的文件数据可以走一遍与实测相同的后处理管线——这是"离线重算校准"的入口。

**⑤ TouchstoneImport 控件：挑端口的状态机** —— [touchstoneimport.cpp:L122-L173](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/touchstoneimport.cpp#L122-L173)：`evaluateFile` 是核心——每次文件名变化就重新 `Touchstone::fromFile`，成功则点亮"文件里实际存在的端口"单选钮、显示点数与上下频限、`status=true` 并发 `statusChanged` 信号（让外层对话框的 Update 按钮解禁）；失败则把异常文本写进状态栏。[L175-L194](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/touchstoneimport.cpp#L175-L194) 的 `preventCollisionWithGroup` 保证 port1/port2 不会选中同一物理端口。最终 [L53-L66](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/touchstoneimport.cpp#L53-L66) 的 `getTouchstone` 用 `reduceTo1Port`/`reduceTo2Port` 裁出用户选的端口组合。

**⑥ 导出对话框：按名字自动排兵布阵** —— [tracetouchstoneexport.cpp:L69-L96](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracetouchstoneexport.cpp#L69-L96)：`setPortNum` 改端口数时，遍历 `S11..Snn` 的名字在 TraceModel 里找"名字包含该串"的 Trace 自动填格——这就是为什么按默认命名（S11/S21/…）工作的 Trace 导出时几乎不用手动选。

**⑦ 导出执行：Trace 网格 → Datapoint → 文件** —— [tracetouchstoneexport.cpp:L98-L140](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracetouchstoneexport.cpp#L98-L140)：逐采样点组装 `Datapoint`：外两层循环 `i`(接收口)×`j`(激励口) 把 `selector->getTrace(i,j)->sample(s).y` 按行优先压入 `S`；**缺格的 Trace 直接补 0**（L113-115）；频率取自任意一条 Trace 的 `sample(s).x`（注释明说各 Trace 频点本就相同）。单位/格式由两个下拉框映射成 `Touchstone::Scale`/`Format`（L126-138），最后 `t.toFile(filename, unit, format)`。L142-158 把端口数、格式、单位、Trace 名单存进 Preferences，下次打开对话框自动恢复（构造函数 [L22-L40](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracetouchstoneexport.cpp#L22-L40) 读回）。

**⑧ 导出菜单的挂载点** —— [tracewidgetvna.cpp:L15-L41](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/tracewidgetvna.cpp#L15-L41)：VNA 模式 Trace 面板的 Export 按钮挂一个菜单，两个动作分别 `new TraceCSVExport(model)` 与 `new TraceTouchstoneExport(model)`。注意只在 `AppWindow::showGUI()` 为真时 `show()`——无头模式下不会弹窗。

#### 4.2.4 代码实践

**实践目标**：亲手走一遍"导入 → 导出"往返（round-trip），验证 S12/S21 交换与 12 位小数确实如源码所述。

1. **操作步骤**：
   - 用 u1-l3 的方式导入仓库自带示例 `Documentation/Measurements/Mini-circuits_VAT-6+.s2p`（4 条 Trace：S11/S12/S21/S22）；
   - 点 Export → Touchstone，端口数选 2，确认 S 网格自动填好，格式选 **DB**、单位选 **GHz**，保存为 `roundtrip.s2p`；
   - 用文本编辑器并排打开原文件与 `roundtrip.s2p`，比对选项行与第一行数据。
2. **需要观察的现象**：
   - `roundtrip.s2p` 选项行应为 `# GHZ S DB R 50`（四个 token 的顺序由 [touchstone.cpp:L64-L80](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/touchstone.cpp#L64-L80) 的固定书写顺序决定）；
   - 每行数值为固定 12 位小数；
   - 每行第 3、4 个数（文件序的 S21）与原文件相同——因为原文件也是 DB/GHz，往返只做了两次互逆的 swap。
3. **预期结果**：数值在 12 位小数内逐位一致（若原文件只有 12 位小数则应完全相同）。若你把格式改选 RI，则数值会变为实/虚部形式，可用 4.1.5 练习 1 的方法抽一行手算核对。
4. 以上为源码逻辑推导，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 SA 模式的 Trace 面板不能导入 S1P 文件？

**答案**：白名单由子类决定：频谱仪面板的 `supportsImportFileFormats()` 只返回 `{"csv"}`（[tracewidgetsa.h:L12](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/tracewidgetsa.h#L12)）。根本原因是数据模型不同：SA 测的是绝对功率（dBm），而 Touchstone 存的是无量纲 S 参数比值。

**练习 2**：导出对话框里端口数选 2，但只给 S11、S22 两格填了 Trace，文件会怎样？

**答案**：[tracetouchstoneexport.cpp:L110-L121](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracetouchstoneexport.cpp#L110-L121) 里缺格补 0，文件合法但 S12/S21 全为 0（RI 下是 `0.000000000000 0.000000000000`）。仿真器读到这样的文件会把器件当成"两个孤立的单端口"。

**练习 3**：`TouchstoneImport` 控件与主导入按钮各自产出什么？

**答案**：主导入（`TraceWidget::importFile`）最终向 `TraceModel` 添加若干条 `Source::File` 的 Trace；`TouchstoneImport` 只产出（可能经 `reduceTo1Port/2Port` 裁剪的）`Touchstone` 对象及其解析状态，供校准件定义、匹配网络等**消费数据本身**的模块使用，不产生任何 Trace。

### 4.3 CSV 与单位：另一条更俗但更通用的路

#### 4.3.1 概念说明

CSV 在本项目中扮演"万能侧门"：SA 模式只有它能进（功率谱没有标准文件格式的同等地位）；导出时它能携带 Touchstone 装不下的量——驻波比、阻抗、群延时、眼图参数等任意 Y 轴显示量；表格软件直接可读。

但 CSV 有个本质差异：**CSV 存的是"已经换算好的显示量"（实数），Touchstone 存的是"原始复数"**。一条 Trace 的 y 是复数，CSV 的一个格子却只能放一个 double。所以 CSV 导出让用户勾选若干 Y 轴量（Real、Imaginary、Magnitude、Phase…），每勾一个就多一列；**再导入时靠 `YAxis::reconstructValueFromYAxisType` 把若干列实数重新拼回复数**。这个"拼回去"的能力决定了：只导出 Magnitude 一列，相位就永久丢失了。

#### 4.3.2 核心流程

**CSV 的数据模型**（[csv.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/csv.h) 里就是 `Column{表头, vector<double>}` 的数组）：

- 读：第一行按分隔符切开当表头（遇到空表头即止），后续每行按列 `toDouble`，缺的格子补 0；
- 写：表头行 + 数据行，每行末尾也带一个分隔符；`setprecision(10)` **不带 fixed**，即约 10 位有效数字。

**导出流程**（`TraceCSVExport::on_buttonBox_accepted`）：

```
选中的 traces（必须同点数/同 minX maxX/同数据类型，模型层强制）
  → 第 0 列: X 值, 表头 = Trace::dataTypeToString(outputType)  ("Frequency"/"Power"/"Time"/"Time (Zero Span)")
  → 对每条 trace × 每个勾选的 Y 轴类型:
        一列, 表头 = "<Trace名>_<Y轴名>"   (如 "S11_Real", "S11_Phase")
        值 = axis.sampleToCoordinate(...)  ← 与绘图完全同源的坐标换算
  → CSV::toFile
```

**再导入流程**（`Trace::fillFromCSV`）：

```
第 0 列表头 → 判定 domain: "time"→Time, "power"→Power, "time (zero span)"→TimeZeroSpan, 其余→Frequency
逐列扫表头, 按 "<Trace名>_<Y轴名>" 的最后一个下划线拆分 → 列映射 {YAxis::Type → 列号}
逐行: y = YAxis::reconstructValueFromYAxisType({各列值})  ← 若干实数拼回复数
```

#### 4.3.3 源码精读

**① 极简 CSV 读写器** —— [csv.cpp:L15-L53](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/csv.cpp#L15-L53) 读、[L55-L82](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/csv.cpp#L55-L82) 写。三个值得注意的细节：解析对 `toDouble` 失败不报错而是静默得 0（L45-46）；表头遇到空串就停止建列（L31-34）；写文件在[csv.cpp:L62](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/csv.cpp#L62) 处 `setprecision(10)`（默认非 fixed 格式 ≈ 10 位有效数字），比 Touchstone 的 fixed 12 位小数更紧凑。分隔符默认 `,` 但作为参数可换（如 `\t`）。

**② 导出前的"同构检查"** —— [tracecsvexport.cpp:L196-L234](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracecsvexport.cpp#L196-L234)：`TraceCSVModel::updateEnabledTraces` 找第一条被勾选的 Trace 作为"基准"（点数、minX、maxX、数据类型），之后凡与基准不同的 Trace 一律禁选。原因很直白：CSV 是按行对齐的表格，不同频点的两条曲线塞进同一张表只会错位。

**③ X 列与单位** —— [tracecsvexport.cpp:L85-L93](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracecsvexport.cpp#L85-L93)：X 列直接取 `sample(i).x` 的**原始 SI 值**（频率就是 Hz，不是 MHz），表头由 [tracemath.cpp:L176-L190](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/Math/tracemath.cpp#L176-L190) 的 `dataTypeToString` 给出（"Frequency"/"Power"/"Time"/"Time (Zero Span)"）。CSV 里**没有** Touchstone 选项行那样的单位声明——单位全靠表头字符串约定，这是 CSV 格式的先天弱点。

**④ Y 列 = 绘图同源换算** —— [tracecsvexport.cpp:L95-L106](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracecsvexport.cpp#L95-L106)：每列调 `axis.sampleToCoordinate(trace->sample(i), trace, i)`——这正是 u8-l2 讲过的 XY 图把复数变显示量的同一个函数。所以"导出的 CSV"="你图上看到的数"，而非"仪器原始数"。可选列由 [L44-L49](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracecsvexport.cpp#L44-L49) 按首条 Trace 的数据域与数据源从 `YAxis::getSupported` 填充。列名 `"<Trace>_<Y轴名>"`（如 `S11_Magnitude`、`S11_Real`，名字映射见 [traceaxis.cpp:L183-L209](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L183-L209)）。

**⑤ 复数重建** —— [traceaxis.cpp:L366-L391](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L366-L391)：`reconstructValueFromYAxisType` 的规则——有 `Real` 列则实部取之、虚部取 `Imaginary` 列（缺则 0）；否则若存在 `Magnitude`/`MagnitudeLinear`/`dBuV` 之一，先化回线性幅度（dB → \(10^{dB/20}\)），相位取 `Phase` 列（缺则 0），再 `polar` 合成。即：**RI 两列或 MA/DB+Phase 两列都能完整还原复数；只导出一列则另一维信息丢失**。

**⑥ CSV 导入解析** —— [trace.cpp:L259-L332](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L259-L332)：`fillFromCSV` 用表头里最后一个 `_` 把列名拆成"Trace 名 + Y 轴名"（L266-295），同一 Trace 的多个列聚成一个 `parameter`；老版本列名 `real`/`imag` 会被翻译成新名（L287-291，向后兼容）。X 列表头判定 domain（L307-315，大小写不敏感）。逐行合成 `d.y = reconstructValueFromYAxisType(...)`（L316-325）。上层 [trace.cpp:L1110-L1133](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1110-L1133) 的 `createFromCSV` 用"列数耗尽抛异常"当循环终止条件——异常在这里被当作正常控制流使用。另外 [trace.cpp:L1006-L1018](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L1006-L1018) 显示：保存的 setup 重新载入文件型 Trace 时，也是按扩展名 `.csv` 与否走 `fillFromCSV`/`fillFromTouchstone`——**文件才是这类 Trace 的"硬盘"**。

#### 4.3.4 代码实践

**实践目标**：完成规格要求的"导出 CSV 并对比两种格式"，并验证"列选择决定信息是否完整"。

1. **操作步骤**：
   - 承接 4.2.4 的导入数据（或 4.1.4 的两点 S1P），点 Export → **CSV**；
   - 勾选一条 Trace，列勾 **Real + Imaginary + Magnitude + Phase** 四项，导出为 `full.csv`；
   - 重复一次，只勾 **Magnitude** 一项，导出为 `magonly.csv`；
   - 用文本编辑器打开两个文件看表头与数值。
2. **需要观察的现象**：
   - `full.csv` 表头形如 `Frequency,S11_Real,S11_Imaginary,S11_Magnitude,S11_Phase,`（注意每行末尾多一个逗号——`toFile` 对每列都追加分隔符）；`Frequency` 列的值是 **Hz 原值**（1 MHz 就是 `1000000`）；
   - `magonly.csv` 只有两列；
   - 把 `full.csv` 重新 Import，Marker 读数应与原 Trace 一致（复数被完整重建）；把 `magonly.csv` Import，幅度正确但**相位全为 0**。
3. **预期结果**（对照源码逐条解释）：
   - 末尾逗号 ← [csv.cpp:L64-L70](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/csv.cpp#L64-L70) 每列先写值再写 sep；
   - 相位丢失 ← [traceaxis.cpp:L386-L390](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceaxis.cpp#L386-L390) Phase 列缺失时相位取 0。
4. 以上为源码逻辑推导，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：想把一批测量发给同事用 Excel 看，同时又要保证他将来能无损导回 GUI，CSV 应该怎么导？

**答案**：勾选 **Real + Imaginary**（或 **Magnitude + Phase**）成对的列。`reconstructValueFromYAxisType` 只能从成对列恢复复数；Excel 里好看的是 Magnitude 列，但那一列单独存在时相位信息为零。最稳妥的做法是四列都导（Magnitude/Phase 给人看，Real/Imag 给机器读）。

**练习 2**：Touchstone 写文件用 `fixed << setprecision(12)`，CSV 用 `setprecision(10)`（无 fixed）。各是什么精度？对 6 GHz 频率下的 S 参数够不够？

**答案**：Touchstone 是小数点后固定 12 位；CSV 是约 10 位**有效数字**。对 |S|≤1 的复数与 ≤1e10 Hz 的频率，两者分辨率都在 \(10^{-10}\) 以下，而 VNA 实测噪声典型在 \(10^{-2}\)～\(10^{-3}\) 量级，因此两种写法都可视为无损，精度差异只在文件体积上。

**练习 3**：CSV 的 X 列单位是什么？导入时怎么知道一条 CSV 是频谱（功率域）还是时域？

**答案**：X 列写的是原始 SI 值（Hz/s），没有单位声明，单位约定完全在表头字符串里。导入时 [trace.cpp:L307-L315](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/trace.cpp#L307-L315) 按第 0 列表头大小写不敏感匹配 `time`/`power`/`time (zero span)`，都不匹配则默认 Frequency。这也解释了为什么手工造 CSV 时表头拼写必须与 `dataTypeToString` 的输出一致。

## 5. 综合实践

**任务：做一次"三格式往返 + 信息保全审计"。**

背景：你要把一次测量交付给别人，对方一会儿要用 Touchstone 进仿真器，一会儿要用 Python 画图，一会儿要贴进报告。请完成：

1. **造数据**：手工编写一份 3 点的 S2P 文件（RI 格式、MHz 单位），其中自己设计一个物理上合理的器件——例如一段 3 dB 衰减电缆：S21=S12≈0.7（即 −3.1 dB），S11=S22≈0.1，相位随频率线性增加（模拟时延，如每 GHz 转 36°）。算清楚再写。
2. **导入并验证**：导入 GUI，用 Marker 在中间频点核对 |S21| ≈ −3 dB、S11 ≈ −20 dB。
3. **导出三份**：
   - Touchstone（DB 格式、GHz 单位）；
   - CSV（Real+Imag+Magnitude+Phase 全勾）；
   - CSV（只勾 Magnitude）。
4. **审计**：用文本编辑器逐份检查，填写下表并写 200 字小结：

| | Touchstone | CSV(全列) | CSV(仅幅度) |
|---|---|---|---|
| 频率单位声明 | 选项行 | 表头约定 | 表头约定 |
| 复数完整性 | 完整 | 完整 | 丢失相位 |
| 参考阻抗 | `R 50` | 无 | 无 |
| 通用软件可读 | 射频软件 | 任何表格软件 | 任何表格软件 |

5. **无损性验证**：把 `CSV(全列)` 重新导入，对比与第 2 步的 Marker 读数；差异应在 10 位有效数字内不可见。

**预期结论**：Touchstone 是"射频界的通用语"（带元数据、无损、但只有 S 参数）；CSV 是"大众语"（人人能读、可携带任意派生量、但元数据与相位都依赖导出时的选择）。仓库自带的 `Documentation/Measurements/*.s2p` 可作为你手工文件的"文体对照样本"。全程无需硬件；运行现象**待本地验证**。

## 6. 本讲小结

- Touchstone 是纯文本的 S 参数交换标准：`# 单位 S 格式 R 阻抗` 选项行 + 数据行；本解析器**要求选项行**、只支持 S 参数、端口数取自**文件扩展名** `sNp`。
- 三种格式 DB/MA/RI 只是同一复数的不同记法，读写两侧分别做 \(10^{dB/20}\)/\(20\lg|\cdot|\) 与度/弧度换算。
- 最大的历史坑：**两端口文件中参数顺序是 S11 S21 S12 S22**，内部存储是行优先矩阵序，读写两端各有一次 `swap(S[1], S[2])` 互为镜像。
- 主导入路径 `TraceWidget::importFile` → `createFromTouchstone/CSV` → `TraceImportDialog` → `TraceModel`；`TouchstoneImport` 控件是另一个东西——服务于校准件/编辑对话框的"选文件挑端口"控件，产出 `Touchstone` 而非 Trace。
- 导出：Touchstone 侧按 `Sij` 名自动配格、缺格补 0、设置记忆进 Preferences；CSV 侧强制同构 Trace（同点数同范围同类型），每勾一个 Y 轴量出一列，列名 `<Trace>_<Y轴名>`。
- CSV 存"显示量"而非原始复数：能否无损往返取决于是否导出了成对列（Real+Imag 或 Mag+Phase），`YAxis::reconstructValueFromYAxisType` 负责拼回复数；X 列是原始 SI 值，域由第 0 列表头判定。

## 7. 下一步学习建议

- **u8-l5（Trace 数学运算框架）**：本讲多次出现的 `sampleToCoordinate`/`reconstructValueFromYAxisType` 都属于"复数 ↔ 显示量"换算族，下一讲把视角扩展到 Trace 上的数学链（TDR、DFT、时间门）。
- **提前翻一眼 u9-l1（校准件与套件）**：`TouchstoneImport` 控件四个使用点全在 `calstandard.cpp`——校准件的标准响应就是用 Touchstone 文件描述的，本讲的解析器正是它的地基。
- **动手方向**：给 `Touchstone::fromFile` 写一个"故意喂坏文件"的清单实验（缺选项行、RI 缺第二个数、非升序频率、扩展名错误），逐条核对抛出的异常消息与行号提示，这是熟悉解析器防御性编程的最快路径；有余力可参考 LibreVNA-Test 工程的风格把这份清单固化成单元测试（衔接 u11-l1）。
